# NX MCP Server

NX MCP is a local Model Context Protocol server for Siemens NX automation. The
`0.2.0.dev0` line replaces the unverified direct-attach design with two explicit
processes:

```text
MCP client <--stdio--> Python sidecar <--authenticated loopback JSON-RPC--> NX bridge <--NXOpen--> NX
```

The sidecar can start without NX. Tool calls fail with `NX_BRIDGE_UNAVAILABLE`
until an NX journal starts the bridge.

## Current status

The sidecar, bridge protocol, input/output schemas, workspace confinement, and
core workflow have automated coverage. The Python bridge passed the documented
20-run batch workflow on Siemens NX 2506 (`ugraf` 2506.4021) on 2026-08-21.
It remains opt-in while a non-blocking NX GUI event pump is validated; the
bundled Python Journal runner is intentionally batch-only.

The default `tools/list` exposes only these 16 tools:

- Status: `nx_status`
- Files: `nx_create_part`, `nx_open_part`, `nx_save_part`, `nx_close_part`, `nx_export_step`
- Queries: `nx_list_sketches`, `nx_list_bodies`, `nx_list_features`
- Sketch: `nx_create_sketch`, `nx_sketch_line`, `nx_sketch_rectangle`, `nx_finish_sketch`
- Modeling: `nx_extrude`
- Recovery/view: `nx_undo`, `nx_fit_view`

The 34 old tools outside the certified surface remain unverified and hidden by
default. `NX_MCP_ENABLE_EXPERIMENTAL=1` registers them through the bridge;
Journal tools additionally require `NX_MCP_ENABLE_JOURNAL=1`.

## Requirements

- Windows with a local native Siemens NX installation (validated on NX 2506)
- Python 3.10+
- The package installed in the sidecar interpreter
- An NX journal that can import `nx_mcp` (the bundled Journal examples load
  the checkout's `src` directory automatically; the NX side has no `mcp` or
  `pydantic` dependency)
- A dedicated test/project directory configured as `NX_MCP_WORKSPACE`

Install the sidecar and development dependencies:

```powershell
python -m pip install -e ".[dev]"
```

## Internal feasibility run

1. Set `NX_MCP_WORKSPACE` to a disposable directory.
2. For the target-build feasibility test only, set
   `NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE=1` in the NX environment.
3. Set `NX_MCP_BRIDGE_STOP_FILE` to a new path inside the workspace, then run
   `examples/start_nx_bridge.py` with `run_journal.exe -nx`. The journal pumps
   requests on NX's main thread and writes an authenticated session descriptor
   to `%LOCALAPPDATA%\nx-mcp\bridge.json`.
4. Configure the MCP client to launch the sidecar:

```json
{
  "mcpServers": {
    "nx-mcp": {
      "command": "python",
      "args": ["-m", "nx_mcp.server"],
      "env": {
        "NX_MCP_WORKSPACE": "D:\\NX_MCP_WORKSPACE"
      }
    }
  }
}
```

5. Run the real-NX acceptance loop from an external PowerShell 7 terminal:

```powershell
python -m nx_mcp.real_smoke --workspace D:\NX_MCP_WORKSPACE --iterations 20 --run-prefix acceptance
```

6. Create the configured stop file when finished; the journal stops the bridge
   cleanly.

Do not use production parts for this test. The batch bridge is not evidence of
interactive GUI responsiveness; use a non-blocking NX UI scheduler or the
agreed minimal C# NX-side bridge before enabling an interactive pilot.

## Security model

- IPC binds only to `127.0.0.1` on a random port and requires a random 256-bit
  session token.
- Every file argument is relative to `NX_MCP_WORKSPACE`; traversal, absolute
  paths, and resolved links outside the workspace are rejected.
- Journal execution and all 34 legacy tools are disabled by default. Both the
  sidecar and NX bridge must receive the opt-in environment flags.
- Object IDs are opaque and valid only for the current part session.

## Local quality gates

The ordinary suite does not require NX. Install the Git hooks once, then use
the same checks as CI:

```powershell
python -m pip install -e ".[dev]"
python -m pre_commit install --install-hooks
python -m pre_commit run --all-files
python -m pytest -q -p no:cacheprovider -m "not real_nx" --basetemp .pytest-tmp
```

The pre-commit hook runs file and style checks. The pre-push hook runs the
non-real-NX pytest suite and the sidecar mypy gate. Tests marked `legacy` cover
the opt-in 0.1 surface; tests marked `fake_nx` do not validate NXOpen itself.
Hosted CI runs the core suite across supported Python and OS combinations,
runs legacy mock-NX tests separately, and enforces at least 78% branch
coverage in its canonical Ubuntu/Python 3.12 coverage job.

Real NX acceptance is intentionally separate. Dispatch
`.github/workflows/real-nx.yml` from a dedicated self-hosted Windows runner
labelled `self-hosted`, `windows`, and `nx`, with `NX_RUN_JOURNAL` set to the
absolute path of `run_journal.exe`.

See [architecture](docs/architecture.md), [0.1 migration](docs/migration-0.2.md),
and [real NX validation](docs/real-nx-validation.md) for implementation and
release gates.
