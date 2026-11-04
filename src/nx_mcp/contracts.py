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
    "ExtrudeResult",
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


class ToolSuccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    message: str


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
