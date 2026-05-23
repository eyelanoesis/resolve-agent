#!/usr/bin/env python3
"""
color_agent_server.py — in-Resolve JSON-RPC listener for agentic color control.

v1 — 2026-05-21

Runs INSIDE DaVinci Resolve. Launch via:
    Workspace -> Scripts -> Utility -> color_agent_server

Protocol (line-delimited JSON over TCP on 127.0.0.1:7878):
    request : {"id": "...", "verb": "color.set_lut", "args": {...}}
    response: {"id": "...", "ok": true|false, "data": ..., "error": "..."}

Design notes (see resolve-agent-handoff.pdf §5, §7, §9):
 * Server runs as a daemon thread so the Scripts-menu invocation returns
   immediately and Resolve's UI stays responsive.
 * Handles to Resolve/Project/Timeline/TimelineItem are re-resolved at the
   start of every request to survive project/timeline switches.
 * Mutation calls are serialized per-clip via a per-uniqueId lock so
   concurrent agents can't interleave on the same node graph.
 * Verb catalog implements §5.2 v1 plus a system.ping healthcheck.
"""

import json
import os
import socket
import subprocess
import threading
import time
import traceback
from pathlib import Path

# DaVinciResolveScript is importable when this script runs from Resolve's
# Scripts menu (PYTHONPATH is pre-configured by Resolve).
import DaVinciResolveScript as dvr_script  # noqa: E402

HOST = "127.0.0.1"
PORT = 7878
LOG_PATH = "/tmp/color_agent_server.log"

