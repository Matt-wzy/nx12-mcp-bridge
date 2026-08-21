# Real NX validation gate

## Latest validated run

- Date: 2026-08-21
- NX: v2506 (`ugraf.exe` 2506.4021; `run_journal.exe` 2506.4000)
- NX embedded Python: 3.12.9
- Sidecar: Python 3.12.7 with `mcp` 1.27.0
- Host: Windows 11 build 26200
- Mode: `run_journal.exe` batch journal with main-thread request pumping
- Result: one acceptance run and 20 consecutive runs passed.

This validates the Python bridge for the recorded batch environment. It does
not validate non-blocking interactive NX GUI responsiveness.

## Record before testing

- Exact NX release/build and installed maintenance pack
- NX Python version and architecture
- Sidecar Python and `mcp` versions
- Whether NX is native or Teamcenter-managed mode
- Test machine identifier and Windows version

Version 0.2 initially supports only this recorded NX build and native parts.

## Preconditions

- Use a disposable `NX_MCP_WORKSPACE`; do not copy production parts into it.
- Install this package in the sidecar interpreter. The supplied NX journal
  examples load the checkout's `src` directory and require only NX's standard
  Python library, not `mcp` or `pydantic`.
- Before starting the bridge, set `NX_MCP_PROBE_OUTPUT` to a JSON file inside
  the disposable workspace and run `examples/nx_runtime_probe.py` as an NX
  journal. It must report `"bridge_import_error": null`.
- Enable `NX_MCP_ALLOW_UNVERIFIED_PYTHON_BRIDGE=1` only during feasibility.

Set `NX_MCP_BRIDGE_STOP_FILE` to a new path inside the disposable workspace
before running `start_nx_bridge.py` with `run_journal.exe`. The journal pumps
each bridge request on NX's main thread, keeps NX alive until that file is
created, then stops the bridge cleanly. The supplied Python runner requires
this batch mode.

## Acceptance command

```powershell
python -m nx_mcp.real_smoke --workspace D:\NX_MCP_WORKSPACE --iterations 20 --run-prefix acceptance
```

Every iteration must connect, create a metric part, create and finish an XY
rectangle sketch, extrude a new body, query the result, fit the view, export
STEP, undo the extrude, verify the original body count, save, and close. A
prefix must be unique for each rerun because NX will not overwrite a part.

## Pass criteria

- All 20 iterations pass without retry.
- The batch journal does not crash or hang.
- Every STEP and part file stays within the workspace.
- No partial geometry remains after a failed command or undo.
- Bridge stop/start and NX restart are followed by successful reconnection.
- Invalid token, path traversal, no work part, wrong-kind ID, and stale ID fail
  with their documented error codes.

If clean unload or GUI responsiveness fails, do not remove the feasibility
gate. Implement the minimal C# NX-side bridge or a non-blocking UI scheduler
and rerun this entire matrix before changing the package version from
`0.2.0.dev0` to `0.2.0`.

## GitHub Actions self-hosted gate

The repository provides `.github/workflows/real-nx.yml` for this acceptance
gate. It is intentionally manual so ordinary pull requests are not blocked
until a dedicated NX runner exists. The runner must have the labels
`self-hosted`, `windows`, and `nx`, plus a runner-level `NX_RUN_JOURNAL`
environment variable containing the absolute path to `run_journal.exe`.

The workflow creates a disposable workspace, runs the embedded-runtime probe,
starts the Python bridge, then runs `pytest -m real_nx`. It requests the bridge
to stop even when acceptance fails. Once the runner is reliable, make this
workflow a required release/branch gate in the repository settings; the normal
hosted CI deliberately excludes `real_nx` because it cannot provide Siemens NX.
