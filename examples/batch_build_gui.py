"""Batch model builder for Siemens NX - runs once, outputs PRT + STEP, exits.

BATCH MODE (no bridge, no NX lock):
  - Run via Alt+F8 (pick this file) or place a copy in <user_dir>/startup for
    automatic execution at NX launch.
  - Reads batch_task.json from the workspace, builds the model, saves PRT,
    exports STEP, writes batch_result.json, then exits. NX is never locked.

Workspace resolution order:
  1. NX_MCP_WORKSPACE environment variable
  2. %USERPROFILE%\\NX_MCP_WORKSPACE

Supported features:
  {"type": "rect_extrude", "w":, "h":, "d":, "x":, "y":, "subtract": false}
  {"type": "hole", "diameter":, "depth":, "x":, "y":}
  {"type": "edge_blend", "radius":}          (all edges, per-edge tolerant)
  {"type": "chamfer", "offset":}             (all edges, per-edge tolerant)

rect_extrude centers at (x, y) on top of the previous feature; the first one
creates the base, later ones unite (or subtract if "subtract": true).
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path


def _resolve_workspace() -> Path:
    env = os.environ.get("NX_MCP_WORKSPACE")
    if env:
        return Path(env)
    return Path.home() / "NX_MCP_WORKSPACE"


WORKSPACE = _resolve_workspace()
WORKSPACE.mkdir(parents=True, exist_ok=True)
TASK_FILE = WORKSPACE / "batch_task.json"
RESULT_FILE = WORKSPACE / "batch_result.json"
LOG_FILE = WORKSPACE / "batch_journal.log"


def _log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass


def _write_result(payload: dict) -> None:
    try:
        RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# NXOpen helpers (same verified API family as the visual bridge)
# --------------------------------------------------------------------------
def _plane(part, nxopen, z: float, normal=(0.0, 0.0, 1.0)):
    return part.Planes.CreatePlane(
        nxopen.Point3d(0.0, 0.0, z),
        nxopen.Vector3d(*normal),
        nxopen.SmartObject.UpdateOption.WithinModeling,
    )


def _create_sketch(part, nxopen, z: float, name: str):
    plane = _plane(part, nxopen, z)
    builder = part.Sketches.CreateSketchInPlaceBuilder2(nxopen.Sketch.Null)
    try:
        builder.PlaneReference = plane
        sketch = builder.Commit()
    finally:
        builder.Destroy()
    if name:
        sketch.SetName(name)
    sketch.Activate(nxopen.Sketch.ViewReorient.TrueValue)
    return sketch


def _sketch_line(part, nxopen, sketch, x1, y1, x2, y2):
    curve = part.Curves.CreateLine(
        nxopen.Point3d(float(x1), float(y1), 0.0),
        nxopen.Point3d(float(x2), float(y2), 0.0),
    )
    sketch.AddGeometry(curve, nxopen.Sketch.InferConstraintsOption.InferNoConstraints)
    return curve


def _sketch_rectangle(part, nxopen, sketch, cx, cy, w, h):
    x1, y1 = cx - w / 2.0, cy - h / 2.0
    x2, y2 = cx + w / 2.0, cy + h / 2.0
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    for i in range(4):
        _sketch_line(part, nxopen, sketch, corners[i][0], corners[i][1],
                     corners[(i + 1) % 4][0], corners[(i + 1) % 4][1])


def _sketch_circle(part, nxopen, sketch, cx, cy, diameter):
    sketch.Activate(nxopen.Sketch.ViewReorient.TrueValue)
    builder = part.Sketches.CreateCircleBuilder()
    try:
        builder.SetCenterPoint(nxopen.Point3d(float(cx), float(cy), 0.0))
        builder.SetSizePoint(nxopen.Point3d(float(cx) + float(diameter) / 2.0, float(cy), 0.0))
        circle = builder.Commit()
    finally:
        builder.Destroy()
    return circle


def _finish_sketch(nxopen, sketch):
    sketch.Deactivate(nxopen.Sketch.ViewReorient.TrueValue, nxopen.Sketch.UpdateLevel.Model)


def _extrude(part, nxopen, sketch, start_z, end_z, boolean_type, target_body=None, reverse=False):
    section = part.Sections.CreateSection()
    rule = part.ScRuleFactory.CreateRuleCurveFeature(
        [sketch.Feature],
        nxopen.DisplayableObject.Null,
        part.ScRuleFactory.CreateRuleOptions(),
    )
    section.AddToSection(
        [rule], nxopen.NXObject.Null, nxopen.NXObject.Null, nxopen.NXObject.Null,
        nxopen.Point3d(0.0, 0.0, 0.0), nxopen.Section.Mode.Create, False,
    )
    direction = part.Directions.CreateDirection(
        sketch,
        nxopen.Sense.Reverse if reverse else nxopen.Sense.Forward,
        nxopen.SmartObject.UpdateOption.WithinModeling,
    )
    builder = part.Features.CreateExtrudeBuilder(nxopen.Features.Feature.Null)
    try:
        builder.Section = section
        builder.Direction = direction
        builder.Limits.StartExtend.Value.RightHandSide = f"{start_z:g}"
        builder.Limits.EndExtend.Value.RightHandSide = f"{end_z:g}"
        builder.BooleanOperation.Type = boolean_type
        if target_body is not None:
            builder.BooleanOperation.SetTargetBodies([target_body])
        builder.AllowSelfIntersectingSection(True)
        feature = builder.CommitFeature()
    finally:
        builder.Destroy()
    return feature


def _select_all_edges(part, nxopen, body):
    edges = list(body.GetEdges())
    collector = part.ScCollectors.CreateCollector()
    rule = part.ScRuleFactory.CreateRuleEdgeDumb(edges)
    collector.ReplaceRules([rule], False)
    return collector


def _edge_filter(body, spec):
    """Resolve an edge selection spec to a list of edges.
    spec: None/"all" -> every edge; list -> indices; dict {"top_outline": {...}}
    -> horizontal edges at z whose midpoint lies on the given x/y outline."""
    edges = list(body.GetEdges())
    if spec is None or spec == "all":
        return edges
    if isinstance(spec, list):
        return [edges[i] for i in spec if 0 <= i < len(edges)]
    if isinstance(spec, dict) and "top_outline" in spec:
        cfg = spec["top_outline"]
        z, hw, hh = float(cfg["z"]), float(cfg["half_w"]), float(cfg["half_h"])
        out = []
        for e in edges:
            try:
                verts = list(e.GetVertices())
                if len(verts) < 2:
                    continue
                p1, p2 = verts[0].Point, verts[1].Point
            except Exception:
                continue
            if abs(p1.Z - p2.Z) > 1e-3 or abs(p1.Z - z) > 1e-3:
                continue
            mx = (p1.X + p2.X) / 2.0
            my = (p1.Y + p2.Y) / 2.0
            if abs(abs(mx) - hw) < 1e-3 or abs(abs(my) - hh) < 1e-3:
                out.append(e)
        return out
    return edges


def _edge_blend(part, nxopen, body, radius, spec=None):
    selected = _edge_filter(body, spec)
    done = 0
    for edge in selected:
        collector = part.ScCollectors.CreateCollector()
        rule = part.ScRuleFactory.CreateRuleEdgeDumb([edge])
        collector.ReplaceRules([rule], False)
        builder = part.Features.CreateEdgeBlendBuilder(nxopen.Features.Feature.Null)
        try:
            builder.AddChainset(collector, str(float(radius)))
            builder.CommitFeature()
            done += 1
        except Exception:
            pass
        finally:
            builder.Destroy()
    return done


def _chamfer(part, nxopen, body, offset, spec=None):
    selected = _edge_filter(body, spec)
    done = 0
    for edge in selected:
        collector = part.ScCollectors.CreateCollector()
        rule = part.ScRuleFactory.CreateRuleEdgeDumb([edge])
        collector.ReplaceRules([rule], False)
        builder = part.Features.CreateChamferBuilder(nxopen.Features.Feature.Null)
        try:
            builder.SmartCollector = collector
            builder.FirstOffset = str(float(offset))
            builder.Option = nxopen.Features.ChamferBuilder.ChamferOption.SymmetricOffsets
            builder.CommitFeature()
            done += 1
        except Exception:
            pass
        finally:
            builder.Destroy()
    return done


# --------------------------------------------------------------------------
def main() -> None:
    _log("batch builder starting")
    if not TASK_FILE.exists():
        _log("no task file, exiting")
        return

    task = json.loads(TASK_FILE.read_text(encoding="utf-8-sig"))
    part_rel = task["part"]
    units = task.get("units", "mm")
    features = task.get("features", [])
    export_step = task.get("export_step", True)
    _log(f"task loaded: {part_rel} units={units} features={len(features)}")

    import NXOpen
    import NXOpen.Features
    import NXOpen.GeometricUtilities

    the_session = NXOpen.Session.GetSession()
    the_lw = the_session.ListingWindow
    the_lw.Open()
    work_dir = str(WORKSPACE)
    part_path = str(WORKSPACE / part_rel)
    for stale in (WORKSPACE / part_rel, WORKSPACE / (Path(part_rel).stem + ".step")):
        try:
            if stale.exists():
                stale.unlink()
                _log(f"cleaned stale file: {stale.name}")
        except OSError as exc:
            _log(f"cleanup failed for {stale.name}: {exc}")
    out = {"status": "ok", "part": part_path, "step": None, "features_built": 0}

    the_session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "BatchStart")
    pbuilder = the_session.Parts.FileNew()
    try:
        pbuilder.TemplateFileName = (
            "model-plain-1-mm-template.prt"
            if units == "mm"
            else "model-plain-1-inch-template.prt"
        )
        pbuilder.Units = (
            NXOpen.Part.Units.Millimeters if units == "mm" else NXOpen.Part.Units.Inches
        )
        pbuilder.NewFileName = part_path
        pbuilder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
        new_part = pbuilder.Commit() or the_session.Parts.Work
    finally:
        pbuilder.Destroy()
    _log("part created")

    body = None
    top_z = 0.0
    bool_t = NXOpen.GeometricUtilities.BooleanOperation.BooleanType

    try:
        for fi, feat in enumerate(features):
            typ = feat.get("type", "rect_extrude")
            fname = feat.get("name", f"F{fi + 1}")
            if typ == "rect_extrude":
                w = float(feat["w"])
                h = float(feat["h"])
                d = float(feat["d"])
                cx = float(feat.get("x", 0.0))
                cy = float(feat.get("y", 0.0))
                subtract = bool(feat.get("subtract", False))
                if subtract:
                    if body is None:
                        raise RuntimeError("subtract requires an existing body")
                    sketch = _create_sketch(new_part, NXOpen, top_z - d, f"BATCH_SK_{fi}")
                    _sketch_rectangle(new_part, NXOpen, sketch, cx, cy, w, h)
                    _finish_sketch(NXOpen, sketch)
                    _extrude(new_part, NXOpen, sketch, 0.0, d, bool_t.Subtract, body)
                    _log(f"feature {fname}: subtract rect {w}x{h}x{d} at z={top_z}")
                else:
                    sketch = _create_sketch(new_part, NXOpen, top_z, f"BATCH_SK_{fi}")
                    _sketch_rectangle(new_part, NXOpen, sketch, cx, cy, w, h)
                    _finish_sketch(NXOpen, sketch)
                    if body is None:
                        feature = _extrude(new_part, NXOpen, sketch, 0.0, d, bool_t.Create)
                    else:
                        feature = _extrude(new_part, NXOpen, sketch, 0.0, d, bool_t.Unite, body)
                    feature_bodies = list(feature.GetBodies()) if hasattr(feature, "GetBodies") else []
                    if feature_bodies:
                        body = feature_bodies[0]
                    else:
                        body = list(new_part.Bodies)[-1]
                    top_z += d
                    _log(f"feature {fname}: rect {w}x{h}x{d} -> top_z={top_z}")
            elif typ == "hole":
                diameter = float(feat["diameter"])
                depth = float(feat["depth"])
                hx = float(feat.get("x", 0.0))
                hy = float(feat.get("y", 0.0))
                if body is None:
                    raise RuntimeError("hole requires an existing body")
                sketch = _create_sketch(new_part, NXOpen, top_z - depth, f"BATCH_SK_H{fi}")
                _sketch_circle(new_part, NXOpen, sketch, hx, hy, diameter)
                _finish_sketch(NXOpen, sketch)
                _extrude(new_part, NXOpen, sketch, 0.0, depth, bool_t.Subtract, body)
                _log(f"feature {fname}: hole d={diameter} depth={depth} at ({hx},{hy})")
            elif typ == "edge_blend":
                if body is None:
                    raise RuntimeError("edge_blend requires an existing body")
                done = _edge_blend(new_part, NXOpen, body, float(feat["radius"]),
                                   feat.get("edges"))
                _log(f"feature {fname}: edge blend r={feat['radius']} done={done} edges")
            elif typ == "chamfer":
                if body is None:
                    raise RuntimeError("chamfer requires an existing body")
                done = _chamfer(new_part, NXOpen, body, float(feat["offset"]),
                                feat.get("edges"))
                _log(f"feature {fname}: chamfer off={feat['offset']} done={done} edges")
            else:
                raise RuntimeError(f"unknown feature type: {typ}")
            out["features_built"] = fi + 1
    except Exception:
        _log("model loop FAILED: " + traceback.format_exc())
        raise

    # save PRT
    save_status = None
    try:
        save_status = new_part.Save(
            NXOpen.BasePart.SaveComponents.FalseValue,
            NXOpen.BasePart.CloseAfterSave.FalseValue,
        )
    finally:
        if save_status is not None:
            save_status.Dispose()
    _log("part saved")

    # export STEP (verified translator options)
    if export_step:
        step_path = str(WORKSPACE / (Path(part_rel).stem + ".step"))
        creator = the_session.DexManager.CreateStepCreator()
        try:
            creator.OutputFile = step_path
            creator.InputFile = part_path
            if hasattr(creator, "ObjectTypes"):
                creator.ObjectTypes.Solids = True
            creator.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
            base_dir = os.environ.get("UGII_BASE_DIR")
            if base_dir:
                settings_file = Path(base_dir) / "STEP214UG" / "ugstep214.def"
                if settings_file.is_file():
                    creator.SettingsFile = str(settings_file)
            creator.Commit()
        finally:
            creator.Destroy()
        _log(f"step exported: {step_path}")
        out["step"] = step_path

    out["output_dir"] = work_dir
    _write_result(out)
    _log("batch builder DONE ok")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("batch builder FAILED: " + traceback.format_exc())
        _write_result({"status": "error", "message": traceback.format_exc()})
        raise
