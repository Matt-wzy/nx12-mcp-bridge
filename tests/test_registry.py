"""Tests for tool registry."""

import pytest

from nx_mcp.tools.registry import ToolRegistry, mcp_tool

pytestmark = pytest.mark.legacy


def test_register_and_list_tools():
    @mcp_tool(
        name="test_tool",
        description="A test tool",
        params={"input": {"type": "string", "description": "Some input"}},
    )
    async def my_tool(input: str) -> dict:
        return {"result": input}

    tools = ToolRegistry.list_tools()
    assert any(t["name"] == "test_tool" for t in tools)


def test_tool_has_input_schema():
    @mcp_tool(
        name="test_schema",
        description="Schema test",
        params={"x": {"type": "number", "description": "X coordinate"}},
    )
    async def schema_tool(x: float) -> dict:
        return {"x": x}

    tools = ToolRegistry.list_tools()
    t = next(t for t in tools if t["name"] == "test_schema")
    assert "properties" in t["inputSchema"]
    assert "x" in t["inputSchema"]["properties"]


def test_get_handler():
    @mcp_tool(
        name="test_handler",
        description="Handler test",
        params={},
    )
    async def handler_tool() -> dict:
        return {"ok": True}

    handler = ToolRegistry.get_handler("test_handler")
    assert handler is not None
    assert handler.__name__ == "handler_tool"


def test_get_nonexistent_handler():
    handler = ToolRegistry.get_handler("does_not_exist")
    assert handler is None


def test_clear_registry():
    ToolRegistry.clear()
    assert ToolRegistry.list_tools() == []
