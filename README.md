# NX MCP Server

> **Original project**: [DreamEnding/NX_MCP](https://github.com/DreamEnding/NX_MCP) — MIT License.
> This checkout is an **enhanced edition** that keeps the original architecture
> (MCP sidecar + NX bridge) and adds two runnable workflows plus extended modeling
> tools. See [License](#license) and [Credits](#credits).

NX MCP is a local Model Context Protocol server for Siemens NX automation:

```text
MCP client <--stdio--> Python sidecar <--authenticated loopback JSON-RPC--> NX bridge <--NXOpen--> NX
```

The sidecar can start without NX. Tool calls fail with `NX_BRIDGE_UNAVAILABLE`
until an NX journal starts the bridge.

---

## Two workflows

This edition ships two complete workflows. Both run on a local Siemens NX
installation; no cloud service is involved.

### 1. Visual bridge mode (interactive, step by step)

Start a persistent bridge inside NX, then drive modeling from an MCP client.
Each tool call executes in the visible NX window; after every call the bridge
releases the NX GUI so the part stays manually editable between steps. Finish
with the `nx_release` tool to stop the bridge and keep editing normally.

**Start** — inside NX press `Alt+F8`, pick
`examples/start_nx_bridge_gui.py`, run. The journal auto-detects the workspace
(see [Paths](#paths)) and serves MCP requests until `nx_release` or the stop
file is created.

```json
{
  "mcpServers": {
    "nx-mcp": {
      "command": "<abs path to .venv\\Scripts\\python.exe>",
      "args": ["-m", "nx_mcp.server"],
      "env": { "NX_MCP_WORKSPACE": "<workspace>" }
    }
  }
}
```

**End** — call the `nx_release` tool; the bridge stops about a second later and
the NX GUI is fully editable without restarting NX.

### 2. Batch mode (scripted, no bridge, no lock)

Define a model as JSON, run one journal, get `.prt` + `.step`. No bridge, no
persistent process, NX is never locked.

1. Write `batch_task.json` in the workspace (see
   [examples/batch_task.sample.json](examples/batch_task.sample.json)).
2. Inside NX press `Alt+F8`, pick `examples/batch_build_gui.py`, run.
3. The journal reads the task, builds the model, saves the `.prt`, exports the
   `.step`, writes `batch_result.json`, and exits.

Supported batch features: `rect_extrude` (create/unite/subtract), `hole`,
`edge_blend`, `chamfer`.

---

## Extended modeling tools

The certified tool list is 21 tools (original 16 + 5 modeling extensions):

- Status: `nx_status`
- Files: `nx_create_part`, `nx_open_part`, `nx_save_part`, `nx_close_part`, `nx_export_step`
- Queries: `nx_list_sketches`, `nx_list_bodies`, `nx_list_features`
- Sketch: `nx_create_sketch`, `nx_sketch_line`, `nx_sketch_rectangle`,
  `nx_sketch_circle`, `nx_sketch_arc`, `nx_finish_sketch`
- Modeling: `nx_extrude` (create/unite/subtract), `nx_hole`, `nx_edge_blend`, `nx_chamfer`
- Recovery/view: `nx_undo`, `nx_fit_view`, `nx_release`

The original 34 legacy tools outside the certified surface remain unverified and
hidden by default. `NX_MCP_ENABLE_EXPERIMENTAL=1` registers them through the
bridge; Journal tools additionally require `NX_MCP_ENABLE_JOURNAL=1`.

---

## Paths

All machine-specific paths are auto-detected; nothing is hardcoded.

| Item | Resolution order |
| --- | --- |
| NX install (`UGII_BASE_DIR`) | `UGII_BASE_DIR` env → `PATH` (`ugraf.exe` / `run_journal.exe`) → `%ProgramFiles%\Siemens\NX*` / `%ProgramW6432%\Siemens\NX*` |
| Workspace (`NX_MCP_WORKSPACE`) | `NX_MCP_WORKSPACE` env → `%USERPROFILE%\NX_MCP_WORKSPACE` |
| License server | `UGS_LICENSE_SERVER` env → `27800@localhost` default |

`start_bridge_gui.bat` / `start_bridge_elevated.bat` implement the NX install
detection and launch NX with the correct journal. The journals
(`examples/start_nx_bridge_gui.py`, `examples/batch_build_gui.py`) resolve the
workspace themselves, so running them directly from NX's `Alt+F8` dialog works
without any launcher.

STEP export uses the NX STEP translator with `InputFile`, `ObjectTypes.Solids`,
`ExportAs = Ap214`, and `SettingsFile = <UGII_BASE_DIR>\STEP214UG\ugstep214.def`
(auto-located through `UGII_BASE_DIR`).

---

## Installation

See [INSTALL.md](INSTALL.md) for a from-scratch setup, or the essentials:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Requirements: Windows with a local Siemens NX installation (validated on NX
2506), Python 3.10+, and a workspace directory. The NX side has no `mcp` or
`pydantic` dependency; the bundled journals load the checkout's `src` directory
automatically.

---

## Validation status

Automated regression run on 2026-09-17, Siemens NX 2506, batch + visual bridge:

| Check | Result |
| --- | --- |
| Batch mode: `batch_build_gui.py` (100×60×10 base, 60×40×30 boss, Ø12 hole, 24×16×6 subtract, C2 chamfer ×38 edges, R3 blend ×79 edges) | ✅ PRT 913 KB + STEP 227 KB, auto workspace detection |
| Visual bridge: circle sketch + cylinder | ✅ |
| Visual bridge: arc sketch | ✅ |
| Visual bridge: boolean subtract | ✅ |
| Visual bridge: hole | ✅ |
| Visual bridge: edge blend | ✅ |
| Visual bridge: chamfer | ✅ |
| STEP export (Ap214, solids) | ✅ valid ISO-10303-21, manifold solid |
| `nx_release` | ✅ NX GUI unlocked, bridge stopped, no restart needed |

The Python unit suite (`pytest -m "not real_nx"`) is independent of NX and
covered by CI.

---

## Known limitations

- **Batch features**: `rect_extrude` and `hole` build on top of the previous
  feature (stacked along +Z); the first `rect_extrude` creates the body, later
  ones unite. Offset planes for arbitrary positions are not part of the batch DSL.
- **Edge blend / chamfer**: applied per-edge to remain robust; a body with many
  edges produces many small features in the part navigator. Edges that NX
  rejects (e.g. already-blended edges) are skipped and reported in the journal.
  Prefer chamfer before blend when both target the same edges.
- **Visual bridge is a journal**: NX shows a "working" indicator while the
  bridge is up. The GUI remains responsive and editable between MCP calls; the
  indicator disappears after `nx_release`.
- **Units**: always pass `units="mm"` (or inch) explicitly when creating a part.
  NX_MCP does not guess units.
- **Object IDs** are session-scoped, not persistent asset IDs; re-query after
  undo, rollback, or a part change.
- **Real-NX certification** covers the tool list and workflows above on NX 2506;
  other NX versions are not formally validated.

---

## Original project content

The original architecture, security model, and quality gates are preserved:

- [Architecture](docs/architecture.md)
- [Real NX validation](docs/real-nx-validation.md)
- [USD validation example](docs/usd-validation.md)
- [0.2 migration](docs/migration-0.2.md)
- [MCP SDK v2 migration](docs/migration-mcp-sdk-2.md)

Security model (unchanged): IPC binds to `127.0.0.1` with a random 256-bit
session token; every file argument is confined to `NX_MCP_WORKSPACE`; journal
execution and legacy tools are disabled by default.

### Credits

- Original author and maintainer: DreamEnding — [NX_MCP](https://github.com/DreamEnding/NX_MCP)
- This enhanced edition adds the visual bridge workflow (`nx_release`), the
  batch workflow (`batch_build_gui.py`), the five modeling extensions
  (circle/arc sketch, hole, edge blend, chamfer, boolean subtract), and
  auto-detected paths.

### License

MIT License. Copyright (c) 2026 NX MCP contributors. See [LICENSE](LICENSE).
