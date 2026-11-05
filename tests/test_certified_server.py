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
async def test_weld_symbol_maps_chinese_type_contour_finish():
    bridge = RecordingBridge({"status": "success", "message": "created"})
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_weld_symbol",
            {
                "weld_type": "角焊缝",
                "weld_size": " 5 ",
                "pos_x": 10.0,
                "pos_y": 20.0,
                "contour": "凹面",
                "finish": "打磨",
            },
        )

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_create_weld_symbol"
    assert params["weld_type"] == "fillet"
    assert params["weld_size"] == "5"
    assert params["contour"] == "concave"
    assert params["finish"] == "grinding"
    assert params["standard"] == "gb"
    assert params["side"] == "arrow"


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


@pytest.mark.asyncio
async def test_create_stock_passes_mode_and_defaults():
    bridge = RecordingBridge(
        {
            "status": "success",
            "mode": "box",
            "body_id": "BODY_1",
            "feature_tag": 42,
            "stock_volume_mm3": 123456.0,
            "part_volume_mm3": 90000.0,
            "utilization_pct": 72.9,
            "bbox_min": {"x": 0.0, "y": 0.0, "z": 0.0},
            "bbox_max": {"x": 100.0, "y": 60.0, "z": 10.0},
            "message": "stock block 104x64x14 allowance=2 utilization=72.9%",
        }
    )
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool("nx_create_stock", {"mode": "cyl", "dia": 55.0})

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_create_stock"
    assert params["mode"] == "cyl"
    assert params["dia"] == 55.0
    assert params["allowance"] == 2.0
    # axis defaults to auto -> omitted from the wire params
    assert "axis" not in params
    assert "body_id" not in params
    content = result.structured_content
    assert content["body_id"] == "BODY_1"
    assert content["utilization_pct"] == 72.9
    assert content["bbox_min"] == {"x": 0.0, "y": 0.0, "z": 0.0}


@pytest.mark.asyncio
async def test_create_table_carries_cells_and_echoes_contract():
    bridge = RecordingBridge(
        {
            "status": "success",
            "rows": 2,
            "cols": 3,
            "note_tag": 77,
            "tables_after": 1,
            "cell_echo": "孔号",
            "font": "font=chinesef (id 4) applied",
            "message": "table 2x3 created at (30,200) cells=6 tables 0->1",
        }
    )
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_table",
            {
                "pos_x": 30.0,
                "pos_y": 200.0,
                "cells": "NO,DIA,DEPTH|1,8,15",
                "header": True,
                "font": "chinesef",
            },
        )

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_create_table"
    assert params["cells"] == "NO,DIA,DEPTH|1,8,15"
    assert params["header"] is True
    assert params["font"] == "chinesef"
    # zero-default sizes stay off the wire; the loader applies 20/6
    assert "col_width" not in params
    assert "row_height" not in params
    content = result.structured_content
    assert content["rows"] == 2
    assert content["cols"] == 3
    assert content["cell_echo"] == "孔号"


@pytest.mark.asyncio
async def test_create_table_rejects_cjk_cells_by_default():
    bridge = RecordingBridge({})
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_table",
            {"pos_x": 30.0, "pos_y": 200.0, "cells": "孔号,直径|1,8"},
        )

    assert result.is_error is True
    assert "NX_CJK_CELL_TEXT" in result.content[0].text
    # rejection happens before any bridge call
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_create_table_allow_cjk_passes_through():
    bridge = RecordingBridge(
        {
            "status": "success",
            "rows": 1,
            "cols": 2,
            "note_tag": 5,
            "tables_after": 1,
            "cell_echo": "孔号",
            "font": "font=none",
            "message": "created (CJK cells render as boxes in NX12)",
        }
    )
    server = create_server(bridge)

    async with Client(server) as client:
        result = await client.call_tool(
            "nx_create_table",
            {"pos_x": 30.0, "pos_y": 200.0, "cells": "孔号,直径", "allow_cjk": True},
        )

    assert result.is_error is False
    method, params = bridge.calls[0]
    assert method == "nx_create_table"
    assert params["allow_cjk"] is True
    assert params["cells"] == "孔号,直径"
