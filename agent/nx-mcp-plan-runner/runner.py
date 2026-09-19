# -*- coding: utf-8 -*-
"""nx-mcp-plan-runner — a generic, plan-driven executor for nx-agent modeling plans.

This Runner is PART-AGNOSTIC and PLAN-AGNOSTIC:
- it never branches on step numbers, feature names, or part dimensions;
- every behaviour is driven by the plan JSON (tool_args / selection_criteria /
  expectation / topology_changes) and by the executable-format extensions
  (result_bindings / selection_binding / $references / declared retries);
- it never invents repairs: a failed step stops the run (declared retries only).

Modes:
  python runner.py validate-drawing <drawing.json>  # Gate A structural/evidence check, no NX
  python runner.py run   <plan.json> [--workspace DIR] [--report OUT.json]
  python runner.py check <plan.json> [--frozen]     # static, no NX
  python runner.py build <plan.json> <out.json>     # frozen -> executable, no NX

The Runner talks to the resident C# Loader through the existing NX_MCP-Enhanced
bridge (named pipe / SendMessage architecture is untouched). Nothing in
NX_MCP-Enhanced, the C# Loader, or the environment is modified.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as _dt
import inspect
import json
import math
import os
import re
import sys
import time
from typing import Any

# --------------------------------------------------------------------------
# certified tool contract — static data, not part-specific
# --------------------------------------------------------------------------
CERTIFIED_TOOLS = {
    "nx_status", "nx_create_part", "nx_open_part", "nx_save_part",
    "nx_close_part", "nx_export_step", "nx_list_sketches", "nx_list_bodies",
    "nx_list_features", "nx_list_edges", "nx_list_faces", "nx_create_sketch",
    "nx_sketch_line", "nx_sketch_rectangle", "nx_sketch_circle",
    "nx_sketch_arc", "nx_finish_sketch", "nx_extrude", "nx_hole",
    "nx_counterbore_hole", "nx_countersink_hole", "nx_shell", "nx_edge_blend",
    "nx_chamfer", "nx_unite", "nx_revolve", "nx_mirror", "nx_linear_pattern",
    "nx_circular_pattern", "nx_undo", "nx_fit_view", "nx_release",
}

# tool -> (required params, optional params). Derived from certified.py.
TOOL_PARAMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "nx_status": ((), ()),
    "nx_create_part": (("path",), ("units",)),
    "nx_open_part": (("path",), ()),
    "nx_save_part": ((), ()),
    "nx_close_part": ((), ("save",)),
    "nx_export_step": (("path",), ()),
    "nx_list_sketches": ((), ()),
    "nx_list_bodies": ((), ()),
    "nx_list_features": ((), ()),
    "nx_list_edges": (("body_id",), ()),
    "nx_list_faces": (("body_id",), ()),
    "nx_create_sketch": ((), ("plane", "name")),
    "nx_sketch_line": (("sketch_id", "start", "end"), ()),
    "nx_sketch_rectangle": (("sketch_id", "corner1", "corner2"), ()),
    "nx_sketch_circle": (("sketch_id", "center", "diameter"), ()),
    "nx_sketch_arc": (("sketch_id", "center", "radius"), ("start_angle", "end_angle")),
    "nx_finish_sketch": (("sketch_id",), ()),
    "nx_extrude": (("sketch_id", "distance"), ("reverse", "start_offset", "operation", "target_body_id")),
    "nx_hole": (("body_id", "center", "diameter", "depth"), ("start_offset",)),
    "nx_counterbore_hole": (("body_id", "center", "hole_diameter", "hole_depth", "counterbore_diameter", "counterbore_depth"), ("start_offset",)),
    "nx_countersink_hole": (("body_id", "center", "hole_diameter", "hole_depth", "countersink_diameter"), ("countersink_angle", "start_offset")),
    "nx_shell": (("body_id", "thickness", "remove_face_index"), ("inward",)),
    "nx_edge_blend": (("body_id", "radius"), ("edge_indices",)),
    "nx_chamfer": (("body_id", "offset"), ("edge_indices",)),
    "nx_unite": (("target_body_id", "tool_body_ids"), ()),
    "nx_revolve": (("sketch_id", "axis_start", "axis_end"), ("angle", "reverse")),
    "nx_mirror": (("body_id", "plane"), ("offset",)),
    "nx_linear_pattern": (("body_id", "direction", "count", "spacing"), ("reverse",)),
    "nx_circular_pattern": (("body_id", "axis", "center", "count", "angle"), ("reverse",)),
    "nx_undo": ((), ()),
    "nx_fit_view": ((), ()),
    "nx_release": ((), ()),
}

# params that always stay lists after $selection resolution
LIST_PARAMS = {"edge_indices", "tool_body_ids"}
# tools whose raw loader result carries a "done=N" count that the bridge drops
RAW_PIPE_TOOLS = {"nx_edge_blend", "nx_chamfer"}

RUNTIME_CONFIG_FILENAME = "runtime-config.json"


def load_runtime_config(path: str | None = None) -> dict:
    """Load installer-written runtime config; missing/invalid config is non-fatal."""
    cfg_path = path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), RUNTIME_CONFIG_FILENAME
    )
    if not os.path.isfile(cfg_path):
        return {}
    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}



DEFAULT_TOL = 0.5


class PlanError(Exception):
    """Fatal, plan-level error (stops the run)."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _approx(a: Any, b: Any, tol: float) -> bool:
    a, b = _num(a), _num(b)
    return a is not None and b is not None and abs(a - b) <= tol


def _item_id(x: Any) -> Any:
    return getattr(x, "id", x)


def _walk(value: Any):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)
    else:
        yield value


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# symbol table and reference resolution
# --------------------------------------------------------------------------
class Symbols:
    """Unified symbol table: logical names -> real ids, plus selection symbols."""

    def __init__(self) -> None:
        self.names: dict[str, Any] = {}
        self.selection: dict[str, Any] = {}

    def bind(self, name: str, value: Any) -> None:
        self.names[name] = value

    def bind_selection(self, name: str, value: Any) -> None:
        self.selection[name] = value


def resolve_value(value: Any, symbols: Symbols) -> Any:
    """Resolve $references and bare logical names recursively.

    - "$name"             -> symbols.names[name]
    - "$selection.x"      -> symbols.selection[x] (or dotted path into it)
    - a bare string that is a known logical name -> its real id
    """
    if isinstance(value, dict):
        return {k: resolve_value(v, symbols) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, symbols) for v in value]
    if isinstance(value, str):
        if value.startswith("$"):
            return _resolve_ref(value, symbols)
        if value in symbols.names:
            return symbols.names[value]
    return value


def _resolve_ref(text: str, symbols: Symbols) -> Any:
    path = text[1:].split(".")
    if path[0] == "selection":
        cur: Any = symbols.selection
        for p in path[1:]:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                raise PlanError(f"unresolved reference {text!r}")
        return cur
    if path[0] in symbols.names:
        return symbols.names[path[0]]
    raise PlanError(f"unresolved reference {text!r}")


