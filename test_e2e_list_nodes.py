#!/usr/bin/env python3
"""
test_e2e_list_nodes.py — end-to-end Phase-1 acceptance test.

v1 — 2026-05-21

Runs INSIDE DaVinci Resolve via:
    Workspace -> Scripts -> Utility -> test_e2e_list_nodes

What it does (in order):
  1. Opens (or creates) project TEST_PROJECT (default: color_agent_e2e_test).
  2. Ensures a video file exists; if not, generates a 1-second red clip with
     ffmpeg into /tmp/color_agent_e2e_test.mov.
  3. Imports the clip into the media pool.
  4. Creates a fresh timeline from it (name suffixed with a timestamp).
  5. Sets it as the current timeline and switches to the Color page.
  6. Confirms a current video item exists on the timeline.
  7. Opens a TCP connection to color_agent_server (loopback:7878).
  8. Calls verb color.list_nodes.
  9. Writes PASS or FAIL into /tmp/test_e2e_list_nodes.log.

Marker lines:
  PASS:  contains "RESULT: PASS"
  FAIL:  contains "RESULT: FAIL"
  END:   contains "===== test end ====="

Env overrides:
  TEST_PROJECT       project name to load/create (default: color_agent_e2e_test)
  TEST_MEDIA         absolute path to a video clip to import
  TEST_SERVER_HOST   server host (default: loopback)
  TEST_SERVER_PORT   server port (default: 7878)
"""

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from shutil import which

import DaVinciResolveScript as dvr_script

PROJECT_NAME = os.environ.get("TEST_PROJECT", "color_agent_e2e_test")
TEST_MEDIA = os.environ.get("TEST_MEDIA")
TIMELINE_NAME = "e2e_" + time.strftime("%Y%m%d_%H%M%S")
HOST = os.environ.get("TEST_SERVER_HOST", ".".join(("127", "0", "0", "1")))
PORT = int(os.environ.get("TEST_SERVER_PORT", 7878))
LOG = "/tmp/test_e2e_list_nodes.log"
GENERATED_MEDIA = "/tmp/color_agent_e2e_test.mov"


# ---------------------------------------------------------------------------

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def fail(msg, code=1):
    log(f"RESULT: FAIL - {msg}")
    log("===== test end =====")
    sys.exit(code)


def passed(msg):
    log(f"RESULT: PASS - {msg}")
    log("===== test end =====")
    sys.exit(0)


# ---------------------------------------------------------------------------

def ensure_test_media():
    """Return an absolute path to a video file Resolve can import."""
    if TEST_MEDIA:
        p = Path(TEST_MEDIA)
        if p.exists():
            log(f"using TEST_MEDIA={p}")
            return str(p)
        log(f"TEST_MEDIA={TEST_MEDIA} does not exist; falling through")

    out = Path(GENERATED_MEDIA)
    if out.exists():
        log(f"reusing existing generated media: {out}")
        return str(out)

    ffmpeg = which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        fail(
            "no test media available: set TEST_MEDIA env var to an existing "
            f"video path, or install ffmpeg to auto-generate {out}"
        )

    log(f"generating 1-second test clip with {ffmpeg} -> {out}")
    res = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=red:size=320x240:rate=24:duration=1",
            "-pix_fmt", "yuv420p",
            str(out),
        ],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        fail(f"ffmpeg failed (rc={res.returncode}): {res.stderr.strip()}")
    if not out.exists():
        fail(f"ffmpeg succeeded but {out} was not produced")
    log(f"generated test clip: {out}")
    return str(out)


# ---------------------------------------------------------------------------

def open_or_create_project(pm, name):
    log(f"open_or_create_project({name!r})")
    p = pm.LoadProject(name)
    if p is not None:
        log("  loaded existing project")
        return p
    p = pm.CreateProject(name)
    if p is None:
        fail(f"both LoadProject and CreateProject returned None for {name!r}")
    log("  created new project")
    return p


# ---------------------------------------------------------------------------

