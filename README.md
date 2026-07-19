# resolve-agent

Give AI agents hands inside **DaVinci Resolve Studio**.

An in-Resolve JSON-RPC server, a CLI client, an MCP shim for Claude
Code / Cursor / Warp, and an end-to-end acceptance test — together they
let any agent drive Resolve's color page: set LUTs and CDLs, walk node
graphs, copy grades across clips, manage versions and groups, export
stills.

The interesting part is *how*: macOS TCC gates Resolve's scripting
bridge by signing identity, so arbitrary external processes can't
reach it. The server therefore runs **inside** Resolve (launched from
the Scripts menu), inheriting its permission context, and exposes a
line-delimited JSON-RPC protocol on loopback that anything can speak.
See the architecture notes below for the subprocess-model discovery
this design rests on.

## TL;DR

```bash
# 1. Open Resolve, then start the server:
#    Workspace > Scripts > color_agent_server
# 2. Ping it:
./resolve-cli system.ping
# 3. Run the end-to-end acceptance test:
./test-e2e
```

## Components

| File                                            | Role                                                                                                  |
|-------------------------------------------------|-------------------------------------------------------------------------------------------------------|
| `color_agent_server.py`                         | In-Resolve TCP/JSON-RPC server. Launched from the Scripts menu. Hosts the verb catalog.               |
| `resolve-cli`                                   | External CLI client. Send any verb with `key=value` or `--json` args.                                 |
| `test_e2e_list_nodes.py`                        | In-Resolve acceptance test. Creates a project, generates/imports a clip, calls `color.list_nodes`.    |
| `test-e2e`                                      | Bash runner. Drives the test script via the menu and exits 0/1/3 on PASS / FAIL / TIMEOUT.            |
| `README.md`                                     | This file.                                                                                            |

## Architecture

```
external agent (shell / MCP host / n8n / Warp workflow)
        |
        |  line-delimited JSON-RPC, loopback:7878
        v
fuscript subprocess  -- child of Resolve, runs color_agent_server.py
        |
        |  DaVinciResolveScript over Mach ports (inherits Resolve's TCC perms)
        v
DaVinci Resolve Studio  -- color graph, media pool, project, timelines
```

### Key finding: Workspace > Scripts runs items in a short-lived subprocess

Resolve 20.3.2.9 on macOS launches each Workspace > Scripts item in a
separate `fuscript` subprocess whose parent is the main Resolve
process. The subprocess exits the moment the script's `__main__`
returns, taking any daemon threads and their bound sockets with it.

Implication: the accept loop must run on the subprocess's main
thread. `color_agent_server.start()` does the bind + listen on the
main thread and then calls `_accept_loop(srv)` synchronously. The
`fuscript` subprocess then lives as long as the loop does, hosting the
listener. Resolve's UI is unaffected because the blocking is in the
child, not in Resolve itself.

The handoff document's assumption that "scripts launched from
Workspace > Scripts run in-process" does NOT hold for this Resolve
build. The subprocess model is what the rest of the design rests on.

### Why a server inside Resolve, not direct external Python

macOS TCC gates the Mach-port scripting bridge by responsible-binary
signing identity. Arbitrary terminal processes calling
`DaVinciResolveScript.scriptapp("Resolve")` get `None` back. Scripts
launched via the Scripts menu (and therefore the `fuscript` child)
inherit Resolve's permission context, so they can reach the bridge.

### Other architectural notes

- Handles refreshed per request. Every verb starts with `ctx()`, which
  re-resolves Resolve -> Project -> Timeline -> TimelineItem. Stale
  handles after project/timeline switch are not a problem.
- Per-clip mutation lock. A `dict[clip_uid] -> threading.Lock`
  serializes concurrent mutations against the same node graph.
- Daemon-thread heartbeat + per-client handler threads. Heartbeat
  logs every 5s so we can see the accept loop is alive. Each accepted
  connection is handled in its own daemon thread (these are fine
  because the subprocess main thread is blocking forever on `accept`).

### Subprocess pid life-cycle

```
Resolve (main, e.g. PID 98196)
  +-- fuscript (e.g. PID 99503)        <- hosts the listener
        +-- color-agent-heartbeat      (daemon)
        +-- per-client handler threads (daemon)
```

When you re-click `Workspace > Scripts > color_agent_server`, Resolve
spawns a new `fuscript`. If a previous one is still listening on 7878
the new bind fails -- this is logged as
`FATAL: bind(...) failed - OSError 48: ...` and the old listener
keeps serving. To restart the server: quit Resolve or kill the
existing `fuscript` pid that owns port 7878.

## Setup

### One-time prerequisites

1. External scripting enabled: Resolve -> Preferences -> System ->
   General -> "External scripting using" = `Local` (or `Network` if
   driving from another host).
2. Accessibility permission for whatever terminal / shell you run the
   test runner from. System Settings -> Privacy & Security ->
   Accessibility. Required only for `test-e2e` (which uses
   `osascript` + System Events to drive the Scripts menu). Not
   required for `resolve-cli` once the server is running.
3. ffmpeg (only needed by `test-e2e` if no `TEST_MEDIA` is provided):
   `brew install ffmpeg`.

