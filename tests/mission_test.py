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
import sys
import tempfile
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
                       options=None, cwd=None, mgr=None):
        mgr = mgr or self.mgr
        try:
            result = mgr.create(objective, cwd=cwd, acceptance_criteria=acceptance,
                                options=options)
        except TypeError:  # parameter naming variant (acceptanceCriteria)
            result = mgr.create(objective, cwd=cwd, acceptanceCriteria=acceptance,
                                options=options)
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
        # simulate "the background job outlived the gateway crash": pin the
        # pid to this very test process so recover() must see a live pid
        job["pid"] = os.getpid()
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


if __name__ == "__main__":
    if MISSION_IMPORT_ERROR is not None:
        print("NOTE: web/mission.py 无法导入（%r）— 全部用例跳过，待主线合入后验证"
              % (MISSION_IMPORT_ERROR,))
    unittest.main(verbosity=2)
