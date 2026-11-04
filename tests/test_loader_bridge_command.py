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


def test_id_symbol_wire_format():
    cmd = _command(
        "nx_create_id_symbol",
        {"symbol_type": "circle", "pos_x": 40.0, "pos_y": 50.0, "upper_text": "3"},
    )
    assert cmd == "nx_create_id_symbol circle 40 50 3"
    cmd2 = _command(
        "nx_create_id_symbol",
        {
            "symbol_type": "divcir",
            "pos_x": 40.0, "pos_y": 50.0,
            "size": 8.0,
            "upper_text": "OP 10",
            "lower_text": "A2",
        },
    )
    assert cmd2 == "nx_create_id_symbol divcir 40 50 size=8 lower=A2 OP 10"


def test_datum_symbol_wire_format():
    assert _command(
        "nx_create_datum_symbol", {"letter": "A", "pos_x": 100.0, "pos_y": 40.0}
    ) == "nx_create_datum_symbol A 100 40"
    assert _command(
        "nx_create_datum_symbol", {"letter": "b", "pos_x": 12.5, "pos_y": 0.0}
    ) == "nx_create_datum_symbol b 12.5 0"


def test_gdt_frame_wire_format():
    cmd = _command(
        "nx_create_gdt_frame",
        {
            "characteristic": "flatness",
            "tolerance": "0.02",
            "pos_x": 60.0,
            "pos_y": 30.0,
            "datum_primary": "A",
        },
    )
    assert cmd == "nx_create_gdt_frame flatness 0.02 60 30 d1=A"
    cmd2 = _command(
        "nx_create_gdt_frame",
        {
            "characteristic": "position",
            "tolerance": "0.05",
            "pos_x": 10.5,
            "pos_y": 0.0,
            "datum_primary": "A",
            "datum_secondary": "B",
            "datum_tertiary": "C",
            "zone_shape": "diameter",
            "material_modifier": "mmc",
        },
    )
    assert cmd2 == (
        "nx_create_gdt_frame position 0.05 10.5 0"
        " d1=A d2=B d3=C shape=diameter mat=mmc"
    )


def test_add_section_segment_wire_format():
    cmd = _command(
        "nx_add_section_segment",
        {"view_id": "SEC_A", "cut_x1": 210.0, "cut_y1": 80.0, "cut_x2": 210.0, "cut_y2": 220.0},
    )
    assert cmd == "nx_add_section_segment SEC_A 210 80 210 220"


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


def test_adapt_list_assembly_maps_qty_and_attributes():
    from nx_mcp.loader_bridge import _adapt

    adapted = _adapt(
        "nx_list_assembly",
        {},
        {
            "ok": True,
            "count": 1,
            "rows": [
                {
                    "level": 1,
                    "name": "BOLT_M8",
                    "display": "BOLT_M8",
                    "part": "C:/m/bolt.prt",
                    "children": 0,
                    "qty": 4,
                    "attrs": {"MATERIAL": "45钢", "图号": "JX-01", "NOTE": "42钢代用"},
                }
            ],
            "result": "assembly has 1 component instance(s)",
        },
    )
    row = adapted["rows"][0]
    assert row["qty"] == 4
    assert row["attributes"]["MATERIAL"] == "45钢"
    assert row["attributes"]["图号"] == "JX-01"
    # legacy rows without attrs/qty still work
    legacy = _adapt(
        "nx_list_assembly",
        {},
        {"ok": True, "count": 1, "rows": [{"level": 1, "name": "X"}]},
    )
    assert legacy["rows"][0]["qty"] == 1
    assert legacy["rows"][0]["attributes"] == {}