def finalize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Post-resolution coercions: unwrap single-element selection lists for
    scalar params; int-ify index / count params. tool_body_ids stay strings."""
    out = dict(args)
    for k, v in list(out.items()):
        if k == "edge_indices":
            if isinstance(v, list):
                out[k] = [int(_item_id(x)) for x in v]
        elif isinstance(v, list) and len(v) == 1:
            out[k] = _item_id(v[0])
        if k in ("remove_face_index", "count"):
            out[k] = int(_item_id(out[k]))
    return out


# --------------------------------------------------------------------------
# generic adapters (loader grammar, tool-level only)
# --------------------------------------------------------------------------
def adapt_rectangle(args: dict[str, Any]) -> dict[str, Any]:
    """nx_sketch_rectangle: plan corner1/corner2 -> bridge slots corner1={cx,cy},
    corner2={w,h} (the loader grammar is: sketch_id cx cy w h = center, size)."""
    c1, c2 = args.get("corner1"), args.get("corner2")
    if not (isinstance(c1, dict) and isinstance(c2, dict) and "x" in c1 and "x" in c2):
        return args
    out = dict(args)
    out["corner1"] = {
        "x": (c1["x"] + c2["x"]) / 2.0,
        "y": (c1["y"] + c2["y"]) / 2.0,
    }
    out["corner2"] = {"x": c2["x"] - c1["x"], "y": c2["y"] - c1["y"]}
    return out


# --------------------------------------------------------------------------
# generic selection engine (understands geometry only)
# --------------------------------------------------------------------------
def _is_info_key(key: str) -> bool:
    return key in ("note", "use") or "auxiliary" in key


def _base_key(key: str) -> str:
    return key[:-6] if key.endswith("_approx") else key


def _match_num(actual: Any, spec: Any, tol: float) -> bool:
    a = _num(actual)
    if a is None:
        return False
    if isinstance(spec, bool):
        return False
    if isinstance(spec, (int, float)):
        return _approx(a, spec, tol)
    if isinstance(spec, dict):
        if "value" in spec:
            return _approx(a, spec["value"], spec.get("tol", tol))
        if "min" in spec or "max" in spec:
            lo = spec.get("min", -math.inf)
            hi = spec.get("max", math.inf)
            return lo <= a <= hi
        if "any" in spec:
            return any(_match_num(a, c, tol) for c in spec["any"])
        return False
    if isinstance(spec, (list, tuple)):
        if len(spec) == 2 and all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in spec):
            return spec[0] <= a <= spec[1]
        return any(_approx(a, c, tol) for c in spec)
    return False


def _vec_close(a: Any, b: Any, tol: float) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return all(_approx(a[i], b[i], tol) for i in range(len(b)))


def _match_vec(actual: Any, spec: Any, tol: float) -> bool:
    if actual is None:
        return False
    if isinstance(spec, dict):
        if "any" in spec:
            return any(_vec_close(actual, c, spec.get("tol", tol)) for c in spec["any"])
        if "value" in spec:
            return _vec_close(actual, spec["value"], spec.get("tol", tol))
        return all(_approx(actual[i], spec[k], tol) for i, k in enumerate(("x", "y", "z")) if k in spec)
    if isinstance(spec, (list, tuple)):
        return _vec_close(actual, spec, tol)
    return False


def _match_edge(e: dict, criteria: dict, tol: float) -> bool:
    for key, spec in criteria.items():
        if _is_info_key(key):
            continue
        k = _base_key(key)
        if k == "curve_type":
            types = spec if isinstance(spec, list) else [spec]
            if e.get("curve_type") not in types:
                return False
        elif k == "direction":
            dirs = spec if isinstance(spec, list) else [spec]
            if e.get("direction") not in dirs:
                return False
        elif k == "length":
            if not _match_num(e.get("length"), spec, tol):
                return False
        elif k == "midpoint":
            if not _match_vec(e.get("midpoint"), spec, tol):
                return False
        elif k == "midpoint_z":
            mid = e.get("midpoint")
            if mid is None or not _match_num(mid[2], spec, tol):
                return False
        elif k == "bbox":
            bmin, bmax = e.get("bbox_min"), e.get("bbox_max")
            if bmin is None or bmax is None:
                return False
            if isinstance(spec, dict) and "min" in spec and "max" in spec:
                if not (_vec_close(bmin, spec["min"], tol) and _vec_close(bmax, spec["max"], tol)):
                    return False
            else:
                return False
        elif k in ("bbox_x", "bbox_y", "bbox_z"):
            bmin, bmax = e.get("bbox_min"), e.get("bbox_max")
            if bmin is None or bmax is None:
                return False
            axis = {"bbox_x": 0, "bbox_y": 1, "bbox_z": 2}[k]
            lo, hi = min(bmin[axis], bmax[axis]), max(bmin[axis], bmax[axis])
            if not (_match_num(lo, spec, tol) and _match_num(hi, spec, tol)):
                return False
        elif k == "corners_xy":
            bmin, bmax = e.get("bbox_min"), e.get("bbox_max")
            if bmin is None or bmax is None:
                return False
            cands = spec if isinstance(spec, list) else spec.get("any", [])
            ctol = spec.get("tol", tol) if isinstance(spec, dict) else tol
            if not any(_approx(bmin[0], cx, ctol) and _approx(bmin[1], cy, ctol)
                       and _approx(bmax[0], cx, ctol) and _approx(bmax[1], cy, ctol)
                       for cx, cy in cands):
                return False
        elif k == "adjacent_faces":
            if not _match_num(e.get("adjacent_faces"), spec, tol):
                return False
        elif k == "linear_only":
            if spec and e.get("curve_type") != "Linear":
                return False
        # unknown keys are ignored (forward compatible)
    return True


def _match_face(f: dict, criteria: dict, tol: float) -> bool:
    for key, spec in criteria.items():
        if _is_info_key(key):
            continue
        k = _base_key(key)
        if k == "face_type":
            types = spec if isinstance(spec, list) else [spec]
            if f.get("face_type") not in types:
                return False
        elif k == "centroid":
            if not _match_vec(f.get("centroid"), spec, tol):
                return False
        elif k == "centroid_z":
            c = f.get("centroid")
            if c is None or not _match_num(c[2], spec, tol):
                return False
        elif k == "centroid_radius":
            c = f.get("centroid")
            if c is None or not _match_num(math.hypot(c[0], c[1]), spec, tol):
                return False
        elif k == "normal":
            if not _match_vec(f.get("normal"), spec, tol):
                return False
        elif k == "area":
            if not _match_num(f.get("area"), spec, tol):
                return False
        # unknown keys ignored
    return True


def _match_items(items: list, criteria: dict, kind: str, tol: float) -> list:
    if kind == "edges":
        return [it for it in items if _match_edge(it, criteria, tol)]
    return [it for it in items if _match_face(it, criteria, tol)]


def _is_group_criteria(criteria: dict) -> bool:
    known = {
        "curve_type", "direction", "length", "midpoint", "midpoint_z", "bbox",
        "bbox_x", "bbox_y", "bbox_z", "corners_xy", "adjacent_faces",
        "linear_only", "face_type", "centroid", "centroid_z",
        "centroid_radius", "normal", "area",
    }
    return any(key not in known and not _is_info_key(key) for key in criteria)


def compute_extents(items: list, kind: str) -> dict | None:
    xs, ys, zs = [], [], []
    for it in items:
        if kind == "edges":
            if it.get("curve_type") != "Linear":
                continue
            bmin, bmax = it.get("bbox_min"), it.get("bbox_max")
            if not bmin or not bmax:
                continue
            xs += [bmin[0], bmax[0]]
            ys += [bmin[1], bmax[1]]
            zs += [bmin[2], bmax[2]]
        else:
            c = it.get("centroid")
            if not c:
                continue
            xs.append(c[0])
            ys.append(c[1])
            zs.append(c[2])
    if not xs:
        return None
    return {"x_min": min(xs), "x_max": max(xs),
            "y_min": min(ys), "y_max": max(ys),
            "z_min": min(zs), "z_max": max(zs)}


def merge_extents(a: dict | None, b: dict | None) -> dict | None:
    """Span-wise union of two extents dicts (None-safe). Report semantics only."""
    if not b:
        return a
    if not a:
        return dict(b)
    return {
        "x_min": min(a["x_min"], b["x_min"]), "x_max": max(a["x_max"], b["x_max"]),
        "y_min": min(a["y_min"], b["y_min"]), "y_max": max(a["y_max"], b["y_max"]),
        "z_min": min(a["z_min"], b["z_min"]), "z_max": max(a["z_max"], b["z_max"]),
    }


def run_selection(items: list, criteria: dict, kind: str, tol: float = DEFAULT_TOL) -> dict:
    """Run flat or named-group criteria. Returns counts / group items / extents."""
    if _is_group_criteria(criteria):
        group_items: dict[str, list] = {}
        for gname, gcrit in criteria.items():
            if _is_info_key(gname):
                continue
            if isinstance(gcrit, dict):
                group_items[gname] = _match_items(items, gcrit, kind, tol)
        flat = [it for g in group_items.values() for it in g]
        return {
            "count": len(flat),
            "groups": {g: len(v) for g, v in group_items.items()},
            "group_items": group_items,
            "extents": compute_extents(flat, kind),
            "items": flat,
        }
    flat = _match_items(items, criteria, kind, tol)
    return {"count": len(flat), "groups": {}, "group_items": {},
            "extents": compute_extents(flat, kind), "items": flat}


def _parse_done(resp: dict) -> int | None:
    for key in ("result", "message"):
        m = re.search(r"done=(\d+)", str(resp.get(key, "")))
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# expectation checker
# --------------------------------------------------------------------------
def check_expectation(expect: dict, selection: dict, resp: dict, tol: float = DEFAULT_TOL) -> list[str]:
    fails: list[str] = []
    count = selection.get("count", 0)
    groups = selection.get("groups", {})
    ext = selection.get("extents") or {}
    for key, spec in expect.items():
        if key in ("purpose", "note", "tolerance_mm") or "auxiliary" in key or key.endswith("_approx"):
            continue
        if key == "count":
            if count != spec:
                fails.append(f"count {count} != {spec}")
        elif key == "count_range":
            lo, hi = spec if isinstance(spec, (list, tuple)) else (spec["min"], spec["max"])
            if not (lo <= count <= hi):
                fails.append(f"count {count} outside {lo}..{hi}")
        elif key == "body_count":
            n = len(resp.get("objects") or [])
            if n != spec:
                fails.append(f"body_count {n} != {spec}")
        elif key.endswith("_count_range"):
            gname = key[: -len("_count_range")]
            n = groups.get(gname)
            lo, hi = spec if isinstance(spec, (list, tuple)) else (spec["min"], spec["max"])
            if n is None or not (lo <= n <= hi):
                fails.append(f"{gname} count {n} outside {lo}..{hi}")
        elif key.endswith("_count"):
            gname = key[: -len("_count")]
            n = groups.get(gname)
            if n != spec:
                fails.append(f"{gname} count {n} != {spec}")
        elif key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"):
            t = expect.get("tolerance_mm", tol)
            if key not in ext:
                fails.append(f"{key}: no extent available")
            elif not _approx(ext[key], spec, t):
                fails.append(f"{key} {ext[key]} != {spec} (±{t})")
        elif key == "done":
            got = _parse_done(resp)
            if got != spec:
                fails.append(f"done {got} != {spec}")
        # unknown expectation keys ignored (forward compatible)
    return fails


# --------------------------------------------------------------------------
# transport (lazy NX imports; resident Loader via existing bridge)
# --------------------------------------------------------------------------
class NXTransport:
    def __init__(self, workspace_root: str | None = None) -> None:
        self._bridge = None
        self._lb = None
        self._ws = None
        self._runtime_config = load_runtime_config()
        self.workspace_root = (
            workspace_root
            or os.environ.get("NX_MCP_WORKSPACE")
            or str(self._runtime_config.get("workspace_root") or "")
        )

    def _ensure(self):
        if self._bridge is not None:
            return
        src = (
            os.environ.get("NX_MCP_ENHANCED_SRC")
            or str(self._runtime_config.get("nx_mcp_src") or "")
        )
        if not src:
            cand = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "NX_MCP-Enhanced",
                "src",
            )
            if os.path.isdir(cand):
                src = cand
        if src and os.path.isdir(src):
            sys.path.insert(0, src)
        try:
            from nx_mcp.loader_bridge import LoaderBridge
            import nx_mcp.loader_bridge as lb
            from nx_mcp.workspace import Workspace
        except Exception as e:  # pragma: no cover - env-dependent
            raise PlanError(f"cannot import NX_MCP bridge: {e}") from e
        self._lb = lb
        self._bridge = LoaderBridge()
        self._ws = Workspace(self.workspace_root or ".")

    def ping(self) -> bool:
        self._ensure()
        return bool(self._bridge.ping())

    def ping_error(self) -> str | None:
        self._ensure()
        return getattr(self._bridge, "last_ping_error", None)

    def resolve_path(self, path: str) -> str:
        self._ensure()
        return str(self._ws.resolve(path))

    async def call(self, tool: str, args: dict) -> dict:
        self._ensure()
        return await self._bridge.call(tool, args)

    async def call_raw(self, tool: str, args: dict) -> dict:
        """Direct named-pipe call (keeps the loader's done=N in the result)."""
        self._ensure()
        cmd = self._lb._command(tool, args)
        if cmd is None:
            raise PlanError(f"tool {tool!r} not supported by the C# Loader")
        return await asyncio.to_thread(self._lb._pipe_call, cmd, 180.0, self._bridge.pipe)

    async def preflight_close(self) -> None:
        """Close a stale part left by an aborted earlier run (never saved)."""
        try:
            await self.call("nx_close_part", {"save": False})
        except Exception:
            pass


# --------------------------------------------------------------------------
# topology state
# --------------------------------------------------------------------------
class TopologyState:
    def __init__(self) -> None:
        self.edges_valid = True
        self.faces_valid = True
        self.edges_cached: list = []
        self.faces_cached: list = []

    def invalidate(self) -> None:
        self.edges_valid = False
        self.faces_valid = False
        self.edges_cached = []
        self.faces_cached = []


# --------------------------------------------------------------------------
# run history (preflight safety: never destroy parts we do not own)
# --------------------------------------------------------------------------
def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(p or ""))


