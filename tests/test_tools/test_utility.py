"""Tests for utility & view tools (7 tools)."""

import json
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch

import pytest

from nx_mcp.nx_session import NXSession
from nx_mcp.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Reusable helpers
# ---------------------------------------------------------------------------


def _make_mock_nxopen():
    """Build a self-contained mock NXOpen module tree for utility tests."""
    nxopen = types.ModuleType("NXOpen")

    # --- Session ---
    mock_session = MagicMock()
    mock_session.Parts = MagicMock()
    mock_work_part = MagicMock()
    mock_work_part.Name = "test_part"
    mock_session.Parts.Work = mock_work_part
    mock_session.Parts.Display = mock_work_part
    nxopen.Session = MagicMock()
    nxopen.Session.GetSession = MagicMock(return_value=mock_session)
    nxopen._mock_session = mock_session

    # --- ModelingViews ---
    mock_work_view = MagicMock()
    mock_work_view.Fit = MagicMock()
    mock_work_view.Orient = MagicMock()
    mock_work_part.ModelingViews = MagicMock()
    mock_work_part.ModelingViews.WorkView = mock_work_view

    # --- UF ---
    uf = types.ModuleType("NXOpen.UF")
    uf_session = MagicMock()
    uf_session.GetUFSession = MagicMock(return_value=uf_session)
    mock_image_builder = MagicMock()
    mock_image_builder.Commit = MagicMock()
    uf_session.Disp = MagicMock()
    uf_session.Disp.CreateImageExportBuilder = MagicMock(return_value=mock_image_builder)
    uf.UFSession = uf_session

    # Image export builder file format enum
    image_export_mod = MagicMock()
    image_export_mod.FileFormatType = MagicMock()
    image_export_mod.FileFormatType.Png = "Png"
    uf.ImageExportBuilder = image_export_mod

    nxopen.UF = uf

    # --- View enums ---
    view_mod = types.ModuleType("NXOpen.View")
    view_orient_mock = MagicMock()
    view_orient_mock.kFront = "kFront"
    view_orient_mock.kBack = "kBack"
    view_orient_mock.kTop = "kTop"
    view_orient_mock.kBottom = "kBottom"
    view_orient_mock.kLeft = "kLeft"
    view_orient_mock.kRight = "kRight"
    view_orient_mock.kIsometric = "kIsometric"
    view_orient_mock.kTrimetric = "kTrimetric"
    view_mod.ViewOrientation = view_orient_mock
    nxopen.View = view_mod

    nxopen._mock_work_view = mock_work_view
    nxopen._mock_image_builder = mock_image_builder

    modules = {"NXOpen": nxopen, "NXOpen.UF": uf, "NXOpen.View": view_mod}
    return modules, nxopen, mock_work_part


@pytest.fixture(autouse=True)
def _setup_nx():
    """Patch NXOpen and reset session/registry for each test."""
    modules, nxopen, work_part = _make_mock_nxopen()
    with patch.dict(sys.modules, modules):
        NXSession._instance = None
        ToolRegistry.clear()

        import nx_mcp.tools.utility as mod  # noqa: F401

        yield nxopen, work_part

        ToolRegistry.clear()
        NXSession._instance = None


# ---------------------------------------------------------------------------
# nx_fit_view
# ---------------------------------------------------------------------------


class TestFitView:
    """Tests for nx_fit_view tool."""

    @pytest.mark.asyncio
    async def test_fit_view_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_fit_view")

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert "fitted" in parsed["message"].lower()
        nxopen._mock_work_view.Fit.assert_called_once()

    @pytest.mark.asyncio
    async def test_fit_view_no_work_part(self, _setup_nx):
        nxopen, work_part = _setup_nx
        NXSession._instance = MagicMock(spec=NXSession)
        NXSession._instance.is_connected = True
        NXSession._instance.require_work_part.side_effect = RuntimeError("No work part is open.")

        handler = ToolRegistry.get_handler("nx_fit_view")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"


# ---------------------------------------------------------------------------
# nx_set_view
# ---------------------------------------------------------------------------


