"""C# Loader backend for the MCP sidecar.

Talks to the resident NX_MCP_Loader (NXOpen C# plugin) over a named pipe.
The Loader owns the NX main-thread dispatch (SendMessage + WndProc), so this
sidecar never touches NXOpen directly — it only sends text commands and reads
single-line JSON responses.

Backend selection lives in ``server.create_server``: when the Loader answers
``nx_status`` it is preferred; otherwise the sidecar falls back to the
original Python bridge (``DescriptorBridgeClient``).

Only the 15 phase-1 commands plus the 7 phase-2 commands (open/close part,
list sketches/bodies/features, undo, release) are supported — i.e. all 22
certified tools. Anything else raises ``NXToolError`` with ``NX_UNSUPPORTED``.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import time
from typing import Any

from nx_mcp.certified import BridgeCaller
from nx_mcp.runtime import NXToolError, ObjectKind, ObjectRef

PIPE_NAME = r"\\.\pipe\nx_mcp_loader"
_LOADER_VERSION = "2506"

# Loader command syntax is a plain space-separated line. Numbers are formatted
# with invariant culture; booleans become 1/0. The full grammar is defined by
# NX_MCP_Loader.cs (Execute / Dispatch).


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        # avoid exponent / locale surprises; keep enough precision
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _p2(params: dict[str, Any], key: str) -> tuple[float, float]:
    pt = params[key]
    if isinstance(pt, dict):
        return float(pt["x"]), float(pt["y"])
    return float(pt.x), float(pt.y)


def _command(method: str, params: dict[str, Any]) -> str | None:
    if method == "nx_status":
        return "nx_status"
    if method == "nx_create_part":
        return "nx_create_part " + params["path"]
    if method == "nx_open_part":
        return "nx_open_part " + params["path"]
    if method == "nx_save_part":
        return "nx_save_part"
    if method == "nx_close_part":
        return "nx_close_part " + _fmt(params.get("save", True))
    if method == "nx_export_step":
        return "nx_export_step " + params["path"]
    if method in ("nx_list_sketches", "nx_list_bodies", "nx_list_features"):
        return method
    if method == "nx_list_edges":
        return "nx_list_edges " + params["body_id"]
    if method == "nx_list_faces":
        return "nx_list_faces " + params["body_id"]
    if method == "nx_undo":
        return "nx_undo"
    if method == "nx_release":
        return "nx_release"
    if method == "nx_create_sketch":
        return "nx_create_sketch " + params.get("plane", "XY")
    if method == "nx_sketch_line":
        x1, y1 = _p2(params, "start")
        x2, y2 = _p2(params, "end")
        return "nx_sketch_line %s %s %s %s %s" % (
            params["sketch_id"], _fmt(x1), _fmt(y1), _fmt(x2), _fmt(y2))
    if method == "nx_sketch_rectangle":
        x1, y1 = _p2(params, "corner1")
        x2, y2 = _p2(params, "corner2")
        return "nx_sketch_rectangle %s %s %s %s %s" % (
            params["sketch_id"], _fmt(x1), _fmt(y1), _fmt(x2), _fmt(y2))
    if method == "nx_sketch_circle":
        cx, cy = _p2(params, "center")
        return "nx_sketch_circle %s %s %s %s" % (
            params["sketch_id"], _fmt(cx), _fmt(cy), _fmt(params["diameter"]))
    if method == "nx_sketch_arc":
        cx, cy = _p2(params, "center")
        return "nx_sketch_arc %s %s %s %s %s %s" % (
            params["sketch_id"], _fmt(cx), _fmt(cy), _fmt(params["radius"]),
            _fmt(params.get("start_angle", 0.0)), _fmt(params.get("end_angle", 90.0)))
    if method == "nx_finish_sketch":
        return "nx_finish_sketch " + params["sketch_id"]
    if method == "nx_extrude":
        parts = [
            "nx_extrude", params["sketch_id"], _fmt(params["distance"]),
            _fmt(params.get("reverse", False)), _fmt(params.get("start_offset", 0.0)),
            params.get("operation", "create"),
        ]
        if params.get("target_body_id"):
            parts.append(params["target_body_id"])
        return " ".join(parts)
    if method == "nx_hole":
        cx, cy = _p2(params, "center")
        return "nx_hole %s %s %s %s %s %s" % (
            params["body_id"], _fmt(cx), _fmt(cy), _fmt(params["diameter"]),
            _fmt(params["depth"]), _fmt(params.get("start_offset", 0.0)))
    if method == "nx_counterbore_hole":
        cx, cy = _p2(params, "center")
        return "nx_counterbore_hole %s %s %s %s %s %s %s %s" % (
            params["body_id"], _fmt(cx), _fmt(cy),
            _fmt(params["hole_diameter"]), _fmt(params["hole_depth"]),
            _fmt(params["counterbore_diameter"]), _fmt(params["counterbore_depth"]),
            _fmt(params.get("start_offset", 0.0)))
    if method == "nx_countersink_hole":
        cx, cy = _p2(params, "center")
        return "nx_countersink_hole %s %s %s %s %s %s %s %s" % (
            params["body_id"], _fmt(cx), _fmt(cy),
            _fmt(params["hole_diameter"]), _fmt(params["hole_depth"]),
            _fmt(params["countersink_diameter"]), _fmt(params["countersink_angle"]),
            _fmt(params.get("start_offset", 0.0)))
    if method == "nx_shell":
        inward = "0" if params.get("inward", True) is False else "1"
        return "nx_shell %s %s %s %s" % (
            params["body_id"], _fmt(params["thickness"]),
            int(params["remove_face_index"]), inward)
    if method == "nx_edge_blend":
        cmd = "nx_edge_blend %s %s" % (params["body_id"], _fmt(params["radius"]))
        idx = params.get("edge_indices")
        if idx:
            cmd += " " + ",".join(str(int(i)) for i in idx)
        return cmd
    if method == "nx_chamfer":
        cmd = "nx_chamfer %s %s" % (params["body_id"], _fmt(params["offset"]))
        idx = params.get("edge_indices")
        if idx:
            cmd += " " + ",".join(str(int(i)) for i in idx)
        return cmd
    if method == "nx_unite":
        ids = params["tool_body_ids"]
        if isinstance(ids, str):
            csv = ids
        else:
            csv = ",".join(str(i) for i in ids)
        return "nx_unite %s %s" % (params["target_body_id"], csv)
    if method == "nx_revolve":
        sx, sy = _p2(params, "axis_start")
        ex, ey = _p2(params, "axis_end")
        return "nx_revolve %s %s %s %s %s %s %s" % (
            params["sketch_id"], _fmt(sx), _fmt(sy), _fmt(ex), _fmt(ey),
            _fmt(params.get("angle", 360.0)), _fmt(params.get("reverse", False)))
    if method == "nx_mirror":
        return "nx_mirror %s %s %s" % (
            params["body_id"], params["plane"], _fmt(params.get("offset", 0.0)))
    if method == "nx_linear_pattern":
        return "nx_linear_pattern %s %s %s %s %s" % (
            params["body_id"], params["direction"], int(params["count"]),
            _fmt(params["spacing"]), _fmt(params.get("reverse", False)))
    if method == "nx_circular_pattern":
        c = params["center"]
        return "nx_circular_pattern %s %s %s %s %s %s %s %s" % (
            params["body_id"], params["axis"],
            _fmt(c["x"]), _fmt(c["y"]), _fmt(c["z"]),
            int(params["count"]), _fmt(params["angle"]),
            _fmt(params.get("reverse", False)))
    if method == "nx_fit_view":
        return "nx_fit_view"
    return None  # unsupported by the phase-1 Loader


def _pipe_call(cmd: str, timeout: float = 180.0, pipe: str = PIPE_NAME) -> dict[str, Any]:
    """Send one command over the named pipe and read the single-line JSON reply.

    Uses ctypes so no third-party dependency (pywin32) is required.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE = wt.HANDLE(-1).value
    ERROR_PIPE_BUSY = 231
    kernel32.CreateFileW.restype = wt.HANDLE

    handle = None
    for _ in range(30):
        handle = kernel32.CreateFileW(
            pipe,
            GENERIC_READ | GENERIC_WRITE,
            0, None, OPEN_EXISTING, 0, None,
        )
        if handle and handle != INVALID_HANDLE:
            break
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_BUSY:
            kernel32.WaitNamedPipeW(pipe, 500)
        else:
            # server briefly re-creates the single-instance pipe between
            # connections; retry instead of failing immediately
            time.sleep(0.3)
        handle = None
    if not handle or handle == INVALID_HANDLE:
        raise NXToolError(
            "NX_LOADER_UNAVAILABLE",
            "NX_MCP_Loader pipe is not available; start NX with the plugin installed.",
        )
    try:
        payload = (cmd + "\n").encode("utf-8")
        written = wt.DWORD(0)
        ok = False
        for attempt in range(5):
            ok = bool(kernel32.WriteFile(
                handle, payload, len(payload), ctypes.byref(written), None))
            if ok:
                break
            time.sleep(0.25)
        if not ok:
            raise NXToolError(
                "NX_LOADER_WRITE",
                "failed to write loader command (err=%d)" % ctypes.get_last_error(),
            )

        buffer = bytearray()
        chunk = ctypes.create_string_buffer(65536)
        n = wt.DWORD(0)
        while True:
            if not kernel32.ReadFile(handle, chunk, 65536, ctypes.byref(n), None):
                break
            if n.value == 0:
                continue
            buffer.extend(chunk.raw[: n.value])
            if b"\n" in buffer:
                break
    finally:
        kernel32.CloseHandle(handle)

    line = bytes(buffer).decode("utf-8", errors="replace").strip()
    if not line:
        raise NXToolError("NX_LOADER_EMPTY", "loader returned an empty response")
    try:
        return json.loads(line)
    except ValueError:
        raise NXToolError("NX_LOADER_BAD_JSON", "loader returned invalid JSON: " + line[:200])


