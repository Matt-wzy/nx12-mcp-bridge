"""Inspect and exercise the supported sketch-creation API inside NX."""

from __future__ import annotations

import json
import os
from pathlib import Path

import NXOpen


def _new_part(session: NXOpen.Session, path: Path) -> NXOpen.Part:
    builder = session.Parts.FileNew()
    try:
        builder.TemplateFileName = "model-plain-1-mm-template.prt"
        builder.Units = NXOpen.Part.Units.Millimeters
        builder.NewFileName = str(path)
        builder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
        return builder.Commit()
    finally:
        builder.Destroy()


def _attempt(name: str, callback: object) -> dict[str, object]:
    try:
        result = callback()
        return {"name": name, "ok": True, "result_type": type(result).__name__}
    except Exception as error:
        return {
            "name": name,
            "ok": False,
            "error": str(error),
            "nx_code": getattr(error, "ErrorCode", None),
        }


def main() -> None:
    output = Path(os.environ["NX_MCP_SKETCH_PROBE_OUTPUT"])
    session = NXOpen.Session.GetSession()
    part = _new_part(session, output.with_suffix(".prt"))
    sketches = part.Sketches
    plane = part.Planes.CreatePlane(
        NXOpen.Point3d(0.0, 0.0, 0.0),
        NXOpen.Vector3d(0.0, 0.0, 1.0),
        NXOpen.SmartObject.UpdateOption.WithinModeling,
    )
    results: list[dict[str, object]] = []

    def create_in_place() -> object:
        builder = sketches.CreateSketchInPlaceBuilder2(NXOpen.Sketch.Null)
        try:
            builder.PlaneReference = plane
            return builder.Commit()
        finally:
            builder.Destroy()

    def create_simple() -> object:
        builder = sketches.CreateSimpleSketchInPlaceBuilder()
        try:
            return builder.Commit()
        finally:
            builder.Destroy()

    results.append(_attempt("CreateSketchInPlaceBuilder2", create_in_place))
    results.append(_attempt("CreateSimpleSketchInPlaceBuilder", create_simple))
    payload = {
        "sketch_collection_members": [
            member
            for member in dir(sketches)
            if "sketch" in member.lower() or "builder" in member.lower()
        ],
        "in_place_builder_members": [
            member
            for member in dir(sketches.CreateSketchInPlaceBuilder2(NXOpen.Sketch.Null))
            if not member.startswith("_")
        ],
        "simple_builder_members": [
            member
            for member in dir(sketches.CreateSimpleSketchInPlaceBuilder())
            if not member.startswith("_")
        ],
        "close_whole_tree_members": [
            member for member in dir(NXOpen.BasePart.CloseWholeTree) if not member.startswith("_")
        ],
        "part_close_members": [
            member for member in dir(part) if member.lower().startswith("close")
        ],
        "attempts": results,
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
