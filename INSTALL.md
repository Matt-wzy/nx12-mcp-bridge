# NX MCP — Installation Guide

From-scratch setup on Windows with a local Siemens NX installation.

## Prerequisites

- Windows 10/11
- Siemens NX installed (validated on NX 2506). Any NX that ships `ugraf.exe`
  and `run_journal.exe` should work; paths are auto-detected, no registry edits.
- Python 3.10+ (the sidecar interpreter; NX's own embedded Python is separate)

## 1. Install the sidecar

```powershell
cd <repo-root>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The `-e` install links `src` into the venv; the NX journals also load `src`
directly via `sys.path`, so the NX side needs no package installation.

## 2. Set up the workspace

The workspace is where NX MCP may create, save, and export files. It is the
only directory the MCP server will write into.

```powershell
# optional: point NX_MCP_WORKSPACE anywhere you like
setx NX_MCP_WORKSPACE "%USERPROFILE%\my_nx_workspace"
# if unset, the tools default to %USERPROFILE%\NX_MCP_WORKSPACE
```

## 3. Launch the visual bridge (interactive mode)

Option A — launcher (auto-detects NX install and workspace):

```powershell
.\start_bridge_gui.bat
```

Option B — inside NX: press `Alt+F8`, pick
`examples\start_nx_bridge_gui.py`, run. The journal auto-detects the workspace.

Then configure your MCP client (e.g. Claude Desktop, Cursor, or any MCP client)
to start the sidecar:

```json
{
  "mcpServers": {
    "nx-mcp": {
      "command": "<abs path to repo>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "nx_mcp.server"],
      "env": { "NX_MCP_WORKSPACE": "<workspace>" }
    }
  }
}
```

Use the client to call `nx_create_part`, sketch/extrude, etc. When done, call
`nx_release` to unlock the NX GUI.

## 4. Batch mode (no bridge, no lock)

1. Write `batch_task.json` in the workspace (copy
   `examples\batch_task.sample.json` and edit it).
2. In NX press `Alt+F8`, pick `examples\batch_build_gui.py`, run.
3. Find `batch_result.json`, `<part>.prt` and `<part>.step` in the workspace.

## 5. Optional: auto-start on NX launch

Copy `examples\batch_build_gui.py` (or `start_nx_bridge_gui.py`) into NX's
`<user_dir>\startup` directory. Journals there run automatically when NX
starts. `batch_build_gui.py` only acts when `batch_task.json` exists, so an
idle auto-start is safe.

## 6. Optional: license server

If your NX license server differs from `27800@localhost`, set:

```powershell
setx UGS_LICENSE_SERVER "27000@lic-server"
```

## Verification

Run the unit suite (no NX required):

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
```

Real-NX smoke: start the visual bridge, then from a PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe <workspace>\nx_capability_test.py
```

This drives circle/arc/hole/subtract/blend/chamfer/STEP and ends with
`nx_release`. NX should become fully editable after it finishes.
