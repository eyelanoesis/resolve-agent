#!/usr/bin/env python3
"""
resolve_color_mcp.py - MCP shim for the in-Resolve color_agent_server.

v1 - 2026-05-22

Exposes the verb catalog (see ../README.md "Verb catalog (v1)") as
MCP tools. Each tool opens a TCP connection to color_agent_server
(loopback:7878 by default), sends a JSON-RPC request, and returns the
`data` field. Server errors are surfaced as Python exceptions so the
MCP client receives a proper tool-error response.

Run via stdio transport (the MCP default). The color_agent_server must
be running inside DaVinci Resolve:
    Workspace > Scripts > color_agent_server

Environment overrides:
    RESOLVE_AGENT_HOST     (default loopback)
    RESOLVE_AGENT_PORT     (default 7878)
    RESOLVE_AGENT_TIMEOUT  (default 30 seconds)
"""

import json
import os
import socket
import uuid
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

DEFAULT_HOST = ".".join(("127", "0", "0", "1"))
HOST = os.environ.get("RESOLVE_AGENT_HOST", DEFAULT_HOST)
PORT = int(os.environ.get("RESOLVE_AGENT_PORT", 7878))
TIMEOUT = float(os.environ.get("RESOLVE_AGENT_TIMEOUT", 30))

mcp = FastMCP("resolve-color")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _call(verb: str, args: dict[str, Any]) -> Any:
    """Send a JSON-RPC request to color_agent_server and return data.

    Raises RuntimeError on transport failures and on ok=false responses.
    """
    req = {"id": str(uuid.uuid4()), "verb": verb, "args": args}
    try:
        sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise RuntimeError(
            f"could not reach color_agent_server at {HOST}:{PORT}: "
            f"{type(e).__name__}: {e}. Launch from Resolve: "
            "Workspace > Scripts > color_agent_server"
        ) from e

    sock.settimeout(TIMEOUT)
    try:
        f = sock.makefile("rwb", buffering=0)
        f.write((json.dumps(req) + "\n").encode("utf-8"))
        line = f.readline()
        if not line:
            raise RuntimeError("server closed connection with no response")
        resp = json.loads(line)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    if not resp.get("ok"):
        raise RuntimeError(f"server error: {resp.get('error') or 'unknown'}")
    return resp.get("data")


# ---------------------------------------------------------------------------
# Tool catalog - 1:1 with color_agent_server verbs
# ---------------------------------------------------------------------------

# ---------- system --------------------------------------------------------

@mcp.tool()
def system_ping() -> dict:
    """Sanity check. Returns product, version, current page, and verbs."""
    return _call("system.ping", {})


@mcp.tool()
def system_get_setting(
    scope: str = "project", key: Optional[str] = None
) -> dict:
    """Get a project or timeline setting.

    scope: 'project' (default) or 'timeline'.
    key: setting name; omit for the full dict of all keys.
    """
    args: dict[str, Any] = {"scope": scope}
    if key:
        args["key"] = key
    return _call("system.get_setting", args)


@mcp.tool()
def system_set_setting(
    key: str, value: Any, scope: str = "project"
) -> dict:
    """Set a project or timeline setting.

    key: setting name. value: new value (string in Resolve).
    scope: 'project' (default) or 'timeline'.
    """
    return _call(
        "system.set_setting", {"key": key, "value": value, "scope": scope}
    )


# ---------- context / introspection ---------------------------------------

@mcp.tool()
def color_context() -> dict:
    """Snapshot of current page, project, timeline, clip, and node count."""
    return _call("color.context", {})


@mcp.tool()
def color_list_nodes() -> dict:
    """List all color-correction nodes on the current clip's node graph."""
    return _call("color.list_nodes", {})


# ---------- L1: property mutation -----------------------------------------

@mcp.tool()
def color_set_lut(node: int, path: str) -> dict:
    """Apply a .cube LUT file to the given node (1-based).

    Returns whether the set succeeded, plus the readback LUT path.
    """
    return _call("color.set_lut", {"node": node, "path": path})


