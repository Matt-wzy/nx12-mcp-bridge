"""Drive NX directly via the resident C# Loader named pipe (bypasses the
wedged MCP server / TCP bridge). Uses LoaderBridge which is the same backend
the MCP tools use."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nx_mcp.loader_bridge import LoaderBridge


async def main() -> None:
    bridge = LoaderBridge()
    print("ping:", bridge.ping())

    part = r"C:\code\ug_nx120_dev\nx_mcp_ws\链接体.prt"
    for method, params in [
        ("nx_open_part", {"path": part}),
        ("nx_list_features", {}),
        ("nx_list_sketches", {}),
        ("nx_list_bodies", {}),
        ("nx_list_dimensions", {}),
        ("nx_list_drawing_sheets", {}),
        ("nx_list_drafting_views", {}),
        ("nx_list_annotations", {}),
        ("nx_list_attributes", {}),
    ]:
        try:
            resp = await bridge.call(method, params)
            print("\n### %s" % method)
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        except Exception as e:
            print("\n### %s ERROR: %s" % (method, e))


if __name__ == "__main__":
    asyncio.run(main())
