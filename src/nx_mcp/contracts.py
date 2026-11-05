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
    "HoleInfo",
    "HoleListResult",
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
    "StatusResult",
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