### Install the in-Resolve scripts

```bash
TARGET="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
cp color_agent_server.py    "$TARGET/"
cp test_e2e_list_nodes.py   "$TARGET/"
```

Restart Resolve so it re-scans the Scripts directory (or just open
the `Workspace > Scripts` menu -- re-scanning happens on open in most
builds).

### Run the server

`Workspace > Scripts > color_agent_server`

Confirm it bound the port:

```bash
tail -f /tmp/color_agent_server.log
```

Expected tail:

```
[...] connected to DaVinci Resolve Studio <ver>, page=<page>
[...] bind ok on <loopback>:7878
[...] color_agent_server listening on <loopback>:7878
[...] entering accept loop on main thread (subprocess will block here)
[...] heartbeat #1: alive, getsockname=(...), fileno=18, pid=<PID>
```

Or use `lsof` to see the listener directly:

```bash
lsof -nP -iTCP:7878 -sTCP:LISTEN
# COMMAND    PID USER   FD   TYPE       DEVICE SIZE/OFF NODE NAME
# fuscript <PID>  raa  18u  IPv4 ...               TCP loopback:7878 (LISTEN)
```

## Driving the server

### `resolve-cli`

```bash
./resolve-cli system.ping
./resolve-cli color.context
./resolve-cli color.list_nodes
./resolve-cli color.list_luts filter=arri
./resolve-cli color.set_lut node=1 path="/Library/.../some.cube"
./resolve-cli color.set_cdl node=1 slope=[1.0,0.95,0.9] sat=1.0

# Anything non-trivial: use --json
./resolve-cli color.copy_grades --json '{"to_clip_ids":["<uid1>","<uid2>"]}'
```

Env overrides:

- `RESOLVE_AGENT_HOST` (default loopback)
- `RESOLVE_AGENT_PORT` (default 7878)
- `RESOLVE_AGENT_TIMEOUT` (default 30s)

### Raw protocol

Line-delimited JSON over TCP. One request per line, one response per
line, request-response correlated by an `id` you provide.

Request:
```
{"id":"...","verb":"color.set_lut","args":{"node":1,"path":"/.../foo.cube"}}
```
Response:
```
{"id":"...","ok":true,"data":{"set":true,"node":1,"path":"...","readback":"foo.cube"},"error":null}
```

### MCP (Claude Code / Cursor / Warp)

`mcp/resolve_color_mcp.py` is a FastMCP stdio shim that wraps the 23-verb
catalog as MCP tools. Each tool opens a TCP connection to
`color_agent_server`, sends the JSON-RPC request, and returns the `data`
field. The in-Resolve server must still be running — the shim is just a
protocol translator.

One-time setup:

```bash
python3 -m venv mcp/.venv
mcp/.venv/bin/pip install -r mcp/requirements.txt
```

Register with Claude Code (user scope, available in every session):

```bash
claude mcp add --scope user resolve-color \
  "$(pwd)/mcp/.venv/bin/python" \
  "$(pwd)/mcp/resolve_color_mcp.py"
claude mcp list   # should show `resolve-color: ... - ✓ Connected`
```

Tools surface in Claude Code as `mcp__resolve-color__<tool>` — e.g.
`mcp__resolve-color__system_ping`, `mcp__resolve-color__color_context`,
`mcp__resolve-color__color_set_lut`. Naming is 1:1 with the verb catalog
below (dots replaced with underscores).

Same env overrides as `resolve-cli`: `RESOLVE_AGENT_HOST`,
`RESOLVE_AGENT_PORT`, `RESOLVE_AGENT_TIMEOUT`.

## Verb catalog (v1)

| Verb                     | Args                                                            | Backed by                          |
|--------------------------|-----------------------------------------------------------------|------------------------------------|
| `system.ping`            | -                                                               | sanity, product+version, verb list |
| `system.get_setting`     | `scope?` (project\|timeline), `key?`                            | `Project.GetSetting` / `Timeline.GetSetting` |
| `system.set_setting`     | `key`, `value`, `scope?` (project\|timeline)                    | `Project.SetSetting` / `Timeline.SetSetting` |
| `color.context`          | -                                                               | page, project, timeline, clip      |
| `color.list_nodes`       | -                                                               | `Graph.GetNumNodes` + per-node info|
| `color.set_lut`          | `node`, `path`                                                  | `Graph.SetLUT`                     |
| `color.get_lut`          | `node`                                                          | `Graph.GetLUT`                     |
| `color.set_node_enabled` | `node`, `enabled`                                               | `Graph.SetNodeEnabled`             |
| `color.set_cdl`          | `node`, `slope?`, `offset?`, `power?`, `sat?`                   | `TimelineItem.SetCDL`              |
| `color.reset_grades`     | -                                                               | `Graph.ResetAllGrades`             |
| `color.list_luts`        | `filter?`                                                       | filesystem walk of LUT roots       |
| `color.refresh_luts`     | -                                                               | `Project.RefreshLUTList`           |
| `color.add_version`      | `name`, `type?` (0=local, 1=remote)                             | `TimelineItem.AddVersion`          |
| `color.load_version`     | `name`, `type?`                                                 | `TimelineItem.LoadVersionByName`   |
| `color.copy_grades`      | `to_clip_ids[]`, `from_clip_id?`                                | `TimelineItem.CopyGrades`          |
| `color.list_clips`       | `track_type?`, `track_index?`                                   | `Timeline.GetItemListInTrack` walk |
| `color.set_lut_many`     | `clip_uids[]`, `path`, `node?`                                  | per-clip `Graph.SetLUT`            |
| `color.set_lut_timeline` | `path`, `node?`, `track_index?`                                 | per-clip `Graph.SetLUT` across track|
| `color.apply_drx`        | `path`, `mode?`                                                 | `Graph.ApplyGradeFromDRX`          |
| `color.assign_group`     | `group_name`, `create_if_missing?`                              | `TimelineItem.AssignToColorGroup`  |
| `color.export_lut`       | `path`, `export_type?`                                          | `TimelineItem.ExportLUT`           |
| `color.export_still`     | `path`                                                          | `Project.ExportCurrentFrameAsStill`|
| `color.list_powergrades` | -                                                               | `Gallery.GetGalleryPowerGradeAlbums`|

