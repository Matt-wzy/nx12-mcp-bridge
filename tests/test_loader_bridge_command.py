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
