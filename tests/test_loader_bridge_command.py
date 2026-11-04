"""Tests for the Loader command translation layer (nx_mcp.loader_bridge._command).

The C# Loader speaks a space-separated token protocol; `_command` maps MCP
tool calls onto it. These tests pin the wire format of the WAVE-link /
sketch-read / dimension-value commands added for the NX12 adaptation, plus a
few regressions for the existing grammar.
"""

from nx_mcp.loader_bridge import _command


def test_status_command_has_no_args():
    assert _command("nx_status", {}) == "nx_status"


def test_open_part_passes_path_verbatim():
    assert (
        _command("nx_open_part", {"path": "C:/dir/part.prt"})
        == "nx_open_part C:/dir/part.prt"
    )


def test_wave_link_minimal_form_uses_timestamp_snapshot():
    cmd = _command(
        "nx_create_wave_link",
        {"source_part": "C:/m/master.prt", "target_part": "C:/m/op10.prt"},
    )
    # body_index defaults to 1; no assoc flag -> fixed timestamp (extract)
    assert cmd == "nx_create_wave_link C:/m/master.prt C:/m/op10.prt 1"


def test_wave_link_associative_flag():
    cmd = _command(
        "nx_create_wave_link",
        {
            "source_part": "C:/m/master.prt",
            "target_part": "C:/m/op10.prt",
            "associative": True,
        },
    )
    assert cmd.endswith(" 1 assoc=1")


def test_wave_link_body_index_and_placement():
    cmd = _command(
        "nx_create_wave_link",
        {
            "source_part": "C:/m/master.prt",
            "target_part": "C:/m/op20.prt",
            "body_index": 2,
            "position": {"x": 10.0, "y": 0.0, "z": -3.5},
        },
    )
    assert cmd == (
        "nx_create_wave_link C:/m/master.prt C:/m/op20.prt 2"
        " x=10 y=0 z=-3.5"
    )


def test_read_sketch():
    assert (
        _command("nx_read_sketch", {"sketch_name": "SKETCH_1"})
        == "nx_read_sketch SKETCH_1"
    )


def test_set_dimension_value_formats_numbers():
    cmd = _command(
        "nx_set_dimension_value",
        {"target_size": 99.96, "new_value": 100.0},
    )
    assert cmd == "nx_set_dimension_value 99.96 100"


def test_mass_properties_defaults_to_steel():
    assert _command("nx_mass_properties", {}) == "nx_mass_properties 7830 0.99"


def test_mass_properties_custom_density():
    assert (
        _command("nx_mass_properties", {"density_kg_m3": 2700.5})
        == "nx_mass_properties 2700.5 0.99"
    )


def test_centerline_wire_format():
    cmd = _command(
        "nx_create_centerline",
        {"view_id": "0", "body_id": "1", "face_index": 3},
    )
    assert cmd == "nx_create_centerline 0 1 3"


def test_export_dwg_is_local_not_pipe():
    # NX 内 DxfdwgCreator.Commit() 从不 spawn translator（2026-11-04 实测，
    # 见 受阻项调研档案 第 2 节），nx_export_dwg 由 Python 侧
    # 进程外 translator 实现，没有线格式。
    assert _command("nx_export_dwg", {"path": "C:/w/out.dxf"}) is None


def test_detail_view_boundary_and_placement():
    cmd = _command(
        "nx_create_detail_view",
        {
            "parent_view_id": "V_TOP",
            "center_x": 30.0,
            "center_y": 30.0,
            "radius": 12.5,
            "pos_x": 200.0,
            "pos_y": 60.0,
        },
    )
    assert cmd == "nx_create_detail_view V_TOP 30 30 12.5 200 60"


