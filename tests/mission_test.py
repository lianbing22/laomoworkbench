"""Unit tests for the P0.6 Durable Mission Loop (web/mission.py).

Black-box, contract-driven (docs/mission-contract.md): every assertion goes
through the public MissionManager API or the on-disk layout under
``<workspace_root>/.laomo/runs/<mission-id>/``. No real codex process is
involved — FakeAdapter scripts run_turn responses, including marker-block
injection (LAOMO_PLAN / LAOMO_JOB / LAOMO_VERDICT), unparseable output and
turn-latency simulation.

FakeAdapter role dispatch stays deliberately coarse (prompt wording is the
mainline's business, the contract only fixes the marker protocols):

* read_only=True                                  -> evaluator
* prompt teaches LAOMO_VERDICT (and no LAOMO_JOB) -> evaluator
* prompt teaches LAOMO_JOB / HANDOFF              -> worker
* prompt mentions LAOMO_PLAN / 规划 / Planner / 分解 -> planner
* prompt mentions 验收 / evaluator                 -> evaluator
* anything else                                   -> worker

Coverage map (numbers refer to the task list):
 01 create -> draft; start -> planning -> plan.json on disk (>=1 unit)
 02 all-PASS flow -> verifying -> done + progress/checkpoints/verdicts shape
 03 default-fail: unparseable evaluator output is NEEDS_WORK, never PASS
 04 NEEDS_WORK -> repair (repairCount+1, repairs/ RepairDirective) -> PASS
 05 repair over cap (>3) -> failed (stopReason mentions 修复/repair)
 06 no-progress: 2 consecutive cycles with unchanged unit map -> failed
 07 stop fuses: maxMissionCycles=1 and maxWallTimeSec=1 (+ fake sleep hook)
 08 builder claims completion but evaluator NEEDS_WORK -> never done
 09 LAOMO_JOB registration: waiting + jobs/<jobId>.json fields + wake delta
    flowing into the next worker prompt
 10 pause/resume/cancel idempotency + terminal-state protection
 11 crash-resume: rebuild MissionManager + recover() keeps waiting + active
 12 DONE triple gate: units not all passed -> never verifying/done
 13 start guard: a second mission cannot start while another is active

If web/mission.py is not importable (mainline not merged yet) every case is
skipped with a note — rerun after the mainline lands.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

MISSION_IMPORT_ERROR = None
try:
    from mission import MissionError, MissionManager  # noqa: E402
except Exception as _exc:  # mainline module not merged yet
    MISSION_IMPORT_ERROR = _exc

    class MissionError(Exception):  # type: ignore[no-redef]
        pass

    class MissionManager:  # type: ignore[no-redef]  placeholder; cases skip
        pass


POLL_INTERVAL = 0.05
POLL_TIMEOUT = 10.0
TERMINAL_STATES = {"done", "failed", "cancelled", "canceled", "blocked"}

DEFAULT_OBJECTIVE = "完成示例项目并产出总结报告"
WAKE_MARKER = "laomo-wake-marker-6f2a"


# ------------------------------------------------------------------ helpers


def mission_payload(result):
    """Extract the mission dict from an API response (tolerant envelope)."""
    if isinstance(result, dict):
        inner = result.get("mission")
        if isinstance(inner, dict):
            return inner
        return result
    return {}


def mission_id_of(result):
    payload = mission_payload(result)
    return payload.get("id") or payload.get("missionId") or payload.get("mission_id")


def plan_block(units):
    return "<<<LAOMO_PLAN\n" + json.dumps(units, ensure_ascii=False, indent=1) + "\nLAOMO_PLAN>>>"


def verdict_block(verdict, reasons, repair=None):
    payload = {"verdict": verdict, "reasons": reasons}
    if repair is not None:
        payload["repair"] = repair
    return "<<<LAOMO_VERDICT\n" + json.dumps(payload, ensure_ascii=False) + "\nLAOMO_VERDICT>>>"


def job_block(command, reason="等待后台作业", expected_seconds=30, cwd=None):
    spec = {"command": command, "reason": reason, "expectedSeconds": expected_seconds}
    if cwd is not None:
        spec["cwd"] = cwd
    return ("需要等待一个后台作业，本轮到此结束。\n<<<LAOMO_JOB\n"
            + json.dumps(spec, ensure_ascii=False) + "\nLAOMO_JOB>>>")


def handoff_text(note="当前单元已完成。", body="按单元要求完成了工作。"):
    return body + "\nHANDOFF: " + note


def rotating_handoff(prefix="修复进展"):
    """Worker factory: a DIFFERENT handoff each turn (so each evaluation cycle
    changes the progress signature — used to isolate the repair-limit fuse
    from the no-progress fuse)."""
    counter = {"n": 0}

    def generate(prompt):
        counter["n"] += 1
        return handoff_text(note="%s 第%d 次" % (prefix, counter["n"]))

    return generate


def sample_units(n=2):
    letters = "ABCDEFGH"
    return [
        {
            "index": i,
            "title": "单元" + letters[i],
            "description": "实现单元" + letters[i] + "并产出对应结果",
            "acceptance": ["产出包含单元" + letters[i] + "的结果"],
        }
        for i in range(n)
    ]


PLANNER_KEYS = ("LAOMO_PLAN", "规划", "Planner", "planner", "分解")


def detect_role(prompt, read_only=False):
    """Coarse prompt-feature dispatch (see module docstring)."""
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


DEFAULT_ROLE_TEXT = {
    "planner": plan_block(sample_units(1)),
    "worker": handoff_text(),
    "evaluator": verdict_block("PASS", ["条件满足"]),
}


class FakeAdapter:
    """Programmable run_turn stand-in (no real codex process).

    Each call is dispatched to a role, then answered from that role's FIFO
    script queue (strings become ok-results); when the queue is empty the
    role default is used. ``hook`` runs inside every turn — the wall-clock
    test uses it to fake latency.
    """

    def __init__(self):
        self.calls = []
        self.scripts = {"planner": [], "worker": [], "evaluator": []}
        self.defaults = dict(DEFAULT_ROLE_TEXT)
        self.hook = None  # callable(role, prompt) or None

    # -- scripting --
    def script(self, role, *items):
        self.scripts[role].extend(items)

    def set_default(self, role, item):
        self.defaults[role] = item

    # -- introspection --
    def calls_for(self, role):
        return [c for c in self.calls if c["role"] == role]

    def prompts_for(self, role):
        return [c["prompt"] for c in self.calls if c["role"] == role]

    # -- the adapter contract --
    def run_turn(self, *, prompt, cwd=None, read_only=False, model=None,
                 effort=None, timeout=600):
        role = detect_role(prompt, read_only)
        self.calls.append({"role": role, "prompt": prompt, "read_only": read_only,
                           "cwd": cwd, "text": None})
        if self.hook is not None:
            self.hook(role, prompt)
        queue = self.scripts.get(role) or []
        item = queue.pop(0) if queue else self.defaults.get(role, "")
        if callable(item):
            item = item(prompt)
        self.calls[-1]["text"] = self._as_result(item).get("text")
        return self._as_result(item)

    @staticmethod
    def _as_result(item):
        if isinstance(item, str):
            return {"ok": True, "text": item, "error": None, "usage": {"totalTokens": 1}}
        if isinstance(item, dict):
            return {"ok": bool(item.get("ok", True)), "text": item.get("text", ""),
                    "error": item.get("error"), "usage": item.get("usage") or {}}
        return {"ok": True, "text": str(item), "error": None, "usage": {}}


# ------------------------------------------------------------------ base class


class MissionLoopTest(unittest.TestCase):
    def setUp(self):
        if MISSION_IMPORT_ERROR is not None:
            self.skipTest("web/mission.py 尚未实现或无法导入 (%r) — 待主线合入后验证"
                          % (MISSION_IMPORT_ERROR,))
        ws = tempfile.mkdtemp(prefix="laomo-mission-test-")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        self.root = Path(ws)
        self.adapter = FakeAdapter()
        self.mgr = MissionManager(self.adapter, self.root)
        self.tracked = []  # (manager, mission_id) cancelled in tearDown

    def tearDown(self):
        for mgr, mid in self.tracked:
            self._cancel_quiet(mgr, mid)
        # give daemon runner threads a moment to finish their exit bookkeeping
        # before the temp workspace is removed
        time.sleep(0.2)

    @staticmethod
    def _cancel_quiet(mgr, mid):
        try:
            state = str(mission_payload(mgr.status(mid)).get("state", "")).lower()
            if state and state not in TERMINAL_STATES:
                mgr.cancel(mid)
        except Exception:
            pass

    # -- mission lifecycle conveniences --
    def create_mission(self, objective=DEFAULT_OBJECTIVE, acceptance=None,
                       options=None, cwd=None, mgr=None, verification=None):
        mgr = mgr or self.mgr
        try:
            result = mgr.create(objective, cwd=cwd, acceptance_criteria=acceptance,
                                options=options, verification=verification)
        except TypeError:  # parameter naming variant (acceptanceCriteria)
            result = mgr.create(objective, cwd=cwd, acceptanceCriteria=acceptance,
                                options=options, verification=verification)
        mid = mission_id_of(result)
        self.assertTrue(mid, "create() 应返回含 id 的 mission 摘要: %r" % (result,))
        self.track(mid, mgr=mgr)
        return mid

    def track(self, mid, mgr=None):
        self.tracked.append((mgr or self.mgr, mid))

    # -- disk layout --
    def mdir(self, mid):
        d = self.root / ".laomo" / "runs" / mid
        if d.is_dir():
            return d
        runs = self.root / ".laomo" / "runs"
        if runs.is_dir():
            for cand in runs.iterdir():
                if cand.name == mid:
                    return cand
        return d

    def sub_files(self, mid, folder, pattern="*"):
        d = self.mdir(mid) / folder
        if not d.is_dir():
            return []
        return sorted(d.glob(pattern))

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def read_ndjson(self, path):
        out = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    # -- state polling (manager internals are async; poll disk/API) --
    def status_of(self, mgr, mid):
        return mission_payload(mgr.status(mid))

    def state_vals(self, mgr, mid):
        vals = set()
        try:
            payload = self.status_of(mgr, mid)
            for key in ("state", "phase"):
                v = payload.get(key)
                if isinstance(v, str):
                    vals.add(v.lower())
            waiting = payload.get("waiting")
            if waiting:
                vals.add("waiting")
        except Exception:
            pass
        try:
            payload = self.read_json(self.mdir(mid) / "state.json")
            for key in ("state", "phase"):
                v = payload.get(key)
                if isinstance(v, str):
                    vals.add(v.lower())
            if payload.get("waitingJobId"):
                vals.add("waiting")
        except Exception:
            pass
        return vals

    def wait_state(self, mgr, mid, names, timeout=POLL_TIMEOUT):
        wanted = {n.lower() for n in names}
        deadline = time.monotonic() + timeout
        while True:
            vals = self.state_vals(mgr, mid)
            if vals & wanted:
                return True
            if (vals & TERMINAL_STATES) and not (wanted & TERMINAL_STATES):
                return False  # hit an unrelated terminal state; fail fast
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_INTERVAL)

    def wait_until(self, pred, timeout=POLL_TIMEOUT, desc="condition"):
        deadline = time.monotonic() + timeout
        while True:
            try:
                ok = bool(pred())
            except Exception:
                ok = False
            if ok:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_INTERVAL)

    def stop_reason(self, mgr, mid):
        parts = []
        for source in (self.status_of(mgr, mid),
                       self.read_json(self.mdir(mid) / "state.json")):
            for key in ("stopReason", "stop_reason"):
                v = source.get(key)
                if isinstance(v, str):
                    parts.append(v)
        return " ".join(parts).lower()

    def reason_has(self, mgr, mid, *keywords):
        reason = self.stop_reason(mgr, mid)
        return any(str(k).lower() in reason for k in keywords)


# ------------------------------------------------------------------ tests


class TestPlanAndHappyPath(MissionLoopTest):
    def test_01_create_draft_then_planner_writes_plan(self):
        mid = self.create_mission(acceptance=["最终产出包含总结报告"], options={})
        self.assertIn("draft", self.state_vals(self.mgr, mid),
                      "新建 mission 初始态应为 draft")

        listing = self.mgr.list()
        ids = [mission_id_of(m) for m in listing.get("missions", [])]
        self.assertIn(mid, [i for i in ids if i])
        self.assertFalse(listing.get("activeId"), "尚未 start，activeId 应为空")

        self.mgr.start(mid)

        def plan_ready():
            path = self.mdir(mid) / "plan.json"
            if not path.is_file():
                return None
            try:
                data = self.read_json(path)
            except Exception:
                return None
            units = data.get("units")
            return data if isinstance(units, list) and units else None

        data = None
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            data = plan_ready()
            if data:
                break
            time.sleep(POLL_INTERVAL)
        self.assertIsNotNone(data, "planner 应落盘 plan.json（含至少 1 个 unit）")
        for unit in data["units"]:
            for key in ("title", "description", "acceptance", "status", "repairCount"):
                self.assertIn(key, unit, "plan.json unit 缺少契约字段 %s: %r" % (key, unit))
            self.assertTrue(unit["acceptance"], "每个 unit 必须携带 acceptance: %r" % unit)

        meta = self.read_json(self.mdir(mid) / "mission.json")
        self.assertIn("objective", meta)
        self.assertIn("createdAt", meta)
        self.assertGreaterEqual(len(self.adapter.calls_for("planner")), 1,
                                "start 后应执行一次 planner turn")
        self.assertFalse({"draft"} & self.state_vals(self.mgr, mid),
                         "start 后状态不应停留在 draft")

    def test_02_all_pass_reaches_done_and_writes_disk(self):
        self.adapter.script("planner", plan_block(sample_units(2)))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"]),
                        "全部 PASS 应到达 done，实际: %s" % self.state_vals(self.mgr, mid))

        # status carries the full plan, all units passed, no repairs
        payload = self.status_of(self.mgr, mid)
        self.assertIn("plan", payload)
        units = payload["plan"]["units"]
        self.assertEqual(len(units), 2)
        for u in units:
            self.assertIn(str(u.get("status", "")).lower(), ("passed", "pass"),
                          "单元应全部 passed: %r" % u)
            self.assertEqual(u.get("repairCount", 0), 0)

        # the final evaluator must have run beyond the two per-unit evals
        self.assertGreaterEqual(len(self.adapter.calls_for("evaluator")), 3)

        # disk shapes
        d = self.mdir(mid)
        progress = d / "progress.md"
        self.assertTrue(progress.is_file(), "progress.md 应落盘")
        self.assertGreater(progress.stat().st_size, 0)
        self.assertTrue((d / "handoff.md").is_file(), "handoff.md 应落盘")

        checkpoints = self.sub_files(mid, "checkpoints", "*.md")
        self.assertGreaterEqual(len(checkpoints), 2,
                                "每单元至少一个 checkpoint: %s" % checkpoints)
        verdicts = [self.read_json(p) for p in self.sub_files(mid, "verdicts", "*.json")]
        self.assertGreaterEqual(len(verdicts), 2)
        for v in verdicts:
            self.assertIn("verdict", v)
        events = self.read_ndjson(d / "events.ndjson")
        self.assertGreaterEqual(len(events), 1)
        for ev in events:
            self.assertIn("type", ev)


class TestVerdictPaths(MissionLoopTest):
    def test_03_default_fail_unparseable_verdict_is_needs_work(self):
        # first eval: no parseable marker block at all; second eval: real PASS
        self.adapter.script(
            "evaluator",
            "我觉得差不多可以了，不过说不好，先这样吧。",
            verdict_block("PASS", ["二轮判定通过"]),
        )
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"]),
                        "修复后应到达 done，实际: %s" % self.state_vals(self.mgr, mid))

        # the unparseable output must have been recorded as NEEDS_WORK, never PASS
        verdicts = [self.read_json(p) for p in self.sub_files(mid, "verdicts", "*.json")]
        nw = [v for v in verdicts
              if str(v.get("verdict", "")).upper().replace("-", "_") in
              ("NEEDS_WORK", "NEEDSWORK")]
        self.assertGreaterEqual(len(nw), 1,
                                "不可解析输出必须按 NEEDS_WORK 落盘（绝不默认 PASS）: %s" % verdicts)
        units = self.read_json(self.mdir(mid) / "plan.json")["units"]
        self.assertEqual(sum(u.get("repairCount", 0) for u in units), 1,
                         "default-fail 应触发恰好一次修复")
        self.assertGreaterEqual(len(self.adapter.calls_for("worker")), 2,
                                "default-fail 之后应有一个修复 worker turn")
        self.assertGreaterEqual(len(self.sub_files(mid, "repairs", "*.md")), 1,
                                "default-fail 也应落盘 RepairDirective")

    def test_04_needs_work_repair_directive_then_pass(self):
        directive = "请在产出末尾补充一段使用说明"
        self.adapter.script(
            "evaluator",
            verdict_block("NEEDS_WORK", ["缺少使用说明段"], repair=directive),
            verdict_block("PASS", ["已补齐"]),
        )
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"]),
                        "修复后应到达 done，实际: %s" % self.state_vals(self.mgr, mid))

        units = self.read_json(self.mdir(mid) / "plan.json")["units"]
        self.assertEqual(sum(u.get("repairCount", 0) for u in units), 1,
                         "repairCount 应递增到 1")
        repairs = self.sub_files(mid, "repairs", "*.md")
        self.assertGreaterEqual(len(repairs), 1, "RepairDirective 应写入 repairs/")
        joined = "\n".join(p.read_text(encoding="utf-8") for p in repairs)
        self.assertIn("RepairDirective", joined, "repairs/ 内容应为 RepairDirective")
        self.assertIn(directive, joined, "RepairDirective 应包含具体修复指令")

    def test_05_repair_limit_trips_failed(self):
        # rotating handoff keeps the progress signature changing so the
        # repair cap is the fuse that fires (not no-progress)
        self.adapter.set_default("worker", rotating_handoff())
        self.adapter.set_default(
            "evaluator",
            verdict_block("NEEDS_WORK", ["缺少使用说明"], repair="请补充使用说明"),
        )
        mid = self.create_mission(options={"maxRepairPerTask": 3})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "修复超限应熔断为 failed，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertTrue(self.reason_has(self.mgr, mid, "修复", "repair"),
                        "stopReason 应指明修复超限: %r" % self.stop_reason(self.mgr, mid))
        units = self.read_json(self.mdir(mid) / "plan.json")["units"]
        self.assertGreaterEqual(units[0].get("repairCount", 0), 3,
                                "repairCount 应达到上限: %r" % units[0])
        self.assertGreaterEqual(len(self.sub_files(mid, "repairs", "*.md")), 3)


class TestStopPolicy(MissionLoopTest):
    def test_06_no_progress_trips_failed(self):
        # static worker output: unit status map + handoff hash stay identical
        # across consecutive evaluation cycles -> no-progress fuse
        self.adapter.set_default(
            "evaluator",
            verdict_block("NEEDS_WORK", ["缺少使用说明"], repair="请补充使用说明"),
        )
        mid = self.create_mission(options={"maxNoProgressCycles": 2})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "连续无进展应熔断为 failed，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertTrue(self.reason_has(self.mgr, mid, "进展", "progress"),
                        "stopReason 应指明无进展熔断: %r" % self.stop_reason(self.mgr, mid))
        units = self.read_json(self.mdir(mid) / "plan.json")["units"]
        self.assertLess(units[0].get("repairCount", 0), 3,
                        "no-progress 熔断应先于修复上限触发: %r" % units[0])

    def test_07a_max_mission_cycles_trips_failed(self):
        mid = self.create_mission(options={"maxMissionCycles": 1})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "轮次超限应熔断为 failed，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertTrue(self.reason_has(self.mgr, mid, "cycle"),
                        "stopReason 应指明轮次熔断: %r" % self.stop_reason(self.mgr, mid))

    def test_07b_max_wall_time_trips_failed(self):
        self.adapter.hook = lambda role, prompt: time.sleep(0.6)  # fake latency
        mid = self.create_mission(options={"maxWallTimeSec": 1})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "墙钟超限应熔断为 failed，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertTrue(self.reason_has(self.mgr, mid, "wall", "墙钟"),
                        "stopReason 应指明墙钟熔断: %r" % self.stop_reason(self.mgr, mid))

    def test_08_builder_completion_claim_never_done(self):
        # contract ironclad rule: the builder's own "all done" claim has no
        # effect; only evaluator verdicts move the mission
        self.adapter.set_default(
            "worker",
            "我认为整个任务已经全部完成，无需再做任何工作。\nHANDOFF: 全部完成，任务结束。",
        )
        self.adapter.set_default(
            "evaluator",
            verdict_block("NEEDS_WORK", ["尚未满足条件"], repair="请继续完成当前单元"),
        )
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "evaluator 一直 NEEDS_WORK 时应熔断，实际: %s"
                        % self.state_vals(self.mgr, mid))
        vals = self.state_vals(self.mgr, mid)
        self.assertNotIn("done", vals)
        self.assertNotIn("verifying", vals)
        verdicts = [self.read_json(p) for p in self.sub_files(mid, "verdicts", "*.json")]
        self.assertGreaterEqual(len(verdicts), 1)
        self.assertTrue(any(str(v.get("verdict", "")).upper() == "NEEDS_WORK"
                            for v in verdicts),
                        "builder 自称完成不能产生 PASS verdict: %s" % verdicts)


class TestBackgroundJob(MissionLoopTest):
    def test_09_job_registration_waiting_and_wake_delta(self):
        # sleep 1 guarantees an observable waiting window; the echo line
        # becomes the log-tail feature string that must reach the next prompt
        command = "sleep 1 && echo " + WAKE_MARKER
        self.adapter.script(
            "worker",
            job_block(command, reason="等待标记输出", expected_seconds=30),
            handoff_text(note="作业完成后收尾"),
        )
        mid = self.create_mission()
        self.mgr.start(mid)

        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]),
                        "注册作业后应进入 waiting，实际: %s" % self.state_vals(self.mgr, mid))
        payload = self.status_of(self.mgr, mid)
        waiting = payload.get("waiting")
        self.assertIsInstance(waiting, dict, "waiting 态 status 应携带作业描述: %r" % (waiting,))
        if isinstance(waiting, dict):
            for key in ("jobId", "command", "startedAt", "expectedWakeAt"):
                self.assertIn(key, waiting, "waiting 描述缺少 %s: %r" % (key, waiting))

        job_files = self.sub_files(mid, "jobs", "*.json")
        self.assertGreaterEqual(len(job_files), 1, "jobs/<jobId>.json 应落盘")
        job = self.read_json(job_files[0])
        for key in ("jobId", "pid", "command", "cwd", "logPath", "startedAt",
                    "expectedWakeAt", "completionCondition"):
            self.assertIn(key, job, "job 注册字段缺失 %s: %r" % (key, job))
        self.assertIsInstance(job["pid"], int)
        self.assertGreater(job["pid"], 0)
        if isinstance(waiting, dict) and waiting.get("jobId"):
            self.assertEqual(str(job["jobId"]), str(waiting["jobId"]))

        # job exits -> watcher wakes -> delta (log tail) enters next prompt
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"]),
                        "wake 后应继续走到 done，实际: %s" % self.state_vals(self.mgr, mid))
        prompts = self.adapter.prompts_for("worker")
        self.assertGreaterEqual(len(prompts), 2, "wake 后应有一个后续 worker turn")
        self.assertTrue(any(WAKE_MARKER in p for p in prompts[1:]),
                        "wake delta 应把日志尾部带给下一个 worker prompt")
        log_path = Path(str(job["logPath"]))
        self.assertTrue(log_path.is_file(), "作业日志应落盘: %s" % log_path)
        self.assertIn(WAKE_MARKER, log_path.read_text("utf-8", errors="replace"))


class TestControlOps(MissionLoopTest):
    def test_10_pause_resume_cancel_idempotency_and_terminal_guard(self):
        # (a) pausing a fresh draft mission is allowed and idempotent
        m1 = self.create_mission()
        self.mgr.pause(m1)
        self.assertTrue(self.wait_state(self.mgr, m1, ["paused"]),
                        "实际: %s" % self.state_vals(self.mgr, m1))
        self.mgr.pause(m1)  # idempotent repeat
        self.assertTrue(self.wait_state(self.mgr, m1, ["paused"]))
        self.mgr.cancel(m1)
        self.assertTrue(self.wait_state(self.mgr, m1, ["cancelled"]),
                        "实际: %s" % self.state_vals(self.mgr, m1))

        # (b) mid-flight control: park the mission in waiting via a long job
        self.adapter.script("worker", job_block("sleep 15", reason="长作业"))
        m2 = self.create_mission()
        self.mgr.start(m2)
        self.assertTrue(self.wait_state(self.mgr, m2, ["waiting"]),
                        "实际: %s" % self.state_vals(self.mgr, m2))

        self.mgr.pause(m2)
        self.assertTrue(self.wait_state(self.mgr, m2, ["paused"]),
                        "实际: %s" % self.state_vals(self.mgr, m2))
        self.mgr.pause(m2)  # idempotent repeat
        self.assertTrue(self.wait_state(self.mgr, m2, ["paused"]))
        self.mgr.resume(m2)  # returns to the pre-pause phase (waiting)
        self.assertTrue(self.wait_state(self.mgr, m2, ["waiting"]),
                        "resume 应回到暂停前相位，实际: %s" % self.state_vals(self.mgr, m2))

        # (c) cancel lands in a terminal state; every op is rejected afterwards
        self.mgr.cancel(m2)
        self.assertTrue(self.wait_state(self.mgr, m2, ["cancelled"]),
                        "实际: %s" % self.state_vals(self.mgr, m2))
        with self.assertRaises(MissionError):
            self.mgr.pause(m2)
        with self.assertRaises(MissionError):
            self.mgr.resume(m2)
        with self.assertRaises(MissionError):
            self.mgr.cancel(m2)


class TestStartGuard(MissionLoopTest):
    def test_11_second_start_rejected_while_another_active(self):
        self.adapter.script(
            "worker",
            job_block("sleep 15", reason="长作业一"),
            job_block("sleep 15", reason="长作业二"),
        )
        m1 = self.create_mission()
        self.mgr.start(m1)
        self.assertTrue(self.wait_state(self.mgr, m1, ["waiting"]),
                        "实际: %s" % self.state_vals(self.mgr, m1))
        self.assertEqual(str((self.mgr.list() or {}).get("activeId")), str(m1),
                         "活跃 mission 应出现在 activeId")

        m2 = self.create_mission()
        with self.assertRaises(MissionError):
            self.mgr.start(m2)  #已有 active mission

        self.mgr.cancel(m1)
        self.assertTrue(self.wait_state(self.mgr, m1, ["cancelled"]),
                        "实际: %s" % self.state_vals(self.mgr, m1))
        self.assertTrue(
            self.wait_until(lambda: not (self.mgr.list() or {}).get("activeId"),
                            timeout=5, desc="activeId 清空"),
            "取消后 activeId 应清空")
        self.mgr.start(m2)  # now allowed
        self.assertTrue(self.wait_state(self.mgr, m2, ["waiting"]),
                        "实际: %s" % self.state_vals(self.mgr, m2))
        self.assertEqual(str((self.mgr.list() or {}).get("activeId")), str(m2))


class TestCrashResume(MissionLoopTest):
    def test_12_crash_resume_waiting_mission_stays_active(self):
        self.adapter.script("worker", job_block("sleep 15", reason="跨重启作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]),
                        "实际: %s" % self.state_vals(self.mgr, mid))

        job_files = self.sub_files(mid, "jobs", "*.json")
        self.assertGreaterEqual(len(job_files), 1)
        job_path = job_files[0]
        job = self.read_json(job_path)
        # simulate "the background job outlived the gateway crash": pin a
        # consistent pid+pgid+start identity onto a dedicated live process so
        # recover() must see a live, *matching* pid (an identity mismatch
        # means the pid was recycled and the job is treated as lost). The
        # standin must be its own process group: cancel/terminate would
        # SIGKILL it and must never touch the test runner.
        standin = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: standin.poll() is not None or standin.kill())
        job["pid"] = standin.pid
        job["pgid"] = os.getpgid(standin.pid)
        try:
            job["startIdentity"] = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(standin.pid)],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except OSError:
            job["startIdentity"] = ""
        tmp = Path(str(job_path) + ".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, job_path)

        # rebuild the control plane from disk (as after a gateway restart)
        fresh = MissionManager(FakeAdapter(), self.root)
        self.addCleanup(self._cancel_quiet, fresh, mid)
        fresh.recover()

        self.assertTrue(self.wait_state(fresh, mid, ["waiting"], timeout=5),
                        "recover() 应恢复 waiting 态，实际: %s" % self.state_vals(fresh, mid))
        active = (fresh.list() or {}).get("activeId")
        self.assertIn(str(active), (str(mid),), "恢复后 mission 应仍为 active")
        time.sleep(0.3)  # a live pid must NOT be woken
        self.assertIn("waiting", self.state_vals(fresh, mid),
                      "存活的作业不应被唤醒，实际: %s" % self.state_vals(fresh, mid))


class TestDoneTripleGate(MissionLoopTest):
    def test_13_not_all_units_passed_never_verifying_or_done(self):
        self.adapter.script("planner", plan_block(sample_units(2)))
        self.adapter.set_default("worker", rotating_handoff())
        self.adapter.set_default(
            "evaluator",
            verdict_block("NEEDS_WORK", ["尚未满足条件"], repair="请继续完成当前单元"),
        )
        mid = self.create_mission(options={"maxRepairPerTask": 3})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"]),
                        "存在未通过单元时不得 done，实际: %s" % self.state_vals(self.mgr, mid))

        units = self.read_json(self.mdir(mid) / "plan.json")["units"]
        statuses = [str(u.get("status", "")).lower() for u in units]
        self.assertFalse(all(s in ("passed", "pass") for s in statuses),
                         "不应全部 passed: %s" % statuses)
        self.assertNotIn("done", self.state_vals(self.mgr, mid))
        # verifying would have run the final evaluator; it must never happen
        self.assertFalse((self.mdir(mid) / "verdicts" / "final.json").is_file(),
                         "未全部通过时不得进入 verifying（不应有 final verdict）")
        events = self.read_ndjson(self.mdir(mid) / "events.ndjson")
        self.assertFalse(any(e.get("type") == "final-verdict" for e in events),
                         "不应出现 final-verdict 事件")


# ------------------------------------------------------------------ P1.1 suite


def _truly_dead(pid):
    """A pid that is gone or a zombie counts as dead (kernel-level exit)."""
    try:
        out = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
        state = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        state = ""
    return state == "" or state.startswith("Z")


def _killpg_quiet(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class TestBlockedTerminalGate(MissionLoopTest):
    def test_14_blocked_is_real_terminal(self):
        self.adapter.script("evaluator", verdict_block("BLOCKED", ["外部依赖不可用"]))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["blocked"]),
                        "BLOCKED 应到达 blocked 终态，实际: %s" % self.state_vals(self.mgr, mid))
        for op, name in ((self.mgr.start, "start"), (self.mgr.pause, "pause"),
                         (self.mgr.cancel, "cancel")):
            with self.assertRaises(MissionError, msg="终态后 %s 必须拒绝" % name):
                op(mid)
        fresh = MissionManager(FakeAdapter(), self.root)
        self.assertEqual(fresh.recover(), [], "终态不应被 recover 继续推进")
        self.assertIsNone((fresh.list() or {}).get("activeId"))
        self.assertTrue(self.reason_has(self.mgr, mid, "blocked", "block"),
                        "stopReason 应指明 BLOCKED: %r" % self.stop_reason(self.mgr, mid))


class TestJobLifecycleOwnership(MissionLoopTest):
    def test_15_cancel_terminates_waiting_job_process(self):
        self.adapter.script("worker", job_block("sleep 60", reason="长跑作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        job_path = self.sub_files(mid, "jobs", "*.json")[0]
        job = self.read_json(job_path)
        self.assertEqual(job.get("status"), "running")
        pid, pgid = int(job.get("pid") or 0), int(job.get("pgid") or 0)
        self.assertGreater(pid, 0)
        self.assertTrue(job.get("startIdentity"), "作业应记录 start identity")
        self.assertTrue(job.get("commandHash"), "作业应记录 command hash")
        self.mgr.cancel(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["cancelled"]))
        self.assertTrue(self.wait_until(lambda: _truly_dead(pid),
                                        timeout=10, desc="job 进程彻底退出"),
                        "cancel 后作业进程必须真正死亡 (pid=%s pgid=%s)" % (pid, pgid))
        for j in self.mgr.status(mid)["mission"]["jobs"]:
            if j["jobId"] == job["jobId"]:
                self.assertEqual(j.get("status"), "cancelled")
                self.assertIn(j.get("exitKind"), ("terminated", "exited"))
                break
        else:
            self.fail("cancel 后作业记录应保留")
        self.assertEqual((self.mgr.list() or {}).get("activeId"), None)

    def test_16_job_failed_exit_persisted(self):
        self.adapter.script("worker", job_block("exit 5", reason="会失败的作业"),
                            handoff_text())
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20),
                        "作业失败应被当成一次唤醒继续走完，实际: %s" % self.state_vals(self.mgr, mid))
        job = self.read_json(self.sub_files(mid, "jobs", "*.json")[0])
        self.assertEqual(job.get("status"), "failed")
        self.assertEqual(job.get("exitCode"), 5)
        self.assertEqual(job.get("exitKind"), "exited")

    def test_17_job_orphaned_when_died_while_paused(self):
        self.adapter.script("worker", job_block("sleep 60", reason="长跑作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        job = self.read_json(self.sub_files(mid, "jobs", "*.json")[0])
        pid = int(job["pid"])
        self.addCleanup(lambda: _killpg_quiet(int(job.get("pgid") or pid)))
        self.mgr.pause(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"]))

        def threads_dead():
            runner = self.mgr._runners.get(mid)
            watcher = self.mgr._watchers.get(mid)
            return ((runner is None or not runner.is_alive())
                    and (watcher is None or not watcher.is_alive()))

        self.assertTrue(self.wait_until(threads_dead, timeout=10,
                                        desc="runner 与 watcher 线程完全退出"),
                        "暂停后 runner/watcher 线程必须退出（不再观察作业）")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self.assertTrue(self.wait_until(lambda: _truly_dead(pid), timeout=8,
                                        desc="job 进程死亡"),
                        "kill 后作业进程必须真正死亡")
        # job finished while paused: resume must wake immediately, not hang
        self.mgr.resume(mid)
        self.assertTrue(self.wait_until(
            lambda: self.read_json(self.sub_files(mid, "jobs", "*.json")[0]).get("status") == "orphaned",
            timeout=8, desc="作业标记 orphaned"),
            "暂停期间死亡的作业应标记 orphaned（当前: %s）"
            % self.read_json(self.sub_files(mid, "jobs", "*.json")[0]).get("status"))
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20),
                        "resume 后应继续直至 done，实际: %s" % self.state_vals(self.mgr, mid))

    def test_18_waiting_pause_keeps_job_running_resume_reattaches(self):
        self.adapter.script("worker", job_block("sleep 60", reason="长跑作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        job = self.read_json(self.sub_files(mid, "jobs", "*.json")[0])
        pid = int(job["pid"])
        self.addCleanup(lambda: _killpg_quiet(int(job.get("pgid") or pid)))
        self.mgr.pause(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"]))
        time.sleep(1.0)
        self.assertFalse(_truly_dead(pid), "暂停不终止后台作业")
        self.mgr.resume(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"], timeout=10),
                        "resume 后应回 waiting（watcher 重新挂上），实际: %s" % self.state_vals(self.mgr, mid))
        time.sleep(1.0)
        self.assertIn("waiting", self.state_vals(self.mgr, mid),
                      "等待中不得自行推进; 实际: %s" % self.state_vals(self.mgr, mid))
        # now let the watcher do its job: kill the job -> mission wakes
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20),
                        "resume 后 watcher 应重新生效，实际: %s" % self.state_vals(self.mgr, mid))


class TestCrashRecoveryIdentity(MissionLoopTest):
    def test_19_pid_reuse_detected_and_job_orphaned(self):
        self.adapter.script("worker", job_block("sleep 60", reason="跨重启作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        job_path = self.sub_files(mid, "jobs", "*.json")[0]
        job = self.read_json(job_path)
        real_pid, real_pgid = int(job["pid"]), int(job.get("pgid") or job["pid"])
        self.addCleanup(lambda: _killpg_quiet(real_pgid))
        # the pid got recycled: a live process that is NOT ours
        standin = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: standin.poll() is not None or standin.kill())
        job["pid"] = standin.pid
        job["pgid"] = os.getpgid(standin.pid)
        job["startIdentity"] = "Wed Jan  1 00:00:00 1970"
        tmp = Path(str(job_path) + ".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, job_path)
        # old control plane's runner+watcher are out of the picture before the
        # fresh one recovers (the old runner would otherwise sit parked)
        old_runner = self.mgr._runners.get(mid)
        if old_runner:
            old_runner.request_stop()

        fresh = MissionManager(self.adapter, self.root)
        self.addCleanup(self._cancel_quiet, fresh, mid)
        fresh.recover()
        self.assertTrue(self.wait_until(
            lambda: self.read_json(job_path).get("status") == "orphaned",
            timeout=8, desc="PID 复用应标记 orphaned"),
            "identity 不匹配的 pid 不得被当作存活作业（当前: %s）"
            % self.read_json(job_path).get("status"))
        self.assertTrue(self.wait_until(
            lambda: self.read_json(self.mdir(mid) / "state.json").get("waitingJobId") is None,
            timeout=8, desc="mission 离开 waiting"),
            "PID 复用后 mission 应立即被唤醒，实际: %s" % self.state_vals(fresh, mid))


class TestTimeBudget(MissionLoopTest):
    def test_20_time_buckets_invariant_after_done(self):
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20))
        st = self.read_json(self.mdir(mid) / "state.json")
        for key in ("wallElapsedMs", "agentActiveMs", "waitingMs", "pausedMs"):
            self.assertIn(key, st, "状态桶 %s 必须持久化" % key)
            self.assertGreaterEqual(int(st.get(key, 0) or 0), 0)
        self.assertEqual(int(st["wallElapsedMs"]),
                         int(st["agentActiveMs"]) + int(st["waitingMs"]),
                         "wall = agent + waiting（paused 单独计）")
        time_in_summary = self.status_of(self.mgr, mid)["time"]
        self.assertEqual(int(time_in_summary["wallElapsedMs"]), int(st["wallElapsedMs"]))

    def test_21_paused_time_excluded_from_wall_budget(self):
        self.adapter.script("worker", job_block("sleep 60", reason="长跑作业"))
        # wall budget of 2s would be blown instantly by a 2.5s pause if
        # paused time counted against the mission wall clock
        mid = self.create_mission(options={"maxWallTimeSec": 2})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        self.mgr.pause(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"]))
        time.sleep(2.5)
        self.mgr.resume(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"], timeout=10))
        self.mgr.cancel(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["cancelled"]))
        self.assertNotIn("failed", self.state_vals(self.mgr, mid),
                         "暂停不得消耗 wall budget，实际: %s" % self.state_vals(self.mgr, mid))
        t = self.status_of(self.mgr, mid)["time"]
        self.assertGreaterEqual(int(t["pausedMs"]), 2000, "pausedMs 应覆盖暂停时长")
        self.assertLess(int(t["wallElapsedMs"]), 2000,
                        "wallElapsedMs 不应包含暂停时间: %s" % t)


class TestNoProgressPersist(MissionLoopTest):
    def test_22_no_progress_survives_restart(self):
        self.adapter.set_default("evaluator",
                                 verdict_block("NEEDS_WORK", ["未见进展"], repair="请继续"))
        mid = self.create_mission(options={"maxNoProgressCycles": 2})
        # freeze the 3rd evaluator turn so we can pause mid-flight with the
        # no-progress counter already >= 1 on disk (persisted at eval #2)
        n = {"e": 0}
        frozen = threading.Event()
        release = threading.Event()

        def hook(role, prompt):
            if role == "evaluator":
                n["e"] += 1
                if n["e"] == 3:
                    frozen.set()
                    release.wait(timeout=15)

        self.adapter.hook = hook
        self.mgr.start(mid)
        self.assertTrue(self.wait_until(lambda: frozen.is_set(), timeout=30,
                                        desc="第 3 次 evaluation 被冻结"))
        st = self.read_json(self.mdir(mid) / "state.json")
        no_before = int(st.get("noProgress") or 0)
        self.assertGreaterEqual(no_before, 1,
                                "no-progress 计数应先持久化到磁盘（当前 %s）" % no_before)
        self.assertTrue(st.get("progressSignature"), "progressSignature 应持久化")
        self.mgr.pause(mid)   # control plane stops the in-flight runner
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"]))
        runner = self.mgr._runners.get(mid)
        self.adapter.hook = None   # no more freezes on the post-release turns
        release.set()
        if runner:
            runner.join(timeout=15)
            self.assertFalse(runner.is_alive(), "释放后 runner 必须退出（暂停即停）")

        fresh = MissionManager(self.adapter, self.root)
        self.addCleanup(self._cancel_quiet, fresh, mid)
        fresh.resume(mid)     # crash-recovery: same adapter, fresh control plane
        self.assertTrue(self.wait_state(fresh, mid, ["failed"], timeout=20),
                        "重启后无进展熔断应继续生效，实际: %s" % self.state_vals(fresh, mid))
        st2 = self.read_json(self.mdir(mid) / "state.json")
        self.assertGreaterEqual(int(st2.get("noProgress") or 0), no_before,
                                "重启后计数器不得回退（%s -> %s）"
                                % (no_before, st2.get("noProgress")))
        self.assertLessEqual(int(st2.get("noProgress") or 0), no_before + 1,
                             "重启后最多再计一轮: %s" % st2.get("noProgress"))
        self.assertIn("无进展", self.stop_reason(fresh, mid))


class TestHarnessVerificationGate(MissionLoopTest):
    def test_23_machine_gate_fail_blocks_done_then_fix_passes(self):
        marker = self.root / "marker.txt"

        def fix(prompt):
            if "机器验收未通过" in prompt:
                marker.write_text("ok", encoding="utf-8")
            return handoff_text()

        self.adapter.script("worker", handoff_text(), fix)
        self.adapter.set_default("evaluator", verdict_block("PASS", ["条件满足"]))
        mid = self.create_mission(cwd=str(self.root), verification={
            "commands": ["test -f marker.txt"],
            "requiredFiles": ["marker.txt"],
        })
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=30),
                        "修复后机器门禁应最终 PASS 并 DONE，实际: %s" % self.state_vals(self.mgr, mid)
                        + " | " + self.stop_reason(self.mgr, mid))
        st = self.read_json(self.mdir(mid) / "state.json")
        self.assertEqual(st.get("verifyResult"), "pass")
        final = self.sub_files(mid, "verdicts", "final.json")
        self.assertTrue(final, "final evaluator 应运行过（fresh final 门禁）")
        results = self.read_json(self.sub_files(mid, "verification", "results.json")[0])
        self.assertTrue(results.get("passed"), "最终机器门禁应为 PASS")

    def test_24_machine_gate_fail_forever_never_done(self):
        self.adapter.set_default("evaluator", verdict_block("PASS", ["条件满足"]))
        mid = self.create_mission(cwd=str(self.root), verification={
            "commands": ["exit 9", "echo hi"],
            "requiredFiles": ["nope-missing.txt"],
        })
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["failed"], timeout=30),
                        "机器门禁永远失败时不得 DONE，实际: %s" % self.state_vals(self.mgr, mid)
                        + " | " + self.stop_reason(self.mgr, mid))
        self.assertNotIn("done", self.state_vals(self.mgr, mid))
        st = self.read_json(self.mdir(mid) / "state.json")
        self.assertEqual(st.get("verifyResult"), "fail")
        self.assertFalse(self.sub_files(mid, "verdicts", "final.json"),
                         "机器门禁未过不得跑 final evaluator")
        results = self.read_json(self.sub_files(mid, "verification", "results.json")[0])
        self.assertFalse(results.get("passed"))
        by_key = {(c["kind"], c["name"]): c for c in results["checks"]}
        cmd = by_key[("command", "exit 9")]
        self.assertEqual(cmd.get("exitCode"), 9)
        for field in ("command", "stdoutTail", "stderrTail", "startedAt", "endedAt", "resultHash"):
            self.assertIn(field, cmd, "每个检查结果必须包含完整字段 %s" % field)
        self.assertEqual(cmd.get("passed"), False)
        self.assertIn("hi", by_key[("command", "echo hi")].get("stdoutTail", ""))
        self.assertIn("missing", by_key[("file", "nope-missing.txt")].get("error", ""))
        self.assertTrue(self.sub_files(mid, "verification", "raw/*.stdout"),
                        "原始 stdout 应保留")

    def test_25_http_check_gate(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302 if self.path.startswith("/redirect") else 200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        ok_url = "http://127.0.0.1:%d/" % server.server_port
        self.adapter.set_default("evaluator", verdict_block("PASS", ["ok"]))
        mid = self.create_mission(cwd=str(self.root),
                                  verification={"httpChecks": [{"url": ok_url}]})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20),
                        "HTTP 检查通过应 DONE，实际: %s" % self.state_vals(self.mgr, mid))
        results = self.read_json(self.sub_files(mid, "verification", "results.json")[0])
        self.assertTrue(results["checks"][0]["passed"] and results["checks"][0]["kind"] == "http")

    def test_38_non_git_mission_verifies_in_mission_cwd(self):
        """M5-B ⑤ 回退契约：非 git 工作区的机器门禁与 final evaluator 仍以
        mission cwd 为准（门禁摘要记录 cwd 作为证据），不创建 integration
        worktree、不产生 integration 事件。"""
        marker = self.root / "marker.txt"

        def plant(prompt):
            marker.write_text("ok", encoding="utf-8")
            return handoff_text()

        self.adapter.script("worker", plant)
        self.adapter.set_default("evaluator", verdict_block("PASS", ["条件满足"]))
        mid = self.create_mission(cwd=str(self.root), verification={
            "commands": ["test -f marker.txt"], "requiredFiles": ["marker.txt"]})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=30),
                        "非 git 回退应照常 DONE，实际: %s" % self.state_vals(self.mgr, mid)
                        + " | " + self.stop_reason(self.mgr, mid))
        results = self.read_json(self.sub_files(mid, "verification", "results.json")[0])
        self.assertEqual(results.get("cwd"), str(self.root.resolve()),
                         "门禁摘要必须记录实际运行的 cwd（证据化）")
        final_calls = self.adapter.calls_for("evaluator")
        self.assertTrue(final_calls, "final evaluator 应已运行")
        last = final_calls[-1]
        self.assertEqual(last["cwd"], str(self.root.resolve()),
                         "非 git 使命的 final evaluator cwd 应为 mission cwd")
        self.assertIn(f"工作区：{last['cwd']}", last["prompt"])
        self.assertFalse((self.mdir(mid).parent.parent / "worktrees" / mid
                          / "integration").is_dir(),
                         "非 git 使命不得创建 integration worktree")
        kinds = {e["type"] for e in self.read_ndjson(self.mdir(mid) / "events.ndjson")}
        self.assertNotIn("integration", kinds)


class TestEvidenceManifest(MissionLoopTest):
    def test_39_done_without_manifest_recover_repairs_evidence(self):
        """DONE 状态落盘与 manifest 写入之间的崩溃窗口：recover() 对
        done-but-no-manifest 的 mission 幂等重建 evidence，且不复活 runner。"""
        marker = self.root / "marker.txt"

        def plant(prompt):
            marker.write_text("ok", encoding="utf-8")
            return handoff_text()

        self.adapter.script("worker", plant)
        self.adapter.set_default("evaluator", verdict_block("PASS", ["条件满足"]))
        mid = self.create_mission(cwd=str(self.root), verification={
            "requiredFiles": ["marker.txt"]})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=30))
        manifest = self.mdir(mid) / "evidence" / "manifest.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not manifest.is_file():
            time.sleep(POLL_INTERVAL)
        self.assertTrue(manifest.is_file(), "DONE 后应落盘 manifest")
        # 模拟崩溃窗口：manifest 丢失
        manifest.unlink()
        self.mgr.recover()
        self.assertTrue(manifest.is_file(), "recover 必须重建缺失的 manifest")
        data = self.read_json(manifest)
        self.assertEqual(data.get("state"), "done")
        self.assertIn("artifact/marker.txt", data.get("entries") or {})
        # 幂等：重建结果确定（相同 sha256），且不复活已终结 mission
        first = data.get("sha256")
        manifest.unlink()
        self.mgr.recover()
        again = self.read_json(manifest)
        self.assertEqual(again.get("sha256"), first, "重建必须幂等")
        self.assertEqual(self.state_vals(self.mgr, mid) & {"done"}, {"done"},
                         "recover 不得复活已终结的 mission")

    def test_26_manifest_at_done_with_hashes_immutable(self):
        artifact = self.root / "artifact.txt"
        artifact.write_text("v1", encoding="utf-8")
        mid = self.create_mission(cwd=str(self.root),
                                  verification={"requiredFiles": ["artifact.txt"]})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20))
        manifest_path = self.mdir(mid) / "evidence" / "manifest.json"
        self.assertTrue(self.wait_until(lambda: manifest_path.is_file(),
                                        timeout=5, desc="manifest 生成"),
                        "DONE 后 evidence manifest 应在数毫秒内落盘")
        manifest = self.read_json(manifest_path)
        self.assertEqual(manifest.get("state"), "done")
        entries = manifest.get("entries") or {}
        for rel in ("mission.json", "state.json", "plan.json",
                    "verification/results.json", "artifact/artifact.txt"):
            self.assertIn(rel, entries, "manifest 应包含 %s（实际有 %s）"
                          % (rel, ", ".join(sorted(entries))))
        for rel, entry in entries.items():
            self.assertIn("path", entry)
            self.assertIn("sha256", entry, "条目 %s 缺少 sha256" % rel)
            self.assertIn("generatedAt", entry, "条目 %s 缺少 generatedAt" % rel)
            if entry.get("sha256"):
                self.assertEqual(len(entry["sha256"]), 64)
        # immutability: nothing may rewrite the manifest after DONE
        before = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        artifact.write_text("v2", encoding="utf-8")
        after = json.dumps(self.read_json(manifest_path), sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after, "DONE 后 manifest 不得被后续变更覆盖")
        self.assertEqual(entries["artifact/artifact.txt"].get("sha256"),
                         manifest["sha256"] and entries["artifact/artifact.txt"]["sha256"])


class TestShutdownGate(MissionLoopTest):
    def test_27_terminal_mission_rejects_all_operations(self):
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=20))
        for op, name in ((self.mgr.start, "start"), (self.mgr.pause, "pause"),
                         (self.mgr.cancel, "cancel")):
            with self.assertRaises(MissionError, msg="DONE 后 %s 必须拒绝" % name):
                op(mid)
        fresh = MissionManager(FakeAdapter(), self.root)
        self.assertEqual(fresh.recover(), [], "DONE 后 recover 必须跳过")
        self.assertEqual((fresh.list() or {}).get("activeId"), None)
        # no managed job may still be alive after DONE terminal
        jobs = self.mgr.status(mid)["mission"]["jobs"]
        self.assertFalse(any(j.get("status") == "running" for j in jobs),
                         "终态不得残留 running 作业")


class TestWatcherReattachReap(MissionLoopTest):
    """A watcher re-attached after pause/resume has no Popen handle; it must
    still record the REAL exit status via waitpid (we are the parent) instead
    of failing a healthy exit with exitCode=null (caught live in Gate A)."""

    def _reattach_job(self, command):
        self.adapter.script("worker", job_block(command, reason="重挂作业"))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"]))
        job_path = self.sub_files(mid, "jobs", "*.json")[0]
        job = self.read_json(job_path)
        self.mgr.pause(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"]))
        time.sleep(1.0)  # let the runner/watcher fully stop before re-attach
        self.mgr.resume(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["waiting"], timeout=10),
                        "resume 后 watcher 必须重挂 job=%s" % job["jobId"])
        return job_path, job

    def test_28a_reattached_watcher_reaps_killed_exit_code(self):
        job_path, job = self._reattach_job("sleep 60")
        os.kill(int(job["pid"]), signal.SIGKILL)
        self.assertTrue(self.wait_until(
            lambda: self.read_json(job_path).get("status") == "failed", timeout=10,
            desc="kill -9 后标记 failed"),
            "重挂 watcher 后作业被杀应标记 failed")
        j = self.read_json(job_path)
        self.assertEqual(j.get("exitCode"), 137, "waitpid 必须取到真实退出码 128+9")
        self.assertFalse(j.get("exitUnknown"), "同进程可回收退出码，不得标 unknown")
        self.assertEqual(j.get("exitKind"), "exited")

    def test_28b_reattached_watcher_natural_exit_completed(self):
        job_path, job = self._reattach_job("sleep 8")
        self.assertTrue(self.wait_until(
            lambda: self.read_json(job_path).get("status") == "completed",
            timeout=20, desc="自然退出应为 completed"),
            "重挂 watcher 后健康退出必须 completed，而不是 failed/null")
        j = self.read_json(job_path)
        self.assertEqual(j.get("exitCode"), 0)
        self.assertFalse(j.get("exitUnknown"))
        self.assertEqual(j.get("exitKind"), "exited")


class TestParallelWorkUnits(MissionLoopTest):
    """P1.2/M4: MissionScheduler — 多工作单元并行 + 依赖就绪 + 并发上限。

    * 无依赖单元必须真并发（worker 阶段重叠、先后 dispatch 同处一个窗口）
    * 带依赖单元必须等前置 passed/integrated 后才被调度（不许抢跑）
    * maxParallelWorkers=1 退化为与 P1.1 等价的串行执行
    * 并发 worker 数永远不超过硬上限 MAX_PARALLEL_WORKERS(4)
    * 每个单元持独立 lease 令牌；结束后 lease 必须释放
    """

    @staticmethod
    def _slow_worker(records, delay=0.5, label=None):
        """worker 轮次脚本：sleep 后记录 (start, end)，返回标准 handoff。"""
        def slow(prompt):
            start = time.monotonic()
            time.sleep(delay)
            records.append({"label": label, "start": start, "end": time.monotonic()})
            return handoff_text()
        return slow

    def _events(self, mid):
        return self.read_ndjson(self.mdir(mid) / "events.ndjson")

    def _verdict_events(self, mid, unit):
        return [e for e in self._events(mid)
                if e["type"] == "verdict" and isinstance(e.get("detail"), dict)
                and e["detail"].get("unit") == unit]

    def _dispatch_events(self, mid, unit):
        return [e for e in self._events(mid)
                if e["type"] == "dispatch" and isinstance(e.get("detail"), dict)
                and e["detail"].get("unit") == unit]

    def test_30_independent_units_run_in_parallel(self):
        records = []
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w0"))
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w1"))
        self.adapter.script("planner", plan_block(sample_units(2)))
        mid = self.create_mission()
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=40),
                        "独立单元并行执行后应 done，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertEqual(len(records), 2, "应恰好 2 次 worker 轮次")
        a, b = sorted(records, key=lambda r: r["start"])
        self.assertLess(b["start"], a["end"],
                        "无依赖单元必须真并发（worker 轮次窗口应重叠）")
        # 两个 dispatch 都发生在第一个 verdict 之前 => 并行窗口
        self.assertEqual({e["detail"]["unit"] for e in self._events(mid)
                          if e["type"] == "dispatch"}, {0, 1})
        v0 = self._verdict_events(mid, 0)
        self.assertTrue(v0, "单元 0 应有 PASS 判定")
        self.assertLess(self._dispatch_events(mid, 1)[0]["ts"], v0[0]["ts"],
                        "并行调度下单元 1 必须在单元 0 判定前就已派发")
        # lease：每个单元独立令牌，结束后释放
        tokens = [e["detail"]["lease"] for e in self._events(mid)
                  if e["type"] == "dispatch"]
        self.assertEqual(len(set(tokens)), 2, "每个单元必须持有独立 lease 令牌")
        plan = self.read_json(self.mdir(mid) / "plan.json")
        for u in plan["units"]:
            self.assertEqual(u.get("state"), "passed")
            self.assertIsNone(u.get("lease"), "完成后 lease 必须释放")

    def test_31_dependent_unit_waits_for_dependency_passed(self):
        records = []
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w0"))
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w1"))
        units = sample_units(2)
        units[0]["id"] = "a"
        units[1]["id"] = "b"
        units[1]["dependencies"] = ["a"]
        self.adapter.script("planner", plan_block(units))
        mid = self.create_mission()
        self.mgr.start(mid)
        # 轮询窗：只要 A 仍活跃，B 就必须停在 pending（依赖就绪判定）
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.state_vals(self.mgr, mid) & TERMINAL_STATES:
                break
            plan_path = self.mdir(mid) / "plan.json"
            if not plan_path.is_file():  # planner 尚未产出
                time.sleep(POLL_INTERVAL)
                continue
            plan = self.read_json(plan_path)
            ua, ub = plan["units"][0], plan["units"][1]
            if ua.get("state") in ("running", "evaluating", "waiting"):
                self.assertEqual(ub.get("state"), "pending",
                                 "前置单元未 passed/integrated 前，依赖单元不得离开 pending")
            time.sleep(POLL_INTERVAL)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=40),
                        "依赖链全过后应 done，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertEqual(len(records), 2)
        a, b = sorted(records, key=lambda r: r["start"])
        self.assertGreaterEqual(b["start"], a["end"],
                                "依赖单元的 worker 必须晚于前置单元完成")
        # 事件序：b 的 dispatch 严格晚于 a 的 PASS
        va = self._verdict_events(mid, 0)
        db = self._dispatch_events(mid, 1)
        self.assertTrue(va and db, "应存在 a 的判定与 b 的派发")
        self.assertGreater(db[0]["ts"], va[0]["ts"],
                           "b 只能在 a 判定 PASS 之后被调度")

    def test_32_max_parallel_1_serial_equivalence(self):
        records = []
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w0"))
        self.adapter.script("worker", self._slow_worker(records, 0.5, "w1"))
        self.adapter.script("planner", plan_block(sample_units(2)))
        mid = self.create_mission(options={"maxParallelWorkers": 1})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=40),
                        "串行策略同样必须 done，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertEqual(len(records), 2)
        a, b = sorted(records, key=lambda r: r["start"])
        self.assertGreaterEqual(b["start"], a["end"],
                                "maxParallelWorkers=1 时两个单元不得并发")
        # 事件序：b 的 dispatch 严格晚于 a 的 PASS（P1.1 式的顺序执行）
        va = self._verdict_events(mid, 0)
        db = self._dispatch_events(mid, 1)
        self.assertTrue(va and db)
        self.assertGreater(db[0]["ts"], va[0]["ts"],
                           "串行模式 b 必须等 a 的 PASS 之后才派发")
        plan = self.read_json(self.mdir(mid) / "plan.json")
        for u in plan["units"]:
            self.assertEqual(u.get("state"), "passed")

    def test_33_parallel_workers_capped_at_hard_limit(self):
        records = []
        n = 5
        self.adapter.script("planner", plan_block(sample_units(n)))
        for _ in range(n):
            self.adapter.script("worker", self._slow_worker(records, 0.5))
        mid = self.create_mission(options={"maxParallelWorkers": 99})
        self.mgr.start(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=60),
                        "5 个独立单元应全部完成，实际: %s" % self.state_vals(self.mgr, mid))
        self.assertEqual(len(records), n, "每个单元恰好一次 worker 轮次")
        # 最大并发 = 任意时刻重叠的 worker 数；99 被钳制到硬上限 4
        intervals = sorted(records, key=lambda r: r["start"])
        peak, ends = 0, []
        for it in intervals:
            ends = [e for e in ends if e > it["start"]]
            peak = max(peak, len(ends) + 1)
            ends.append(it["end"])
        self.assertLessEqual(peak, 4,
                             "并发 worker 数不得超过硬上限 4（99 被钳制）")
        self.assertGreaterEqual(peak, 4,
                                "前 4 个单元应在同一调度窗口内并发")
        plan = self.read_json(self.mdir(mid) / "plan.json")
        self.assertEqual(len([u for u in plan["units"] if u.get("state") == "passed"]),
                         n, "全部单元应通过")

    # -- M4.1 parallel wait/wake mailbox --

    def _waiting_units(self, mid, count, timeout=30):
        """Wait until `count` units are concurrently WAITING (each with a job)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            plan_path = self.mdir(mid) / "plan.json"
            if plan_path.is_file():
                try:
                    plan = self.read_json(plan_path)
                except Exception:
                    plan = None
                if plan:
                    waiting = [u for u in plan["units"]
                               if u.get("state") == "waiting" and u.get("jobId")]
                    if len(waiting) >= count:
                        return plan
            time.sleep(POLL_INTERVAL)
        return None

    def test_34_two_jobs_wake_independently(self):
        """M4.1: two parallel WAITING units, each with its own background job,
        both jobs finish near-simultaneously, both wakes are consumed by their
        OWN unit (no single-slot overwrite), both continue to the evaluator,
        mission reaches DONE."""
        self.adapter.script("planner", plan_block(sample_units(2)))
        self.adapter.script("worker", job_block("sleep 2", reason="作业A"))
        self.adapter.script("worker", job_block("sleep 2", reason="作业B"))
        mid = self.create_mission(options={"maxParallelWorkers": 2})
        self.mgr.start(mid)
        plan = self._waiting_units(mid, 2)
        self.assertIsNotNone(plan, "两个单元必须同时处于 WAITING（各持 jobId）")
        job_ids = {u["jobId"] for u in plan["units"]}
        self.assertEqual(len(job_ids), 2, "两个单元必须各持独立 jobId")
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=60),
                        "两作业近同时结束后应 done，实际: %s" % self.state_vals(self.mgr, mid))
        # 每个作业都必须 completed（两个 watcher 都如实持久化后才 wake）
        jobs = [self.read_json(p) for p in self.sub_files(mid, "jobs", "*.json")]
        self.assertEqual(len(jobs), 2)
        for j in jobs:
            self.assertEqual(j.get("status"), "completed", "job %s 未 completed" % j.get("jobId"))
            self.assertEqual(j.get("exitCode"), 0)
        # 每个单元都消费了自己的 wake（事件里两个 jobId 各自一份）
        wakes = {}
        for e in self._events(mid):
            if e["type"] == "wake" and isinstance(e.get("detail"), dict):
                wakes.setdefault(e["detail"].get("jobId"), []).append(e)
        for jid in job_ids:
            self.assertIn(jid, wakes, "job %s 的 wake 必须被其单元消费" % jid)
            self.assertEqual(wakes[jid][0]["detail"].get("exitKind"), "exited")
        plan = self.read_json(self.mdir(mid) / "plan.json")
        for u in plan["units"]:
            self.assertEqual(u.get("state"), "passed", "两个单元都应 PASS 并集入")
        self.assertFalse(self.reason_has(self.mgr, mid, "no-progress", "stop"),
                         "近同时双 wake 不得触发任何 stopReason")

    def test_35_no_spin_after_sibling_wake(self):
        """M4.1: A finishes first, B still waits several seconds. B must NOT
        busy-poll the disk (the old never-cleared set() wake_event made
        wait(timeout=0.5) return immediately); B only wakes when its own job
        completes, e.g. ~2 load_job checks per second."""
        from mission import MissionStore
        self.adapter.script("planner", plan_block(sample_units(2)))
        self.adapter.script("worker", job_block("sleep 1", reason="作业A先结束"))
        self.adapter.script("worker", job_block("sleep 6", reason="作业B后结束"))
        mid = self.create_mission(options={"maxParallelWorkers": 2})
        self.mgr.start(mid)
        plan = self._waiting_units(mid, 2)
        self.assertIsNotNone(plan, "两个单元必须同时处于 WAITING")
        job_paths = {u["jobId"]: self.mdir(mid) / "jobs" / f"{u['jobId']}.json"
                     for u in plan["units"]}
        # 两个 job_block 脚本按 FIFO 被最先执行的 worker 轮次取走，无法假定
        # 哪个 job 属于哪个 index —— 以「谁先完成」识别兄弟 wake
        def first_done():
            return next((jid for jid, p in job_paths.items()
                         if self.read_json(p).get("status") in ("completed", "failed")), None)
        a_job = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            a_job = first_done()
            if a_job:
                break
            time.sleep(POLL_INTERVAL)
        self.assertTrue(a_job, "A 作业应先结束（触发兄弟 wake）")
        b_job = next(jid for jid in job_paths if jid != a_job)
        self.assertNotEqual(self.read_json(job_paths[b_job]).get("status"), "completed",
                            "B 作业在 A 结束时必须仍在运行（留出测量窗）")
        # A 的 wake 已经发出；在 B 的作业结束前测量 B 的磁盘检查频率
        orig = MissionStore.load_job
        counted = {"n": 0}
        def counting(store, job_id):
            if str(job_id or "") == b_job:
                counted["n"] += 1
            return orig(store, job_id)
        MissionStore.load_job = counting
        try:
            time.sleep(2.0)
            n = counted["n"]
        finally:
            MissionStore.load_job = orig
        self.assertGreaterEqual(n, 1, "B 等待期间必须仍在检查磁盘（保证存活）")
        self.assertLessEqual(n, 100,
                             "兄弟 wake 后 B 不得忙轮询磁盘（2 秒内 load_job %d 次）" % n)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=60),
                        "B 最终被自己的 job 唤醒后应 done，实际: %s" % self.state_vals(self.mgr, mid))
        plan = self.read_json(self.mdir(mid) / "plan.json")
        for u in plan["units"]:
            self.assertEqual(u.get("state"), "passed")
        self.assertFalse(self.reason_has(self.mgr, mid, "no-progress", "stop"),
                         "兄弟 wake 不得被误判为 no-progress")

    def test_36_parallel_pause_then_resume_completes(self):
        """并行执行中 pause：mission 转 paused、不再派发新单元（在途轮次
        允许收尾）；resume 后全部单元完成。"""
        records = []
        n = 3
        self.adapter.script("planner", plan_block(sample_units(n)))
        for _ in range(n * 2):  # resume 后允许重跑，脚本备足
            self.adapter.script("worker", self._slow_worker(records, 0.4))
        mid = self.create_mission(options={"maxParallelWorkers": 2})
        self.mgr.start(mid)
        self.assertTrue(self.wait_until(
            lambda: len(self._dispatch_events(mid, 0)) >= 1, timeout=15,
            desc="至少一个单元已派发"), "应立即派发首批单元")
        self.mgr.pause(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["paused"], timeout=15),
                        "pause 应在并行执行中生效")
        # 在途 worker（0.4s）收尾后不得再有新的 worker 完成
        time.sleep(1.2)
        frozen = len(records)
        time.sleep(1.0)
        self.assertEqual(len(records), frozen,
                         "paused 期间在途轮次收尾后不得继续执行 worker")
        self.mgr.resume(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["done"], timeout=60),
                        "resume 后全部单元应完成，实际: %s" % self.state_vals(self.mgr, mid))
        plan = self.read_json(self.mdir(mid) / "plan.json")
        self.assertEqual([u.get("state") for u in plan["units"]],
                         ["passed"] * n, "resume 后 n 个单元全部 passed")
        self.assertFalse(self.reason_has(self.mgr, mid, "no-progress", "stop"))

    def test_37_parallel_cancel_stops_all_unit_threads(self):
        """并行执行中 cancel：mission 及时转 cancelled，所有单元线程退出，
        不留僵尸 worker。"""
        records = []
        self.adapter.script("planner", plan_block(sample_units(3)))
        for _ in range(3):
            self.adapter.script("worker", self._slow_worker(records, 5.0))
        mid = self.create_mission(options={"maxParallelWorkers": 3})
        self.mgr.start(mid)
        self.assertTrue(self.wait_until(
            lambda: len([e for e in self._events(mid) if e["type"] == "dispatch"]) >= 2,
            timeout=15, desc="至少两个单元已派发"), "并行单元应已派发")
        self.mgr.cancel(mid)
        self.assertTrue(self.wait_state(self.mgr, mid, ["cancelled"], timeout=15),
                        "cancel 应及时生效")
        runner = self.mgr._runners.get(mid) if hasattr(self.mgr, "_runners") else None
        if runner is not None:
            self.assertFalse(runner.is_alive(), "runner 线程应已退出")
        frozen = len(records)
        time.sleep(1.5)
        self.assertEqual(len(records), frozen, "cancel 后不得再有 worker 完成")


