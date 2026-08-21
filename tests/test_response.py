"""Tests for response types."""

import pytest

from nx_mcp.response import ToolError, ToolResult

pytestmark = pytest.mark.legacy


def test_tool_result_success():
    result = ToolResult.success(
        data={"feature_name": "EXTRUDE(1)", "type": "EXTRUDE"},
        message="Extrude created: EXTRUDE(1)",
    )
    assert result.status == "success"
    assert result.data["feature_name"] == "EXTRUDE(1)"
    assert result.message == "Extrude created: EXTRUDE(1)"
    text = result.to_text()
    assert "EXTRUDE(1)" in text


def test_tool_error():
    err = ToolError(
        error_code="NX_FEATURE_NOT_FOUND",
        message="Feature 'Boss' not found",
        suggestion="Use nx_list_features to see all features",
    )
    assert err.status == "error"
    assert err.error_code == "NX_FEATURE_NOT_FOUND"
    text = err.to_text()
    assert "NX_FEATURE_NOT_FOUND" in text


def test_tool_result_from_exception():
    err = ToolResult.from_exception(
        Exception("NX is not running"),
        error_code="NX_NOT_CONNECTED",
    )
    assert err.status == "error"
    assert "NX is not running" in err.message


def test_tool_error_without_suggestion():
    err = ToolError(
        error_code="NX_INVALID_PARAMS",
        message="Missing required parameter: path",
    )
    assert err.suggestion is None
    text = err.to_text()
    assert "NX_INVALID_PARAMS" in text