def test_bom_csv_carries_material_and_drawing_no(tmp_path):
    from nx_mcp.certified import _write_bom_csv

    out = str(tmp_path / "bom.csv")
    msg = _write_bom_csv(
        out,
        [
            {
                "level": 1,
                "name": "BOLT_M8",
                "display_name": "BOLT_M8",
                "part_path": "C:/m/bolt.prt",
                "children": 0,
                "qty": 4,
                "attributes": {"MATERIAL": "45钢", "DB_PART_NO": "JX-01"},
            },
            {
                "level": 1,
                "name": "PLATE",
                "display_name": "PLATE",
                "part_path": "C:/m/plate.prt",
                "children": 2,
                "attributes": {"材料": "Q235", "图号": "JX-02"},
            },
        ],
    )
    assert "2 BOM row(s)" in msg
    text = open(out, encoding="utf-8-sig").read()
    lines = text.strip().splitlines()
    assert lines[0] == "level,name,display_name,part_path,children,qty,material,drawing_no"
    assert "45钢" in lines[1] and "JX-01" in lines[1]
    assert "Q235" in lines[2] and "JX-02" in lines[2]
    # missing qty defaults to 1, empty attrs leave the cells blank
    assert lines[2].split(",")[5] == "1"


def test_adapt_list_attributes_structured():
    from nx_mcp.loader_bridge import _adapt

    adapted = _adapt(
        "nx_list_attributes",
        {},
        {
            "ok": True,
            "attributes": "3",
            "attrs": {"名称": "阶梯轴", "OP10_NAME": "车", "MATERIAL": "45钢"},
            "result": "found 3 attribute(s) [...]",
        },
    )
    assert adapted["count"] == 3
    assert adapted["attributes"]["名称"] == "阶梯轴"
    assert adapted["attributes"]["OP10_NAME"] == "车"


def _write_card(tmp_path, attributes, fmt="text"):
    from nx_mcp.certified import _write_process_card

    out = str(tmp_path / ("card." + ("csv" if fmt == "csv" else "txt")))
    message, warnings = _write_process_card(out, attributes, fmt)
    return out, open(out, encoding="utf-8-sig").read(), message, warnings


def test_process_card_text_renders_sorted_ops(tmp_path):
    attrs = {
        "名称": "阶梯轴",
        "图号": "JX-01",
        "材料": "45钢",
        "OP100_NAME": "磨",
        "OP100_CONTENT": "磨外圆至图样",
        "OP5_NAME": "备料",
        "OP5_CONTENT": "下料 φ50×250",
        "OP10_NAME": "车",
        "OP10_CONTENT": "车两端面",
        "OP10_MACHNE": "CA6140",  # typo -> warning
        "OP20_设备": "CA6140",
        "OP20_内容": "半精车各外圆",
    }
    out, text, message, warnings = _write_card(tmp_path, attrs, "text")
    assert "exported 4 operation(s)" in message
    # numeric op order, not string order (OP5 < OP10 < OP100)
    assert text.index("工序 5") < text.index("工序 10") < text.index("工序 100")
    assert "零件名称: 阶梯轴" in text and "图号: JX-01" in text and "材料: 45钢" in text
    assert "设备: CA6140" in text  # Chinese field key
    assert any("MACHNE" in w for w in warnings)
    # OP20 has 设备/内容 but no NAME -> warning
    assert any("OP20 missing 工序名称" in w for w in warnings)


def test_process_card_csv_columns(tmp_path):
    attrs = {
        "OP10_NAME": "铣",
        "OP10_CONTENT": "铣键槽",
        "OP10_MACHINE": "X52K",
        "OP10_装夹": "分度头",
        "OP10_刀具": "键槽铣刀",
        "OP10_切削参数": "n=475r/min",
        "OP10_单件工时": "15min",
    }
    out, text, message, warnings = _write_card(tmp_path, attrs, "csv")
    lines = text.strip().splitlines()
    assert lines[0] == "工序号,工序名称,工序内容,设备,装夹,刀具,切削参数,单件工时"
    assert lines[1] == "10,铣,铣键槽,X52K,分度头,键槽铣刀,n=475r/min,15min"
    assert warnings == []


def test_process_card_no_ops_errors(tmp_path):
    import json

    import pytest

    from nx_mcp.contracts import NXToolError
    from nx_mcp.certified import _write_process_card

    with pytest.raises(NXToolError) as exc:
        _write_process_card(str(tmp_path / "x.txt"), {"名称": "轴"}, "text")
    assert "NX_NO_PROCESS_OPS" in json.dumps(exc.value.as_dict(), ensure_ascii=False)
