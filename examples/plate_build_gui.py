"""Device mounting plate builder for Siemens NX — runs once, outputs PRT + STEP.

BATCH MODE (no persistent bridge lock):
  - Run via Alt+F8 (pick this file) or place a copy in <user_dir>/startup.
  - Builds the part strictly from the 2D drawing dimensions, saves PRT,
    exports STEP, writes plate_build_result.json, then exits.

Part (from the drawing "设备安装底板", Steel / mm / 1:1):
  - Base plate:     120 x 80 x 12, four corners R8
  - Center boss:    dia 30 x 20 (from plate top face, total height 32), dia 12 through hole
  - Mounting holes: 2 x dia 10 through, centers at (+-40, 0)  (20 mm from side edge)
  - Slot:           40 x 12 rounded slot (obround), center (0, SLOT_Y), through
                    (SLOT_Y is not dimensioned on the drawing; taken from view
                     projection as -18.0 mm — adjust SLOT_Y if needed)

Workspace resolution order:
  1. NX_MCP_WORKSPACE environment variable
  2. %USERPROFILE%\\NX_MCP_WORKSPACE
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
PART_NAME = "mounting_plate.prt"
PART_PATH = WORKSPACE / PART_NAME
RESULT_FILE = WORKSPACE / "plate_build_result.json"
LOG_FILE = WORKSPACE / "plate_build_journal.log"

# ---- drawing dimensions (mm) ----
PLATE_W, PLATE_H, PLATE_D = 120.0, 80.0, 12.0
CORNER_R = 8.0
BOSS_D, BOSS_H = 30.0, 20.0
HOLE_D = 12.0                      # center through hole
MOUNT_D, MOUNT_Y, MOUNT_OFF = 10.0, 0.0, 20.0   # 2 x dia10, X = +- (W/2 - 20)
SLOT_L, SLOT_W = 40.0, 12.0
SLOT_Y = -18.0                     # not dimensioned on the drawing; view projection


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
# NXOpen helpers (same verified API family as batch_build_gui.py)
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


def _sketch_arc(part, nxopen, sketch, center, radius, start_angle_deg, end_angle_deg):
    """Arc via the certified CreateArc(center, xDir, yDir, radius, start, end) API."""
    arc = part.Curves.CreateArc(
        nxopen.Point3d(float(center[0]), float(center[1]), 0.0),
        nxopen.Vector3d(1.0, 0.0, 0.0),
        nxopen.Vector3d(0.0, 1.0, 0.0),
        float(radius),
        float(start_angle_deg),
        float(end_angle_deg),
    )
    sketch.AddGeometry(arc, nxopen.Sketch.InferConstraintsOption.InferNoConstraints)
    return arc


def _sketch_obround(part, nxopen, sketch, cx, cy, length, width):
    """Rounded slot built from a middle rectangle + two end circles.

    The single "2 lines + 2 arcs" contour is NOT used here: NX does not chain
    the arc endpoints without explicit constraints, which makes the section
    unclosed and the extrude a sheet. Rect + circles (each a verified closed
    profile) is the robust equivalent: rect (L-2r) x W plus dia-W caps.
    """
    r = width / 2.0
    lx = length / 2.0 - r
    _sketch_rectangle(part, nxopen, sketch, cx, cy, length - 2 * r, width)
    _sketch_circle(part, nxopen, sketch, cx - lx, cy, width)
    _sketch_circle(part, nxopen, sketch, cx + lx, cy, width)


def _finish_sketch(nxopen, sketch):
    sketch.Deactivate(nxopen.Sketch.ViewReorient.TrueValue, nxopen.Sketch.UpdateLevel.Model)


def _extrude(part, nxopen, sketch, start_z, end_z, boolean_type, target_body=None):
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
        nxopen.Sense.Forward,
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


def _blend_vertical_edges(body, z0, z1):
    """Return the vertical edges of a body (Z changes along the edge).
    For a plate this is exactly the 4 corner edges. Includes diagnostics."""
    edges = list(body.GetEdges())
    out = []
    for e in edges:
        try:
            verts = list(e.GetVertices())
            if len(verts) < 2:
                continue
            p1, p2 = verts[0].Point, verts[1].Point
        except Exception:
            continue
        if abs(p1.Z - p2.Z) < 1e-3:          # not vertical
            continue
        if min(p1.Z, p2.Z) > z0 + 1e-2 or max(p1.Z, p2.Z) < z1 - 1e-2:
            continue
        out.append(e)
    _log(f"  diagnostics: body edges={len(edges)} vertical-in-range={len(out)}")
    return out


def _edge_blend_one(part, nxopen, edge, radius):
    collector = part.ScCollectors.CreateCollector()
    rule = part.ScRuleFactory.CreateRuleEdgeDumb([edge])
    collector.ReplaceRules([rule], False)
    builder = part.Features.CreateEdgeBlendBuilder(nxopen.Features.Feature.Null)
    try:
        builder.AddChainset(collector, str(float(radius)))
        builder.CommitFeature()
        return True
    except Exception:
        return False
    finally:
        builder.Destroy()


# --------------------------------------------------------------------------
def main() -> None:
    _log("mounting plate builder starting")
    import NXOpen
    import NXOpen.Features
    import NXOpen.GeometricUtilities

    the_session = NXOpen.Session.GetSession()
    the_lw = the_session.ListingWindow
    the_lw.Open()
    part_path = str(PART_PATH)
    for stale in (PART_PATH, WORKSPACE / "mounting_plate.step"):
        try:
            if stale.exists():
                stale.unlink()
                _log(f"cleaned stale file: {stale.name}")
        except OSError as exc:
            _log(f"cleanup failed for {stale.name}: {exc}")
    out = {"status": "ok", "part": part_path, "step": None, "features_built": 0}

    the_session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "PlateStart")
    pbuilder = the_session.Parts.FileNew()
    try:
        pbuilder.TemplateFileName = "model-plain-1-mm-template.prt"
        pbuilder.Units = NXOpen.Part.Units.Millimeters
        pbuilder.NewFileName = part_path
        pbuilder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
        new_part = pbuilder.Commit() or the_session.Parts.Work
    finally:
        pbuilder.Destroy()
    _log("part created")

    bool_t = NXOpen.GeometricUtilities.BooleanOperation.BooleanType
    body = None
    fi = 0

    try:
        # 1. base plate 120 x 80 x 12
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_BASE")
        _sketch_rectangle(new_part, NXOpen, sk, 0.0, 0.0, PLATE_W, PLATE_H)
        _finish_sketch(NXOpen, sk)
        feat = _extrude(new_part, NXOpen, sk, 0.0, PLATE_D, bool_t.Create)
        bodies = list(feat.GetBodies()) if hasattr(feat, "GetBodies") else []
        body = bodies[0] if bodies else list(new_part.Bodies)[-1]
        fi += 1
        _log(f"feature {fi}: base plate 120x80x12")

        # 2. center boss dia30 x 20 (unite, from z=12)
        sk = _create_sketch(new_part, NXOpen, PLATE_D, "PLATE_BOSS")
        _sketch_circle(new_part, NXOpen, sk, 0.0, 0.0, BOSS_D)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, BOSS_H, bool_t.Unite, body)
        fi += 1
        _log(f"feature {fi}: boss dia{BOSS_D:g} x {BOSS_H:g}")

        # 4. center through hole dia12 (z 0 -> 32)
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_HOLE12")
        _sketch_circle(new_part, NXOpen, sk, 0.0, 0.0, HOLE_D)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, PLATE_D + BOSS_H, bool_t.Subtract, body)
        fi += 1
        _log(f"feature {fi}: through hole dia{HOLE_D:g}")

        # 5. mounting holes 2 x dia10 through (z 0 -> 12)
        mx = PLATE_W / 2 - MOUNT_OFF
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_HOLES10")
        _sketch_circle(new_part, NXOpen, sk, -mx, MOUNT_Y, MOUNT_D)
        _sketch_circle(new_part, NXOpen, sk, mx, MOUNT_Y, MOUNT_D)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, PLATE_D, bool_t.Subtract, body)
        fi += 1
        _log(f"feature {fi}: 2 x dia{MOUNT_D:g} through at x=+-{mx:g}")

        # 6. slot 40 x 12 obround through (center 0, SLOT_Y)
        #    = middle rect (L-W) x W + two dia-W caps, three separate subtracts
        lx = SLOT_L / 2.0 - SLOT_W / 2.0
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_SLOT_MID")
        _sketch_rectangle(new_part, NXOpen, sk, 0.0, SLOT_Y, SLOT_L - SLOT_W, SLOT_W)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, PLATE_D, bool_t.Subtract, body)
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_SLOT_L")
        _sketch_circle(new_part, NXOpen, sk, -lx, SLOT_Y, SLOT_W)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, PLATE_D, bool_t.Subtract, body)
        sk = _create_sketch(new_part, NXOpen, 0.0, "PLATE_SLOT_R")
        _sketch_circle(new_part, NXOpen, sk, lx, SLOT_Y, SLOT_W)
        _finish_sketch(NXOpen, sk)
        _extrude(new_part, NXOpen, sk, 0.0, PLATE_D, bool_t.Subtract, body)
        fi += 3
        _log(f"feature {fi}: slot {SLOT_L:g}x{SLOT_W:g} obround at y={SLOT_Y:g} (rect + 2 caps)")

        # 7. corner R8 on the 4 vertical (corner) edges of the final body
        corner_edges = _blend_vertical_edges(body, 0.0, PLATE_D)
        done = 0
        for e in corner_edges:
            if _edge_blend_one(new_part, NXOpen, e, CORNER_R):
                done += 1
        fi += 1
        _log(f"feature {fi}: corner blend R8 done={done}/{len(corner_edges)}")
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
    step_path = str(WORKSPACE / "mounting_plate.step")
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

    out["output_dir"] = str(WORKSPACE)
    out["dimensions"] = {
        "plate": f"{PLATE_W:g}x{PLATE_H:g}x{PLATE_D:g}",
        "corner_radius": CORNER_R,
        "boss": f"dia{BOSS_D:g} x {BOSS_H:g}",
        "center_hole": f"dia{HOLE_D:g} through",
        "mounting_holes": f"2 x dia{MOUNT_D:g} at y={MOUNT_Y:g}",
        "slot": f"{SLOT_L:g}x{SLOT_W:g} obround at y={SLOT_Y:g}",
    }
    _write_result(out)
    _log("mounting plate builder DONE ok")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("mounting plate builder FAILED: " + traceback.format_exc())
        _write_result({"status": "error", "message": traceback.format_exc()})
        raise
