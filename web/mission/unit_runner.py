"""UnitRunner: the per-unit execution layer (P1.2/M2).

One UnitRunner drives ONE work unit through its phases: worker turn
(running/repairing) -> background job wait (waiting) -> unit evaluator
(evaluating) -> verdict (passed/blocked/needs-work). With
maxParallelWorkers=1 the MissionRunner calls run() synchronously, so the
observable behavior (state transitions, prompt order, events) is identical
to the P1.1 single-threaded state machine. M4 turns each UnitRunner into a
real worker thread under the mission scheduler.

Mission-level decisions stay with MissionRunner (scheduler): this class
only executes a unit and reports its outcome. All state persistence goes
through MissionStore so crash recovery can resume a unit mid-phase.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .jobs import JobWatcher, _ps_start_identity, job_log_tail
from .models import (EVALUATOR_TURN_TIMEOUT, WORKER_TURN_TIMEOUT,
                     TERMINAL_STATES, _HANDOFF_RE, _JOB_RE, _VERDICT_RE,
                     _now_ms, parse_json_marker)
from .store import MissionStore


class UnitRunner:
    """Executes one unit's phases; returns an outcome to the scheduler.

    Outcomes: PASS / BLOCKED / NEEDS_WORK (mission decides repair-cap),
    STOP (paused/stopped/terminal), IDLE (state does not belong to any
    unit phase). run() does not block on anything except the mission's
    wake_event while waiting for a background job.
    """

    PASS = "PASS"
    BLOCKED = "BLOCKED"
    NEEDS_WORK = "NEEDS_WORK"
    STOP = "STOP"
    IDLE = "IDLE"

    def __init__(self, runner: Any, index: int) -> None:
        self.runner = runner          # MissionRunner (scheduler)
        self.index = index            # expected unit index (state is authoritative)
        self.manager = runner.manager
        self.mission_id = runner.mission_id
        self.store: MissionStore = runner.store
        self.policy = runner.policy

    # -- scheduler-facing entry point --
    def run(self, state: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        """Drive the current unit's lifecycle phases until the unit reaches a
        verdict (PASS/BLOCKED/NEEDS_WORK) or the loop must stop
        (STOP/IDLE). Returns (outcome, state) — state carries payload
        fields (repairDirective/lastVerdict) the scheduler needs."""
        state = state or self.runner._state()
        while True:
            if self.runner._control.is_set():
                return (self.STOP, state)
            state = self.runner._state()
            current = state.get("state")
            if current in TERMINAL_STATES:
                return (self.STOP, state)
            if not self.runner._wait_while_paused(state):
                self.store.event("runner", "exit: paused (unit)")
                return (self.STOP, state)
            reason = self.runner.policy.check(state)
            if reason:
                self.runner._transition(state, "failed", stopReason=reason)
                return (self.STOP, state)
            if current in ("running", "repairing"):
                if not self._phase_worker(state, repair=(current == "repairing")):
                    return (self.STOP, state)
            elif current == "waiting":
                if not self._phase_waiting(state):
                    return (self.STOP, state)
            elif current == "evaluating":
                return self._phase_evaluating(state)
            else:
                return (self.IDLE, state)

    # -- plan/unit persistence --
    def _unit(self, index: int) -> dict[str, Any] | None:
        plan = self.store.load_plan()
        for u in plan["units"]:
            if u["index"] == index:
                return u
        return None

    def _save_unit(self, unit: dict[str, Any]) -> None:
        plan = self.store.load_plan()
        for i, u in enumerate(plan["units"]):
            if u["index"] == unit["index"]:
                plan["units"][i] = unit
        self.store.save_plan(plan)

    # -- phases --

    def _phase_worker(self, state: dict[str, Any], *, repair: bool) -> bool:
        index = int(state.get("currentUnit") or 0)
        unit = self._unit(index)
        if unit is None:
            self.runner._transition(state, "failed", stopReason=f"工作单元 {index} 不存在")
            return False
        mission = self.store.load_mission()
        unit["state"] = unit["status"] = "running"
        unit["attempt"] = int(unit.get("attempt", 0)) + 1
        unit["worker"]["startedAt"] = _now_ms()
        if repair:
            unit["repairCount"] = int(unit.get("repairCount", 0)) + 1
            unit["repairDirective"] = str(state.get("repairDirective") or "修复验收未通过的问题")[:2000]
            self._save_unit(unit)
            self.store.write_repair(
                f"{state.get('cycles')}-{index}-{unit['repairCount']}",
                f"# RepairDirective\n\n{unit['repairDirective']}\n\n## verdict\n{json.dumps(state.get('lastVerdict') or {}, ensure_ascii=False)}")
        else:
            self._save_unit(unit)
        prompt = (
            "你是 Mission Worker（构建者）。只做当前这一个工作单元，不要做别的。\n"
            f"总目标：{mission.get('objective')}\n"
            f"当前单元 #{index + 1}：{unit['title']}\n{unit['description']}\n"
            "验收标准：\n- " + "\n- ".join(unit.get("acceptance") or ["实现并自测通过"]) + "\n"
            + (f"\n上次验收反馈（必须修复）：{state.get('repairDirective')}\n" if repair else "")
            + (f"\n交接摘要（此前进展）：\n{self.store.load_handoff() or '（无）'}\n"
               if self.store.load_handoff() else "")
            + (f"\n自上次唤醒的增量：\n{state.get('delta')}\n" if state.get("delta") else "")
            + "\n规则：\n"
              "1) 需要运行预计超过 20 秒的命令时，不要等待它：在回复末尾输出标记块并结束本轮，"
              "系统会运行它并在结束后叫醒你：\n"
              "<<<LAOMO_JOB\n"
              '{"command":"...","cwd":"...","reason":"...","expectedSeconds":600}'
              "\nLAOMO_JOB>>>\n"
              "2) 完工时输出一段以 HANDOFF: 开头的交接摘要（≤300 字：做了什么/改了哪些文件/下一步建议）。\n"
              "3) 你不能宣布整个 Mission 完成；只交付当前单元。"
        )
        result = self.runner._turn(state, prompt)
        if not result.get("ok"):
            self.store.event("worker", f"turn failed: {(result.get('error') or '')[:160]}")
        text = result.get("text") or ""
        handoff = _HANDOFF_RE.search(text)
        if handoff:
            self.store.save_handoff(handoff.group(1).strip()[:2000])
        job = parse_json_marker(text, _JOB_RE)
        if isinstance(job, dict) and job.get("command"):
            self._register_job(state, index, job)
            return True
        unit = self._unit(index)
        if unit is not None:
            unit["worker"]["finishedAt"] = _now_ms()
            unit["delta"] = None
            self._save_unit(unit)
        state.pop("delta", None)
        self.runner._transition(state, "evaluating")
        return True

    def _register_job(self, state: dict[str, Any], unit_index: int, job_spec: dict[str, Any]) -> None:
        mission = self.store.load_mission()
        job_id = uuid.uuid4().hex[:12]
        cwd = str(job_spec.get("cwd") or mission.get("cwd") or os.getcwd())
        command = str(job_spec.get("command"))
        log_path = self.store.job_log(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "wb") as log_fh:
                proc = subprocess.Popen(
                    ["/bin/zsh", "-lc", command],
                    stdout=log_fh, stderr=subprocess.STDOUT, cwd=cwd,
                    start_new_session=True)
        except OSError as exc:
            self.store.event("job", f"spawn failed: {exc}")
            state["delta"] = f"后台作业启动失败：{exc}"
            self.runner._transition(state, "evaluating")
            return
        expected = time.time() + max(30, int(job_spec.get("expectedSeconds") or 600))
        job = {"jobId": job_id, "pid": proc.pid, "pgid": proc.pid,  # start_new_session => session leader
               "command": command, "cwd": cwd, "logPath": str(log_path),
               "startedAt": _now_ms(), "expectedWakeAt": expected,
               "completionCondition": str(job_spec.get("reason") or "process exit"),
               "unitIndex": unit_index, "status": "running",
               "startIdentity": _ps_start_identity(proc.pid),
               "commandHash": hashlib.sha256(command.encode()).hexdigest()[:16]}
        self.store.save_job(job)
        watcher = JobWatcher(job, self.store, self._on_job_wake, proc=proc)
        self.manager.attach_watcher(self.mission_id, watcher)
        watcher.start()
        u = self._unit(unit_index)
        if u is not None:
            u["state"] = u["status"] = "waiting"
            u["jobId"] = job_id
            u["worker"]["finishedAt"] = _now_ms()
            self._save_unit(u)
        self.store.event("job", {"jobId": job_id, "pid": proc.pid,
                                 "command": command[:120], "expectedWakeAt": expected})
        self.runner._transition(state, "waiting", waitingJobId=job_id)

    def _on_job_wake(self, woken: dict[str, Any]) -> None:
        self.runner.wake(woken)

    def _phase_waiting(self, state: dict[str, Any]) -> bool:
        # Event-driven: no model polling. The job watcher sets wake_event.
        # request_stop sets BOTH events — check control first so a stop is
        # never mistaken for a job completion wake.
        while not self.runner.wake_event.wait(timeout=0.5):
            if self.runner._control.is_set():
                return False
            state = self.runner._state()
            if self.runner._paused(state):
                self.store.event("runner", "exit: paused while waiting")
                return False
        if self.runner._control.is_set():
            return False
        self.runner.wake_event.clear()
        woken = self.runner.wake_payload or {}
        self.runner.wake_payload = None
        job_id = woken.get("jobId") or state.get("waitingJobId")
        exit_kind = woken.get("exitKind") or "unknown"
        persisted = self.store.load_job(str(job_id)) if job_id else {}
        if persisted:
            woken = {**persisted, **woken}
        tail = job_log_tail(Path(woken.get("logPath") or self.store.job_log(job_id or "")))
        delta = (f"后台作业已{'超时(仍在运行,被强制唤醒)' if exit_kind == 'overdue' else '结束'}："
                 f"{woken.get('command') or job_id}\n--- 日志尾部 ---\n{tail}")
        state.pop("waitingJobId", None)
        state["delta"] = delta[:6000]
        job_unit_idx = persisted.get("unitIndex")
        if job_unit_idx is not None:
            u = self._unit(int(job_unit_idx))
            if u is not None:
                u["state"] = u["status"] = "running"
                u["delta"] = delta[:6000]
                self._save_unit(u)
        self.store.event("wake", {"jobId": job_id, "exitKind": exit_kind})
        self.runner._transition(state, "running")
        return True

    def _phase_evaluating(self, state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        index = int(state.get("currentUnit") or 0)
        unit = self._unit(index)
        if unit is None:
            self.runner._transition(state, "failed", stopReason="evaluating: 单元缺失")
            return (self.STOP, state)
        mission = self.store.load_mission()
        unit["state"] = unit["status"] = "evaluating"
        self._save_unit(unit)
        prompt = (
            "你是独立验收员（Evaluator）。你与构建者无关，只依据事实验收。\n"
            "你处于只读沙箱：不得创建/修改/删除任何文件，只能读取与运行只读检查。\n"
            f"总目标：{mission.get('objective')}\n"
            f"待验收单元 #{index + 1}：{unit['title']}\n{unit['description']}\n"
            "验收标准：\n- " + "\n- ".join(unit.get("acceptance") or []) + "\n"
            f"工作区：{mission.get('cwd')}\n"
            f"证据目录：{self.store.evidence_dir}\n"
            "可自行运行只读命令（查看文件、跑测试可以；测试若有写行为导致失败就按 NEEDS_WORK 记录）。\n"
            "在回复末尾必须输出（三选一，NEEDS_WORK 时 repair 必填）：\n"
            "<<<LAOMO_VERDICT\n"
            '{"verdict":"PASS|NEEDS_WORK|BLOCKED","reasons":["..."],"repair":"..."}'
            "\nLAOMO_VERDICT>>>"
        )
        result = self.runner._turn(state, prompt, read_only=True, timeout=EVALUATOR_TURN_TIMEOUT)
        verdict = parse_json_marker(result.get("text") or "", _VERDICT_RE)
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("PASS", "NEEDS_WORK", "BLOCKED"):
            # default-fail contract: unparseable verdict is NEVER a pass
            verdict = {"verdict": "NEEDS_WORK",
                       "reasons": ["evaluator 输出不可解析（default-fail）"],
                       "repair": "重新运行实现并确保验收标准可被客观验证"}
        verdict_name = f"{state.get('cycles')}-{index}"
        self.store.write_verdict(verdict_name, verdict)
        state["lastVerdict"] = {"unit": index, **{k: verdict.get(k) for k in ("verdict", "reasons")}}
        self.store.event("verdict", state["lastVerdict"])

        # no-progress signature: STABLE per-unit progress (passed/blocked) +
        # handoff hash. Transient per-unit states (running/waiting/evaluating)
        # must not count as progress: a repair loop would otherwise never trip
        # the breaker. Persisted so a crash cannot reset the counter.
        plan = self.store.load_plan()
        sig = hashlib.sha1(json.dumps({
            "statuses": [u.get("state") if u.get("state") in ("passed", "integrated", "blocked", "failed", "cancelled")
                         else "active" for u in plan["units"]],
            "handoff": hashlib.sha1(self.store.load_handoff().encode()).hexdigest()[:12],
        }, sort_keys=True).encode()).hexdigest()
        if sig == self.runner._last_progress_sig:
            no_progress = int(state.get("noProgress", 0)) + 1
        else:
            no_progress = 0
        self.runner._last_progress_sig = sig
        disk = self.store.load_state()
        disk["noProgress"] = no_progress
        disk["progressSignature"] = sig
        self.store.save_state(disk)
        state.update({"noProgress": no_progress, "progressSignature": sig})

        v = verdict.get("verdict")
        if v == "PASS":
            unit["state"] = unit["status"] = "passed"
            unit["lastVerdict"] = "PASS"
            self._save_unit(unit)
            self._checkpoint(state, index, unit, verdict)
            self._update_progress_md()
            return (self.PASS, state)
        if v == "BLOCKED":
            unit["state"] = unit["status"] = "blocked"
            unit["lastVerdict"] = "BLOCKED"
            self._save_unit(unit)
            self._checkpoint(state, index, unit, verdict)
            self._update_progress_md()
            return (self.BLOCKED, state)
        # NEEDS_WORK
        unit["state"] = unit["status"] = "repairing"
        unit["lastVerdict"] = "NEEDS_WORK"
        unit["repairDirective"] = str(verdict.get("repair") or "; ".join(verdict.get("reasons") or []))[:2000]
        self._save_unit(unit)
        state["repairDirective"] = str(verdict.get("repair") or "; ".join(verdict.get("reasons") or []))[:2000]
        return (self.NEEDS_WORK, state)

    def _checkpoint(self, state: dict[str, Any], index: int, unit: dict[str, Any],
                    verdict: dict[str, Any]) -> None:
        name = f"{state.get('cycles')}-{index}"
        self.store.write_checkpoint(name, (
            f"# Checkpoint {name}\n\n单元：{unit['title']}\nverdict：{verdict.get('verdict')}\n"
            f"reasons：{json.dumps(verdict.get('reasons'), ensure_ascii=False)}\n\n"
            f"## handoff\n{self.store.load_handoff()[:1500]}\n"))

    def _update_progress_md(self) -> None:
        plan = self.store.load_plan()
        rows = [f"- [{u['status']}] #{u['index'] + 1} {u['title']}（repair×{u.get('repairCount', 0)}，"
                f"最后判定 {u.get('lastVerdict') or '—'}）" for u in plan["units"]]
        self.store.write_progress(
            f"# Mission 进度\n\n更新：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n" + "\n".join(rows))
