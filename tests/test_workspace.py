from pathlib import Path

import pytest

from nx_mcp.workspace import Workspace, WorkspaceViolation


def test_workspace_accepts_relative_paths_inside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)

    assert workspace.resolve("parts/bracket.prt") == tmp_path / "parts" / "bracket.prt"


@pytest.mark.parametrize("path", ["../outside.prt", "C:/outside.prt"])
def test_workspace_rejects_paths_outside_root(tmp_path: Path, path: str):
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceViolation, match="workspace"):
        workspace.resolve(path)


def test_workspace_accepts_absolute_path_already_resolved_inside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)
    safe_path = tmp_path / "parts" / "bracket.prt"

    assert workspace.ensure_inside(safe_path) == safe_path


def test_workspace_rejects_absolute_path_outside_root(tmp_path: Path):
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceViolation, match="workspace"):
        workspace.ensure_inside(tmp_path.parent / "outside.prt")
