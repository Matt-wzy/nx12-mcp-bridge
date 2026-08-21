"""Probe the core FileNew call on NX's journal thread."""

from __future__ import annotations

import json
import os
from pathlib import Path

import NXOpen


def main() -> None:
    output = os.environ.get("NX_MCP_MAIN_THREAD_PROBE_OUTPUT")
    part_path = os.environ.get("NX_MCP_MAIN_THREAD_PROBE_PART")
    if not output or not part_path:
        raise RuntimeError(
            "Set NX_MCP_MAIN_THREAD_PROBE_OUTPUT and NX_MCP_MAIN_THREAD_PROBE_PART"
        )

    session = NXOpen.Session.GetSession()
    destination = Path(part_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    builder = session.Parts.FileNew()
    try:
        builder.TemplateFileName = "model-plain-1-mm-template.prt"
        builder.Units = NXOpen.Part.Units.Millimeters
        builder.NewFileName = str(destination)
        builder.DisplayPartOption = NXOpen.DisplayPartOption.AllowAdditional
        builder.UseBlankTemplate = True
        part = builder.Commit()
        result = {"ok": True, "part_name": str(part.Name)}
    except Exception as error:
        result = {
            "ok": False,
            "error": str(error),
            "nx_code": getattr(error, "ErrorCode", None),
        }
    finally:
        builder.Destroy()

    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
