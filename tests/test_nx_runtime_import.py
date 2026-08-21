"""Checks for modules that must load in NX's embedded Python runtime."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys


def test_nx_bridge_import_has_no_third_party_runtime_dependency() -> None:
    """NX 2506 ships Python without the sidecar's site-packages directory."""
    source_root = Path(__file__).parents[1] / "src"
    environment = os.environ | {"PYTHONPATH": str(source_root)}

    result = subprocess.run(
        [sys.executable, "-S", "-c", "import nx_mcp.nx_bridge"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
