"""Tests for drawing tools (5 tools)."""

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
    """Build a self-contained mock NXOpen module tree for drawing tests."""
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

    # --- DrawingSheets ---
    mock_sheet_builder = MagicMock()
    mock_sheet = MagicMock()
    mock_sheet.Name = "Sheet1"
    mock_sheet_builder.Commit = MagicMock(return_value=mock_sheet)
    mock_sheet_builder.Destroy = MagicMock()
    mock_work_part.DrawingSheets = MagicMock()
    mock_work_part.DrawingSheets.CreateDrawingSheetBuilder = MagicMock(
        return_value=mock_sheet_builder
    )
    mock_work_part.DrawingSheets.ToArray = MagicMock(return_value=[mock_sheet])

    # --- DrawingViews ---
    mock_base_view_builder = MagicMock()
    mock_base_view = MagicMock()
    mock_base_view.Name = "BaseView1"
    mock_base_view_builder.Commit = MagicMock(return_value=mock_base_view)
    mock_base_view_builder.Destroy = MagicMock()

    mock_proj_view_builder = MagicMock()
    mock_proj_view = MagicMock()
    mock_proj_view.Name = "ProjView1"
    mock_proj_view_builder.Commit = MagicMock(return_value=mock_proj_view)
    mock_proj_view_builder.Destroy = MagicMock()

    mock_work_part.DrawingViews = MagicMock()
    mock_work_part.DrawingViews.CreateBaseViewBuilder = MagicMock(
        return_value=mock_base_view_builder
    )
    mock_work_part.DrawingViews.CreateProjectedViewBuilder = MagicMock(
        return_value=mock_proj_view_builder
    )
    mock_work_part.DrawingViews.ToArray = MagicMock(return_value=[mock_base_view])

    # --- Annotations ---
    mock_dim_builder = MagicMock()
    mock_dim = MagicMock()
    mock_dim.Name = "Dim0"
    mock_dim_builder.Commit = MagicMock(return_value=mock_dim)
    mock_dim_builder.Destroy = MagicMock()
    mock_work_part.Annotations = MagicMock()
    mock_work_part.Annotations.CreateDimensionBuilder = MagicMock(return_value=mock_dim_builder)

    # --- ExportManager ---
    mock_pdf_exporter = MagicMock()
    mock_pdf_exporter.Apply = MagicMock()
    mock_work_part.ExportManager = MagicMock()
    mock_work_part.ExportManager.CreatePdfExporter = MagicMock(return_value=mock_pdf_exporter)

    # --- UF ---
    uf = types.ModuleType("NXOpen.UF")
    uf.UFSession = MagicMock()
    uf.UFSession.GetUFSession = MagicMock(return_value=MagicMock())
    nxopen.UF = uf

    nxopen._mock_sheet_builder = mock_sheet_builder
    nxopen._mock_base_view_builder = mock_base_view_builder
    nxopen._mock_proj_view_builder = mock_proj_view_builder
    nxopen._mock_dim_builder = mock_dim_builder
    nxopen._mock_pdf_exporter = mock_pdf_exporter

    modules = {"NXOpen": nxopen, "NXOpen.UF": uf}
    return modules, nxopen, mock_work_part


@pytest.fixture(autouse=True)
def _setup_nx():
    """Patch NXOpen and reset session/registry for each test."""
    modules, nxopen, work_part = _make_mock_nxopen()
    with patch.dict(sys.modules, modules):
        NXSession._instance = None
        ToolRegistry.clear()

        import nx_mcp.tools.drawing as mod  # noqa: F401

        yield nxopen, work_part

        ToolRegistry.clear()
        NXSession._instance = None


# ---------------------------------------------------------------------------
# nx_create_drawing
# ---------------------------------------------------------------------------