class RunHistory:
    """Persistent record of parts the Runner created and their save state.

    The C# Loader exposes no dirty/modified flag through certified tools, so the
    Runner reconstructs dirtiness conservatively: a part is CLEAN only when the
    Runner's own record proves the last run saved it; anything else (no record,
    or a failed/aborted run) is treated as DIRTY.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.entries = data
        except (OSError, ValueError):
            self.entries = {}

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _key(self, p: str) -> str:
        return _norm_path(p)

    def paths(self) -> set[str]:
        return set(self.entries.keys())

    def most_recent(self, p: str) -> dict | None:
        return self.entries.get(self._key(p))

    def record_start(
        self, p: str, mode: str, plan: str, repair_attempt: int = 0
    ) -> None:
        self.entries[self._key(p)] = {
            "save_ok": False,
            "mode": mode,
            "plan": plan or "",
            "repair_attempt": int(repair_attempt),
            "started_at": _now_iso(),
            "saved_at": None,
        }
        self.save()

    def record_saved(self, p: str) -> None:
        e = self.entries.setdefault(self._key(p), {})
        e["save_ok"] = True
        e["saved_at"] = _now_iso()
        self.save()

    def record_failed(self, p: str) -> None:
        e = self.entries.setdefault(self._key(p), {})
        e["save_ok"] = False
        e["failed_at"] = _now_iso()
        self.save()


# --------------------------------------------------------------------------
# preflight safety policy
# --------------------------------------------------------------------------
def runtime_dirty(active_path: str, history: RunHistory | None) -> bool:
    """Dirty state of the active part as seen by the Runner.

    The loader cannot report IsModified, so the Runner is conservative:
    no record / failed last run -> dirty; proven successful save -> clean.
    """
    if not active_path:
        return False
    rec = history.most_recent(active_path) if history is not None else None
    if rec is None:
        return True
    return not rec.get("save_ok", False)


def preflight_decision(active_path: str, planned_path: str, mode: str,
                       overwrite_allowed: bool, dirty: bool,
                       runner_parts, repair_authorized: bool = False) -> tuple[str, dict]:
    """Pure preflight policy (unit-testable without NX).

    Runner may auto-close ONLY:
      A. the part whose path exactly matches the plan's target part, and
      B. parts the Runner itself created (recorded in its own history).
    Anything else -> blocked. A dirty planned part is closed only for an
    explicitly authorized controlled-repair attempt in benchmark mode.
    """
    if not active_path:
        return ("allow", {"active_part": None, "state": "no_active_part"})
    active_n = _norm_path(active_path)
    planned_n = _norm_path(planned_path) if planned_path else ""
    if planned_n and active_n == planned_n:
        if not dirty:
            return ("allow", {"active_part": active_path, "state": "planned_clean"})
        if mode == "benchmark" and overwrite_allowed and repair_authorized:
            return ("allow", {"active_part": active_path,
                              "state": "planned_dirty_controlled_repair"})
        return ("blocked", {
            "reason": "planned_part_dirty",
            "active_part": active_path, "planned_part": planned_path,
            "mode": mode, "overwrite_allowed": overwrite_allowed})
    if active_n in {_norm_path(p) for p in (runner_parts or ())}:
        return ("allow", {"active_part": active_path, "state": "runner_test_part"})
    return ("blocked", {
        "reason": "unrelated_part_open",
        "active_part": active_path, "planned_part": planned_path})


def derive_planned_part(plan: dict, transport) -> str | None:
    """The plan's target output part (first nx_create_part / nx_open_part path)."""
    for op in plan.get("operations") or []:
        if op.get("tool") in ("nx_create_part", "nx_open_part"):
            p = (op.get("tool_args") or {}).get("path")
            if isinstance(p, str):
                return transport.resolve_path(p)
    return None


def repair_request_errors(
    repair_attempt: int,
    mode: str,
    overwrite_allowed: bool,
    planned_part: str | None,
    previous_report: dict | None,
) -> list[str]:
    """Validate the one-shot Controlled Self-Healing gate."""
    if repair_attempt not in (0, 1):
        return ["repair_attempt must be 0 or 1"]

    errors: list[str] = []
    if repair_attempt == 0:
        if previous_report is not None:
            errors.append("repair_report is only valid when repair_attempt=1")
        return errors

    if mode != "benchmark":
        errors.append("repair attempt requires --mode benchmark")
    if not overwrite_allowed:
        errors.append("repair attempt requires --allow-overwrite")
    if previous_report is None:
        errors.append("repair attempt requires a previous failed report")
        return errors

    if previous_report.get("status") != "failed":
        errors.append("previous report is not failed")
    if previous_report.get("failed_step") is None:
        errors.append("previous report has no failed_step")
    if int(previous_report.get("repair_attempt", 0) or 0) != 0:
        errors.append("controlled self-healing already consumed")

    previous_part = previous_report.get("planned_part")
    if not previous_part or not planned_part:
        errors.append("planned part is missing from repair metadata")
    elif _norm_path(str(previous_part)) != _norm_path(str(planned_part)):
        errors.append("repair plan targets a different part")

    return errors


async def run_preflight(transport, plan: dict, mode: str, overwrite_allowed: bool,
                        history: RunHistory, repair_authorized: bool = False
                        ) -> tuple[dict | None, dict | None]:
    """Query the active part and apply the safety policy.

    Returns (blocked_payload, info); exactly one is non-None.
    """
    planned = derive_planned_part(plan, transport)
    resp = await transport.call("nx_status", {})
    active = resp.get("active_part")
    active_path = str(_item_id(active)) if active else ""
    dirty = runtime_dirty(active_path, history) if active_path else False
    decision, payload = preflight_decision(
        active_path, planned or "", mode, overwrite_allowed, dirty, history.paths(),
        repair_authorized=repair_authorized)
    if decision == "blocked":
        payload["status"] = "precheck_blocked"
        return payload, None
    if active_path:
        # allowed close: planned part (clean / benchmark overwrite) or own test part
        await transport.call("nx_close_part", {"save": False})
    return None, {"planned_part": planned, "active_part": active_path,
                  "state": payload.get("state")}


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------
def _validate_params(tool: str, args: dict) -> None:
    spec = TOOL_PARAMS.get(tool)
    if spec is None:
        raise PlanError(f"tool {tool!r} is not a certified tool")
    required, optional = spec
    for k in required:
        if k not in args:
            raise PlanError(f"{tool}: missing required param {k!r}")
    for k in args:
        if k not in required and k not in optional:
            raise PlanError(f"{tool}: illegal param {k!r} (certified params: {', '.join(required + optional)})")


def _timing_bucket(tool: str, op_index: int, validation_start_idx: int) -> str:
    """Classify operation wall time for report diagnostics.

    Save/export are separated from geometric validation so an asynchronous STEP
    file settle cannot make "final validation" look slow.
    """
    if tool == "nx_save_part":
        return "save"
    if tool == "nx_export_step":
        return "export"
    if op_index >= validation_start_idx and tool in {
        "nx_list_bodies", "nx_list_faces", "nx_list_edges", "nx_list_features",
        "nx_list_sketches", "nx_status", "nx_fit_view",
    }:
        return "validation"
    if op_index < validation_start_idx:
        return "modeling"
    return "other"


async def run_plan(plan: dict, transport: NXTransport, plan_path: str = "",
                   wall_start: float | None = None, history: RunHistory | None = None,
                   mode: str = "normal", planned_part: str | None = None) -> dict:
    ops = plan.get("operations") or []
    symbols = Symbols()
    topo = TopologyState()
    steps_log: list[dict] = []
    body_count = None
    model_bbox = None
    linear_edge_bbox = None
    prt_path = ""
    step_path = ""
    status = "success"
    failed_step = None
    operations_completed = 0
    total_retries = 0
    selections: dict[str, Any] = {}
    export_settle_elapsed = 0.0
    phase_elapsed = {
        "modeling": 0.0,
        "validation": 0.0,
        "save": 0.0,
        "export": 0.0,
        "other": 0.0,
    }

    # validation phase = first step after the last topology-changing operation
    validation_start_idx = len(ops)
    for i in range(len(ops) - 1, -1, -1):
        if ops[i].get("topology_changes"):
            validation_start_idx = i + 1
            break

    first_op_at: float | None = None
    last_op_at: float | None = None
    validation_start_at: float | None = None
    loop_start = time.monotonic()

    for idx, op in enumerate(ops):
        step = op.get("step")
        tool = op.get("tool")
        if idx == validation_start_idx:
            validation_start_at = time.monotonic()
        t0 = time.monotonic()
        if first_op_at is None:
            first_op_at = t0
        started_at = _now_iso()
        retried = 0
        try:
            args = copy.deepcopy(op.get("tool_args") or {})
            args = resolve_value(args, symbols)
            if tool == "nx_sketch_rectangle":
                args = adapt_rectangle(args)
            if tool in ("nx_create_part", "nx_open_part", "nx_export_step"):
                args["path"] = transport.resolve_path(args["path"])
            args = finalize_args(args)
            _validate_params(tool, args)

            # dispatch with plan-declared retries only
            max_retry = int((op.get("retry") or {}).get("max", 0))
            while True:
                try:
                    if tool in RAW_PIPE_TOOLS:
                        resp = await transport.call_raw(tool, args)
                    else:
                        resp = await transport.call(tool, args)
                    break
                except Exception as e:
                    err = str(e)
                    if retried >= max_retry or not _retry_applies(op, err):
                        raise
                    retried += 1
                    total_retries += 1

            # selection (list steps with selection_criteria)
            selection = None
            # full-model extents for the report: every list step contributes
            if tool in ("nx_list_edges", "nx_list_faces"):
                _kind = "edges" if tool == "nx_list_edges" else "faces"
                _items = resp.get("edges") or resp.get("faces") or []
                model_bbox = merge_extents(model_bbox, compute_extents(_items, _kind))
                if tool == "nx_list_edges":
                    linear_edge_bbox = merge_extents(linear_edge_bbox,
                                                     compute_extents(_items, "edges"))
            if op.get("selection_criteria") and tool in ("nx_list_edges", "nx_list_faces"):
                kind = "edges" if tool == "nx_list_edges" else "faces"
                sel = run_selection(resp.get("edges") or resp.get("faces") or [],
                                    op["selection_criteria"], kind)
                if op.get("selection_binding"):
                    if sel["group_items"]:
                        bound = {g: [it.get("index") for it in its]
                                 for g, its in sel["group_items"].items()}
                    else:
                        bound = [it.get("index") for it in sel["items"]]
                    symbols.bind_selection(op["selection_binding"], bound)
                    selections[op["selection_binding"]] = bound
                selection = sel
            elif tool == "nx_list_bodies":
                n = len(resp.get("objects") or [])
                body_count = n
                selection = {"count": n, "groups": {}, "group_items": {},
                             "extents": None, "items": []}
            elif tool == "nx_list_edges":
                items = resp.get("edges") or []
                selection = {"count": len(items), "groups": {}, "group_items": {},
                             "extents": compute_extents(items, "edges"), "items": items}
            elif tool == "nx_list_faces":
                items = resp.get("faces") or []
                selection = {"count": len(items), "groups": {}, "group_items": {},
                             "extents": compute_extents(items, "faces"), "items": items}

            # expectation
            if op.get("expectation"):
                sel0 = selection or {"count": 0, "groups": {}, "group_items": {},
                                     "extents": None, "items": []}
                fails = check_expectation(op["expectation"], sel0, resp)
                if fails:
                    raise PlanError(f"{tool}: expectation failed: " + "; ".join(fails))

            # result bindings
            for field, names in (op.get("result_bindings") or {}).items():
                val = resp.get(field)
                if val is None:
                    raise PlanError(f"{tool}: result_bindings field {field!r} missing from response")
                names_list = [names] if isinstance(names, str) else names
                if isinstance(val, list):
                    vals = [_item_id(v) for v in val]
                    if len(vals) != len(names_list):
                        raise PlanError(f"{tool}: result_bindings {field}: got {len(vals)} values for {len(names_list)} names")
                    for n, v in zip(names_list, vals):
                        symbols.bind(n, v)
                else:
                    oid = _item_id(val)
                    for n in names_list:
                        symbols.bind(n, oid)

            # topology invalidation
            topo_note = ""
            if op.get("topology_changes"):
                topo.invalidate()
                topo_note = " [topology changed: edge/face caches invalidated]"

            # generic report bookkeeping
            if tool in ("nx_create_part", "nx_open_part") and resp.get("part"):
                prt_path = str(_item_id(resp["part"]))
            if tool == "nx_save_part" and history is not None:
                history.record_saved(planned_part or prt_path or "")
            if tool == "nx_export_step":
                target = args.get("path") or str(resp.get("path") or "")
                step_path = target
                t_settle = time.monotonic()
                file_ok = False
                for _ in range(120):  # up to 60 s for the async file settle
                    if target and os.path.isfile(target) and os.path.getsize(target) > 0:
                        file_ok = True
                        break
                    await asyncio.sleep(0.5)
                settle = time.monotonic() - t_settle
                export_settle_elapsed += settle
                if not file_ok:
                    raise PlanError(f"STEP export returned but file not present/non-empty: {target}")

            dur = time.monotonic() - t0
            bucket = _timing_bucket(tool, idx, validation_start_idx)
            phase_elapsed[bucket] += dur
            operations_completed += 1
            last_op_at = time.monotonic()
            msg = str(resp.get("message") or resp.get("result") or "")
            done = _parse_done(resp)
            if done is not None:
                msg += f" done={done}"
            steps_log.append({
                "step": step, "tool": tool, "started_at": started_at,
                "elapsed_seconds": round(dur, 3), "phase": bucket, "status": "ok",
                "retry_count": retried, "note": (msg + topo_note).strip(),
            })
        except Exception as e:
            dur = time.monotonic() - t0
            bucket = _timing_bucket(tool, idx, validation_start_idx)
            phase_elapsed[bucket] += dur
            last_op_at = time.monotonic()
            steps_log.append({
                "step": step, "tool": tool, "started_at": started_at,
                "elapsed_seconds": round(dur, 3), "phase": bucket, "status": "failed",
                "retry_count": retried, "error": str(e),
            })
            status = "failed"
            failed_step = step
            if history is not None and planned_part:
                history.record_failed(planned_part)
            break

    loop_end = time.monotonic()
    nx_execution = (last_op_at - first_op_at) if (first_op_at and last_op_at) else 0.0
    validation_ops_elapsed = phase_elapsed["validation"]
    export_total_elapsed = phase_elapsed["export"]
    export_call_elapsed = max(0.0, export_total_elapsed - export_settle_elapsed)
    report = {
        "status": status,
        "elapsed_seconds": round(loop_end - loop_start, 3),
        "runner_start_to_first_nx_call": round(first_op_at - wall_start, 3) if wall_start and first_op_at else None,
        "nx_execution_elapsed": round(nx_execution, 3),
        "nx_modeling_elapsed": round(phase_elapsed["modeling"], 3),
        "final_validation_elapsed": round(validation_ops_elapsed, 3),
        "validation_ops_elapsed": round(validation_ops_elapsed, 3),
        "save_elapsed": round(phase_elapsed["save"], 3),
        "export_call_elapsed": round(export_call_elapsed, 3),
        "export_settle_elapsed": round(export_settle_elapsed, 3),
        "step_export_settle_elapsed": round(export_settle_elapsed, 3),
        "other_ops_elapsed": round(phase_elapsed["other"], 3),
        "total_runner_elapsed": round(time.monotonic() - wall_start, 3) if wall_start else None,
        "operations_total": len(ops),
        "operations_completed": operations_completed,
        "failed_step": failed_step,
        "retries": total_retries,
        "body_count": body_count,
        "model_bbox": model_bbox,
        "linear_edge_bbox": linear_edge_bbox,
        "selections": selections,
        "prt_path": prt_path,
        "step_path": step_path,
        "plan": plan_path,
        "planned_part": planned_part,
        "mode": plan.get("mode"),
        "run_mode": mode,
        "modified_nx_mcp_enhanced": False,
        "steps": steps_log,
    }
    return report


