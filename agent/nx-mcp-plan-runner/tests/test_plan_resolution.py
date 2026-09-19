# -*- coding: utf-8 -*-
"""Unit tests for nx-mcp-plan-runner (no NX involved).

Run:  python tests/test_plan_resolution.py
or:   pytest tests/test_plan_resolution.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner as R  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN_PLAN = os.path.join(PROJECT, "examples", "modeling-plan-example.json")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class ObjectRef:
    def __init__(self, oid):
        self.id = oid


def make_edge(index, **kw):
    base = {"index": index, "tag": index, "curve_type": "Linear", "start": [0, 0, 0],
            "end": [0, 0, 0], "midpoint": [0, 0, 0], "length": 12.0,
            "bbox_min": [0, 0, 0], "bbox_max": [0, 0, 0], "direction": "Z",
            "adjacent_faces": 2}
    base.update(kw)
    return base


def make_face(index, **kw):
    base = {"index": index, "tag": index, "face_type": "Planar",
            "centroid": [0, 0, 0], "area": 100.0, "normal": [0, 0, 1]}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# 1. symbol table
# --------------------------------------------------------------------------
def test_symbol_table():
    s = R.Symbols()
    s.bind("sketch_base", "SKT1")
    s.bind_selection("r6_edges", [9, 135, 151, 65])
    assert s.names["sketch_base"] == "SKT1"
    assert s.selection["r6_edges"] == [9, 135, 151, 65]


# --------------------------------------------------------------------------
# 2. result binding (producer op binds response fields to logical names)
# --------------------------------------------------------------------------
def test_result_binding_single():
    resp = {"body": ObjectRef("BODY_1")}
    s = R.Symbols()
    for n in ["body_base"]:
        s.bind(n, R._item_id(resp["body"]))
    assert s.names["body_base"] == "BODY_1"


def test_result_binding_multi():
    resp = {"objects": [ObjectRef("B2"), ObjectRef("B3"), ObjectRef("B4")]}
    names = ["body_boss2", "body_boss3", "body_boss4"]
    s = R.Symbols()
    vals = [R._item_id(v) for v in resp["objects"]]
    for n, v in zip(names, vals):
        s.bind(n, v)
    assert s.names["body_boss2"] == "B2"
    assert s.names["body_boss3"] == "B3"
    assert s.names["body_boss4"] == "B4"


def test_result_binding_mismatch_raises():
    # len mismatch between response list and names must be an error
    resp = {"objects": [ObjectRef("B2"), ObjectRef("B3")]}
    names = ["a", "b", "c"]
    s = R.Symbols()
    try:
        vals = [R._item_id(v) for v in resp["objects"]]
        if len(vals) != len(names):
            raise R.PlanError("mismatch")
        for n, v in zip(names, vals):
            s.bind(n, v)
        assert False, "should have raised"
    except R.PlanError:
        pass


# --------------------------------------------------------------------------
# 3. variable resolution
# --------------------------------------------------------------------------
def test_resolution_forms():
    s = R.Symbols()
    s.bind("body_main", "BODY_9")
    s.bind_selection("sel_49", [9, 135, 151, 65])
    s.bind_selection("sel_56", {"flange_top": [1], "boss_tops": [2, 3, 4, 5]})

    assert R.resolve_value("$body_main", s) == "BODY_9"
    assert R.resolve_value("$selection.sel_49", s) == [9, 135, 151, 65]
    assert R.resolve_value("$selection.sel_56.flange_top", s) == [1]
    assert R.resolve_value("body_main", s) == "BODY_9"  # bare name


def test_resolution_nested_args():
    s = R.Symbols()
    s.bind("body_main", "BODY_9")
    args = {
        "target_body_id": "$body_main",
        "tool_body_ids": ["$body_a", "$body_b"],
        "center": {"x": 1.0, "y": 2.0},
    }
    s.bind("body_a", "A1")
    s.bind("body_b", "B1")
    out = R.resolve_value(args, s)
    assert out["target_body_id"] == "BODY_9"
    assert out["tool_body_ids"] == ["A1", "B1"]
    assert out["center"] == {"x": 1.0, "y": 2.0}


def test_unresolved_raises():
    s = R.Symbols()
    try:
        R.resolve_value("$ghost", s)
        assert False, "should have raised"
    except R.PlanError:
        pass


def test_finalize_args():
    args = {"body_id": "B1", "remove_face_index": [3], "edge_indices": [1.0, 2.0],
            "tool_body_ids": ["B2", "B3"], "count": 4.0}
    out = R.finalize_args(args)
    assert out["remove_face_index"] == 3
    assert out["edge_indices"] == [1, 2]
    assert out["tool_body_ids"] == ["B2", "B3"]
    assert out["count"] == 4


# --------------------------------------------------------------------------
# 4. rectangle adapter (generic, no step numbers)
# --------------------------------------------------------------------------
def test_rectangle_adapter():
    args = {"sketch_id": "$sketch_base",
            "corner1": {"x": -90.0, "y": -60.0},
            "corner2": {"x": 90.0, "y": 60.0}}
    out = R.adapt_rectangle(args)
    assert out["corner1"] == {"x": 0.0, "y": 0.0}   # center
    assert out["corner2"] == {"x": 180.0, "y": 120.0}  # width/height


def test_rectangle_adapter_passthrough_without_corners():
    args = {"sketch_id": "$s", "center": {"x": 0, "y": 0}, "diameter": 10}
    assert R.adapt_rectangle(args) == args


# --------------------------------------------------------------------------
# 5. edge selection engine
# --------------------------------------------------------------------------
def test_edge_selection_corner_candidates():
    edges = [
        make_edge(0, bbox_min=[-90, -60, 0], bbox_max=[-90, -60, 12]),
        make_edge(1, bbox_min=[-90, 60, 0], bbox_max=[-90, 60, 12]),
        make_edge(2, bbox_min=[90, -60, 0], bbox_max=[90, -60, 12]),
        make_edge(3, bbox_min=[90, 60, 0], bbox_max=[90, 60, 12]),
        make_edge(4, bbox_min=[-90, -22, 0], bbox_max=[-90, -22, 12]),  # distractor
    ]
    crit = {"curve_type": "Linear", "direction": "Z", "length": {"value": 12.0, "tol": 0.05},
            "corners_xy": [[-90, -60], [-90, 60], [90, -60], [90, 60]],
            "bbox_z": {"min": 0.0, "max": 12.0}}
    sel = R.run_selection(edges, crit, "edges")
    assert sel["count"] == 4
    assert sorted(it["index"] for it in sel["items"]) == [0, 1, 2, 3]


def test_edge_selection_curve_type_list_and_length_tol():
    edges = [
        make_edge(0, curve_type="Circular", length=326.73),
        make_edge(1, curve_type="Elliptical", length=326.7),
        make_edge(2, curve_type="Linear", length=100.0),
    ]
    crit = {"curve_type": ["Circular", "Elliptical"], "length": {"value": 326.73, "tol": 1.5}}
    sel = R.run_selection(edges, crit, "edges")
    assert sel["count"] == 2


def test_edge_selection_midpoint_z_and_linear_only():
    edges = [
        make_edge(0, curve_type="Linear", midpoint=[0, 0, 56]),
        make_edge(1, curve_type="Circular", midpoint=[52, 0, 56]),
        make_edge(2, curve_type="Linear", midpoint=[0, 0, 10]),
    ]
    crit = {"linear_only": True, "midpoint_z": {"value": 56.0, "tol": 0.5}}
    sel = R.run_selection(edges, crit, "edges")
    assert [it["index"] for it in sel["items"]] == [0]


def test_edge_selection_bbox_x_containment():
    edges = [
        make_edge(0, bbox_min=[-115, -60, 0], bbox_max=[-115, -60, 12]),
        make_edge(1, bbox_min=[-115, 60, 0], bbox_max=[-115, 60, 12]),
        make_edge(2, bbox_min=[-120, -60, 0], bbox_max=[-120, -60, 12]),  # outside x
        make_edge(3, bbox_min=[-115, 80, 0], bbox_max=[-115, 80, 12]),    # outside y
    ]
    crit = {"bbox_x": [-115.0, 115.0], "bbox_y": [-60.0, 60.0]}
    sel = R.run_selection(edges, crit, "edges")
    assert [it["index"] for it in sel["items"]] == [0, 1]


def test_edge_selection_adjacent_faces_and_count():
    edges = [make_edge(0, adjacent_faces=2), make_edge(1, adjacent_faces=1)]
    sel = R.run_selection(edges, {"adjacent_faces": 2}, "edges")
    assert sel["count"] == 1


# --------------------------------------------------------------------------
# 6. face selection engine
# --------------------------------------------------------------------------
def test_face_selection_flat():
    faces = [
        make_face(0, face_type="Planar", centroid=[0, 0, 48], area=5541.77, normal=[0, 0, 1]),
        make_face(1, face_type="Cylindrical", centroid=[0, 0, 20]),
        make_face(2, face_type="Planar", centroid=[0, 0, 10], normal=[0, 0, 1]),
    ]
    crit = {"face_type": "Planar", "centroid": [0, 0, 48], "normal": [0, 0, 1],
            "area_approx_auxiliary": 5541.77}  # auxiliary key must be ignored
    sel = R.run_selection(faces, crit, "faces")
    assert sel["count"] == 1
    assert sel["items"][0]["index"] == 0


def test_face_selection_named_groups():
    faces = [
        make_face(0, face_type="Planar", centroid=[0, 0, 56], normal=[0, 0, 1]),
        make_face(1, face_type="Planar", centroid=[65, 0, 20], normal=[0, 0, 1]),
        make_face(2, face_type="Planar", centroid=[-65, 0, 20], normal=[0, 0, 1]),
        make_face(3, face_type="Cylindrical", centroid=[0, 0, 20]),
    ]
    crit = {
        "flange_top": {"face_type": "Planar", "centroid": [0, 0, 56]},
        "boss_tops": {"face_type": "Planar", "centroid_z": 20.0,
                      "centroid_radius": {"value": 65.0, "tol": 1.0}},
        "holes_reasonable": {"face_type": "Cylindrical"},
    }
    sel = R.run_selection(faces, crit, "faces")
    assert sel["count"] == 4
    assert sel["groups"]["flange_top"] == 1
    assert sel["groups"]["boss_tops"] == 2
    assert sel["groups"]["holes_reasonable"] == 1


def test_face_selection_centroid_radius_and_centroid_z():
    faces = [
        make_face(0, face_type="Planar", centroid=[52, 0, 56]),
        make_face(1, face_type="Planar", centroid=[10, 0, 56]),
    ]
    sel = R.run_selection(faces, {"centroid_radius": {"value": 52.0, "tol": 0.5}}, "faces")
    assert [it["index"] for it in sel["items"]] == [0]
    sel2 = R.run_selection(faces, {"centroid_z": {"value": 56.0, "tol": 0.5}}, "faces")
    assert sel2["count"] == 2


def test_centroid_radius_is_global_xy_distance_not_local_face_radius():
    # centroid_radius is sqrt(global_x^2 + global_y^2), not a cylinder/hole radius.
    face = make_face(0, face_type="Swept", centroid=[10, 10, 5])
    wrong = R.run_selection(
        [face], {"centroid_radius": {"value": 4.0, "tol": 0.5}}, "faces"
    )
    assert wrong["count"] == 0

    correct = R.run_selection(
        [face], {"centroid_radius": {"value": 14.142, "tol": 0.5}}, "faces"
    )
    assert [it["index"] for it in correct["items"]] == [0]


def test_hole_face_verification_by_explicit_centroid_groups():
    faces = [
        make_face(0, face_type="Swept", centroid=[10, 10, 5]),
        make_face(1, face_type="Swept", centroid=[90, 10, 5]),
        make_face(2, face_type="Cylindrical", centroid=[10, 50, 5]),
        make_face(3, face_type="Swept", centroid=[90, 50, 5]),
        make_face(4, face_type="Planar", centroid=[50, 30, 10]),
    ]
    crit = {
        "hole_1": {"face_type": ["Swept", "Cylindrical"], "centroid": [10, 10, 5]},
        "hole_2": {"face_type": ["Swept", "Cylindrical"], "centroid": [90, 10, 5]},
        "hole_3": {"face_type": ["Swept", "Cylindrical"], "centroid": [10, 50, 5]},
        "hole_4": {"face_type": ["Swept", "Cylindrical"], "centroid": [90, 50, 5]},
    }
    sel = R.run_selection(faces, crit, "faces")
    assert sel["count"] == 4
    assert R.check_expectation(
        {
            "hole_1_count": 1,
            "hole_2_count": 1,
            "hole_3_count": 1,
            "hole_4_count": 1,
        },
        sel,
        {},
    ) == []


# --------------------------------------------------------------------------
# 7. topology cache invalidation
# --------------------------------------------------------------------------
def test_topology_invalidation():
    topo = R.TopologyState()
    assert topo.edges_valid and topo.faces_valid
    topo.edges_cached = [1, 2]
    topo.faces_cached = [3]
    topo.invalidate()
    assert not topo.edges_valid and not topo.faces_valid
    assert topo.edges_cached == [] and topo.faces_cached == []


def test_topology_invalidation_does_not_auto_list():
    # a topology_changes op must NOT trigger list calls by itself
    calls = []

    async def fake_loop():
        plan = {"operations": [{"step": 1, "tool": "nx_unite",
                                "tool_args": {"target_body_id": "b", "tool_body_ids": ["c"]},
                                "topology_changes": True}]}
        s = R.Symbols()
        s.bind("b", "B1")
        s.bind("c", "B2")

        class T:
            async def call(self, tool, args):
                calls.append(tool)
                return {"message": "ok", "target": ObjectRef("B1")}

        await R.run_plan(plan, T())
        assert calls == ["nx_unite"], calls

    import asyncio
    asyncio.run(fake_loop())


# --------------------------------------------------------------------------
# 8. expectation checker
# --------------------------------------------------------------------------
def test_expectation_checks():
    sel = {"count": 4, "groups": {"flange_top": 1, "boss_tops": 4},
           "extents": {"x_min": -115.0, "x_max": 115.0, "y_min": -60.0,
                       "y_max": 60.0, "z_min": 0.0, "z_max": 56.0}}
    assert R.check_expectation({"count": 4}, sel, {}) == []
    assert R.check_expectation({"count": 5}, sel, {}) != []
    assert R.check_expectation({"flange_top_count": 1, "boss_tops_count": 4}, sel, {}) == []
    assert R.check_expectation({"boss_tops_count": 3}, sel, {}) != []
    assert R.check_expectation({"cylindrical_count_range": [4, 12]}, sel, {}) != []  # unknown group
    assert R.check_expectation({"x_min": -115.0, "z_max": 56.0, "tolerance_mm": 0.5}, sel, {}) == []
    assert R.check_expectation({"body_count": 1}, sel, {"objects": [ObjectRef("B1")]}) == []
    assert R.check_expectation({"body_count": 2}, sel, {"objects": [ObjectRef("B1")]}) != []
    # informational keys must be ignored
    assert R.check_expectation({"purpose": "check", "area_auxiliary": 123.0}, sel, {}) == []
    # done check on raw pipe result
    raw = {"result": "blend ok done=4"}
    assert R.check_expectation({"done": 4}, sel, raw) == []
    assert R.check_expectation({"done": 3}, sel, raw) != []


def test_extents_computation():
    edges = [
        make_edge(0, bbox_min=[-115, -60, 0], bbox_max=[-115, -60, 12]),
        make_edge(1, bbox_min=[115, 60, 48], bbox_max=[115, 60, 56]),
    ]
    ext = R.compute_extents(edges, "edges")
    assert ext["x_min"] == -115 and ext["x_max"] == 115
    assert ext["y_min"] == -60 and ext["y_max"] == 60
    assert ext["z_min"] == 0 and ext["z_max"] == 56


# --------------------------------------------------------------------------
# 9. build + static check on the real frozen plan (no NX)
# --------------------------------------------------------------------------
def test_frozen_plan_loads():
    assert os.path.isfile(FROZEN_PLAN)
    with open(FROZEN_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    assert plan["mode"] == "FAST"
    assert len(plan["operations"]) == 59


def test_build_executable_plan_resolves_all_references():
    with open(FROZEN_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    exe = R.build_executable_plan(plan)
    errs = R.check_plan(exe, executable=True)
    assert errs == [], errs
    # no natural-language placeholders left in any tool_args
    for op in exe["operations"]:
        for v in R._walk(op.get("tool_args") or {}):
            assert not (isinstance(v, str) and ("<" in v or "匹配" in v)), (op["step"], v)
    # selection consumers rewritten to machine references
    ta = {op["step"]: op.get("tool_args") or {} for op in exe["operations"]}
    assert ta[17]["remove_face_index"] == "$selection.sel_16"
    assert ta[50]["edge_indices"] == "$selection.sel_49"
    assert ta[52]["edge_indices"] == "$selection.sel_51"
    assert ta[54]["edge_indices"] == "$selection.sel_53"
    # key logical bindings present
    bindings = {}
    for op in exe["operations"]:
        for field, names in (op.get("result_bindings") or {}).items():
            ns = [names] if isinstance(names, str) else names
            for n in ns:
                bindings[n] = (op["step"], field)
    assert bindings.get("sketch_base") and bindings.get("body_base")
    assert bindings.get("body_main") is not None       # alias on the base producer
    assert bindings.get("body_cyl") and bindings.get("body_flange")
    assert bindings.get("body_boss1") and bindings.get("body_boss2")
    assert bindings.get("body_ribL1") and bindings.get("body_ribR1")
    # pattern copies bound via the "objects" response field
    assert bindings.get("body_boss2")[1] == "objects"
    assert bindings.get("body_ribR2")[1] == "objects"
    # mirror results bound via the bridge's "object" field (regression)
    assert bindings.get("body_earR") == (10, "object")
    assert bindings.get("body_ribR1") == (36, "object")
    # save retry declared
    save = [op for op in exe["operations"] if op["tool"] == "nx_save_part"]
    assert save and save[0].get("retry", {}).get("max", 0) >= 1


def test_frozen_check_rejects_executable_only_fields():
    plan = {"mode": "FAST", "operations": [{
        "step": 1,
        "tool": "nx_list_edges",
        "tool_args": {"body_id": "body_main"},
        "selection_criteria": {"linear_only": True},
        "result_bindings": {"edges": "selected_edges"},
        "selection_binding": "sel_1",
        "retry": {"max": 1, "if_error_contains": ["x"]},
        "topology_changes": False,
    }]}
    errs = R.check_plan(plan, executable=False)
    assert any("result_bindings" in e for e in errs)
    assert any("selection_binding" in e for e in errs)
    assert any("retry" in e for e in errs)


def test_frozen_check_rejects_dollar_references():
    plan = {"mode": "FAST", "operations": [{
        "step": 1,
        "tool": "nx_list_edges",
        "tool_args": {"body_id": "$body_main"},
        "topology_changes": False,
    }]}
    errs = R.check_plan(plan, executable=False)
    assert any("executable reference" in e for e in errs)


def test_frozen_check_rejects_bare_selection_consumer_name():
    plan = {"mode": "FAST", "operations": [{
        "step": 24,
        "tool": "nx_edge_blend",
        "tool_args": {
            "body_id": "body_main",
            "radius": 5,
            "edge_indices": "base_plate_vertical_edges",
        },
        "topology_changes": True,
    }]}
    errs = R.check_plan(plan, executable=False)
    assert any("frozen edge_indices string must be a <stepN" in e for e in errs)


def test_executable_check_rejects_unresolved_selection_consumer_name():
    plan = {"mode": "FAST", "operations": [{
        "step": 24,
        "tool": "nx_edge_blend",
        "tool_args": {
            "body_id": "$body_main",
            "radius": 5,
            "edge_indices": "base_plate_vertical_edges",
        },
        "topology_changes": True,
        "result_bindings": {"body": "body_dummy"},
    }]}
    errs = R.check_plan(plan, executable=True)
    assert any("executable edge_indices string must be a $selection reference" in e for e in errs)


def test_build_is_idempotent_shape():
    with open(FROZEN_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    exe1 = R.build_executable_plan(plan)
    exe2 = R.build_executable_plan(exe1)  # re-running build must not break
    errs = R.check_plan(exe2, executable=True)
    assert errs == [], errs


def test_param_validation_rejects_planner_fields_in_tool_args():
    # planner custom fields in tool_args must be flagged
    plan = {"operations": [{
        "step": 1, "tool": "nx_list_edges",
        "tool_args": {"body_id": "$body_main", "expected_count": 4},  # illegal
        "selection_criteria": {}, "expectation": {}}]}
    errs = R.check_plan(plan, executable=True)
    assert any("illegal param" in e for e in errs)


# --------------------------------------------------------------------------
# 10. preflight safety policy (no NX)
# --------------------------------------------------------------------------
def test_preflight_unrelated_part_open_blocked():
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\user_own.prt", planned_path=r"C:\work\test.prt",
        mode="benchmark", overwrite_allowed=True, dirty=False, runner_parts=())
    assert dec == "blocked"
    assert payload["reason"] == "unrelated_part_open"


def test_preflight_planned_benchmark_clean_allowed():
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="benchmark", overwrite_allowed=False, dirty=False, runner_parts=())
    assert dec == "allow"
    assert payload["state"] == "planned_clean"


def test_preflight_planned_clean_normal_allowed():
    dec, _ = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="normal", overwrite_allowed=False, dirty=False, runner_parts=())
    assert dec == "allow"


def test_preflight_planned_dirty_controlled_repair_allowed():
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="benchmark", overwrite_allowed=True, dirty=True, runner_parts=(),
        repair_authorized=True)
    assert dec == "allow"
    assert payload["state"] == "planned_dirty_controlled_repair"


def test_preflight_planned_dirty_benchmark_without_repair_blocked():
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="benchmark", overwrite_allowed=True, dirty=True, runner_parts=(),
        repair_authorized=False)
    assert dec == "blocked"
    assert payload["reason"] == "planned_part_dirty"


def test_preflight_planned_dirty_normal_blocked():
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="normal", overwrite_allowed=True, dirty=True, runner_parts=())
    assert dec == "blocked"
    assert payload["reason"] == "planned_part_dirty"


def test_preflight_planned_dirty_benchmark_without_overwrite_blocked():
    dec, _ = R.preflight_decision(
        active_path=r"C:\work\test.prt", planned_path=r"C:\work\test.prt",
        mode="benchmark", overwrite_allowed=False, dirty=True, runner_parts=())
    assert dec == "blocked"


def test_preflight_no_active_part_allowed():
    dec, payload = R.preflight_decision(
        active_path="", planned_path=r"C:\work\test.prt",
        mode="normal", overwrite_allowed=False, dirty=False, runner_parts=())
    assert dec == "allow"
    assert payload["state"] == "no_active_part"


def test_preflight_runner_own_test_part_allowed():
    # category B: a part the Runner itself created and recorded
    dec, payload = R.preflight_decision(
        active_path=r"C:\work\tmp_test.prt", planned_path=r"C:\work\test.prt",
        mode="normal", overwrite_allowed=False, dirty=False,
        runner_parts=(R._norm_path(r"C:\work\tmp_test.prt"),))
    assert dec == "allow"
    assert payload["state"] == "runner_test_part"


def test_preflight_path_normalization():
    # same file, different slash/case spelling must still match
    dec, _ = R.preflight_decision(
        active_path="c:/Work/Test.prt", planned_path=r"C:\work\test.prt",
        mode="normal", overwrite_allowed=False, dirty=False, runner_parts=())
    assert dec == "allow"


def test_run_history_and_runtime_dirty(tmp_path=None):
    import tempfile
    d = tmp_path or tempfile.mkdtemp()
    hp = os.path.join(str(d), "history.json")
    h = R.RunHistory(hp)
    # unknown provenance -> dirty (conservative)
    assert R.runtime_dirty(r"C:\work\test.prt", h) is True
    # Runner started a run, did not save -> dirty
    h.record_start(r"C:\work\test.prt", "benchmark", "plan.json")
    assert h.most_recent(r"C:\work\test.prt")["repair_attempt"] == 0
    assert R.runtime_dirty(r"C:\work\test.prt", h) is True
    # saved -> clean
    h.record_saved(r"C:\work\test.prt")
    assert R.runtime_dirty(r"C:\work\test.prt", h) is False
    # path normalization in history keys
    assert h.most_recent("c:/work/TEST.prt")["save_ok"] is True
    h.record_failed(r"C:\work\test.prt")
    assert R.runtime_dirty(r"C:\work\test.prt", h) is True
    # history persists across instances
    h2 = R.RunHistory(hp)
    assert h2.most_recent(r"C:\work\test.prt")["save_ok"] is False


def test_run_plan_mirror_object_binding():
    # regression: the bridge adapts nx_mirror results under "object"; a plan that
    # binds {"object": ...} must resolve in the very next step
    import asyncio
    plan = {"mode": "FAST", "operations": [
        {"step": 10, "tool": "nx_mirror", "tool_args": {"body_id": "src", "plane": "YZ"},
         "result_bindings": {"object": "body_earR"}, "topology_changes": True},
        {"step": 11, "tool": "nx_list_edges", "tool_args": {"body_id": "$body_earR"},
         "selection_criteria": {"linear_only": True}, "expectation": {"count": 1}}]}

    class T:
        async def call(self, tool, args):
            if tool == "nx_mirror":
                return {"status": "success", "object": ObjectRef("MIR1"), "message": "mirrored"}
            return {"status": "success",
                    "edges": [{"index": 0, "curve_type": "Linear", "length": 1.0,
                               "midpoint": [0, 0, 0], "bbox_min": [0, 0, 0],
                               "bbox_max": [1, 1, 1], "direction": "X",
                               "adjacent_faces": 2}],
                    "message": "1 edge"}

    rep = asyncio.run(R.run_plan(plan, T()))
    assert rep["status"] == "success", rep["steps"]




def test_build_existing_single_body_part_binding():
    frozen = {
        "mode": "FAST",
        "operations": [
            {
                "step": 1,
                "tool": "nx_open_part",
                "tool_args": {"path": "existing.prt"},
                "topology_changes": False,
            },
            {
                "step": 2,
                "tool": "nx_list_bodies",
                "tool_args": {},
                "expectation": {"body_count": 1},
                "topology_changes": False,
            },
            {
                "step": 3,
                "tool": "nx_hole",
                "tool_args": {
                    "body_id": "body_main",
                    "center": {"x": 0, "y": 0},
                    "diameter": 8,
                    "depth": 10,
                },
                "topology_changes": True,
            },
        ],
    }
    exe = R.build_executable_plan(frozen)
    assert exe["operations"][1]["result_bindings"] == {"objects": "body_main"}
    assert exe["operations"][2]["tool_args"]["body_id"] == "$body_main"
    assert R.check_plan(exe, executable=True) == []

# --------------------------------------------------------------------------
# controlled self-healing + runtime config
# --------------------------------------------------------------------------
def test_transport_ping_error_passthrough():
    class Bridge:
        last_ping_error = "status failed: inactive NX object"

        def ping(self):
            return False

    transport = R.NXTransport(workspace_root=".")
    transport._bridge = Bridge()
    assert transport.ping() is False
    assert transport.ping_error() == "status failed: inactive NX object"


def test_load_runtime_config_missing_is_empty():
    missing = os.path.join(PROJECT, "__missing_runtime_config__.json")
    assert R.load_runtime_config(missing) == {}


def test_repair_request_first_attempt_rejects_report():
    errs = R.repair_request_errors(
        0,
        "normal",
        False,
        r"C:\\ws\\part.prt",
        {
            "status": "failed",
            "failed_step": 4,
            "repair_attempt": 0,
            "planned_part": r"C:\\ws\\part.prt",
        },
    )
    assert any("only valid" in e for e in errs)


def test_repair_request_second_attempt_requires_benchmark_overwrite():
    prev = {
        "status": "failed",
        "failed_step": 4,
        "repair_attempt": 0,
        "planned_part": r"C:\\ws\\part.prt",
    }
    errs = R.repair_request_errors(1, "normal", False, r"C:\\ws\\part.prt", prev)
    assert any("benchmark" in e for e in errs)
    assert any("allow-overwrite" in e for e in errs)


def test_repair_request_second_attempt_accepts_same_failed_part():
    prev = {
        "status": "failed",
        "failed_step": 4,
        "repair_attempt": 0,
        "planned_part": r"C:\\ws\\part.prt",
    }
    assert R.repair_request_errors(
        1, "benchmark", True, r"C:\\ws\\part.prt", prev
    ) == []


def test_repair_request_rejects_second_repair():
    prev = {
        "status": "failed",
        "failed_step": 8,
        "repair_attempt": 1,
        "planned_part": r"C:\\ws\\part.prt",
    }
    errs = R.repair_request_errors(
        1, "benchmark", True, r"C:\\ws\\part.prt", prev
    )
    assert any("already consumed" in e for e in errs)


def test_repair_request_rejects_different_part():
    prev = {
        "status": "failed",
        "failed_step": 4,
        "repair_attempt": 0,
        "planned_part": r"C:\\ws\\part-a.prt",
    }
    errs = R.repair_request_errors(
        1, "benchmark", True, r"C:\\ws\\part-b.prt", prev
    )
    assert any("different part" in e for e in errs)


def test_run_history_persists_consumed_repair(tmp_path=None):
    import tempfile
    d = tmp_path or tempfile.mkdtemp()
    hp = os.path.join(str(d), "repair-history.json")
    h = R.RunHistory(hp)
    h.record_start(
        r"C:\work\repair.prt",
        "benchmark",
        "repair.json",
        repair_attempt=1,
    )
    assert h.most_recent(r"C:\work\repair.prt")["repair_attempt"] == 1
    h2 = R.RunHistory(hp)
    assert h2.most_recent(r"C:\work\repair.prt")["repair_attempt"] == 1


# --------------------------------------------------------------------------
# schema-only normalization / capability / repair lineage regressions
# --------------------------------------------------------------------------
def test_schema_normalizer_preserves_unresolved_slot_null():
    drawing = {
        "features": [{
            "id": "SLOT",
            "type": "slot",
            "bottom_z": None,
            "width": "2.0",
            "required_for_modeling": True,
        }],
        "source_ledger": [{
            "id": "SW",
            "semantic": "slot_width",
            "value": "2.0",
            "target": "feature:SLOT.width",
        }],
        "derived": [],
        "unresolved": [{
            "target": "feature:SLOT.bottom_z",
            "required_for_modeling": True,
        }],
        "dimension_conflicts": [],
    }
    before = R.drawing_semantic_projection(drawing)
    normalized, errors, _ = R.normalize_drawing_schema(drawing)
    assert errors == []
    assert normalized["features"][0]["bottom_z"] is None
    assert normalized["unresolved"] == drawing["unresolved"]
    assert not any(
        source.get("semantic") == "depth"
        for source in normalized["source_ledger"]
    )
    assert R.drawing_semantic_projection(normalized) == before


def test_schema_normalizer_does_not_wash_derived_relation_to_direct():
    drawing = {
        "features": [{"id": "H", "type": "through_hole", "count": "2"}],
        "source_ledger": [{
            "id": "SP",
            "semantic": "center_spacing",
            "value": "24",
            "between": ["feature:H.explicit_centers.0", "feature:H.explicit_centers.1"],
        }],
        "derived": [{
            "id": "D1",
            "target": "feature:H.explicit_centers.1",
            "value": "-10.3",
            "expr": {"op": "sub", "args": ["13.7", "24"]},
            "relation_refs": ["SP"],
        }],
        "unresolved": [],
        "dimension_conflicts": [],
    }
    normalized, errors, _ = R.normalize_drawing_schema(drawing)
    assert errors == []
    assert normalized["source_ledger"][0]["semantic"] == "center_spacing"
    assert normalized["source_ledger"][0].get("target") is None
    assert len(normalized["derived"]) == 1
    assert normalized["derived"][0]["target"] == "feature:H.explicit_centers.1"


def test_uncertain_warning_cannot_coexist_with_closed_geometry():
    drawing_path = os.path.abspath(
        os.path.join(PROJECT, "..", "..", "skills", "nx-agent", "examples", "example-output.json")
    )
    with open(drawing_path, encoding="utf-8") as handle:
        drawing = json.load(handle)
    drawing["warnings"] = ["slot depth 为推定值，需确认"]
    drawing["dimension_closure"] = {"status": "closed"}
    errors = R.check_drawing_json(drawing)
    assert any("warnings contain inferred/uncertain geometry" in error for error in errors)


def _thread_drawing(surrogate=None):
    feature = {
        "id": "T1",
        "type": "tapped_hole",
        "dimensions": {"thread_size": "M6", "depth": 12},
        "axis": "X",
        "position": {"center": [0, 0, 58]},
        "axis_range": [-20, -8],
        "count": 1,
        "required_for_modeling": True,
    }
    if surrogate is not None:
        feature["surrogate_geometry"] = surrogate
    return {"features": [feature]}


def test_bare_m6_uses_standard_pitch_then_parameterized_formula():
    recipes, errors = R.resolve_thread_surrogates(_thread_drawing())
    assert errors == []
    assert recipes == [{
        "feature_id": "T1",
        "kind": "thread_surrogate",
        "thread_spec": "M6",
        "nominal_diameter": 6.0,
        "pitch": 1.0,
        "pitch_source": "standard_default_coarse",
        "representation": "tap_drill",
        "surrogate_method": "nominal_minus_pitch",
        "surrogate_diameter": 5.0,
        "approximation": True,
        "reason": "real thread capability unavailable",
        "provenance": "parameterized_metric_resolver",
        "supplemented_fields": ["representation", "surrogate_diameter"],
    }]
    forbidden = {"axis", "center", "position", "depth", "range", "side", "count"}
    assert forbidden.isdisjoint(recipes[0])


def test_explicit_m6_pitch_is_parsed_and_calculated():
    pitched = _thread_drawing()
    pitched["features"][0]["dimensions"]["thread_size"] = "M6×1.0"
    recipe = R.resolve_thread_surrogates(pitched)[0][0]
    assert recipe["thread_spec"] == "M6x1"
    assert recipe["nominal_diameter"] == 6.0
    assert recipe["pitch"] == 1.0
    assert recipe["pitch_source"] == "drawing_explicit"
    assert recipe["surrogate_diameter"] == 5.0


def test_different_metric_thread_uses_same_parameterized_path():
    drawing = _thread_drawing()
    drawing["features"][0]["dimensions"]["thread_size"] = "M8x1.25"
    recipe = R.resolve_thread_surrogates(drawing)[0][0]
    assert recipe["nominal_diameter"] == 8.0
    assert recipe["pitch"] == 1.25
    assert recipe["surrogate_method"] == "nominal_minus_pitch"
    assert recipe["surrogate_diameter"] == 6.75
    drawing["features"][0]["dimensions"]["thread_size"] = "M8"
    bare_recipe = R.resolve_thread_surrogates(drawing)[0][0]
    assert bare_recipe["pitch_source"] == "standard_default_coarse"
    assert bare_recipe["pitch"] == 1.25
    assert bare_recipe["surrogate_diameter"] == 6.75


def test_runner_has_no_thread_size_to_final_diameter_mapping():
    import inspect
    source = inspect.getsource(R)
    assert "_THREAD_SURROGATE_RECIPES" not in source
    assert '"M6":' not in source
    assert R._PROJECT_SUPPORTED_METRIC_COARSE_PITCH_MM[6.0] == 1.0


def test_project_supported_coarse_pitch_subset_table_driven():
    expected = {
        1.0: 0.25, 1.2: 0.25, 1.4: 0.30, 1.6: 0.35, 1.8: 0.35,
        2.0: 0.40, 2.2: 0.45, 2.5: 0.45, 3.0: 0.50, 3.5: 0.60,
        4.0: 0.70, 4.5: 0.75, 5.0: 0.80, 6.0: 1.00, 7.0: 1.00,
        8.0: 1.25, 10.0: 1.50, 12.0: 1.75, 14.0: 2.00,
        16.0: 2.00, 18.0: 2.50, 20.0: 2.50, 22.0: 2.50,
        24.0: 3.00, 27.0: 3.00, 30.0: 3.50, 33.0: 3.50,
        36.0: 4.00, 39.0: 4.00, 42.0: 4.50, 45.0: 4.50,
        48.0: 5.00, 52.0: 5.00, 56.0: 5.50, 60.0: 5.50,
        64.0: 6.00,
    }
    assert R._PROJECT_SUPPORTED_METRIC_COARSE_PITCH_MM == expected
    assert len(expected) == 36
    for nominal, pitch in expected.items():
        parameters, error = R.resolve_metric_thread_parameters(f"M{nominal:g}")
        assert error is None
        assert parameters["nominal_diameter"] == nominal
        assert parameters["pitch"] == pitch
        assert parameters["pitch_source"] == "standard_default_coarse"
        assert parameters["nominal_diameter"] - parameters["pitch"] > 0
    assert R._METRIC_COARSE_PITCH_METADATA["scope"] == (
        "project_supported_metric_coarse_pitch_subset"
    )
    assert R._METRIC_COARSE_PITCH_METADATA["release_audit_required"] is True


def test_unsupported_bare_nominal_blocks_but_explicit_pitch_bypasses_subset():
    parameters, error = R.resolve_metric_thread_parameters("M6.3")
    assert parameters is None
    assert "no standard coarse-pitch metadata" in error
    explicit, explicit_error = R.resolve_metric_thread_parameters("M6.3x0.7")
    assert explicit_error is None
    assert explicit["nominal_diameter"] == 6.3
    assert explicit["pitch"] == 0.7
    assert explicit["pitch_source"] == "drawing_explicit"


def test_explicit_approved_thread_surrogate_is_allowed():
    drawing = _thread_drawing({
        "representation": "input_cylindrical_surrogate",
        "diameter": 5.0,
        "depth": 12,
        "axis_range": [-20, -8],
        "approved_for_delivery": True,
    })
    recipes, errors = R.resolve_thread_surrogates(drawing)
    assert errors == []
    assert recipes[0]["provenance"] == "drawing_input"
    assert "depth" not in recipes[0]
    assert "axis_range" not in recipes[0]


def test_explicit_surrogate_cannot_change_confirmed_thread_geometry():
    drawing = _thread_drawing({
        "representation": "input_cylindrical_surrogate",
        "diameter": 5.0,
        "depth": 11,
        "axis_range": [-20, -9],
        "approved_for_delivery": True,
    })
    recipes, errors = R.resolve_thread_surrogates(drawing)
    assert recipes == []
    assert any("changes depth" in error for error in errors)


def _thread_frozen_plan(
    recipes,
    geometries,
    include_provenance=True,
    diameter=5.0,
    plane="YZ",
    center=None,
    depth=12,
    start_offset=-20,
    reverse=False,
    omit_start_offset=False,
):
    center = center or {"x": 0, "y": 58}
    subtract_args = {
        "sketch_id": "sketch_thread",
        "distance": depth,
        "start_offset": start_offset,
        "reverse": reverse,
        "operation": "subtract",
        "target_body_id": "body_main",
    }
    if omit_start_offset:
        subtract_args.pop("start_offset")
    subtract = {
        "step": 9,
        "tool": "nx_extrude",
        "target": "body_main",
        "tool_args": subtract_args,
        "topology_changes": True,
    }
    if include_provenance:
        subtract["thread_surrogate_use"] = {
            "feature_id": "T1",
            "recipe_sha256": R._json_sha256(recipes[0]),
            "drawing_geometry_sha256": geometries[0]["drawing_geometry_sha256"],
            "owner_feature_id": geometries[0]["owner_feature_id"],
        }
        if "side" in geometries[0]:
            subtract["thread_surrogate_use"]["side"] = geometries[0]["side"]
    return {
        "mode": "FAST",
        "thread_surrogates": recipes,
        "operations": [
            {
                "step": 1, "tool": "nx_create_part",
                "tool_args": {"path": "part.prt", "units": "mm"},
                "topology_changes": False,
            },
            {
                "step": 2, "tool": "nx_create_sketch", "target": "-",
                "tool_args": {"plane": "YZ"}, "topology_changes": False,
            },
            {
                "step": 3, "tool": "nx_sketch_rectangle", "target": "sketch_base",
                "tool_args": {
                    "sketch_id": "sketch_base",
                    "corner1": {"x": -10, "y": -10},
                    "corner2": {"x": 10, "y": 10},
                },
                "topology_changes": False,
            },
            {
                "step": 4, "tool": "nx_finish_sketch", "target": "sketch_base",
                "tool_args": {"sketch_id": "sketch_base"},
                "topology_changes": False,
            },
            {
                "step": 5, "tool": "nx_extrude", "target": "sketch_base",
                "tool_args": {
                    "sketch_id": "sketch_base", "distance": 40,
                    "start_offset": -20, "reverse": False, "operation": "create",
                },
                "topology_changes": True,
            },
            {
                "step": 6, "tool": "nx_create_sketch", "target": "-",
                "tool_args": {"plane": plane}, "topology_changes": False,
            },
            {
                "step": 7, "tool": "nx_sketch_circle", "target": "sketch_thread",
                "tool_args": {
                    "sketch_id": "sketch_thread", "center": center,
                    "diameter": diameter,
                },
                "topology_changes": False,
            },
            {
                "step": 8, "tool": "nx_finish_sketch", "target": "sketch_thread",
                "tool_args": {"sketch_id": "sketch_thread"},
                "topology_changes": False,
            },
            subtract,
        ],
    }


def _thread_gate_errors(drawing, **plan_overrides):
    recipes, recipe_errors = R.resolve_thread_surrogates(drawing)
    geometries, geometry_errors = R.resolve_thread_drawing_geometries(drawing)
    if geometry_errors:
        return recipe_errors + geometry_errors
    plan = _thread_frozen_plan(recipes, geometries, **plan_overrides)
    return recipe_errors + R.thread_surrogate_plan_errors(plan, recipes, geometries)


def _freeze_test_task(drawing, drawing_path):
    task_path, state = R.create_mode_b_task(drawing, drawing_path)
    R.freeze_mode_b_gate_a(state, task_path, drawing)
    return task_path


def test_thread_gate_b_accepts_equivalent_axis_x_geometry():
    assert _thread_gate_errors(_thread_drawing()) == []


def test_thread_gate_b_rejects_axis_change():
    errors = _thread_gate_errors(_thread_drawing(), plane="XZ")
    assert any("changes axis" in error for error in errors)


def test_thread_gate_b_rejects_center_change():
    errors = _thread_gate_errors(
        _thread_drawing(), center={"x": 1, "y": 58}
    )
    assert any("changes center/position" in error for error in errors)


def test_thread_gate_b_rejects_depth_change():
    errors = _thread_gate_errors(_thread_drawing(), depth=11)
    assert any("changes depth" in error for error in errors)


def test_thread_gate_b_rejects_range_change_and_loader_default():
    changed = _thread_gate_errors(_thread_drawing(), start_offset=-19)
    assert any("changes axial range" in error for error in changed)
    omitted = _thread_gate_errors(_thread_drawing(), omit_start_offset=True)
    assert any("defaults are forbidden" in error for error in omitted)


def test_thread_gate_b_rejects_count_change():
    drawing = _thread_drawing()
    feature = drawing["features"][0]
    feature["count"] = 2
    feature["explicit_centers"] = [[0, 0, 58], [0, 4, 58]]
    errors = _thread_gate_errors(drawing)
    assert any("operation count differs" in error for error in errors)


def test_thread_gate_b_rejects_identity_ownership_and_side_change():
    drawing = _thread_drawing()
    drawing["features"][0]["side"] = "+X"
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    plan = _thread_frozen_plan(recipes, geometries)
    use = plan["operations"][-1]["thread_surrogate_use"]
    use["owner_feature_id"] = "OTHER"
    errors = R.thread_surrogate_plan_errors(plan, recipes, geometries)
    assert any("changes feature ownership" in error for error in errors)
    use["owner_feature_id"] = geometries[0]["owner_feature_id"]
    use["side"] = "-X"
    errors = R.thread_surrogate_plan_errors(plan, recipes, geometries)
    assert any("changes side semantics" in error for error in errors)
    use["feature_id"] = "NOT_IN_DRAWING"
    errors = R.thread_surrogate_plan_errors(plan, recipes, geometries)
    assert any("feature identity is absent" in error for error in errors)


def test_thread_gate_b_blocks_unresolved_or_missing_geometry():
    unresolved = _thread_drawing()
    unresolved["unresolved"] = [{
        "target": "feature:T1.axis_range",
        "required_for_modeling": True,
    }]
    assert any("blocking unresolved" in error
               for error in _thread_gate_errors(unresolved))
    for field in ("axis", "position", "axis_range", "count"):
        drawing = _thread_drawing()
        del drawing["features"][0][field]
        assert _thread_gate_errors(drawing), field
    drawing = _thread_drawing()
    del drawing["features"][0]["dimensions"]["depth"]
    assert _thread_gate_errors(drawing)


def test_gate_b_blocks_planner_generated_m6_tap_drill(tmp_path=None):
    import tempfile
    from types import SimpleNamespace

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "drawing.json")
    frozen_path = os.path.join(directory, "frozen.json")
    executable_path = os.path.join(directory, "executable.json")
    drawing = _thread_drawing()
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    frozen = _thread_frozen_plan(recipes, geometries, include_provenance=False)
    with open(frozen_path, "w", encoding="utf-8") as handle:
        json.dump(frozen, handle)
    code, result = _capture_json_command(
        R._cmd_build,
        SimpleNamespace(
            plan=frozen_path, out=executable_path, drawing=drawing_path,
            task_root=_freeze_test_task(drawing, drawing_path),
        ),
    )
    assert code == 1
    assert not os.path.exists(executable_path)
    assert any("exact machine recipe provenance" in error
               for error in result["capability_errors"])


def test_gate_b_builds_m6_parameterized_surrogate_with_machine_provenance(tmp_path=None):
    import tempfile
    from types import SimpleNamespace

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "drawing.json")
    frozen_path = os.path.join(directory, "frozen.json")
    executable_path = os.path.join(directory, "executable.json")
    drawing = _thread_drawing()
    recipes, errors = R.resolve_thread_surrogates(drawing)
    geometries, geometry_errors = R.resolve_thread_drawing_geometries(drawing)
    assert errors == []
    assert geometry_errors == []
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    with open(frozen_path, "w", encoding="utf-8") as handle:
        json.dump(_thread_frozen_plan(recipes, geometries), handle)
    code, result = _capture_json_command(
        R._cmd_build,
        SimpleNamespace(
            plan=frozen_path, out=executable_path, drawing=drawing_path,
            task_root=_freeze_test_task(drawing, drawing_path),
        ),
    )
    assert code == 0, result
    with open(executable_path, encoding="utf-8") as handle:
        executable = json.load(handle)
    assert executable["design_guard"]["thread_surrogates"] == recipes
    approximations = R.thread_approximations_from_guard(executable["design_guard"])
    assert approximations == [{
        "feature_id": "T1",
        "kind": "thread_surrogate",
        "thread_spec": "M6",
        "nominal_diameter": 6.0,
        "pitch": 1.0,
        "pitch_source": "standard_default_coarse",
        "representation": "tap_drill",
        "surrogate_method": "nominal_minus_pitch",
        "surrogate_diameter": 5.0,
        "approximation": True,
        "reason": "real thread capability unavailable",
        "provenance": "parameterized_metric_resolver",
    }]


def test_unknown_thread_without_recipe_is_blocked():
    drawing = _thread_drawing()
    drawing["features"][0]["dimensions"]["thread_size"] = "UNC 1/4-20"
    recipes, errors = R.resolve_thread_surrogates(drawing)
    assert recipes == []
    assert any("cannot use deterministic surrogate" in error for error in errors)


def test_gate_b_rejects_planner_diameter_different_from_resolver(tmp_path=None):
    import tempfile
    from types import SimpleNamespace

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "drawing.json")
    frozen_path = os.path.join(directory, "frozen.json")
    executable_path = os.path.join(directory, "executable.json")
    drawing = _thread_drawing()
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    with open(frozen_path, "w", encoding="utf-8") as handle:
        json.dump(_thread_frozen_plan(recipes, geometries, diameter=4.9), handle)
    code, result = _capture_json_command(
        R._cmd_build,
        SimpleNamespace(plan=frozen_path, out=executable_path, drawing=drawing_path),
    )
    assert code == 1
    assert any("diameter differs from resolver" in error
               for error in result["capability_errors"])


def test_thread_recipe_cannot_supply_missing_geometry():
    drawing = _thread_drawing()
    feature = drawing["features"][0]
    del feature["axis"]
    del feature["position"]
    del feature["dimensions"]["depth"]
    recipes, errors = R.resolve_thread_surrogates(drawing)
    assert errors == []
    assert set(recipes[0]["supplemented_fields"]) == {
        "representation", "surrogate_diameter"
    }
    assert all(key not in recipes[0] for key in ("axis", "center", "depth", "range"))


def test_gate_b_normal_drawing_path_still_builds_with_lineage(tmp_path=None):
    import tempfile
    from types import SimpleNamespace

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    source_drawing = os.path.abspath(
        os.path.join(PROJECT, "..", "..", "skills", "nx-agent", "examples", "example-output.json")
    )
    drawing_path = os.path.join(directory, "drawing.json")
    with open(source_drawing, encoding="utf-8") as handle:
        drawing = json.load(handle)
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    task_root = _freeze_test_task(drawing, drawing_path)
    executable_path = os.path.join(directory, "executable.json")
    code, result = _capture_json_command(
        R._cmd_build,
        SimpleNamespace(
            plan=FROZEN_PLAN, out=executable_path, drawing=drawing_path,
            task_root=task_root,
        ),
    )
    assert code == 0, result
    with open(executable_path, encoding="utf-8") as handle:
        executable = json.load(handle)
    assert executable["design_guard"]["source_drawing"] == drawing_path
    assert executable["design_guard"]["drawing_semantics_sha256"]
    assert executable["design_guard"]["plan_geometry_sha256"]


def test_failed_design_lineage_cannot_escape_by_output_rename(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    history = R.RunHistory(os.path.join(directory, "history.json"))
    plan = {"operations": [{
        "step": 1,
        "tool": "nx_extrude",
        "tool_args": {"distance": 40, "start_offset": -20},
        "topology_changes": True,
    }]}
    guard = R.make_design_guard({"features": []}, os.path.join(directory, "drawing.json"), plan)
    history.record_start(
        os.path.join(directory, "part.prt"), "normal", "plan.json", design_guard=guard
    )
    history.record_failed(os.path.join(directory, "part.prt"))
    errors = R.normal_run_lineage_errors(0, guard, history)
    assert any("requires --repair-attempt 1" in error for error in errors)
    assert history.most_recent(os.path.join(directory, "part_v2.prt")) is None


def test_repair_rejects_geometry_bearing_range_change():
    drawing = {"features": []}
    plan1 = {"operations": [{
        "step": 1, "tool": "nx_extrude",
        "tool_args": {"distance": 40, "start_offset": -20},
        "topology_changes": True,
    }]}
    plan2 = {"operations": [{
        "step": 1, "tool": "nx_extrude",
        "tool_args": {"distance": 19, "start_offset": 1},
        "topology_changes": True,
    }]}
    guard1 = R.make_design_guard(drawing, r"C:\ws\drawing.json", plan1)
    guard2 = R.make_design_guard(drawing, r"C:\ws\drawing.json", plan2)
    previous = {
        "status": "failed",
        "failed_step": 1,
        "repair_attempt": 0,
        "planned_part": r"C:\ws\part.prt",
        "design_guard": guard1,
    }
    errors = R.repair_request_errors(
        1, "benchmark", True, r"C:\ws\part.prt", previous, guard2
    )
    assert any("canonical executed geometry" in error for error in errors)


def test_mode_b_schema_retry_rejects_slot_null_to_number(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "slot.json")
    drawing = {
        "features": [{"id": "S", "type": "slot", "bottom": None}],
        "unresolved": [{
            "target": "feature:S.bottom", "required_for_modeling": True,
        }],
    }
    task_path, state = R.create_mode_b_task(drawing, drawing_path)
    changed = json.loads(json.dumps(drawing))
    changed["features"][0]["bottom"] = 40
    assert any("schema repair must be semantics-preserving" in error
               for error in R.mode_b_task_drawing_errors(state, changed))
    assert R.load_mode_b_task(task_path)["initial_geometry_semantics"] == (
        R.drawing_semantic_projection(drawing)
    )


def test_mode_b_schema_retry_rejects_added_thread_interval(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "thread.json")
    drawing = _thread_drawing()
    del drawing["features"][0]["axis_range"]
    drawing["unresolved"] = [{
        "target": "feature:T1.axis_range", "required_for_modeling": True,
    }]
    _, state = R.create_mode_b_task(drawing, drawing_path)
    changed = json.loads(json.dumps(drawing))
    changed["features"][0]["axis_range"] = [54, 66]
    changed["unresolved"] = []
    assert R.mode_b_task_drawing_errors(state, changed)


def test_mode_b_schema_retry_allows_equivalent_shape_only_change(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "shape.json")
    drawing = {
        "features": {"H": {
            "type": "hole",
            "dimension": [{"name": "diameter", "value": "6.0"}],
            "center": [1, 2, 3],
        }},
        "source_ledger": {},
    }
    _, state = R.create_mode_b_task(drawing, drawing_path)
    equivalent = {
        "features": [{
            "id": "H", "type": "hole", "dimensions": {"diameter": 6},
            "position": {"center": [1, 2, 3]},
        }],
        "source_ledger": [],
    }
    assert R.mode_b_task_drawing_errors(state, equivalent) == []


def test_gate_a_frozen_geometry_rejects_revalidate_and_build_mutation(tmp_path=None):
    import tempfile
    from types import SimpleNamespace
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing_path = os.path.join(directory, "drawing.json")
    frozen_path = os.path.join(directory, "frozen.json")
    executable_path = os.path.join(directory, "executable.json")
    drawing = _thread_drawing()
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    with open(frozen_path, "w", encoding="utf-8") as handle:
        json.dump(_thread_frozen_plan(recipes, geometries), handle)
    task_root = _freeze_test_task(drawing, drawing_path)
    drawing["features"][0]["dimensions"]["depth"] = 13
    with open(drawing_path, "w", encoding="utf-8") as handle:
        json.dump(drawing, handle)
    validate_code, validate_result = _capture_json_command(
        R._cmd_validate_drawing,
        SimpleNamespace(drawing=drawing_path, new_task=False, task_root=task_root),
    )
    assert validate_code == 1
    assert any("semantics-preserving" in error for error in validate_result["errors"])
    build_code, build_result = _capture_json_command(
        R._cmd_build,
        SimpleNamespace(
            plan=frozen_path, out=executable_path, drawing=drawing_path,
            task_root=task_root,
        ),
    )
    assert build_code == 1
    assert any("immutable Gate A baseline" in error
               for error in build_result["capability_errors"])


def test_mode_b_task_lineage_ignores_drawing_output_and_history_paths(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing = _thread_drawing()
    original = os.path.join(directory, "drawing.json")
    task_path, state = R.create_mode_b_task(drawing, original)
    R.freeze_mode_b_gate_a(state, task_path, drawing)
    R.record_mode_b_attempt(task_path, state, 0, "failed", {
        "failed_step": 26, "planned_part": os.path.join(directory, "part.prt"),
    })
    reloaded = R.load_mode_b_task(task_path)
    assert R.mode_b_task_attempt_errors(reloaded, 0)
    guard = R.make_design_guard(
        drawing, os.path.join(directory, "renamed-drawing.json"), {"operations": []},
        task_path=task_path, task_state=reloaded,
    )
    for history_name in ("history-a.json", "history-b.json"):
        history = R.RunHistory(os.path.join(directory, history_name))
        history.record_start(
            os.path.join(directory, "renamed-output.prt"), "benchmark", "plan.json",
            design_guard=guard,
        )
        assert history.most_recent_design(guard)["mode_b_task_id"] == (
            state["mode_b_task_id"]
        )


def test_run_cannot_escape_failed_task_with_fresh_history_path(tmp_path=None):
    import asyncio
    import contextlib
    import io
    import tempfile
    from types import SimpleNamespace
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing = _thread_drawing()
    drawing_path = os.path.join(directory, "renamed-drawing.json")
    task_path, state = R.create_mode_b_task(drawing, drawing_path)
    R.freeze_mode_b_gate_a(state, task_path, drawing)
    plan = {"operations": [{
        "step": 1, "tool": "nx_create_part",
        "tool_args": {"path": os.path.join(directory, "renamed-output.prt")},
        "result_bindings": {"part": "part"}, "topology_changes": False,
    }]}
    plan["design_guard"] = R.make_design_guard(
        drawing, drawing_path, plan, task_path=task_path, task_state=state,
    )
    R.record_mode_b_attempt(task_path, state, 0, "failed", {"failed_step": 1})
    plan_path = os.path.join(directory, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = asyncio.run(R._cmd_run(SimpleNamespace(
            plan=plan_path,
            repair_attempt=0,
            history=os.path.join(directory, "fresh-history.json"),
        )))
    result = json.loads(output.getvalue())
    assert code == 1
    assert result["status"] == "repair_precheck_blocked"
    assert any("attempt-0 modeling failure" in error for error in result["errors"])


def test_new_mode_b_task_allows_fresh_attempt_zero_for_same_drawing(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing = _thread_drawing()
    path = os.path.join(directory, "drawing.json")
    first_path, first = R.create_mode_b_task(drawing, path)
    R.record_mode_b_attempt(first_path, first, 0, "failed")
    second_path, second = R.create_mode_b_task(drawing, path)
    assert first["mode_b_task_id"] != second["mode_b_task_id"]
    assert first_path != second_path
    assert R.mode_b_task_attempt_errors(second, 0) == []


def test_axial_range_order_has_same_canonical_design_hash():
    first = _thread_drawing()
    second = _thread_drawing()
    first["features"][0]["axis_range"] = [66, 54]
    second["features"][0]["axis_range"] = [54, 66]
    first_projection = R.drawing_semantic_projection(first)
    second_projection = R.drawing_semantic_projection(second)
    assert first_projection == second_projection
    assert R._json_sha256(first_projection) == R._json_sha256(second_projection)
    first_geometry, _ = R.resolve_thread_drawing_geometries(first)
    second_geometry, _ = R.resolve_thread_drawing_geometries(second)
    assert first_geometry[0]["drawing_geometry_sha256"] == (
        second_geometry[0]["drawing_geometry_sha256"]
    )


def test_reverse_and_forward_thread_execution_are_canonically_equivalent(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing = _thread_drawing()
    drawing["features"][0]["axis_range"] = [54, 66]
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    reverse_plan = _thread_frozen_plan(
        recipes, geometries, start_offset=66, reverse=True,
    )
    forward_plan = _thread_frozen_plan(
        recipes, geometries, start_offset=54, reverse=False,
    )
    reverse_projection, reverse_errors = R.canonical_executed_geometry_projection(
        reverse_plan, recipes, geometries
    )
    forward_projection, forward_errors = R.canonical_executed_geometry_projection(
        forward_plan, recipes, geometries
    )
    assert reverse_errors == []
    assert forward_errors == []
    assert reverse_projection == forward_projection
    task_path, state = R.create_mode_b_task(
        drawing, os.path.join(directory, "drawing.json")
    )
    R.freeze_mode_b_gate_a(state, task_path, drawing)
    first_guard = R.make_design_guard(
        drawing, "drawing.json", reverse_plan, recipes, geometries,
        task_path=task_path, task_state=state,
    )
    repair_guard = R.make_design_guard(
        drawing, "drawing.json", forward_plan, recipes, geometries,
        task_path=task_path, task_state=state,
    )
    previous = {
        "status": "failed", "failed_step": 9, "repair_attempt": 0,
        "planned_part": "part.prt", "design_guard": first_guard,
    }
    assert R.repair_request_errors(
        1, "benchmark", True, "part.prt", previous, repair_guard
    ) == []


def test_repair_rejects_canonical_interval_change():
    drawing = _thread_drawing()
    drawing["features"][0]["axis_range"] = [54, 66]
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    baseline = _thread_frozen_plan(recipes, geometries, start_offset=54)
    changed = _thread_frozen_plan(recipes, geometries, start_offset=54, depth=16)
    guard1 = R.make_design_guard(drawing, "drawing.json", baseline, recipes, geometries)
    guard2 = R.make_design_guard(drawing, "drawing.json", changed, recipes, geometries)
    previous = {
        "status": "failed", "failed_step": 9, "repair_attempt": 0,
        "planned_part": "part.prt", "design_guard": guard1,
    }
    errors = R.repair_request_errors(
        1, "benchmark", True, "part.prt", previous, guard2
    )
    assert any("canonical executed geometry" in error for error in errors)


def test_thread_hole_and_sketch_subtract_are_canonically_equivalent(tmp_path=None):
    import tempfile
    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    drawing = _thread_drawing()
    feature = drawing["features"][0]
    feature["axis"] = "Z"
    feature["position"]["center"] = [0, 58, 54]
    feature["axis_range"] = [54, 66]
    recipes, _ = R.resolve_thread_surrogates(drawing)
    geometries, _ = R.resolve_thread_drawing_geometries(drawing)
    sketch_plan = _thread_frozen_plan(
        recipes, geometries, plane="XY", center={"x": 0, "y": 58},
        start_offset=54,
    )
    hole_plan = {
        "mode": "FAST",
        "thread_surrogates": recipes,
        "operations": json.loads(json.dumps(sketch_plan["operations"][:5])),
    }
    use = json.loads(json.dumps(sketch_plan["operations"][-1]["thread_surrogate_use"]))
    hole_plan["operations"].append({
        "step": 6,
        "tool": "nx_hole",
        "tool_args": {
            "body_id": "body_main", "center": {"x": 0, "y": 58},
            "diameter": recipes[0]["surrogate_diameter"], "depth": 12,
            "start_offset": 54,
        },
        "thread_surrogate_use": use,
        "topology_changes": True,
    })
    sketch_projection, sketch_errors = R.canonical_executed_geometry_projection(
        sketch_plan, recipes, geometries
    )
    hole_projection, hole_errors = R.canonical_executed_geometry_projection(
        hole_plan, recipes, geometries
    )
    assert sketch_errors == [], sketch_errors
    assert hole_errors == [], hole_errors
    assert sketch_projection == hole_projection, (sketch_projection, hole_projection)
    task_path, state = R.create_mode_b_task(
        drawing, os.path.join(directory, "drawing.json")
    )
    R.freeze_mode_b_gate_a(state, task_path, drawing)
    sketch_guard = R.make_design_guard(
        drawing, "drawing.json", sketch_plan, recipes, geometries,
        task_path=task_path, task_state=state,
    )
    hole_guard = R.make_design_guard(
        drawing, "drawing.json", hole_plan, recipes, geometries,
        task_path=task_path, task_state=state,
    )
    previous = {
        "status": "failed", "failed_step": 9, "repair_attempt": 0,
        "planned_part": "part.prt", "design_guard": sketch_guard,
    }
    assert R.repair_request_errors(
        1, "benchmark", True, "part.prt", previous, hole_guard
    ) == []


def test_plan_geometry_tamper_hash_still_detects_direct_edit():
    plan = {"operations": [{
        "step": 1, "tool": "nx_extrude",
        "tool_args": {"distance": 12, "start_offset": 0},
        "topology_changes": True,
    }]}
    guard = R.make_design_guard({"features": []}, "drawing.json", plan)
    edited = json.loads(json.dumps(plan))
    edited["operations"][0]["tool_args"]["distance"] = 13
    assert R._json_sha256(R.plan_geometry_projection(edited)) != (
        guard["plan_geometry_sha256"]
    )


def _capture_json_command(command, args):
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = command(args)
    return exit_code, json.loads(output.getvalue())


def test_validate_build_check_return_independent_timing(tmp_path=None):
    import tempfile
    from types import SimpleNamespace

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    source_drawing = os.path.abspath(
        os.path.join(PROJECT, "..", "..", "skills", "nx-agent", "examples", "example-output.json")
    )
    drawing = os.path.join(directory, "timing-drawing.json")
    with open(source_drawing, encoding="utf-8") as source:
        with open(drawing, "w", encoding="utf-8") as target:
            target.write(source.read())
    validate_results = []
    task_root = None
    for index in range(2):
        exit_code, result = _capture_json_command(
            R._cmd_validate_drawing,
            SimpleNamespace(
                drawing=drawing,
                new_task=index == 0,
                task_root=task_root,
            ),
        )
        assert exit_code == 0
        task_root = result["mode_b_task"]["task_root"]
        validate_results.append(result["timing"])
    for timing in validate_results:
        assert timing["stage"] == "A4_VALIDATE"
        assert timing["elapsed_ms"] is None or timing["elapsed_ms"] >= 0
        assert timing["input_file"]["file_mtime_utc"] is not None
        assert timing["input_file"]["first_write_reliable"] is False

    executable = os.path.join(directory, "timing-executable.json")
    build_code, build_result = _capture_json_command(
        R._cmd_build, SimpleNamespace(plan=FROZEN_PLAN, out=executable)
    )
    assert build_code == 0
    assert build_result["timing"]["stage"] == "B3_BUILD"
    assert build_result["timing"]["input_file"]["first_write_reliable"] is False

    check_code, check_result = _capture_json_command(
        R._cmd_check, SimpleNamespace(plan=executable, frozen=False)
    )
    assert check_code == 0
    assert check_result["timing"]["stage"] == "B3_CHECK"
    assert check_result["timing"]["input_file"]["first_write_reliable"] is False


def test_timing_failure_does_not_change_validate_result():
    import tempfile
    from types import SimpleNamespace

    source_drawing = os.path.abspath(
        os.path.join(PROJECT, "..", "..", "skills", "nx-agent", "examples", "example-output.json")
    )
    directory = tempfile.mkdtemp()
    drawing = os.path.join(directory, "timing-failure-drawing.json")
    with open(source_drawing, encoding="utf-8") as source:
        with open(drawing, "w", encoding="utf-8") as target:
            target.write(source.read())
    original = R.time.perf_counter_ns
    try:
        R.time.perf_counter_ns = lambda: (_ for _ in ()).throw(RuntimeError("clock failed"))
        exit_code, result = _capture_json_command(
            R._cmd_validate_drawing,
            SimpleNamespace(drawing=drawing, new_task=True, task_root=None),
        )
    finally:
        R.time.perf_counter_ns = original
    assert exit_code == 0
    assert result["ok"] is True
    assert result["timing"]["elapsed_ms"] is None


def test_runner_timing_boundaries_are_natural_operation_events(tmp_path=None):
    import asyncio
    import tempfile

    directory = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp()
    step_path = os.path.join(directory, "timing.step")

    class Transport:
        def resolve_path(self, path):
            return path

        async def call(self, tool, args):
            if tool == "nx_export_step":
                with open(args["path"], "wb") as handle:
                    handle.write(b"STEP")
                return {"status": "success", "path": args["path"]}
            return {"status": "success"}

    plan = {
        "mode": "FAST",
        "operations": [
            {
                "step": 1,
                "tool": "nx_extrude",
                "tool_args": {"sketch_id": "S1", "distance": 1},
                "topology_changes": True,
            },
            {
                "step": 2,
                "tool": "nx_export_step",
                "tool_args": {"path": step_path},
                "topology_changes": False,
            },
        ],
    }
    timing_state = R._begin_command_timing("C_RUNNER")
    timing_state["c2_modeling_complete_utc"] = None
    timing_state["c3_export_complete_utc"] = None
    report = asyncio.run(R.run_plan(plan, Transport(), timing_state=timing_state))
    assert report["status"] == "success"
    assert timing_state["c2_modeling_complete_utc"] is not None
    assert timing_state["c3_export_complete_utc"] is not None

    no_topology_state = R._begin_command_timing("C_RUNNER")
    no_topology_state["c2_modeling_complete_utc"] = None
    no_topology_state["c3_export_complete_utc"] = None
    no_topology_plan = {
        "mode": "FAST",
        "operations": [
            {
                "step": 1,
                "tool": "nx_status",
                "tool_args": {},
                "topology_changes": False,
            }
        ],
    }
    report = asyncio.run(
        R.run_plan(no_topology_plan, Transport(), timing_state=no_topology_state)
    )
    assert report["status"] == "success"
    assert no_topology_state["c2_modeling_complete_utc"] is None
    assert no_topology_state["c3_export_complete_utc"] is None

    class FailingTransport(Transport):
        async def call(self, tool, args):
            raise R.PlanError("synthetic modeling failure")

    failed_state = R._begin_command_timing("C_RUNNER")
    failed_state["c2_modeling_complete_utc"] = None
    failed_state["c3_export_complete_utc"] = None
    report = asyncio.run(R.run_plan(plan, FailingTransport(), timing_state=failed_state))
    assert report["status"] == "failed"
    assert failed_state["c2_modeling_complete_utc"] is None
    assert failed_state["c3_export_complete_utc"] is None

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
