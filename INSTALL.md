# NX MCP — Installation Guide (V2)

From-scratch setup on Windows with a local Siemens NX installation.

Validated environment: **Siemens NX 2506 on Windows**. Other NX versions are
not formally certified; paths are auto-detected, but please validate on your
own install.

## Prerequisites

- Windows 10/11
- Siemens NX installed (`ugraf.exe` present)
- .NET SDK / MSBuild that ships with the NX .NET runtime (used by
  `loader\build.bat`)
- Python 3.10+ (the sidecar interpreter; separate from NX's embedded Python)

## 1. Install the Python sidecar

```powershell
cd <repo-root>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The `-e` install links `src` into the venv.

## 2. Set up the workspace

The workspace is the only directory MCP may write into.

```powershell
# optional: point NX_MCP_WORKSPACE anywhere you like
setx NX_MCP_WORKSPACE "%USERPROFILE%\my_nx_workspace"
# if unset, tools default to %USERPROFILE%\NX_MCP_WORKSPACE
```

## 3. Build and deploy the C# Loader (recommended)

The Loader is a small NXOpen .NET add-in DLL. It loads automatically when NX
starts, so there is no `Alt+F8` and no Journal in the normal flow.

```powershell
# 3a. build the DLL (UGII_BASE_DIR is auto-detected if unset)
.\loader\build.bat

# 3b. deploy into your NX user startup directory
#     default NX user dir is %USERPROFILE%\nx_mcp_user (or your UGII_USER_DIR)
copy loader\NX_MCP_Loader.dll  %USERPROFILE%\.nx_mcp_user\startup\
```

On NX start the Loader opens a localhost named pipe and logs
`pipe thread + scheduler window started` to
`%USERPROFILE%\NX_MCP_WORKSPACE\nx_mcp_loader.log`.

Verify the Loader is ready:

```powershell
.\.venv\Scripts\python.exe -m nx_mcp.server   # then call nx_status
# expected: ready:true, nx_version:"2506"
```

To **uninstall** the Loader, delete the DLL from
`%USERPROFILE%\.nx_mcp_user\startup\` (or run `loader\uninstall.bat`); NX then
starts without it and the sidecar falls back automatically.

## 4. Configure your MCP client

```json
{
  "mcpServers": {
    "nx-mcp": {
      "command": "<abs path to repo>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "nx_mcp.server"],
      "env": {
        "NX_MCP_WORKSPACE": "<workspace>",
        "NX_MCP_BACKEND": "auto"
      }
    }
  }
}
```

`NX_MCP_BACKEND=auto` (default) prefers the C# Loader and falls back to the
Python bridge only when the Loader is not running.

Now model from your client: create a part, sketch, extrude, add features, save
PRT, export STEP. After each task NX stays editable and the Loader remains
`ready:true`; you can send the next task without restarting NX.

## 5. Compatibility: Batch Journal (no persistent bridge)

For one-shot scripted output:

1. Write `batch_task.json` in the workspace (copy
   `examples\batch_task.sample.json`).
2. In NX press `Alt+F8`, pick `examples\batch_build_gui.py`, run.
3. Find `<part>.prt`, `<part>.step`, `batch_result.json` in the workspace.

There is no persistent bridge lock; the journal completes and NX returns to
normal.

## 6. Compatibility: Python visual bridge (legacy)

Kept for compatibility. Inside NX press `Alt+F8`, pick
`examples\start_nx_bridge_gui.py`, run. Call `nx_release` to stop the bridge and
restore manual editing. This mode blocks NX GUI while the bridge is up; prefer
the C# Loader for interactive work.

## 7. Optional: license server

If your license server differs from `27800@localhost`:

```powershell
setx UGS_LICENSE_SERVER "27000@lic-server"
```

## Verification

Unit suite (no NX required):

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
```

Real-NX smoke: start NX (Loader auto-loads), then call `nx_status`
(`ready:true`) and create a simple part through your MCP client.