def _retry_applies(op: dict, err: str) -> bool:
    retry = op.get("retry") or {}
    subs = retry.get("if_error_contains") or []
    return any(s and s in err for s in subs)


# --------------------------------------------------------------------------
# builder: frozen plan -> executable plan (mechanical, plan-driven)
# --------------------------------------------------------------------------
def _refs_in(args: dict, kind: str) -> list[str]:
    """Logical names referenced by one op's tool_args, in order."""
    out: list[str] = []
    if not isinstance(args, dict):
        return out
    pref = "sketch_" if kind == "sketch" else "body_"
    for k in ("sketch_id", "body_id", "target_body_id"):
        v = args.get(k)
        if isinstance(v, str) and v.startswith(pref):
            out.append(v)
    if kind == "body":
        tl = args.get("tool_body_ids")
        if isinstance(tl, list):
            out.extend(t for t in tl if isinstance(t, str) and t.startswith("body_"))
    return out


def build_executable_plan(plan: dict) -> dict:
    """Convert a frozen planner plan into the executable form:
    - explicit result_bindings (producer -> logical name of its first consumer),
    - explicit selection_binding + $selection references,
    - machine-readable retry metadata (declared safe retries only),
    - $ references everywhere logical names are used.
    The modelling rules themselves are untouched.
    """
    out = copy.deepcopy(plan)
    ops = out["operations"]

    sk_live: list[int] = []          # producer op indices with unbound sketch results
    bd_live: list[int] = []          # producer op indices with unbound body results
    sk_bound: dict[str, int] = {}    # sketch name -> producing op index
    bd_bound: dict[str, int] = {}    # body name -> producing op index
    consumed: set[int] = set()       # producer indices consumed as unite tools
    emits: dict[int, dict[str, Any]] = {}  # producer op index -> result_bindings fields

    def emit(op_idx: int, field: str, name: str) -> None:
        cur = emits.setdefault(op_idx, {})
        cur.setdefault(field, [])
        if name not in cur[field]:
            cur[field].append(name)

    def bind_sketch(name: str, op_idx: int) -> None:
        sk_bound[name] = op_idx
        emit(op_idx, "object", name)

    def bind_body(name: str, op_idx: int) -> None:
        bd_bound[name] = op_idx
        tool = ops[op_idx]["tool"]
        if tool in ("nx_circular_pattern", "nx_linear_pattern", "nx_list_bodies"):
            field = "objects"
        elif tool in ("nx_mirror", "nx_unite"):
            field = "object"   # bridge adapted-response field for mirror/unite
        else:
            field = "body"
        emit(op_idx, field, name)

    for i, op in enumerate(ops):
        tool = op["tool"]
        args = op.get("tool_args") or {}

        # 1) resolve referenced sketch names (against previous producers)
        for r in _refs_in(args, "sketch"):
            if r not in sk_bound and sk_live:
                bind_sketch(r, sk_live.pop())

        # 2) resolve referenced body names (against previous producers)
        for r in _refs_in(args, "body"):
            if r in bd_bound:
                continue
            if tool == "nx_unite" and r == args.get("target_body_id"):
                # unite target: oldest live unbound body; alias if none
                if bd_live:
                    bind_body(r, bd_live.pop(0))
                else:
                    live_names = [n for n, p in bd_bound.items() if p not in consumed]
                    if live_names:
                        oldest = min(live_names, key=lambda n: bd_bound[n])
                        bind_body(r, bd_bound[oldest])
                    else:
                        bind_body(r, i)
            else:
                # source / list / feature body: most recent live unbound
                if bd_live:
                    bind_body(r, bd_live.pop())

        # 3) register this op's own producers (results become live for later ops)
        if tool == "nx_create_sketch":
            sk_live.append(i)
        elif tool == "nx_extrude" and args.get("operation", "create") == "create":
            bd_live.append(i)
        elif tool == "nx_mirror":
            bd_live.append(i)
        elif tool in ("nx_circular_pattern", "nx_linear_pattern"):
            try:
                copies = max(0, int(args.get("count", 1)) - 1)
            except (TypeError, ValueError):
                copies = 0
            for _ in range(copies):
                bd_live.append(i)
        elif tool == "nx_list_bodies":
            # Safe existing-part binding: only expose the sole body as a producer
            # when the frozen plan explicitly requires body_count == 1.
            exp = op.get("expectation") or {}
            if exp.get("body_count") == 1:
                bd_live.append(i)

        # 4) unite consumes tool bodies
        if tool == "nx_unite":
            for t in (args.get("tool_body_ids") or []):
                if isinstance(t, str) and t in bd_bound:
                    consumed.add(bd_bound[t])

    # --- apply result_bindings to ops --------------------------------------
    for op_idx, fields in emits.items():
        rb = ops[op_idx].setdefault("result_bindings", {})
        for field, names in fields.items():
            rb[field] = names if len(names) > 1 else names[0]

    # --- selection bindings + $selection references ------------------------
    sel_syms: dict[int, str] = {}
    for op in ops:
        if op.get("selection_criteria") and op["tool"] in ("nx_list_edges", "nx_list_faces"):
            sym = "sel_%s" % op["step"]
            sel_syms[op["step"]] = sym
            op["selection_binding"] = sym
    for op in ops:
        args = op.get("tool_args") or {}
        for k, v in list(args.items()):
            if isinstance(v, str):
                m = re.match(r"<step(\d+)[^>]*>", v)
                if m and int(m.group(1)) in sel_syms:
                    args[k] = "$selection." + sel_syms[int(m.group(1))]

    # --- rewrite logical names to $ references -----------------------------
    for op in ops:
        args = op.get("tool_args") or {}
        for k in ("sketch_id", "body_id", "target_body_id"):
            v = args.get(k)
            if isinstance(v, str) and (v.startswith("sketch_") or v.startswith("body_")):
                args[k] = "$" + v
        tools = args.get("tool_body_ids")
        if isinstance(tools, list):
            args["tool_body_ids"] = [
                "$" + t if isinstance(t, str) and t.startswith("body_") else t
                for t in tools]

    # --- declared safe retries (documented loader transients only) ---------
    for op in ops:
        if op["tool"] == "nx_save_part" and "retry" not in op:
            op["retry"] = {"max": 1, "if_error_contains": ["撤消"]}

    out["notes"] = list(out.get("notes") or [])
    out["notes"].append(
        "executable format: result_bindings / selection_binding / $references added; "
        "natural-language placeholders removed; retries are plan-declared only.")
    return out


# --------------------------------------------------------------------------
# static plan check (no NX)
# --------------------------------------------------------------------------
EDGE_KEYS = {"curve_type", "direction", "length", "midpoint", "midpoint_z",
             "bbox", "bbox_x", "bbox_y", "bbox_z", "corners_xy",
             "adjacent_faces", "linear_only"}
FACE_KEYS = {"face_type", "centroid", "centroid_z", "centroid_radius",
             "normal", "area"}


def _unknown_criteria_keys(criteria: dict, kind: str) -> list[str]:
    known = EDGE_KEYS if kind == "edges" else FACE_KEYS
    unknown: list[str] = []
    if _is_group_criteria(criteria):
        for gname, gcrit in criteria.items():
            if _is_info_key(gname):
                continue
            if not isinstance(gcrit, dict):
                unknown.append(f"{gname}: group criteria must be an object")
                continue
            for k in gcrit:
                if k not in known and not _is_info_key(k) and not k.endswith("_approx"):
                    unknown.append(f"{gname}.{k}")
    else:
        for k in criteria:
            if k not in known and not _is_info_key(k) and not k.endswith("_approx"):
                unknown.append(k)
    return unknown


