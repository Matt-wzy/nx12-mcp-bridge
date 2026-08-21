"""Tests for assembly tools (4 tools)."""

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
    """Build a self-contained mock NXOpen module tree for assembly tests."""
    nxopen = types.ModuleType("NXOpen")

    # --- Session ---
    mock_session = MagicMock()
    mock_session.Parts = MagicMock()
    mock_work_part = MagicMock()
    mock_work_part.Name = "test_assembly"
    mock_session.Parts.Work = mock_work_part
    nxopen.Session = MagicMock()
    nxopen.Session.GetSession = MagicMock(return_value=mock_session)
    nxopen._mock_session = mock_session

    # --- ComponentAssembly ---
    comp_assembly = MagicMock()

    # AddComponent returns a mock component
    mock_added_component = MagicMock()
    mock_added_component.Name = "Bracket"
    comp_assembly.AddComponent = MagicMock(return_value=mock_added_component)

    # RootComponent with children
    root_component = MagicMock()
    child1 = MagicMock()
    child1.Name = "Part_A"
    child2 = MagicMock()
    child2.Name = "Part_B"
    root_component.GetChildren = MagicMock(return_value=[child1, child2])
    comp_assembly.RootComponent = root_component

    # Constraints builder
    constraints_builder = MagicMock()
    mock_constraint = MagicMock()
    mock_constraint.Component = None
    mock_constraint.Offset = 0.0
    mock_constraint.FirstConstraintReference = None
    mock_constraint.SecondConstraintReference = None
    constraints_builder.CreateConstraint = MagicMock(return_value=mock_constraint)
    constraints_builder.Commit = MagicMock()
    constraints_builder.Destroy = MagicMock()
    comp_assembly.CreateConstraintsBuilder = MagicMock(return_value=constraints_builder)

    # MoveComponent
    comp_assembly.MoveComponent = MagicMock()

    mock_work_part.ComponentAssembly = comp_assembly

    # NXOpen.Assemblies.Constraint.Type enum
    assemblies_mod = types.ModuleType("NXOpen.Assemblies")
    constraint_mod = MagicMock()
    constraint_type_mock = MagicMock()
    constraint_type_mock.Touch = "Touch"
    constraint_type_mock.Align = "Align"
    constraint_type_mock.Orient = "Orient"
    constraint_type_mock.Center = "Center"
    constraint_type_mock.Angular = "Angular"
    constraint_mod.Type = constraint_type_mock
    assemblies_mod.Constraint = constraint_mod
    nxopen.Assemblies = assemblies_mod

    # Vector3d / Matrix3x3 / Transform
    nxopen.Vector3d = MagicMock(return_value=MagicMock())
    nxopen.Matrix3x3 = MagicMock(return_value=MagicMock())
    nxopen.Transform = MagicMock(return_value=MagicMock())

    nxopen._constraints_builder = constraints_builder
    nxopen._mock_constraint = mock_constraint
    nxopen._added_component = mock_added_component
    nxopen._comp_assembly = comp_assembly

    # --- UF ---
    uf = types.ModuleType("NXOpen.UF")
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

        # Import the module so decorators register
        import nx_mcp.tools.assembly as mod  # noqa: F401

        yield nxopen, work_part

        ToolRegistry.clear()
        NXSession._instance = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAddComponent:
    """Test nx_add_component tool."""

    @pytest.mark.asyncio
    async def test_add_component_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_component")
        assert handler is not None

        result = await handler(part_path="C:\\parts\\bracket.prt")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["part_path"] == "C:\\parts\\bracket.prt"
        work_part.ComponentAssembly.AddComponent.assert_called_once_with(
            "C:\\parts\\bracket.prt", ""
        )

    @pytest.mark.asyncio
    async def test_add_component_with_name(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_component")

        result = await handler(part_path="C:\\parts\\shaft.prt", name="MainShaft")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        work_part.ComponentAssembly.AddComponent.assert_called_once_with(
            "C:\\parts\\shaft.prt", "MainShaft"
        )


class TestMateComponent:
    """Test nx_mate_component tool."""

    @pytest.mark.asyncio
    async def test_mate_touch_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_mate_component")
        assert handler is not None

        result = await handler(
            component="Bracket",
            mate_type="touch",
            references=["Face_A", "Face_B"],
            offset=0.0,
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["mate_type"] == "touch"
        assert parsed["data"]["component"] == "Bracket"
        assert parsed["data"]["references"] == ["Face_A", "Face_B"]

        constraints_builder = nxopen._constraints_builder
        constraints_builder.CreateConstraint.assert_called_once()
        constraints_builder.Commit.assert_called_once()
        constraints_builder.Destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_mate_invalid_type(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_mate_component")

        result = await handler(component="Bracket", mate_type="weld")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"
        assert "weld" in parsed["message"]

    @pytest.mark.asyncio
    async def test_mate_all_types(self, _setup_nx):
        """Verify each valid mate type is accepted."""
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_mate_component")

        for mtype in ("touch", "align", "orient", "center", "align_angle"):
            ToolRegistry.clear()
            result = await handler(component="Bracket", mate_type=mtype)
            parsed = json.loads(result.to_text())
            assert parsed["status"] == "success", f"mate_type={mtype} should succeed"


class TestListComponents:
    """Test nx_list_components tool."""

    @pytest.mark.asyncio
    async def test_list_components_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_list_components")
        assert handler is not None

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["count"] == 2
        comp_names = [c["name"] for c in parsed["data"]["components"]]
        assert "Part_A" in comp_names
        assert "Part_B" in comp_names

    @pytest.mark.asyncio
    async def test_list_components_empty(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_list_components")

        # Override to return empty children
        work_part.ComponentAssembly.RootComponent.GetChildren = MagicMock(return_value=[])

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["count"] == 0
        assert parsed["data"]["components"] == []


class TestRepositionComponent:
    """Test nx_reposition_component tool."""

    @pytest.mark.asyncio
    async def test_reposition_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_reposition_component")
        assert handler is not None

        result = await handler(
            component="Part_A",
            dx=10.0,
            dy=20.0,
            dz=30.0,
            rx=45.0,
            ry=90.0,
            rz=0.0,
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["component"] == "Part_A"
        assert parsed["data"]["translation"] == [10.0, 20.0, 30.0]
        assert parsed["data"]["rotation"] == [45.0, 90.0, 0.0]

        nxopen._comp_assembly.MoveComponent.assert_called_once()

    @pytest.mark.asyncio
    async def test_reposition_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_reposition_component")

        result = await handler(component="NonExistent", dx=5.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"
        assert "NonExistent" in parsed["message"]

    @pytest.mark.asyncio
    async def test_reposition_defaults(self, _setup_nx):
        """Test with default translation/rotation (all zeros)."""
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_reposition_component")

        result = await handler(component="Part_B")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["translation"] == [0.0, 0.0, 0.0]
        assert parsed["data"]["rotation"] == [0.0, 0.0, 0.0]


class TestToolRegistration:
    """Verify all 4 assembly tools are registered."""

    def test_all_tools_registered(self, _setup_nx):
        expected = {
            "nx_add_component",
            "nx_mate_component",
            "nx_list_components",
            "nx_reposition_component",
        }
        registered = set(ToolRegistry.get_tool_names())
        assert expected == registered
