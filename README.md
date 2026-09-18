# NX MCP — Enhanced Edition (V2)

> **Original project**: [DreamEnding/NX_MCP](https://github.com/DreamEnding/NX_MCP) — MIT License.
> This is an **enhanced edition** that adds a C# resident NXOpen backend
> (`NX_MCP_Loader`), extended modeling tools, and keeps the original Python
> architecture as a fallback. See [License](#license) and [Credits](#credits).

NX MCP is a local Model Context Protocol server for Siemens NX automation.
Everything runs on your own machine; no cloud service is involved.

---

## Recommended architecture (V2)

The recommended V2 backend is a **C# NXOpen add-in that stays resident for the
whole NX session**:

```text
MCP client
  -> Python MCP sidecar
  -> LoaderBridge
  -> named pipe (localhost)
  -> NX_MCP_Loader.dll  (loaded automatically at NX start)
  -> SendMessage -> NX main thread
  -> NXOpen
  -> Siemens NX
```

Properties:

- **NX loads the DLL automatically** on startup — no `Alt+F8`, no Journal.
- The Loader is always ready for the next command; you can send many modeling
  tasks in a row without restarting NX.
- After a task finishes, NX returns to normal manual editing immediately; the
  Loader stays `ready:true`.
- The Python sidecar picks the backend automatically (`NX_MCP_BACKEND=auto`):
  it tries the Loader first and falls back to the Python bridge only when the
  Loader is not running.

### Three modes

| Mode | Status | When to use |
| --- | --- | --- |
| **C# NX_MCP_Loader** (resident) | **Recommended** | Normal modeling. Auto-loads at NX start, zero Alt+F8, continuous tasks. |
| **Batch Journal** | Fallback | Compatibility, debugging, one-shot scripted `.prt` + `.step` output. No persistent bridge lock. |
| **Python visual bridge** | Legacy | Kept for compatibility; no longer the recommended main flow. |

`NX_MCP_BACKEND=auto` is the default and preferred setting.

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

`nx_release` only clears the current task state and returns NX to normal editing;
it does **not** unload the resident Loader (the Loader stays ready for the next
task).

### Geometry inspection

`nx_list_edges` and `nx_list_faces` return real geometry so callers can pick the
correct edge / face index by position before targeting `nx_shell`,
`nx_edge_blend`, or `nx_chamfer`:

- `nx_list_edges(body_id)` → per edge: `index`, `tag`, `curve_type`, `start`,
  `end`, `midpoint`, `length`, `bbox_min`, `bbox_max`, `direction`,
  `adjacent_faces`.
- `nx_list_faces(body_id)` → per face: `index`, `tag`, `face_type`, `centroid`,
  `area`, `normal`, `adjacent_edges`. Centroid and area come from
  `Session.Measurement.GetFaceProperties`. On NX 2506 `normal` may be `null`
  for some faces; centroid and area remain valid.

> Edge `index`, face `index`, and `tag` are diagnostic identifiers for the
> current NX session and current topology only. After any topology change
> (extrude, shell, blend, unite, undo, reopen) re-query `nx_list_edges` /
> `nx_list_faces` and do not reuse old indices.

The original legacy tools outside this certified surface remain hidden by
default.

---

## Install (quick)

See [INSTALL.md](INSTALL.md) for the full procedure. Essentials:

```powershell
cd <repo-root>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# build and deploy the C# Loader (see INSTALL.md)
.\loader\build.bat
```

Requirements: Windows, a local Siemens NX installation (validated on **NX
2506**), Python 3.10+. Paths are auto-detected; no hardcoded machine paths.

---

## Paths (auto-detected)

| Item | Resolution order |
| --- | --- |
| NX install (`UGII_BASE_DIR`) | `UGII_BASE_DIR` env → Machine registry → `PATH` → `%ProgramFiles%\Siemens\NX*` |
| Workspace (`NX_MCP_WORKSPACE`) | `NX_MCP_WORKSPACE` env → `%USERPROFILE%\NX_MCP_WORKSPACE` |
| License server | `UGS_LICENSE_SERVER` env → `27800@localhost` default |

STEP export uses the NX STEP translator with `InputFile`,
`ObjectTypes.Solids`, `ExportAs = Ap214`, and `SettingsFile =
<UGII_BASE_DIR>\STEP214UG\ugstep214.def` (auto-located).

---

## Known limitations

- **Threaded Hole (ISO metric internal thread) is not supported yet.** On the
  validated NX 2506 environment, `HolePackageBuilder` / `ThreadBuilder` cannot
  load the standard thread table through NXOpen ("公差需要三个数" / "找不到标准数据").
  No degraded "plain hole + text label" fallback is offered.
- **Sweep, Loft, Draft, Spline, and complex free-form surfaces** are not part of
  this V2 scope.
- **Batch DSL** is weaker than the Loader mode: it only covers a small subset of
  the certified tools and stacks features along +Z.
- **STEP export** may briefly lag while the translator writes the file
  asynchronously.
- **Object IDs** are session-scoped; re-query after undo / part changes.
- **Real-NX certification** covers the tool list above on **NX 2506 / Windows**;
  other NX versions are not formally validated.

---

## 二维工程图 → NX 自动建模

可选 Agent Pack 能将二维机械工程图自动解析、规划并在 NX 中执行。

- 安装：`.\install-agent.ps1`
- 使用：上传工程图 → “开始建模”
- 详细说明：`docs/DRAWING_TO_NX.md`

NX_MCP 本身可以独立使用，Agent Pack 是可选增强层。

---

## Original project content

The original architecture, security model, and quality gates are preserved:

- [Architecture](docs/architecture.md)
- [Real NX validation](docs/real-nx-validation.md)

Security model (unchanged): IPC binds to `127.0.0.1` with a random 256-bit
session token; every file argument is confined to `NX_MCP_WORKSPACE`; legacy
tools are disabled by default.

### Credits

- Original author and maintainer: DreamEnding — [NX_MCP](https://github.com/DreamEnding/NX_MCP)
- This enhanced edition adds the resident C# `NX_MCP_Loader` backend, the
  extended certified tool set (counterbore / countersink holes, unite, revolve,
  mirror, linear / circular pattern, shell), and auto-detected paths.

### License

MIT License. Copyright (c) 2026 NX MCP contributors. See [LICENSE](LICENSE).
Original project content remains under its own MIT license.