def check_plan(plan: dict, executable: bool = True) -> list[str]:
    errors: list[str] = []
    ops = plan.get("operations") or []
    if not ops:
        errors.append("plan has no operations")
        return errors
    bound_names: set[str] = set()
    bound_selections: dict[str, dict] = {}

    for op in ops:
        step = op.get("step")
        tool = op.get("tool")
        if tool not in CERTIFIED_TOOLS:
            errors.append(f"step {step}: unknown tool {tool!r}")
            continue
        args = op.get("tool_args") or {}
        if not isinstance(args, dict):
            errors.append(f"step {step}: tool_args must be an object")
            continue

        # selection_criteria grammar
        crit = op.get("selection_criteria") or {}
        if isinstance(crit, dict) and crit and tool in ("nx_list_edges", "nx_list_faces"):
            kind = "edges" if tool == "nx_list_edges" else "faces"
            for k in _unknown_criteria_keys(crit, kind):
                errors.append(f"step {step}: unknown {kind} criterion key {k!r}")

        # Frozen plans are Planner output only. Executable-only extensions
        # must be produced by build, never hand-authored by the Planner.
        if not executable:
            for key in ("result_bindings", "selection_binding", "retry"):
                if key in op:
                    errors.append(
                        f"step {step}: frozen plan must not contain executable field {key!r}"
                    )
            for v in _walk(args):
                if isinstance(v, str) and v.startswith("$"):
                    errors.append(
                        f"step {step}: frozen plan must not contain executable reference {v!r}"
                    )

        # natural-language placeholders (executable plans only)
        if executable:
            for v in _walk(args):
                if isinstance(v, str):
                    if re.search(r"<[^>]*>", v):
                        errors.append(f"step {step}: natural-language placeholder {v!r}")
                    if "匹配的" in v:
                        errors.append(f"step {step}: natural-language placeholder {v!r}")

        # reference resolution
        for v in _walk(args):
            if not isinstance(v, str) or not v.startswith("$"):
                continue
            path = v[1:].split(".")
            if path[0] == "selection":
                if len(path) < 2 or path[1] not in bound_selections:
                    errors.append(f"step {step}: unresolved {v!r}")
                else:
                    sel = bound_selections[path[1]]
                    if len(path) > 2 and (sel["kind"] != "groups" or path[2] not in sel["groups"]):
                        errors.append(f"step {step}: bad group reference {v!r}")
            else:
                if path[0] not in bound_names:
                    errors.append(f"step {step}: unresolved {v!r}")

        # Selection-consuming params must use the format-specific reference syntax.
        # A bare semantic string such as "base_plate_vertical_edges" cannot be
        # resolved by build/runtime and must fail statically.
        selection_params = {
            "nx_edge_blend": ("edge_indices",),
            "nx_chamfer": ("edge_indices",),
            "nx_shell": ("remove_face_index",),
        }
        for param in selection_params.get(tool, ()):
            if param not in args:
                continue
            value = args[param]
            if isinstance(value, str):
                if executable:
                    if not value.startswith("$selection."):
                        errors.append(
                            f"step {step}: executable {param} string must be a "
                            f"$selection reference, got {value!r}"
                        )
                else:
                    if not re.fullmatch(r"<step\d+[^>]*>", value):
                        errors.append(
                            f"step {step}: frozen {param} string must be a "
                            f"<stepN ...> selection placeholder, got {value!r}"
                        )

        # param validation (after adaptation)
        a = dict(args)
        if tool == "nx_sketch_rectangle":
            a = adapt_rectangle(a)
        required, optional = TOOL_PARAMS.get(tool, ((), ()))
        for k in required:
            if k not in a:
                errors.append(f"step {step}: {tool} missing required param {k!r}")
        for k in a:
            if k not in required and k not in optional:
                errors.append(f"step {step}: {tool} illegal param {k!r}")

        # apply declared bindings
        for field, names in (op.get("result_bindings") or {}).items():
            names_list = [names] if isinstance(names, str) else names
            for n in names_list:
                bound_names.add(n)
        if op.get("selection_binding"):
            crit2 = op.get("selection_criteria") or {}
            kind2 = "groups" if _is_group_criteria(crit2) else "indices"
            groups = [k for k in crit2 if not _is_info_key(k)] if kind2 == "groups" else []
            bound_selections[op["selection_binding"]] = {"kind": kind2, "groups": groups}

    return errors


# --------------------------------------------------------------------------
# Gate A drawing JSON validator — deterministic, part-agnostic, no NX
# --------------------------------------------------------------------------
_DRAWING_HARD_KEYS = {
    "length",
    "length_x",
    "width",
    "width_y",
    "height",
    "height_z",
    "thickness",
    "diameter",
    "hole_diameter",
    "counterbore_diameter",
    "countersink_diameter",
    "radius",
    "depth",
    "hole_depth",
    "counterbore_depth",
    "axis",
    "width_axis",
    "through_axis",
    "center",
    "centerline",
    "centerline_x",
    "centerline_y",
    "centerline_z",
    "x",
    "y",
    "z",
    "top_z",
    "bottom_z",
    "start_z",
    "end_z",
    "side",
    "start_side",
    "through",
    "through_z",
    "count",
    "count_x",
    "count_y",
    "spacing",
    "spacing_x",
    "spacing_y",
    "pitch",
    "pcd",
    "start_angle_deg",
    "centers",
    "centers_x",
    "centers_y",
    "explicit_centers",
    "hole_x",
    "hole_y",
    "hole_z",
    "x1",
    "y1",
    "z1",
    "x2",
    "y2",
    "z2",
    "start",
    "end",
    "angle",
    "angle_deg",
    "start_angle",
    "end_angle",
    "spec",
    "kind",
    "pattern_type",
}
_DRAWING_META_KEYS = {
    "id",
    "name",
    "type",
    "source_views",
    "confidence",
    "required_for_modeling",
    "notes",
    "warning",
    "warnings",
    "description",
    "evidence",
    "hard_fields",
}
_DRAWING_RELATION_SEMANTICS = {
    "center_distance",
    "center_spacing",
    "edge_offset",
    "symmetry",
    "upper_tangent",
    "lower_tangent",
    "coincident",
    "alignment",
}


def _drawing_path_get(data: dict, target: str) -> Any:
    """Resolve feature:<id>.<path> or a normal dotted/list path."""
    if target.startswith("feature:"):
        rest = target[len("feature:") :]
        feature_id, dot, tail = rest.partition(".")
        if not feature_id:
            raise KeyError(target)
        feature = next(
            (
                item
                for item in data.get("features", [])
                if isinstance(item, dict) and item.get("id") == feature_id
            ),
            None,
        )
        if feature is None:
            raise KeyError(target)
        cur: Any = feature
        parts = tail.split(".") if dot and tail else []
    else:
        cur = data
        parts = target.split(".")
    for part in parts:
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(target)
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx < 0 or idx >= len(cur):
                raise KeyError(target)
            cur = cur[idx]
        else:
            raise KeyError(target)
    return cur


def _drawing_target_feature(data: dict, target: str) -> dict | None:
    if not target.startswith("feature:"):
        return None
    rest = target[len("feature:") :]
    feature_id, _, _ = rest.partition(".")
    return next(
        (
            item
            for item in data.get("features", [])
            if isinstance(item, dict) and item.get("id") == feature_id
        ),
        None,
    )


def _drawing_target_feature_id(target: str) -> str | None:
    if not target.startswith("feature:"):
        return None
    rest = target[len("feature:") :]
    feature_id, _, _ = rest.partition(".")
    return feature_id or None


def _drawing_is_center_target(target: str) -> bool:
    leaf = target.split(".")[-1].lower()
    return (
        ".centerline." in target
        or ".position.center" in target
        or ".explicit_centers." in target
        or leaf in {
            "center",
            "centerline_x",
            "centerline_y",
            "centerline_z",
            "hole_x",
            "hole_y",
            "hole_z",
        }
    )


def _drawing_hard_paths(value: Any, prefix: str = "") -> set[str]:
    """Collect geometry-bearing leaf paths from one required feature/profile."""
    out: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _DRAWING_META_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict):
                out.update(_drawing_hard_paths(child, path))
            elif isinstance(child, list):
                if child and all(isinstance(item, dict) for item in child):
                    for idx, item in enumerate(child):
                        out.update(_drawing_hard_paths(item, f"{path}.{idx}"))
                elif key == "explicit_centers":
                    for idx, item in enumerate(child):
                        if isinstance(item, list):
                            for coord_idx, _ in enumerate(item):
                                out.add(f"{path}.{idx}.{coord_idx}")
                        elif isinstance(item, dict):
                            out.update(_drawing_hard_paths(item, f"{path}.{idx}"))
                        else:
                            out.add(f"{path}.{idx}")
                elif key in _DRAWING_HARD_KEYS:
                    out.add(path)
            elif key in _DRAWING_HARD_KEYS:
                out.add(path)
    return out


def _drawing_target_covered(target: str, covered: set[str]) -> bool:
    if target in covered:
        return True
    return any(target.startswith(f"{ancestor}.") for ancestor in covered)


def _drawing_equal(a: Any, b: Any, tol: float = 1e-9) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= tol
    return a == b


def _drawing_direct_semantic_ok(data: dict, source: dict) -> bool:
    semantic = str(source.get("semantic") or "")
    target = source.get("target")
    if not isinstance(target, str) or not target:
        return False
    feature = _drawing_target_feature(data, target)
    feature_type = str((feature or {}).get("type") or "").lower()
    leaf = target.split(".")[-1].lower()

    if semantic == "overall_dimension":
        return target.startswith("overall_dimensions.")
    if semantic == "profile_dimension":
        return target.startswith("profile.")
    if semantic == "feature_count":
        return leaf == "count"
    if semantic == "diameter":
        return leaf in {
            "diameter",
            "hole_diameter",
            "counterbore_diameter",
            "countersink_diameter",
        }
    if semantic == "radius":
        return leaf == "radius"
    if semantic == "slot_width":
        return feature_type in {"slot", "cut", "slit"} and leaf == "width"
    if semantic == "depth":
        return leaf in {"depth", "hole_depth", "counterbore_depth"}
    if semantic == "thickness":
        return leaf == "thickness"
    if semantic == "axis":
        return leaf in {"axis", "width_axis", "through_axis"}
    if semantic == "center_position":
        return (
            ".centerline" in target
            or ".position.center" in target
            or leaf in {
                "center",
                "centerline_x",
                "centerline_y",
                "centerline_z",
                "hole_x",
                "hole_y",
                "hole_z",
            }
        )
    if semantic == "position_dimension":
        return leaf in {"x", "y", "z", "top_z", "bottom_z", "start_z", "end_z"}
    if semantic == "thread_spec":
        return leaf == "spec"
    if semantic == "feature_kind":
        return leaf == "kind"
    if semantic == "side":
        return leaf in {"side", "start_side"}
    if semantic == "through":
        return leaf in {"through", "through_z"}
    if semantic == "pattern_dimension":
        return leaf in {
            "count_x",
            "count_y",
            "spacing",
            "spacing_x",
            "spacing_y",
            "pitch",
            "pcd",
            "start_angle_deg",
            "centers",
            "centers_x",
            "centers_y",
            "explicit_centers",
            "pattern_type",
        }
    if semantic == "feature_dimension":
        # Generic direct dimensions are intentionally forbidden from fields whose
        # meaning needs a more specific semantic class. A normal body's width is
        # fine; a slot/cut width must use slot_width.
        forbidden = {
            "count",
            "diameter",
            "hole_diameter",
            "counterbore_diameter",
            "countersink_diameter",
            "radius",
            "depth",
            "hole_depth",
            "counterbore_depth",
            "axis",
            "width_axis",
            "through_axis",
            "center",
            "centerline",
            "centerline_x",
            "centerline_y",
            "centerline_z",
            "bottom_z",
            "top_z",
            "side",
            "start_side",
            "spec",
            "pattern_type",
        }
        if feature_type in {"slot", "cut", "slit"} and leaf == "width":
            return False
        return target.startswith("feature:") and leaf not in forbidden
    return False