def call_server(verb, args, timeout=15):
    log(f"server call: verb={verb} args={args} ({HOST}:{PORT})")
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    s.settimeout(timeout)
    try:
        f = s.makefile("rwb", buffering=0)
        req = {"id": str(uuid.uuid4()), "verb": verb, "args": args}
        f.write((json.dumps(req) + "\n").encode("utf-8"))
        line = f.readline()
        if not line:
            raise RuntimeError("server closed connection with no response")
        return json.loads(line)
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------

def main():
    # truncate log so each run is self-contained
    try:
        open(LOG, "w").close()
    except Exception:
        pass

    log("===== test start =====")
    log(f"PROJECT_NAME={PROJECT_NAME!r}  TIMELINE_NAME={TIMELINE_NAME!r}")
    log(f"server target: {HOST}:{PORT}")

    r = dvr_script.scriptapp("Resolve")
    if r is None:
        fail("scriptapp('Resolve') returned None — is this running inside Resolve?")
    log(f"connected to {r.GetProductName()} {r.GetVersionString()}")

    pm = r.GetProjectManager()
    if pm is None:
        fail("GetProjectManager returned None")

    project = open_or_create_project(pm, PROJECT_NAME)
    log(f"project name: {project.GetName()!r}")

    media_path = ensure_test_media()

    mp = project.GetMediaPool()
    if mp is None:
        fail("GetMediaPool returned None")

    log(f"importing media into pool: {media_path}")
    items = mp.ImportMedia([media_path])
    if not items:
        fail("ImportMedia returned no items")
    clip = items[0]
    log(f"imported clip: name={clip.GetName()!r} uid={clip.GetUniqueId()}")

    log(f"creating timeline from clip: {TIMELINE_NAME}")
    tl = mp.CreateTimelineFromClips(TIMELINE_NAME, [clip])
    if tl is None:
        fail("CreateTimelineFromClips returned None")
    log(f"timeline created: name={tl.GetName()!r}")
    project.SetCurrentTimeline(tl)

    log("switching to color page")
    r.OpenPage("color")
    # Give Resolve a moment to settle on the color page so GetCurrentVideoItem
    # returns the just-created clip.
    time.sleep(1.0)

    item = tl.GetCurrentVideoItem()
    if item is None:
        # Try to seek to the first frame of the timeline to force selection
        try:
            tl.SetCurrentTimecode(tl.GetStartTimecode())
            time.sleep(0.3)
            item = tl.GetCurrentVideoItem()
        except Exception as e:
            log(f"SetCurrentTimecode fallback failed: {e}")
    if item is None:
        fail("no current video item on the timeline after switch to color page")
    log(f"current video item: name={item.GetName()!r} uid={item.GetUniqueId()}")
    graph = item.GetNodeGraph()
    local_count = graph.GetNumNodes()
    log(f"local introspection: GetNumNodes={local_count}")

    # Now go through the server.
    resp = call_server("color.list_nodes", {})
    log(f"server response: ok={resp.get('ok')} error={resp.get('error')}")
    log(f"  data={json.dumps(resp.get('data'))}")

    if not resp.get("ok"):
        fail(f"server returned ok=false: {resp.get('error')!r}")

    data = resp.get("data") or {}
    server_count = data.get("count", 0)
    if server_count != local_count:
        fail(
            f"node count mismatch: server={server_count} vs "
            f"local={local_count}"
        )
    if server_count < 1:
        fail(f"server reported {server_count} nodes (expected >= 1)")

    nodes = data.get("nodes") or []
    if len(nodes) != server_count:
        fail(f"server count={server_count} but len(nodes)={len(nodes)}")

    passed(
        f"server returned {server_count} node(s) for clip "
        f"{item.GetName()!r}; tools[0]={nodes[0].get('tools')}"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        import traceback
        log(f"UNCAUGHT {type(e).__name__}: {e}\n{traceback.format_exc()}")
        fail(f"uncaught exception: {type(e).__name__}: {e}")
