# NX MCP — Enhanced Edition (V2)

> **Original project**: [DreamEnding/NX_MCP](https://github.com/DreamEnding/NX_MCP) — MIT License.
> This is an **enhanced edition** that adds a C# resident NXOpen backend
> (`NX_MCP_Loader`), extended modeling tools, and an integrated Agent Pack for
> drawing-to-NX automation.

NX MCP is a local Model Context Protocol server for Siemens NX automation.
Everything runs on your own machine; no cloud service is involved.

---

## Recommended architecture (V2)

```text
Doubao / Agent Pack
  -> Drawing Reader
  -> Modeling Planner
  -> Plan Runner
  -> Python MCP sidecar
  -> LoaderBridge
  -> named pipe (localhost)
  -> NX_MCP_Loader.dll
  -> NXOpen
  -> Siemens NX
```

The resident Loader auto-loads at NX startup, so the normal flow does not need
`Alt+F8` or a Journal. `NX_MCP_BACKEND=auto` prefers the Loader and falls back
to the legacy Python bridge only when needed.

---

## Certified tools (32)

Real-machine certified on Siemens NX 2506, Windows.

| Group | Tools |
| --- | --- |
| Files | `nx_create_part`, `nx_open_part`, `nx_save_part`, `nx_close_part`, `nx_export_step` |
| Status / queries | `nx_status`, `nx_list_sketches`, `nx_list_bodies`, `nx_list_features` |
| Geometry inspection | `nx_list_edges`, `nx_list_faces` |
| Sketch | `nx_create_sketch`, `nx_sketch_line`, `nx_sketch_rectangle`, `nx_sketch_circle`, `nx_sketch_arc`, `nx_finish_sketch` |
| Extrude | `nx_extrude` (create / unite / subtract) |
| Holes | `nx_hole`, `nx_counterbore_hole`, `nx_countersink_hole` |
| Features | `nx_unite`, `nx_revolve`, `nx_mirror`, `nx_linear_pattern`, `nx_circular_pattern`, `nx_shell` |
| Edges | `nx_edge_blend`, `nx_chamfer` |
| View / recovery | `nx_undo`, `nx_fit_view`, `nx_release` |

---

## Install — integrated one-click flow

Requirements:

- Windows 10/11
- Siemens NX installed (validated on **NX 2506**)
- Python 3.10+
- Doubao desktop installed

After cloning the repository, run **one command** from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer automatically completes:

1. Python virtual environment + NX_MCP-Enhanced sidecar
2. Workspace creation
3. `UGII_USER_DIR` configuration for the current Windows user
4. C# Loader build
5. Loader deployment to `%UGII_USER_DIR%\startup`
6. Drawing Reader Skill installation
7. Modeling Planner Skill installation
8. Pipeline Skill installation
9. Plan Runner installation and lightweight tests

For multiple Doubao profiles:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DoubaoProfile "Profile 7"
```

If Siemens NX was already running before installation, restart it once so the
new process can read `UGII_USER_DIR` and auto-load the newly deployed Loader.
If NX was not running, simply start it after installation.

Then open a new Doubao conversation, upload a 2D mechanical drawing, and send:

```
开始建模
```

See [INSTALL.md](INSTALL.md) for details and manual/advanced installation.

---

## Paths (auto-detected)

| Item | Resolution order |
| --- | --- |
| NX install (`UGII_BASE_DIR`) | `UGII_BASE_DIR` env → registry → `PATH` → `%ProgramFiles%\Siemens\NX*` |
| Workspace (`NX_MCP_WORKSPACE`) | environment variable → `%USERPROFILE%\NX_MCP_WORKSPACE` |
| NX user dir (`UGII_USER_DIR`) | existing environment → otherwise installer sets `%USERPROFILE%\.nx_mcp_user` |
| Loader startup | `%UGII_USER_DIR%\startup` |

---

## Known limitations

- Threaded holes / real thread geometry are not supported.
- Sweep, Loft, Draft, Spline, involute gears and complex free-form surfaces are
  outside the current certified scope.
- Real-NX certification is currently based on **NX 2506 / Windows**; other NX
  versions may work but are not formally validated.
- STEP export may briefly lag while the NX translator finishes writing.

---

## 二维工程图 → NX 自动建模

The Agent Pack is **bundled in this repository and installed automatically by
`install.ps1`**. It provides:

- `nx-engineering-drawing-reader`: engineering drawing → structured JSON
- `nx-mcp-modeling-planner`: JSON → executable modeling plan
- `nx-mcp-pipeline`: one-command A → B → C orchestration
- `nx-mcp-plan-runner`: deterministic plan execution in NX

Detailed usage: [docs/DRAWING_TO_NX.md](docs/DRAWING_TO_NX.md)

The NX_MCP core can still be used independently; `install-agent.ps1` remains
available as an advanced helper to reinstall only the Agent Pack.

---

## Credits

- Original project: DreamEnding/NX_MCP
- Enhanced edition / integrated Agent Pack: NX MCP contributors

## License

MIT License. See [LICENSE](LICENSE).
Original project content remains under its own MIT license.
