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


def _leader_tokens(params: dict[str, Any]) -> str:
    """Serialize the optional leader-line options shared by the annotation
    commands (note / roughness / datum symbol / GDT frame).

    Loader-side tokens (must sit right after the fixed positional args and
    before any free text): lv=<view_id> lb=<body_id> le=<edge_index>
    [side=left|right]. All three of lv/lb/le must be given together or the
    loader rejects the command; side is optional. note_font=<name> is a
    note-only option (CJK lettering font, e.g. chinesef).
    """
    lv = str(params.get("leader_view_id", "") or "").strip()
    lb = str(params.get("leader_body_id", "") or "").strip()
    le = params.get("leader_edge_index", None)
    tokens = ""
    if lv:
        tokens += " lv=" + lv
    if lb:
        tokens += " lb=" + lb
    if le is not None and int(le) >= 0:
        tokens += " le=" + str(int(le))
    side = str(params.get("leader_side", "") or "").strip().lower()
    if side:
        tokens += " side=" + side
    font = str(params.get("note_font", "") or "").strip()
    if font:
        tokens += " font=" + font
    return tokens


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
    if method == "nx_export_iges":
        return "nx_export_iges " + params["path"]
    if method in ("nx_list_sketches", "nx_list_bodies", "nx_list_features"):
        return method
    if method == "nx_list_edges":
        return "nx_list_edges " + params["body_id"]
    if method == "nx_list_faces":
        return "nx_list_faces " + params["body_id"]
    if method == "nx_list_holes":
        body_id = str(params.get("body_id", "") or "")
        return ("nx_list_holes " + body_id) if body_id else "nx_list_holes"
    # ---- selection reflux + face provenance (made_by) -----------------------
    if method == "nx_get_selection":
        return "nx_get_selection"
    if method == "nx_face_provenance":
        return "nx_face_provenance %s %d" % (
            str(params.get("body_id", "")),
            int(params.get("face_index", 0)),
        )
    # ---- assembly explosion (爆炸视图, 装配工艺卡配图) -----------------------
    if method == "nx_create_explosion":
        name = str(params.get("name", "") or "")
        return ("nx_create_explosion " + name) if name else "nx_create_explosion"
    if method == "nx_explode_component":
        return "nx_explode_component %s %s %s %s %s" % (
            str(params.get("explosion", "")),
            str(params.get("component", "")),
            _fmt(float(params.get("dx", 0.0))),
            _fmt(float(params.get("dy", 0.0))),
            _fmt(float(params.get("dz", 0.0))),
        )
    if method == "nx_unexplode_component":
        return "nx_unexplode_component %s %s" % (
            str(params.get("explosion", "")),
            str(params.get("component", "")),
        )
    if method == "nx_list_explosions":
        return "nx_list_explosions"
    if method == "nx_refset_add_bodies":
        name = str(params.get("name", "") or "")
        return ("nx_refset_add_bodies " + name) if name else "nx_refset_add_bodies"
    # ---- stock blank (毛坯，工艺材料定额) ---------------------------------
    if method == "nx_create_stock":
        # Loader 语法: nx_create_stock <box|cyl> [body_id] [allow=a] [axis=x|y|z]
        #              [dia=d] [len=l] [ra=r] [aa=r]
        cmd = "nx_create_stock " + str(params.get("mode", "box"))
        body_id = str(params.get("body_id", "") or "")
        if body_id:
            cmd += " " + body_id
        cmd += " allow=" + _fmt(float(params.get("allowance", 2.0)))
        axis = str(params.get("axis", "") or "")
        if axis and axis != "auto":
            cmd += " axis=" + axis
        dia = float(params.get("dia", 0.0) or 0.0)
        if dia > 0.0:
            cmd += " dia=" + _fmt(dia)
        length = float(params.get("length", 0.0) or 0.0)
        if length > 0.0:
            cmd += " len=" + _fmt(length)
        return cmd
    if method == "nx_undo":
        return "nx_undo"
    # ---- feature suppression (工序模型: 抑制后道工序特征得中间工序件) --------
    if method == "nx_set_feature_suppressed":
        # Loader 语法: nx_set_feature_suppressed <feature> <0|1>
        # <feature> 是 nx_list_features 顺序的 1 基索引，或特征名/JournalIdentifier
        return "nx_set_feature_suppressed %s %d" % (
            str(params.get("feature", "") or ""),
            1 if params.get("suppress") else 0,
        )
    # ---- interference / clearance check (装配干涉/间隙检查) ------------------
    if method == "nx_check_interference":
        # Loader 语法: nx_check_interference [asm=1] [c1=<ids>] [c2=<ids>]
        #              [zone=<mm>] [keep_set=1]   ids 逗号分隔
        cmd = "nx_check_interference"
        if params.get("asm"):
            cmd += " asm=1"
        for key, flag in (("collection_one", "c1="), ("collection_two", "c2=")):
            val = params.get(key) or ""
            if isinstance(val, (list, tuple)):
                val = ",".join(str(x) for x in val)
            val = str(val).strip()
            if val:
                cmd += " " + flag + val
        zone = float(params.get("zone_mm", 0.0) or 0.0)
        if zone > 0.0:
            cmd += " zone=" + _fmt(zone)
        if params.get("keep_set"):
            cmd += " keep_set=1"
        return cmd
    if method == "nx_save_part_as":
        # 协议按空格分词，路径不能包含空格（与 nx_open_part 同一限制）
        return "nx_save_part_as " + str(params["path"])
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
        # explosion=<name> must be the LAST token (after the positional name)
        if params.get("explosion"):
            cmd += " explosion=" + str(params["explosion"])
        return cmd
    if method == "nx_create_note":
        # text goes last so it may contain spaces; '|' marks a line break;
        # optional leader tokens sit between the position and the text
        return "nx_create_note %s %s%s %s" % (
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            _leader_tokens(params),
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
        cmd = "nx_mass_properties %s %s" % (
            _fmt(params.get("density_kg_m3", 7830.0)),
            _fmt(params.get("accuracy", 0.99)),
        )
        if params.get("per_body"):
            cmd += " per=1"
        return cmd
    if method == "nx_create_section_view":
        # Loader 语法: nx_create_section_view <view> <x1> <y1> <x2> <y2> <px> <py> [k=v]
        # 全部坐标均为图纸坐标（mm）；k=v: scale= / arrow=dx,dy / step=dx,dy / name=
        cmd = "nx_create_section_view %s %s %s %s %s %s %s" % (
            str(params.get("parent_view_id", "")),
            _fmt(float(params["cut_x1"])), _fmt(float(params["cut_y1"])),
            _fmt(float(params["cut_x2"])), _fmt(float(params["cut_y2"])),
            _fmt(float(params["pos_x"])), _fmt(float(params["pos_y"])),
        )
        if params.get("scale"):
            cmd += " scale=" + _fmt(float(params["scale"]))
        if params.get("arrow_dx") is not None and params.get("arrow_dy") is not None:
            cmd += " arrow=%s,%s" % (_fmt(float(params["arrow_dx"])), _fmt(float(params["arrow_dy"])))
        if params.get("step_dx") is not None and params.get("step_dy") is not None:
            cmd += " step=%s,%s" % (_fmt(float(params["step_dx"])), _fmt(float(params["step_dy"])))
        name = str(params.get("view_name", "") or "")
        if name:
            cmd += " name=" + name
        return cmd
    if method == "nx_add_section_segment":
        # Loader 语法: nx_add_section_segment <section_view> <x1> <y1> <x2> <y2>
        # 图纸坐标；向剖切线追加割段（阶梯剖），NX 自动生成转折并更新视图
        return "nx_add_section_segment %s %s %s %s %s" % (
            str(params.get("view_id", "")),
            _fmt(float(params["cut_x1"])), _fmt(float(params["cut_y1"])),
            _fmt(float(params["cut_x2"])), _fmt(float(params["cut_y2"])),
        )
    if method == "nx_create_id_symbol":
        # Loader 语法: nx_create_id_symbol <type> <x> <y> [size=k] [lower=t] [upper text...]
        # 上部文字放最后可含空格；图纸坐标
        cmd = "nx_create_id_symbol %s %s %s" % (
            str(params.get("symbol_type", "circle")),
            _fmt(float(params.get("pos_x", 0.0))),
            _fmt(float(params.get("pos_y", 0.0))),
        )
        size = params.get("size", 10.0)
        if size and float(size) != 10.0:
            cmd += " size=" + _fmt(float(size))
        lower = str(params.get("lower_text", "") or "")
        if lower:
            cmd += " lower=" + lower
        upper = str(params.get("upper_text", "") or "")
        if upper:
            cmd += " " + upper
        return cmd
    if method == "nx_delete_view":
        return "nx_delete_view " + str(params.get("view_id", ""))
    if method == "nx_create_table":
        # Loader 语法: nx_create_table <x> <y> [cw=w] [rh=h] [font=name]
        #              [header=1] <cells...>  cells 行分隔'|'列分隔','，
        #              必须是最后一个 token（可含空格）；k=v 选项须在 cells 之前
        cmd = "nx_create_table %s %s" % (
            _fmt(float(params.get("pos_x", 0.0))),
            _fmt(float(params.get("pos_y", 0.0))),
        )
        cw = float(params.get("col_width", 0.0) or 0.0)
        if cw > 0.0:
            cmd += " cw=" + _fmt(cw)
        rh = float(params.get("row_height", 0.0) or 0.0)
        if rh > 0.0:
            cmd += " rh=" + _fmt(rh)
        if params.get("header"):
            cmd += " header=1"
        head_loc = str(params.get("head_loc", "") or "")
        if head_loc:
            cmd += " head_loc=" + head_loc
        font = str(params.get("font", "") or "")
        if font:
            cmd += " font=" + font
        cmd += " " + str(params["cells"])
        return cmd
    if method == "nx_create_angle_dimension":
        # Loader 语法: nx_create_angle_dimension <view> <body> <e1> <e2> <x> <y>
        #              [arc=minor|major] [name=..]
        cmd = "nx_create_angle_dimension %s %s %s %s %s %s" % (
            str(params.get("view_id", "")),
            str(params.get("body_id", "")),
            str(params.get("first_edge_index", 0)),
            str(params.get("second_edge_index", 0)),
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
        )
        arc = str(params.get("arc", "minor") or "minor").strip().lower()
        if arc and arc != "minor":
            cmd += " arc=" + arc
        name = str(params.get("dim_name", "") or "").strip()
        if name and name != "MCP_ANGLE":
            cmd += " name=" + name
        return cmd
    if method == "nx_create_datum_symbol":
        # Loader 语法: nx_create_datum_symbol <letter> <x> <y>（图纸坐标 mm）
        #              [lv=<view> lb=<body> le=<edge> [side=..]]（可选指引线）
        return "nx_create_datum_symbol %s %s %s%s" % (
            str(params.get("letter", "")),
            _fmt(float(params.get("pos_x", 0.0))),
            _fmt(float(params.get("pos_y", 0.0))),
            _leader_tokens(params),
        )
    if method == "nx_list_assembly":
        return "nx_list_assembly"
    if method == "nx_create_weld_symbol":
        # Loader 语法: nx_create_weld_symbol <type> <size> <x> <y>
        #              [side=..] [length_pitch=..] [tail=..] [contour=..] [finish=..] [std=..]
        # weld_type/contour/finish 已在 certified.py 归一化为英文枚举名（中文
        # 别名在那边映射，Loader 源码按 ANSI 代码页编译不能放中文）；size/
        # length_pitch 是单 token，不能含空格；pos 为图纸坐标 mm。
        cmd = "nx_create_weld_symbol %s %s %s %s" % (
            str(params.get("weld_type", "")),
            str(params.get("weld_size", "") or "").strip() or "0",
            _fmt(float(params.get("pos_x", 0.0))),
            _fmt(float(params.get("pos_y", 0.0))),
        )
        cmd += " side=" + str(params.get("side", "arrow"))
        if params.get("length_pitch"):
            cmd += " length_pitch=" + str(params["length_pitch"]).strip()
        if params.get("tail") and params["tail"] != "none":
            cmd += " tail=" + str(params["tail"])
        if params.get("contour"):
            cmd += " contour=" + str(params["contour"])
        if params.get("finish"):
            cmd += " finish=" + str(params["finish"])
        cmd += " std=" + str(params.get("standard", "gb"))
        return cmd
    if method == "nx_add_component":
        # Loader 语法: nx_add_component <child_prt> [name=..] [refset=..] [x=..] [y=..] [z=..] [layer=..]
        # 协议按空格分词，路径不能包含空格（与 nx_open_part 同一限制）。
        cmd = "nx_add_component " + str(params["child_part"])
        name = str(params.get("instance_name", "") or "")
        if name:
            cmd += " name=" + name
        refset = str(params.get("refset", "") or "")
        if refset:
            cmd += " refset=" + refset
        pos = params.get("position") or {}
        if "x" in pos:
            cmd += " x=" + _fmt(float(pos["x"]))
        if "y" in pos:
            cmd += " y=" + _fmt(float(pos["y"]))
        if "z" in pos:
            cmd += " z=" + _fmt(float(pos["z"]))
        layer = params.get("layer")
        if layer is not None and int(layer) != 0:
            cmd += " layer=" + str(int(layer))
        return cmd
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
        # optional leader tokens come before the positional value
        return "nx_create_roughness %s %s%s %s" % (
            _fmt(params.get("pos_x", 0.0)),
            _fmt(params.get("pos_y", 0.0)),
            _leader_tokens(params),
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
    if method == "nx_create_gdt_frame":
        # Loader 语法: nx_create_gdt_frame <char> <tol> <x> <y>
        #              [d1=A] [d2=B] [d3=C] [shape=..] [mat=..]
        # characteristic 已在 certified.py 归一化为英文枚举名（中文别名
        # 平面度/位置度等在那边映射，Loader 源码按 ANSI 代码页编译不能放中文）。
        cmd = "nx_create_gdt_frame %s %s %s %s" % (
            str(params.get("characteristic", "")),
            str(params.get("tolerance", "")),
            _fmt(float(params.get("pos_x", 0.0))),
            _fmt(float(params.get("pos_y", 0.0))),
        )
        for slot, key in (("d1", "datum_primary"),
                          ("d2", "datum_secondary"),
                          ("d3", "datum_tertiary")):
            d = str(params.get(key, "") or "").strip()
            if d:
                cmd += " %s=%s" % (slot, d)
        zone = str(params.get("zone_shape", "none") or "none").strip().lower()
        if zone and zone != "none":
            cmd += " shape=" + zone
        mat = str(params.get("material_modifier", "none") or "none").strip().lower()
        if mat and mat != "none":
            cmd += " mat=" + mat
        cmd += _leader_tokens(params)
        return cmd
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
    if method == "nx_export_iges":
        return {"status": "success", "path": resp.get("iges", params["path"]), "message": msg}
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
    if method == "nx_list_holes":
        holes = []
        for h in resp.get("holes") or []:
            holes.append(
                {
                    "index": int(h.get("index", 0)),
                    "face_index": int(h.get("face_index", 0)),
                    "tag": int(h.get("tag", 0)),
                    "kind": str(h.get("kind", "unknown")),
                    "diameter": float(h.get("diameter", 0.0)),
                    "radius": float(h.get("radius", 0.0)),
                    "depth": float(h.get("depth", 0.0)),
                    "x": float(h.get("x", 0.0)),
                    "y": float(h.get("y", 0.0)),
                    "z": float(h.get("z", 0.0)),
                    "area": float(h.get("area", 0.0)),
                    "sweep_rad": float(h.get("sweep_rad", 0.0)),
                    "full_wall": bool(h.get("full_wall", False)),
                    "containment": int(h.get("containment", 0)),
                    "uf_type": int(h.get("uf_type", 0)),
                    "axis_uf": [float(v) for v in (h.get("axis_uf") or [])],
                    "point_uf": [float(v) for v in (h.get("point_uf") or [])],
                    "err": str(h.get("err", "")),
                }
            )
        return {
            "status": "success",
            "count": sum(1 for h in holes if h["kind"] == "hole"),
            "cylinders": int(resp.get("cylinder_count", len(holes))),
            "face_count": int(resp.get("face_count", 0)),
            "holes": holes,
            "message": msg,
        }
    # ---- selection reflux + face provenance (made_by) -----------------------
    if method == "nx_get_selection":
        selection = []
        for s in resp.get("selection") or []:
            if isinstance(s, dict):
                owners = []
                for m in s.get("made_by") or []:
                    if isinstance(m, dict):
                        owners.append(
                            {
                                "feature": str(m.get("feature", "")),
                                "type": str(m.get("type", "")),
                            }
                        )
                selection.append(
                    {
                        "kind": str(s.get("kind", "unknown")),
                        "tag": int(s.get("tag", 0)),
                        "body_id": str(s.get("body_id", "")),
                        "index": int(s.get("face_index", s.get("edge_index", -1))),
                        "face_type": str(s.get("face_type", "")),
                        "name": str(s.get("name", "")),
                        "owner_count": int(s.get("owner_count", 0)),
                        "made_by": owners,
                        "err": str(s.get("err", "")),
                    }
                )
        return {
            "status": "success",
            "count": int(resp.get("count", len(selection))),
            "route": str(resp.get("route", "")),
            "note": str(resp.get("note", "")),
            "selection": selection,
            "message": msg,
        }
    if method == "nx_face_provenance":
        owners = []
        for m in resp.get("made_by") or []:
            if isinstance(m, dict):
                owners.append(
                    {
                        "feature": str(m.get("feature", "")),
                        "type": str(m.get("type", "")),
                    }
                )
        return {
            "status": "success",
            "body_id": str(resp.get("body_id", params.get("body_id", ""))),
            "face_index": int(resp.get("face_index", params.get("face_index", -1))),
            "tag": int(resp.get("tag", 0)),
            "face_type": str(resp.get("face_type", "")),
            "owner_count": int(resp.get("owner_count", 0)),
            "made_by": owners,
            "owner_err": str(resp.get("owner_err", "")),
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
        bodies = []
        for b in resp.get("per_body") or []:
            bodies.append(
                {
                    "body_id": str(b.get("body_id", "")),
                    "mass_kg": float(b.get("mass_kg", 0.0)),
                    "volume_mm3": float(b.get("volume_mm3", 0.0)),
                    "surface_area_mm2": float(b.get("surface_area_mm2", 0.0)),
                    "center_of_gravity": {
                        "x": float(b.get("cog_x_mm", 0.0)),
                        "y": float(b.get("cog_y_mm", 0.0)),
                        "z": float(b.get("cog_z_mm", 0.0)),
                    },
                }
            )
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
            "bodies": bodies,
            "message": msg,
        }
    if method == "nx_list_assembly":
        rows = []
        for r in resp.get("rows") or []:
            attrs_raw = r.get("attrs")
            attrs: dict[str, str] = {}
            if isinstance(attrs_raw, dict):
                for k, v in attrs_raw.items():
                    attrs[str(k)] = str(v)
            rows.append(
                {
                    "level": int(r.get("level", 1)),
                    "name": str(r.get("name", "")),
                    "display_name": str(r.get("display", "")),
                    "part_path": str(r.get("part", "")),
                    "children": int(r.get("children", 0)),
                    "qty": int(r.get("qty", 1)),
                    "attributes": attrs,
                }
            )
        return {
            "status": "success",
            "count": int(resp.get("count", len(rows))),
            "rows": rows,
            "message": msg,
        }
    if method == "nx_list_attributes":
        attrs_raw = resp.get("attrs")
        attributes: dict[str, str] = {}
        if isinstance(attrs_raw, dict):
            for k, v in attrs_raw.items():
                attributes[str(k)] = str(v)
        try:
            count = int(resp.get("attributes", len(attributes)))
        except (TypeError, ValueError):
            count = len(attributes)
        return {
            "status": "success",
            "count": count,
            "attributes": attributes,
            "message": msg,
        }
    if method == "nx_add_component":
        return {
            "status": "success",
            "component_name": str(resp.get("component", "")),
            "journal_id": str(resp.get("journal_id", "")),
            "part_path": str(resp.get("part_path", "")),
            "children": int(resp.get("children", 0)),
            "message": msg,
        }
    if method == "nx_create_explosion":
        return {"status": "success", "name": str(resp.get("explosion", "")), "message": msg}
    if method == "nx_explode_component":
        return {"status": "success", "component": str(resp.get("component", "")), "message": msg}
    if method == "nx_unexplode_component":
        return {"status": "success", "component": str(resp.get("component", "")), "message": msg}
    if method == "nx_list_explosions":
        return {
            "status": "success",
            "count": int(resp.get("count", 0)),
            "explosions": resp.get("explosions") or [],
            "message": msg,
        }
    if method == "nx_refset_add_bodies":
        return {"status": "success", "name": str(resp.get("refset", "")), "message": msg}
    if method == "nx_create_section_view":
        return {"status": "success", "view": str(resp.get("view", "")), "message": msg}
    if method == "nx_add_section_segment":
        return {"status": "success", "message": msg}
    if method == "nx_delete_view":
        return {"status": "success", "message": msg}
    if method == "nx_measure_angle":
        return {
            "status": "success",
            "angle_deg": float(resp.get("angle_deg", 0.0)),
            "acute_deg": float(resp.get("acute_deg", 0.0)),
            "message": msg,
        }
    if method == "nx_create_stock":
        bbox_min = resp.get("bbox_min") or [0.0, 0.0, 0.0]
        bbox_max = resp.get("bbox_max") or [0.0, 0.0, 0.0]
        util = resp.get("utilization_pct")
        return {
            "status": "success",
            "mode": str(resp.get("mode", params.get("mode", "box"))),
            "body_id": str(resp.get("body_id", "")),
            "feature_tag": int(resp.get("feature_tag", 0) or 0),
            "stock_volume_mm3": float(resp.get("stock_volume_mm3", 0.0) or 0.0),
            "part_volume_mm3": float(resp.get("part_volume_mm3", 0.0) or 0.0),
            "utilization_pct": None if util is None else float(util),
            "bbox_min": {
                "x": float(bbox_min[0]),
                "y": float(bbox_min[1]),
                "z": float(bbox_min[2]),
            },
            "bbox_max": {
                "x": float(bbox_max[0]),
                "y": float(bbox_max[1]),
                "z": float(bbox_max[2]),
            },
            "message": msg,
        }
    if method == "nx_create_table":
        return {
            "status": "success",
            "rows": int(resp.get("rows", 0)),
            "cols": int(resp.get("cols", 0)),
            "note_tag": int(resp.get("note_tag", 0) or 0),
            "tables_after": int(resp.get("tables_after", 0)),
            "cell_echo": str(resp.get("cell_echo", "")),
            "font": str(resp.get("font", "")),
            "message": msg,
        }
    if method == "nx_set_feature_suppressed":
        return {
            "status": "success",
            "feature_index": int(resp.get("feature_index", 0) or 0),
            "feature_name": str(resp.get("feature_name", "")),
            "suppressed_before": bool(resp.get("suppressed_before", False)),
            "suppressed_after": bool(resp.get("suppressed_after", False)),
            "suppressed_count": int(resp.get("suppressed_count", 0) or 0),
            "message": msg,
        }
    if method == "nx_check_interference":
        nx_count = resp.get("nx_count")
        return {
            "status": "success",
            "mode": str(resp.get("mode", params.get("asm") and "components" or "bodies")),
            "objects_checked": int(resp.get("objects", 0) or 0),
            "pairs_checked": int(resp.get("pairs_checked", 0) or 0),
            "loaded_children": int(resp.get("loaded_children", 0) or 0),
            "interference_count": int(resp.get("count", 0) or 0),
            "nx_count": None if nx_count is None else int(nx_count),
            "items": [str(x) for x in (resp.get("items") or [])],
            "set_deleted": bool(resp.get("set_deleted", False)),
            "message": msg,
        }
    if method == "nx_save_part_as":
        return {
            "status": "success",
            "saved_path": str(resp.get("saved_as", params.get("path", ""))),
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
