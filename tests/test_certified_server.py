from pathlib import Path

import pytest
from mcp.client import Client

from nx_mcp.certified import CERTIFIED_TOOL_NAMES
from nx_mcp.contracts import NXToolError
from nx_mcp.server import create_server
from nx_mcp.workspace import Workspace

pytestmark = pytest.mark.integration


class StubBridge:
    async def call(self, method: str, params: dict):
        return {
            "connected": True,
            "nx_version": "NX test",
            "bridge_protocol": 1,
            "active_part": None,
        }


class RecordingBridge:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        return self.response


@pytest.mark.asyncio
async def test_default_server_lists_only_certified_tools():
    server = create_server(StubBridge())

    async with Client(server) as client:
        response = await client.list_tools()

    assert {tool.name for tool in response.tools} == CERTIFIED_TOOL_NAMES
    # 2026-11: 53 tools + mass-properties = 54. If this trips after adding a
    # tool, update the count (or derive it from the set).
    assert len(response.tools) == len(CERTIFIED_TOOL_NAMES)


@pytest.mark.asyncio
async def test_status_returns_structured_content():
    server = create_server(StubBridge())

    async with Client(server) as client:
        result = await client.call_tool("nx_status", {})

    assert result.is_error is False
    assert result.structured_content == {
        "status": "success",
        "connected": True,
        "nx_version": "NX test",
        "bridge_protocol": 1,
        "active_part": None,
    }


@pytest.mark.asyncio
async def test_create_part_resolves_path_inside_workspace(tmp_path: Path):
    bridge = RecordingBridge(
        {
            "part": {"id": "part-1", "kind": "part", "name": "bracket", "part_id": "part-1"},
            "message": "created",
        }
    )
    server = create_server(bridge, Workspace(tmp_path))

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_part", {"path": "parts/bracket.prt", "units": "mm"}
        )

    assert result.is_error is False
    assert bridge.calls == [
        (
            "nx_create_part",
            {"path": str(tmp_path / "parts" / "bracket.prt"), "units": "mm"},
        )
    ]


@pytest.mark.asyncio
async def test_file_tool_rejects_workspace_escape_before_bridge_call(tmp_path: Path):
    bridge = RecordingBridge({})
    server = create_server(bridge, Workspace(tmp_path))

    async with Client(server) as client:
        result = await client.call_tool("nx_open_part", {"path": "../secret.prt"})

    assert result.is_error is True
    assert "NX_PATH_OUTSIDE_WORKSPACE" in result.content[0].text
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_bridge_error_preserves_machine_readable_code():
    class ErrorBridge:
        async def call(self, method: str, params: dict):
            raise NXToolError("NX_NO_WORK_PART", "No work part is open")

    server = create_server(ErrorBridge())

    async with Client(server) as client:
        result = await client.call_tool("nx_save_part", {})

    assert result.is_error is True
    assert "NX_NO_WORK_PART" in result.content[0].text


@pytest.mark.asyncio
async def test_certified_tools_publish_strict_input_and_output_schemas():
    server = create_server(StubBridge())

    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert tools["nx_create_part"].input_schema["properties"]["units"]["enum"] == ["mm", "inch"]
    assert tools["nx_create_sketch"].input_schema["properties"]["plane"]["enum"] == [
        "XY",
        "XZ",
        "YZ",
    ]
    assert tools["nx_extrude"].input_schema["properties"]["distance"]["exclusiveMinimum"] == 0
    assert all(tool.output_schema is not None for tool in tools.values())


@pytest.mark.asyncio
async def test_experimental_tools_require_explicit_opt_in_and_keep_journals_disabled():
    server = create_server(StubBridge(), enable_experimental=True)

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert names > CERTIFIED_TOOL_NAMES
    assert "nx_blend" in names
    assert "nx_measure_distance" in names
    assert "nx_run_journal" not in names
    assert "nx_record_start" not in names
    assert "nx_record_stop" not in names


@pytest.mark.asyncio
async def test_journal_tools_require_their_own_explicit_opt_in():
    server = create_server(StubBridge(), enable_experimental=True, enable_journal=True)

    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert {"nx_run_journal", "nx_record_start", "nx_record_stop"} <= names


@pytest.mark.asyncio
async def test_experimental_file_tools_still_enforce_workspace_boundary(tmp_path: Path):
    bridge = RecordingBridge({})
    server = create_server(
        bridge,
        Workspace(tmp_path),
        enable_experimental=True,
    )

    async with Client(server) as client:
        result = await client.call_tool("nx_screenshot", {"path": "../screen.png"})

    assert result.is_error is True
    assert "NX_PATH_OUTSIDE_WORKSPACE" in result.content[0].text
    assert bridge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("tool", ["nx_extrude", "nx_sketch_line"])
async def test_nonfinite_values_fail_before_bridge_call(tmp_path, value, tool):
    bridge = RecordingBridge({})
    server = create_server(bridge, Workspace(tmp_path))
    params = {"sketch_id": "unused", "distance": value}
    if tool == "nx_sketch_line":
        params = {"sketch_id": "unused", "start": {"x": value, "y": 0}, "end": {"x": 1, "y": 1}}
    async with Client(server) as client:
        result = await client.call_tool(tool, params)
    assert result.is_error
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_gdt_frame_maps_chinese_characteristic_and_passes_through():
    bridge = RecordingBridge({"status": "success", "message": "created"})
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_gdt_frame",
            {
                "characteristic": "位置度",
                "tolerance": " 0.05 ",
                "pos_x": 60.0,
                "pos_y": 30.0,
                "datum_primary": "A",
                "datum_secondary": "b",
                "zone_shape": "diameter",
                "material_modifier": "mmc",
            },
        )

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_create_gdt_frame"
    # 中文特征名归一化为英文枚举名；基准字母大写；tolerance 去空白
    assert params["characteristic"] == "position"
    assert params["tolerance"] == "0.05"
    assert params["datum_primary"] == "A"
    assert params["datum_secondary"] == "B"
    assert params["zone_shape"] == "diameter"
    assert params["material_modifier"] == "mmc"


@pytest.mark.asyncio
async def test_gdt_frame_english_characteristic_passthrough():
    bridge = RecordingBridge({"status": "success", "message": "created"})
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_gdt_frame",
            {"characteristic": "Circular Runout", "tolerance": "0.03",
             "pos_x": 0.0, "pos_y": 0.0},
        )

    assert result.is_error is False
    _, params = bridge.calls[0]
    assert params["characteristic"] == "circularrunout"


@pytest.mark.asyncio
async def test_add_component_passes_child_and_defaults():
    bridge = RecordingBridge(
        {
            "status": "success",
            "component_name": "bolt",
            "journal_id": "jid-1",
            "part_path": "C:/m/bolt.prt",
            "children": 2,
            "message": "component 'bolt' added (children 1 -> 2)",
        }
    )
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_add_component", {"child_part": "C:/m/bolt.prt"}
        )

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_add_component"
    assert params["child_part"] == "C:/m/bolt.prt"
    assert params["refset"] == "MODEL"
    assert params["layer"] == 0
    # instance_name defaults to the file name server-side; omitted here
    assert "instance_name" not in params
    assert result.structured_content["component_name"] == "bolt"
    assert result.structured_content["children"] == 2
