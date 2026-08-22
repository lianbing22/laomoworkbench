"""MissionRunner + MissionManager: the mission state machine and API."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .dag import (UNIT_DEP_DONE, UNIT_EVALUATING, UNIT_INTEGRATED,
                  UNIT_INTEGRATING, UNIT_PASSED, UNIT_PENDING, UNIT_READY,
                  UNIT_REPAIRING, UNIT_RUNNING, UNIT_WAITING, normalize_plan)
from .jobs import (JobWatcher, _process_identity, _terminate_job_process,
                   job_log_tail)
from .models import (EVALUATOR_TURN_TIMEOUT, RUNS_DIRNAME, WORKER_TURN_TIMEOUT,
                     MissionError, StopPolicy, TERMINAL_STATES,
                     _PLAN_RE, _VERDICT_RE,
                     _accrue_state, _file_sha256, _now_ms, parse_json_marker)
from .store import MissionStore
from .unit_runner import UnitRunner
from .verification import VerificationRunner, _git_diff_summary
from .worktree import WorktreeManager


# --- MissionRunner -------------------------------------------------------------------


class MissionRunner(threading.Thread):
    """One OS thread drives one mission through its phases. Turns run
    sequentially; pause takes effect between turns (never kills in-flight
    work); cancel/stop reasons terminate the loop."""

    def __init__(self, manager: "MissionManager", mission_id: str) -> None:
        super().__init__(name=f"mission-{mission_id[:8]}", daemon=True)
        self.manager = manager
        self.mission_id = mission_id
        self.store = manager.store_for(mission_id)
        self.policy = manager.policy_for(mission_id)
        # parallel wake mailbox: one slot per jobId, so a wake for unit A can
        # never be overwritten by a wake for unit B
        self._wake_payloads: dict[str, dict[str, Any]] = {}
        self._wake_condition = threading.Condition()
        self._control = threading.Event()  # set => stop loop (cancel/fail)
        self._last_progress_sig: str | None = None
        # per-unit worker threads report back through this queue; the
        # scheduler thread is the only consumer (serializes outcomes)
        self._unit_outcomes: dict[int, tuple[str, dict[str, Any]]] = {}
        self._unit_lock = threading.Lock()
        self._sig_lock = threading.Lock()  # no-progress signature updates
        # no-progress compare must survive crashes: seed from persisted value
        disk = self.store.load_state()
        self._last_progress_sig = str(disk.get("progressSignature") or "") or None

    # -- thread plumbing --
    def request_stop(self) -> None:
        self._control.set()
        with self._wake_condition:
            self._wake_condition.notify_all()

    def wake(self, payload: dict[str, Any]) -> None:
        """Mail the wake to the slot of its job. A wake for unit B can never
        overwrite the still-undelivered wake for unit A."""
        with self._wake_condition:
            self._wake_payloads[str(payload.get("jobId") or "")] = payload
            self._wake_condition.notify_all()

    def take_wake(self, job_id: str | None) -> dict[str, Any] | None:
        """Pop the pending wake for THIS job only. Parallel units each wait
        on their own job; a wake addressed to a sibling stays queued for it."""
        with self._wake_condition:
            return self._wake_payloads.pop(str(job_id or ""), None)

    # -- state helpers --
    def _state(self) -> dict[str, Any]:
        return self.store.load_state()

    def _transition(self, state: dict[str, Any], new_state: str, **fields: Any) -> None:
        if new_state in TERMINAL_STATES:
            self.manager._stopped_watchers.pop(self.mission_id, None)
        # Races: a pause/cancel can land while a phase holds an older dict.
        # The on-disk state is the source of truth — never clobber a pause or
        # a terminal state with a stale transition. The caller's dict carries
        # payload fields (repairDirective/delta/lastVerdict) that disk lacks,
        # so it is the base; disk wins on anything it also has (fresher
        # counters/phaseStartedAt from the just-run accrue).
        with self.store.lock:
            disk = self.store.load_state()
            _accrue_state(disk)
            disk_state = disk.get("state")
            merged = {**state, **disk}
            merged["phaseStartedAt"] = disk.get("phaseStartedAt")
            merged.update({k: v for k, v in fields.items() if k != "state"})
            if disk_state == "paused" and new_state != "paused" and new_state not in TERMINAL_STATES:
                defer = new_state
                merged["state"] = "paused"
                merged["stateBeforePause"] = defer
                self.store.save_state(merged)
                self.store.event("transition", {"state": "paused", "deferred": defer})
                self.manager.broadcast(self.mission_id, merged)
                return
            if disk_state in TERMINAL_STATES:
                self.store.event("transition-suppressed", {"wanted": new_state, "disk": disk_state})
                return
            merged["state"] = new_state
            merged["wallElapsedMs"] = int(merged.get("agentActiveMs", 0)) + int(merged.get("waitingMs", 0))
            merged["activeMs"] = merged["wallElapsedMs"]
            self.store.save_state(merged)
            self.store.event("transition", {"state": new_state, **{k: v for k, v in fields.items()}})
        self.manager.broadcast(self.mission_id, merged)
        if new_state == "done":
            # evidence snapshot is baked at DONE and never overwritten later
            self._emit_evidence_manifest()
        # A managed job may not outlive its mission: terminal states reached
        # from waiting must take the background job down with them.
        if new_state in TERMINAL_STATES and disk_state == "waiting":
            self.manager.terminate_mission_jobs(self.mission_id, self.store)

    def _emit_evidence_manifest(self) -> dict[str, Any]:
        """Snapshot the evidence trail (verdicts/verification/checkpoints/
        git diff/artifacts) with path+sha256+generatedAt. Immutable after
        DONE: a later call returns the existing manifest unchanged."""
        existing = self.store.evidence_manifest()
        if existing:
            return existing
        mission = self.store.load_mission()
        entries: dict[str, dict[str, Any]] = {}

        def add(rel: str, path: Path, kind: str) -> None:
            try:
                if not path.is_file():
                    return
                entries[rel] = {"path": str(path.resolve()),
                                "sha256": _file_sha256(path),
                                "generatedAt": int(path.stat().st_mtime * 1000),
                                "kind": kind, "size": path.stat().st_size}
            except OSError:
                return

        for rel, kind in (("mission.json", "mission"), ("state.json", "state"),
                          ("plan.json", "plan"), ("progress.md", "progress"),
                          ("handoff.md", "handoff")):
            add(rel, self.store.root / rel, kind)
        for sub, kind in (("checkpoints", "checkpoint"), ("verdicts", "verdict"),
                          ("repairs", "repair"), ("verification", "verification"),
                          ("jobs", "job")):
            base = self.store.root / sub
            if base.is_dir():
                for p in sorted(base.rglob("*")):
                    if p.is_file():
                        add(f"{sub}/{p.relative_to(base)}", p, kind)
        for rel in (mission.get("verification") or {}).get("requiredFiles") or []:
            p = Path(rel)
            if not p.is_absolute():
                p = Path(str(mission.get("cwd") or os.getcwd())) / p
            if p.is_file():
                add(f"artifact/{rel}", p, "artifact")
            else:
                entries[f"artifact/{rel}"] = {"path": str(p), "sha256": None,
                                              "generatedAt": _now_ms(),
                                              "kind": "artifact", "size": -1, "missing": True}
        diff = _git_diff_summary(str(mission.get("cwd") or ""))
        if diff:
            p = self.store.evidence_dir / "git-diff.txt"
            try:
                p.write_text(diff + "\n", "utf-8")
                add("git-diff.txt", p, "git-diff")
            except OSError:
                pass
        manifest = {"state": "done", "missionId": mission.get("id"),
                    "generatedAt": _now_ms(), "entries": entries,
                    "sha256": hashlib.sha256(
                        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        self.store.write_evidence_manifest(manifest)
        self.store.event("evidence", {"entries": len(entries)})
        return manifest

    def _paused(self, state: dict[str, Any]) -> bool:
        return state.get("state") == "paused"

    def _wait_while_paused(self, state: dict[str, Any]) -> bool:
        """A paused runner exits its thread; resume spawns a fresh runner.
        (Keeps exactly one live runner per mission at any time.)"""
        if self._paused(state):
            self.store.event("runner", "exit: paused")
            return False
        return True

    def _turn(self, state: dict[str, Any], prompt: str, *,
              read_only: bool = False, timeout: int = WORKER_TURN_TIMEOUT,
              cwd: str | None = None) -> dict[str, Any]:
        """Run one codex turn on a fresh thread. Counters always accumulate on
        a fresh disk copy: a pause/cancel landing mid-turn must never be
        clobbered by a stale in-memory dict. Mutates the caller's state dict
        in place afterwards so its counters stay in sync. `cwd` overrides the
        mission workspace (per-unit worktrees)."""
        mission = self.store.load_mission()
        started = _now_ms()
        result = self.manager.adapter.run_turn(
            prompt=prompt, cwd=cwd if cwd is not None else mission.get("cwd"),
            read_only=read_only,
            model=mission.get("model"), effort=mission.get("effort"), timeout=timeout)
        elapsed = _now_ms() - started
        usage = result.get("usage") or {}
        tokens = int(usage.get("uncachedInputTokens") or 0) + int(usage.get("outputTokens") or 0)
        with self.store.lock:
            disk = self.store.load_state()
            if disk.get("state") in TERMINAL_STATES:
                # turn result is discarded: the mission is already over
                self.store.event("turn", {"ok": result.get("ok"), "discarded": "terminal"})
                return result
            disk["cycles"] = int(disk.get("cycles", 0)) + 1
            disk["activeMs"] = int(disk.get("activeMs", 0)) + elapsed
            disk["tokensUsed"] = int(disk.get("tokensUsed", 0)) + tokens
            self.store.save_state(disk)
        state.update({k: disk.get(k) for k in ("cycles", "activeMs", "tokensUsed")})
        self.store.event("turn", {"ok": result.get("ok"), "elapsedMs": elapsed,
                                  "tokens": tokens, "error": (result.get("error") or "")[:200]})
        return result

    # -- main loop --
    def run(self) -> None:
        self.store.event("runner", "started")
        try:
            self._loop()
        except Exception as exc:  # runner must never crash silently
            state = self._state()
            if state.get("state") not in TERMINAL_STATES:
                self._transition(state, "failed", stopReason=f"runner 异常: {exc!r}")
            self.store.event("runner", f"crashed: {exc!r}")
        finally:
            self.manager.on_runner_exit(self.mission_id)
            self.store.event("runner", "exited")

    def _loop(self) -> None:
        while not self._control.is_set():
            state = self._state()
            if state.get("state") in TERMINAL_STATES:
                return
            if not self._wait_while_paused(state):
                return
            current = state.get("state")
            reason = self.policy.check(state)
            if reason:
                self._transition(state, "failed", stopReason=reason)
                return
            if current == "planning":
                if not self._phase_planning(state):
                    return
            elif current in ("running", "repairing", "waiting", "evaluating"):
                if not self._schedule(state):
                    return
            elif current == "replanning":
                if not self._phase_replanning(state):
                    return
            elif current == "verification":
                if not self._phase_verification(state):
                    return
            elif current == "verifying":
                if not self._phase_verifying(state):
                    return
            else:
                self._transition(state, "failed", stopReason=f"未知状态 {current}")
                return

    # -- scheduler (P1.2/M4) ------------------------------------------------

    def _owned_units(self) -> set[int]:
        """Units whose worker thread is currently alive (the real lease: an
        alive thread holds the unit; a crashed scheduler loses its threads
        and recovery re-dispatches by inspecting unit states)."""
        with self.manager._lock:
            pool = self.manager._unit_threads.get(self.mission_id) or {}
            return {i for i, t in pool.items() if t.is_alive()}

    def _dispatch(self, index: int) -> None:
        """Start one worker thread for this unit. The unit's durable state is
        the lease token holder; only the thread that owns the current token
        may write its state transitions."""
        unit = self._unit(index)
        if unit is None:
            return
        with self.manager._lock:
            pool = self.manager._unit_threads.setdefault(self.mission_id, {})
            old = pool.get(index)
            if old is not None and old.is_alive() and old is not threading.current_thread():
                return  # already leased by a live thread
            token = uuid.uuid4().hex[:8]
            unit["lease"] = {"token": token, "acquiredAt": _now_ms(),
                             "heartbeatAt": _now_ms()}
            if unit.get("state") in (UNIT_PENDING, UNIT_READY):
                unit["state"] = unit["status"] = UNIT_RUNNING
            self._save_unit(unit)
            thread = threading.Thread(target=self._unit_entry, args=(index, token),
                                      name=f"unit-{self.mission_id[:8]}-{index}",
                                      daemon=True)
            pool[index] = thread
        self.store.event("dispatch", {"unit": index, "lease": token})
        thread.start()

    def _unit_entry(self, index: int, token: str) -> None:
        outcome = UnitRunner.CRASH
        payload: dict[str, Any] = {}
        try:
            outcome, payload = UnitRunner(self, index).run_unit()
        except Exception as exc:  # a unit must never take its mission down silently
            self.store.event("unit", f"unit {index} crashed: {exc!r}")
            payload["error"] = str(exc)[:300]
        finally:
            self._release_lease(index, token)
            with self._unit_lock:
                self._unit_outcomes[index] = (outcome, payload)

    def _release_lease(self, index: int, token: str) -> None:
        unit = self._unit(index)
        if unit is not None and (unit.get("lease") or {}).get("token") == token:
            unit["lease"] = None
            self._save_unit(unit)
        with self.manager._lock:
            pool = self.manager._unit_threads.get(self.mission_id)
            if pool and pool.get(index) is not None:
                pool.pop(index, None)

    def _mirror(self, phase: str, **fields: Any) -> None:
        """Mirror a unit phase into the mission state so the pre-P1.2 UI
        sequence stays identical at maxParallelWorkers=1. While several units
        are active the mission stays "running" (parallel window)."""
        with self.manager._lock:
            pool = self.manager._unit_threads.get(self.mission_id) or {}
            live = sum(1 for t in pool.values() if t.is_alive())
        if live > 1:
            return
        state = self._state()
        if state.get("state") in TERMINAL_STATES or state.get("state") == "paused":
            return
        self._transition(state, phase, **fields)

    def _migrate_legacy_plan(self) -> None:
        """Plans written before P1.2/M1 have no id/state/dependencies (v1).
        One-shot upgrade so a resumed legacy mission keeps its statuses and
        runs through the DAG scheduler like any v2 mission."""
        plan = self.store.load_plan()
        if int(plan.get("version") or 1) >= 2 and all("id" in u for u in plan["units"]):
            return
        with self.store.lock:
            for u in plan["units"]:
                if "id" in u and "state" in u:
                    continue
                st = str(u.get("state") or u.get("status") or "pending")
                u.setdefault("id", f"unit-{int(u.get('index') or 0) + 1}")
                u["state"] = u.get("state") or st
                u["status"] = u.get("status") or st
                u.setdefault("dependencies", [])
                u.setdefault("worktree", {"path": None, "branch": None,
                                          "baseSha": None, "headSha": None})
                u.setdefault("jobId", None)
                u.setdefault("delta", None)
                u.setdefault("repairDirective", None)
                u.setdefault("lastVerdict", None)
                u.setdefault("attempt", 0)
                u.setdefault("repairCount", 0)
                u.setdefault("worker", {"startedAt": None, "finishedAt": None})
            plan["version"] = 2
            self.store.save_plan(plan)
            self.store.event("plan-migration", {"units": len(plan["units"])})

    def _schedule(self, state: dict[str, Any]) -> bool:
        """Run the dispatch loop: harvest finished units, mark
        dependency-ready units, dispatch into free worker slots, drain to
        harness verification when nothing active remains. Returns False when
        the mission loop should exit (terminal / paused / stop)."""
        self._migrate_legacy_plan()
        while not self._control.is_set():
            state = self._state()
            if state.get("state") in TERMINAL_STATES:
                return False
            if not self._wait_while_paused(state):
                return False
            with self.store.lock:
                disk = self.store.load_state()
                _accrue_state(disk)
                if disk.get("state") in TERMINAL_STATES:
                    return False
                self.store.save_state(disk)
            reason = self.policy.check(disk)
            if reason:
                self._transition(disk, "failed", stopReason=reason)
                return False
            if disk.get("state") not in ("running", "repairing", "waiting", "evaluating"):
                return False
            # 1. harvest unit outcomes (the scheduler thread is the only consumer)
            with self._unit_lock:
                finished = dict(self._unit_outcomes)
                self._unit_outcomes.clear()
            for index, (outcome, payload) in sorted(finished.items()):
                if outcome in (UnitRunner.STOP, UnitRunner.IDLE):
                    return False
                if outcome == UnitRunner.PASS:
                    res = self._integrate_harvested(state, index)
                    if res == "conflict":
                        self._transition(state, "blocked",
                                         stopReason=f"单元 #{index + 1} 集成冲突，等待解决")
                        return False
                    if res == "failed":
                        self._transition(state, "failed",
                                         stopReason=f"单元 #{index + 1} 集成失败")
                        return False
                    continue
                if outcome == UnitRunner.BLOCKED:
                    reasons = "; ".join((payload.get("lastVerdict") or {}).get("reasons") or [])
                    self._transition(state, "blocked",
                                     stopReason=("evaluator 判定 BLOCKED：" + reasons[:200]),
                                     **payload)
                    return False
                if outcome == UnitRunner.LIMIT:
                    self._transition(state, "failed",
                                     stopReason=f"单元 #{index + 1} 修复次数超限（{self.policy.max_repair}）",
                                     **payload)
                    return False
                if outcome in (UnitRunner.CRASH, UnitRunner.FAILED):
                    self._transition(state, "failed",
                                     stopReason=f"单元 #{index + 1} 执行异常: "
                                                f"{(payload.get('error') or '')[:120]}",
                                     **payload)
                    return False
                if outcome == UnitRunner.CONFLICT:
                    self._transition(state, "blocked",
                                     stopReason=f"单元 #{index + 1} 集成冲突，等待解决")
                    return False
            # 2. recover/mark: machine-gate repair targets and crash-wedge
            #    integration only run when nothing is in flight (no threads,
            #    no un-harvested outcomes) and nothing was just harvested —
            #    the harvest loop above already integrated those.
            with self._unit_lock:
                pending_outcomes = bool(self._unit_outcomes)
            if not finished and not self._owned_units() and not pending_outcomes:
                state = self._state()
                if state.get("state") == "repairing":
                    target = int(state.get("currentUnit") or 0)
                    unit = self._unit(target)
                    if unit is not None and unit.get("state") in (UNIT_PASSED, UNIT_INTEGRATED):
                        unit["state"] = unit["status"] = UNIT_REPAIRING
                        unit["repairDirective"] = str(
                            state.get("repairDirective") or "修复验收未通过的问题")[:2000]
                        self._save_unit(unit)
                        self.store.event("repair", {"unit": target, "scope": "machine-gate"})
                        continue
                plan = self.store.load_plan()
                # M5 reconcile: a crash mid-integration wedges a unit in
                # `integrating` (owned by no thread, never dispatched again).
                # Nothing is in flight here, so git truth can safely settle
                # the transaction — without this the drain clause would end
                # the mission as "DAG 依赖无法满足".
                for unit in plan["units"]:
                    if unit.get("state") != UNIT_INTEGRATING:
                        continue
                    res = self._reconcile_integration(state, unit["index"])
                    if res == "conflict":
                        self._transition(state, "blocked",
                                         stopReason=f"单元 #{unit['index'] + 1} 集成冲突（崩溃恢复重放）")
                        return False
                    if res == "failed":
                        self._transition(state, "failed",
                                         stopReason=f"单元 #{unit['index'] + 1} 集成失败（崩溃恢复重放）")
                        return False
                for unit in plan["units"]:
                    if unit.get("state") == UNIT_PASSED:
                        self._integrate_harvested(state, unit["index"])
            # 3. dispatch ready units into free slots
            plan = self.store.load_plan()
            owned = self._owned_units()
            slots = self.policy.max_parallel - len(owned)
            if slots > 0:
                # atomic RMW: a unit thread may persist a verdict/lease on the
                # same plan.json between load and save; a stale write-back here
                # would revert it and make the scheduler re-dispatch forever.
                with self.store.lock:
                    plan = self.store.load_plan()
                    by_id = {u["id"]: u for u in plan["units"]}
                    done = {u["id"]: u["state"] in UNIT_DEP_DONE for u in plan["units"]}
                    changed = False
                    for u in plan["units"]:
                        if u["state"] != UNIT_PENDING:
                            continue
                        deps = [d for d in (u.get("dependencies") or []) if d in by_id]
                        if all(done.get(d, True) for d in deps):
                            u["state"] = u["status"] = UNIT_READY
                            changed = True
                    if changed:
                        self.store.save_plan(plan)
            owned = self._owned_units()
            plan = self.store.load_plan()
            slots = self.policy.max_parallel - len(owned)
            if slots > 0:
                need = [u["index"] for u in plan["units"]
                        if u["state"] in (UNIT_RUNNING, UNIT_EVALUATING,
                                          UNIT_REPAIRING, UNIT_WAITING)
                        and u["index"] not in owned]
                ready = [u["index"] for u in plan["units"]
                         if u["state"] == UNIT_READY and u["index"] not in owned]
                for index in (need + ready)[:max(0, slots)]:
                    self._dispatch(index)
            # 4. drain: whenever no slot is occupied, decide the next phase
            with self._unit_lock:
                pending_outcomes = bool(self._unit_outcomes)
            if self._owned_units() or pending_outcomes:
                time.sleep(0.08)
                continue
            plan = self.store.load_plan()
            units = plan["units"]
            if all(u.get("state") in UNIT_DEP_DONE for u in units):
                last = max((u["index"] for u in units), default=0)
                self._transition(state, "verification", currentUnit=last)
                return True
            bad = [u for u in units if u.get("state") == "failed"]
            if bad:
                self._transition(state, "failed", stopReason=f"单元 #{bad[0]['index'] + 1} 失败")
                return False
            conf = [u for u in units if u.get("state") in ("conflict", "blocked")]
            if conf:
                self._transition(state, "blocked",
                                 stopReason=f"单元 #{conf[0]['index'] + 1} 被阻塞")
                return False
            cancelled = [u for u in units if u.get("state") == "cancelled"]
            if cancelled:
                self._transition(state, "cancelled")
                return False
            self._transition(state, "blocked", stopReason="DAG 依赖无法满足（存在无法就绪的单元）")
            return False
        return False

    def _integrate_harvested(self, state: dict[str, Any], index: int) -> str:
        """A unit PASSed: integrate (Control Plane duty) then advance the
        mission's currentUnit hint for the next runnable unit. Returns
        "ok" | "conflict" | "failed" | "none"."""
        integ = self._integrate(state, index)
        if integ in ("conflict", "failed"):
            return integ
        nxt = self._next_ready(self.store.load_plan())
        self._transition(state, "running",
                         currentUnit=nxt if nxt is not None else index)
        return integ

    @staticmethod
    def _next_ready(plan: dict[str, Any]) -> int | None:
        for u in plan["units"]:
            if u["state"] == UNIT_READY:
                return u["index"]
        return None

    def _integrate(self, state: dict[str, Any], index: int) -> str:
        """Integrate one passed unit: its worktree branch into the mission
        branch. Integration is a write-ahead transaction: the unit's
        `integration` record (pre-merge unitHead + dirty flag) is persisted
        ATOMICALLY with state=integrating BEFORE git runs, so a crash at any
        point can be reconciled from git truth (see _reconcile_integration).
        Returns "ok" | "conflict" | "failed" | "none" (never had a worktree —
        P1.1 mode edits the workspace directly)."""
        unit = self._unit(index)
        if unit is None:
            return "failed"
        mission = self.store.load_mission()
        wtree = WorktreeManager(str(mission.get("cwd") or os.getcwd()),
                                self.store, self.mission_id)
        info = unit.get("worktree") or {}
        if not info.get("path") or not wtree.available:
            return "none"
        if unit.get("state") in ("integrated",):
            return "ok"  # crash between integrate-save and next-pending
        tx = dict(unit.get("integration") or {})
        if unit.get("state") != UNIT_INTEGRATING:
            unit["state"] = unit["status"] = UNIT_INTEGRATING
            tx = {"phase": "prepared", "branch": info.get("branch"),
                  "unitHead": wtree.rev(Path(str(info["path"]))),
                  "dirty": wtree.is_dirty(index),
                  "startedAt": _now_ms()}
            # atomic with the integrating state: one plan.json write
            unit["integration"] = tx
            self._save_unit(unit)
            self.store.event("integration", {"unit": index, "phase": "start",
                                             "branch": info.get("branch"),
                                             "baseSha": info.get("baseSha"),
                                             "txUnitHead": tx.get("unitHead")})
        elif not tx.get("unitHead"):
            # pre-M5 crash record: backfill before touching git so reconcile
            # data exists even for a wedge created by an older build
            tx = {"phase": "prepared", "branch": info.get("branch"),
                  "unitHead": wtree.rev(Path(str(info["path"]))),
                  "dirty": wtree.is_dirty(index),
                  "startedAt": _now_ms(), "backfilled": True}
            unit["integration"] = tx
            self._save_unit(unit)
        result = wtree.integrate(index, unit.get("title"), branch=info.get("branch"))
        if result.get("ok"):
            unit["worktree"] = wtree.refresh_head(unit.get("worktree") or info)
            unit["integration"] = {**tx, "phase": "merged",
                                   "headSha": result.get("headSha"),
                                   "finishedAt": _now_ms()}
            unit["state"] = unit["status"] = "integrated"
            unit["delta"] = None
            self._save_unit(unit)
            self.store.write_progress_md()
            self.store.event("integration", {"unit": index, "phase": "integrated",
                                             "branch": info.get("branch"),
                                             "headSha": result.get("headSha")})
            wtree.cleanup(index, branch=info.get("branch"))
            unit["integration"] = {**unit["integration"], "phase": "cleaned"}
            self._save_unit(unit)
            return "ok"
        if result.get("conflict"):
            unit["integration"] = {**tx, "phase": "conflict"}
            unit["state"] = unit["status"] = "conflict"
            self._save_unit(unit)
            self.store.event("integration", {"unit": index, "phase": "conflict",
                                             "reason": (result.get("reason") or "")[:200]})
            return "conflict"
        unit["integration"] = {**tx, "phase": "failed"}
        unit["state"] = unit["status"] = "failed"
        self._save_unit(unit)
        self.store.event("integration", {"unit": index, "phase": "failed",
                                         "reason": (result.get("reason") or "")[:200]})
        return "failed"

    def _reconcile_integration(self, state: dict[str, Any], index: int) -> str:
        """M5 crash reconcile for a unit wedged in `integrating`. Git is the
        truth for whether the merge landed; the persisted transaction record
        says which probe is trustworthy:

        * tx.unitHead recorded on a CLEAN tree and already an ancestor of the
          mission HEAD => the merge landed and only plan.json lagged behind:
          adopt it (no new commits, no re-merge), then clean up.
        * a MERGE_HEAD is sitting in the repo => a conflicted merge crashed
          mid-way: abort it (mission branch must stay clean), then replay.
        * anything else (crash before the merge, dirty tree, legacy record)
          => replay the idempotent integrate (commit-if-needed + merge;
          re-merging an already-merged branch is "Already up to date").
        """
        unit = self._unit(index)
        if unit is None:
            return "failed"
        mission = self.store.load_mission()
        wtree = WorktreeManager(str(mission.get("cwd") or os.getcwd()),
                                self.store, self.mission_id)
        info = unit.get("worktree") or {}
        tx = unit.get("integration") or {}
        if not info.get("path") or not wtree.available:
            unit["integration"] = {**tx, "phase": "failed",
                                   "reason": "reconcile: worktree 不存在"}
            unit["state"] = unit["status"] = "failed"
            self._save_unit(unit)
            self.store.event("integration", {"unit": index, "phase": "failed",
                                             "reconciled": True,
                                             "reason": "worktree 不存在"})
            return "failed"
        removed = wtree.clear_stale_locks(
            [Path(str(info["path"])), wtree.workspace])
        if removed:
            self.store.event("integration", {"unit": index,
                                             "phase": "cleared-stale-locks",
                                             "locks": removed})
        if tx.get("unitHead") and not tx.get("dirty") \
                and wtree.is_merged(str(tx["unitHead"])):
            head = wtree.rev(wtree.workspace)
            unit["integration"] = {**tx, "phase": "merged", "headSha": head,
                                   "finishedAt": _now_ms(), "reconciled": True}
            unit["state"] = unit["status"] = "integrated"
            unit["delta"] = None
            self._save_unit(unit)
            self.store.write_progress_md()
            self.store.event("integration", {"unit": index, "phase": "integrated",
                                             "reconciled": True,
                                             "alreadyMerged": tx.get("unitHead"),
                                             "headSha": head})
            wtree.cleanup(index, branch=info.get("branch"))
            unit["integration"] = {**unit["integration"], "phase": "cleaned"}
            self._save_unit(unit)
            return "ok"
        if wtree.merge_in_progress():
            wtree.abort_merge()
            self.store.event("integration", {"unit": index,
                                             "phase": "aborted-stale-merge",
                                             "reconciled": True})
        res = self._integrate(state, index)
        if res == "ok":
            self.store.event("integration", {"unit": index, "phase": "replayed",
                                             "reconciled": True})
        return res

    # -- phases --
    def _phase_planning(self, state: dict[str, Any]) -> bool:
        mission = self.store.load_mission()
        prompt = (
            "你是 Mission Planner。把下面的长期目标拆解为可独立验收的工作单元。\n"
            f"目标：{mission.get('objective')}\n"
            + (f"总验收标准：\n- " + "\n- ".join(mission.get("acceptanceCriteria") or []) + "\n"
               if mission.get("acceptanceCriteria") else "")
            + "要求：2-6 个单元；每个单元给出 title、description、acceptance（可勾选的验收标准列表）。\n"
              "单元之间若存在先后依赖（后续单元需要前置结果），请给每个单元一个稳定 id（如 schema）"
              "并把依赖写入 dependencies（前置单元的 id 或 title 列表）；无依赖的并行单元不要写 dependencies。\n"
              "只在回复末尾输出标记块（JSON 数组，不要其它格式）：\n"
              "<<<LAOMO_PLAN\n"
              '[{"id":"schema","title":"...","description":"...","acceptance":["..."],"dependencies":[]}]'
              "\nLAOMO_PLAN>>>"
        )
        result = self._turn(state, prompt)
        raw_units = parse_json_marker(result.get("text") or "", _PLAN_RE)
        if not isinstance(raw_units, list) or not raw_units:
            # default-fail: unparseable plan retries once via replan cycle cap
            state["planningFailures"] = int(state.get("planningFailures", 0)) + 1
            if state["planningFailures"] >= 2:
                self._transition(state, "failed", stopReason="planner 输出不可解析")
                return False
            self.store.event("planning", "unparseable plan output")
            return True
        units, notes = normalize_plan(raw_units)
        if not units:
            self._transition(state, "failed", stopReason="planner 未产出有效单元")
            return False
        plan = {"version": 2, "units": units, "replans": 0}
        self.store.save_plan(plan)
        self.store.event("planning", {"units": len(units), "dag": notes})
        first = self._next_pending(plan, current=-1)
        self._transition(state, "running", currentUnit=first if first is not None else 0)
        return True

    def _unit(self, index: int) -> dict[str, Any] | None:
        plan = self.store.load_plan()
        for u in plan["units"]:
            if u["index"] == index:
                return u
        return None

    def _save_unit(self, unit: dict[str, Any]) -> None:
        with self.store.lock:
            plan = self.store.load_plan()
            for i, u in enumerate(plan["units"]):
                if u["index"] == unit["index"]:
                    plan["units"][i] = unit
            self.store.save_plan(plan)


    @staticmethod
    def _next_pending(plan_index: dict[str, Any], current: int) -> int | None:
        """Next runnable unit: pending + all dependencies passed/integrated.
        Backward-compatible fallback for broken graphs (unknown dep ids)."""
        by_id = {u["id"]: u for u in plan_index["units"]}
        statuses = {u["id"]: (u.get("state") or u.get("status")) for u in plan_index["units"]}
        for u in plan_index["units"]:
            if u["status"] != "pending" or u["index"] == current:
                continue
            deps = [d for d in (u.get("dependencies") or []) if d in by_id]
            if all(statuses.get(d) in ("passed", "integrated") for d in deps):
                return u["index"]
        for u in plan_index["units"]:
            if u["status"] == "pending" and u["index"] != current:
                return u["index"]
        for u in plan_index["units"]:
            if u["status"] == "pending":
                return u["index"]
        return None

    def _phase_replanning(self, state: dict[str, Any]) -> bool:
        self._migrate_legacy_plan()
        plan = self.store.load_plan()
        if int(plan.get("replans", 0)) >= self.policy.max_no_progress:
            self._transition(state, "failed", stopReason="replan 次数超限")
            return False
        mission = self.store.load_mission()
        gaps = [u for u in plan["units"] if u.get("status") not in ("passed", "integrated")]
        prompt = (
            "你是 Mission Planner（补缺口轮）。以下单元尚未通过验收，给出修正后的后续单元计划。\n"
            f"目标：{mission.get('objective')}\n"
            "未通过单元：\n"
            + "\n".join(f"- #{u['index'] + 1} {u['title']} 状态 {u['status']} 最后判定 {u.get('lastVerdict')}"
                        for u in gaps)
            + "\n\n输出（只输出标记块）：\n<<<LAOMO_PLAN\n"
              '[{"id":"schema","title":"...","description":"...","acceptance":["..."],"dependencies":[]}]'
              "\nLAOMO_PLAN>>>"
        )
        result = self._turn(state, prompt)
        raw_units = parse_json_marker(result.get("text") or "", _PLAN_RE)
        if not isinstance(raw_units, list) or not raw_units:
            self._transition(state, "failed", stopReason="replanner 输出不可解析")
            return False
        added, notes = normalize_plan(raw_units, existing=plan["units"])
        plan["units"].extend(added)
        plan["replans"] = int(plan.get("replans", 0)) + 1
        self.store.save_plan(plan)
        self.store.event("replanning", {"added": len(added), "dag": notes})
        # the replanner's new units address the gaps: run the first runnable one
        first = self._next_pending(
            {**plan, "units": [u for u in plan["units"] if u["index"] >= added[0]["index"]]},
            current=-1)
        if first is None:
            first = self._next_pending(plan, current=-1)
        self._transition(state, "running", currentUnit=first if first is not None else 0)
        return True

    def _phase_verification(self, state: dict[str, Any]) -> bool:
        """Harness Verification Gate — machine-only, no model turn. A failing
        gate sends the mission back to repair; it can never reach DONE."""
        mission = self.store.load_mission()
        gateway = VerificationRunner(self.store, mission.get("verification") or {},
                                     str(mission.get("cwd") or os.getcwd()))
        result = gateway.run()
        disk = self.store.load_state()
        disk["verifyResult"] = "pass" if result["passed"] else "fail"
        disk["verifyChecks"] = len(result["checks"])
        self.store.save_state(disk)
        state.update({"verifyResult": disk["verifyResult"], "verifyChecks": disk["verifyChecks"]})
        self.store.event("verification", {"passed": result["passed"], "checks": len(result["checks"])})
        if result["passed"]:
            self._transition(state, "verifying")
            return True
        failed = [c for c in result["checks"] if not c.get("passed")]
        lines = []
        for c in failed[:8]:
            detail = (c.get("error") or c.get("stdoutTail") or "").strip()
            detail = detail[-200:] if detail else ""
            lines.append(f"- [{c['kind']}] {c['name']}：{detail}")
        state["repairDirective"] = (
            "Harness 机器验收未通过（原始验证命令/文件/HTTP 检查必须全部通过后才能 DONE）：\n"
            + "\n".join(lines)[:2000])
        self._transition(state, "repairing")
        return True

    def _phase_verifying(self, state: dict[str, Any]) -> bool:
        mission = self.store.load_mission()
        plan = self.store.load_plan()
        # triple gate: the machine gate must have PASSED before the final
        # evaluator runs — nobody may reach DONE without machine evidence
        if state.get("verifyResult") != "pass":
            self.store.event("final-verdict", {"gate": "machine-not-passed"})
            self._transition(state, "verification")
            return True
        criteria = mission.get("acceptanceCriteria") or []
        for u in plan["units"]:
            criteria.extend(u.get("acceptance") or [])
        prompt = (
            "你是最终验收员（Final Evaluator）。只读沙箱，逐条核验全部验收标准。\n"
            f"目标：{mission.get('objective')}\n工作区：{mission.get('cwd')}\n"
            "全部验收标准：\n- " + "\n- ".join(criteria) + "\n"
            "可运行测试与只读检查。末尾必须输出：\n"
            "<<<LAOMO_VERDICT\n"
            '{"verdict":"PASS|NEEDS_WORK|BLOCKED","reasons":["..."],"repair":"..."}'
            "\nLAOMO_VERDICT>>>"
        )
        result = self._turn(state, prompt, read_only=True, timeout=EVALUATOR_TURN_TIMEOUT)
        verdict = parse_json_marker(result.get("text") or "", _VERDICT_RE)
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("PASS", "NEEDS_WORK", "BLOCKED"):
            verdict = {"verdict": "NEEDS_WORK", "reasons": ["final evaluator 输出不可解析（default-fail）"],
                       "repair": "整体复查"}
        self.store.write_verdict("final", verdict)
        state["lastVerdict"] = {"unit": "final", **{k: verdict.get(k) for k in ("verdict", "reasons")}}
        self.store.event("final-verdict", state["lastVerdict"])
        if verdict.get("verdict") == "PASS":
            # triple gate: all units passed/integrated + final regression
            # (the evaluator ran the checks above) + final evaluator PASS
            all_passed = all(u.get("status") in ("passed", "integrated")
                             for u in plan["units"])
            if all_passed:
                self._transition(state, "done")
                return False
            self._transition(state, "replanning")
            return True
        if verdict.get("verdict") == "BLOCKED":
            self._transition(state, "blocked",
                             stopReason="final evaluator BLOCKED：" + "; ".join(verdict.get("reasons") or [])[:200])
            return False
        self._transition(state, "replanning")
        return True


# --- MissionManager -------------------------------------------------------------------


class MissionManager:
    """Control-plane facade used by the gateway: CRUD + lifecycle + recovery."""

    def __init__(self, adapter: Any, workspace_root: Path,
                 broadcast_fn: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.adapter = adapter            # needs run_turn(...)
        self.workspace_root = workspace_root
        self.broadcast_fn = broadcast_fn or (lambda mid, state: None)
        self._lock = threading.RLock()
        self._runners: dict[str, MissionRunner] = {}
        # unit worker threads per mission, keyed by unit index. An alive
        # thread is the unit's lease: crash recovery re-dispatches by
        # inspecting unit states, not this table.
        self._unit_threads: dict[str, dict[int, threading.Thread]] = {}
        self._watchers: dict[str, list[JobWatcher]] = {}
        # Watchers stopped while their job is still running. They keep the
        # Popen handle referenced: a re-attached watcher reuses it so proc.poll()
        # reaps the real exit status (a dropped Popen would let Python's
        # subprocess._cleanup() reap the zombie before os.waitpid sees it).
        self._stopped_watchers: dict[str, list[JobWatcher]] = {}

    # -- paths --
    def runs_root(self, cwd: Path | None) -> Path:
        base = Path(cwd) if cwd else self.workspace_root
        return base / RUNS_DIRNAME

    @property
    def index_dir(self) -> Path:
        # Missions may run in arbitrary workspaces; this index maps ids to
        # their run dirs so start/status/list/recover can find them again.
        return self.workspace_root / ".laomo" / "index"

    def _register_run(self, mission_id: str, run_dir: Path) -> None:
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            (self.index_dir / f"{mission_id}.path").write_text(str(run_dir), "utf-8")
        except OSError:
            pass

    def _indexed_roots(self) -> list[Path]:
        roots: list[Path] = []
        if not self.index_dir.is_dir():
            return roots
        for marker in self.index_dir.glob("*.path"):
            try:
                candidate = Path(marker.read_text("utf-8").strip())
            except OSError:
                continue
            if (candidate / "mission.json").is_file():
                roots.append(candidate)
            else:
                try:
                    marker.unlink()
                except OSError:
                    pass
        return roots

    def store_for(self, mission_id: str) -> MissionStore:
        for candidate in self._all_run_roots():
            if candidate.name == mission_id:
                return MissionStore(candidate)
        raise MissionError("mission 不存在", "not-found")

    def _all_run_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[Path] = set()
        # Only the manager's own workspace root is scanned: every mission it
        # created is registered in the index (and under workspace_root/.laomo/
        # runs), so a manager for a different root must never harvest — or
        # worse, resume — missions that belong to another workspace.
        base = self.workspace_root / RUNS_DIRNAME
        if base.is_dir():
            roots.extend(p for p in base.iterdir() if p.is_dir() and (p / "mission.json").is_file())
        roots.extend(self._indexed_roots())
        unique: list[Path] = []
        for p in roots:
            resolved = p.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    def policy_for(self, mission_id: str) -> StopPolicy:
        return StopPolicy(self.store_for(mission_id).load_mission().get("options"))

    # -- events broadcast --
    def broadcast(self, mission_id: str, state: dict[str, Any]) -> None:
        try:
            self.broadcast_fn(mission_id, state)
        except Exception:
            pass

    def on_runner_exit(self, mission_id: str) -> None:
        with self._lock:
            self._runners.pop(mission_id, None)
            watchers = self._watchers.pop(mission_id, [])
            if watchers:
                self._stopped_watchers.setdefault(mission_id, []).extend(watchers)
        for watcher in watchers:
            watcher.stop()

    def attach_watcher(self, mission_id: str, watcher: JobWatcher) -> None:
        # one watcher per managed job: parallel units each hold their own
        with self._lock:
            self._watchers.setdefault(mission_id, []).append(watcher)

    # -- summaries --
    def _summary(self, store: MissionStore) -> dict[str, Any]:
        mission = store.load_mission()
        state = store.load_state()
        plan = store.load_plan()
        current = None
        idx = int(state.get("currentUnit") or 0)
        for u in plan.get("units", []):
            if u["index"] == idx:
                current = u["title"]
        waiting = None
        if state.get("state") == "waiting" and state.get("waitingJobId"):
            job = store.load_job(state["waitingJobId"])
            waiting = {k: job.get(k) for k in ("jobId", "command", "startedAt", "expectedWakeAt")}
        active_ms = int(state.get("activeMs", 0))
        return {
            "id": store.root.name, "objective": mission.get("objective"),
            "state": state.get("state", "draft"), "phase": state.get("state", "draft"),
            "currentTask": current, "cycles": state.get("cycles", 0),
            "waiting": waiting, "elapsedSec": active_ms // 1000,
            "lastVerdict": state.get("lastVerdict"),
            "stopReason": state.get("stopReason"),
            "createdAt": mission.get("createdAt"), "updatedAt": state.get("updatedAt"),
            "tokensUsed": state.get("tokensUsed", 0),
            "verifyResult": state.get("verifyResult"),
            "time": {"wallElapsedMs": int(state.get("wallElapsedMs") or active_ms or 0),
                     "agentActiveMs": int(state.get("agentActiveMs", 0)),
                     "waitingMs": int(state.get("waitingMs", 0)),
                     "pausedMs": int(state.get("pausedMs", 0))},
        }

    def list(self) -> dict[str, Any]:
        with self._lock:
            active = [mid for mid, r in self._runners.items() if r.is_alive()]
        missions = [self._summary(MissionStore(p)) for p in sorted(self._all_run_roots())]
        missions.sort(key=lambda m: m.get("updatedAt") or 0, reverse=True)
        return {"ok": True, "missions": missions,
                "activeId": active[0] if active else None}

    def status(self, mission_id: str) -> dict[str, Any]:
        store = self.store_for(mission_id)
        summary = self._summary(store)
        summary["plan"] = store.load_plan()
        summary["events"] = store.events_tail(40)
        summary["jobs"] = store.list_jobs()
        summary["verification"] = store.verification_results()
        summary["evidence"] = store.evidence_manifest()
        checkpoint = ""
        if store.checkpoints_dir.is_dir():
            files = sorted(store.checkpoints_dir.glob("*.md"))
            if files:
                try:
                    checkpoint = files[-1].read_text("utf-8")[:600]
                except OSError:
                    checkpoint = ""
        summary["lastCheckpoint"] = checkpoint
        return {"ok": True, "mission": summary}

    def verify_gate(self, mission_id: str) -> dict[str, Any]:
        """Re-run the machine verification gate on demand. Inspection only:
        writes under verification/ but never touches mission state."""
        store = self.store_for(mission_id)
        mission = store.load_mission()
        gateway = VerificationRunner(store, mission.get("verification") or {},
                                     str(mission.get("cwd") or os.getcwd()))
        return {"ok": True, "result": gateway.run()}

    # -- lifecycle --
    def create(self, objective: str, cwd: str | None = None,
               acceptance_criteria: list[str] | None = None,
               options: dict[str, Any] | None = None,
               verification: dict[str, Any] | None = None) -> dict[str, Any]:
        objective = str(objective or "").strip()
        if not objective:
            raise MissionError("objective 不能为空")
        mission_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        run_cwd = str(Path(cwd).resolve()) if cwd else str(self.workspace_root)
        store = MissionStore(self.runs_root(Path(run_cwd)) / mission_id)
        store.ensure_dirs()
        self._register_run(mission_id, store.root)
        store.save_mission({
            "id": mission_id, "objective": objective,
            "acceptanceCriteria": [str(a) for a in (acceptance_criteria or [])],
            "cwd": run_cwd, "options": options or {},
            "verification": verification or {},
            "createdAt": _now_ms(),
        })
        store.save_state({"state": "draft", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "", "tokensUsed": 0,
                          "wallElapsedMs": 0, "agentActiveMs": 0,
                          "waitingMs": 0, "pausedMs": 0, "phaseStartedAt": 0})
        store.event("created", {"objective": objective[:120], "cwd": run_cwd})
        return {"ok": True, "mission": self._summary(store)}

    def _ensure_not_active(self, exclude: str | None = None) -> None:
        with self._lock:
            for mid, runner in self._runners.items():
                if mid == exclude:
                    continue
                if runner.is_alive():
                    raise MissionError("已有正在运行的 Mission，先暂停或结束它", "busy")

    def _reap_runner(self, mission_id: str, timeout: float = 5.0) -> None:
        """A paused runner exits asynchronously; reap it before respawn."""
        with self._lock:
            runner = self._runners.get(mission_id)
        if runner and runner.is_alive():
            runner.request_stop()
            runner.join(timeout=timeout)
        with self._lock:
            self._runners.pop(mission_id, None)

    def start(self, mission_id: str) -> dict[str, Any]:
        store = self.store_for(mission_id)
        state = store.load_state()
        if state.get("state") in TERMINAL_STATES:
            raise MissionError("mission 已结束", "terminal")
        if state.get("state") not in ("draft",) and not state.get("state"):
            pass
        self._ensure_not_active()
        if state.get("state") == "draft":
            state["state"] = "planning"
            store.save_state(state)
            store.event("start", None)
        runner = MissionRunner(self, mission_id)
        with self._lock:
            self._runners[mission_id] = runner
        runner.start()
        self.broadcast(mission_id, state)
        return {"ok": True, "mission": self._summary(store)}

    def pause(self, mission_id: str) -> dict[str, Any]:
        store = self.store_for(mission_id)
        state = store.load_state()
        if state.get("state") in TERMINAL_STATES:
            raise MissionError("mission 已结束", "terminal")
        if state.get("state") == "paused":
            return {"ok": True, "mission": self._summary(store)}
        # Charge the pre-pause phase time now and reset the phase clock so the
        # paused duration lands in pausedMs (paused time never counts against
        # the wall budget; the runner exits and nothing can auto-advance).
        _accrue_state(state)
        state["stateBeforePause"] = state.get("state")
        self._save_state_state(store, state, "paused")
        store.event("pause", {"from": state.get("stateBeforePause")})
        return {"ok": True, "mission": self._summary(store)}

    @staticmethod
    def _save_state_state(store: MissionStore, state: dict[str, Any], new_state: str) -> None:
        # Save without clobbering a concurrent writer: disk wins on counters
        # (a mid-turn _turn save may add newer values), the caller wins on its
        # own writes (phaseStartedAt / stateBeforePause).
        merged = {**state, **store.load_state()}
        merged["pausedMs"] = max(int(merged.get("pausedMs", 0) or 0),
                                 int(state.get("pausedMs", 0) or 0))
        merged["phaseStartedAt"] = state.get("phaseStartedAt")
        merged["state"] = new_state
        merged["wallElapsedMs"] = int(merged.get("agentActiveMs", 0)) + int(merged.get("waitingMs", 0))
        merged["activeMs"] = merged["wallElapsedMs"]
        store.save_state(merged)
        store.event("transition", {"state": new_state})

    def resume(self, mission_id: str) -> dict[str, Any]:
        store = self.store_for(mission_id)
        state = store.load_state()
        if state.get("state") in TERMINAL_STATES:
            raise MissionError("mission 已结束", "terminal")
        if state.get("state") != "paused":
            return {"ok": True, "mission": self._summary(store)}
        previous = state.pop("stateBeforePause", None) or "running"
        _accrue_state(state)  # idle time while paused => pausedMs
        self._reap_runner(mission_id)
        self._ensure_not_active(exclude=mission_id)
        self._save_state_state(store, state, previous)
        # parallel units may hold managed jobs; re-attach their watchers
        self._reconcile_unit_waits(mission_id, store)
        runner = MissionRunner(self, mission_id)
        with self._lock:
            self._runners[mission_id] = runner
        runner.start()
        if previous == "waiting" and state.get("waitingJobId"):
            self._attach_waiting_wake(mission_id, store, state, runner)
        return {"ok": True, "mission": self._summary(store)}

    def _attach_waiting_wake(self, mission_id: str, store: MissionStore,
                             state: dict[str, Any], runner: MissionRunner) -> None:
        """Resuming a waiting mission: re-attach a watcher to the managed job
        if it is still alive; if it finished while paused, wake immediately
        instead of waiting forever for a wake that never comes."""
        job_id = state.get("waitingJobId")
        job = store.load_job(job_id) if job_id else {}
        ident = _process_identity(job) if job.get("pid") else {"alive": False, "reason": "no-pid"}
        if ident["alive"]:
            proc = None
            for w in self._stopped_watchers.pop(mission_id, []):
                if (w.job or {}).get("jobId") == job_id:
                    proc = w.proc
                    break
            watcher = JobWatcher(job, store, lambda w, mid=mission_id: self._wake_resume(mid, w),
                                 proc=proc)
            self.attach_watcher(mission_id, watcher)
            watcher.start()
            store.event("resume", "waiting: watcher re-attached")
            return
        if job.get("status") == "running":
            # its exit was never observed (watcher stopped while paused) —
            # the control plane found it dead on resume
            job["status"] = "orphaned"
            job["orphanReason"] = ident.get("reason")
            job["finishedAt"] = _now_ms()
            store.save_job(job)
        runner.wake({**job, "exitKind": "exited"})

    def cancel(self, mission_id: str) -> dict[str, Any]:
        store = self.store_for(mission_id)
        state = store.load_state()
        if state.get("state") in TERMINAL_STATES:
            raise MissionError("mission 已结束", "terminal")
        with self._lock:
            runner = self._runners.get(mission_id)
        if runner:
            runner.request_stop()
        # Background job lifecycle is owned by the Control Plane: a cancelled
        # mission must take its managed jobs down (grace terminate then kill).
        self.terminate_mission_jobs(mission_id, store, mark="cancelled")
        state.pop("waitingJobId", None)
        self._save_state_state(store, state, "cancelled")
        store.event("cancelled", None)
        # cancel() 返回即须终止：等调度循环观察到 control 事件并退出，
        # 否则 list().activeId 仍会报告一个即将退场的 runner。
        if runner is not None and runner.is_alive():
            runner.join(timeout=5.0)
        return {"ok": True, "mission": self._summary(store)}

    def terminate_mission_jobs(self, mission_id: str, store: MissionStore,
                               mark: str = "cancelled") -> list[dict[str, Any]]:
        """SIGTERM -> grace -> SIGKILL every managed job still running.
        The mission's watcher is stopped and joined FIRST: the Control Plane
        owns the job lifecycle, and a watcher racing the termination could
        persist a different status (e.g. failed) after we persist the mark.
        Returns per-job outcomes; terminal jobs are left untouched."""
        with self._lock:
            watchers = self._watchers.pop(mission_id, [])
        for watcher in watchers:
            watcher.stop()
            watcher.join(timeout=5.0)
        self._stopped_watchers.pop(mission_id, None)
        outcomes: list[dict[str, Any]] = []
        for job in store.list_jobs():
            if job.get("status") in ("completed", "failed", "cancelled", "orphaned"):
                continue
            result = _terminate_job_process(job)
            if result.get("killed"):
                job["status"] = mark
                job["exitKind"] = "terminated"
                job["finishedAt"] = _now_ms()
                job["terminateMode"] = result.get("mode") or ("kill" if result.get("linger") else "term")
            elif result.get("reason") in ("dead", "gone", "no-pid"):
                # process already gone without anyone observing the exit
                job["status"] = "orphaned"
                job["orphanReason"] = result.get("reason")
                job["finishedAt"] = _now_ms()
            else:
                result["status"] = job.get("status") or "running"
            store.save_job(job)
            outcomes.append({"jobId": job.get("jobId"), **result})
            store.event("job-terminate", {"jobId": job.get("jobId"), **result})
        return outcomes

    # -- crash recovery --
    def recover(self) -> list[str]:
        """Scan run dirs; resume non-terminal missions. Waiting missions get
        their job re-checked: a vanished pid wakes immediately."""
        resumed: list[str] = []
        for run_root in self._all_run_roots():
            mission_id = run_root.name
            store = MissionStore(run_root)
            state = store.load_state()
            name = state.get("state")
            if name in TERMINAL_STATES or name in ("draft", None):
                continue
            if name == "paused":
                continue
            if name == "waiting":
                job_id = state.get("waitingJobId")
                job = store.load_job(job_id) if job_id else {}
                ident = _process_identity(job) if job.get("pid") else {"alive": False, "reason": "no-pid"}
                if ident["alive"]:
                    watcher = JobWatcher(job, store, lambda w, mid=mission_id: self._wake_resume(mid, w))
                    self.attach_watcher(mission_id, watcher)
                    watcher.start()
                    store.event("recover", "waiting: job alive, rewatching")
                else:
                    # PID reuse / dead process: neither kill nor attach may
                    # touch it — wake the runner and let the worker repair.
                    tail = job_log_tail(Path(job.get("logPath") or ""))
                    state["delta"] = (f"网关重启期间后台作业已结束（{ident.get('reason')}）："
                                      f"{job.get('command') or job_id}\n--- 日志尾部 ---\n{tail}")
                    if job.get("pid"):
                        # whose exit nobody observed: it is no longer runnable
                        job["status"] = "orphaned"
                        job["orphanReason"] = ident.get("reason")
                        store.save_job(job)
                    state.pop("waitingJobId", None)
                    state["state"] = "running"
                    store.save_state(state)
                    store.event("recover", f"waiting: job gone ({ident.get('reason')}), waking")
            else:
                # P1.2 parallel waits live on units (mission state stays
                # "running"); re-attach their watchers / re-open their jobs
                # before the runner comes back.
                self._reconcile_unit_waits(mission_id, store)
                store.event("recover", f"resuming from {name}")
            self._ensure_not_active_quiet(mission_id)
            runner = MissionRunner(self, mission_id)
            with self._lock:
                self._runners[mission_id] = runner
            runner.start()
            resumed.append(mission_id)
        return resumed

    def _reconcile_unit_waits(self, mission_id: str, store: MissionStore) -> None:
        """After a control-plane restart or a resume, P1.2 missions keep
        their parallel wait state on the UNITS (mission state is "running").
        Re-attach a watcher for every still-alive unit job; a job that died
        while nobody watched is re-opened on its unit with a delta (honest
        evidence, never a fake wake)."""
        state = store.load_state()
        if state.get("state") == "waiting":
            return  # legacy serial wait lives on mission state; own path
        plan = store.load_plan()
        changed = False
        for unit in plan["units"]:
            if unit.get("state") != UNIT_WAITING or not unit.get("jobId"):
                continue
            job = store.load_job(str(unit["jobId"])) or {}
            ident = _process_identity(job) if job.get("pid") else {"alive": False, "reason": "no-pid"}
            if ident["alive"]:
                proc = None
                for w in self._stopped_watchers.pop(mission_id, []):
                    if (w.job or {}).get("jobId") == unit.get("jobId"):
                        proc = w.proc
                        break
                watcher = JobWatcher(job, store,
                                     lambda w, mid=mission_id: self._wake_resume(mid, w),
                                     proc=proc)
                self.attach_watcher(mission_id, watcher)
                watcher.start()
                store.event("recover", f"unit {unit['index']} job alive, rewatching")
            else:
                tail = job_log_tail(Path(job.get("logPath") or ""))
                unit["state"] = unit["status"] = UNIT_RUNNING
                unit["delta"] = (f"网关重启期间后台作业已结束（{ident.get('reason')}）："
                                 f"{job.get('command') or unit['jobId']}\n--- 日志尾部 ---\n{tail}")[:6000]
                if job.get("pid"):
                    job["status"] = "orphaned"
                    job["orphanReason"] = ident.get("reason")
                    job["finishedAt"] = _now_ms()
                    store.save_job(job)
                changed = True
                store.event("recover", f"unit {unit['index']} job gone, delta set")
        if changed:
            store.save_plan(plan)

    def _wake_resume(self, mission_id: str, woken: dict[str, Any]) -> None:
        with self._lock:
            runner = self._runners.get(mission_id)
        if runner and runner.is_alive():
            runner.wake(woken)

    def _ensure_not_active_quiet(self, mission_id: str) -> None:
        with self._lock:
            runner = self._runners.get(mission_id)
            if runner and runner.is_alive():
                runner.request_stop()
                runner.join(timeout=3)
