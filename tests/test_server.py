"""Tests for MCP server entry point."""

import pytest

from nx_mcp.certified import CERTIFIED_TOOL_NAMES
from nx_mcp.server import create_server


@pytest.mark.asyncio
async def test_server_lists_tools(mock_nx):
    server = create_server()
    assert server is not None
    assert server.name == "nx-mcp"
    assert {tool.name for tool in await server.list_tools()} == CERTIFIED_TOOL_NAMES
