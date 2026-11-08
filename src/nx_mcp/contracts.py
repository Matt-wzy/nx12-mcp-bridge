"""Typed public contracts shared by MCP tools and the NX bridge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nx_mcp.runtime import NXToolError, ObjectKind, ObjectRef

__all__ = [
    "AssemblyListResult",
    "BodyMassProperties",
    "BomRow",
    "ExportResult",
    "ExplosionInfo",
    "ExplosionListResult",
    "ExplosionResult",
    "ExtrudeResult",
    "FaceProvenanceResult",
    "FeatureOwner",
    "FeatureSuppressResult",
    "HoleInfo",
    "HoleListResult",
    "InterferenceResult",
    "MassPropertiesResult",
    "MeasureAngleResult",
    "NXToolError",
    "ObjectKind",
    "ObjectListResult",
    "ObjectRef",
    "ObjectResult",
    "OperationResult",
    "PartResult",
    "Point2D",
    "SaveAsResult",
    "SelectionResult",
    "SelectedEntity",
    "StockResult",
    "StatusResult",
    "TableResult",
    "ToolSuccess",
    "Vector3D",
]


class Point2D(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class Vector3D(BaseModel):
    """A 3D vector/point in model space (mm)."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    z: float = Field(allow_inf_nan=False)


class BodyMassProperties(BaseModel):
    """Per-body mass properties (nx_mass_properties with per_body=True)."""

    model_config = ConfigDict(extra="forbid")

    body_id: str = ""
    mass_kg: float = 0.0
    volume_mm3: float = 0.0
    surface_area_mm2: float = 0.0
    center_of_gravity: Vector3D = Field(default_factory=lambda: Vector3D(x=0.0, y=0.0, z=0.0))


class MassPropertiesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    body_count: int
    mass_kg: float
    volume_mm3: float
    surface_area_mm2: float
    density_kg_m3: float
    center_of_gravity: Vector3D
    bodies: list[BodyMassProperties] = Field(default_factory=list)
    message: str = ""


class BomRow(BaseModel):
    """One flattened component instance of the assembly tree (BOM data)."""

    model_config = ConfigDict(extra="forbid")

    level: int = 1
    name: str = ""
    display_name: str = ""
    part_path: str = ""
    children: int = 0
    # total instances of the same prototype part across the whole assembly
    qty: int = 1
    # prototype part's string user attributes (MATERIAL/材料, DB_PART_NO/图号...)
    attributes: dict[str, str] = Field(default_factory=dict)


class AttributeListResult(BaseModel):
    """All string user attributes of the active part (structured)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int = 0
    attributes: dict[str, str] = Field(default_factory=dict)
    message: str = ""


class AssemblyListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int = 0
    rows: list[BomRow] = Field(default_factory=list)
    message: str = ""


class MeasureAngleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    angle_deg: float
    acute_deg: float
    message: str = ""


class HoleInfo(BaseModel):
    """One cylindrical wall found by nx_list_holes.

    kind=hole means the material is outside the cylinder (void on the axis);
    kind=boss means the material is inside (shaft/boss wall). x/y/z is the
    axis point at the bbox mid-height, so a blind hole reads z = start +
    depth/2. The row comes from a sampled cylinder fit, so both analytic
    walls (UF type 16) and parametric swept walls (type 20, e.g. holes made
    by nx_hole) are covered; uf_type records which one it was. full_wall
   =False marks partial walls (cross-drilled / milled): their geometry is
    still exact, but the hole position should be verified manually.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    face_index: int
    kind: Literal["hole", "boss", "unknown"]
    diameter: float
    radius: float = 0.0
    depth: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    area: float = 0.0
    sweep_rad: float = 0.0
    full_wall: bool = False
    containment: int = 0
    uf_type: int = 0
    tag: int = 0
    axis_uf: list[float] = Field(default_factory=list)
    point_uf: list[float] = Field(default_factory=list)
    err: str = ""


class HoleListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int = 0
    cylinders: int = 0
    face_count: int = 0
    holes: list[HoleInfo] = Field(default_factory=list)
    message: str = ""


class FeatureOwner(BaseModel):
    """The feature that produced a face (made_by provenance).

    feature is the (possibly localized) NX feature name, e.g. HOLE_1(7);
    type is the NXOpen FeatureType string, e.g. HOLE / EXTRUDE. Post-unite
    bodies may legitimately report zero owners (known NX limitation, see
    受阻项调研档案) - owner_count=0 is a fact, not an error.
    """

    model_config = ConfigDict(extra="forbid")

    feature: str = ""
    type: str = ""


