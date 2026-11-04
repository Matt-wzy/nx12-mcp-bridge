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
import os
import subprocess
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
    # ---- drafting (2D drawing) -------------------------------------------
    # Backed by the C# Loader commands added on top of CAD-Agent-Hub's proven
    # sequences; see loader/NX_MCP_Loader.cs (CreateDrawingSheet / CreateProjectedView).
    # Tokenised protocol: sheet name / view name must not contain spaces.
    if method == "nx_create_drawing_sheet":
        scale = params.get("scale_numerator", params.get("scale", 1.0))
        return "nx_create_drawing_sheet %s %s %s %s %s" % (
            params.get("name", "MCP_SHEET_1"),
            params.get("size", "A4"),
            params.get("projection", "first"),
            _fmt(scale),
            params.get("model_view", "Top"),
        )
    if method == "nx_create_projected_view":
        if "position" in params:
            x, y = _p2(params, "position")
        else:
            x, y = params.get("x"), params.get("y")
        parent = str(params.get("parent_view_id") or params.get("parent_view") or "")
        name = str(params.get("name") or params.get("view_name") or "")
        cmd = "nx_create_projected_view %s %s" % (_fmt(x), _fmt(y))
        # parent is positional; supply "0" (first view) when only a name was given
        if name and not parent:
            parent = "0"
        if parent:
            cmd += " " + parent
            if name:
                cmd += " " + name
        return cmd
    if method == "nx_create_note":
        # text goes last so it may contain spaces; '|' marks a line break
        return "nx_create_note %s %s %s" % (
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            str(params.get("text", "")),
        )
    if method == "nx_create_diameter_dimension":
        return "nx_create_diameter_dimension %s %s %s %s %s %s %s %s" % (
            str(params.get("view_id", "")),
            str(params.get("body_id", "")),
            str(params.get("edge_index", 0)),
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            _fmt(params.get("upper_tolerance", 0.1)),
            _fmt(params.get("lower_tolerance", -0.1)),
            str(params.get("dim_name", "MCP_DIAMETER")),
        )
    if method == "nx_create_radius_dimension":
        return "nx_create_radius_dimension %s %s %s %s %s %s" % (
            str(params.get("view_id", "")),
            str(params.get("body_id", "")),
            str(params.get("edge_index", 0)),
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            str(params.get("dim_name", "MCP_RADIUS")),
        )
    if method == "nx_measure_distance":
        return "nx_measure_distance %s %s %s" % (
            str(params.get("body_id", "")),
            str(params.get("edge_index_1", params.get("first_edge_index", 0))),
            str(params.get("edge_index_2", params.get("second_edge_index", 0))),
        )
    if method == "nx_measure_angle":
        return "nx_measure_angle %s %s %s" % (
            str(params.get("body_id", "")),
            str(params.get("edge_index_1", 0)),
            str(params.get("edge_index_2", 0)),
        )
    if method == "nx_mass_properties":
        return "nx_mass_properties %s %s" % (
            _fmt(params.get("density_kg_m3", 7830.0)),
            _fmt(params.get("accuracy", 0.99)),
        )
    if method == "nx_create_centerline":
        return "nx_create_centerline %s %s %s" % (
            str(params.get("view_id", "")),
            str(params.get("body_id", "")),
            str(params.get("face_index", 0)),
        )
    if method == "nx_create_detail_view":
        # 边界中心/半径按模型坐标；px/py 是新视图在图纸上的放置点（mm）
        cmd = "nx_create_detail_view %s %s %s %s %s %s" % (
            str(params.get("parent_view_id", "")),
            _fmt(params.get("center_x", 0.0)),
            _fmt(params.get("center_y", 0.0)),
            _fmt(params.get("radius", 10.0)),
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
        )
        scale = params.get("scale", 0.0) or 0.0
        if scale:
            cmd += " " + _fmt(scale)
        name = str(params.get("view_name", "") or "")
        if name:
            cmd += " " + name
        return cmd
    if method == "nx_create_sheet_border":
        return "nx_create_sheet_border %s" % (
            "1" if str(params.get("zones", "1")) in ("1", "true", "True") else "0"
        )
    if method == "nx_set_attribute":
        return "nx_set_attribute %s %s" % (
            str(params.get("name", "")),
            str(params.get("value", "")),
        )
    if method == "nx_list_attributes":
        return "nx_list_attributes"
    if method == "nx_create_title_block":
        return "nx_create_title_block %s %s" % (
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
        )
    if method == "nx_create_detail_view":
        return "nx_create_detail_view %s %s %s %s %s %s" % (
            str(params.get("parent_view_id", "")),
            _fmt(params.get("x", 0.0)),
            _fmt(params.get("y", 0.0)),
            _fmt(params.get("radius", 10.0)),
            _fmt(params.get("scale", 2.0)),
            str(params.get("view_name", "")),
        )
    if method == "nx_create_roughness":
        return "nx_create_roughness %s %s %s" % (
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            str(params.get("value", "1.6")),
        )
    if method == "nx_create_centerline":
        return "nx_create_centerline"
    if method == "nx_export_pdf":
        cmd = "nx_export_pdf " + str(params.get("path", ""))
        sheet = str(params.get("sheet_name", "") or "")
        if sheet:
            cmd += " " + sheet
        return cmd
    if method == "nx_delete_annotation":
        return "nx_delete_annotation " + str(params.get("annotation_id", ""))
    if method in (
        "nx_list_drawing_sheets",
        "nx_list_drafting_views",
        "nx_list_dimensions",
        "nx_list_annotations",
    ):
        return method
    # ---- drafting: view update + dimension annotation ------------
    if method == "nx_update_views":
        return "nx_update_views"
    if method == "nx_create_dimension":
        if "position" in params:
            x, y = _p2(params, "position")
        else:
            x = params.get("pos_x")
            y = params.get("pos_y")
        return "nx_create_dimension %s %s %s %s %s %s %s %s %s %s" % (
            str(params.get("view_id", "")),
            str(params.get("body_id", "")),
            int(params["first_edge_index"]), int(params["second_edge_index"]),
            _fmt(x), _fmt(y),
            params.get("measurement", "horizontal"),
            _fmt(params.get("upper_tolerance", 0.1)),
            _fmt(params.get("lower_tolerance", -0.1)),
            params.get("dim_name", "MCP_DIMENSION"),
        )
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
    # ---- WAVE 链接 / 草图读取 / 尺寸改值（NX12 适配新增） ----------------
    if method == "nx_create_wave_link":
        # Loader 语法: nx_create_wave_link <src> <tgt> [body_index] [assoc=1] [x=..] [y=..] [z=..]
        # 协议按空格分词，路径不能包含空格（与 nx_open_part 同一限制）。
        cmd = "nx_create_wave_link %s %s %s" % (
            params["source_part"],
            params["target_part"],
            int(params.get("body_index", 1) or 1),
        )
        if params.get("associative"):
            cmd += " assoc=1"
        pos = params.get("position") or {}
        if pos:
            if "x" in pos:
                cmd += " x=" + _fmt(float(pos["x"]))
            if "y" in pos:
                cmd += " y=" + _fmt(float(pos["y"]))
            if "z" in pos:
                cmd += " z=" + _fmt(float(pos["z"]))
        return cmd
    if method == "nx_read_sketch":
        return "nx_read_sketch " + str(params.get("sketch_name", ""))
    if method == "nx_set_dimension_value":
        return "nx_set_dimension_value %s %s" % (
            _fmt(float(params["target_size"])),
            _fmt(float(params["new_value"])),
        )
    return None  # unsupported by the phase-1 Loader