class TestCreateDrawing:
    """Tests for nx_create_drawing tool."""

    @pytest.mark.asyncio
    async def test_create_drawing_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_drawing")

        result = await handler(name="MySheet", size="A3", scale=1.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["sheet_name"] == "Sheet1"
        assert parsed["data"]["size"] == "A3"
        assert parsed["data"]["scale"] == "1.0:1"
        nxopen._mock_sheet_builder.Commit.assert_called_once()
        nxopen._mock_sheet_builder.Destroy.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_drawing_invalid_size(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_drawing")

        result = await handler(name="Sheet1", size="Z9", scale=1.0)
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_create_drawing_defaults(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_create_drawing")

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["size"] == "A3"


# ---------------------------------------------------------------------------
# nx_add_base_view
# ---------------------------------------------------------------------------


class TestAddBaseView:
    """Tests for nx_add_base_view tool."""

    @pytest.mark.asyncio
    async def test_add_base_view_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_base_view")

        result = await handler(drawing="Sheet1", body="Body1", view="front")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["view_name"] == "BaseView1"
        assert parsed["data"]["orientation"] == "front"
        nxopen._mock_base_view_builder.Commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_base_view_invalid_view(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_base_view")

        result = await handler(drawing="Sheet1", body="Body1", view="diagonal")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_add_base_view_sheet_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx
        work_part.DrawingSheets.ToArray = MagicMock(return_value=[])
        handler = ToolRegistry.get_handler("nx_add_base_view")

        result = await handler(drawing="MissingSheet", body="Body1", view="front")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"


# ---------------------------------------------------------------------------
# nx_add_projection_view
# ---------------------------------------------------------------------------


class TestAddProjectionView:
    """Tests for nx_add_projection_view tool."""

    @pytest.mark.asyncio
    async def test_add_projection_view_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_projection_view")

        result = await handler(base_view="BaseView1", direction="right")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["view_name"] == "ProjView1"
        assert parsed["data"]["direction"] == "right"
        nxopen._mock_proj_view_builder.Commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_projection_view_invalid_direction(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_projection_view")

        result = await handler(base_view="BaseView1", direction="diagonal")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_add_projection_view_base_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx
        work_part.DrawingViews.ToArray = MagicMock(return_value=[])
        handler = ToolRegistry.get_handler("nx_add_projection_view")

        result = await handler(base_view="MissingView", direction="right")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"


# ---------------------------------------------------------------------------
# nx_add_dimension
# ---------------------------------------------------------------------------


class TestAddDimension:
    """Tests for nx_add_dimension tool."""

    @pytest.mark.asyncio
    async def test_add_dimension_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_dimension")

        result = await handler(
            view="BaseView1",
            object1="Edge1",
            object2="Edge2",
            dim_type="horizontal",
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["dim_type"] == "horizontal"
        assert parsed["data"]["object1"] == "Edge1"
        assert parsed["data"]["object2"] == "Edge2"
        nxopen._mock_dim_builder.Commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_dimension_invalid_type(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_dimension")

        result = await handler(view="V1", object1="E1", dim_type="chamfer")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_add_dimension_diameter_with_object2_rejected(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_dimension")

        result = await handler(
            view="BaseView1",
            object1="Edge1",
            object2="Edge2",
            dim_type="diameter",
        )
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"
        assert "single object" in parsed["message"].lower()

    @pytest.mark.asyncio
    async def test_add_dimension_view_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx
        work_part.DrawingViews.ToArray = MagicMock(return_value=[])
        handler = ToolRegistry.get_handler("nx_add_dimension")

        result = await handler(view="MissingView", object1="E1", dim_type="aligned")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_add_radius_dimension_single_object(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_add_dimension")

        result = await handler(view="BaseView1", object1="Arc1", dim_type="radius")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["dim_type"] == "radius"
        assert parsed["data"]["object2"] is None


# ---------------------------------------------------------------------------
# nx_export_drawing_pdf
# ---------------------------------------------------------------------------


class TestExportDrawingPdf:
    """Tests for nx_export_drawing_pdf tool."""

    @pytest.mark.asyncio
    async def test_export_pdf_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_export_drawing_pdf")

        result = await handler(path=r"C:\output\drawing.pdf")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["path"] == r"C:\output\drawing.pdf"
        nxopen._mock_pdf_exporter.Apply.assert_called_once()

    @pytest.mark.asyncio
    async def test_export_pdf_no_work_part(self, _setup_nx):
        nxopen, work_part = _setup_nx
        NXSession._instance = MagicMock(spec=NXSession)
        NXSession._instance.is_connected = True
        NXSession._instance.require.return_value = nxopen._mock_session
        NXSession._instance.require_work_part.side_effect = RuntimeError("No work part is open.")

        handler = ToolRegistry.get_handler("nx_export_drawing_pdf")
        result = await handler(path=r"C:\output\drawing.pdf")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all 5 drawing tools are registered."""

    def test_all_tools_registered(self, _setup_nx):
        expected = {
            "nx_create_drawing",
            "nx_add_base_view",
            "nx_add_projection_view",
            "nx_add_dimension",
            "nx_export_drawing_pdf",
        }
        registered = set(ToolRegistry.get_tool_names())
        assert expected == registered
