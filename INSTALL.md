# NX MCP — Installation Guide (V2)

From-scratch setup on Windows with Siemens NX and Agent.

Validated environment: **Siemens NX 2506 on Windows**. Other NX versions are
not formally certified; the installer auto-detects installed NX paths.

## Prerequisites

- Windows 10/11
- Siemens NX installed (`ugraf.exe` present)
- Python 3.10+
- Agent desktop installed and launched at least once

## Recommended: integrated one-click installation

Clone the repository, enter its root directory, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

If you use multiple Agent profiles, you can explicitly choose one:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -AgentProfile "Profile 7"
```

The single installer performs the complete setup:

1. Checks Python 3.10+
2. Creates/reuses `.venv`
3. Installs NX_MCP-Enhanced and its Python dependencies
4. Creates/uses `%USERPROFILE%\NX_MCP_WORKSPACE`
5. Determines and persists `UGII_USER_DIR`
6. Auto-detects the local Siemens NX installation
7. Builds `NX_MCP_Loader.dll`
8. Deploys the Loader to `%UGII_USER_DIR%\startup`
9. Installs the bundled Drawing Reader / Modeling Planner / Pipeline Skills
10. Installs the generic Plan Runner
11. Runs lightweight Agent Pack tests

No separate MCP-client JSON configuration is required for the bundled Agent
Agent Pack workflow.

### Important: first NX start after installation

`UGII_USER_DIR` is read when Siemens NX starts.

- If NX was **not running** during installation: start NX normally afterward.
- If NX was **already running** during installation: save your work and restart
  NX once after installation.

You should no longer need to manually create or set `UGII_USER_DIR`; the
installer does that automatically.

After NX is started with the new environment:

1. Open a new Agent conversation.
2. Upload a 2D mechanical engineering drawing.
3. Send:

```
开始建模
```

The normal path is:

```text
Drawing
→ Drawing Reader
→ Modeling Planner
→ Plan Runner
→ NX_MCP-Enhanced
→ C# Loader
→ Siemens NX
→ PRT + STEP
```

## What `install.ps1` installs

### NX_MCP-Enhanced core

- Python sidecar in `.venv`
- 32 certified tools
- `NX_MCP_BACKEND=auto` runtime behavior
- workspace at `%USERPROFILE%\NX_MCP_WORKSPACE` unless overridden

### Resident Loader

The installer calls `loader\build.bat`. NX is auto-detected in this order:

1. `UGII_BASE_DIR`
2. Windows Registry
3. `PATH` / `ugraf.exe`
4. `%ProgramFiles%\Siemens\NX*`

For `UGII_USER_DIR`:

1. Existing process/user/machine environment value is respected when present.
2. If none exists, the installer uses `%USERPROFILE%\.nx_mcp_user`.
3. The chosen value is written to the current user environment and to the
   current installer process.
4. The Loader is deployed to `%UGII_USER_DIR%\startup`.

This fixes the first-install case where the DLL existed under
`~\.nx_mcp_user\startup` but NX had never been told to use that user directory.

### Integrated Agent Pack

The installer automatically runs `install-agent.ps1` internally and installs:

- `nx-engineering-drawing-reader`
- `nx-mcp-modeling-planner`
- `nx-mcp-pipeline`
- `nx-mcp-plan-runner`

`install-agent.ps1` remains available only as an advanced helper when you want
to reinstall the Agent Pack without reinstalling the NX_MCP core.

## Manual / advanced installation

If you intentionally want the core without the integrated Agent Pack:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\loader\build.bat
```

Then set `UGII_USER_DIR`, deploy `loader\NX_MCP_Loader.dll` to
`%UGII_USER_DIR%\startup`, and restart NX.

## Verification

The integrated installer verifies:

- `nx_mcp` can be imported from the repository venv
- `UGII_USER_DIR` is configured
- built and deployed Loader DLL SHA256 values match
- all three Skill frontmatter names are correct
- Plan Runner files exist
- Plan Runner lightweight tests pass

It deliberately does **not** execute a real modeling task during installation.

## Uninstall / rollback notes

- Loader only: run `loader\uninstall.bat` and restart NX.
- Agent Pack only: remove the three installed Skill folders and
  `%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner`.
- Repository venv: remove `.venv` if you no longer need this installation.