def _drawing_source_value(source: dict) -> float | None:
    value = source.get("value")
    return _num(value)


def _drawing_eval_expr(
    data: dict,
    sources: dict[str, dict],
    expr: Any,
) -> tuple[float | None, set[str], set[str], list[str]]:
    """Evaluate a tiny arithmetic expression and return provenance."""
    errors: list[str] = []
    if not isinstance(expr, dict):
        return None, set(), set(), ["derived expr must be an object"]

    if "const" in expr:
        value = _num(expr.get("const"))
        if value is None:
            errors.append("derived const must be numeric")
        return value, set(), set(), errors

    if "target" in expr:
        target = expr.get("target")
        if not isinstance(target, str) or not target:
            return None, set(), set(), ["derived target reference must be a string"]
        try:
            value = _drawing_path_get(data, target)
        except KeyError:
            return None, set(), {target}, [f"derived references missing target {target!r}"]
        number = _num(value)
        if number is None:
            errors.append(f"derived target {target!r} is not numeric")
        return number, set(), {target}, errors

    if "source" in expr:
        sid = expr.get("source")
        if not isinstance(sid, str) or not sid:
            return None, set(), set(), ["derived source reference must be a string"]
        source = sources.get(sid)
        if source is None:
            return None, {sid}, set(), [f"derived references unknown source {sid!r}"]
        value = _drawing_source_value(source)
        if value is None:
            errors.append(f"derived source {sid!r} has no numeric value")
        return value, {sid}, set(), errors

    op = expr.get("op")
    args = expr.get("args")
    if op not in {"add", "sub", "mul", "div", "neg", "abs"}:
        return None, set(), set(), [f"unsupported derived op {op!r}"]
    if not isinstance(args, list):
        return None, set(), set(), ["derived op args must be a list"]

    expected = 1 if op in {"neg", "abs"} else 2
    if len(args) != expected:
        return None, set(), set(), [f"derived op {op!r} requires {expected} args"]

    values: list[float] = []
    source_refs: set[str] = set()
    target_refs: set[str] = set()
    for arg in args:
        value, child_sources, child_targets, child_errors = _drawing_eval_expr(
            data, sources, arg
        )
        errors.extend(child_errors)
        source_refs.update(child_sources)
        target_refs.update(child_targets)
        if value is not None:
            values.append(value)
    if errors or len(values) != expected:
        return None, source_refs, target_refs, errors

    if op == "add":
        result = values[0] + values[1]
    elif op == "sub":
        result = values[0] - values[1]
    elif op == "mul":
        result = values[0] * values[1]
    elif op == "div":
        if abs(values[1]) <= 1e-12:
            return None, source_refs, target_refs, ["derived division by zero"]
        result = values[0] / values[1]
    elif op == "neg":
        result = -values[0]
    else:
        result = abs(values[0])
    return result, source_refs, target_refs, errors


def _drawing_relation_source_ok(
    data: dict,
    source: dict,
    derived_target: str,
    target_refs: set[str],
) -> tuple[bool, str | None]:
    semantic = str(source.get("semantic") or "")

    if semantic in {"center_distance", "center_spacing"}:
        between = source.get("between")
        if (
            not isinstance(between, list)
            or len(between) != 2
            or not all(isinstance(item, str) and item for item in between)
        ):
            return False, f"{semantic} source requires between=[targetA,targetB]"
        if derived_target not in between:
            return False, f"{semantic} source cannot derive {derived_target!r}"
        other = between[1] if between[0] == derived_target else between[0]
        if other not in target_refs:
            return False, f"{semantic} source requires the opposite endpoint {other!r}"
        try:
            a = _num(_drawing_path_get(data, between[0]))
            b = _num(_drawing_path_get(data, between[1]))
        except KeyError:
            return False, f"{semantic} source references a missing endpoint"
        expected = _drawing_source_value(source)
        if a is None or b is None or expected is None:
            return False, f"{semantic} source/endpoints must be numeric"
        if abs(abs(a - b) - expected) > 1e-9:
            return False, f"{semantic} source value does not match endpoint distance"
        return True, None

    return True, None


def _drawing_relation_ref_ok(
    data: dict,
    source: dict,
    derived_target: str,
    target_refs: set[str],
) -> tuple[bool, str | None]:
    semantic = str(source.get("semantic") or "")
    links = source.get("links")
    if semantic not in {
        "upper_tangent",
        "lower_tangent",
        "coincident",
        "alignment",
    }:
        return False, f"source semantic {semantic!r} is not a relation_ref"
    if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
        return False, f"relation source {semantic!r} requires links"
    if derived_target not in links:
        return False, f"relation source {semantic!r} does not cover {derived_target!r}"
    if target_refs and not any(ref in links for ref in target_refs):
        return False, f"relation source {semantic!r} does not link a derived dependency"

    if semantic in {"upper_tangent", "lower_tangent"}:
        center = source.get("center")
        diameter = source.get("diameter")
        tangent = source.get("tangent")
        if not all(isinstance(item, str) and item for item in (center, diameter, tangent)):
            return False, f"{semantic} source requires center/diameter/tangent targets"
        try:
            center_value = _num(_drawing_path_get(data, center))
            diameter_value = _num(_drawing_path_get(data, diameter))
            tangent_value = _num(_drawing_path_get(data, tangent))
        except KeyError:
            return False, f"{semantic} source references a missing target"
        if center_value is None or diameter_value is None or tangent_value is None:
            return False, f"{semantic} targets must be numeric"
        sign = 1.0 if semantic == "upper_tangent" else -1.0
        expected = center_value + sign * diameter_value / 2.0
        if abs(expected - tangent_value) > 1e-9:
            return False, f"{semantic} relation does not match target geometry"
    return True, None


def _drawing_overall_bbox(data: dict) -> tuple[float, float, float] | None:
    overall = data.get("overall_dimensions")
    if not isinstance(overall, dict):
        return None

    def first_number(*keys: str) -> float | None:
        for key in keys:
            value = _num(overall.get(key))
            if value is not None:
                return value
        return None

    lx = first_number("length_x", "length")
    ly = first_number("width_y", "width")
    hz = first_number("height_z", "height")
    if lx is None or ly is None or hz is None:
        return None
    if lx <= 0 or ly <= 0 or hz <= 0:
        return None
    return lx, ly, hz


def _drawing_check_coord(
    errors: list[str],
    label: str,
    axis: str,
    value: Any,
    bbox: tuple[float, float, float],
) -> None:
    number = _num(value)
    if number is None:
        return
    lx, ly, hz = bbox
    if axis == "x":
        lo, hi = -lx / 2.0, lx / 2.0
    elif axis == "y":
        lo, hi = -ly / 2.0, ly / 2.0
    else:
        lo, hi = 0.0, hz
    if number < lo - 1e-9 or number > hi + 1e-9:
        errors.append(
            f"{label} {axis}={number:g} outside overall bbox [{lo:g},{hi:g}]"
        )


def _drawing_check_feature_bbox(
    errors: list[str],
    feature: dict,
    bbox: tuple[float, float, float],
) -> None:
    fid = str(feature.get("id") or "?")
    centerline = feature.get("centerline")
    if isinstance(centerline, dict):
        for axis in ("x", "y", "z"):
            if axis in centerline:
                _drawing_check_coord(
                    errors,
                    f"feature {fid} centerline",
                    axis,
                    centerline.get(axis),
                    bbox,
                )

    for axis in ("x", "y", "z"):
        key = f"centerline_{axis}"
        if key in feature:
            _drawing_check_coord(errors, f"feature {fid}", axis, feature.get(key), bbox)
        hole_key = f"hole_{axis}"
        if hole_key in feature:
            _drawing_check_coord(
                errors, f"feature {fid}", axis, feature.get(hole_key), bbox
            )

    position = feature.get("position")
    if isinstance(position, dict):
        center = position.get("center")
        if isinstance(center, dict):
            for axis in ("x", "y", "z"):
                if axis in center:
                    _drawing_check_coord(
                        errors,
                        f"feature {fid} position.center",
                        axis,
                        center.get(axis),
                        bbox,
                    )
        elif isinstance(center, list):
            for axis, value in zip(("x", "y", "z"), center):
                _drawing_check_coord(
                    errors, f"feature {fid} position.center", axis, value, bbox
                )

    centers = feature.get("explicit_centers")
    if isinstance(centers, list):
        for idx, center in enumerate(centers):
            label = f"feature {fid} explicit_centers[{idx}]"
            if isinstance(center, dict):
                for axis in ("x", "y", "z"):
                    if axis in center:
                        _drawing_check_coord(
                            errors, label, axis, center.get(axis), bbox
                        )
            elif isinstance(center, list):
                for axis, value in zip(("x", "y", "z"), center):
                    _drawing_check_coord(errors, label, axis, value, bbox)

    for axis in ("x", "y", "z"):
        values = feature.get(f"centers_{axis}")
        if isinstance(values, list):
            for idx, value in enumerate(values):
                _drawing_check_coord(
                    errors,
                    f"feature {fid} centers_{axis}[{idx}]",
                    axis,
                    value,
                    bbox,
                )


