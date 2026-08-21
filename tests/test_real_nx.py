"""Acceptance test for a dedicated runner with a live Siemens NX session."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nx_mcp.real_smoke import run

pytestmark = pytest.mark.real_nx


@pytest.mark.asyncio
async def test_real_nx_certified_workflow() -> None:
    if os.environ.get("NX_MCP_REAL_NX") != "1":
        pytest.skip("requires NX_MCP_REAL_NX=1 on a dedicated Siemens NX runner")

    workspace = Path(os.environ["NX_MCP_WORKSPACE"]).resolve()
    iterations = int(os.environ.get("NX_MCP_REAL_NX_ITERATIONS", "20"))
    prefix = os.environ.get("NX_MCP_REAL_NX_RUN_PREFIX", "pytest-real-nx")
    workspace.mkdir(parents=True, exist_ok=True)

    results = await run(workspace, iterations, prefix)

    assert len(results) == iterations
    assert all(result["nx_version"] for result in results)
    assert all(result["feature_id"] and result["body_id"] for result in results)
