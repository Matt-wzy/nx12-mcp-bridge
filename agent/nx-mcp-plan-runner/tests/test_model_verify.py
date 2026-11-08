# -*- coding: utf-8 -*-
"""Unit tests for the `verify` read-back audit (no NX involved).

Run:  python tests/test_model_verify.py
or:   pytest tests/test_model_verify.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner as R  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXECUTABLE_PLAN = os.path.join(PROJECT, "examples", "modeling-plan-executable.json")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def readback_hole(index, dia, depth, x=0.0, y=0.0, kind="hole", err=""):
    return {"index": index, "kind": kind, "diameter": dia, "depth": depth,
            "x": x, "y": y, "err": err}


def spec(dia, depth, count=1, pos=None, step=1, role="hole", group=0):
    return {"diameter": dia, "depth": depth, "count": count,
            "position": pos, "step": step, "tool": "nx_hole",
            "role": role, "group": group}


def op_hole(step, dia, depth, center=None, binds=None):
    args = {"body_id": "$body", "diameter": dia, "depth": depth}
    if center is not None:
        args["center"] = center
    return {"step": step, "tool": "nx_hole", "tool_args": args,
            "result_bindings": binds}


# ---- derive_expected_holes -------------------------------------------------
def test_derive_simple_hole():
    plan = {"operations": [op_hole(1, 6.6, 20, {"x": 10, "y": 20})]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert len(specs) == 1
    assert specs[0]["diameter"] == 6.6
    assert specs[0]["depth"] == 20
    assert specs[0]["count"] == 1
    assert specs[0]["position"] == {"x": 10.0, "y": 20.0}


def test_derive_counterbore_yields_two_specs():
    plan = {"operations": [{
        "step": 1, "tool": "nx_counterbore_hole",
        "tool_args": {"body_id": "$body", "center": {"x": -72, "y": -42},
                      "hole_diameter": 9, "hole_depth": 12,
                      "counterbore_diameter": 16, "counterbore_depth": 5},
    }]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    roles = sorted(s["role"] for s in specs)
    assert roles == ["counterbore", "hole"]
    by_role = {s["role"]: s for s in specs}
    assert by_role["hole"]["diameter"] == 9 and by_role["hole"]["depth"] == 12
    assert by_role["counterbore"]["diameter"] == 16
    assert by_role["counterbore"]["depth"] == 5


def test_derive_countersink_yields_one_cylinder_spec():
    # countersink seats are conical -> excluded from the cylinder census
    plan = {"operations": [{
        "step": 1, "tool": "nx_countersink_hole",
        "tool_args": {"body_id": "$body", "center": {"x": 0, "y": 0},
                      "hole_diameter": 8, "hole_depth": 12,
                      "countersink_diameter": 14, "countersink_angle": 90},
    }]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert len(specs) == 1
    assert specs[0]["diameter"] == 8 and specs[0]["depth"] == 12


def test_derive_pattern_on_hole_multiplies():
    plan = {"operations": [
        op_hole(1, 6.6, 20, {"x": 10, "y": 0}, binds={"object": "body_h1"}),
        {"step": 2, "tool": "nx_linear_pattern",
         "tool_args": {"body_id": "$body_h1", "direction": "X",
                       "count": 4, "spacing": 20}},
    ]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert len(specs) == 1
    assert specs[0]["count"] == 4


def test_derive_two_patterns_stack_multiplication():
    plan = {"operations": [
        op_hole(1, 6.6, 20, {"x": 10, "y": 0}, binds={"object": "body_h1"}),
        {"step": 2, "tool": "nx_linear_pattern",
         "tool_args": {"body_id": "$body_h1", "direction": "X", "count": 2, "spacing": 20}},
        {"step": 3, "tool": "nx_linear_pattern",
         "tool_args": {"body_id": "$body_h1", "direction": "Y", "count": 3, "spacing": 15}},
    ]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert specs[0]["count"] == 6


def test_derive_pattern_on_non_hole_body_is_neutral():
    plan = {"operations": [
        {"step": 1, "tool": "nx_extrude",
         "tool_args": {"sketch_id": "$sk1", "distance": 10},
         "result_bindings": {"body": "body_boss1"}},
        {"step": 2, "tool": "nx_linear_pattern",
         "tool_args": {"body_id": "$body_boss1", "direction": "X",
                       "count": 3, "spacing": 10}},
        op_hole(3, 6.6, 20, {"x": 10, "y": 0}),
    ]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert len(specs) == 1
    assert specs[0]["count"] == 1


def test_derive_pattern_on_unbound_reference_fails_closed():
    plan = {"operations": [
        op_hole(1, 6.6, 20, {"x": 10, "y": 0}),
        {"step": 2, "tool": "nx_linear_pattern",
         "tool_args": {"body_id": "$body_ghost", "direction": "X",
                       "count": 3, "spacing": 10}},
    ]}
    specs, errors = R.derive_expected_holes(plan)
    assert specs  # the plain hole is still derived
    assert errors and "unbound reference" in errors[0]


def test_derive_unresolvable_center_drops_position():
    plan = {"operations": [op_hole(1, 6.6, 20, {"x": "$cx", "y": 0})]}
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    assert specs[0]["position"] is None


def test_derive_example_executable_plan_census():
    """Integration: the repo's own 59-step example derives a clean census."""
    with open(EXECUTABLE_PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    specs, errors = R.derive_expected_holes(plan)
    assert not errors
    # 2 countersink (Ø8) + 4 through (Ø6.6) + 4 counterbore pairs (Ø9 + Ø16)
    instances = sum(int(s["count"]) for s in specs)
    assert instances == 14
    dias = sorted({round(s["diameter"], 3) for s in specs})
    assert dias == [6.6, 8.0, 9.0, 16.0]


# ---- match_hole_census ------------------------------------------------------
def test_match_all_expected_present():
    expected = [spec(6.6, 20, pos={"x": 10.0, "y": 0.0}),
                spec(9, 12, pos={"x": -72.0, "y": 0.0}, role="hole"),
                spec(16, 5, pos={"x": -72.0, "y": 0.0}, role="counterbore")]
    holes = [readback_hole(1, 6.6, 20, 10.0, 0.0),
             readback_hole(2, 9, 12, -72.0, 0.0),
             readback_hole(3, 16, 5, -72.0, 0.0)]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    assert not findings
    assert facts["matched"] == 3 and facts["extras"] == 0


def test_match_diameter_mismatch_is_fail():
    expected = [spec(6.6, 20, pos={"x": 10.0, "y": 0.0})]
    holes = [readback_hole(1, 6.8, 20, 10.0, 0.0)]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    fails = [f for f in findings if f["type"] == "missing_holes"]
    assert len(fails) == 1 and fails[0]["severity"] == "fail"
    # the Ø6.8 cylinder becomes an extra review finding
    assert facts["extras"] == 1


def test_match_missing_instance_is_fail():
    expected = [spec(6.6, 20, count=4)]
    holes = [readback_hole(i, 6.6, 20, float(i), 0.0) for i in range(3)]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    assert any(f["type"] == "missing_holes" and f["matched"] == 3 for f in findings)


def test_match_extra_hole_review_by_default_fail_on_policy():
    expected = [spec(6.6, 20)]
    holes = [readback_hole(1, 6.6, 20, 0, 0), readback_hole(2, 6.6, 20, 20, 0)]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    assert facts["extras"] == 1
    assert findings[0]["severity"] == "review"
    findings2, _ = R.match_hole_census(expected, holes, 0.05, extra_policy="fail")
    assert findings2[0]["severity"] == "fail"


def test_match_position_mismatch_is_fail():
    expected = [spec(6.6, 20, pos={"x": 10.0, "y": 0.0})]
    holes = [readback_hole(1, 6.6, 20, 12.0, 0.0)]
    findings, _ = R.match_hole_census(expected, holes, 0.05)
    assert any(f["type"] == "missing_holes" for f in findings)
    assert any(f["type"] == "extra_hole" for f in findings)


def test_match_patterned_spec_ignores_positions():
    expected = [spec(6.6, 20, count=4, pos={"x": 10.0, "y": 0.0})]
    holes = [readback_hole(i, 6.6, 20, float(i) * 7, 3.0) for i in range(4)]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    assert not findings
    assert facts["matched"] == 4


def test_match_ignores_non_hole_kinds_and_err_entries():
    expected = [spec(6.6, 20)]
    holes = [readback_hole(1, 6.6, 20, 0, 0),
             readback_hole(2, 30.0, 0.0, 0, 0, kind="cylinder"),
             readback_hole(3, 12.0, 8.0, 0, 0, err="face unreadable")]
    findings, facts = R.match_hole_census(expected, holes, 0.05)
    assert not findings
    assert facts["candidates"] == 1
    assert facts["readback_errors"] and facts["readback_errors"][0]["index"] == 3


def test_match_tolerance_boundary():
    expected = [spec(6.6, 20)]
    inside = [readback_hole(1, 6.63, 20, 0, 0)]
    outside = [readback_hole(1, 6.68, 20, 0, 0)]
    findings_in, _ = R.match_hole_census(expected, inside, 0.05)
    findings_out, _ = R.match_hole_census(expected, outside, 0.05)
    assert not findings_in
    assert any(f["type"] == "missing_holes" for f in findings_out)


# ---- expected_bbox_spans / bbox_findings -------------------------------------
def test_bbox_spans_from_drawing():
    drawing = {"overall_dimensions": {"length": 80, "width": 60, "height": 25}}
    spans, errors = R.expected_bbox_spans(drawing)
    assert not errors
    assert spans == {"x": 80.0, "y": 60.0, "z": 25.0}


def test_bbox_spans_missing_axis_is_error():
    drawing = {"overall_dimensions": {"length": 80, "width": 60}}
    spans, errors = R.expected_bbox_spans(drawing)
    assert "z" not in spans
    assert errors and "height" in errors[0]


def test_bbox_findings_pass():
    extents = {"x_min": -40.0, "x_max": 40.0, "y_min": -30.0, "y_max": 30.0,
               "z_min": 0.0, "z_max": 25.0}
    findings = R.bbox_findings(extents, {"x": 80.0, "y": 60.0, "z": 25.0}, 0.05)
    assert not findings


def test_bbox_findings_span_mismatch_is_fail():
    extents = {"x_min": -40.0, "x_max": 40.0, "y_min": -30.0, "y_max": 30.0,
               "z_min": 0.0, "z_max": 26.5}
    findings = R.bbox_findings(extents, {"x": 80.0, "y": 60.0, "z": 25.0}, 0.05)
    assert len(findings) == 1
    assert findings[0]["type"] == "bbox_mismatch"
    assert findings[0]["severity"] == "fail"


def test_bbox_findings_position_independent():
    # the same part shifted in space must still pass: spans, not placement
    extents = {"x_min": 10.0, "x_max": 90.0, "y_min": 5.0, "y_max": 65.0,
               "z_min": -3.0, "z_max": 22.0}
    findings = R.bbox_findings(extents, {"x": 80.0, "y": 60.0, "z": 25.0}, 0.05)
    assert not findings


def test_bbox_findings_no_linear_edges_is_fail_not_silent():
    findings = R.bbox_findings(None, {"x": 80.0, "y": 60.0, "z": 25.0}, 0.05)
    assert len(findings) == 3
    assert all(f["severity"] == "fail" for f in findings)


# ---- verdict aggregation ------------------------------------------------------
def test_verdict_ordering():
    assert R._verify_verdict([], []) == "pass"
    assert R._verify_verdict([{"severity": "review"}], []) == "review"
    assert R._verify_verdict([{"severity": "fail"}], []) == "fail"
    # a blocked family is unverified even when it also produced findings;
    # both stay visible in the output and both exit 1
    assert R._verify_verdict([{"severity": "fail"}], ["nx_list_holes failed"]) \
        == "unverified"


# ---- report/plan lineage ------------------------------------------------------
def test_verify_lineage_mismatch_rejected():
    errs = R.verify_lineage_errors({"plan": "other.json"}, "smoke.json")
    assert errs and "different plan" in errs[0]


def test_verify_lineage_same_path_ok():
    assert R.verify_lineage_errors({"plan": "smoke.json"}, "smoke.json") == []
    # legacy reports without a recorded plan path stay auditable
    assert R.verify_lineage_errors({}, "smoke.json") == []
    assert R.verify_lineage_errors({"plan": ""}, "smoke.json") == []


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")
