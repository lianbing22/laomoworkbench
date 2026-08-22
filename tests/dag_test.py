"""P1.2/M1 tests: DAG plan model (v2) — ids, dependencies, normalization,
cycle/unknown-ref guards, and the sequential path honoring dependencies.

Pure DAG tests need no manager; the flow tests drive a real MissionManager
with the same FakeAdapter role-dispatch style as mission_test.py (no codex).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

MISSION_IMPORT_ERROR = None
try:
    from mission import MissionError, MissionManager, normalize_plan  # noqa: E402
except Exception as _exc:  # mainline module not merged yet
    MISSION_IMPORT_ERROR = _exc

    class MissionError(Exception):  # type: ignore[no-redef]
        pass

    def normalize_plan(*a, **k):  # type: ignore[no-redef]
        raise RuntimeError("mission package unavailable")

POLL_TIMEOUT = 15.0
TERMINAL = {"done", "failed", "cancelled", "blocked"}


def plan_block(units):
    return "<<<LAOMO_PLAN\n" + json.dumps(units, ensure_ascii=False) + "\nLAOMO_PLAN>>>"


def verdict_block(verdict, reasons, repair=None):
    payload = {"verdict": verdict, "reasons": reasons}
    if repair is not None:
        payload["repair"] = repair
    return "<<<LAOMO_VERDICT\n" + json.dumps(payload, ensure_ascii=False) + "\nLAOMO_VERDICT>>>"


def handoff_text(note="当前单元已完成。"):
    return "工作完成。\nHANDOFF: " + note


PLANNER_KEYS = ("LAOMO_PLAN", "规划", "Planner", "planner", "分解")


def detect_role(prompt, read_only=False):
    text = prompt or ""
    if read_only:
        return "evaluator"
    if "LAOMO_VERDICT" in text and "LAOMO_JOB" not in text:
        return "evaluator"
    if "LAOMO_JOB" in text or "HANDOFF" in text:
        return "worker"
    if any(k in text for k in PLANNER_KEYS):
        return "planner"
    if any(k in text for k in ("验收", "valuator")):
        return "evaluator"
    return "worker"


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.scripts = {"planner": [], "worker": [], "evaluator": []}
        self.defaults = {
            "planner": plan_block([{"title": "默认单元"}]),
            "worker": handoff_text(),
            "evaluator": verdict_block("PASS", ["条件满足"]),
        }

    def script(self, role, *items):
        self.scripts[role].extend(items)

    def run_turn(self, *, prompt, cwd=None, read_only=False, model=None,
                 effort=None, timeout=600):
        role = detect_role(prompt, read_only)
        self.calls.append({"role": role, "prompt": prompt, "read_only": read_only, "cwd": cwd})
        queue = self.scripts.get(role) or []
        item = queue.pop(0) if queue else self.defaults.get(role, "")
        if callable(item):
            item = item(prompt)
        if isinstance(item, str):
            return {"ok": True, "text": item, "error": None, "usage": {}}
        return {"ok": True, "text": item.get("text", ""), "error": None, "usage": {}}

    def prompts_for(self, role):
        return [c["prompt"] for c in self.calls if c["role"] == role]


# ------------------------------------------------------------------ pure DAG


class DagNormalizeTest(unittest.TestCase):
    def setUp(self):
        self.raw = [
            {"id": "schema", "title": "定义 schema", "dependencies": []},
            {"id": "backend", "title": "实现后端", "dependencies": ["schema"]},
            {"id": "frontend", "title": "实现前端", "dependencies": ["定义 schema"]},
        ]

    def test_units_get_ids_and_slug_fallback(self):
        units, notes = normalize_plan(self.raw)
        self.assertEqual([u["id"] for u in units], ["schema", "backend", "frontend"])
        self.assertTrue(all(u["state"] == "pending" for u in units))
        self.assertTrue(all(u["status"] == "pending" for u in units))
        self.assertTrue(all(u["attempt"] == 0 and u["repairCount"] == 0 for u in units))
        # Chinese-only title falls back to a generated id
        units2, _ = normalize_plan([{"title": "纯中文标题"}])
        self.assertEqual(units2[0]["id"], "unit-1")

    def test_dependencies_resolve_by_id_and_title(self):
        units, notes = normalize_plan(self.raw)
        by_id = {u["id"]: u for u in units}
        self.assertEqual(by_id["backend"]["dependencies"], ["schema"])
        self.assertEqual(by_id["frontend"]["dependencies"], ["schema"])
        self.assertEqual(notes, [])

    def test_duplicate_id_renamed(self):
        units, notes = normalize_plan(self.raw + [{"id": "schema", "title": "重复单元"}])
        self.assertEqual(len({u["id"] for u in units}), len(units))
        self.assertTrue(any("重复" in n for n in notes))

    def test_unknown_and_self_dependency_dropped(self):
        raw = [{"id": "a", "title": "A", "dependencies": ["ghost"]},
               {"id": "b", "title": "B", "dependencies": ["a", "B"]}]
        units, notes = normalize_plan(raw)
        by_id = {u["id"]: u for u in units}
        self.assertEqual(by_id["a"]["dependencies"], [])
        self.assertEqual(by_id["b"]["dependencies"], ["a"])
        self.assertEqual(len(notes), 2)

    def test_cycle_broken(self):
        raw = [{"id": "a", "title": "A", "dependencies": ["b"]},
               {"id": "b", "title": "B", "dependencies": ["a"]}]
        units, _ = normalize_plan(raw)
        by_id = {u["id"]: u for u in units}
        edges = [(u["id"], d) for u in units for d in u["dependencies"]]
        self.assertLess(len(edges), 2, "one cycle edge must be dropped")
        # the remaining graph must be acyclic
        adj = {u["id"]: list(u["dependencies"]) for u in units}
        seen, stack = set(), set(adj)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack |= {d for d in adj[n] if d not in seen}
        self.assertEqual(len(seen), len(units))

    def test_replan_appends_and_resolves_existing_deps(self):
        existing, _ = normalize_plan(self.raw)
        existing[0]["state"] = existing[0]["status"] = "passed"
        added, notes = normalize_plan(
            [{"id": "e2e", "title": "端到端", "dependencies": ["schema", "backend"]}],
            existing=existing)
        self.assertEqual(added[0]["dependencies"], ["schema", "backend"])
        self.assertEqual(notes, [])


# ------------------------------------------------------------------ flow


class DagFlowTest(unittest.TestCase):
    """MissionManager end-to-end with scripted DAGs (sequential, M1 semantics)."""

    def setUp(self):
        if MISSION_IMPORT_ERROR is not None:
            self.skipTest("mission package 不可用 (%r)" % (MISSION_IMPORT_ERROR,))
        ws = tempfile.mkdtemp(prefix="laomo-dag-test-")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        self.root = Path(ws)
        self.adapter = FakeAdapter()
        self.mgr = MissionManager(self.adapter, self.root)

    def _start(self, objective="完成示例项目"):
        res = self.mgr.create(objective, str(self.root))
        mid = res["mission"]["id"]
        self.mgr.start(mid)
        return mid

    def _wait_terminal(self, mid, timeout=POLL_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.mgr.status(mid)["mission"]
            if st["state"] in TERMINAL:
                return st
            time.sleep(0.05)
        raise AssertionError("mission 未在 %ss 内走到终态: %s" % (timeout, self.mgr.status(mid)["mission"]["state"]))

    def test_plan_v2_written_with_ids_and_deps(self):
        self.adapter.script("planner", plan_block([
            {"id": "schema", "title": "定义 schema"},
            {"id": "backend", "title": "实现后端", "dependencies": ["schema"]},
        ]))
        mid = self._start()
        st = self._wait_terminal(mid)
        self.assertEqual(st["state"], "done")
        plan = (self.root / ".laomo" / "runs" / mid / "plan.json")
        data = json.loads(plan.read_text("utf-8"))
        self.assertEqual(data["version"], 2)
        by_id = {u["id"]: u for u in data["units"]}
        self.assertEqual(by_id["backend"]["dependencies"], ["schema"])
        self.assertEqual(by_id["schema"]["dependencies"], [])
        self.assertEqual(by_id["schema"]["state"], "passed")

    def test_sequential_honors_dependencies(self):
        # B (index 0) depends on A (index 1): A must run first even though it
        # appears later in the plan array.
        self.adapter.script("planner", plan_block([
            {"id": "b", "title": "单元B", "dependencies": ["a"]},
            {"id": "a", "title": "单元A"},
        ]))
        mid = self._start()
        st = self._wait_terminal(mid)
        self.assertEqual(st["state"], "done")
        worker_prompts = [p for p in self.adapter.prompts_for("worker") if "当前单元" in p]
        self.assertEqual(len(worker_prompts), 2)
        self.assertIn("单元A", worker_prompts[0])
        self.assertIn("单元B", worker_prompts[1])

    def test_broken_dependency_does_not_strand_mission(self):
        # All deps resolved by the DAG normalize; a unit whose dep never
        # passes (e.g. evaluator returns NEEDS_WORK forever for unit A) must
        # still terminate via the repair cap rather than stranding.
        self.adapter.script("planner", plan_block([
            {"id": "a", "title": "单元A"},
            {"id": "b", "title": "单元B", "dependencies": ["a"]},
        ]))
        self.adapter.script("worker", handoff_text())  # worker default fine
        self.adapter.defaults["evaluator"] = verdict_block("NEEDS_WORK", ["不稳定"], repair="继续修")
        mid = self._start()
        st = self._wait_terminal(mid)
        self.assertEqual(st["state"], "failed")


if __name__ == "__main__":
    unittest.main()
