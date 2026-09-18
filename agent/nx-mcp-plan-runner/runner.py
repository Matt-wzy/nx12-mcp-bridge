# -*- coding: utf-8 -*-
"""nx-mcp-plan-runner — a generic, plan-driven executor for nx-agent modeling plans.

This Runner is PART-AGNOSTIC and PLAN-AGNOSTIC:
- it never branches on step numbers, feature names, or part dimensions;
- every behaviour is driven by the plan JSON (tool_args / selection_criteria /
  expectation / topology_changes) and by the executable-format extensions
  (result_bindings / selection_binding / $references / declared retries);
- it never invents repairs: a failed step stops the run (declared retries only).

Modes:
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
# certified tool contract (NX_MCP-Enhanced v2.1.1) — static data, not part-specific
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
            operations_completed += 1
            last_op_at = time.monotonic()
            msg = str(resp.get("message") or resp.get("result") or "")
            done = _parse_done(resp)
            if done is not None:
                msg += f" done={done}"
            steps_log.append({
                "step": step, "tool": tool, "started_at": started_at,
                "elapsed_seconds": round(dur, 3), "status": "ok",
                "retry_count": retried, "note": (msg + topo_note).strip(),
            })
        except Exception as e:
            dur = time.monotonic() - t0
            last_op_at = time.monotonic()
            steps_log.append({
                "step": step, "tool": tool, "started_at": started_at,
                "elapsed_seconds": round(dur, 3), "status": "failed",
                "retry_count": retried, "error": str(e),
            })
            status = "failed"
            failed_step = step
            if history is not None and planned_part:
                history.record_failed(planned_part)
            break

    loop_end = time.monotonic()
    nx_execution = (last_op_at - first_op_at) if (first_op_at and last_op_at) else 0.0
    validation_elapsed = ((last_op_at - validation_start_at)
                          if (validation_start_at is not None and last_op_at) else 0.0)
    report = {
        "status": status,
        "elapsed_seconds": round(loop_end - loop_start, 3),
        "runner_start_to_first_nx_call": round(first_op_at - wall_start, 3) if wall_start and first_op_at else None,
        "nx_execution_elapsed": round(nx_execution, 3),
        "final_validation_elapsed": round(validation_elapsed, 3),
        "step_export_settle_elapsed": round(export_settle_elapsed, 3),
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
        if tool in ("nx_circular_pattern", "nx_linear_pattern"):
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
        print(json.dumps({"status": "failed", "failed_step": None,
                          "errors": ["loader pipe not reachable"]}, ensure_ascii=False))
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

    blocked, info = await run_preflight(
        transport,
        plan,
        args.mode,
        args.allow_overwrite,
        history,
        repair_authorized=(args.repair_attempt == 1),
    )
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
    exe = build_executable_plan(plan)
    errs = check_plan(exe, executable=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(exe, f, ensure_ascii=False, indent=2)
    print(json.dumps({"built": args.out, "operations": len(exe.get("operations") or []),
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

    args = p.parse_args(argv)
    if inspect.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