All responses share the same envelope: `{id, ok, data, error}`.

## Acceptance test (`test-e2e`)

```bash
./test-e2e
```

What it does:

1. Truncates `/tmp/test_e2e_list_nodes.log`.
2. Clicks `Workspace > Scripts > test_e2e_list_nodes` via `osascript`
   + System Events.
3. Polls the log for `===== test end =====` (90s timeout, override
   with `TIMEOUT_SEC=...`).
4. Prints the log and exits 0 (PASS) / 1 (FAIL) / 2 (osascript
   failed) / 3 (timeout).

The in-Resolve side does:

1. Open (or create) project `color_agent_e2e_test` (override with
   `TEST_PROJECT=<name>`).
2. Ensure a clip exists at `TEST_MEDIA` or generate
   `/tmp/color_agent_e2e_test.mov` via ffmpeg.
3. Import it, create timeline `e2e_<timestamp>`, switch to Color page.
4. Confirm a current video item, count nodes locally.
5. Call `color.list_nodes` via TCP and confirm the server's count
   matches the local count and is >= 1.
6. Write `RESULT: PASS - ...` or `RESULT: FAIL - ...` to the test
   log.

Env overrides for the in-Resolve script:

- `TEST_PROJECT` (default `color_agent_e2e_test`)
- `TEST_MEDIA` (default: generated `/tmp/color_agent_e2e_test.mov`)
- `TEST_SERVER_HOST` (default loopback)
- `TEST_SERVER_PORT` (default 7878)

Last successful run produced:

```
[...] server response: ok=True error=None
[...]   data={"count": 1, "nodes": [{"index": 1, "label": "", "tools": [], "lut": ""}]}
[...] RESULT: PASS - server returned 1 node(s) for clip 'color_agent_e2e_test.mov'; tools[0]=[]
[test-e2e] PASS
```

## Logs

- `/tmp/color_agent_server.log` -- server activity (bind, accept,
  heartbeats, per-request errors).
- `/tmp/test_e2e_list_nodes.log` -- last test run.

## Risk / fragility log

| Item                                                       | Mitigation                                                                              |
|------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `fuscript` subprocess model could change in future Resolve | Synchronous accept loop also works in-process; heartbeat reveals interpreter teardown.   |
| Re-clicking the server menu while a `fuscript` already listens | New `fuscript` logs `bind(...) failed - OSError 48`; existing listener keeps serving.  |
| `osascript` menu drive needs Accessibility perm            | Only relevant to `test-e2e`. `resolve-cli` does not need it once the server is running.  |
| Project / timeline references go stale on switch           | `ctx()` re-resolves all four handles at the start of every request.                      |
| Concurrent mutations on the same clip                      | `dict[clip_uid] -> threading.Lock` serializes per-clip.                                  |
| Encoding of log lines in restricted child locale           | `open(..., encoding="utf-8", errors="replace")` used in test log; server log is ASCII.   |

## Status

- Phase 1.1 -- server: **done**
- Phase 1.2 -- verb catalog v1: **done** (23 verbs)
- Phase 1.3 -- CLI client: **done**
- Phase 1.4 -- launch docs: **done**
- Phase 1.5 -- concurrency smoke: **done** (3 concurrent clients cycled cleanly through accept loop)
- Phase 1.6 -- end-to-end acceptance test: **done** (`./test-e2e` PASSes)
- Phase 2.6 -- MCP server shim: **done** (`mcp/resolve_color_mcp.py`, registered with Claude Code as `resolve-color`)

## Next

- Phase 2.7 -- n8n HTTP node (thin FastAPI in front of the socket).
- Phase 3.9 -- probe `Fusion.ActionManager` / `MenuManager` to discover
  action IDs (L3 fallback, e.g. for `Add Serial Node`).
- Phase 3.10 -- scope-data emulation via `ExportCurrentFrameAsStill`
  -> numpy.
- Phase 4.13 -- Resolve `-nogui` headless mode on the Mac Studio,
  reusing the same socket server.
