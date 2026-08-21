"""Tests for NX session wrapper."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.legacy, pytest.mark.fake_nx]


def _create_mock_nxopen():
    """Create a mock NXOpen module tree."""
    nxopen = types.ModuleType("NXOpen")
    nxopen.Session = MagicMock()
    nxopen.Session.GetSession = MagicMock()
    nxopen.UF = types.ModuleType("NXOpen.UF")
    nxopen.UF.UFSession = MagicMock()
    nxopen.UF.UFSession.GetUFSession = MagicMock(return_value=MagicMock())
    nxopen.BasePart = MagicMock()
    nxopen.Part = MagicMock()
    return nxopen


@pytest.fixture
def mock_nxopen():
    """Patch NXOpen into sys.modules."""
    nxopen = _create_mock_nxopen()
    modules = {
        "NXOpen": nxopen,
        "NXOpen.UF": nxopen.UF,
    }
    with patch.dict(sys.modules, modules):
        yield nxopen


def test_session_singleton(mock_nxopen):
    from nx_mcp.nx_session import NXSession

    NXSession._instance = None
    s1 = NXSession.get_instance()
    s2 = NXSession.get_instance()
    assert s1 is s2


def test_session_connected(mock_nxopen):
    mock_session = MagicMock()
    mock_nxopen.Session.GetSession.return_value = mock_session
    from nx_mcp.nx_session import NXSession

    NXSession._instance = None
    session = NXSession.get_instance()
    assert session.is_connected


def test_session_not_connected():
    from nx_mcp.nx_session import NXSession

    NXSession._instance = None
    session = NXSession.get_instance()
    assert not session.is_connected


def test_session_get_work_part_returns_none_when_not_connected():
    from nx_mcp.nx_session import NXSession

    NXSession._instance = None
    session = NXSession.get_instance()
    assert session.work_part is None