class TestSetView:
    """Tests for nx_set_view tool."""

    @pytest.mark.asyncio
    async def test_set_view_valid_orientation(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_set_view")

        result = await handler(orientation="front")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["orientation"] == "front"
        nxopen._mock_work_view.Orient.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_view_invalid_orientation(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_set_view")

        result = await handler(orientation="diagonal")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_set_view_all_orientations(self, _setup_nx):
        """Verify all 8 valid orientations are accepted."""
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_set_view")
        valid = ["front", "back", "top", "bottom", "left", "right", "isometric", "trimetric"]

        for orient in valid:
            result = await handler(orientation=orient)
            parsed = json.loads(result.to_text())
            assert parsed["status"] == "success", f"Expected success for '{orient}'"
            assert parsed["data"]["orientation"] == orient


# ---------------------------------------------------------------------------
# nx_undo
# ---------------------------------------------------------------------------


class TestUndo:
    """Tests for nx_undo tool."""

    @pytest.mark.asyncio
    async def test_undo_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_undo")

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert "undo" in parsed["message"].lower()
        nxopen._mock_session.UndoLastNVisibleMarks.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_undo_no_session(self, _setup_nx):
        nxopen, work_part = _setup_nx
        NXSession._instance = MagicMock(spec=NXSession)
        NXSession._instance.is_connected = True
        NXSession._instance.require.side_effect = RuntimeError("NX is not connected.")

        handler = ToolRegistry.get_handler("nx_undo")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"


# ---------------------------------------------------------------------------
# nx_screenshot
# ---------------------------------------------------------------------------


class TestScreenshot:
    """Tests for nx_screenshot tool."""

    @pytest.mark.asyncio
    async def test_screenshot_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_screenshot")

        result = await handler(path=r"C:\tmp\shot.png")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["path"] == r"C:\tmp\shot.png"
        nxopen._mock_image_builder.Commit.assert_called_once()


# ---------------------------------------------------------------------------
# nx_run_journal
# ---------------------------------------------------------------------------


class TestRunJournal:
    """Tests for nx_run_journal tool."""

    @pytest.mark.asyncio
    async def test_run_journal_file_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_run_journal")

        result = await handler(path=r"C:\nonexistent\script.py")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_run_journal_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_run_journal")

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
            tmp.write(b"# test journal")
            tmp_path = tmp.name

        try:
            result = await handler(path=tmp_path)
            parsed = json.loads(result.to_text())

            assert parsed["status"] == "success"
            assert parsed["data"]["path"] == tmp_path
            nxopen._mock_session.ExecuteJournal.assert_called_once_with(tmp_path)
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_run_journal_wrong_extension(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_run_journal")

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp.write(b"# test")
            tmp_path = tmp.name

        try:
            result = await handler(path=tmp_path)
            parsed = json.loads(result.to_text())

            assert parsed["status"] == "error"
            assert parsed["error_code"] == "NX_INVALID_PARAMS"
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# nx_record_start
# ---------------------------------------------------------------------------


class TestRecordStart:
    """Tests for nx_record_start tool."""

    @pytest.mark.asyncio
    async def test_record_start_success(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_record_start")

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert "started" in parsed["message"].lower()
        nxopen._mock_session.BeginJournalRecording.assert_called_once()


# ---------------------------------------------------------------------------
# nx_record_stop
# ---------------------------------------------------------------------------


class TestRecordStop:
    """Tests for nx_record_stop tool."""

    @pytest.mark.asyncio
    async def test_record_stop_without_save_path(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_record_stop")

        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert "stopped" in parsed["message"].lower()
        assert "save_path" not in parsed.get("data", {})
        nxopen._mock_session.EndJournalRecording.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_stop_with_save_path(self, _setup_nx):
        nxopen, work_part = _setup_nx
        handler = ToolRegistry.get_handler("nx_record_stop")

        result = await handler(save_path=r"C:\journals\recording.py")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["save_path"] == r"C:\journals\recording.py"
        nxopen._mock_session.EndJournalRecording.assert_called_once()


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all 7 utility tools are registered."""

    def test_all_tools_registered(self, _setup_nx):
        expected = {
            "nx_fit_view",
            "nx_set_view",
            "nx_undo",
            "nx_screenshot",
            "nx_run_journal",
            "nx_record_start",
            "nx_record_stop",
        }
        registered = set(ToolRegistry.get_tool_names())
        assert expected == registered
