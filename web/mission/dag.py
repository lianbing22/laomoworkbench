"""DAG planning model (P1.2/M1): unit ids, dependencies, normalization.

The planner (LLM) only proposes a DAG; this module normalizes it into the v2
plan schema and validates it deterministically:
- every unit gets a stable `id` (provided or slugified, de-duplicated)
- `dependencies` are resolved by id OR title; unknown references are dropped
- cycles are broken by dropping the closing edge
Notes are returned as events so the reconciliation is observable.
"""
from __future__ import annotations

import re
from typing import Any

PLAN_VERSION = 2

# per-unit lifecycle states (P1.2 contract)
UNIT_PENDING = "pending"
UNIT_READY = "ready"
UNIT_RUNNING = "running"
UNIT_WAITING = "waiting"
UNIT_EVALUATING = "evaluating"
UNIT_REPAIRING = "repairing"
UNIT_PASSED = "passed"
UNIT_INTEGRATING = "integrating"
UNIT_INTEGRATED = "integrated"
UNIT_CONFLICT = "conflict"
UNIT_BLOCKED = "blocked"
UNIT_FAILED = "failed"
UNIT_CANCELLED = "cancelled"

# states that count as "the unit is occupying a worker slot"
UNIT_ACTIVE = {UNIT_READY, UNIT_RUNNING, UNIT_WAITING, UNIT_EVALUATING,
               UNIT_REPAIRING, UNIT_INTEGRATING}
# a dependency counts as satisfied when it reached at least this stage
UNIT_DEP_DONE = {UNIT_PASSED, UNIT_INTEGRATED}


def dependency_satisfied(dep_state: str, require_integrated: bool) -> bool:
    """Dependency gate for dispatching a dependent unit. With
    require_integrated (git missions: worktrees + serial integration) the
    dep must be INTEGRATED — a bare evaluator PASS is not enough, the
    work must have landed on the integration branch. Without it (non-git
    P1.1 fallback, integration is a no-op) a PASS counts as done."""
    return dep_state in UNIT_DEP_DONE and (not require_integrated
                                           or dep_state == UNIT_INTEGRATED)


def plan_requires_integration(plan: dict) -> bool:
    """True when the plan runs in git integration mode (per-unit worktrees +
    serial integration into an integration branch), where a dependency only
    satisfies dependents once INTEGRATED. Contract: the MissionManager stamps
    ``plan["gitIntegration"] = wtree.available`` at planning time (planning
    phase, before the first dispatch); this helper only reads that flag and
    defaults to False — no flag / False means the non-git P1.1 fallback in
    which integration is a no-op and a PASS satisfies dependents."""
    return bool(plan.get("gitIntegration"))


def slugify(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return s[:32]


def _creates_cycle(units: list[dict[str, Any]], u_id: str, dep_id: str) -> bool:
    adj: dict[str, list[str]] = {u["id"]: list(u.get("dependencies") or []) for u in units}
    adj.setdefault(u_id, []).append(dep_id)
    stack = [dep_id]
    seen: set[str] = set()
    while stack:
        n = stack.pop()
        if n == u_id:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, []))
    return False


def normalize_plan(raw_units: list[Any],
                   existing: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Build v2 plan units from raw planner output. `existing` carries the
    units already in the plan (replanning): ids/titles resolve against them,
    and cycle checks run over the combined graph. Returns (units, notes)."""
    units: list[dict[str, Any]] = []
    notes: list[str] = []
    base_index = len(existing or [])
    used_ids: set[str] = {u["id"] for u in (existing or [])}
    title_to_id: dict[str, str] = {u["title"]: u["id"] for u in (existing or [])}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for n, raw in enumerate(raw_units):
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        title = str(raw["title"])[:120]
        base = str(raw.get("id") or "").strip() or slugify(title) or f"unit-{n + 1}"
        uid = base
        while uid in used_ids:
            uid = f"{base}-{len(used_ids) + 1}"
            notes.append(f"单元 id 重复，已改名 {uid}")
        used_ids.add(uid)
        title_to_id[title] = uid
        unit = {
            "id": uid,
            "index": base_index + len(pairs),
            "title": title,
            "description": str(raw.get("description") or "")[:600],
            "acceptance": [str(a) for a in (raw.get("acceptance") or [])][:8],
            "dependencies": [],
            "state": UNIT_PENDING, "status": UNIT_PENDING,  # status = legacy mirror
            "attempt": 0, "repairCount": 0,
            "worktree": {"path": None, "branch": None, "baseSha": None, "headSha": None},
            "jobId": None, "delta": None, "repairDirective": None,
            "lastVerdict": None,
            "worker": {"startedAt": None, "finishedAt": None},
        }
        pairs.append((unit, raw))
    combined = list(existing or []) + [u for u, _ in pairs]
    by_id = {u["id"]: u for u in combined}
    for unit, raw in pairs:
        for ref in (raw.get("dependencies") or []):
            ref = str(ref).strip()
            if not ref:
                continue
            target = by_id.get(ref) or by_id.get(title_to_id.get(ref))
            if target is None:
                notes.append(f"单元 {unit['id']} 的依赖 {ref!r} 不存在，已忽略")
                continue
            if target["id"] == unit["id"]:
                notes.append(f"单元 {unit['id']} 自依赖，已忽略")
                continue
            if target["id"] not in unit["dependencies"]:
                unit["dependencies"].append(target["id"])
    for unit, _ in pairs:
        kept: list[str] = []
        for dep in unit["dependencies"]:
            if _creates_cycle(combined, unit["id"], dep):
                notes.append(f"单元 {unit['id']} 的依赖 {dep} 形成环，已忽略")
                continue
            kept.append(dep)
        unit["dependencies"] = kept
    return [u for u, _ in pairs], notes
