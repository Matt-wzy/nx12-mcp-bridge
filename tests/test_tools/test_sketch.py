"""Tests for sketch tools (6 tools)."""

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from nx_mcp.nx_session import NXSession
from nx_mcp.tools.registry import ToolRegistry

pytestmark = [pytest.mark.legacy, pytest.mark.fake_nx]

# ---------------------------------------------------------------------------
# Reusable helpers
# ---------------------------------------------------------------------------


def _make_mock_nxopen():
    """Build a self-contained mock NXOpen module tree for sketch tests."""
    nxopen = types.ModuleType("NXOpen")

    # --- Session ---
    mock_session = MagicMock()
    mock_session.Parts = MagicMock()
    mock_work_part = MagicMock()
    mock_work_part.Name = "test_part"
    mock_session.Parts.Work = mock_work_part
    nxopen.Session = MagicMock()
    nxopen.Session.GetSession = MagicMock(return_value=mock_session)
    nxopen._mock_session = mock_session

    # --- Active sketch on work part ---
    mock_active_sketch = MagicMock()
    mock_active_sketch.Deactivate = MagicMock()
    mock_active_sketch.CreateGeometricConstraint = MagicMock()
    mock_active_sketch.CreateDimension = MagicMock()
    mock_work_part.Sketches.ActiveSketch = mock_active_sketch

    # --- Point3d / Vector3d ---
    nxopen.Point3d = MagicMock(return_value=MagicMock())
    nxopen.Vector3d = MagicMock(return_value=MagicMock())

    # --- Sketches ---
    mock_work_part.Sketches = MagicMock()

    _created_sketch_builders: list[MagicMock] = []

    def _create_sketch_builder(*args, **kwargs):
        builder = MagicMock()
        mock_sketch = MagicMock()
        mock_sketch.Name = f"Sketch_{len(_created_sketch_builders)}"
        builder.Commit = MagicMock(return_value=mock_sketch)
        builder.Destroy = MagicMock()
        builder.SetPlaneNormal = MagicMock()
        builder.SetName = MagicMock()
        _created_sketch_builders.append(builder)
        return builder

    mock_work_part.Sketches.CreateSketchBuilder = MagicMock(
        side_effect=_create_sketch_builder,
    )

    _created_constraint_builders: list[MagicMock] = []

    def _create_constraint_builder(*args, **kwargs):
        builder = MagicMock()
        builder.Commit = MagicMock()
        builder.Destroy = MagicMock()
        builder.SetConstraintType = MagicMock()
        builder.AddTarget = MagicMock()
        builder.SetValue = MagicMock()
        _created_constraint_builders.append(builder)
        return builder

    mock_work_part.Sketches.CreateConstraintBuilder = MagicMock(
        side_effect=_create_constraint_builder,
    )

    # --- Curves ---
    mock_work_part.Curves = MagicMock()

    _created_curve_builders: list[MagicMock] = []
    _curve_counter = [0]

    def _create_curve_builder(*args, **kwargs):
        builder = MagicMock()
        mock_curve = MagicMock()
        _curve_counter[0] += 1
        mock_curve.Name = f"Curve_{_curve_counter[0]}"
        builder.Commit = MagicMock(return_value=mock_curve)
        builder.Destroy = MagicMock()
        builder.SetStartPoint = MagicMock()
        builder.SetEndPoint = MagicMock()
        builder.SetCenter = MagicMock()
        builder.SetRadius = MagicMock()
        builder.SetStartAngle = MagicMock()
        builder.SetEndAngle = MagicMock()
        _created_curve_builders.append(builder)
        return builder

    mock_work_part.Curves.CreateLineBuilder = MagicMock(
        side_effect=_create_curve_builder,
    )
    mock_work_part.Curves.CreateArcBuilder = MagicMock(
        side_effect=_create_curve_builder,
    )

    nxopen._created_sketch_builders = _created_sketch_builders
    nxopen._created_constraint_builders = _created_constraint_builders
    nxopen._created_curve_builders = _created_curve_builders

    # --- UF ---
    uf = types.ModuleType("NXOpen.UF")

    # --- Sketch enums ---
    sketch_mod = types.ModuleType("NXOpen.Sketch")
    sketch_mod.ViewOrientation = MagicMock()
    sketch_mod.ViewOrientation.TrueValue = "TrueValue"
    sketch_mod.CloseLevel = MagicMock()
    sketch_mod.CloseLevel.FalseValue = "FalseValue"
    sketch_mod.GeometricConstraintType = MagicMock()
    sketch_mod.GeometricConstraintType.Horizontal = "Horizontal"
    sketch_mod.GeometricConstraintType.Vertical = "Vertical"
    sketch_mod.GeometricConstraintType.Parallel = "Parallel"
    sketch_mod.GeometricConstraintType.Perpendicular = "Perpendicular"
    sketch_mod.GeometricConstraintType.Tangent = "Tangent"
    sketch_mod.GeometricConstraintType.EqualLength = "EqualLength"
    sketch_mod.GeometricConstraintType.Fixed = "Fixed"
    sketch_mod.GeometricConstraintType.Coincident = "Coincident"
    sketch_mod.GeometricConstraintType.Midpoint = "Midpoint"
    sketch_mod.GeometricConstraintType.Concentric = "Concentric"
    sketch_mod.ConstraintType = MagicMock()
    sketch_mod.ConstraintType.HorizontalDimension = "HorizontalDimension"
    nxopen.Sketch = sketch_mod
    uf.UFSession = MagicMock()
    uf.UFSession.GetUFSession = MagicMock(return_value=MagicMock())
    nxopen.UF = uf

    modules = {"NXOpen": nxopen, "NXOpen.UF": uf}
    return modules, nxopen, mock_work_part