def _find_nx_base_dir() -> str | None:
    """Locate the NX install root (the one containing DXFDWG\\dxfdwg.exe).

    Order: UGII_BASE_DIR env -> registry (HKLM ...\\Siemens\\NX*) ->
    default Program Files location. Returns None when no candidate validates.
    """
    candidates: list[str] = []
    env = os.environ.get("UGII_BASE_DIR")
    if env:
        candidates.append(env)
    try:
        import winreg

        for sub in (r"SOFTWARE\WOW6432Node\Siemens", r"SOFTWARE\Siemens"):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    if not name.upper().startswith("NX"):
                        continue
                    try:
                        with winreg.OpenKey(key, name) as nk:
                            base, _ = winreg.QueryValueEx(nk, "UGII_BASE_DIR")
                        candidates.append(str(base))
                    except OSError:
                        continue
    except Exception:
        pass
    candidates.append(r"C:\Program Files\Siemens\NX 12.0")
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, "DXFDWG", "dxfdwg.exe")):
            return cand
    return None


def _dxfdwg_def_copy(base: str, outdir: str, sheet: str, logfile: str) -> str:
    """Copy the official dxfdwg.def with LOG_FILE turned on (and the drawing
    list pinned when a sheet is given), so every translation leaves evidence."""
    src = os.path.join(base, "DXFDWG", "dxfdwg.def")
    dst = os.path.join(outdir, "dxfdwg_mcp.def")
    try:
        with open(src, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
        out = []
        for ln in lines:
            low = ln.strip().lower()
            if low.startswith("log_file"):
                out.append("LOG_FILE = " + logfile)
            elif low.startswith("ugi_drawing_names") and sheet:
                out.append("UGI_DRAWING_NAMES = " + sheet)
            else:
                out.append(ln)
        with open(dst, "w") as fh:
            fh.write("\r\n".join(out) + "\r\n")
        return dst
    except Exception:
        return src


def _export_dwg_external(params: dict[str, Any]) -> dict[str, Any]:
    """DXF/DWG export via the official translator OUT-OF-PROCESS.

    Why not DxfdwgCreator.Commit() inside NX: in a headless session it returns
    success yet never spawns dxfdwg.exe - with or without a SettingsFile
    (verified 2026-11-04, see 受阻项调研档案 section 2). The standalone
    translator works: it reads the saved .prt directly, so the part is saved
    over the pipe first and the translator is then run as a plain subprocess.
    """
    out = str(params.get("path", "") or "").strip()
    if not out:
        raise NXToolError(
            "NX_LOADER_ERROR", "nx_export_dwg requires an output path", retryable=False)
    if not (out.lower().endswith(".dxf") or out.lower().endswith(".dwg")):
        out += ".dxf"
    mode = "ugtodwg" if out.lower().endswith(".dwg") else "ugtodxf"
    sheet = str(params.get("sheet_name", "") or "").strip()

    status = _pipe_call("nx_status", timeout=60.0)
    part = str(status.get("part", "") or "").strip()
    if not part:
        raise NXToolError(
            "NX_LOADER_ERROR",
            "no active part to export; create or open a part first",
            retryable=False,
        )
    _pipe_call("nx_save_part", timeout=180.0)

    base = _find_nx_base_dir()
    exe = os.path.join(base, "DXFDWG", "dxfdwg.exe") if base else ""
    if not base or not os.path.isfile(exe):
        raise NXToolError(
            "NX_LOADER_ERROR",
            "DXFDWG translator not found; set UGII_BASE_DIR to the NX install root",
            retryable=False,
        )

    out_abs = os.path.abspath(out)
    outdir = os.path.dirname(out_abs)
    os.makedirs(outdir, exist_ok=True)
    logfile = os.path.join(outdir, "dxfdwg_mcp.log")
    defcopy = _dxfdwg_def_copy(base, outdir, sheet, logfile)
    try:
        os.remove(out_abs)
    except OSError:
        pass

    env = dict(os.environ)
    env["UGII_BASE_DIR"] = base
    env["PATH"] = os.pathsep.join([
        os.path.join(base, "NXBIN"),
        os.path.join(base, "UGII"),
        os.path.join(base, "DXFDWG"),
        env.get("PATH", ""),
    ])
    try:
        proc = subprocess.run(
            [exe, part, "o=" + out_abs, "l=" + logfile, "d=" + defcopy, "m=" + mode],
            env=env, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise NXToolError(
            "NX_LOADER_ERROR", "dxfdwg translator timed out after 300s", retryable=True,
        ) from exc

    if os.path.isfile(out_abs) and os.path.getsize(out_abs) > 0:
        size = os.path.getsize(out_abs)
        msg = "exported %d bytes (external dxfdwg translator, %s)" % (size, mode)
        if sheet:
            msg += " sheet=" + sheet
        return {"status": "success", "path": out_abs, "message": msg}

    tail = ""
    if os.path.isfile(logfile):
        with open(logfile, "r", errors="replace") as fh:
            tail = " | ".join(fh.read().splitlines()[-8:])
    detail = (proc.stderr or "").strip()[:300] or tail or "no log written"
    raise NXToolError(
        "NX_LOADER_ERROR",
        "translator produced no file (exit %s): %s" % (proc.returncode, detail),
        retryable=True,
    )


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
    # Drafting lists: ObjectRef.kind is a restricted literal (part/sketch/curve/
    # feature/body) and cannot carry "sheet"/"view", so surface the items inside
    # the message instead of returning them as objects.
    if method in (
        "nx_list_drawing_sheets",
        "nx_list_drafting_views",
        "nx_list_dimensions",
        "nx_list_annotations",
    ):
        items = resp.get("sheets")
        if items is None:
            items = resp.get("views")
        if items is None:
            items = resp.get("dimensions")
        if items is None:
            items = resp.get("annotations")
        items = items or []
        detail = ", ".join(str(i) for i in items) if items else "none"
        return {"status": "success", "message": "%s [%s]" % (msg, detail)}
    if method == "nx_update_views":
        return {"status": "success", "message": msg}
    if method == "nx_create_dimension":
        return {
            "status": "success",
            "dimension": resp.get("dimension"),
            "message": msg,
        }
    if method == "nx_mass_properties":
        return {
            "status": "success",
            "body_count": int(resp.get("body_count", 0)),
            "mass_kg": float(resp.get("mass_kg", 0.0)),
            "volume_mm3": float(resp.get("volume_mm3", 0.0)),
            "surface_area_mm2": float(resp.get("surface_area_mm2", 0.0)),
            "density_kg_m3": float(resp.get("density_kg_m3", 0.0)),
            "center_of_gravity": {
                "x": float(resp.get("cog_x_mm", 0.0)),
                "y": float(resp.get("cog_y_mm", 0.0)),
                "z": float(resp.get("cog_z_mm", 0.0)),
            },
            "message": msg,
        }
    if method == "nx_measure_angle":
        return {
            "status": "success",
            "angle_deg": float(resp.get("angle_deg", 0.0)),
            "acute_deg": float(resp.get("acute_deg", 0.0)),
            "message": msg,
        }
    return {"status": "success", "message": msg}


class LoaderBridge:
    """BridgeCaller-compatible backend backed by the resident C# loader."""

    def __init__(self, pipe: str = PIPE_NAME) -> None:
        self.pipe = pipe
        self.last_ping_error: str | None = None

    def ping(self, timeout: float = 15.0) -> bool:
        self.last_ping_error = None
        try:
            resp = _pipe_call("nx_status", timeout=timeout, pipe=self.pipe)
            if resp.get("ok"):
                return True
            self.last_ping_error = str(resp.get("error") or "loader status returned ok=false")
            return False
        except Exception as exc:
            self.last_ping_error = str(exc)
            return False

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "nx_export_dwg":
            # Not a pipe command: the in-NX DxfdwgCreator never spawns the
            # translator headless, so this runs the official translator locally
            # (part is saved over the pipe first). See _export_dwg_external.
            return await asyncio.to_thread(_export_dwg_external, params)
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
