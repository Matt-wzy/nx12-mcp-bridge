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