# LUT roots confirmed on this machine (see handoff §4.1). Add more here if
# you drop LUT libraries elsewhere.
LUT_ROOTS = [
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT",
    os.path.expanduser(
        "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT"
    ),
    os.path.expanduser("~/Library/Application Support/Adobe/Common/LUTs"),
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        try:
            with open(LOG_PATH, "a") as lf:
                lf.write(line + "\n")
        except Exception:
            pass
    try:
        print(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handle resolution — re-fetched on every request (handoff §9)
# ---------------------------------------------------------------------------

_resolve_singleton = None


def get_resolve():
    """Return a live Resolve handle, validating and refreshing the cache.

    Validation: call GetProductName() — if it returns None or raises,
    the cache is stale (Resolve quit/crashed or the Mach port died).
    Drop the cache and try scriptapp() again. If that also fails,
    raise a clean RuntimeError pointing the user at the restart path.
    """
    global _resolve_singleton

    if _resolve_singleton is not None:
        try:
            if _resolve_singleton.GetProductName() is not None:
                return _resolve_singleton
        except Exception:
            pass
        _resolve_singleton = None  # stale; fall through to re-fetch

    _resolve_singleton = dvr_script.scriptapp("Resolve")
    if _resolve_singleton is None or _resolve_singleton.GetProductName() is None:
        _resolve_singleton = None
        raise RuntimeError(
            "Cannot reach DaVinci Resolve. Either Resolve is not running, "
            "or this fuscript subprocess has been orphaned from a quit/crashed "
            "Resolve. Fix: relaunch Resolve and restart the server via "
            "Workspace > Scripts > color_agent_server "
            "(killing any orphan fuscript holding port 7878 first)."
        )
    return _resolve_singleton


def ctx():
    """Re-resolve all handles. Cheap. Run at the start of every request."""
    r = get_resolve()
    pm = r.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    timeline = project.GetCurrentTimeline() if project else None
    item = timeline.GetCurrentVideoItem() if timeline else None
    return {
        "resolve": r,
        "project_manager": pm,
        "project": project,
        "timeline": timeline,
        "item": item,
    }


def require_item(c):
    item = c["item"]
    if not item:
        raise RuntimeError("no current video item on the timeline")
    return item


def require_project(c):
    project = c["project"]
    if not project:
        raise RuntimeError("no current project")
    return project


def require_timeline(c):
    timeline = c["timeline"]
    if not timeline:
        raise RuntimeError("no current timeline")
    return timeline


# ---------------------------------------------------------------------------
# Per-clip mutation lock
# ---------------------------------------------------------------------------

_clip_locks = {}
_clip_locks_guard = threading.Lock()


def clip_lock(clip_uid):
    with _clip_locks_guard:
        lock = _clip_locks.get(clip_uid)
        if lock is None:
            lock = threading.Lock()
            _clip_locks[clip_uid] = lock
    return lock


# ---------------------------------------------------------------------------
# Verb registry
# ---------------------------------------------------------------------------

VERBS = {}


def verb(name):
    def deco(fn):
        VERBS[name] = fn
        return fn

    return deco


# ---------- system ---------------------------------------------------------


@verb("system.ping")
def v_ping(args):
    r = get_resolve()
    return {
        "product": r.GetProductName(),
        "version": r.GetVersionString(),
        "page": r.GetCurrentPage(),
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verbs": sorted(VERBS.keys()),
    }


@verb("system.get_setting")
def v_get_setting(args):
    """Get a project or timeline setting.

    args:
      scope: 'project' (default) or 'timeline'
      key:   optional setting name; omit for the full dict of all keys
    """
    scope = args.get("scope", "project")
    key = args.get("key")
    c = ctx()
    obj = require_timeline(c) if scope == "timeline" else require_project(c)
    value = obj.GetSetting(key) if key else obj.GetSetting()
    return {"scope": scope, "key": key, "value": value}


@verb("system.set_setting")
def v_set_setting(args):
    """Set a project or timeline setting.

    args:
      key:   setting name (required)
      value: new value (coerced to str — Resolve's SetSetting expects strings)
      scope: 'project' (default) or 'timeline'
    """
    key = args["key"]
    value = str(args["value"])
    scope = args.get("scope", "project")
    c = ctx()
    obj = require_timeline(c) if scope == "timeline" else require_project(c)
    ok = obj.SetSetting(key, value)
    return {"scope": scope, "key": key, "value": value, "set": bool(ok)}


# ---------- context / introspection ---------------------------------------


@verb("color.context")
def v_context(args):
    c = ctx()
    project = c["project"]
    timeline = c["timeline"]
    item = c["item"]
    snap = {
        "page": c["resolve"].GetCurrentPage(),
        "project": project.GetName() if project else None,
        "timeline": timeline.GetName() if timeline else None,
        "clip": item.GetName() if item else None,
        "clip_uid": item.GetUniqueId() if item else None,
        "num_nodes": item.GetNodeGraph().GetNumNodes() if item else 0,
    }
    return snap


@verb("color.list_nodes")
def v_list_nodes(args):
    c = ctx()
    item = require_item(c)
    g = item.GetNodeGraph()
    n = g.GetNumNodes()
    nodes = []
    for i in range(1, n + 1):
        nodes.append(
            {
                "index": i,
                "label": g.GetNodeLabel(i),
                "tools": list(g.GetToolsInNode(i) or []),
                "lut": g.GetLUT(i),
            }
        )
    return {"count": n, "nodes": nodes}


# ---------- L1: property mutation -----------------------------------------


@verb("color.set_lut")
def v_set_lut(args):
    node = int(args["node"])
    path = args["path"]
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.GetNodeGraph().SetLUT(node, path)
        readback = item.GetNodeGraph().GetLUT(node)
    return {"set": bool(ok), "node": node, "path": path, "readback": readback}


@verb("color.get_lut")
def v_get_lut(args):
    node = int(args["node"])
    c = ctx()
    item = require_item(c)
    return {"node": node, "lut": item.GetNodeGraph().GetLUT(node)}


@verb("color.set_node_enabled")
def v_set_node_enabled(args):
    node = int(args["node"])
    enabled = bool(args["enabled"])
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.GetNodeGraph().SetNodeEnabled(node, enabled)
    return {"set": bool(ok), "node": node, "enabled": enabled}


@verb("color.set_cdl")
def v_set_cdl(args):
    """args: {node, slope, offset, power, sat|saturation}.

    slope/offset/power may be either a 3-tuple ([r,g,b]) or a space-separated
    string ("0.5 0.4 0.2") — Resolve's SetCDL wants strings, so we normalize.
    """
    node = int(args["node"])

    def norm(v):
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v)
        return str(v)

    cdl = {"NodeIndex": str(node)}
    if "slope" in args:
        cdl["Slope"] = norm(args["slope"])
    if "offset" in args:
        cdl["Offset"] = norm(args["offset"])
    if "power" in args:
        cdl["Power"] = norm(args["power"])
    sat = args.get("sat", args.get("saturation"))
    if sat is not None:
        cdl["Saturation"] = norm(sat)

    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.SetCDL(cdl)
    return {"set": bool(ok), "cdl": cdl}


@verb("color.reset_grades")
def v_reset_grades(args):
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.GetNodeGraph().ResetAllGrades()
    return {"reset": bool(ok)}


# ---------- LUT library ----------------------------------------------------


@verb("color.list_luts")
def v_list_luts(args):
    filt = (args.get("filter") or "").lower()
    results = []
    seen = set()
    for root in LUT_ROOTS:
        rootp = Path(root)
        if not rootp.exists():
            continue
        for p in rootp.rglob("*.cube"):
            sp = str(p)
            if sp in seen:
                continue
            if filt and filt not in p.name.lower():
                continue
            seen.add(sp)
            results.append(sp)
    return {"count": len(results), "luts": results, "roots": LUT_ROOTS}


@verb("color.refresh_luts")
def v_refresh_luts(args):
    c = ctx()
    project = require_project(c)
    ok = project.RefreshLUTList()
    return {"refreshed": bool(ok)}


# ---------- L2: grade composition -----------------------------------------


@verb("color.add_version")
def v_add_version(args):
    name = args["name"]
    vtype = int(args.get("type", 0))  # 0=local, 1=remote
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.AddVersion(name, vtype)
    return {"added": bool(ok), "name": name, "type": vtype}


@verb("color.load_version")
def v_load_version(args):
    name = args["name"]
    vtype = int(args.get("type", 0))
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.LoadVersionByName(name, vtype)
    return {"loaded": bool(ok), "name": name, "type": vtype}


@verb("color.copy_grades")
def v_copy_grades(args):
    """Copy the source clip's current grade to a list of target clip uids.

    args: {to_clip_ids: [uid,...], from_clip_id?: uid}
    Defaults source to the current video item.
    """
    to_ids = set(args["to_clip_ids"])
    c = ctx()
    timeline = require_timeline(c)

    # walk video tracks once, collect source + targets
    targets = []
    src = c["item"]
    want_src = args.get("from_clip_id")
    for ti in range(1, timeline.GetTrackCount("video") + 1):
        for it in timeline.GetItemListInTrack("video", ti) or []:
            try:
                uid = it.GetUniqueId()
            except Exception:
                continue
            if uid in to_ids:
                targets.append(it)
            if want_src and uid == want_src:
                src = it
    if src is None:
        raise RuntimeError("no source clip resolved")

    with clip_lock(src.GetUniqueId()):
        ok = src.CopyGrades(targets)
    return {
        "copied": bool(ok),
        "from": src.GetUniqueId(),
        "to_count": len(targets),
    }


@verb("color.apply_drx")
def v_apply_drx(args):
    """mode: 0=no keyframes, 1=source TC aligned, 2=start frames aligned."""
    path = args["path"]
    mode = int(args.get("mode", 0))
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.GetNodeGraph().ApplyGradeFromDRX(path, mode)
    return {"applied": bool(ok), "path": path, "mode": mode}


@verb("color.assign_group")
def v_assign_group(args):
    group_name = args["group_name"]
    create_if_missing = bool(args.get("create_if_missing", True))
    c = ctx()
    project = require_project(c)
    item = require_item(c)

    grp = None
    for g in project.GetColorGroupsList() or []:
        if g.GetName() == group_name:
            grp = g
            break
    if grp is None:
        if not create_if_missing:
            raise RuntimeError(f"color group not found: {group_name}")
        grp = project.AddColorGroup(group_name)
        if grp is None:
            raise RuntimeError(f"failed to create color group: {group_name}")

    with clip_lock(item.GetUniqueId()):
        ok = item.AssignToColorGroup(grp)
    return {"assigned": bool(ok), "group": group_name}


@verb("color.export_lut")
def v_export_lut(args):
    """Bake the current grade to a .cube on disk.

    args: {path, export_type?}
    export_type is the Resolve LUT-size enum; defaults to 1 (33-point cube).
    """
    path = args["path"]
    export_type = int(args.get("export_type", 1))
    c = ctx()
    item = require_item(c)
    with clip_lock(item.GetUniqueId()):
        ok = item.ExportLUT(export_type, path)
    return {"exported": bool(ok), "path": path, "export_type": export_type}


# ---------- L2.5: multi-clip ops ------------------------------------------


@verb("color.list_clips")
def v_list_clips(args):
    """List clips on the current timeline.

    args:
      track_type:  'video' (default) or 'audio' or 'subtitle'
      track_index: 1-based; omit to walk all tracks of track_type
    """
    track_type = args.get("track_type", "video")
    track_index = args.get("track_index")
    c = ctx()
    timeline = require_timeline(c)

    if track_index is not None:
        tracks = [int(track_index)]
    else:
        tracks = list(range(1, timeline.GetTrackCount(track_type) + 1))

    clips = []
    for ti in tracks:
        for item in timeline.GetItemListInTrack(track_type, ti) or []:
            try:
                clips.append(
                    {
                        "uid": item.GetUniqueId(),
                        "name": item.GetName(),
                        "track_type": track_type,
                        "track_index": ti,
                    }
                )
            except Exception as e:
                clips.append(
                    {"error": f"{type(e).__name__}: {e}", "track_index": ti}
                )
    return {"count": len(clips), "track_type": track_type, "clips": clips}


@verb("color.set_lut_many")
def v_set_lut_many(args):
    """Apply a LUT to node N of multiple clips identified by uid.

    args:
      clip_uids: list of TimelineItem uids
      node:      1-based node index (default 1)
      path:      LUT path (absolute, or relative to a known LUT root)
    """
    clip_uids = set(args["clip_uids"])
    node = int(args.get("node", 1))
    path = args["path"]
    c = ctx()
    timeline = require_timeline(c)

    results = []
    found_uids = set()
    for ti in range(1, timeline.GetTrackCount("video") + 1):
        for item in timeline.GetItemListInTrack("video", ti) or []:
            try:
                uid = item.GetUniqueId()
            except Exception:
                continue
            if uid not in clip_uids:
                continue
            found_uids.add(uid)
            with clip_lock(uid):
                ok = item.GetNodeGraph().SetLUT(node, path)
            results.append(
                {
                    "uid": uid,
                    "name": item.GetName(),
                    "track_index": ti,
                    "set": bool(ok),
                }
            )

    missing = sorted(clip_uids - found_uids)
    return {
        "applied": len(results),
        "succeeded": sum(1 for r in results if r["set"]),
        "failed": sum(1 for r in results if not r["set"]),
        "missing_uids": missing,
        "node": node,
        "path": path,
        "results": results,
    }


@verb("color.set_lut_timeline")
def v_set_lut_timeline(args):
    """Convenience: apply a LUT to node N of every clip on a video track.

    args:
      path:        LUT path (required)
      node:        1-based node index (default 1)
      track_index: 1-based; omit to apply across all video tracks
    """
    path = args["path"]
    node = int(args.get("node", 1))
    track_index = args.get("track_index")
    c = ctx()
    timeline = require_timeline(c)

    if track_index is not None:
        tracks = [int(track_index)]
    else:
        tracks = list(range(1, timeline.GetTrackCount("video") + 1))

    results = []
    for ti in tracks:
        for item in timeline.GetItemListInTrack("video", ti) or []:
            try:
                uid = item.GetUniqueId()
            except Exception:
                continue
            with clip_lock(uid):
                ok = item.GetNodeGraph().SetLUT(node, path)
            results.append(
                {
                    "uid": uid,
                    "name": item.GetName(),
                    "track_index": ti,
                    "set": bool(ok),
                }
            )
    return {
        "applied": len(results),
        "succeeded": sum(1 for r in results if r["set"]),
        "failed": sum(1 for r in results if not r["set"]),
        "tracks": tracks,
        "node": node,
        "path": path,
        "results": results,
    }


# ---------- L1.5: previews / gallery --------------------------------------


@verb("color.export_still")
def v_export_still(args):
    path = args["path"]
    c = ctx()
    project = require_project(c)
    ok = project.ExportCurrentFrameAsStill(path)
    return {"exported": bool(ok), "path": path}


@verb("color.list_powergrades")
def v_list_powergrades(args):
    c = ctx()
    project = require_project(c)
    gallery = project.GetGallery()
    if not gallery:
        return {"count": 0, "albums": []}
    albums = gallery.GetGalleryPowerGradeAlbums() or []
    out = []
    for a in albums:
        try:
            name = gallery.GetAlbumName(a)
        except Exception:
            name = None
        try:
            stills = a.GetStills() or []
        except Exception:
            stills = []
        out.append({"name": name, "still_count": len(stills)})
    return {"count": len(out), "albums": out}


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def handle_client(conn, addr):
    log(f"client connected: {addr}")
    try:
        f = conn.makefile("rwb", buffering=0)
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            resp = {"ok": False, "id": None, "data": None, "error": None}
            try:
                req = json.loads(line)
                resp["id"] = req.get("id")
                verb_name = req.get("verb") or req.get("method")
                args = req.get("args") or req.get("params") or {}
                fn = VERBS.get(verb_name)
                if fn is None:
                    resp["error"] = f"unknown verb: {verb_name!r}"
                else:
                    resp["data"] = fn(args)
                    resp["ok"] = True
            except Exception as e:
                resp["error"] = f"{type(e).__name__}: {e}"
                log(
                    f"error handling request: {resp['error']}\n"
                    f"{traceback.format_exc()}"
                )
            payload = (json.dumps(resp) + "\n").encode("utf-8")
            try:
                f.write(payload)
            except Exception as werr:
                log(f"write failed to {addr}: {werr}")
                break
    except Exception as e:
        log(f"client {addr} loop error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        log(f"client disconnected: {addr}")


def _heartbeat(srv):
    """Log every 5s so we can tell if the daemon thread is still alive."""
    n = 0
    while True:
        n += 1
        try:
            sn = srv.getsockname()
            fno = srv.fileno()
        except Exception as e:
            log(f"heartbeat #{n}: socket dead — {type(e).__name__}: {e}")
            return
        log(f"heartbeat #{n}: alive, getsockname={sn}, fileno={fno}, pid={os.getpid()}")
        time.sleep(5)


def _accept_loop(srv):
    log(f"accept loop entered (pid={os.getpid()}, tid={threading.get_ident()})")
    log(f"socket getsockname()={srv.getsockname()}, fileno()={srv.fileno()}")
    # Self-introspect: run lsof on our own pid to see what the KERNEL thinks.
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-p", str(os.getpid()), "-iTCP"],
            capture_output=True, text=True, timeout=5,
        )
        log(f"self-lsof stdout:\n{out.stdout}")
        if out.stderr:
            log(f"self-lsof stderr: {out.stderr}")
    except Exception as e:
        log(f"self-lsof failed: {type(e).__name__}: {e}")
    log(f"verbs: {sorted(VERBS.keys())}")
    # Start heartbeat in another daemon thread so we can see the accept loop is alive.
    threading.Thread(target=_heartbeat, args=(srv,),
                     name="color-agent-heartbeat", daemon=True).start()
    while True:
        try:
            conn, addr = srv.accept()
        except Exception as e:
            log(f"accept failed: {type(e).__name__}: {e}")
            time.sleep(0.5)
            continue
        log(f"accept got conn from {addr}")
        t = threading.Thread(
            target=handle_client, args=(conn, addr), daemon=True
        )
        t.start()


# Module-level guards. Survive script re-invocation in the same Python session.
_SERVER_THREAD = None
_SERVER_SOCKET = None


def start():
    """Bind the socket on the *main* (Scripts-menu) thread so any failure is
    visible immediately. Only the accept() loop runs in a daemon thread.
    """
    global _SERVER_THREAD, _SERVER_SOCKET
    if _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
        log("server thread already running — skipping start()")
        return _SERVER_THREAD

    # Touch Resolve once so failures surface immediately to the Scripts menu.
    try:
        r = get_resolve()
        log(
            f"connected to {r.GetProductName()} {r.GetVersionString()}, "
            f"page={r.GetCurrentPage()}"
        )
    except Exception as e:
        log(f"FATAL connecting to Resolve: {e}")
        raise

    log(f"creating server socket on main thread (HOST={HOST!r}, PORT={PORT})")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    log("socket() ok")
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    log("setsockopt SO_REUSEADDR ok")
    try:
        srv.bind((HOST, PORT))
    except BaseException as e:
        log(
            f"FATAL: bind({HOST}:{PORT}) failed — "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        try:
            srv.close()
        except Exception:
            pass
        raise  # surfaces to the Scripts menu error dialog
    log(f"bind ok on {HOST}:{PORT}")
    srv.listen(8)
    log(f"color_agent_server listening on {HOST}:{PORT}")

    _SERVER_SOCKET = srv
    # IMPORTANT: Resolve runs Workspace > Scripts items in a *short-lived
    # subprocess* (parent = Resolve PID, child exits when the script returns).
    # Daemon threads die with that subprocess. So we run the accept loop
    # SYNCHRONOUSLY on the calling thread — the subprocess then lives as long
    # as the loop does, hosting our listener. Resolve's UI is unaffected
    # because the blocking is in the child process, not in Resolve.
    log("entering accept loop on main thread (subprocess will block here)")
    _accept_loop(srv)
    log("accept loop returned — should not happen")


if __name__ == "__main__":
    start()
