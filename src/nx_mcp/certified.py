"""The explicitly certified v0.2 MCP tool surface."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError
from pydantic import Field

from nx_mcp import __version__
from nx_mcp.contracts import (
    ExportResult,
    ExtrudeResult,
    NXToolError,
    ObjectListResult,
    ObjectResult,
    OperationResult,
    PartResult,
    Point2D,
    StatusResult,
)
from nx_mcp.workspace import Workspace, WorkspaceViolation


class BridgeCaller(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


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
    "nx_create_sketch",
    "nx_sketch_line",
    "nx_sketch_rectangle",
    "nx_sketch_circle",
    "nx_sketch_arc",
    "nx_finish_sketch",
    "nx_extrude",
    "nx_hole",
    "nx_edge_blend",
    "nx_chamfer",
    "nx_unite",
    "nx_revolve",
    "nx_mirror",
    "nx_undo",
    "nx_fit_view",
    "nx_release",
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
    async def nx_undo() -> OperationResult:
        """Undo the last visible NX MCP operation."""
        return OperationResult(**await call("nx_undo", {}))

    @mcp.tool()
    async def nx_fit_view() -> OperationResult:
        """Fit the active modeling view."""
        return OperationResult(**await call("nx_fit_view", {}))

    @mcp.tool()
    async def nx_release() -> OperationResult:
        """Gracefully stop the bridge and release the NX GUI for manual editing.

        The current command completes, then the journal ends and NX becomes
        fully interactive (no restart of NX required). Start the bridge again
        later by running the launcher journal (Alt+F8) again.
        """
        return OperationResult(**await call("nx_release", {}))

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