@pytest.fixture(autouse=True)
def _setup_nx():
    """Patch NXOpen and reset session/registry for each test."""
    modules, nxopen, work_part = _make_mock_nxopen()
    with patch.dict(sys.modules, modules):
        NXSession._instance = None
        ToolRegistry.clear()

        # Reset builder tracking lists
        nxopen._created_sketch_builders.clear()
        nxopen._created_constraint_builders.clear()
        nxopen._created_curve_builders.clear()

        # Import the module so decorators register
        import nx_mcp.tools.sketch as mod  # noqa: F401

        yield nxopen, work_part

        ToolRegistry.clear()
        NXSession._instance = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateSketch:
    """Test nx_create_sketch tool."""

    @pytest.mark.asyncio
    async def test_create_sketch_default_plane(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_sketch")
        assert handler is not None

        result = await handler(plane="XY")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["plane"] == "XY"
        work_part.Sketches.CreateSketchBuilder.assert_called_once()

        # Verify builder methods were called
        builders = nxopen._created_sketch_builders
        assert len(builders) == 1
        builders[0].SetPlaneNormal.assert_called_once()
        builders[0].Destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_sketch_with_name(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_sketch")

        result = await handler(plane="XZ", name="MySketch")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["plane"] == "XZ"
        builders = nxopen._created_sketch_builders
        assert len(builders) == 1
        builders[0].SetName.assert_called_once_with("MySketch")

    @pytest.mark.asyncio
    async def test_create_sketch_invalid_plane(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_sketch")

        result = await handler(plane="AB")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert "AB" in parsed["message"]


class TestSketchLine:
    """Test nx_sketch_line tool."""

    @pytest.mark.asyncio
    async def test_sketch_line_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_line")
        assert handler is not None

        result = await handler(x1=0.0, y1=0.0, x2=10.0, y2=5.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["start"] == [0.0, 0.0]
        assert parsed["data"]["end"] == [10.0, 5.0]
        work_part.Curves.CreateLineBuilder.assert_called_once()

        builders = nxopen._created_curve_builders
        assert len(builders) >= 1
        builders[-1].SetStartPoint.assert_called_once()
        builders[-1].SetEndPoint.assert_called_once()
        builders[-1].Commit.assert_called_once()
        builders[-1].Destroy.assert_called_once()


class TestSketchArc:
    """Test nx_sketch_arc tool."""

    @pytest.mark.asyncio
    async def test_sketch_arc_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_arc")
        assert handler is not None

        result = await handler(cx=5.0, cy=5.0, radius=3.0, start_angle=0.0, end_angle=180.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["center"] == [5.0, 5.0]
        assert parsed["data"]["radius"] == 3.0
        assert parsed["data"]["start_angle"] == 0.0
        assert parsed["data"]["end_angle"] == 180.0
        work_part.Curves.CreateArcBuilder.assert_called_once()


class TestSketchRectangle:
    """Test nx_sketch_rectangle tool."""

    @pytest.mark.asyncio
    async def test_sketch_rectangle_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_rectangle")
        assert handler is not None

        result = await handler(x1=0.0, y1=0.0, x2=10.0, y2=5.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["corner1"] == [0.0, 0.0]
        assert parsed["data"]["corner2"] == [10.0, 5.0]
        assert parsed["data"]["width"] == 10.0
        assert parsed["data"]["height"] == 5.0
        assert len(parsed["data"]["curves"]) == 4

        # 4 line builders should have been created
        assert work_part.Curves.CreateLineBuilder.call_count == 4


class TestSketchConstraint:
    """Test nx_sketch_constraint tool."""

    @pytest.mark.asyncio
    async def test_constraint_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_constraint")
        assert handler is not None

        result = await handler(
            constraint_type="horizontal",
            targets=["Line1", "Line2"],
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["constraint_type"] == "horizontal"
        assert parsed["data"]["targets"] == ["Line1", "Line2"]
        work_part.Sketches.ActiveSketch.CreateGeometricConstraint.assert_called_once()

    @pytest.mark.asyncio
    async def test_constraint_with_value(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_constraint")

        result = await handler(
            constraint_type="dimension",
            targets=["Line1"],
            value=25.0,
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["value"] == 25.0
        work_part.Sketches.ActiveSketch.CreateDimension.assert_called_once()

    @pytest.mark.asyncio
    async def test_constraint_invalid_type(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_sketch_constraint")

        result = await handler(
            constraint_type="invalid_type",
            targets=["Line1"],
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert "invalid_type" in parsed["message"]


class TestFinishSketch:
    """Test nx_finish_sketch tool."""

    @pytest.mark.asyncio
    async def test_finish_sketch_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_finish_sketch")
        assert handler is not None

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert "finished" in parsed["message"].lower()
        work_part.Sketches.ActiveSketch.Deactivate.assert_called_once()

    @pytest.mark.asyncio
    async def test_finish_sketch_no_active_sketch(self, _setup_nx):
        nxopen, work_part = _setup_nx
        # Simulate no active sketch
        work_part.Sketches.ActiveSketch = None

        handler = ToolRegistry.get_handler("nx_finish_sketch")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"


class TestToolRegistration:
    """Verify all 6 sketch tools are registered."""

    def test_all_tools_registered(self, _setup_nx):
        expected = {
            "nx_create_sketch",
            "nx_sketch_line",
            "nx_sketch_arc",
            "nx_sketch_rectangle",
            "nx_sketch_constraint",
            "nx_finish_sketch",
        }
        registered = set(ToolRegistry.get_tool_names())
        assert expected == registered