class TestCancelInterruptPropagation(MissionLoopTest):
    """Gate F hardening: cancel() must reach the runtime adapter's directed
    turn-interrupt (real codex turns), not just persist a state."""

    def test_40_cancel_directs_runtime_interrupt_and_stops_work(self):
        calls = {"interrupt": 0, "worker_started": False,
                 "stalled": threading.Event()}

        class InterruptableAdapter(FakeAdapter):
            def interrupt_active_turns(self, max_wait=10.0):
                calls["interrupt"] += 1
                calls["stalled"].set()   # the real impl also unblocks turns
                return []

        adapter = InterruptableAdapter()

        def slow(prompt):
            calls["worker_started"] = True
            calls["stalled"].wait(timeout=30)  # blocks inside run_turn
            return handoff_text()

        adapter.script("planner", plan_block(sample_units(1)))
        adapter.script("worker", slow)
        adapter.set_default("evaluator", verdict_block("PASS", ["ok"]))
        mgr = MissionManager(adapter, self.root)
        mid = mission_id_of(mgr.create("obj", acceptance_criteria=["a"]))
        self.track(mid, mgr=mgr)
        mgr.start(mid)
        self.assertTrue(self.wait_until(
            lambda: calls["worker_started"], timeout=15),
            "worker turn 应已进入运行")
        mgr.cancel(mid)
        self.assertEqual(calls["interrupt"], 1,
                         "cancel 必须调用 runtime 的定向 turn-interrupt")
        self.assertTrue(self.wait_state(mgr, mid, ["cancelled"], timeout=10),
                        "cancel 后应及时进入 cancelled")
        # cancel 返回后不得再有工作推进
        frozen = len(adapter.calls)
        time.sleep(1.0)
        self.assertEqual(len(adapter.calls), frozen,
                         "cancel 后不得再发生任何模型 turn")


if __name__ == "__main__":
    if MISSION_IMPORT_ERROR is not None:
        print("NOTE: web/mission.py 无法导入（%r）— 全部用例跳过，待主线合入后验证"
              % (MISSION_IMPORT_ERROR,))
    unittest.main(verbosity=2)