@mcp.tool()
def color_get_lut(node: int) -> dict:
    """Read the LUT currently applied to the given node (1-based)."""
    return _call("color.get_lut", {"node": node})


@mcp.tool()
def color_set_node_enabled(node: int, enabled: bool) -> dict:
    """Enable or disable a node by index (1-based)."""
    return _call(
        "color.set_node_enabled", {"node": node, "enabled": enabled}
    )


@mcp.tool()
def color_set_cdl(
    node: int,
    slope: Optional[list[float]] = None,
    offset: Optional[list[float]] = None,
    power: Optional[list[float]] = None,
    sat: Optional[float] = None,
) -> dict:
    """Apply an ASC CDL primary grade to a node.

    slope, offset, power are [r, g, b] triples. sat is a single float.
    Omitted axes are left untouched.
    """
    args: dict[str, Any] = {"node": node}
    if slope is not None:
        args["slope"] = slope
    if offset is not None:
        args["offset"] = offset
    if power is not None:
        args["power"] = power
    if sat is not None:
        args["sat"] = sat
    return _call("color.set_cdl", args)


@mcp.tool()
def color_reset_grades() -> dict:
    """Reset all grades on the current clip's node graph."""
    return _call("color.reset_grades", {})


# ---------- LUT library ---------------------------------------------------

@mcp.tool()
def color_list_luts(filter: Optional[str] = None) -> dict:
    """List .cube files under the known LUT roots.

    Optional `filter` matches case-insensitive substrings in the file name.
    """
    args: dict[str, Any] = {}
    if filter:
        args["filter"] = filter
    return _call("color.list_luts", args)


@mcp.tool()
def color_refresh_luts() -> dict:
    """Refresh Resolve's LUT list (run after adding/removing LUT files)."""
    return _call("color.refresh_luts", {})


# ---------- L2: grade composition -----------------------------------------

@mcp.tool()
def color_add_version(name: str, type: int = 0) -> dict:
    """Add a named color version to the current clip. type: 0=local, 1=remote."""
    return _call("color.add_version", {"name": name, "type": type})


@mcp.tool()
def color_load_version(name: str, type: int = 0) -> dict:
    """Load a named color version on the current clip. type: 0=local, 1=remote."""
    return _call("color.load_version", {"name": name, "type": type})


@mcp.tool()
def color_copy_grades(
    to_clip_ids: list[str],
    from_clip_id: Optional[str] = None,
) -> dict:
    """Copy the source clip's grade to a list of target clip uids.

    `from_clip_id` defaults to the current video item.
    """
    args: dict[str, Any] = {"to_clip_ids": to_clip_ids}
    if from_clip_id:
        args["from_clip_id"] = from_clip_id
    return _call("color.copy_grades", args)


@mcp.tool()
def color_apply_drx(path: str, mode: int = 0) -> dict:
    """Apply a saved .drx still's grade to the current clip.

    mode: 0=no keyframes, 1=source TC aligned, 2=start frames aligned.
    """
    return _call("color.apply_drx", {"path": path, "mode": mode})


@mcp.tool()
def color_assign_group(
    group_name: str, create_if_missing: bool = True
) -> dict:
    """Assign the current clip to a named color group (created if missing)."""
    return _call(
        "color.assign_group",
        {
            "group_name": group_name,
            "create_if_missing": create_if_missing,
        },
    )


@mcp.tool()
def color_export_lut(path: str, export_type: int = 1) -> dict:
    """Bake the current grade to a .cube file on disk."""
    return _call(
        "color.export_lut", {"path": path, "export_type": export_type}
    )


# ---------- previews / gallery --------------------------------------------

@mcp.tool()
def color_export_still(path: str) -> dict:
    """Export the current frame as a still image to the given file path."""
    return _call("color.export_still", {"path": path})


@mcp.tool()
def color_list_powergrades() -> dict:
    """List PowerGrade gallery albums and their still counts."""
    return _call("color.list_powergrades", {})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