def test_detail_view_scale_and_name():
    cmd = _command(
        "nx_create_detail_view",
        {
            "parent_view_id": "0",
            "center_x": 0.0,
            "center_y": 0.0,
            "radius": 10.0,
            "pos_x": 100.0,
            "pos_y": 50.0,
            "scale": 2.0,
            "view_name": "DETAIL_A",
        },
    )
    assert cmd == "nx_create_detail_view 0 0 0 10 100 50 2 DETAIL_A"


def test_measure_angle_wire_format():
    cmd = _command("nx_measure_angle", {"body_id": "1", "edge_index_1": 2, "edge_index_2": 5})
    assert cmd == "nx_measure_angle 1 2 5"


def test_mass_properties_per_body_flag():
    cmd = _command(
        "nx_mass_properties",
        {"density_kg_m3": 2700.0, "accuracy": 0.99, "per_body": True},
    )
    assert cmd == "nx_mass_properties 2700 0.99 per=1"


def test_section_view_minimal_wire_format():
    cmd = _command(
        "nx_create_section_view",
        {
            "parent_view_id": "0",
            "cut_x1": 20.0, "cut_y1": 30.0,
            "cut_x2": 120.0, "cut_y2": 30.0,
            "pos_x": 200.0, "pos_y": 100.0,
        },
    )
    assert cmd == "nx_create_section_view 0 20 30 120 30 200 100"


def test_section_view_full_wire_format():
    cmd = _command(
        "nx_create_section_view",
        {
            "parent_view_id": "TOP",
            "cut_x1": 20.0, "cut_y1": 30.0,
            "cut_x2": 120.0, "cut_y2": 30.0,
            "pos_x": 200.0, "pos_y": 100.0,
            "scale": 2.0,
            "arrow_dx": 0.0, "arrow_dy": -1.0,
            "step_dx": 0.0, "step_dy": 1.0,
            "view_name": "SECTION_A",
        },
    )
    assert cmd == (
        "nx_create_section_view TOP 20 30 120 30 200 100"
        " scale=2 arrow=0,-1 step=0,1 name=SECTION_A"
    )


def test_delete_view_wire_format():
    assert _command("nx_delete_view", {"view_id": "SECTION_A"}) == "nx_delete_view SECTION_A"


def test_list_assembly_wire_format():
    assert _command("nx_list_assembly", {}) == "nx_list_assembly"


def test_adapt_mass_properties_carries_per_body_rows():
    from nx_mcp.loader_bridge import _adapt

    adapted = _adapt(
        "nx_mass_properties",
        {},
        {
            "ok": True,
            "body_count": 2,
            "mass_kg": 1.0,
            "volume_mm3": 1000.0,
            "surface_area_mm2": 600.0,
            "density_kg_m3": 7830.0,
            "cog_x_mm": 1.0,
            "cog_y_mm": 2.0,
            "cog_z_mm": 3.0,
            "per_body": [
                {
                    "body_id": "Block",
                    "volume_mm3": 1000.0,
                    "surface_area_mm2": 600.0,
                    "mass_kg": 1.0,
                    "cog_x_mm": 1.0,
                    "cog_y_mm": 2.0,
                    "cog_z_mm": 3.0,
                }
            ],
            "result": "ok",
        },
    )
    assert adapted["bodies"][0]["body_id"] == "Block"
    assert adapted["bodies"][0]["center_of_gravity"] == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_adapt_list_assembly_maps_rows():
    from nx_mcp.loader_bridge import _adapt

    adapted = _adapt(
        "nx_list_assembly",
        {},
        {
            "ok": True,
            "count": 2,
            "rows": [
                {"level": 1, "name": "OP10", "display": "OP10", "part": "C:/m/op10.prt", "children": 1},
                {"level": 2, "name": "COMP_2", "display": "COMP_2", "part": "", "children": 0},
            ],
            "result": "assembly has 2 component instance(s)",
        },
    )
    assert adapted["count"] == 2
    assert adapted["rows"][0]["part_path"] == "C:/m/op10.prt"
    assert adapted["rows"][1]["display_name"] == "COMP_2"
