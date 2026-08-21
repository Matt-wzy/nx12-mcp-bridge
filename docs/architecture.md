# NX MCP 0.2 architecture

## Runtime boundary

The MCP sidecar owns stdio, type validation, structured MCP results, and path
resolution. It never imports NXOpen. The manually loaded NX bridge owns the
live NX session, object registry, undo marks, builders, and all NXOpen calls.

The bridge listens on `127.0.0.1` using a random port. Its descriptor contains
the protocol version, host, port, random token, NX PID, and exact NX version.
The sidecar reloads the descriptor for every call, so restarting NX does not
leave a permanently disconnected singleton.

Requests are newline-delimited JSON-RPC objects containing `protocol_version`,
`id`, `token`, `method`, and `params`. Responses echo the ID and contain either
`result` or a stable error with `code`, `message`, optional `suggestion`, and
optional native `nx_code`. Payloads are limited to 1 MiB and calls time out
after 120 seconds.

## Object and operation lifecycle

NX objects are returned as `{id, kind, name, part_id}`. IDs map to live NXOpen
objects inside the bridge and are invalidated when their part closes. Commands
reject unknown, stale, or wrong-kind IDs instead of guessing by display name.

Model mutations are serialized. Each mutation creates a visible undo mark and
rolls back to it when execution fails. Successful marks are tracked so
`nx_undo` affects the last NX MCP mutation rather than an unrelated user
operation; saving clears these native marks. Builders are destroyed from
`finally` blocks. File operations are independently confined to the configured
workspace on both sides of the process boundary.

## Certification boundary

`server.py` explicitly registers the 16 certified tools. Legacy modules under
`tools/` are imported only when `NX_MCP_ENABLE_EXPERIMENTAL=1` is set on both
processes; Journal tools require `NX_MCP_ENABLE_JOURNAL=1` as well. A tool may
join the default surface only after strict boundary tests and a real-NX
contract test pass for the target build.

The listener thread only queues requests. `pump_bridge()` executes them on the
NX journal's main thread, which is required by NXOpen. The bundled runner pumps
while waiting for `NX_MCP_BRIDGE_STOP_FILE`, so it is a batch feasibility path,
not an interactive GUI integration. If a target build cannot provide a
non-blocking GUI scheduler, the NX-side executor moves to a minimal C# plugin;
the JSON-RPC and MCP contracts remain unchanged.
