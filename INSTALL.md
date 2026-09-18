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
9. Installs the unified `nx-agent` Skill
10. Installs the generic Plan Runner
11. Runs lightweight Agent Pack tests

No separate MCP-client JSON configuration is required for the bundled Agent Pack workflow.

### Important: first NX start after installation

`UGII_USER_DIR` is read when Siemens NX starts.

- If NX was **not running** during installation: start NX normally afterward.
- If NX was **already running** during installation: save your work and restart
  NX once after installation.

You should no longer need to manually create or set `UGII_USER_DIR`; the
installer does that automatically.

After NX is started with the new environment, two workflows are available:

### Text-description modeling

Open a new Agent conversation and directly describe the NX model you want to create or edit. The unified `nx-agent` converts the text request into a modeling plan and executes it through the installed Plan Runner and resident Loader; no extra MCP-client JSON is required.

Example:

```
在 NX 中创建一个 100×60×10 mm 的底板，并在四角各打一个 Ø8 通孔。
```

### 2D engineering drawing modeling

Open a new Agent conversation, upload a 2D mechanical engineering drawing, and send:

```
开始建模
```

The drawing workflow is:

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

- `nx-agent` — the single user-facing Skill for text modeling, safe edits of explicitly selected saved workspace parts, and 2D drawing modeling
- `nx-mcp-plan-runner` — the deterministic execution runtime used internally by both text-description and drawing workflows

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
- the unified Skill frontmatter name is correct
- Plan Runner files and runtime-config exist
- Plan Runner lightweight tests and Agent Pack static verification pass

It deliberately does **not** execute a real modeling task during installation.

## Uninstall / rollback notes

- Loader only: run `loader\uninstall.bat` and restart NX.
- Agent Pack only: remove the installed `nx-agent` Skill folder and
  `%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner`.
- Repository venv: remove `.venv` if you no longer need this installation.