def _drawing_check_profile_bbox(
    errors: list[str],
    value: Any,
    bbox: tuple[float, float, float],
    prefix: str = "profile",
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if key in {"x", "y", "z", "x_range", "y_range", "z_range"}:
                axis = key[0]
                if (
                    isinstance(child, list)
                    and len(child) == 2
                    and all(_num(item) is not None for item in child)
                ):
                    for item in child:
                        _drawing_check_coord(errors, path, axis, item, bbox)
                    continue
            if key in {"x1", "x2", "y1", "y2", "z1", "z2"}:
                _drawing_check_coord(errors, path, key[0], child, bbox)
                continue
            if key in {"start", "end", "center"} and isinstance(child, list):
                for axis, item in zip(("x", "y", "z"), child):
                    _drawing_check_coord(errors, path, axis, item, bbox)
                continue
            _drawing_check_profile_bbox(errors, child, bbox, path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _drawing_check_profile_bbox(errors, child, bbox, f"{prefix}.{idx}")


def _drawing_feature_coord(feature: dict, axis: str) -> Any:
    centerline = feature.get("centerline")
    if isinstance(centerline, dict) and axis in centerline:
        return centerline.get(axis)
    key = f"centerline_{axis}"
    if key in feature:
        return feature.get(key)
    position = feature.get("position")
    if isinstance(position, dict):
        center = position.get("center")
        if isinstance(center, dict):
            return center.get(axis)
        if isinstance(center, list):
            idx = {"x": 0, "y": 1, "z": 2}[axis]
            if idx < len(center):
                return center[idx]
    return None


def _drawing_check_feature_structure(errors: list[str], feature: dict) -> None:
    fid = str(feature.get("id") or "?")
    feature_type = str(feature.get("type") or "").lower()
    hole_like = "hole" in feature_type or feature_type == "coaxial_hole_group"
    if not hole_like:
        return

    axis = str(feature.get("axis") or "").upper()
    if axis not in {"X", "Y", "Z"}:
        errors.append(f"feature {fid!r} hole-like geometry requires axis X/Y/Z")
        return

    needed = {
        "X": ("y", "z"),
        "Y": ("x", "z"),
        "Z": ("x", "y"),
    }[axis]
    for coord in needed:
        if _drawing_feature_coord(feature, coord) is None:
            errors.append(
                f"feature {fid!r} axis {axis} requires center coordinate {coord}"
            )


def _drawing_check_count(errors: list[str], item: dict, label: str) -> None:
    count = item.get("count")
    if count is None:
        return
    n = _num(count)
    if n is None or int(n) != n or n <= 0:
        errors.append(f"{label} count must be a positive integer")
        return
    n_int = int(n)

    explicit = item.get("explicit_centers")
    if isinstance(explicit, list) and len(explicit) != n_int:
        errors.append(f"{label} explicit_centers {len(explicit)} != count {n_int}")

    count_x, count_y = _num(item.get("count_x")), _num(item.get("count_y"))
    if item.get("pattern_type") == "rectangular" and count_x is not None and count_y is not None:
        product = int(count_x) * int(count_y)
        if product != n_int:
            errors.append(f"{label} count_x*count_y {product} != count {n_int}")


def _drawing_check_symmetry(
    errors: list[str],
    source: dict,
    features: dict[str, dict],
) -> None:
    feature_id = source.get("feature")
    axis = str(source.get("axis") or "").lower()
    about = _num(source.get("about"))
    feature = features.get(str(feature_id))
    if feature is None or axis not in {"x", "y", "z"} or about is None:
        errors.append("symmetry source requires feature, axis and numeric about")
        return

    values: list[float] = []
    explicit = feature.get("explicit_centers")
    if isinstance(explicit, list):
        for center in explicit:
            value: Any = None
            if isinstance(center, dict):
                value = center.get(axis)
            elif isinstance(center, list):
                idx = {"x": 0, "y": 1, "z": 2}[axis]
                if idx < len(center):
                    value = center[idx]
            number = _num(value)
            if number is not None:
                values.append(number)
    if not values:
        axis_values = feature.get(f"centers_{axis}")
        if isinstance(axis_values, list):
            values = [number for item in axis_values if (number := _num(item)) is not None]
    if not values:
        errors.append(f"symmetry source has no machine-checkable centers for feature {feature_id!r}")
        return

    for value in values:
        mirror = 2.0 * about - value
        if not any(abs(other - mirror) <= 1e-9 for other in values):
            errors.append(
                f"feature {feature_id!r} centers are not symmetric about {axis}={about:g}"
            )
            return


def check_drawing_json(data: dict) -> list[str]:
    """Machine-check Gate A provenance, derivations, counts and coordinate sanity."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["drawing JSON root must be an object"]

    required_roots = (
        "overall_dimensions",
        "coordinate_system",
        "features",
        "source_ledger",
        "derived",
        "unresolved",
        "dimension_conflicts",
        "dimension_closure",
    )
    for key in required_roots:
        if key not in data:
            errors.append(f"drawing JSON missing {key}")

    features_raw = data.get("features")
    if not isinstance(features_raw, list) or not features_raw:
        errors.append("features must be a non-empty list")
        features_raw = []

    features: dict[str, dict] = {}
    for idx, feature in enumerate(features_raw):
        if not isinstance(feature, dict):
            errors.append(f"features[{idx}] must be an object")
            continue
        fid = feature.get("id")
        if not isinstance(fid, str) or not fid.strip():
            errors.append(f"features[{idx}] missing stable id")
            continue
        if fid in features:
            errors.append(f"duplicate feature id {fid!r}")
            continue
        features[fid] = feature

    ledger = data.get("source_ledger")
    if not isinstance(ledger, list) or not ledger:
        errors.append("source_ledger must be a non-empty list")
        ledger = []

    sources: dict[str, dict] = {}
    direct_targets: set[str] = set()
    relation_targets: set[str] = set()
    for idx, source in enumerate(ledger):
        if not isinstance(source, dict):
            errors.append(f"source_ledger[{idx}] must be an object")
            continue
        sid = source.get("id")
        semantic = source.get("semantic")
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"source_ledger[{idx}] missing id")
            continue
        if sid in sources:
            errors.append(f"duplicate source id {sid!r}")
            continue
        if not isinstance(semantic, str) or not semantic:
            errors.append(f"source {sid!r} missing semantic")
            continue
        sources[sid] = source

        if semantic in _DRAWING_RELATION_SEMANTICS:
            if "target" in source:
                errors.append(f"relation source {sid!r} must not use direct target")
            if semantic == "edge_offset":
                targets = source.get("targets")
                axis = str(source.get("axis") or "").lower()
                side = str(source.get("from") or "").lower()
                value = _drawing_source_value(source)
                if (
                    not isinstance(targets, list)
                    or not targets
                    or not all(isinstance(item, str) and item for item in targets)
                    or axis not in {"x", "y", "z"}
                    or side not in {"min", "max"}
                    or value is None
                ):
                    errors.append(
                        f"source {sid!r} edge_offset requires targets/axis/from/value"
                    )
                else:
                    bbox = _drawing_overall_bbox(data)
                    if bbox is None:
                        errors.append(
                            f"source {sid!r} edge_offset requires valid overall dimensions"
                        )
                    else:
                        lx, ly, hz = bbox
                        bounds = {
                            "x": (-lx / 2.0, lx / 2.0),
                            "y": (-ly / 2.0, ly / 2.0),
                            "z": (0.0, hz),
                        }
                        lo, hi = bounds[axis]
                        expected = lo + value if side == "min" else hi - value
                        for target in targets:
                            try:
                                actual = _num(_drawing_path_get(data, target))
                            except KeyError:
                                actual = None
                            if actual is None:
                                errors.append(
                                    f"source {sid!r} edge_offset target {target!r} is missing"
                                )
                                continue
                            if abs(actual - expected) > 1e-9:
                                errors.append(
                                    f"source {sid!r} edge_offset does not match {target!r}"
                                )
                            relation_targets.add(target)
                continue
            if semantic in {"center_distance", "center_spacing"}:
                between = source.get("between")
                if (
                    not isinstance(between, list)
                    or len(between) != 2
                    or not all(isinstance(item, str) and item for item in between)
                ):
                    errors.append(f"source {sid!r} requires between=[targetA,targetB]")
                else:
                    if not all(_drawing_is_center_target(target) for target in between):
                        errors.append(
                            f"source {sid!r} center distance endpoints must be center coordinates"
                        )
                    for target in between:
                        try:
                            _drawing_path_get(data, target)
                        except KeyError:
                            errors.append(
                                f"source {sid!r} references missing endpoint {target!r}"
                            )
                    value = _drawing_source_value(source)
                    if value is None:
                        errors.append(f"source {sid!r} must have numeric value")
                    else:
                        try:
                            a = _num(_drawing_path_get(data, between[0]))
                            b = _num(_drawing_path_get(data, between[1]))
                        except KeyError:
                            a, b = None, None
                        if a is not None and b is not None:
                            if abs(abs(a - b) - value) > 1e-9:
                                errors.append(
                                    f"source {sid!r} center distance does not match endpoints"
                                )
            elif semantic in {"upper_tangent", "lower_tangent"}:
                center = source.get("center")
                diameter = source.get("diameter")
                tangent = source.get("tangent")
                links = source.get("links")
                if not all(
                    isinstance(item, str) and item
                    for item in (center, diameter, tangent)
                ):
                    errors.append(
                        f"source {sid!r} requires center/diameter/tangent targets"
                    )
                if (
                    not isinstance(links, list)
                    or tangent not in links
                    or center not in links
                    or diameter not in links
                ):
                    errors.append(f"source {sid!r} tangent links are incomplete")
                else:
                    try:
                        center_value = _num(_drawing_path_get(data, center))
                        diameter_value = _num(_drawing_path_get(data, diameter))
                        tangent_value = _num(_drawing_path_get(data, tangent))
                    except KeyError:
                        center_value, diameter_value, tangent_value = None, None, None
                    if (
                        center_value is None
                        or diameter_value is None
                        or tangent_value is None
                    ):
                        errors.append(f"source {sid!r} tangent targets must be numeric")
                    else:
                        sign = 1.0 if semantic == "upper_tangent" else -1.0
                        expected = center_value + sign * diameter_value / 2.0
                        if abs(expected - tangent_value) > 1e-9:
                            errors.append(
                                f"source {sid!r} tangent relation does not match geometry"
                            )
                    relation_targets.add(tangent)
            elif semantic == "symmetry":
                _drawing_check_symmetry(errors, source, features)
            continue

        target = source.get("target")
        if not isinstance(target, str) or not target:
            errors.append(f"direct source {sid!r} missing target")
            continue
        try:
            actual = _drawing_path_get(data, target)
        except KeyError:
            errors.append(f"source {sid!r} targets missing field {target!r}")
            continue
        if not _drawing_direct_semantic_ok(data, source):
            errors.append(
                f"source {sid!r} semantic {semantic!r} is incompatible with {target!r}"
            )
            continue
        if "value" in source and not _drawing_equal(actual, source.get("value")):
            errors.append(f"source {sid!r} value does not match {target!r}")
        direct_targets.add(target)

    derived_raw = data.get("derived")
    if not isinstance(derived_raw, list):
        errors.append("derived must be a list")
        derived_raw = []

    derived_targets: set[str] = set()
    for idx, item in enumerate(derived_raw):
        if not isinstance(item, dict):
            errors.append(f"derived[{idx}] must be an object")
            continue
        did = item.get("id")
        target = item.get("target")
        if not isinstance(did, str) or not did:
            errors.append(f"derived[{idx}] missing id")
            did = f"#{idx}"
        if not isinstance(target, str) or not target:
            errors.append(f"derived {did!r} missing target")
            continue
        if target in derived_targets:
            errors.append(f"multiple derived entries target {target!r}")
        derived_targets.add(target)
        try:
            actual = _drawing_path_get(data, target)
        except KeyError:
            errors.append(f"derived {did!r} targets missing field {target!r}")
            actual = None

        value, source_refs, target_refs, expr_errors = _drawing_eval_expr(
            data, sources, item.get("expr")
        )
        errors.extend(f"derived {did!r}: {error}" for error in expr_errors)

        if value is not None:
            if "value" not in item:
                errors.append(f"derived {did!r} missing value")
            elif not _drawing_equal(value, item.get("value")):
                errors.append(f"derived {did!r} expr does not match declared value")
            if actual is not None and not _drawing_equal(value, actual):
                errors.append(f"derived {did!r} expr does not match target {target!r}")

        relation_refs = item.get("relation_refs") or []
        if not isinstance(relation_refs, list) or not all(
            isinstance(ref, str) and ref for ref in relation_refs
        ):
            errors.append(f"derived {did!r} relation_refs must be a list")
            relation_refs = []

        relation_ok = False
        for sid in source_refs:
            source = sources.get(sid)
            if source is None:
                continue
            semantic = str(source.get("semantic") or "")
            if semantic in {"center_distance", "center_spacing"}:
                ok, reason = _drawing_relation_source_ok(
                    data, source, target, target_refs
                )
                if not ok and reason:
                    errors.append(f"derived {did!r}: {reason}")
                relation_ok = relation_ok or ok
            elif semantic in _DRAWING_RELATION_SEMANTICS:
                errors.append(
                    f"derived {did!r}: relation source {sid!r} cannot be numeric operand"
                )

        for sid in relation_refs:
            source = sources.get(sid)
            if source is None:
                errors.append(f"derived {did!r} references unknown relation {sid!r}")
                continue
            ok, reason = _drawing_relation_ref_ok(data, source, target, target_refs)
            if not ok and reason:
                errors.append(f"derived {did!r}: {reason}")
            relation_ok = relation_ok or ok

        target_feature = _drawing_target_feature_id(target)
        dependency_features = {
            feature_id
            for ref in target_refs
            if (feature_id := _drawing_target_feature_id(ref)) is not None
        }
        cross_feature = (
            target_feature is not None
            and any(feature_id != target_feature for feature_id in dependency_features)
        )
        if cross_feature and not relation_ok:
            errors.append(
                f"derived {did!r} crosses feature boundaries without relation evidence"
            )

    covered_targets = direct_targets | derived_targets | relation_targets

    # Required feature geometry must have evidence; profile/overall get the same rule.
    for fid, feature in features.items():
        if feature.get("required_for_modeling") is False:
            continue
        hard_paths = _drawing_hard_paths(feature)
        declared = feature.get("hard_fields")
        if isinstance(declared, list):
            hard_paths.update(
                item for item in declared if isinstance(item, str) and item
            )
        for path in sorted(hard_paths):
            target = f"feature:{fid}.{path}"
            if not _drawing_target_covered(target, covered_targets):
                errors.append(f"required geometry field lacks evidence: {target}")

    overall = data.get("overall_dimensions")
    if isinstance(overall, dict):
        for key in ("length_x", "length", "width_y", "width", "height_z", "height"):
            if key in overall and _num(overall.get(key)) is not None:
                target = f"overall_dimensions.{key}"
                if target not in direct_targets and target not in derived_targets:
                    errors.append(f"overall dimension lacks evidence: {target}")

    profile = data.get("profile")
    if isinstance(profile, dict):
        for path in sorted(_drawing_hard_paths(profile)):
            target = f"profile.{path}"
            if not _drawing_target_covered(
                target, direct_targets | derived_targets | relation_targets
            ):
                errors.append(f"profile geometry lacks evidence: {target}")

    for feature in features.values():
        _drawing_check_feature_structure(errors, feature)

    # Machine-computed coordinate sanity.
    coord = data.get("coordinate_system")
    if not isinstance(coord, dict) or coord.get("origin") != "part_center_xy_bottom_z0":
        errors.append("coordinate_system.origin must be part_center_xy_bottom_z0")
    bbox = _drawing_overall_bbox(data)
    if bbox is None:
        errors.append("overall_dimensions must provide positive X/Y/Z extents")
    else:
        for feature in features.values():
            _drawing_check_feature_bbox(errors, feature, bbox)
        if isinstance(profile, dict):
            _drawing_check_profile_bbox(errors, profile, bbox)

    # Count conservation for features and top-level patterns.
    for fid, feature in features.items():
        _drawing_check_count(errors, feature, f"feature {fid!r}")
    patterns = data.get("patterns")
    if isinstance(patterns, list):
        for idx, pattern in enumerate(patterns):
            if not isinstance(pattern, dict):
                continue
            _drawing_check_count(errors, pattern, f"patterns[{idx}]")
            for path in sorted(_drawing_hard_paths(pattern)):
                target = f"patterns.{idx}.{path}"
                if not _drawing_target_covered(
                    target, direct_targets | derived_targets | relation_targets
                ):
                    errors.append(f"pattern geometry lacks evidence: {target}")

    unresolved = data.get("unresolved")
    if isinstance(unresolved, list):
        blockers = [
            item
            for item in unresolved
            if isinstance(item, dict) and item.get("required_for_modeling") is True
        ]
        if blockers:
            errors.append(f"blocking_unresolved={len(blockers)}")

    conflicts = data.get("dimension_conflicts")
    if isinstance(conflicts, list) and conflicts:
        errors.append(f"dimension_conflicts={len(conflicts)}")

    if (data.get("dimension_closure") or {}).get("status") != "closed":
        errors.append("dimension_closure.status must be closed")

    return errors


def _load_drawing(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PlanError(f"{path}: drawing JSON root must be an object")
    return data


def _cmd_validate_drawing(args: argparse.Namespace) -> int:
    drawing = _load_drawing(args.drawing)
    errors = check_drawing_json(drawing)
    result = {
        "drawing": args.drawing,
        "source_ownership": {"status": "pass" if not errors else "fail"},
        "coordinate_sanity": {"status": "pass" if not errors else "fail"},
        "errors": errors,
        "ok": not errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    if not isinstance(plan, dict) or "operations" not in plan:
        raise PlanError(f"{path}: not a modeling plan (missing operations)")
    return plan


def _is_executable(plan: dict) -> bool:
    return any(op.get("result_bindings") or op.get("selection_binding") for op in plan.get("operations") or [])


async def _cmd_run(args: argparse.Namespace) -> int:
    t_start = time.monotonic()
    plan = _load_plan(args.plan)
    if not _is_executable(plan):
        print(json.dumps({"status": "failed", "failed_step": None,
                          "errors": ["plan is not in executable format "
                                     "(no result_bindings / selection_binding); run `build` first"]},
                         ensure_ascii=False, indent=2))
        return 1
    errs = check_plan(plan, executable=True)
    if errs:
        print(json.dumps({"status": "failed", "failed_step": None,
                          "errors": errs[:20], "error_count": len(errs)}, ensure_ascii=False, indent=2))
        return 1
    transport = NXTransport(workspace_root=args.workspace)
    if not transport.ping():
        detail = transport.ping_error()
        error = "loader health check failed"
        if detail:
            error += ": " + detail
        print(json.dumps({"status": "failed", "failed_step": None,
                          "errors": [error]}, ensure_ascii=False))
        return 1

    planned_for_repair = derive_planned_part(plan, transport)
    previous_report = None
    if args.repair_report:
        try:
            with open(args.repair_report, encoding="utf-8-sig") as f:
                previous_report = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "status": "repair_precheck_blocked",
                "failed_step": None,
                "errors": [f"cannot read repair report: {exc}"],
            }, ensure_ascii=False, indent=2))
            return 1

    repair_errors = repair_request_errors(
        args.repair_attempt,
        args.mode,
        args.allow_overwrite,
        planned_for_repair,
        previous_report,
    )
    if repair_errors:
        print(json.dumps({
            "status": "repair_precheck_blocked",
            "failed_step": None,
            "errors": repair_errors,
        }, ensure_ascii=False, indent=2))
        return 1

    history_path = args.history or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "run_history.json")
    history = RunHistory(history_path)

    if args.repair_attempt == 1 and planned_for_repair:
        prior_history = history.most_recent(planned_for_repair) or {}
        if int(prior_history.get("repair_attempt", 0) or 0) >= 1:
            print(json.dumps({
                "status": "repair_precheck_blocked",
                "failed_step": None,
                "errors": ["controlled self-healing already consumed for this planned part"],
            }, ensure_ascii=False, indent=2))
            return 1

    t_preflight = time.monotonic()
    blocked, info = await run_preflight(
        transport,
        plan,
        args.mode,
        args.allow_overwrite,
        history,
        repair_authorized=(args.repair_attempt == 1),
    )
    preflight_elapsed = time.monotonic() - t_preflight
    if blocked is not None:
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 1
    planned_part = info["planned_part"] if info else None
    if planned_part:
        history.record_start(
            planned_part, args.mode, args.plan, repair_attempt=args.repair_attempt
        )
    report = await run_plan(plan, transport, plan_path=args.plan,
                            wall_start=t_start, history=history,
                            mode=args.mode, planned_part=planned_part)
    report["preflight_elapsed"] = round(preflight_elapsed, 3)
    report["repair_attempt"] = int(args.repair_attempt)
    report["repair_source_report"] = args.repair_report
    print("===REPORT===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return 0 if report["status"] == "success" else 1


def _cmd_check(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    errs = check_plan(plan, executable=not args.frozen)
    print(json.dumps({"plan": args.plan, "frozen": bool(args.frozen),
                      "operations": len(plan.get("operations") or []),
                      "errors": errs, "ok": not errs}, ensure_ascii=False, indent=2))
    return 0 if not errs else 1


def _cmd_build(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    frozen_errs = check_plan(plan, executable=False)
    if frozen_errs:
        print(json.dumps({
            "built": None,
            "operations": len(plan.get("operations") or []),
            "frozen_check_errors": frozen_errs,
            "ok": False,
        }, ensure_ascii=False, indent=2))
        return 1

    exe = build_executable_plan(plan)
    errs = check_plan(exe, executable=True)
    if not errs:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(exe, f, ensure_ascii=False, indent=2)
    print(json.dumps({"built": args.out if not errs else None,
                      "operations": len(exe.get("operations") or []),
                      "check_errors": errs, "ok": not errs}, ensure_ascii=False, indent=2))
    return 0 if not errs else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nx-mcp-plan-runner",
                                description="Generic plan-driven executor for nx-mcp-modeling-planner plans")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="execute an executable plan against the resident NX Loader")
    pr.add_argument("plan")
    pr.add_argument("--workspace", default=None)
    pr.add_argument("--report", default=None)
    pr.add_argument("--mode", choices=("normal", "benchmark"), default="normal",
                    help="normal: never discard an unsaved part; "
                         "benchmark: allow overwriting the plan's own test part only")
    pr.add_argument("--allow-overwrite", action="store_true",
                    help="explicit permission to close the plan's test part without saving "
                         "(required in benchmark mode when the part is dirty)")
    pr.add_argument("--history", default=None,
                    help="run-history JSON (default: run_history.json next to runner.py)")
    pr.add_argument("--repair-attempt", type=int, choices=(0, 1), default=0,
                    help="0=normal first attempt; 1=the single allowed controlled repair attempt")
    pr.add_argument("--repair-report", default=None,
                    help="attempt-1 failed report; required when --repair-attempt 1")
    pr.set_defaults(func=_cmd_run)

    pc = sub.add_parser("check", help="static plan check (no NX)")
    pc.add_argument("plan")
    pc.add_argument("--frozen", action="store_true")
    pc.set_defaults(func=_cmd_check)

    pb = sub.add_parser("build", help="convert a frozen plan to the executable format (no NX)")
    pb.add_argument("plan")
    pb.add_argument("out")
    pb.set_defaults(func=_cmd_build)

    pd = sub.add_parser("validate-drawing", help="Gate A evidence/source validator (no NX)")
    pd.add_argument("drawing")
    pd.set_defaults(func=_cmd_validate_drawing)

    args = p.parse_args(argv)
    if inspect.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
