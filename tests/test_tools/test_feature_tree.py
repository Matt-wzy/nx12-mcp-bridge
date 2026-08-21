"""Tests for feature tree tools (3 tools)."""

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
    """Build a self-contained mock NXOpen module tree for feature_tree tests."""
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

    # --- Features ---
    mock_work_part.Features = MagicMock()
    mock_work_part.Features.ToArray = MagicMock(return_value=[])

    # --- Bodies ---
    mock_work_part.Bodies = MagicMock()
    mock_work_part.Bodies.ToArray = MagicMock(return_value=[])

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

        import nx_mcp.tools.feature_tree as mod  # noqa: F401

        yield nxopen, work_part

        ToolRegistry.clear()
        NXSession._instance = None


# ---------------------------------------------------------------------------
# nx_list_features
# ---------------------------------------------------------------------------


class TestListFeatures:
    """Tests for nx_list_features tool."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_features(self, _setup_nx):
        nxopen, work_part = _setup_nx
        work_part.Features.ToArray = MagicMock(return_value=[])

        handler = ToolRegistry.get_handler("nx_list_features")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["features"] == []
        assert parsed["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_returns_feature_list(self, _setup_nx):
        nxopen, work_part = _setup_nx

        feat1 = MagicMock()
        feat1.Name = "Extrude(1)"
        feat1.FeatureType = "EXTRUDE"
        feat1.Timestamp = "2025-01-01T00:00:00"

        feat2 = MagicMock()
        feat2.Name = "Blend(1)"
        feat2.FeatureType = "BLEND"
        feat2.Timestamp = "2025-01-01T00:01:00"

        work_part.Features.ToArray = MagicMock(return_value=[feat1, feat2])

        handler = ToolRegistry.get_handler("nx_list_features")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["count"] == 2
        assert parsed["data"]["features"][0]["name"] == "Extrude(1)"
        assert parsed["data"]["features"][1]["type"] == "BLEND"

    @pytest.mark.asyncio
    async def test_error_when_no_work_part(self, _setup_nx):
        nxopen, work_part = _setup_nx
        NXSession._instance = MagicMock(spec=NXSession)
        NXSession._instance.is_connected = True
        NXSession._instance.require_work_part.side_effect = RuntimeError("No work part is open.")

        handler = ToolRegistry.get_handler("nx_list_features")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"


# ---------------------------------------------------------------------------
# nx_get_feature_info
# ---------------------------------------------------------------------------


class TestGetFeatureInfo:
    """Tests for nx_get_feature_info tool."""

    @pytest.mark.asyncio
    async def test_finds_feature_case_insensitive(self, _setup_nx):
        nxopen, work_part = _setup_nx

        feat = MagicMock()
        feat.Name = "Extrude(1)"
        feat.FeatureType = "EXTRUDE"
        feat.Timestamp = "2025-01-01T00:00:00"
        expr = MagicMock()
        expr.Name = "p0"
        expr.Value = 50.0
        feat.GetExpressions.return_value = [expr]

        work_part.Features.ToArray = MagicMock(return_value=[feat])

        handler = ToolRegistry.get_handler("nx_get_feature_info")
        result = await handler(name="extrude(1)")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["name"] == "Extrude(1)"
        assert parsed["data"]["expressions"][0]["name"] == "p0"

    @pytest.mark.asyncio
    async def test_returns_error_when_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx

        feat = MagicMock()
        feat.Name = "Extrude(1)"
        work_part.Features.ToArray = MagicMock(return_value=[feat])

        handler = ToolRegistry.get_handler("nx_get_feature_info")
        result = await handler(name="nonexistent")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"
        assert "Extrude(1)" in parsed["suggestion"]


# ---------------------------------------------------------------------------
# nx_get_bounding_box
# ---------------------------------------------------------------------------


class TestGetBoundingBox:
    """Tests for nx_get_bounding_box tool."""

    @pytest.mark.asyncio
    async def test_returns_bounding_box_for_first_body(self, _setup_nx):
        nxopen, work_part = _setup_nx

        body = MagicMock()
        body.Name = "Body(1)"
        body.GetBoundingBox.return_value = (0.0, 0.0, 0.0, 10.0, 20.0, 30.0)
        work_part.Bodies.ToArray = MagicMock(return_value=[body])

        handler = ToolRegistry.get_handler("nx_get_bounding_box")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["body"] == "Body(1)"
        assert parsed["data"]["min"] == {"x": 0.0, "y": 0.0, "z": 0.0}
        assert parsed["data"]["max"] == {"x": 10.0, "y": 20.0, "z": 30.0}
        assert parsed["data"]["dimensions"]["length_x"] == 10.0

    @pytest.mark.asyncio
    async def test_finds_named_body(self, _setup_nx):
        nxopen, work_part = _setup_nx

        body1 = MagicMock()
        body1.Name = "Body(1)"
        body2 = MagicMock()
        body2.Name = "MyBody"
        body2.GetBoundingBox.return_value = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        work_part.Bodies.ToArray = MagicMock(return_value=[body1, body2])

        handler = ToolRegistry.get_handler("nx_get_bounding_box")
        result = await handler(body="mybody")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "success"
        assert parsed["data"]["body"] == "MyBody"
        assert parsed["data"]["dimensions"]["length_x"] == 3.0

    @pytest.mark.asyncio
    async def test_error_when_no_bodies(self, _setup_nx):
        nxopen, work_part = _setup_nx
        work_part.Bodies.ToArray = MagicMock(return_value=[])

        handler = ToolRegistry.get_handler("nx_get_bounding_box")
        result = await handler()
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert parsed["error_code"] == "NX_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_error_when_named_body_not_found(self, _setup_nx):
        nxopen, work_part = _setup_nx

        body = MagicMock()
        body.Name = "Body(1)"
        work_part.Bodies.ToArray = MagicMock(return_value=[body])

        handler = ToolRegistry.get_handler("nx_get_bounding_box")
        result = await handler(body="missing")
        parsed = json.loads(result.to_text())

        assert parsed["status"] == "error"
        assert "Body(1)" in parsed["suggestion"]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Verify all 3 feature tree tools are registered."""

    def test_all_tools_registered(self, _setup_nx):
        expected = {
            "nx_list_features",
            "nx_get_feature_info",
            "nx_get_bounding_box",
        }
        registered = set(ToolRegistry.get_tool_names())
        assert expected == registered
