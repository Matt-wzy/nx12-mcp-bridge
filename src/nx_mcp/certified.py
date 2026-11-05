"""The explicitly certified v0.2 MCP tool surface."""

from __future__ import annotations

import asyncio
import csv
import json
import os
from typing import Annotated, Any, Literal, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from pydantic import BaseModel, Field

from nx_mcp import __version__
from nx_mcp.contracts import (
    AssemblyListResult,
    AttributeListResult,
    ComponentAddResult,
    ExportResult,
    ExtrudeResult,
    MassPropertiesResult,
    MeasureAngleResult,
    NXToolError,
    ObjectListResult,
    ObjectResult,
    OperationResult,
    PartResult,
    Point2D,
    StatusResult,
)
from nx_mcp.workspace import Workspace, WorkspaceViolation


class Point3D(BaseModel):
    """A 3D point in model space (mm)."""

    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    z: float = Field(allow_inf_nan=False)


class BridgeCaller(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


# GB/T 1182 中文名 -> Loader 英文枚举名（Loader 源码按 ANSI 代码页编译，
# 中文字面量只能放这边）。键不含空格，值小写英文枚举名。
_FCF_CHARACTERISTIC_ALIASES = {
    "直线度": "straightness",
    "平面度": "flatness",
    "圆度": "circularity",
    "圆柱度": "cylindricity",
    "线轮廓度": "profileofaline",
    "面轮廓度": "profileofasurface",
    "倾斜度": "angularity",
    "垂直度": "perpendicularity",
    "平行度": "parallelism",
    "位置度": "position",
    "同轴度": "concentricity",
    "对称度": "symmetry",
    "圆跳动": "circularrunout",
    "全跳动": "totalrunout",
}


# GB/T 324 中文名 -> Loader WeldSymbolType 英文枚举名（Loader 源码按 ANSI 代码
# 页编译，中文字面量只能放这边）。X 形坡口 = 双 V，用 side="both" + vbutt 表达。
_WELD_TYPE_ALIASES = {
    "角焊缝": "fillet",
    "角焊": "fillet",
    "i形": "squarebutt",
    "i形坡口": "squarebutt",
    "v形": "vbutt",
    "v形坡口": "vbutt",
    "单边v形": "bevelbutt",
    "单边v形坡口": "bevelbutt",
    "k形": "kgroove",
    "k形坡口": "kgroove",
    "u形": "ubutt",
    "u形坡口": "ubutt",
    "单边u形": "jbutt",
    "单边u形坡口": "jbutt",
    "塞焊": "plugslot",
    "槽焊": "plugslot",
    "点焊": "spot",
    "缝焊": "seam",
    "卷边": "flarev",
    "喇叭形": "flarebevel",
    "封底焊": "backing",
}

# 焊缝表面形状（ContourSymbolType）与加工方法（FinishSymbolType）中文名
_WELD_CONTOUR_ALIASES = {"凸面": "convex", "凹面": "concave", "平面": "flat", "平齐": "flush"}
_WELD_FINISH_ALIASES = {
    "去毛刺": "chipping",
    "打磨": "grinding",
    "锤击": "hammering",
    "机加工": "machining",
    "滚压": "rolling",
    "敲击": "peening",
}


def _pick_attr(attrs: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first attribute value matching any key (case-insensitive)."""
    lowered = {str(k).strip().lower(): str(v) for k, v in (attrs or {}).items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val:
            return val
    return ""


def _write_bom_csv(path: str, rows: list[dict[str, Any]]) -> str:
    """Write flattened assembly rows as a CSV bill of materials.

    utf-8-sig so Excel opens Chinese part names correctly; one row per
    component instance. qty totals the instances of the same prototype part;
    material / drawing_no are pulled from the prototype part's user
    attributes (MATERIAL/材料, DB_PART_NO/图号/PART_NO/DWG_NO/名称).
    """
    out_abs = os.path.abspath(path)
    outdir = os.path.dirname(out_abs)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    with open(out_abs, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["level", "name", "display_name", "part_path", "children",
             "qty", "material", "drawing_no"]
        )
        for row in rows:
            attrs = row.get("attributes") or {}
            # NX renames a user "MATERIAL" attribute to "NX_Material" on save,
            # so both spellings (and the Chinese 材料/材质) are looked up.
            material = _pick_attr(
                attrs, ("MATERIAL", "NX_MATERIAL", "材料", "MATERIAL_SPEC", "材质")
            )
            drawing_no = _pick_attr(
                attrs, ("DB_PART_NO", "图号", "PART_NO", "DWG_NO", "DRAWING_NO", "件号")
            )
            writer.writerow(
                [
                    int(row.get("level", 1)),
                    str(row.get("name", "")),
                    str(row.get("display_name", "")),
                    str(row.get("part_path", "")),
                    int(row.get("children", 0)),
                    int(row.get("qty", 1)),
                    material,
                    drawing_no,
                ]
            )
    return "exported %d BOM row(s) to %s" % (len(rows), out_abs)


# ---- 工序卡（process card）模板 --------------------------------------------
# Data model: the part's user attributes ARE the process card. Per-operation
# entries follow OP<序号>_<字段> (e.g. OP10_NAME / OP10_设备), set via
# nx_set_attribute; part-level 名称/图号/材料 come from the same attribute pool
# as the title block. One regex extracts the ops, so the field vocabulary can
# grow without a schema change.
_OP_FIELDS_ORDER = ("name", "content", "machine", "fixture", "tool", "cutting", "time")

_OP_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("name", "工序名", "工序名称", "名称"),
    "content": ("content", "工序内容", "内容"),
    "machine": ("machine", "设备", "机床"),
    "fixture": ("fixture", "clamp", "装夹", "装夹方式"),
    "tool": ("tool", "刀具"),
    "cutting": ("cutting", "切削参数", "切削用量", "切削"),
    "time": ("time", "工时", "单件工时"),
}

_OP_FIELD_LABELS = {
    "name": "工序名称",
    "content": "工序内容",
    "machine": "设备",
    "fixture": "装夹",
    "tool": "刀具",
    "cutting": "切削参数",
    "time": "单件工时",
}

_HEADER_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("名称", "name", "part_name", "零件名称"),
    "drawing_no": ("图号", "db_part_no", "part_no", "dwg_no", "drawing_no", "件号"),
    "material": ("material", "nx_material", "材料", "材质"),
    "scale": ("比例", "scale"),
}

_HEADER_LABELS = {
    "name": "零件名称",
    "drawing_no": "图号",
    "material": "材料",
    "scale": "比例",
}


def _parse_process_ops(
    attributes: dict[str, str],
) -> tuple[list[tuple[int, dict[str, str]]], list[str]]:
    """Group OP<nn>_<field> attributes into sorted ops; return (ops, unknown).

    ops = [(op_number, {canonical_field: value, ...})] sorted numerically
    (OP5 < OP10 < OP100, plain string sort would give OP10 < OP100 < OP5).
    Bare OP<nn> keys hold an op title (no field). Unknown field tokens are
    reported so typos like OP10_MACHNE surface instead of silently vanishing.
    """
    import re

    ops: dict[int, dict[str, str]] = {}
    unknown: set[str] = set()
    lookup = {k: canon for canon, keys in _OP_FIELD_KEYS.items() for k in keys}
    for key, value in (attributes or {}).items():
        m = re.match(r"^OP\s*(\d+)(?:[._\-\s]+(.+))?$", str(key).strip(), re.IGNORECASE)
        if not m:
            continue
        entry = ops.setdefault(int(m.group(1)), {})
        field = (m.group(2) or "").strip()
        if not field:
            entry.setdefault("title", str(value))
            continue
        canon = lookup.get(field.lower())
        if canon is None:
            unknown.add(field)
        elif str(value).strip():
            entry[canon] = str(value).strip()
    return sorted(ops.items()), sorted(unknown)


def _render_process_card_text(
    header: dict[str, str], ops: list[tuple[int, dict[str, str]]]
) -> str:
    lines = ["机械加工工艺卡片", "=" * 36]
    pairs = [
        "%s: %s" % (_HEADER_LABELS[k], header[k])
        for k in ("name", "drawing_no", "material", "scale")
        if header.get(k)
    ]
    lines.append(" | ".join(pairs) if pairs else "(零件头信息未设)")
    lines.append("-" * 36)
    for num, fields in ops:
        title = fields.get("name") or fields.get("title") or ("工序 %d" % num)
        lines.append("工序 %d  %s" % (num, title))
        for field in _OP_FIELDS_ORDER:
            if field == "name":
                continue
            value = fields.get(field)
            if value:
                lines.append("  %s: %s" % (_OP_FIELD_LABELS[field], value))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_process_card(
    path: str, attributes: dict[str, str], fmt: str
) -> tuple[str, list[str]]:
    """Write the process card as text or CSV; return (message, warnings)."""
    import re

    header = {
        canon: str(value)
        for canon, keys in _HEADER_KEYS.items()
        for key, value in (attributes or {}).items()
        if str(key).strip().lower() in keys and str(value).strip()
    }
    ops, unknown = _parse_process_ops(attributes)
    if not ops:
        raise NXToolError(
            "NX_NO_PROCESS_OPS",
            "no OP<n>_<field> attributes on the part; set them with "
            "nx_set_attribute first (e.g. OP10_NAME, OP20_CONTENT, OP20_设备)",
        )
    warnings = [
        "unknown field 'OP<n>_%s' (typo? accepted: %s)"
        % (f, ",".join(_OP_FIELD_LABELS.values()))
        for f in unknown
    ]
    missing = [
        "OP%d missing %s" % (num, _OP_FIELD_LABELS[f])
        for num, fields in ops
        for f in ("name", "content", "machine")
        if not fields.get(f)
    ]
    warnings.extend(missing)

    out_abs = os.path.abspath(path)
    outdir = os.path.dirname(out_abs)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    if fmt == "csv":
        with open(out_abs, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["工序号"] + [_OP_FIELD_LABELS[f] for f in _OP_FIELDS_ORDER]
            )
            for num, fields in ops:
                writer.writerow(
                    [num] + [fields.get(f, "") for f in _OP_FIELDS_ORDER]
                )
    else:
        with open(out_abs, "w", encoding="utf-8") as fh:
            fh.write(_render_process_card_text(header, ops))
    message = "exported %d operation(s) to %s (%s)" % (len(ops), out_abs, fmt)
    if warnings:
        message += "; " + "; ".join(warnings)
    return message, warnings


CERTIFIED_TOOL_NAMES = {
    "nx_status",
    "nx_create_part",
    "nx_open_part",
    "nx_save_part",
    "nx_close_part",
    "nx_export_step",
    "nx_list_sketches",
    "nx_list_bodies",
    "nx_list_features",
    "nx_list_edges",
    "nx_list_faces",
    "nx_create_sketch",
    "nx_sketch_line",
    "nx_sketch_rectangle",
    "nx_sketch_circle",
    "nx_sketch_arc",
    "nx_finish_sketch",
    "nx_extrude",
    "nx_hole",
    "nx_counterbore_hole",
    "nx_countersink_hole",
    "nx_shell",
    "nx_edge_blend",
    "nx_chamfer",
    "nx_unite",
    "nx_revolve",
    "nx_mirror",
    "nx_linear_pattern",
    "nx_circular_pattern",
    "nx_undo",
    "nx_fit_view",
    "nx_release",
    # drafting (2D drawing) - backed by the C# Loader on NX 12
    "nx_create_drawing_sheet",
    "nx_create_projected_view",
    "nx_list_drawing_sheets",
    "nx_list_drafting_views",
    "nx_update_views",
    "nx_create_dimension",
    "nx_create_angle_dimension",
    "nx_list_dimensions",
    "nx_list_annotations",
    "nx_create_note",
    "nx_create_diameter_dimension",
    "nx_create_radius_dimension",
    "nx_delete_annotation",
    "nx_export_pdf",
    # DXF/DWG：NX 内 Commit() 从不 spawn translator（实测），由 Python 侧
    # 进程外官方 translator 实现（loader_bridge._export_dwg_external）
    "nx_export_dwg",
    "nx_measure_distance",
    # 两直线边夹角（装夹/工序定位常用）
    "nx_measure_angle",
    # 质量属性查询（体积/表面积/重心/质量）——工艺材料定额/毛坯选用常用
    "nx_mass_properties",
    # 简单剖视图（UFUN CreateSimpleSxview 路线，工艺图纸核心刚需）
    "nx_create_section_view",
    # 阶梯剖（向剖切线追加割段，NX 自动转折更新；多孔零件工艺图常用）
    "nx_add_section_segment",
    # 删图纸视图（改图前必用）
    "nx_delete_view",
    # 装配树拍平 + BOM CSV 导出（工艺路线表基础）
    "nx_list_assembly",
    "nx_export_bom_csv",
    # 装配添加组件（工序装配/合件建模；装配工艺路线第一步）
    "nx_add_component",
    # 焊接符号（GB/T 324 焊缝标注，WeldCollection.CreateLineWeldBuilder 路线）
    "nx_create_weld_symbol",
    # NOTE: nx_create_symb_thread is implemented (UF_MODL.CreateSymbThread route)
    # but NOT exposed: the NX12 .NET UF wrapper's SymbThreadData struct has NO
    # MarshalAs attributes on its Double[]/String fields (inline C arrays vs
    # marshalled pointers), so the struct reaches NX with a garbled layout and
    # EVERY variant fails with "未定义目标实体" (probed 2026-11-05, matrix in
    # 受阻项调研档案 section 9). A tool that can never succeed is worse
    # than no tool.
    # 工序卡导出（OP<nn>_<字段> 属性模板 → 结构化工艺卡片，纯 Python 侧）
    "nx_export_process_card",
    # 球标/ID 符号（装配工艺零件序号气泡，与 BOM 导出配套）
    "nx_create_id_symbol",
    # 基准符号（GD&T 基准字母框，GB/T 17851；与形状公差框格配套）
    "nx_create_datum_symbol",
    # 形位公差框（GD&T 特征控制框，GB/T 1182；与基准符号配套，工艺图刚需）
    "nx_create_gdt_frame",
    # 对称中心线（轴类零件轴线，SymmetricalCenterlineBuilder，管道实测通过）
    "nx_create_centerline",
    "nx_create_roughness",
    "nx_create_title_block",
    "nx_set_attribute",
    "nx_list_attributes",
    # WAVE 链接 / 草图读取 / 尺寸改值（NX12 适配，2026-11 已在管道实测）
    "nx_create_wave_link",
    "nx_read_sketch",
    "nx_set_dimension_value",
    # NOTE: nx_create_sheet_border is implemented but NOT exposed. Commit() always
    # fails with "宽度必须大于零或小于水平或竖直区域大小..." (invalid zone width).
    # Likely cause: NXOpen.Diagramming is NX's *diagramming* (schematic) app, not
    # the drafting app, so it does not apply to drafting sheets. Zone sizes were
    # set (no warnings in log) and it still failed, so the route is wrong.
    # NOTE: nx_create_detail_view is implemented (UFDraw.AddDetailView route) but
    # NOT exposed yet: the view/boundary/label are created, but the enlarged
    # geometry does not render, and the pending refPt double[2] fix is unbuilt/
    # untested (deploy ran out of time 2026-11-04). See 受阻项调研档案
    # section 4 for the full matrix and the exact next steps. A tool that
    # silently produces an empty view is worse than no tool.
}


def create_certified_server(
    bridge: BridgeCaller,
    workspace: Workspace | None = None,
    *,
    enable_experimental: bool = False,
    enable_journal: bool = False,
) -> MCPServer:
    mcp = MCPServer(
        "nx-mcp",
        instructions="Certified Siemens NX tools for a local NX session.",
        version=__version__,
    )

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await bridge.call(method, params)
        except NXToolError as error:
            raise MCPToolError(json.dumps(error.as_dict(), ensure_ascii=False)) from error

    def resolve_path(path: str) -> str:
        if workspace is None:
            raise MCPToolError(
                json.dumps(
                    NXToolError(
                        "NX_WORKSPACE_NOT_CONFIGURED",
                        "NX_MCP_WORKSPACE must be configured before file operations are used.",
                    ).as_dict(),
                    ensure_ascii=False,
                )
            )
        try:
            return str(workspace.resolve(path))
        except WorkspaceViolation as error:
            tool_error = NXToolError("NX_PATH_OUTSIDE_WORKSPACE", str(error))
            raise MCPToolError(json.dumps(tool_error.as_dict(), ensure_ascii=False)) from error

    @mcp.tool()
    async def nx_status() -> StatusResult:
        """Report bridge, NX version, and active-part status."""
        return StatusResult(**await call("nx_status", {}))

    @mcp.tool()
    async def nx_create_part(path: str, units: Literal["mm", "inch"] = "mm") -> PartResult:
        """Create a part inside the configured workspace."""
        return PartResult(
            **await call("nx_create_part", {"path": resolve_path(path), "units": units})
        )

    @mcp.tool()
    async def nx_open_part(path: str) -> PartResult:
        """Open a part from the configured workspace."""
        return PartResult(**await call("nx_open_part", {"path": resolve_path(path)}))

    @mcp.tool()
    async def nx_save_part() -> OperationResult:
        """Save the active work part."""
        return OperationResult(**await call("nx_save_part", {}))

    @mcp.tool()
    async def nx_close_part(save: bool = True) -> OperationResult:
        """Close the active work part, optionally saving it first."""
        return OperationResult(**await call("nx_close_part", {"save": save}))

    @mcp.tool()
    async def nx_export_step(path: str) -> ExportResult:
        """Export the active work part as STEP inside the configured workspace."""
        return ExportResult(**await call("nx_export_step", {"path": resolve_path(path)}))

    @mcp.tool()
    async def nx_list_sketches() -> ObjectListResult:
        """List sketches in the active work part."""
        return ObjectListResult(**await call("nx_list_sketches", {}))

    @mcp.tool()
    async def nx_list_bodies() -> ObjectListResult:
        """List bodies in the active work part."""
        return ObjectListResult(**await call("nx_list_bodies", {}))

    @mcp.tool()
    async def nx_list_features() -> ObjectListResult:
        """List features in the active work part."""
        return ObjectListResult(**await call("nx_list_features", {}))

    @mcp.tool()
    async def nx_list_edges(body_id: str) -> dict[str, Any]:
        """Inspect every edge of a body: index, curve type, endpoints, midpoint,
        length, bounding box, direction and adjacent face count. Read-only."""
        return await call("nx_list_edges", {"body_id": body_id})

    @mcp.tool()
    async def nx_list_faces(body_id: str) -> dict[str, Any]:
        """Inspect every face of a body: index, tag, face type, centroid, area,
        normal (planar only) and adjacent edge count. Read-only."""
        return await call("nx_list_faces", {"body_id": body_id})

    @mcp.tool()
    async def nx_create_sketch(
        plane: Literal["XY", "XZ", "YZ"] = "XY", name: str | None = None
    ) -> ObjectResult:
        """Create and activate a sketch on a principal datum plane."""
        return ObjectResult(**await call("nx_create_sketch", {"plane": plane, "name": name}))

    @mcp.tool()
    async def nx_sketch_line(sketch_id: str, start: Point2D, end: Point2D) -> ObjectResult:
        """Add a line to an explicit sketch reference."""
        return ObjectResult(
            **await call(
                "nx_sketch_line",
                {"sketch_id": sketch_id, "start": start.model_dump(), "end": end.model_dump()},
            )
        )

    @mcp.tool()
    async def nx_sketch_rectangle(
        sketch_id: str, corner1: Point2D, corner2: Point2D
    ) -> ObjectListResult:
        """Add a rectangle to an explicit sketch reference."""
        return ObjectListResult(
            **await call(
                "nx_sketch_rectangle",
                {
                    "sketch_id": sketch_id,
                    "corner1": corner1.model_dump(),
                    "corner2": corner2.model_dump(),
                },
            )
        )

    @mcp.tool()
    async def nx_sketch_circle(
        sketch_id: str,
        center: Point2D,
        diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
    ) -> ObjectResult:
        """Add a circle (center + diameter) to an explicit sketch reference."""
        return ObjectResult(
            **await call(
                "nx_sketch_circle",
                {"sketch_id": sketch_id, "center": center.model_dump(), "diameter": diameter},
            )
        )

    @mcp.tool()
    async def nx_sketch_arc(
        sketch_id: str,
        center: Point2D,
        radius: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        start_angle: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0,
        end_angle: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 90.0,
    ) -> ObjectResult:
        """Add an arc (center, radius, start/end degrees) to an explicit sketch reference."""
        return ObjectResult(
            **await call(
                "nx_sketch_arc",
                {
                    "sketch_id": sketch_id,
                    "center": center.model_dump(),
                    "radius": radius,
                    "start_angle": start_angle,
                    "end_angle": end_angle,
                },
            )
        )

    @mcp.tool()
    async def nx_finish_sketch(sketch_id: str) -> ObjectResult:
        """Deactivate and finish an explicit sketch reference."""
        return ObjectResult(**await call("nx_finish_sketch", {"sketch_id": sketch_id}))

    @mcp.tool()
    async def nx_extrude(
        sketch_id: str,
        distance: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        reverse: bool = False,
        start_offset: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0,
        operation: Literal["create", "subtract"] = "create",
        target_body_id: str | None = None,
    ) -> ExtrudeResult:
        """Extrude a sketch into a new body, or subtract it from an existing target body."""
        return ExtrudeResult(
            **await call(
                "nx_extrude",
                {
                    "sketch_id": sketch_id,
                    "distance": distance,
                    "reverse": reverse,
                    "start_offset": start_offset,
                    "operation": operation,
                    "target_body_id": target_body_id,
                },
            )
        )

    @mcp.tool()
    async def nx_hole(
        body_id: str,
        center: Point2D,
        diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        depth: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        start_offset: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0,
    ) -> ObjectResult:
        """Create a hole on a target body (circle sketch + boolean subtract)."""
        return ObjectResult(
            **await call(
                "nx_hole",
                {
                    "body_id": body_id,
                    "center": center.model_dump(),
                    "diameter": diameter,
                    "depth": depth,
                    "start_offset": start_offset,
                },
            )
        )

    @mcp.tool()
    async def nx_counterbore_hole(
        body_id: str,
        center: Point2D,
        hole_diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        hole_depth: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        counterbore_diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        counterbore_depth: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        start_offset: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0,
    ) -> ObjectResult:
        """Create a coaxial counterbore hole: a smaller deep through hole plus a
        larger shallow counterbore from the top face. Independent from nx_hole."""
        if counterbore_diameter <= hole_diameter:
            raise ValueError("counterbore_diameter must be > hole_diameter")
        if counterbore_depth >= hole_depth:
            raise ValueError("counterbore_depth must be < hole_depth")
        return ObjectResult(
            **await call(
                "nx_counterbore_hole",
                {
                    "body_id": body_id,
                    "center": center.model_dump(),
                    "hole_diameter": hole_diameter,
                    "hole_depth": hole_depth,
                    "counterbore_diameter": counterbore_diameter,
                    "counterbore_depth": counterbore_depth,
                    "start_offset": start_offset,
                },
            )
        )

    @mcp.tool()
    async def nx_countersink_hole(
        body_id: str,
        center: Point2D,
        hole_diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        hole_depth: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        countersink_diameter: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        countersink_angle: Annotated[float, Field(gt=0, lt=180, allow_inf_nan=False)] = 90.0,
        start_offset: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0,
    ) -> ObjectResult:
        """Create a coaxial countersink (conical) hole: a smaller deep through
        cylinder plus a conical countersink from the top face. Independent of
        nx_hole / nx_counterbore_hole."""
        if countersink_diameter <= hole_diameter:
            raise ValueError("countersink_diameter must be > hole_diameter")
        return ObjectResult(
            **await call(
                "nx_countersink_hole",
                {
                    "body_id": body_id,
                    "center": center.model_dump(),
                    "hole_diameter": hole_diameter,
                    "hole_depth": hole_depth,
                    "countersink_diameter": countersink_diameter,
                    "countersink_angle": countersink_angle,
                    "start_offset": start_offset,
                },
            )
        )

    @mcp.tool()
    async def nx_shell(
        body_id: str,
        thickness: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        remove_face_index: int,
        inward: bool = True,
    ) -> ObjectResult:
        """Shell (hollow) a body: remove one face as an opening and keep a
        constant wall thickness. First version removes a single face; inward=True
        keeps the outer dimensions."""
        return ObjectResult(
            **await call(
                "nx_shell",
                {
                    "body_id": body_id,
                    "thickness": thickness,
                    "remove_face_index": remove_face_index,
                    "inward": inward,
                },
            )
        )

    @mcp.tool()
    async def nx_edge_blend(
        body_id: str,
        radius: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        edge_indices: list[int] | None = None,
    ) -> ObjectResult:
        """Create an edge blend (fillet) on edges of a body; default all edges."""
        return ObjectResult(
            **await call(
                "nx_edge_blend",
                {"body_id": body_id, "radius": radius, "edge_indices": edge_indices},
            )
        )

    @mcp.tool()
    async def nx_chamfer(
        body_id: str,
        offset: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        edge_indices: list[int] | None = None,
    ) -> ObjectResult:
        """Create a symmetric chamfer on edges of a body; default all edges."""
        return ObjectResult(
            **await call(
                "nx_chamfer",
                {"body_id": body_id, "offset": offset, "edge_indices": edge_indices},
            )
        )

    @mcp.tool()
    async def nx_unite(
        target_body_id: str,
        tool_body_ids: list[str],
    ) -> ObjectResult:
        """Boolean-unite one or more tool bodies into the target body.

        The target body keeps its id; the tool bodies are consumed and
        disappear from ``nx_list_bodies`` after the boolean.
        """
        return ObjectResult(
            **await call(
                "nx_unite",
                {"target_body_id": target_body_id, "tool_body_ids": tool_body_ids},
            )
        )

    @mcp.tool()
    async def nx_revolve(
        sketch_id: str,
        axis_start: Point2D,
        axis_end: Point2D,
        angle: Annotated[float, Field(gt=0, le=360, allow_inf_nan=False)] = 360.0,
        reverse: bool = False,
    ) -> ObjectResult:
        """Revolve a closed sketch section around an in-sketch 2D axis.

        Creates a new solid only (no unite/subtract). ``axis_start`` and
        ``axis_end`` are sketch XY coordinates defining the revolution axis;
        ``angle`` is the sweep angle in degrees (0 < angle <= 360).
        """
        return ObjectResult(
            **await call(
                "nx_revolve",
                {
                    "sketch_id": sketch_id,
                    "axis_start": axis_start.model_dump(),
                    "axis_end": axis_end.model_dump(),
                    "angle": angle,
                    "reverse": reverse,
                },
            )
        )

    @mcp.tool()
    async def nx_mirror(
        body_id: str,
        plane: Literal["XY", "XZ", "YZ"],
        offset: Annotated[float, Field(allow_inf_nan=False)] = 0.0,
    ) -> ObjectResult:
        """Mirror a body about a datum plane.

        ``plane`` selects the mirror datum plane (XY/XZ/YZ); ``offset`` shifts
        that plane along its normal by the given mm value (default 0). The
        mirrored body is created as an independent body and is NOT united with
        the source; use ``nx_unite`` if merging is required.
        """
        return ObjectResult(
            **await call(
                "nx_mirror",
                {"body_id": body_id, "plane": plane, "offset": offset},
            )
        )

    @mcp.tool()
    async def nx_linear_pattern(
        body_id: str,
        direction: Literal["X", "Y", "Z"],
        count: Annotated[int, Field(ge=2, le=1000)],
        spacing: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        reverse: bool = False,
    ) -> ObjectListResult:
        """Pattern a body linearly along X/Y/Z.

        ``count`` is the total number of instances including the original
        (count=4 yields 4 bodies); ``spacing`` is the per-instance delta in mm.
        The original body is kept and new instances are independent bodies
        (NOT united); use ``nx_unite`` if merging is required.
        """
        return ObjectListResult(
            **await call(
                "nx_linear_pattern",
                {
                    "body_id": body_id,
                    "direction": direction,
                    "count": count,
                    "spacing": spacing,
                    "reverse": reverse,
                },
            )
        )

    @mcp.tool()
    async def nx_circular_pattern(
        body_id: str,
        axis: Literal["X", "Y", "Z"],
        center: Point3D,
        count: Annotated[int, Field(ge=2, le=1000)],
        angle: Annotated[float, Field(gt=0, le=360, allow_inf_nan=False)],
        reverse: bool = False,
    ) -> ObjectListResult:
        """Pattern a body circularly about an axis through ``center``.

        ``count`` is the total number of instances including the original
        (count=4 yields 4 bodies); ``angle`` is the total sweep in degrees
        (360.0 distributes evenly around a full circle). The original body is
        kept and new instances are independent bodies (NOT united); use
        ``nx_unite`` if merging is required.
        """
        return ObjectListResult(
            **await call(
                "nx_circular_pattern",
                {
                    "body_id": body_id,
                    "axis": axis,
                    "center": {"x": center.x, "y": center.y, "z": center.z},
                    "count": count,
                    "angle": angle,
                    "reverse": reverse,
                },
            )
        )

    @mcp.tool()
    async def nx_undo() -> OperationResult:
        """Undo the last visible NX MCP operation."""
        return OperationResult(**await call("nx_undo", {}))

    @mcp.tool()
    async def nx_fit_view() -> OperationResult:
        """Fit the active modeling view."""
        return OperationResult(**await call("nx_fit_view", {}))

    @mcp.tool()
    async def nx_release() -> OperationResult:
        """Clear the current task state and return NX to normal interaction.

        With the resident C# Loader backend, the Loader and named pipe remain
        alive for the entire NX session; nx_release does not unload the plugin
        or require Alt+F8 to start it again.
        """
        return OperationResult(**await call("nx_release", {}))

    # ---- drafting (2D drawing) -------------------------------------------
    # Executed by the resident C# Loader. Sheet and view names are tokenised in
    # the pipe protocol, so they must not contain spaces.

    @mcp.tool()
    async def nx_create_drawing_sheet(
        name: str = "MCP_SHEET_1",
        size: Literal["A0", "A1", "A2", "A3", "A4"] = "A4",
        projection: Literal["first", "third"] = "first",
        scale: float = 1.0,
        model_view: str = "Top",
    ) -> OperationResult:
        """Create a drawing sheet and place a base view of the given model view.

        The part must already contain geometry for the base view to be meaningful.
        """
        return OperationResult(
            **await call(
                "nx_create_drawing_sheet",
                {
                    "name": name,
                    "size": size,
                    "projection": projection,
                    "scale": scale,
                    "model_view": model_view,
                },
            )
        )

    @mcp.tool()
    async def nx_create_projected_view(
        x: float,
        y: float,
        parent_view_id: str = "",
        name: str = "",
    ) -> OperationResult:
        """Project a view from an existing drafting view at sheet position (x, y).

        Leave parent_view_id empty to use the first drafting view on the sheet.
        """
        return OperationResult(
            **await call(
                "nx_create_projected_view",
                {"x": x, "y": y, "parent_view_id": parent_view_id, "name": name},
            )
        )

    @mcp.tool()
    async def nx_list_drawing_sheets() -> OperationResult:
        """List the drawing sheets in the active part."""
        return OperationResult(**await call("nx_list_drawing_sheets", {}))

    @mcp.tool()
    async def nx_list_drafting_views() -> OperationResult:
        """List the drafting views on the current drawing sheet."""
        return OperationResult(**await call("nx_list_drafting_views", {}))

    @mcp.tool()
    async def nx_list_dimensions() -> OperationResult:
        """Read back every dimension in the part: measured size and tolerance.

        Returns one entry per dimension, formatted as
        "<journal_id>|<name>|size=<value>|tol=<upper>/<lower>|<tolerance_type>".
        Use this to verify dimensions you just created, or to READ an existing
        NX 2D drawing: sizes and tolerances are recovered from the model, so an
        agent can understand what a drawing specifies without opening the UI.
        """
        return OperationResult(**await call("nx_list_dimensions", {}))

    @mcp.tool()
    async def nx_list_annotations() -> OperationResult:
        """Read the non-dimension annotations of a drawing: notes, GD&T, roughness.

        Each entry is prefixed by kind:
          "NOTE|<journal_id>|<text>"            drafting / technical-requirement text
          "GDT|<journal_id>|<characteristic>|tol=<value>|datum=<d1>,<d2>,<d3>"
          "ROUGH|<journal_id>"                  surface finish symbol
        Use this together with nx_list_dimensions to fully understand an existing
        NX 2D process drawing.
        """
        return OperationResult(**await call("nx_list_annotations", {}))

    @mcp.tool()
    async def nx_create_radius_dimension(
        view_id: str = "",
        body_id: str = "",
        edge_index: int = 0,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        dim_name: str = "MCP_RADIUS",
    ) -> OperationResult:
        """Create a RADIUS dimension on one edge (fillets, arcs, rounds).

        edge_index is the 0-based index from nx_list_edges on the given body.
        """
        return OperationResult(
            **await call(
                "nx_create_radius_dimension",
                {
                    "view_id": view_id,
                    "body_id": body_id,
                    "edge_index": edge_index,
                    "pos_x": pos_x,
                    "pos_y": pos_y,
                    "dim_name": dim_name,
                },
            )
        )

    @mcp.tool()
    async def nx_delete_annotation(annotation_id: str) -> OperationResult:
        """Delete a note / dimension / GDT / roughness annotation.

        Pass either its journal id or name (e.g. "LEN_120", "HANDLE R-5132"), or a
        "kind:index" selector: note:0, dim:1, gdt:0, rough:0 (0-based, in the order
        returned by nx_list_dimensions / nx_list_annotations).
        Useful for cleaning up test annotations that nx_undo cannot reach.
        """
        return OperationResult(
            **await call("nx_delete_annotation", {"annotation_id": annotation_id})
        )

    @mcp.tool()
    async def nx_export_pdf(path: str, sheet_name: str = "") -> OperationResult:
        """Export drawings to a PDF file.

        Pass sheet_name to export that one sheet; pass "ALL" to export EVERY
        sheet of the part into ONE multi-page PDF (a full drawing package in a
        single call); leave empty for the current sheet. Use
        nx_list_drawing_sheets to see available sheet names.
        """
        return OperationResult(
            **await call("nx_export_pdf", {"path": path, "sheet_name": sheet_name})
        )

    @mcp.tool()
    async def nx_export_dwg(path: str, sheet_name: str = "") -> ExportResult:
        """Export the active part's drawing to a DXF or DWG file.

        Runs the official NX DXFDWG translator out-of-process: the in-NX
        DxfdwgCreator.Commit() silently does nothing in a headless session
        (verified 2026-11-04). The part is saved to disk first, so the export
        reflects the current session state. Pass sheet_name to pin the drawing
        list; omit it to translate everything. Use .dxf or .dwg as the
        extension - laser/nesting workflows usually want .dxf.
        """
        return ExportResult(
            **await call(
                "nx_export_dwg",
                {"path": resolve_path(path), "sheet_name": sheet_name},
            )
        )

    @mcp.tool()
    async def nx_create_roughness(
        pos_x: float = 20.0,
        pos_y: float = 20.0,
        value: str = "1.6",
        leader_view_id: str = "",
        leader_body_id: str = "",
        leader_edge_index: int = -1,
        leader_side: Literal["", "left", "right"] = "",
    ) -> OperationResult:
        """Create a surface finish (roughness) symbol on the current drawing sheet.

        value goes into the symbol's a1 slot - e.g. "1.6" for Ra 1.6, "3.2" for Ra 3.2.
        Position is in sheet coordinates (mm).

        Optional leader line (GB/T 131 加工面指向): pass leader_view_id +
        leader_body_id + leader_edge_index together (edge from nx_list_edges
        on the body) and the symbol points at that surface edge inside the
        view; leader_side nudges which side the stub leaves from.
        """
        params: dict[str, Any] = {"pos_x": pos_x, "pos_y": pos_y, "value": value}
        if leader_view_id:
            params["leader_view_id"] = leader_view_id
        if leader_body_id:
            params["leader_body_id"] = leader_body_id
        if leader_edge_index >= 0:
            params["leader_edge_index"] = leader_edge_index
        if leader_side:
            params["leader_side"] = leader_side
        return OperationResult(**await call("nx_create_roughness", params))

    @mcp.tool()
    async def nx_create_title_block(
        pos_x: float = 0.0,
        pos_y: float = 0.0,
    ) -> OperationResult:
        """Place a title block on the current drawing sheet.

        Position is in sheet coordinates (mm); typically bottom-right, e.g.
        pos_x=340, pos_y=5 for an A3 landscape sheet.
        """
        return OperationResult(
            **await call(
                "nx_create_title_block", {"pos_x": pos_x, "pos_y": pos_y}
            )
        )

    @mcp.tool()
    async def nx_set_attribute(name: str, value: str) -> OperationResult:
        """Set a string attribute on the part.

        This is how a title block gets its CONTENT: title-block cells usually
        reference part attributes. Typical names: 名称 / 图号 / 材料 / 设计 /
        审核 / 比例 / 单位. Value may contain spaces.
        """
        return OperationResult(
            **await call("nx_set_attribute", {"name": name, "value": value})
        )

    @mcp.tool()
    async def nx_list_attributes() -> AttributeListResult:
        """Read back all user attributes set on the part (title-block content).

        Returns the structured ``attributes`` dict plus count. This is both the
        title-block readback and the data source for nx_export_process_card:
        set per-operation entries like OP10_NAME / OP20_CONTENT via
        nx_set_attribute, then export the card.
        """
        return AttributeListResult(**await call("nx_list_attributes", {}))

    @mcp.tool()
    async def nx_measure_distance(
        body_id: str = "",
        edge_index_1: int = 0,
        edge_index_2: int = 0,
    ) -> OperationResult:
        """Measure the minimum 3D distance between two edges of a body.

        This is a QUERY: no annotation is created. It returns the numeric distance
        (mm) plus the two closest points, so an agent can verify geometry instead of
        guessing. Indices are 0-based from nx_list_edges.
        """
        return OperationResult(
            **await call(
                "nx_measure_distance",
                {
                    "body_id": body_id,
                    "edge_index_1": edge_index_1,
                    "edge_index_2": edge_index_2,
                },
            )
        )

    @mcp.tool()
    async def nx_measure_angle(
        body_id: str = "",
        edge_index_1: int = 0,
        edge_index_2: int = 0,
    ) -> MeasureAngleResult:
        """Measure the angle between two STRAIGHT edges of a body.

        Query only - nothing is created. Returns the angle in degrees in [0,180]
        plus its acute complement in [0,90] (fixture/setup work usually wants the
        acute one). Both indices are 0-based from nx_list_edges; edges must be
        linear (the error names the types when they are not).
        """
        return MeasureAngleResult(
            **await call(
                "nx_measure_angle",
                {
                    "body_id": body_id,
                    "edge_index_1": edge_index_1,
                    "edge_index_2": edge_index_2,
                },
            )
        )

    @mcp.tool()
    async def nx_mass_properties(
        density_kg_m3: float = 7830.0,
        accuracy: float = 0.99,
        per_body: bool = False,
    ) -> MassPropertiesResult:
        """Compute mass properties of all solid bodies in the active part.

        Returns volume (mm^3), surface area (mm^2), centre of gravity (mm) and
        mass (kg) = volume x density. The default density 7830 kg/m^3 is steel;
        override for the actual material (e.g. 2700 aluminium, 7190 cast iron,
        8940 copper, 1150 nylon). Useful for material quoting, blank (毛坯)
        selection and fixture/hoisting planning.

        With per_body=True the ``bodies`` list additionally reports each solid
        separately (multi-cavity dies / multi-operation parts need per-part
        values, not just the sum).
        """
        return MassPropertiesResult(
            **await call(
                "nx_mass_properties",
                {
                    "density_kg_m3": density_kg_m3,
                    "accuracy": accuracy,
                    "per_body": per_body,
                },
            )
        )

    @mcp.tool()
    async def nx_create_section_view(
        parent_view_id: str,
        cut_x1: float,
        cut_y1: float,
        cut_x2: float,
        cut_y2: float,
        pos_x: float,
        pos_y: float,
        scale: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.0,
        arrow_dx: float | None = None,
        arrow_dy: float | None = None,
        step_dx: float | None = None,
        step_dy: float | None = None,
        view_name: str = "",
    ) -> OperationResult:
        """Create a simple (straight) SECTION view of a parent view (剖视图).

        The cut segment runs from (cut_x1, cut_y1) to (cut_x2, cut_y2) in SHEET
        coordinates (mm, origin bottom-left) across the parent view - it is
        drawn as a member-view line, then UF_DRAW_create_simple_sxview builds
        the section line + view. The section view is placed at (pos_x, pos_y)
        on the sheet. Defaults (verified live): step direction = the cut-line
        direction, arrow direction = the perpendicular viewing direction
        (pointing left of the x1,y1 -> x2,y2 run). Override arrow_dx/dy (or
        step_dx/dy) to flip the viewing side; NX rejects parallel step/arrow
        vectors, so never set both to the cut-line direction. scale is
        relative to the parent view (1.0 = same).
        """
        params: dict[str, Any] = {
            "parent_view_id": parent_view_id,
            "cut_x1": cut_x1,
            "cut_y1": cut_y1,
            "cut_x2": cut_x2,
            "cut_y2": cut_y2,
            "pos_x": pos_x,
            "pos_y": pos_y,
        }
        if scale != 1.0:
            params["scale"] = scale
        if arrow_dx is not None and arrow_dy is not None:
            params["arrow_dx"], params["arrow_dy"] = arrow_dx, arrow_dy
        if step_dx is not None and step_dy is not None:
            params["step_dx"], params["step_dy"] = step_dx, step_dy
        if view_name:
            params["view_name"] = view_name
        return OperationResult(**await call("nx_create_section_view", params))

    @mcp.tool()
    async def nx_add_section_segment(
        view_id: str,
        cut_x1: float,
        cut_y1: float,
        cut_x2: float,
        cut_y2: float,
    ) -> OperationResult:
        """Add one more cut segment to a section view's section line (阶梯剖).

        Turns an existing simple section view (from nx_create_section_view)
        into a stepped section: the new cut runs from (cut_x1, cut_y1) to
        (cut_x2, cut_y2) in SHEET coordinates across the parent view, and NX
        auto-inserts the bend and updates the section view. Add segments in
        the order the plate is cut through; arrows follow the original view.
        """
        return OperationResult(
            **await call(
                "nx_add_section_segment",
                {
                    "view_id": view_id,
                    "cut_x1": cut_x1,
                    "cut_y1": cut_y1,
                    "cut_x2": cut_x2,
                    "cut_y2": cut_y2,
                },
            )
        )

    @mcp.tool()
    async def nx_create_id_symbol(
        symbol_type: Literal[
            "circle",
            "divcir",
            "tri",
            "triup",
            "square",
            "divsqr",
            "hexagon",
            "divhex",
            "quadrant",
            "roundbox",
            "underline",
        ] = "circle",
        pos_x: float = 20.0,
        pos_y: float = 20.0,
        size: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 10.0,
        upper_text: str = "",
        lower_text: str = "",
    ) -> OperationResult:
        """Create an ID symbol (球标/零件序号气泡) on the current drawing sheet.

        ``circle`` (divided=divcir), triangles (tri/triup), squares, hexagons,
        rounded box... - the part-callout balloon of assembly process drawings;
        pair it with nx_list_assembly / nx_export_bom_csv so the balloon number
        and the BOM row agree. Position is sheet coordinates (mm), size is the
        symbol diameter/width (mm, default 10). upper_text goes into the upper
        slot (spaces allowed), lower_text into the lower slot (single token).
        """
        params: dict[str, Any] = {
            "symbol_type": symbol_type,
            "pos_x": pos_x,
            "pos_y": pos_y,
        }
        if size != 10.0:
            params["size"] = size
        if upper_text:
            params["upper_text"] = upper_text
        if lower_text:
            params["lower_text"] = lower_text
        return OperationResult(**await call("nx_create_id_symbol", params))

    @mcp.tool()
    async def nx_create_datum_symbol(
        letter: Annotated[str, Field(min_length=1, max_length=2)],
        pos_x: float,
        pos_y: float,
        leader_view_id: str = "",
        leader_body_id: str = "",
        leader_edge_index: int = -1,
        leader_side: Literal["", "left", "right"] = "",
    ) -> OperationResult:
        """Create a datum feature symbol (基准符号) on the current drawing sheet.

        The GD&T datum-letter frame (GB/T 17851 / ISO 5459): a square box with
        the datum letter, e.g. [A]. Pair it with form/position tolerance
        annotations - a datum is meaningless without a tolerance frame that
        references it. letter is uppercased (1-2 characters), pos_x/pos_y are
        sheet coordinates (mm).

        Optional leader line: pass leader_view_id + leader_body_id +
        leader_edge_index together (edge from nx_list_edges) and the frame
        attaches with a Gb-style leader to that edge inside the view; without
        them the symbol is standalone - put it on the extension line of a
        dimension or next to the surface edge.
        """
        params: dict[str, Any] = {"letter": letter, "pos_x": pos_x, "pos_y": pos_y}
        if leader_view_id:
            params["leader_view_id"] = leader_view_id
        if leader_body_id:
            params["leader_body_id"] = leader_body_id
        if leader_edge_index >= 0:
            params["leader_edge_index"] = leader_edge_index
        if leader_side:
            params["leader_side"] = leader_side
        return OperationResult(**await call("nx_create_datum_symbol", params))

    @mcp.tool()
    async def nx_create_gdt_frame(
        characteristic: str,
        tolerance: str,
        pos_x: float,
        pos_y: float,
        datum_primary: str = "",
        datum_secondary: str = "",
        datum_tertiary: str = "",
        zone_shape: Literal["none", "diameter", "spherical", "square"] = "none",
        material_modifier: Literal["none", "mmc", "lmc", "rfs"] = "none",
        leader_view_id: str = "",
        leader_body_id: str = "",
        leader_edge_index: int = -1,
        leader_side: Literal["", "left", "right"] = "",
    ) -> OperationResult:
        """Create a feature control frame (形位公差框, GD&T) on the drawing sheet.

        The geometric-tolerance frame of process drawings (GB/T 1182 /
        ISO 1101): characteristic symbol + tolerance value + up to three datum
        letters, e.g. |⏀|Ø0.05|A|B(C)|. characteristic accepts the 14 GB/T
        names in Chinese (直线度/平面度/圆度/圆柱度/线轮廓度/面轮廓度/倾斜度/
        垂直度/平行度/位置度/同轴度/对称度/圆跳动/全跳动) or English enum
        names (case-insensitive, e.g. flatness, position, totalrunout).
        tolerance is the value text, e.g. "0.02". zone_shape=diameter puts the
        Ø symbol in front of the value (axial holes/axis zones); material
        modifier applies to the tolerance ( mmc/lmc ). Datum letters are
        uppercased single characters, e.g. datum_primary="A". pos_x/pos_y are
        sheet coordinates (mm).

        Optional leader line pointing at the toleranced feature: pass
        leader_view_id + leader_body_id + leader_edge_index together (edge
        from nx_list_edges) - the frame connects to that edge with a filled
        arrow; without them placement is standalone - put it under the
        dimension line it controls, then pair it with
        nx_create_datum_symbol for referenced datums.
        """
        key = _FCF_CHARACTERISTIC_ALIASES.get(characteristic.strip())
        if key is None:
            key = characteristic.strip().lower().replace(" ", "")
        params: dict[str, Any] = {
            "characteristic": key,
            "tolerance": tolerance.strip(),
            "pos_x": pos_x,
            "pos_y": pos_y,
            "datum_primary": datum_primary.strip().upper(),
            "datum_secondary": datum_secondary.strip().upper(),
            "datum_tertiary": datum_tertiary.strip().upper(),
            "zone_shape": zone_shape,
            "material_modifier": material_modifier,
        }
        if leader_view_id:
            params["leader_view_id"] = leader_view_id
        if leader_body_id:
            params["leader_body_id"] = leader_body_id
        if leader_edge_index >= 0:
            params["leader_edge_index"] = leader_edge_index
        if leader_side:
            params["leader_side"] = leader_side
        return OperationResult(**await call("nx_create_gdt_frame", params))

    @mcp.tool()
    async def nx_create_weld_symbol(
        weld_type: str,
        pos_x: float,
        pos_y: float,
        weld_size: str = "",
        side: Literal["arrow", "other", "both"] = "arrow",
        length_pitch: str = "",
        tail: Literal["none", "tail", "box"] = "none",
        contour: str = "",
        finish: str = "",
        standard: Literal["gb", "iso", "ansi", "din", "jis", "eskd"] = "gb",
    ) -> OperationResult:
        """Create a welding symbol (焊接符号, GB/T 324 / ISO 2553) on the sheet.

        weld_type accepts Chinese GB names (角焊缝 / I形 / V形 / 单边V形 /
        K形 / U形 / 单边U形 / 塞焊 / 槽焊 / 点焊 / 缝焊 / 卷边 / 封底焊) or
        English enum names (fillet, squarebutt, vbutt, bevelbutt, kgroove,
        ubutt, jbutt, plugslot, spot, seam, flarev, flarebevel, backing, ...).
        weld_size is the fillet/groove size text, e.g. "5" (⌒5 in the symbol).
        side selects which leg of the reference line carries the symbol:
        arrow (箭头侧) / other (非箭头侧) / both - an X-groove (X形坡口) is
        side="both" with weld_type="vbutt". length_pitch adds the intermittent
        weld data, e.g. "3x50" (3 段 x 每段 50). tail=tail adds the reference
        tail for notes; contour 凸面/凹面/平面 and finish 打磨/机加工/锤击
        accept Chinese or English names. standard defaults to gb (GB/T 324);
        pos_x/pos_y are sheet coordinates (mm), standalone placement like the
        GD&T frame (no leader). weld_size / length_pitch must not contain
        spaces (tokenised pipe protocol).
        """
        wtype = _WELD_TYPE_ALIASES.get(weld_type.strip())
        if wtype is None:
            wtype = weld_type.strip().lower().replace(" ", "")
        contour_key = _WELD_CONTOUR_ALIASES.get(contour.strip()) or contour.strip().lower()
        finish_key = _WELD_FINISH_ALIASES.get(finish.strip()) or finish.strip().lower()
        return OperationResult(
            **await call(
                "nx_create_weld_symbol",
                {
                    "weld_type": wtype,
                    "weld_size": weld_size.strip(),
                    "pos_x": pos_x,
                    "pos_y": pos_y,
                    "side": side,
                    "length_pitch": length_pitch.strip(),
                    "tail": tail,
                    "contour": contour_key,
                    "finish": finish_key,
                    "standard": standard,
                },
            )
        )



    @mcp.tool()
    async def nx_delete_view(view_id: str) -> OperationResult:
        """Delete a drafting view from its sheet (removes the view itself).

        view_id follows nx_list_drafting_views conventions (index, name or
        journal id). Section views also lose their section line. Use before
        re-placing or re-annotating a drawing - NX keeps no recycle bin, so
        verify the view_id first.
        """
        return OperationResult(**await call("nx_delete_view", {"view_id": view_id}))

    @mcp.tool()
    async def nx_list_assembly() -> AssemblyListResult:
        """List the active part's component assembly tree, flattened.

        One row per component INSTANCE: level (1 = top), instance name,
        display name, child part file path (when loaded), number of own
        children, qty (total instances of the same prototype part in the
        whole assembly) and attributes (the prototype part's string user
        attributes - set 材料/MATERIAL or 图号/DB_PART_NO via nx_set_attribute
        on each child part first). This is the raw BOM / 明细表 data a
        process routing table (工艺路线表) is built from; use
        nx_export_bom_csv to write it out.
        """
        return AssemblyListResult(**await call("nx_list_assembly", {}))

    @mcp.tool()
    async def nx_add_component(
        child_part: str,
        instance_name: str = "",
        refset: str = "MODEL",
        position: Point3D | None = None,
        layer: int = 0,
    ) -> ComponentAddResult:
        """Add an existing part file as a component of the active assembly.

        The active part (nx_open_part on the assembly root first) gains one
        component instance of ``child_part``. position is an optional
        {"x","y","z"} placement origin in mm (default 0,0,0); layer follows NX
        convention (0 = keep the child's original layers, -1 = work layer, >0
        a fixed layer); refset defaults to "MODEL". instance_name defaults to
        the child file name without extension. Verification: the result reports
        the assembly's direct child count after the add, so a silent no-op is
        detectable by comparing calls.

        After adding components run nx_save_part on the assembly root, then
        nx_list_assembly / nx_export_bom_csv to verify the tree and BOM.
        Paths must not contain spaces (tokenised pipe protocol, same as
        nx_open_part). NOTE: this does NOT add mating constraints - the
        component is placed at the given origin; constraint support is tracked
        separately in 交接清单档案.
        """
        params: dict[str, Any] = {
            "child_part": child_part,
            "refset": refset,
            "layer": layer,
        }
        if instance_name:
            params["instance_name"] = instance_name
        if position is not None:
            params["position"] = {"x": position.x, "y": position.y, "z": position.z}
        return ComponentAddResult(**await call("nx_add_component", params))

    @mcp.tool()
    async def nx_export_bom_csv(path: str) -> ExportResult:
        """Export the active part's assembly as a CSV bill of materials.

        Writes one row per component instance (level, name, display_name,
        part_path, children, qty, material, drawing_no) into the workspace
        file ``path`` (UTF-8 BOM, so Excel opens Chinese names correctly).
        material/drawing_no come from the prototype part's user attributes
        (MATERIAL/材料, DB_PART_NO/图号) when those parts are loaded; empty
        otherwise. The part must be an assembly; plain parts export an empty
        table and error out - check with nx_list_assembly first.
        """
        resolved = resolve_path(path)
        resp = await call("nx_list_assembly", {})
        rows = resp.get("rows") or []
        if not rows:
            raise MCPToolError(
                json.dumps(
                    NXToolError(
                        "NX_EMPTY_ASSEMBLY",
                        "the active part has no components; BOM export needs an assembly",
                    ).as_dict(),
                    ensure_ascii=False,
                )
            )
        message = await asyncio.to_thread(_write_bom_csv, resolved, rows)
        return ExportResult(status="success", path=resolved, message=message)

    @mcp.tool()
    async def nx_export_process_card(
        path: str,
        format: Literal["text", "csv"] = "text",
    ) -> ExportResult:
        """Export a structured process card (工序卡/工艺卡片) from part attributes.

        The template lives in the part's attributes - set them with
        nx_set_attribute, then export. Schema: ``OP<序号>_<字段>``, e.g.
        OP10_NAME / OP10_工序名, OP10_CONTENT / OP10_内容, OP10_MACHINE / 设备,
        OP10_FIXTURE / 装夹, OP10_TOOL / 刀具, OP10_CUTTING / 切削参数,
        OP10_TIME / 工时; bare OP10 holds the op title. Part-level header
        comes from 名称 / 图号 / 材料 / 比例 (same pool as the title block).
        Ops sort numerically (OP5 < OP10 < OP100). format=text writes a
        readable card, csv writes a UTF-8-BOM table for Excel. The message
        lists typos (unknown fields) and ops missing name/content/machine.
        """
        resolved = resolve_path(path)
        resp = await call("nx_list_attributes", {})
        attributes: dict[str, str] = resp.get("attributes") or {}

        def _write() -> str:
            message, _ = _write_process_card(resolved, attributes, format)
            return message

        message = await asyncio.to_thread(_write)
        return ExportResult(status="success", path=resolved, message=message)

    @mcp.tool()
    async def nx_create_centerline(
        view_id: str = "",
        body_id: str = "",
        face_index: int = 0,
    ) -> OperationResult:
        """Create a symmetrical centreline (轴线/中心线) for a shaft in a view.

        Select the cylindrical FACE of the shaft (face_index from nx_list_faces);
        the axis line is drawn across the view - the classic turned-part centre
        line. view_id follows nx_create_projected_view conventions ("0" = first
        view of the sheet). This replaced the dead-end AutomaticCenterlineBuilder
        route; see 受阻项调研档案.
        """
        return OperationResult(
            **await call(
                "nx_create_centerline",
                {"view_id": view_id, "body_id": body_id, "face_index": face_index},
            )
        )

    # nx_create_detail_view: 暂不暴露（局部放大内容不渲染，修复未实测，
    # 见 受阻项调研档案 第 4 节）。恢复暴露时把下面的函数原样
    # 挂回 @mcp.tool() 并把 "nx_create_detail_view" 加回 CERTIFIED_TOOL_NAMES。
    _DETAIL_VIEW_PARAMS = {
        "parent_view_id": "",
        "center_x": 0.0,
        "center_y": 0.0,
        "radius": 10.0,
        "pos_x": 0.0,
        "pos_y": 0.0,
        "scale": 0.0,
        "view_name": "",
    }

    @mcp.tool()
    async def nx_create_note(
        text: str,
        pos_x: float = 20.0,
        pos_y: float = 20.0,
        leader_view_id: str = "",
        leader_body_id: str = "",
        leader_edge_index: int = -1,
        leader_side: Literal["", "left", "right"] = "",
        note_font: str = "",
    ) -> OperationResult:
        """Add technical-requirement / process text to the current drawing sheet.

        Use "|" to start a new line, e.g. "技术要求|1. 未注倒角 C0.5|2. 调质 28-32HRC".
        Position is in sheet coordinates (mm).

        IMPORTANT for Chinese text: the default lettering font has NO Chinese
        glyphs (renders as hollow boxes). Pass note_font="chinesef" (NX built-in
        FangSong-style CJK font, auto-added to the part font table on demand)
        whenever text contains CJK characters.

        Optional leader line (工序图指向加工面的说明文字): pass leader_view_id
        (from nx_list_drafting_views), leader_body_id and leader_edge_index
        (from nx_list_edges) together - the leader runs from the text to a
        filled arrow on that edge's midpoint inside the view (the annotation
        becomes a NX "label"). leader_side selects which side of the text the
        stub leaves from (default: inferred).
        """
        params: dict[str, Any] = {"text": text, "pos_x": pos_x, "pos_y": pos_y}
        if leader_view_id:
            params["leader_view_id"] = leader_view_id
        if leader_body_id:
            params["leader_body_id"] = leader_body_id
        if leader_edge_index >= 0:
            params["leader_edge_index"] = leader_edge_index
        if leader_side:
            params["leader_side"] = leader_side
        if note_font:
            params["note_font"] = note_font
        return OperationResult(**await call("nx_create_note", params))

    @mcp.tool()
    async def nx_create_angle_dimension(
        view_id: str = "",
        body_id: str = "",
        first_edge_index: int = 0,
        second_edge_index: int = 0,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        arc: Literal["minor", "major"] = "minor",
        dim_name: str = "MCP_ANGLE",
    ) -> OperationResult:
        """Create an associative ANGULAR dimension between two straight edges.

        The angle annotation for inclined faces / chamfer walls / taper flanks
        on process drawings. Edges are 0-based indices from nx_list_edges on
        the given body; both must be straight lines. arc=minor measures the
        acute-ish arc between the lines (default, what NX picks interactively),
        arc=major measures the reflex side. The value reads back in the
        result message in degrees; no linear tolerance is applied (angular
        tolerances use d/m/s and are not supported yet). pos_x/pos_y are
        sheet coordinates (mm) for the dimension text.
        """
        return OperationResult(
            **await call(
                "nx_create_angle_dimension",
                {
                    "view_id": view_id,
                    "body_id": body_id,
                    "first_edge_index": first_edge_index,
                    "second_edge_index": second_edge_index,
                    "pos_x": pos_x,
                    "pos_y": pos_y,
                    "arc": arc,
                    "dim_name": dim_name,
                },
            )
        )

    @mcp.tool()
    async def nx_create_diameter_dimension(
        view_id: str = "",
        body_id: str = "",
        edge_index: int = 0,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        upper_tolerance: float = 0.1,
        lower_tolerance: float = -0.1,
        dim_name: str = "MCP_DIAMETER",
    ) -> OperationResult:
        """Create a DIAMETER dimension on one edge (essential for shafts).

        A linear dimension between two edges cannot express a diameter; use this
        for cylindrical/circular features. edge_index is the 0-based index from
        nx_list_edges on the given body.
        """
        return OperationResult(
            **await call(
                "nx_create_diameter_dimension",
                {
                    "view_id": view_id,
                    "body_id": body_id,
                    "edge_index": edge_index,
                    "pos_x": pos_x,
                    "pos_y": pos_y,
                    "upper_tolerance": upper_tolerance,
                    "lower_tolerance": lower_tolerance,
                    "dim_name": dim_name,
                },
            )
        )

    @mcp.tool()
    async def nx_update_views() -> OperationResult:
        """Force every drafting view on the current sheet to a fresh update.

        Clears the out-of-date ("clock") flag shown in the Part Navigator after a
        drawing or projected view is created or modified.
        """
        return OperationResult(**await call("nx_update_views", {}))

    @mcp.tool()
    async def nx_create_dimension(
        view_id: str = "",
        body_id: str = "",
        first_edge_index: int = 0,
        second_edge_index: int = 0,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        measurement: Literal["horizontal", "vertical", "parallel"] = "horizontal",
        upper_tolerance: float = 0.1,
        lower_tolerance: float = -0.1,
        dim_name: str = "MCP_DIMENSION",
    ) -> OperationResult:
        """Create an associative linear dimension with a bilateral tolerance.

        Measures the distance between two model edges (selected by their 0-based
        index from nx_list_edges on the given body) inside the drafting view.
        Default tolerance is ±0.1 mm using the BilateralTwoLines style.
        """
        return OperationResult(
            **await call(
                "nx_create_dimension",
                {
                    "view_id": view_id,
                    "body_id": body_id,
                    "first_edge_index": first_edge_index,
                    "second_edge_index": second_edge_index,
                    "pos_x": pos_x,
                    "pos_y": pos_y,
                    "measurement": measurement,
                    "upper_tolerance": upper_tolerance,
                    "lower_tolerance": lower_tolerance,
                    "dim_name": dim_name,
                },
            )
        )

    @mcp.tool()
    async def nx_create_wave_link(
        source_part: str,
        target_part: str,
        body_index: int = 1,
        associative: bool = False,
        position: Point3D | None = None,
    ) -> OperationResult:
        """Create a WAVE geometry link from a master part into a process part.

        The source part becomes the assembly root and the target part is added
        as its downstream component (NX WAVE requires this downstream
        structure), then the solid body is linked into the target part.

        Default link type is a fixed timestamp / extracted geometry snapshot:
        the process part does NOT update when the master changes - exactly
        what "freeze the master into each operation part" needs. Pass
        associative=true to keep the live link instead.

        body_index is 1-based (default 1 = first solid body). position is an
        optional {"x","y","z"} mm offset - RESERVED for now: the loader
        currently rejects it with a clear error while the transform path is
        being validated (see NX_MCP_Loader.cs CreateWaveLink).

        WARNING: the target part file is deleted and re-created if it exists.
        Paths must not contain spaces (tokenised pipe protocol, same as
        nx_open_part).
        """
        params: dict[str, Any] = {
            "source_part": source_part,
            "target_part": target_part,
            "body_index": body_index,
            "associative": associative,
        }
        if position is not None:
            params["position"] = {"x": position.x, "y": position.y, "z": position.z}
        return OperationResult(**await call("nx_create_wave_link", params))

    @mcp.tool()
    async def nx_read_sketch(sketch_name: str) -> OperationResult:
        """Dump a sketch's 2D geometry in sketch-local coordinates (u, w).

        Works on pre-existing / externally-authored sketches: returns the
        sketch plane (XY/XZ/YZ), origin and axes in part coordinates, plus
        every curve as line {a,b}, arc {c,r,a1,a2} or circle {c,d}. Use it to
        faithfully re-create a profile in another part, e.g. for WAVE-based
        process planning.
        """
        return OperationResult(
            **await call("nx_read_sketch", {"sketch_name": sketch_name})
        )

    @mcp.tool()
    async def nx_set_dimension_value(target_size: float, new_value: float) -> OperationResult:
        """Force an associative dimension's displayed nominal to a given value.

        Finds the dimension whose computed size is closest to target_size
        (e.g. a mid-tolerance nominal: computed 99.96, displayed 100) and pins
        its displayed value to new_value. NX 12 exposes no public value
        override on Dimension, so the loader probes the API surface at runtime;
        the reply text records which override path succeeded.
        """
        return OperationResult(
            **await call(
                "nx_set_dimension_value",
                {"target_size": target_size, "new_value": new_value},
            )
        )

    if enable_experimental:
        from nx_mcp.experimental import add_experimental_tools

        add_experimental_tools(
            mcp,
            call,
            CERTIFIED_TOOL_NAMES,
            workspace,
            enable_journal=enable_journal,
        )

    return mcp
