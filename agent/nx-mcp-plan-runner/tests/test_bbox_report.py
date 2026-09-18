# -*- coding: utf-8 -*-
"""Unit tests for the report bbox semantics (no NX involved).

Covers: model_bbox (merged full-response extents across every list step)
vs linear_edge_bbox (linear-edge bbox only). Selection engine untouched.
Run:  python tests/test_bbox_report.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner as R  # noqa: E402


def make_edge(index, **kw):
    base = {"index": index, "tag": index, "curve_type": "Linear", "start": [0, 0, 0],
            "end": [0, 0, 0], "midpoint": [0, 0, 0], "length": 26.0,
            "bbox_min": [0, 0, 0], "bbox_max": [0, 0, 0], "direction": "Z",
            "adjacent_faces": 2}
    base.update(kw)
    return base


def make_face(index, **kw):
    base = {"index": index, "tag": index, "face_type": "Planar",
            "centroid": [0, 0, 0], "area": 100.0, "normal": None}
    base.update(kw)
    return base


class Transport:
    """Scripted fake: faces list then edges list."""

    def __init__(self, faces, edges):
        self.faces = faces
        self.edges = edges
        self.calls = []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "nx_list_faces":
            return {"status": "success", "faces": self.faces, "message": "ok"}
        return {"status": "success", "edges": self.edges, "message": "ok"}


# --------------------------------------------------------------------------
# merge_extents
# --------------------------------------------------------------------------
def test_merge_extents_none_safe():
    assert R.merge_extents(None, None) is None
    assert R.merge_extents(None, {"x_min": -1, "x_max": 1, "y_min": 0, "y_max": 0,
                                  "z_min": 0, "z_max": 0}) == {
        "x_min": -1, "x_max": 1, "y_min": 0, "y_max": 0, "z_min": 0, "z_max": 0}
    a = {"x_min": -5, "x_max": 5, "y_min": -1, "y_max": 1, "z_min": 0, "z_max": 2}
    assert R.merge_extents(a, None) == a


def test_merge_extents_span_union():
    a = {"x_min": -100, "x_max": 100, "y_min": -50, "y_max": 50, "z_min": 0, "z_max": 20}
    b = {"x_min": -115, "x_max": 115, "y_min": -60, "y_max": 60, "z_min": 10, "z_max": 56}
    m = R.merge_extents(a, b)
    assert m["x_min"] == -115 and m["x_max"] == 115
    assert m["y_min"] == -60 and m["y_max"] == 60
    assert m["z_min"] == 0 and m["z_max"] == 56


# --------------------------------------------------------------------------
# report fields via run_plan
# --------------------------------------------------------------------------
def _plan(steps):
    return {"mode": "FAST", "operations": steps}


def test_report_model_bbox_merges_faces_and_edges():
    faces = [
        make_face(0, centroid=[0, 0, 56]),      # flange top
        make_face(1, centroid=[0, 0, 0]),       # base bottom
        make_face(2, centroid=[115, 0, 6]),
        make_face(3, centroid=[-115, 0, 6]),
        make_face(4, centroid=[0, 60, 6]),
        make_face(5, centroid=[0, -60, 6]),
    ]
    edges = [
        make_edge(0, bbox_min=[-115, -60, 0], bbox_max=[-115, -60, 26]),
        make_edge(1, bbox_min=[115, 60, 0], bbox_max=[115, 60, 26]),
        make_edge(2, curve_type="Circular", bbox_min=None, bbox_max=None,
                  length=100.0),  # non-linear: excluded from linear extents
    ]
    plan = _plan([
        {"step": 1, "tool": "nx_list_faces", "tool_args": {"body_id": "B1"},
         "selection_criteria": {"face_type": "Planar", "centroid_z": {"value": 56, "tol": 0.5}},
         "expectation": {"count": 1}},
        {"step": 2, "tool": "nx_list_edges", "tool_args": {"body_id": "B1"},
         "selection_criteria": {"linear_only": True},
         "expectation": {"count": 2}},
    ])
    rep = asyncio.run(R.run_plan(plan, Transport(faces, edges)))
    assert rep["status"] == "success", rep["steps"]
    assert rep["model_bbox"] == {
        "x_min": -115, "x_max": 115, "y_min": -60, "y_max": 60,
        "z_min": 0, "z_max": 56}, rep["model_bbox"]
    assert rep["linear_edge_bbox"] == {
        "x_min": -115, "x_max": 115, "y_min": -60, "y_max": 60,
        "z_min": 0, "z_max": 26}, rep["linear_edge_bbox"]
    assert "bbox" not in rep


def test_report_model_bbox_survives_partial_early_steps():
    # an early list on a partial body must not shrink the merged extents
    faces_partial = [make_face(0, centroid=[0, 0, 12]), make_face(1, centroid=[0, 0, 48])]
    faces_final = [
        make_face(0, centroid=[0, 0, 56]), make_face(1, centroid=[0, 0, 0]),
        make_face(2, centroid=[115, 0, 6]), make_face(3, centroid=[-115, 0, 6]),
    ]
    edges = [make_edge(0, bbox_min=[-115, -60, 0], bbox_max=[-115, -60, 26]),
             make_edge(1, bbox_min=[115, 60, 0], bbox_max=[115, 60, 26])]
    plan = _plan([
        {"step": 1, "tool": "nx_list_faces", "tool_args": {"body_id": "B0"},
         "selection_criteria": {"centroid_z": {"value": 48, "tol": 0.5}},
         "expectation": {"count": 1}},
        {"step": 2, "tool": "nx_list_faces", "tool_args": {"body_id": "B1"},
         "selection_criteria": {"centroid_z": {"value": 56, "tol": 0.5}},
         "expectation": {"count": 1}},
        {"step": 3, "tool": "nx_list_edges", "tool_args": {"body_id": "B1"},
         "selection_criteria": {"linear_only": True},
         "expectation": {"count": 2}},
    ])

    class T(Transport):
        def __init__(self):
            super().__init__(faces_final, edges)
            self.seen = []

        async def call(self, tool, args):
            self.seen.append(tool)
            if tool == "nx_list_faces":
                faces = faces_partial if args.get("body_id") == "B0" else faces_final
                return {"status": "success", "faces": faces, "message": "ok"}
            return {"status": "success", "edges": edges, "message": "ok"}

    rep = asyncio.run(R.run_plan(plan, T()))
    assert rep["status"] == "success", rep["steps"]
    assert rep["model_bbox"]["z_min"] == 0 and rep["model_bbox"]["z_max"] == 56
    assert rep["model_bbox"]["x_min"] == -115 and rep["model_bbox"]["x_max"] == 115
    assert rep["linear_edge_bbox"]["z_max"] == 26


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print("PASS  %s" % fn.__name__)
            passed += 1
        except Exception:
            print("FAIL  %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (passed, len(fns)))
    sys.exit(0 if passed == len(fns) else 1)
