"""GUI-session variant of start_nx_bridge.py.

Same bridge, but with workspace defaults resolved automatically so it can be
run from NX's Run Journal dialog (Alt+F8) where the launcher environment
variables are not inherited. Workspace resolution order:
  1. NX_MCP_WORKSPACE environment variable
  2. %USERPROFILE%\\NX_MCP_WORKSPACE
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _resolve_workspace() -> Path:
    env = os.environ.get("NX_MCP_WORKSPACE")
    if env:
        return Path(env)
    return Path.home() / "NX_MCP_WORKSPACE"


_WORKSPACE = _resolve_workspace()
_WORKSPACE.mkdir(parents=True, exist_ok=True)
_JOURNAL_LOG = _WORKSPACE / "gui_journal.log"


def _log(msg: str) -> None:
    try:
        with _JOURNAL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass


_log("journal starting")

source_root = Path(__file__).resolve().parents[1] / "src"
if source_root.is_dir():
    sys.path.insert(0, str(source_root))

os.environ.setdefault("NX_MCP_WORKSPACE", str(_WORKSPACE))
os.environ.setdefault("NX_MCP_BRIDGE_STOP_FILE", str(_WORKSPACE / "stop.txt"))
os.environ.setdefault("NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE", "1")

try:
    from nx_mcp.nx_bridge import (
        bridge_is_active,
        pump_bridge,
        start_bridge,
        stop_bridge,
    )  # noqa: E402
    _log("import nx_mcp ok")
except Exception:
    _log("import nx_mcp FAILED: " + traceback.format_exc())
    raise


def main() -> None:
    _log("main() entered")
    workspace = os.environ["NX_MCP_WORKSPACE"]
    stop_file = os.environ["NX_MCP_BRIDGE_STOP_FILE"]
    try:
        descriptor = start_bridge(workspace)
    except Exception:
        _log("start_bridge FAILED: " + traceback.format_exc())
        raise
    _log(f"bridge started {descriptor.host}:{descriptor.port} ({descriptor.nx_version})")
    print(
        f"NX MCP bridge started on {descriptor.host}:{descriptor.port} "
        f"({descriptor.nx_version})"
    )
    destination = Path(stop_file)
    print(f"NX MCP bridge waiting for stop file: {destination}")
    try:
        while not destination.exists() and bridge_is_active():
            pump_bridge(timeout=0.1)
    finally:
        stop_bridge()


if __name__ == "__main__":
    main()