class SelectedEntity(BaseModel):
    """One object currently selected in the NX graphics area.

    kind is body/face/edge or the raw NX type name for anything else. Faces
    carry body_id + index (same indexing as nx_list_faces) and made_by
    ownership facts. err keeps per-entity read failures visible instead of
    dropping the row silently.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = ""
    tag: int = 0
    body_id: str = ""
    index: int = -1
    face_type: str = ""
    name: str = ""
    owner_count: int = 0
    made_by: list[FeatureOwner] = Field(default_factory=list)
    err: str = ""


class SelectionResult(BaseModel):
    """Current graphics-area selection (selection reflux for the agent).

    route names the NX API member the resident Loader used; note carries a
    named reason when nothing could be read (empty selection reads as
    count=0 with an empty note, not an error).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int = 0
    route: str = ""
    note: str = ""
    selection: list[SelectedEntity] = Field(default_factory=list)
    message: str = ""


class FaceProvenanceResult(BaseModel):
    """made_by facts for one face (pairs with nx_list_faces indexing).

    face_index uses the body.GetFaces() order, identical to nx_list_faces /
    nx_get_selection, so geometry facts and ownership facts share one index
    space.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    body_id: str = ""
    face_index: int = -1
    tag: int = 0
    face_type: str = ""
    owner_count: int = 0
    made_by: list[FeatureOwner] = Field(default_factory=list)
    owner_err: str = ""
    message: str = ""


class ToolSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    message: str


class ComponentAddResult(BaseModel):
    """Result of nx_add_component: the new component instance plus the
    assembly child count after the add (verification-by-counting)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    component_name: str = ""
    journal_id: str = ""
    part_path: str = ""
    children: int = 0
    message: str = ""


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    connected: bool
    nx_version: str
    bridge_protocol: int
    active_part: ObjectRef | None = None


class PartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    part: ObjectRef
    message: str = ""


class ObjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    object: ObjectRef
    message: str = ""


class ObjectListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    objects: list[ObjectRef]
    message: str = ""


class ExtrudeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    feature: ObjectRef
    body: ObjectRef
    message: str = ""


class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    path: str
    message: str = ""


class ExplosionResult(BaseModel):
    """Result of nx_create_explosion / explode / unexplode operations.

    name is the explosion name (create) and component the instance name
    (explode/unexplode); both messages echo what NX stored so a transform
    convention mismatch surfaces immediately, not at PDF time."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    name: str = ""
    component: str = ""
    message: str = ""


class ExplosionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tag: int = 0


class ExplosionListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    count: int = 0
    explosions: list[ExplosionInfo] = Field(default_factory=list)
    message: str = ""


class StockResult(BaseModel):
    """Result of nx_create_stock: the machining blank around the resolved body.

    utilization_pct = part_volume / stock_volume * 100 - the everyday stock
    allowance check (NaN becomes null). The blank is a NEW solid registered as
    BODY_<n> (body_id), so it can be referenced by later tools; bbox echoes the
    source body's bounding box the blank was derived from."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    mode: str = "box"
    body_id: str = ""
    feature_tag: int = 0
    stock_volume_mm3: float = 0.0
    part_volume_mm3: float = 0.0
    utilization_pct: float | None = None
    bbox_min: Vector3D = Field(default_factory=lambda: Vector3D(x=0.0, y=0.0, z=0.0))
    bbox_max: Vector3D = Field(default_factory=lambda: Vector3D(x=0.0, y=0.0, z=0.0))
    message: str = ""


class TableResult(BaseModel):
    """Result of nx_create_table: a tabular note (表格注解) on the drawing sheet.

    cell_echo is the round-trip read-back of the first cell (AskCellText), so a
    text/encoding mismatch surfaces in the response instead of at PDF time.
    font reports what happened to the optional font= request (applied id,
    skipped reason, or none)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    rows: int = 0
    cols: int = 0
    note_tag: int = 0
    tables_after: int = 0
    cell_echo: str = ""
    font: str = ""
    message: str = ""


class FeatureSuppressResult(BaseModel):
    """Result of nx_set_feature_suppressed: read-back of one feature's state.

    suppressed_before/after are the Feature.Suppressed read-backs around the
    call and suppressed_count is the part-wide suppressed total (verification
    by counting). A state that did not change as requested never reaches this
    model - the loader turns it into an explicit error."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    feature_index: int = 0
    feature_name: str = ""
    suppressed_before: bool = False
    suppressed_after: bool = False
    suppressed_count: int = 0
    message: str = ""


class InterferenceResult(BaseModel):
    """Result of nx_check_interference: per-pair interference report.

    items lines look like "HARD BODY_1 BODY_2 depth=12.5" or
    "SOFT A B clearance<2" (zone check). nx_count echoes the loader's
    GetNumberOfInterferences so a pair read-back mismatch is visible; the
    analysis set is deleted after read-back unless keep_set was requested."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    mode: str = "bodies"
    objects_checked: int = 0
    pairs_checked: int = 0
    loaded_children: int = 0
    interference_count: int = 0
    nx_count: int | None = None
    items: list[str] = Field(default_factory=list)
    set_deleted: bool = False
    message: str = ""


class SaveAsResult(BaseModel):
    """Result of nx_save_part_as: the work part switched to the new file.

    saved_path is the loader's FullPath read-back (may differ in separators
    from the request); the loader's active-part tracking follows the new
    file, so subsequent tools operate on the copy."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    saved_path: str = ""
    message: str = ""