def _ref(kind: ObjectKind, obj_id: str, part_id: str = "") -> ObjectRef:
    return ObjectRef(id=obj_id, kind=kind, name="", part_id=part_id)


def _adapt(method: str, params: dict[str, Any], resp: dict[str, Any]) -> dict[str, Any]:
    part_id = resp.get("part", params.get("path", ""))
    msg = resp.get("result") or resp.get("message") or "ok"
    if method == "nx_status":
        active = None
        if resp.get("part"):
            active = _ref("part", resp["part"], resp["part"])
        return {
            "status": "success",
            "connected": True,
            "nx_version": resp.get("nx", _LOADER_VERSION),
            "bridge_protocol": 1,
            "active_part": active,
        }
    if method == "nx_create_part":
        return {"status": "success", "part": _ref("part", resp.get("part", params["path"]), part_id), "message": msg}
    if method == "nx_open_part":
        return {"status": "success", "part": _ref("part", resp.get("part", params["path"]), part_id), "message": msg}
    if method == "nx_close_part":
        return {"status": "success", "message": msg}
    if method == "nx_export_step":
        return {"status": "success", "path": resp.get("step", params["path"]), "message": msg}
    if method == "nx_save_part":
        return {"status": "success", "message": "saved"}
    if method == "nx_fit_view":
        return {"status": "success", "message": "view fitted"}
    if method == "nx_undo":
        return {"status": "success", "message": msg}
    if method == "nx_release":
        # Loader stays resident: only task state is cleared.
        return {"status": "success", "message": msg}
    if method in ("nx_list_sketches", "nx_list_bodies", "nx_list_features"):
        kind: ObjectKind = {
            "nx_list_sketches": "sketch",
            "nx_list_bodies": "body",
            "nx_list_features": "feature",
        }[method]
        names = resp.get("objects") or []
        return {
            "status": "success",
            "objects": [_ref(kind, str(name), part_id) for name in names],
            "message": msg,
        }
    if method == "nx_list_edges":
        return {
            "status": "success",
            "edges": resp.get("edges") or [],
            "edge_count": resp.get("edge_count", 0),
            "message": msg,
        }
    if method == "nx_list_faces":
        return {
            "status": "success",
            "faces": resp.get("faces") or [],
            "face_count": resp.get("face_count", 0),
            "message": msg,
        }
    if method == "nx_extrude":
        body_id = resp.get("body_id", "")
        return {
            "status": "success",
            "feature": _ref("feature", body_id, part_id),
            "body": _ref("body", body_id, part_id),
            "message": msg,
        }
    if method == "nx_unite":
        body_id = resp.get("body_id", params.get("target_body_id", ""))
        return {
            "status": "success",
            "object": _ref("body", body_id, part_id),
            "message": msg,
        }
    if method == "nx_sketch_rectangle":
        obj_id = params.get("sketch_id", "")
        return {
            "status": "success",
            "objects": [_ref("curve", obj_id, part_id)],
            "message": msg,
        }
    if method == "nx_linear_pattern":
        ids = resp.get("body_ids") or []
        return {
            "status": "success",
            "objects": [_ref("body", str(i), part_id) for i in ids],
            "message": msg,
        }
    if method == "nx_circular_pattern":
        ids = resp.get("body_ids") or []
        return {
            "status": "success",
            "objects": [_ref("body", str(i), part_id) for i in ids],
            "message": msg,
        }
    if method in ("nx_create_sketch", "nx_sketch_line", "nx_sketch_circle",
                  "nx_sketch_arc", "nx_finish_sketch", "nx_hole",
                  "nx_counterbore_hole", "nx_countersink_hole",
                  "nx_shell",
                  "nx_edge_blend", "nx_chamfer", "nx_revolve", "nx_mirror"):
        obj_id = (
            resp.get("sketch_id")
            or resp.get("body_id")
            or params.get("sketch_id")
            or params.get("body_id")
            or ""
        )
        kind: ObjectKind = "body" if method in ("nx_hole", "nx_counterbore_hole", "nx_countersink_hole", "nx_shell", "nx_edge_blend", "nx_chamfer", "nx_revolve", "nx_mirror") else "sketch"
        return {"status": "success", "object": _ref(kind, obj_id, part_id), "message": msg}
    return {"status": "success", "message": msg}


class LoaderBridge:
    """BridgeCaller-compatible backend backed by the resident C# loader."""

    def __init__(self, pipe: str = PIPE_NAME) -> None:
        self.pipe = pipe

    def ping(self, timeout: float = 15.0) -> bool:
        try:
            resp = _pipe_call("nx_status", timeout=timeout, pipe=self.pipe)
            return bool(resp.get("ok"))
        except Exception:
            return False

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        cmd = _command(method, params)
        if cmd is None:
            raise NXToolError(
                "NX_UNSUPPORTED",
                "C# Loader backend does not support %r yet; start the Python bridge for this tool." % method,
                retryable=False,
            )
        resp = await asyncio.to_thread(_pipe_call, cmd, 180.0, self.pipe)
        if not resp.get("ok"):
            raise NXToolError("NX_LOADER_ERROR", resp.get("error", "loader command failed"))
        return _adapt(method, params, resp)


def select_loader() -> BridgeCaller | None:
    """Return a LoaderBridge when the resident loader answers, else None."""
    try:
        bridge = LoaderBridge()
        if bridge.ping():
            return bridge
    except Exception:
        pass
    return None
