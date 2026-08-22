#!/usr/bin/env python3
"""P1.2 Real Codex Gates A–K — the full product chain against real codex.

Run:  python3 scripts/gate_p12_driver.py A
      python3 scripts/gate_p12_driver.py all
      GATE_CODEX_BIN=<codex> optional override

Unlike Gate 0 (which probed the app-server directly), these gates drive the
REAL product chain end to end:

    MissionManager -> MissionRunner/Scheduler -> UnitRunner
        -> CodexRuntimeAdapter.run_turn() -> one codex app-server
        -> real Codex -> real git worktrees -> integration branch

Every gate: build a fixture, run for real, trust the evidence, freeze on
FAIL. Discipline: a FAIL is first classified driver-bug vs product-bug;
only product bugs change the harness (then full regression + gate re-run).

Implemented gates:
  A  Parallel Workers — two independent units built by REAL concurrent codex
     turns in their own worktrees; both integrated; the user's checked-out
     branch untouched (HEAD and working tree); unit commits disjoint; real
     cross-unit turn overlap > 1s (raw turn/started & turn/completed
     timestamps, Gate-0 standard).

Evidence per gate: <scratch>/.laomo/gates/p12/<gate>/
  summary.json  timeline.ndjson  stdout.log  (+ fixture artifacts)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))

from codex_adapter import CodexRuntimeAdapter  # noqa: E402
from mission import MissionManager  # noqa: E402

MISSION_TIMEOUT = 900.0   # one real mission end to end
POLL = 0.5
GATE_BIN = os.environ.get("GATE_CODEX_BIN") or shutil.which("codex") \
    or os.path.expanduser("~/.local/bin/codex")


# ---------------------------------------------------------------- evidence


class Evidence:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeline = self.root / "timeline.ndjson"
        self.stdout = self.root / "stdout.log"
        self._fh = self.stdout.open("a", encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except (ValueError, OSError):
            pass  # reader threads may flush after close at teardown

    def raw(self, rec: dict) -> None:
        with self.timeline.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def write(self, name: str, payload: dict) -> None:
        (self.root / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------- raw tap


class TurnTap:
    """Collect per-thread turn lifecycle from RAW app-server notifications:
    thread cwd (which unit worktree a turn lives in) + turn started/completed
    timestamps — the Gate-0-grade evidence standard. Installed on the adapter
    INSTANCE before any process exists (CodexProcess binds the callback at
    construction)."""

    def __init__(self, ev: Evidence, adapter: CodexRuntimeAdapter) -> None:
        self.ev = ev
        self.lock = threading.Lock()
        self.threads: dict[str, dict] = {}   # threadId -> {cwd, turns: {...}}
        self.commands: dict[str, list[str]] = {}  # threadId -> shell commands
        original = adapter._on_notification

        def wrapped(note: dict) -> None:
            try:
                self._collect(note)
            except Exception as exc:
                ev.log(f"tap error: {exc}")
            original(note)

        adapter._on_notification = wrapped

    def _collect(self, note: dict) -> None:
        method = str(note.get("method") or "")
        params = note.get("params") or {}
        tid = str(params.get("threadId") or "")
        rec = {"ts": round(time.time(), 3), "method": method, "threadId": tid}
        if method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "commandExecution":
                cmd = str(item.get("command") or "")[:200]
                rec["cmd"] = cmd
                with self.lock:
                    self.commands.setdefault(tid, []).append(cmd)
        if method == "thread/started":
            thread = params.get("thread") or {}
            tid = str(thread.get("id") or tid)
            rec["threadId"] = tid
            rec["cwd"] = thread.get("cwd")
            rec["ephemeral"] = thread.get("ephemeral")
            with self.lock:
                self.threads.setdefault(tid, {"cwd": thread.get("cwd"),
                                              "turns": {}})
        turn = params.get("turn") or {}
        turn_id = str(params.get("turnId") or turn.get("id") or "")
        if turn_id:
            rec["turnId"] = turn_id
        if method == "turn/started" and tid and turn_id:
            with self.lock:
                self.threads.setdefault(tid, {"cwd": None, "turns": {}})
                self.threads[tid]["turns"][turn_id] = {
                    "startedAt": rec["ts"], "endedAt": None,
                    "status": None}
        if method == "turn/completed" and tid:
            with self.lock:
                t = self.threads.get(tid)
                if t is not None:
                    for known in t["turns"]:
                        if not turn_id or known == turn_id:
                            t["turns"][known]["endedAt"] = rec["ts"]
                            t["turns"][known]["status"] = turn.get("status")
        self.ev.raw(rec)

    def unit_turns(self, suffix: str) -> list[dict]:
        """All turns whose thread cwd ends with `suffix` (e.g. '/u0')."""
        with self.lock:
            out = []
            for tid, t in self.threads.items():
                if t.get("cwd") and str(t["cwd"]).endswith(suffix):
                    for turn_id, turn in t["turns"].items():
                        out.append({"threadId": tid, "turnId": turn_id,
                                    "cwd": t["cwd"], **turn})
            out.sort(key=lambda x: x.get("startedAt") or 0)
            return out


class PromptTap:
    """Record every REAL run_turn invocation (prompt / cwd / read_only /
    result) on the adapter INSTANCE — driver-side evidence, no harness
    change. Captured prompts prove which role template each turn used."""

    def __init__(self, ev: Evidence, adapter: CodexRuntimeAdapter) -> None:
        self.ev = ev
        self.calls: list[dict] = []
        original = adapter.run_turn

        def wrapped(*, prompt, **kwargs):
            started = time.time()
            result = original(prompt=prompt, **kwargs)
            rec = {
                "ts": round(started, 3),
                "endedAt": round(time.time(), 3),
                "cwd": kwargs.get("cwd"),
                "read_only": bool(kwargs.get("read_only")),
                "prompt": (prompt or "")[:600],
                "ok": result.get("ok"),
                "text": (result.get("text") or "")[:4000],
                "error": (result.get("error") or "")[:300],
            }
            self.calls.append(rec)
            try:
                with (ev.root / "turns.ndjson").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
            return rec and result

        adapter.run_turn = wrapped

    def in_cwd(self, cwd_suffix: str) -> list[dict]:
        return [c for c in self.calls
                if c["cwd"] and str(c["cwd"]).endswith(cwd_suffix)]


def overlap(a: dict, b: dict) -> float | None:
    if not all(a.get(k) and b.get(k) for k in ("startedAt", "endedAt")):
        return None
    return round(min(a["endedAt"], b["endedAt"])
                 - max(a["startedAt"], b["startedAt"]), 3)


# ---------------------------------------------------------------- git fixture


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {args} 失败: {proc.stderr[:300]}")
    return proc.stdout.strip()


def porcelain(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain")
    return [line for line in out.splitlines()
            if ".laomo/" not in line]


def init_fixture(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "gate@local")
    git(repo, "config", "user.name", "gate")
    (repo / "README.md").write_text("gate fixture\n", "utf-8")
    (repo / "base.txt").write_text("base\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------- Gate A


UNIT_A_DESC = ("创建文件 backend/a.txt（目录不存在则创建），"
               "内容恰好为一行：REAL-CODEX-A。"
               "除此以外不要创建或修改任何文件，不要执行 git 命令。")
UNIT_B_DESC = ("创建文件 frontend/b.txt（目录不存在则创建），"
               "内容恰好为一行：REAL-CODEX-B。"
               "除此以外不要创建或修改任何文件，不要执行 git 命令。")


def craft_two_unit_plan(store, mid: str) -> None:
    """Deterministic plan: two independent pending units (the planner turn is
    NOT under test in Gate A; workers/evaluators/integration all run real)."""
    def unit(uid: str, index: int, title: str, desc: str, acceptance: list) -> dict:
        return {"id": uid, "index": index, "title": title, "description": desc,
                "acceptance": acceptance, "dependencies": [],
                "state": "pending", "status": "pending",
                "attempt": 0, "repairCount": 0, "conflictCount": 0,
                "conflict": None,
                "worktree": {"path": None, "branch": None,
                             "baseSha": None, "headSha": None},
                "jobId": None, "delta": None, "repairDirective": None,
                "lastVerdict": None,
                "worker": {"startedAt": None, "finishedAt": None},
                "integration": None}
    plan = {"version": 2, "replans": 0, "gitIntegration": True,
            "units": [
                unit("backend", 0, "后端文件", UNIT_A_DESC,
                     ["backend/a.txt 存在且内容包含 REAL-CODEX-A"]),
                unit("frontend", 1, "前端文件", UNIT_B_DESC,
                     ["frontend/b.txt 存在且内容包含 REAL-CODEX-B"]),
            ]}
    store.save_plan(plan)
    store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                      "noProgress": 0, "progressSignature": "",
                      "tokensUsed": 0, "wallElapsedMs": 0,
                      "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                      "phaseStartedAt": 0})


def gate_a(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate A — Parallel Workers (real codex, real worktrees)")
    repo = scratch / "fixture" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    try:
        created = mgr.create(
            "并行完成两个独立小单元，最终在集成分支产出两个文件",
            cwd=str(repo),
            acceptance_criteria=["integration 分支包含 backend/a.txt 与 frontend/b.txt"],
            verification={"requiredFiles": ["backend/a.txt", "frontend/b.txt"],
                          "commands": ["test -f backend/a.txt",
                                       "test -f frontend/b.txt"]})
        mid = created["mission"]["id"]
        ev.log(f"mission {mid} created")
        craft_two_unit_plan(mgr.store_for(mid), mid)
        mgr.start(mid)

        deadline = time.monotonic() + MISSION_TIMEOUT
        state = {}
        while time.monotonic() < deadline:
            state = mgr.status(mid)["mission"]
            if state.get("state") in ("done", "failed", "blocked", "cancelled"):
                break
            time.sleep(POLL)
        ev.log(f"mission terminal: {state.get('state')} "
               f"(stopReason={state.get('stopReason')})")
        detail["mission"] = {"id": mid, "state": state.get("state"),
                             "stopReason": state.get("stopReason")}
        checks["mission-done"] = state.get("state") == "done"

        runs = repo / ".laomo" / "runs" / mid
        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        states = [u["state"] for u in plan["units"]]
        checks["units-integrated"] = states == ["integrated", "integrated"]
        detail["unitStates"] = states

        integ_branch = f"laomo/{mid}/integration"
        integ_dir = repo / ".laomo" / "worktrees" / mid / "integration"

        def show(path: str) -> str:
            return git(repo, "show", f"{integ_branch}:{path}")

        checks["integration-content"] = (
            "REAL-CODEX-A" in show("backend/a.txt")
            and "REAL-CODEX-B" in show("frontend/b.txt"))
        detail["integration"] = {"branch": integ_branch,
                                 "backend/a.txt": show("backend/a.txt").strip(),
                                 "frontend/b.txt": show("frontend/b.txt").strip()}

        # user's checked-out branch untouched: HEAD, working tree, no artifacts
        checks["source-head-unchanged"] = git(repo, "rev-parse", "HEAD") == base_sha
        checks["source-worktree-unchanged"] = porcelain(repo) == before
        checks["source-clean-of-artifacts"] = (
            not (repo / "backend" / "a.txt").exists()
            and not (repo / "frontend" / "b.txt").exists())

        # worktree isolation: each unit's commits touch only its own file
        log = git(repo, "log", "--name-only", "--pretty=format:@@%s",
                  integ_branch)
        unit_files: dict[int, set] = {0: set(), 1: set()}
        current: int | None = None
        marker = re.compile(r"laomo: unit #(\d+)")
        for line in log.splitlines():
            m = marker.search(line) if line.startswith("@@") else None
            if m:
                current = int(m.group(1)) - 1
            elif line.strip() and current is not None:
                unit_files[current].add(line.strip())
            elif line.startswith("@@"):
                current = None  # non-unit commit (base etc.) owns nothing
        checks["unit-commits-disjoint"] = bool(
            unit_files[0] and unit_files[1]
            and "frontend/b.txt" not in unit_files[0]
            and "backend/a.txt" not in unit_files[1])
        detail["unitCommitFiles"] = {k: sorted(v) for k, v in unit_files.items()}

        # REAL codex turn overlap across units (Gate-0 standard, >1s)
        turns_u0 = tap.unit_turns("/u0")
        turns_u1 = tap.unit_turns("/u1")
        best, best_pair = -1.0, None
        for ta in turns_u0:
            for tb in turns_u1:
                ov = overlap(ta, tb)
                if ov is not None and ov > best:
                    best, best_pair = ov, (ta, tb)
        checks["real-turn-overlap"] = best > 1.0
        detail["codex"] = {
            "u0Turns": turns_u0, "u1Turns": turns_u1,
            "bestOverlapSec": best,
            "bestPair": {k: {x: t[x] for x in ("threadId", "turnId", "startedAt", "endedAt")}
                         for k, t in (("A", best_pair[0]), ("B", best_pair[1]))}
            if best_pair else None,
        }
        ev.log(f"u0 turns={len(turns_u0)} u1 turns={len(turns_u1)} "
               f"best overlap={best}s")
    finally:
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "A", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate A verdict: {result['verdict']} checks={checks}")
    return result




UNIT_C_DESC = ("读取工作目录中的 deps/a.txt 与 deps/b.txt，然后创建 result.txt，"
               "内容为三行：第一行是 deps/a.txt 的内容，第二行是 deps/b.txt 的内容，"
               "第三行是 DEPENDENCY-OK。"
               "除此以外不要创建或修改任何文件，不要执行 git 命令。")


def craft_three_unit_plan(store, mid: str) -> None:
    """A/B independent, C depends on both — the dependency-barrier fixture."""

    def unit(uid, index, title, desc, acceptance, deps=None):
        return {"id": uid, "index": index, "title": title, "description": desc,
                "acceptance": acceptance, "dependencies": deps or [],
                "state": "pending", "status": "pending",
                "attempt": 0, "repairCount": 0, "conflictCount": 0,
                "conflict": None,
                "worktree": {"path": None, "branch": None,
                             "baseSha": None, "headSha": None},
                "jobId": None, "delta": None, "repairDirective": None,
                "lastVerdict": None,
                "worker": {"startedAt": None, "finishedAt": None},
                "integration": None}
    desc_a = ("创建文件 deps/a.txt（目录不存在则创建），"
              "内容恰好为一行：REAL-CODEX-A。"
              "除此以外不要创建或修改任何文件，不要执行 git 命令。")
    desc_b = ("创建文件 deps/b.txt（目录不存在则创建），"
              "内容恰好为一行：REAL-CODEX-B。"
              "除此以外不要创建或修改任何文件，不要执行 git 命令。")
    plan = {"version": 2, "replans": 0, "gitIntegration": True,
            "units": [
                unit("a", 0, "产物A", desc_a,
                     ["deps/a.txt 存在且内容包含 REAL-CODEX-A"]),
                unit("b", 1, "产物B", desc_b,
                     ["deps/b.txt 存在且内容包含 REAL-CODEX-B"]),
                unit("c", 2, "合成C", UNIT_C_DESC,
                     ["result.txt 存在且包含 REAL-CODEX-A、REAL-CODEX-B、DEPENDENCY-OK"],
                     deps=["a", "b"]),
            ]}
    store.save_plan(plan)
    store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                      "noProgress": 0, "progressSignature": "",
                      "tokensUsed": 0, "wallElapsedMs": 0,
                      "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                      "phaseStartedAt": 0})


def gate_b(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate B — Dependency Barrier (real codex, bare PASS must not "
           "satisfy a git dependency)")
    repo = scratch / "fixture-b" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    window: dict = {"captured": False}
    try:
        created = mgr.create(
            "三个单元：A/B 并行产出，C 依赖二者并合成结果",
            cwd=str(repo),
            acceptance_criteria=["integration 分支包含三个产物文件且 result.txt 合成正确"],
            options={"maxParallelWorkers": 3},
            verification={"requiredFiles": ["deps/a.txt", "deps/b.txt",
                                            "result.txt"],
                          "commands": ["grep -q REAL-CODEX-A result.txt",
                                       "grep -q REAL-CODEX-B result.txt",
                                       "grep -q DEPENDENCY-OK result.txt"]})
        mid = created["mission"]["id"]
        ev.log(f"mission {mid} created (maxParallelWorkers=3)")
        craft_three_unit_plan(mgr.store_for(mid), mid)

        # ONE-SHOT test hook: delay unit B's INTEGRATION only (never the
        # dependency logic). While A is not yet integrated, keep deferring;
        # once A is integrated, observe one full window (A integrated, B bare
        # passed, C pending, free slot) and release on the next call.
        from mission.manager import MissionRunner
        orig_integrate = MissionRunner._integrate_harvested

        def delayed(self, state, index):
            if index != 1 or window.get("released"):
                return orig_integrate(self, state, index)
            plan = self.store.load_plan()
            a_state = next((u["state"] for u in plan["units"]
                            if u["index"] == 0), None)
            b_state = next((u["state"] for u in plan["units"]
                            if u["index"] == 1), None)
            c_state = next((u["state"] for u in plan["units"]
                            if u["index"] == 2), None)
            if a_state != "integrated":
                ev.log(f"hook: defer B integration (A={a_state})")
                return "ok"
            if not window["captured"]:
                with mgr._lock:
                    pool = mgr._unit_threads.get(mid) or {}
                    owned = sum(1 for t in pool.values() if t.is_alive())
                window.update({"captured": True, "ts": time.time(),
                               "A": a_state, "B": b_state, "C": c_state,
                               "ownedUnits": owned,
                               "freeSlots": 3 - owned})
                ev.log(f"hook WINDOW: A={a_state} B={b_state} C={c_state} "
                       f"owned={owned} free={3 - owned} — C must stay pending")
                return "ok"  # one more iteration with B still bare-passed
            window["released"] = True
            ev.log("hook: release B integration")
            return orig_integrate(self, state, index)

        MissionRunner._integrate_harvested = delayed
        try:
            mgr.start(mid)
            deadline = time.monotonic() + MISSION_TIMEOUT
            state = {}
            while time.monotonic() < deadline:
                state = mgr.status(mid)["mission"]
                if state.get("state") in ("done", "failed", "blocked", "cancelled"):
                    break
                time.sleep(POLL)
        finally:
            MissionRunner._integrate_harvested = orig_integrate
        ev.log(f"mission terminal: {state.get('state')} "
               f"(stopReason={state.get('stopReason')})")
        checks["mission-done"] = state.get("state") == "done"
        detail["mission"] = {"id": mid, "state": state.get("state"),
                             "stopReason": state.get("stopReason")}

        runs = repo / ".laomo" / "runs" / mid
        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        states = [u["state"] for u in plan["units"]]
        checks["units-integrated"] = states == ["integrated"] * 3
        detail["unitStates"] = states

        events = [json.loads(l) for l in
                  (runs / "events.ndjson").read_text("utf-8").splitlines() if l.strip()]
        dispatch_ts = {e["detail"]["unit"]: e["ts"]
                       for e in events if e["type"] == "dispatch"
                       and isinstance(e.get("detail"), dict)}
        integrated_ts = {e["detail"]["unit"]: e["ts"]
                         for e in events if e["type"] == "integration"
                         and isinstance(e.get("detail"), dict)
                         and e["detail"].get("phase") == "integrated"}
        detail["dispatchTs"] = dispatch_ts
        detail["integratedTs"] = integrated_ts

        # 3/4. the window itself: free slot + C pending while B bare-passed
        checks["free-slot-proven"] = bool(
            window.get("captured") and window.get("freeSlots", 0) >= 1)
        checks["bare-pass-barrier"] = bool(
            window.get("captured") and window.get("B") == "passed"
            and window.get("C") == "pending")
        detail["window"] = window

        # 5/6/7. C starts strictly after BOTH deps integrated
        c_dispatch = dispatch_ts.get(2)
        barrier = max(integrated_ts.get(0, 0), integrated_ts.get(1, 0))
        early_dispatches = [e["ts"] for e in events
                            if e["type"] == "dispatch"
                            and isinstance(e.get("detail"), dict)
                            and e["detail"].get("unit") == 2
                            and e["ts"] < barrier]
        checks["no-early-dispatch"] = (c_dispatch is not None
                                       and not early_dispatches)
        c_turns = tap.unit_turns("/u2")
        early_turns = [t for t in c_turns
                       if (t.get("startedAt") or 0) * 1000 < barrier]
        checks["no-early-codex-turn"] = bool(c_turns) and not early_turns
        checks["start-after-integrated"] = bool(
            c_dispatch and c_dispatch >= barrier
            and c_turns
            and (c_turns[0].get("startedAt") or 0) * 1000 >= barrier)
        detail["codex"] = {"cTurns": c_turns,
                           "earlyDispatchTs": early_dispatches,
                           "earlyTurns": early_turns,
                           "barrierTs": barrier}

        # integrated-base visibility: C actually READ A+B's work — its
        # result must combine both markers (C's worktree was cut from the
        # integration HEAD that already carries A+B)
        integ_branch = f"laomo/{mid}/integration"
        try:
            result = git(repo, "show", f"{integ_branch}:result.txt")
        except AssertionError:
            result = ""
        checks["integrated-base-visibility"] = all(
            marker in result for marker in
            ("REAL-CODEX-A", "REAL-CODEX-B", "DEPENDENCY-OK"))
        detail["resultTxt"] = result.strip()

        # 8. source isolation
        checks["source-isolation"] = (git(repo, "rev-parse", "HEAD") == base_sha
                                      and porcelain(repo) == before)
    finally:
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "B", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate B verdict: {result['verdict']} checks={checks}")
    return result

CONFLICT_UNIT_DESC_A = (
    "把工作目录中的 shared.txt 全文替换为恰好一行：REAL-CODEX-A。"
    "除此以外不要创建或修改任何文件，不要执行 git 命令。")
CONFLICT_UNIT_DESC_B = (
    "把工作目录中的 shared.txt 全文替换为恰好一行：REAL-CODEX-B。"
    "除此以外不要创建或修改任何文件，不要执行 git 命令。")


def init_conflict_fixture(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "gate@local")
    git(repo, "config", "user.name", "gate")
    (repo / "README.md").write_text("gate fixture\n", "utf-8")
    (repo / "shared.txt").write_text("BASE\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return git(repo, "rev-parse", "HEAD")


GIT_MUTATION = re.compile(
    r"\bgit\s+(add|commit|merge|rebase|reset|checkout|cherry-pick|stash)\b")


def gate_c(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate C — Conflict Resolver (real codex, real content conflict)")
    repo = scratch / "fixture-c" / "repo"
    base_sha = init_conflict_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    try:
        created = mgr.create(
            "两个并行单元各自改写 shared.txt，冲突由系统自动解决，"
            "最终两侧意图都保留在集成分支",
            cwd=str(repo),
            acceptance_criteria=["shared.txt 同时包含 REAL-CODEX-A 与 REAL-CODEX-B"],
            options={"maxParallelWorkers": 2},
            verification={"requiredFiles": ["shared.txt"],
                          "commands": ["grep -q REAL-CODEX-A shared.txt",
                                       "grep -q REAL-CODEX-B shared.txt"]})
        mid = created["mission"]["id"]

        def unit(uid, index, title, desc, acceptance):
            return {"id": uid, "index": index, "title": title,
                    "description": desc, "acceptance": acceptance,
                    "dependencies": [], "state": "pending", "status": "pending",
                    "attempt": 0, "repairCount": 0, "conflictCount": 0,
                    "conflict": None,
                    "worktree": {"path": None, "branch": None,
                                 "baseSha": None, "headSha": None},
                    "jobId": None, "delta": None, "repairDirective": None,
                    "lastVerdict": None,
                    "worker": {"startedAt": None, "finishedAt": None},
                    "integration": None}
        store = mgr.store_for(mid)
        store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                         "units": [
                             unit("a", 0, "改写A", CONFLICT_UNIT_DESC_A,
                                  ["shared.txt 内容包含 REAL-CODEX-A"]),
                             unit("b", 1, "改写B", CONFLICT_UNIT_DESC_B,
                                  ["shared.txt 内容包含 REAL-CODEX-B"]),
                         ]})
        store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        mgr.start(mid)

        deadline = time.monotonic() + MISSION_TIMEOUT
        state = {}
        while time.monotonic() < deadline:
            state = mgr.status(mid)["mission"]
            if state.get("state") in ("done", "failed", "blocked", "cancelled"):
                break
            time.sleep(POLL)
        ev.log(f"mission terminal: {state.get('state')} "
               f"(stopReason={state.get('stopReason')})")

        runs = repo / ".laomo" / "runs" / mid
        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        events = [json.loads(l) for l in
                  (runs / "events.ndjson").read_text("utf-8").splitlines()
                  if l.strip()]
        dispatch_ts = {e["detail"]["unit"]: e["ts"] for e in events
                       if e["type"] == "dispatch"
                       and isinstance(e.get("detail"), dict)}
        integ_events = [e for e in events if e["type"] == "integration"
                        and isinstance(e.get("detail"), dict)]
        resolve_ev = next((e for e in integ_events
                           if e["detail"].get("phase") == "conflict-resolve"), None)
        integrated_ts = {e["detail"]["unit"]: e["ts"] for e in integ_events
                         if e["detail"].get("phase") == "integrated"}
        verification_ts = next(
            (e["ts"] for e in events if e["type"] == "transition"
             and isinstance(e.get("detail"), dict)
             and e["detail"].get("state") == "verification"), None)

        checks["mission-done"] = state.get("state") == "done"
        checks["units-integrated"] = (
            [u["state"] for u in plan["units"]] == ["integrated"] * 2)
        detail["mission"] = {"id": mid, "state": state.get("state"),
                             "stopReason": state.get("stopReason")}

        # dynamic winner/conflict identification — real race decides
        conflict_idx = None
        if resolve_ev is not None:
            conflict_idx = resolve_ev["detail"].get("unit")
        winner_idx = 1 - conflict_idx if conflict_idx is not None else None
        checks["real-git-conflict"] = conflict_idx is not None
        detail["conflictUnit"] = conflict_idx
        detail["winnerUnit"] = winner_idx
        detail["resolveEventTs"] = resolve_ev["ts"] if resolve_ev else None
        detail["dispatchTs"] = dispatch_ts
        detail["integratedTs"] = integrated_ts

        cu = plan["units"][conflict_idx] if conflict_idx is not None else {}
        conflict_rec = cu.get("conflict") or {}
        checks["conflict-evidence"] = bool(
            conflict_rec.get("integrationHead")
            and conflict_rec.get("unitHead")
            and conflict_rec.get("mergeBase")
            and conflict_rec.get("files"))
        detail["conflictRecord"] = {
            k: conflict_rec.get(k) for k in
            ("integrationHead", "unitHead", "mergeBase", "files", "attempt")}
        files = [f.get("path") for f in conflict_rec.get("files") or []]
        detail["conflictFiles"] = files

        # 1. initial real worker overlap (u0 x u1 first turns)
        t0 = tap.unit_turns("/u0")
        t1 = tap.unit_turns("/u1")
        best = -1.0
        for ta in t0:
            for tb in t1:
                ov = overlap(ta, tb)
                if ov is not None and ov > best:
                    best = ov
        checks["real-worker-overlap"] = best > 1.0
        detail["initialOverlapSec"] = best

        # 4/5/6. resolver turn: real prompt, unit cwd, no git mutations
        cu_path = (cu.get("worktree") or {}).get("path")
        resolver_call = None
        if cu_path and resolve_ev is not None:
            window_end = integrated_ts.get(conflict_idx, float("inf")) / 1000.0
            for c in ptap.in_cwd("/u%d" % conflict_idx):
                if resolve_ev["ts"] / 1000.0 <= c["ts"] <= window_end \
                        and "Conflict Resolver" in c["prompt"]:
                    resolver_call = c
                    break
        checks["resolver-cwd-is-unit"] = bool(
            resolver_call and resolver_call["cwd"] == cu_path
            and str(cu_path).endswith(f"/u{conflict_idx}"))
        checks["resolver-prompt-real"] = bool(
            resolver_call and "Conflict Resolver" in resolver_call["prompt"]
            and "禁止执行" in resolver_call["prompt"])
        # the resolver's thread (matching turn window) ran no git mutations
        resolver_cmds: list[str] = []
        if resolver_call is not None:
            with tap.lock:
                for tid, t in tap.threads.items():
                    if not t.get("cwd") or not str(t["cwd"]).endswith(
                            f"/u{conflict_idx}"):
                        continue
                    for turn in t["turns"].values():
                        if resolver_call["ts"] <= (turn.get("startedAt") or 0) \
                                <= resolver_call["endedAt"]:
                            resolver_cmds.extend(tap.commands.get(tid, []))
        git_mutations = [c for c in resolver_cmds if GIT_MUTATION.search(c)]
        checks["resolver-no-git"] = resolver_call is not None \
            and not git_mutations
        detail["resolver"] = {
            "cwd": resolver_call["cwd"] if resolver_call else None,
            "promptHead": (resolver_call["prompt"][:120]
                           if resolver_call else None),
            "commands": resolver_cmds[:20],
            "gitMutations": git_mutations,
        }
        # resolver thread/turn ids for the report
        if resolver_call is not None:
            with tap.lock:
                for tid, t in tap.threads.items():
                    if t.get("cwd") == resolver_call["cwd"]:
                        for turn_id, turn in t["turns"].items():
                            if resolver_call["ts"] <= (turn.get("startedAt") or 0) \
                                    <= resolver_call["endedAt"]:
                                detail["resolver"]["threadId"] = tid
                                detail["resolver"]["turnId"] = turn_id

        # 7. conflict unit evaluated at least twice (initial + post-resolve)
        evals = [c for c in ptap.in_cwd(f"/u{conflict_idx}")
                 if c["read_only"]]
        checks["reevaluated"] = len(evals) >= 2
        detail["conflictUnitEvaluators"] = len(evals)

        # 8. BOTH intents on the integration branch at the moment the
        # conflict unit's (second) integration completed — before the
        # machine gate even starts
        integ_branch = f"laomo/{mid}/integration"
        shared = git(repo, "show", f"{integ_branch}:shared.txt")
        (ev.root / "pre-verification-shared.txt").write_text(shared, "utf-8")
        ci_integrated_ts = integrated_ts.get(conflict_idx)
        checks["both-intents-before-machine-gate"] = bool(
            "REAL-CODEX-A" in shared and "REAL-CODEX-B" in shared
            and ci_integrated_ts is not None
            and verification_ts is not None
            and resolve_ev["ts"] < ci_integrated_ts < verification_ts)
        detail["preVerificationShared"] = shared.strip()
        detail["verificationTs"] = verification_ts

        # 9. no MERGE_HEAD residue anywhere
        integ_dir = repo / ".laomo" / "worktrees" / mid / "integration"
        checks["conflict-cleaned"] = not subprocess.run(
            ["git", "-C", str(integ_dir), "rev-parse", "-q", "--verify",
             "MERGE_HEAD"], capture_output=True).returncode == 0
        detail["unitWorktreesCleaned"] = all(
            not (repo / ".laomo" / "worktrees" / mid / f"u{i}").is_dir()
            for i in range(2))

        # 10. source isolation
        checks["source-isolation"] = (
            git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before)

        detail["conflictCount"] = cu.get("conflictCount")
        detail["repairCount"] = cu.get("repairCount")
        checks["budgets-honest"] = (cu.get("conflictCount") == 1
                                    and cu.get("repairCount") == 0)
    finally:
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "C", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate C verdict: {result['verdict']} checks={checks}")
    return result


JOB_UNIT_DESC = (
    "本单元必须经过一个真实后台长任务才能完成，分两轮：\n"
    "第一轮：不要自己等待长命令。直接在回复末尾输出 LAOMO_JOB 标记块结束本轮，"
    "内容为：command=\"sleep 65; printf 'BACKGROUND-DONE\\n' > job-output.txt; "
    "printf 'JOB-LOG-DONE\\n'\"，reason=\"真实长任务门禁\"，expectedSeconds=70。"
    "不要写 cwd 字段。\n"
    "第二轮（被系统唤醒后）：读取工作目录中的 job-output.txt，"
    "然后创建 final.txt，内容为两行：第一行 BACKGROUND-DONE，第二行 WAKE-RESUMED。"
    "除此之外不要创建或修改任何其它文件，全程不要执行 git 命令。")


def gate_d(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate D — Long Job Wait/Wake (real codex, real >=60s job)")
    repo = scratch / "fixture-d" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    watch = {"waiting": None, "alive": None, "pgid": None, "pid": None,
             "sampleTs": None}
    stop_watch = threading.Event()
    try:
        created = mgr.create(
            "完成一个必须经过真实后台长任务的产物",
            cwd=str(repo),
            acceptance_criteria=["final.txt 包含 BACKGROUND-DONE 与 WAKE-RESUMED"],
            verification={"requiredFiles": ["final.txt", "job-output.txt"],
                          "commands": ["grep -q BACKGROUND-DONE final.txt",
                                       "grep -q WAKE-RESUMED final.txt",
                                       "grep -q BACKGROUND-DONE job-output.txt"]})
        mid = created["mission"]["id"]
        runs = repo / ".laomo" / "runs" / mid
        store = mgr.store_for(mid)
        store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                         "units": [{"id": "job", "index": 0, "title": "长任务单元",
                                    "description": JOB_UNIT_DESC,
                                    "acceptance": ["final.txt 存在且包含 "
                                                   "BACKGROUND-DONE 与 WAKE-RESUMED"],
                                    "dependencies": [],
                                    "state": "pending", "status": "pending",
                                    "attempt": 0, "repairCount": 0,
                                    "conflictCount": 0, "conflict": None,
                                    "worktree": {"path": None, "branch": None,
                                                 "baseSha": None, "headSha": None},
                                    "jobId": None, "delta": None,
                                    "repairDirective": None, "lastVerdict": None,
                                    "worker": {"startedAt": None, "finishedAt": None},
                                    "integration": None}]})
        store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})

        def watcher():
            plan_path = runs / "plan.json"
            while not stop_watch.is_set():
                try:
                    plan = json.loads(plan_path.read_text("utf-8"))
                    u = plan["units"][0]
                    if u.get("state") == "waiting" and watch["waiting"] is None:
                        watch["waiting"] = time.time()
                    if u.get("state") == "waiting" and u.get("jobId"):
                        jp = runs / "jobs" / (u["jobId"] + ".json")
                        if jp.is_file():
                            j = json.loads(jp.read_text("utf-8"))
                            pid = j.get("pid")
                            if pid and j.get("status") == "running":
                                try:
                                    os.kill(pid, 0)
                                    watch["alive"] = True
                                    watch["pid"] = pid
                                    watch["pgid"] = subprocess.run(
                                        ["ps", "-o", "pgid=", "-p", str(pid)],
                                        capture_output=True,
                                        text=True).stdout.strip()
                                    watch["sampleTs"] = time.time()
                                except OSError:
                                    watch["alive"] = False
                except Exception:
                    pass
                stop_watch.wait(0.4)
        threading.Thread(target=watcher, daemon=True).start()

        mgr.start(mid)
        deadline = time.monotonic() + MISSION_TIMEOUT
        state = {}
        while time.monotonic() < deadline:
            state = mgr.status(mid)["mission"]
            if state.get("state") in ("done", "failed", "blocked", "cancelled"):
                break
            time.sleep(POLL)
        stop_watch.set()
        ev.log(f"mission terminal: {state.get('state')} "
               f"(stopReason={state.get('stopReason')})")

        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        unit = plan["units"][0]
        events = [json.loads(l) for l in
                  (runs / "events.ndjson").read_text("utf-8").splitlines()
                  if l.strip()]

        checks["mission-done"] = state.get("state") == "done"
        checks["unit-integrated"] = unit["state"] == "integrated"
        detail["mission"] = {"id": mid, "state": state.get("state"),
                             "stopReason": state.get("stopReason")}

        # the job record: real process, honest exit, unit cwd
        job_files = sorted((runs / "jobs").glob("*.json"))
        job = json.loads(job_files[0].read_text("utf-8")) if job_files else {}
        checks["job-marker-real"] = bool(job.get("command")) \
            and "sleep 65" in str(job.get("command"))
        checks["job-real-process"] = bool(
            job.get("pid") and job.get("pgid") and job.get("startIdentity"))
        detail["job"] = {k: job.get(k) for k in
                         ("jobId", "pid", "pgid", "cwd", "command", "status",
                          "exitCode", "exitKind", "exitUnknown", "startedAt",
                          "finishedAt", "expectedWakeAt", "startIdentity")}
        checks["job-cwd-unit"] = bool(
            job.get("cwd") and str(job["cwd"]).endswith("/u0"))
        checks["job-exit-honest"] = (job.get("status") == "completed"
                                     and job.get("exitCode") == 0
                                     and job.get("exitUnknown") is False
                                     and job.get("exitKind") == "exited")
        duration_ms = (job.get("finishedAt") or 0) - (job.get("startedAt") or 0)
        checks["long-enough"] = duration_ms >= 60_000
        checks["unit-waiting"] = watch["waiting"] is not None

        # OS-level: model idle while the background process lived
        checks["os-alive-while-model-idle"] = bool(
            watch["alive"] and watch["pgid"]
            and str(watch["pgid"]).strip() == str(watch["pid"]))
        detail["osSample"] = {k: watch[k] for k in
                              ("waiting", "alive", "pid", "pgid", "sampleTs")}

        # turn timeline from the raw tap (u0)
        turns = tap.unit_turns("/u0")
        turn1 = turns[0] if turns else None
        wake_ev = next((e for e in events if e["type"] == "wake"
                        and isinstance(e.get("detail"), dict)
                        and e["detail"].get("jobId") == job.get("jobId")), None)
        wake_ms = wake_ev["ts"] if wake_ev else None
        turn2 = next((t for t in turns
                      if wake_ms is not None
                      and (t.get("startedAt") or 0) * 1000 >= wake_ms), None)
        detail["turns"] = turns
        detail["wakeTs"] = wake_ms

        checks["worker-turn-ended"] = bool(
            turn1 and turn1.get("endedAt")
            and job.get("startedAt")
            and turn1["endedAt"] * 1000 < job["startedAt"])
        checks["wake-event"] = wake_ev is not None
        # ordering: turn1.end < job.start < job.finish <= wake <= turn2.start
        checks["timeline-ordered"] = bool(
            turn1 and turn2 and wake_ms is not None
            and job.get("startedAt") and job.get("finishedAt")
            and turn1["endedAt"] * 1000 < job["startedAt"]
            < job["finishedAt"] <= wake_ms
            <= turn2["startedAt"] * 1000)
        # zero codex turns for this unit while the job ran
        during = [t for t in turns
                  if turn1 and job.get("startedAt") and job.get("finishedAt")
                  and turn1["endedAt"] * 1000 <= (t.get("startedAt") or 0) * 1000
                  < job["finishedAt"]]
        checks["model-not-polling"] = not during
        checks["fresh-turn-after-wake"] = bool(
            turn1 and turn2
            and turn1["threadId"] != turn2["threadId"]
            and turn1["turnId"] != turn2["turnId"])
        detail["turn1"] = {k: turn1.get(k) for k in
                           ("threadId", "turnId", "startedAt", "endedAt")} if turn1 else None
        detail["turn2"] = {k: turn2.get(k) for k in
                           ("threadId", "turnId", "startedAt", "endedAt")} if turn2 else None

        # delta carried into the second turn's prompt
        t2_call = next((c for c in ptap.in_cwd("/u0")
                        if turn2 and abs(c["ts"] - turn2["startedAt"]) < 5
                        and not c["read_only"]), None)
        checks["delta-resumed"] = bool(
            t2_call and "自上次唤醒的增量" in t2_call["prompt"]
            and "job-output" in (t2_call["prompt"]
                                 + (unit.get("delta") or "")))
        detail["turn2PromptHead"] = (t2_call["prompt"][:200]
                                     if t2_call else None)

        integ_branch = f"laomo/{mid}/integration"
        final_txt = git(repo, "show", f"{integ_branch}:final.txt")
        job_out = git(repo, "show", f"{integ_branch}:job-output.txt")
        (ev.root / "final.txt").write_text(final_txt, "utf-8")
        checks["final-content"] = ("BACKGROUND-DONE" in final_txt
                                   and "WAKE-RESUMED" in final_txt
                                   and "BACKGROUND-DONE" in job_out)
        detail["finalTxt"] = final_txt.strip()

        checks["source-isolation"] = (
            git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before)
    finally:
        stop_watch.set()
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "D", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate D verdict: {result['verdict']} checks={checks}")
    return result


PAUSE_UNIT_A = (
    "本轮内直接执行 shell 命令 sleep 15 并等待它完成（不要输出任何任务标记块），"
    "然后创建 a.txt，内容恰好为一行：REAL-CODEX-E-A。"
    "除此以外不要创建或修改任何文件，不要执行 git 命令。")
PAUSE_UNIT_B = (
    "本轮内直接执行 shell 命令 sleep 15 并等待它完成（不要输出任何任务标记块），"
    "然后创建 b.txt，内容恰好为一行：REAL-CODEX-E-B。"
    "除此以外不要创建或修改任何文件，不要执行 git 命令。")
PAUSE_UNIT_C = ("创建 c.txt，内容恰好为一行：REAL-CODEX-E-C。"
                "除此以外不要创建或修改任何文件，不要执行 git 命令。")


def gate_e(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate E — Pause / Resume (quiesce at safe point, no replay)")
    repo = scratch / "fixture-e" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    try:
        created = mgr.create(
            "三个独立单元：A/B 先行（长命令在轮内等待），C 等待空闲槽位",
            cwd=str(repo),
            acceptance_criteria=["integration 分支包含 a.txt/b.txt/c.txt"],
            options={"maxParallelWorkers": 2},
            verification={"requiredFiles": ["a.txt", "b.txt", "c.txt"],
                          "commands": ["grep -q REAL-CODEX-E-A a.txt",
                                       "grep -q REAL-CODEX-E-B b.txt",
                                       "grep -q REAL-CODEX-E-C c.txt"]})
        mid = created["mission"]["id"]
        runs = repo / ".laomo" / "runs" / mid

        def unit(uid, index, title, desc, marker):
            return {"id": uid, "index": index, "title": title,
                    "description": desc,
                    "acceptance": [f"{marker} 所在文件存在且内容正确"],
                    "dependencies": [], "state": "pending", "status": "pending",
                    "attempt": 0, "repairCount": 0, "conflictCount": 0,
                    "conflict": None,
                    "worktree": {"path": None, "branch": None,
                                 "baseSha": None, "headSha": None},
                    "jobId": None, "delta": None, "repairDirective": None,
                    "lastVerdict": None,
                    "worker": {"startedAt": None, "finishedAt": None},
                    "integration": None}
        store = mgr.store_for(mid)
        store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                         "units": [unit("a", 0, "单元A", PAUSE_UNIT_A, "a.txt"),
                                   unit("b", 1, "单元B", PAUSE_UNIT_B, "b.txt"),
                                   unit("c", 2, "单元C", PAUSE_UNIT_C, "c.txt")]})
        store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        mgr.start(mid)

        def events_now():
            if not (runs / "events.ndjson").is_file():
                return []
            return [json.loads(l) for l in
                    (runs / "events.ndjson").read_text("utf-8").splitlines()
                    if l.strip()]

        # -- window 1: both A/B turns ACTIVE, C never dispatched -> pause
        deadline = time.monotonic() + 120
        paused_at = None
        while time.monotonic() < deadline:
            t0 = tap.unit_turns("/u0")
            t1 = tap.unit_turns("/u1")
            a_active = any(t.get("endedAt") is None for t in t0)
            b_active = any(t.get("endedAt") is None for t in t1)
            c_disp = any(e["type"] == "dispatch"
                         and isinstance(e.get("detail"), dict)
                         and e["detail"].get("unit") == 2 for e in events_now())
            if a_active and b_active and not c_disp:
                mgr.pause(mid)
                paused_at = time.time()
                ev.log("pause requested while A and B turns both active")
                break
            time.sleep(0.2)
        checks["two-workers-active-before-pause"] = paused_at is not None
        detail["pauseRequestedAt"] = paused_at

        # -- window 2: safe point — paused persisted, threads drained
        deadline = time.monotonic() + 180
        quiesced = None
        while time.monotonic() < deadline:
            st = (mgr.status(mid).get("mission") or {})
            with mgr._lock:
                pool = mgr._unit_threads.get(mid) or {}
                live_units = sum(1 for t in pool.values() if t.is_alive())
                runner = mgr._runners.get(mid)
                runner_alive = bool(runner and runner.is_alive())
            if (st.get("state") == "paused" and live_units == 0
                    and not runner_alive):
                quiesced = time.time()
                break
            time.sleep(0.2)
        checks["pause-state-persisted"] = quiesced is not None
        checks["safe-point-reached"] = quiesced is not None
        plan_q = json.loads((runs / "plan.json").read_text("utf-8")) \
            if quiesced else {}
        detail["safePoint"] = {
            "quiescedAt": quiesced,
            "unitStates": [u["state"] for u in plan_q.get("units", [])],
        }
        st_q = json.loads((runs / "state.json").read_text("utf-8")) \
            if quiesced else {}
        detail["timing"] = {"pausedMsAtQuiesce": st_q.get("pausedMs"),
                            "wallMsAtQuiesce": st_q.get("wallElapsedMs")}

        # in-flight turns ended NATURALLY (not interrupted)
        t0 = tap.unit_turns("/u0")
        t1 = tap.unit_turns("/u1")
        initial0 = t0[0] if t0 else {}
        initial1 = t1[0] if t1 else {}
        checks["inflight-turns-not-interrupted"] = (
            initial0.get("status") == "completed"
            and initial1.get("status") == "completed")
        detail["initialTurns"] = {"A": initial0, "B": initial1}

        # -- window 3: hold 10s, absolutely nothing may move
        if quiesced is not None:
            ev_snap = events_now()
            disp_before = sum(1 for e in ev_snap if e["type"] == "dispatch")
            integ_before = sum(1 for e in ev_snap if e["type"] == "integration")
            turns_before = len(tap.unit_turns("/u0")) + len(tap.unit_turns("/u1")) \
                + len(tap.unit_turns("/u2"))
            evals_before = len(ptap.calls)
            time.sleep(10.0)
            ev_after = events_now()
            disp_after = sum(1 for e in ev_after if e["type"] == "dispatch")
            integ_after = sum(1 for e in ev_after if e["type"] == "integration")
            turns_after = len(tap.unit_turns("/u0")) + len(tap.unit_turns("/u1")) \
                + len(tap.unit_turns("/u2"))
            evals_after = len(ptap.calls)
            c_starts = sum(1 for e in ev_after
                           if e["type"] == "dispatch"
                           and isinstance(e.get("detail"), dict)
                           and e["detail"].get("unit") == 2) \
                - sum(1 for e in ev_snap
                      if e["type"] == "dispatch"
                      and isinstance(e.get("detail"), dict)
                      and e["detail"].get("unit") == 2)
            checks["paused-window-no-dispatch"] = disp_after == disp_before
            checks["paused-window-no-turns"] = turns_after == turns_before
            checks["paused-window-no-evaluator"] = evals_after == evals_before
            checks["paused-window-no-integration"] = integ_after == integ_before
            checks["C-not-started-while-paused"] = c_starts == 0
            detail["hold"] = {"dispatchDelta": disp_after - disp_before,
                              "turnDelta": turns_after - turns_before,
                              "evaluatorDelta": evals_after - evals_before,
                              "integrationDelta": integ_after - integ_before,
                              "cStarts": c_starts}

        # -- resume: builder never replays; C starts now
        mgr.resume(mid)
        st_r = json.loads((runs / "state.json").read_text("utf-8"))
        detail["timing"]["pausedMsAtResume"] = st_r.get("pausedMs")
        detail["timing"]["wallMsAtResume"] = st_r.get("wallElapsedMs")
        resume_at = time.time()
        deadline = time.monotonic() + MISSION_TIMEOUT
        state = {}
        while time.monotonic() < deadline:
            state = mgr.status(mid)["mission"]
            if state.get("state") in ("done", "failed", "blocked", "cancelled"):
                break
            time.sleep(POLL)
        ev.log(f"mission terminal: {state.get('state')} "
               f"(stopReason={state.get('stopReason')})")
        checks["mission-done"] = state.get("state") == "done"

        # builders: exactly ONE worker turn per unit A/B across the whole run
        builders_a = [c for c in ptap.in_cwd("/u0") if not c["read_only"]]
        builders_b = [c for c in ptap.in_cwd("/u1") if not c["read_only"]]
        checks["resume-no-worker-replay"] = (len(builders_a) == 1
                                             and len(builders_b) == 1)
        evals_a = [c for c in ptap.in_cwd("/u0") if c["read_only"]]
        evals_b = [c for c in ptap.in_cwd("/u1") if c["read_only"]]
        checks["resume-continues-at-evaluator"] = (len(evals_a) >= 1
                                                   and len(evals_b) >= 1)
        # C's real worker turn starts only after resume
        c_builders = [c for c in ptap.in_cwd("/u2") if not c["read_only"]]
        checks["C-started-after-resume"] = bool(
            c_builders and c_builders[0]["ts"] >= resume_at)
        detail["builders"] = {"A": len(builders_a), "B": len(builders_b),
                              "C": len(c_builders)}
        detail["evaluators"] = {"A": len(evals_a), "B": len(evals_b)}

        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        checks["resume-completes-all"] = (
            [u["state"] for u in plan["units"]] == ["integrated"] * 3)

        # timing contract: pause holds the wall budget
        tmg = detail["timing"]
        paused_delta = (tmg.get("pausedMsAtResume") or 0) \
            - (tmg.get("pausedMsAtQuiesce") or 0)
        wall_delta = (tmg.get("wallMsAtResume") or 0) \
            - (tmg.get("wallMsAtQuiesce") or 0)
        checks["pause-budget-contract"] = (paused_delta >= 8000
                                           and wall_delta <= 1500)
        detail["timing"]["pausedDeltaMs"] = paused_delta
        detail["timing"]["wallDeltaMs"] = wall_delta

        integ_branch = f"laomo/{mid}/integration"
        checks["source-isolation"] = (
            git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before
            and all(marker in git(repo, "show", f"{integ_branch}:{f}")
                    for f, marker in (("a.txt", "REAL-CODEX-E-A"),
                                      ("b.txt", "REAL-CODEX-E-B"),
                                      ("c.txt", "REAL-CODEX-E-C"))))
    finally:
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "E", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate E verdict: {result['verdict']} checks={checks}")
    return result


CANCEL_A = (
    "本轮内依次单独执行 10 次 shell 命令 sleep 5，每一次都必须等待它结束；"
    "不要输出任何任务标记块。全部结束后才创建 late-a.txt，"
    "内容恰好为一行：LATE-A。除此以外不要创建或修改任何文件，"
    "不要执行 git 命令。")
CANCEL_B = (
    "本轮内依次单独执行 10 次 shell 命令 sleep 5，每一次都必须等待它结束；"
    "不要输出任何任务标记块。全部结束后才创建 late-b.txt，"
    "内容恰好为一行：LATE-B。除此以外不要创建或修改任何文件，"
    "不要执行 git 命令。")
CANCEL_C = (
    "第一轮不要自己等待长命令，直接在回复末尾输出 LAOMO_JOB 标记块结束本轮，"
    "内容为：command=\"sleep 60; echo LATE-JOB > late-job.txt\"，"
    "reason=\"取消门禁长任务\"，expectedSeconds=90。不要写 cwd 字段。"
    "唤醒后创建 late-c.txt。全程不要执行 git 命令。")


class RpcTap:
    """Record every outbound JSON-RPC request (class-level wrap of
    RpcClient.request — installed before any process spawns). Distinguishes
    'the harness never sent turn/interrupt' from 'it sent and codex ignored'
    when a cancel fails."""

    def __init__(self, ev: Evidence) -> None:
        self.ev = ev
        self.calls: list[dict] = []
        import codex_adapter as ca
        original = ca.RpcClient.request

        def wrapped(self, method, params=None, timeout=60.0):
            rec = {"ts": round(time.time(), 3), "method": method,
                   "threadId": (params or {}).get("threadId"),
                   "turnId": (params or {}).get("turnId")}
            self_calls = RpcTap._active
            if self_calls is not None:
                self_calls.append(rec)
                try:
                    with (ev.root / "rpc.ndjson").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            return original(self, method, params, timeout)

        ca.RpcClient.request = wrapped  # plain function => binds as a method
        RpcTap._original = original
        RpcTap._active = self.calls

    def interrupts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "turn/interrupt"]

    def close(self) -> None:
        import codex_adapter as ca
        ca.RpcClient.request = RpcTap._original
        RpcTap._active = None


def gate_f(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate F — Cancel / Interrupt (expect first-run FAIL evidence)")
    # install the RPC tap at class level BEFORE any process exists
    rpc_tap = RpcTap(ev)
    repo = scratch / "fixture-f" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)

    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    try:
        created = mgr.create(
            "三个单元：A/B 长轮内工作，C 挂后台长任务",
            cwd=str(repo),
            acceptance_criteria=["仅在单元完成时产出各自文件"],
            options={"maxParallelWorkers": 3})
        mid = created["mission"]["id"]
        runs = repo / ".laomo" / "runs" / mid

        def unit(uid, index, title, desc):
            return {"id": uid, "index": index, "title": title,
                    "description": desc,
                    "acceptance": [title + " 产物存在"],
                    "dependencies": [], "state": "pending", "status": "pending",
                    "attempt": 0, "repairCount": 0, "conflictCount": 0,
                    "conflict": None,
                    "worktree": {"path": None, "branch": None,
                                 "baseSha": None, "headSha": None},
                    "jobId": None, "delta": None, "repairDirective": None,
                    "lastVerdict": None,
                    "worker": {"startedAt": None, "finishedAt": None},
                    "integration": None}
        store = mgr.store_for(mid)
        store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                         "units": [unit("a", 0, "单元A", CANCEL_A),
                                   unit("b", 1, "单元B", CANCEL_B),
                                   unit("c", 2, "单元C", CANCEL_C)]})
        store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        mgr.start(mid)

        def plan_states():
            try:
                pl = json.loads((runs / "plan.json").read_text("utf-8"))
                return {u["index"]: u["state"] for u in pl["units"]}
            except Exception:
                return {}

        def job_record():
            for jf in sorted((runs / "jobs").glob("*.json")):
                return json.loads(jf.read_text("utf-8"))
            return {}

        def pid_alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        # -- wait for the strongest cancel scene: A/B turns ACTIVE +
        #    C WAITING + C job alive
        deadline = time.monotonic() + 150
        cancel_at = None
        while time.monotonic() < deadline:
            t0 = tap.unit_turns("/u0")
            t1 = tap.unit_turns("/u1")
            a_active = bool(t0) and t0[0].get("endedAt") is None
            b_active = bool(t1) and t1[0].get("endedAt") is None
            states = plan_states()
            job = job_record()
            job_alive = bool(job.get("pid") and job.get("status") == "running"
                             and pid_alive(job["pid"]))
            if a_active and b_active and states.get(2) == "waiting" and job_alive:
                cancel_at = time.time()
                detail["precondition"] = {
                    "aActive": a_active, "bActive": b_active,
                    "cState": states.get(2),
                    "jobPid": job.get("pid"), "jobAlive": job_alive}
                break
            time.sleep(0.2)
        checks["precondition-two-active-turns"] = cancel_at is not None
        checks["precondition-job-alive"] = cancel_at is not None
        if cancel_at is None:
            ev.log("precondition never met — aborting gate")
            result = {"gate": "F", "checks": checks, "detail": detail,
                      "verdict": "FAIL"}
            ev.write("summary.json", result)
            return result
        ev.log(f"cancel scene reached (job pid={detail['precondition']['jobPid']})")

        def snapshot_events():
            if not (runs / "events.ndjson").is_file():
                return []
            return [json.loads(l) for l in
                    (runs / "events.ndjson").read_text("utf-8").splitlines()
                    if l.strip()]

        def fs_snapshot():
            out = {}
            for path in sorted(repo.rglob("*")):
                rel = str(path.relative_to(repo))
                if ".laomo" in rel:
                    continue
                if path.is_file():
                    try:
                        out[rel] = path.stat().st_mtime
                    except OSError:
                        pass
            return out

        ev_before = snapshot_events()
        fs_before = fs_snapshot()
        rpc_before = len(rpc_tap.calls)
        t0 = tap.unit_turns("/u0")
        t1 = tap.unit_turns("/u1")
        tap_counts_before = (len(tap.unit_turns("/u0")),
                             len(tap.unit_turns("/u1")),
                             len(tap.unit_turns("/u2")))
        cancel_called = time.time()
        mgr.cancel(mid)
        cancel_returned = time.time()
        detail["cancelLatencySec"] = round(cancel_returned - cancel_called, 3)
        ev.log(f"cancel() returned in {detail['cancelLatencySec']}s")

        # -- observe 10s strict window, then (if turns survive) an extended
        #    evidence window to catch zombie writes
        time.sleep(10.0)
        ev_after = snapshot_events()
        fs_after_10 = fs_snapshot()

        def count(evts, etype):
            return sum(1 for e in evts if e["type"] == etype)

        checks["cancel-state-durable"] = (
            json.loads((runs / "state.json").read_text("utf-8"))
            .get("state") == "cancelled")
        checks["no-post-cancel-progress"] = (
            count(ev_after, "dispatch") == count(ev_before, "dispatch")
            and count(ev_after, "integration") == count(ev_before, "integration")
            and count(ev_after, "verification") == count(ev_before, "verification"))
        late_files = ["late-a.txt", "late-b.txt", "late-job.txt", "late-c.txt"]

        def late_existing():
            found = [f for f in late_files
                     if any(p.name == f for p in repo.rglob(f))]
            return found

        checks["no-dead-writes"] = (not late_existing()
                                    and fs_after_10 == fs_before)

        # interrupts actually sent with REAL turn ids?
        sent = rpc_tap.interrupts()
        a_turn = t0[0] if t0 else {}
        b_turn = t1[0] if t1 else {}
        checks["interrupt-a-sent"] = any(
            i.get("threadId") == a_turn.get("threadId")
            and i.get("turnId") not in (None, "")
            for i in sent)
        checks["interrupt-b-sent"] = any(
            i.get("threadId") == b_turn.get("threadId")
            and i.get("turnId") not in (None, "")
            for i in sent)
        detail["interrupts"] = sent

        # turns' final status (wait up to 15s for interrupt to land)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            t0 = tap.unit_turns("/u0")
            t1 = tap.unit_turns("/u1")
            if t0 and t1 and t0[0].get("endedAt") and t1[0].get("endedAt"):
                break
            time.sleep(0.2)
        t0 = tap.unit_turns("/u0")
        t1 = tap.unit_turns("/u1")
        a_final = t0[0] if t0 else {}
        b_final = t1[0] if t1 else {}
        checks["turn-a-interrupted"] = a_final.get("status") == "interrupted"
        checks["turn-b-interrupted"] = b_final.get("status") == "interrupted"
        checks["interrupt-bounded"] = bool(
            a_final.get("endedAt") and b_final.get("endedAt")
            and max(a_final["endedAt"], b_final["endedAt"]) - cancel_at <= 10.0)
        detail["finalTurns"] = {"A": a_final, "B": b_final}

        # job terminated + process dead
        job = job_record()
        checks["job-terminated"] = (job.get("status") == "cancelled"
                                    and job.get("exitKind") == "terminated")
        checks["job-process-dead"] = bool(
            job.get("pid") and not pid_alive(job["pid"]))
        detail["jobAfter"] = {k: job.get(k) for k in
                              ("status", "exitKind", "exitCode", "pid")}

        # four layers of liveness
        with mgr._lock:
            pool = mgr._unit_threads.get(mid) or {}
            live_units = sum(1 for t in pool.values() if t.is_alive())
            runner = mgr._runners.get(mid)
            runner_alive = bool(runner and runner.is_alive())
        checks["unit-threads-zero"] = live_units == 0
        checks["mission-runner-zero"] = not runner_alive
        detail["liveness"] = {"unitThreads": live_units,
                              "runnerAlive": runner_alive}

        # extended zombie-evidence window if A/B turns still alive
        if not (a_final.get("endedAt") and b_final.get("endedAt")):
            ev.log("TURNS STILL ACTIVE after cancel — extended evidence window")
            zombie_deadline = time.monotonic() + 90
            while time.monotonic() < zombie_deadline:
                t0 = tap.unit_turns("/u0")
                t1 = tap.unit_turns("/u1")
                if t0[0].get("endedAt") and t1[0].get("endedAt"):
                    break
                time.sleep(1.0)
            t0 = tap.unit_turns("/u0")
            t1 = tap.unit_turns("/u1")
            detail["zombieEvidence"] = {
                "aEndedAt": t0[0].get("endedAt"),
                "aStatus": t0[0].get("status"),
                "bEndedAt": t1[0].get("endedAt"),
                "bStatus": t1[0].get("status"),
                "lateFilesAtEnd": late_existing(),
                "note": "cancel 返回后真实 codex turn 继续运行直至自然结束",
            }
            ev.log(f"zombie window: {detail['zombieEvidence']}")

        checks["source-isolation"] = (
            git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before)
    finally:
        rpc_tap.close()
        adapter.shutdown()

    verdict = all(checks.values())
    result = {"gate": "F", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate F verdict: {result['verdict']} checks={checks}")
    return result


G_A = ("创建 a.txt，内容恰好为一行：REAL-G-A。"
       "除此以外不要创建或修改任何文件，不要执行 git 命令。")
G_B = ("第一步：创建 progress-b.txt，内容恰好为一行：B-PRE-CRASH（若已存在"
       "且内容相同则不要重建）。第二步：依次单独执行 8 次 shell 命令 sleep 5，"
       "每次必须等待结束，不要输出任何任务标记块。第三步：创建 b.txt，"
       "内容恰好为一行：B-RECOVERED。除此以外不要修改任何文件，"
       "不要执行 git 命令。")
G_C = ("第一步：创建 progress-c.txt，内容恰好为一行：C-PRE-CRASH（若已存在"
       "且内容相同则不要重建）。第二步：依次单独执行 8 次 shell 命令 sleep 5，"
       "每次必须等待结束，不要输出任何任务标记块。第三步：创建 c.txt，"
       "内容恰好为一行：C-RECOVERED。除此以外不要修改任何文件，"
       "不要执行 git 命令。")


def g_plan(store, mid):
    def unit(uid, index, title, desc, deps=None):
        return {"id": uid, "index": index, "title": title,
                "description": desc, "acceptance": [title + " 产物正确"],
                "dependencies": deps or [], "state": "pending",
                "status": "pending", "attempt": 0, "repairCount": 0,
                "conflictCount": 0, "conflict": None,
                "worktree": {"path": None, "branch": None,
                             "baseSha": None, "headSha": None},
                "jobId": None, "delta": None, "repairDirective": None,
                "lastVerdict": None,
                "worker": {"startedAt": None, "finishedAt": None},
                "integration": None}
    store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                     "units": [unit("a", 0, "单元A", G_A),
                               unit("b", 1, "单元B", G_B, deps=["a"]),
                               unit("c", 2, "单元C", G_C, deps=["a"])]})
    store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                      "noProgress": 0, "progressSignature": "",
                      "tokensUsed": 0, "wallElapsedMs": 0,
                      "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                      "phaseStartedAt": 0})


def child_g_phase1(scratch: Path) -> int:
    """The REAL gateway process #1: holds MissionManager + app-server #1.
    Publishes the crash scene, then idles until the supervisor SIGKILLs it
    (no cancel/shutdown path — that would not be a crash)."""
    repo = scratch / "fixture-g" / "repo"
    gdir = scratch / ".laomo" / "gates" / "p12" / "G"
    ev = Evidence(gdir / "phase1")
    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    created = mgr.create("A 先完成；B/C 依赖 A 并行长工作",
                         cwd=str(repo),
                         acceptance_criteria=["三个产物文件内容正确"],
                         options={"maxParallelWorkers": 2},
                         verification={
                             "requiredFiles": ["a.txt", "b.txt", "c.txt"],
                             "commands": ["grep -q REAL-G-A a.txt",
                                          "grep -q B-RECOVERED b.txt",
                                          "grep -q C-RECOVERED c.txt"]})
    mid = created["mission"]["id"]
    g_plan(mgr.store_for(mid), mid)
    proc = adapter._ensure_process()
    (gdir / "phase1-pids.json").write_text(json.dumps({
        "gatewayPid": os.getpid(),
        "appServerPid": proc.proc.pid if proc.proc else None,
    }), "utf-8")
    runs = repo / ".laomo" / "runs" / mid
    mgr.start(mid)

    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            plan = json.loads((runs / "plan.json").read_text("utf-8"))
            states = {u["index"]: u["state"] for u in plan["units"]}
            wts = {u["index"]: (u.get("worktree") or {}).get("path")
                   for u in plan["units"]}
        except Exception:
            time.sleep(0.3)
            continue
        t1 = tap.unit_turns("/u1")
        t2 = tap.unit_turns("/u2")
        b_active = bool(t1) and t1[0].get("endedAt") is None
        c_active = bool(t2) and t2[0].get("endedAt") is None
        pb = Path(str(wts.get(1))) / "progress-b.txt" if wts.get(1) else None
        pc = Path(str(wts.get(2))) / "progress-c.txt" if wts.get(2) else None
        ok = (states.get(0) == "integrated" and b_active and c_active
              and pb is not None and pb.is_file()
              and pc is not None and pc.is_file()
              and not (Path(str(wts.get(1))) / "b.txt").is_file()
              and not (Path(str(wts.get(2))) / "c.txt").is_file())
        if ok:
            (gdir / "crash-scene.json").write_text(json.dumps({
                "mid": mid,
                "gatewayPid": os.getpid(),
                "appServerPid": proc.proc.pid if proc.proc else None,
                "sceneAt": time.time(),
                "aState": states.get(0),
                "bTurn": t1[0], "cTurn": t2[0],
                "bWorktree": wts.get(1), "cWorktree": wts.get(2),
            }, ensure_ascii=False), "utf-8")
            ev.log("crash scene published — waiting for SIGKILL")
            while True:  # no graceful exit path: only SIGKILL ends phase1
                time.sleep(1.0)
        time.sleep(0.3)
    ev.log("phase1 timed out before crash scene")
    return 1


def child_g_phase2(scratch: Path, mid: str) -> int:
    """The REAL gateway process #2: fresh adapter/app-server, recover()."""
    repo = scratch / ".laomo" / "gates" / "p12" / "G" / ".." / ".." / ".." \
        / "fixture-g" / "repo"
    repo = scratch / "fixture-g" / "repo"
    gdir = scratch / ".laomo" / "gates" / "p12" / "G"
    ev = Evidence(gdir / "phase2")
    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)
    recover_at = time.time()
    resumed = mgr.recover()
    proc = adapter._ensure_process()
    runs = repo / ".laomo" / "runs" / mid
    # sample the durable worktrees right after recover (pre-continuation)
    preserved = {}
    try:
        plan = json.loads((runs / "plan.json").read_text("utf-8"))
        for u in plan["units"]:
            wt = (u.get("worktree") or {}).get("path")
            if wt:
                for name in ("progress-b.txt", "progress-c.txt"):
                    f = Path(wt) / name
                    if f.is_file():
                        preserved[name] = f.read_text("utf-8").strip()
    except Exception:
        pass
    deadline = time.monotonic() + MISSION_TIMEOUT
    state = {}
    while time.monotonic() < deadline:
        state = mgr.status(mid)["mission"]
        if state.get("state") in ("done", "failed", "blocked", "cancelled"):
            break
        time.sleep(POLL)
    t1 = tap.unit_turns("/u1")
    t2 = tap.unit_turns("/u2")
    result = {
        "recoverAt": recover_at,
        "resumed": resumed,
        "appServerPid": proc.proc.pid if proc.proc else None,
        "gatewayPid": os.getpid(),
        "finalState": state.get("state"),
        "stopReason": state.get("stopReason"),
        "bFirstTurn": t1[0] if t1 else None,
        "cFirstTurn": t2[0] if t2 else None,
        "bWorkerCwds": [c["cwd"] for c in ptap.in_cwd("/u1")
                        if not c["read_only"]],
        "cWorkerCwds": [c["cwd"] for c in ptap.in_cwd("/u2")
                        if not c["read_only"]],
        "aWorkerTurnsPhase2": len([c for c in ptap.in_cwd("/u0")
                                   if not c["read_only"]]),
        "preservedAtRecover": preserved,
    }
    (gdir / "phase2-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    ev.log(f"phase2 final: {state.get('state')}")
    adapter.shutdown()
    return 0


def gate_g(ev: Evidence, scratch: Path) -> dict:
    import signal
    ev.log("Gate G — Gateway Hard Crash / Recover (supervisor + 2 real gateways)")
    repo = scratch / "fixture-g" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)
    gdir = ev.root
    checks: dict[str, bool] = {}
    detail: dict = {}
    try:
        # -- phase 1: real gateway holding the mission
        p1 = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--gate-g-child", "phase1", str(scratch)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        scene_path = gdir / "crash-scene.json"
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline and not scene_path.is_file():
            if p1.poll() is not None:
                break
            time.sleep(0.5)
        assert scene_path.is_file(), "phase1 never published the crash scene"
        scene = json.loads(scene_path.read_text("utf-8"))
        mid = scene["mid"]
        ev.log(f"crash scene ready (gateway={scene['gatewayPid']} "
               f"appserver={scene['appServerPid']})")
        checks["precondition-a-integrated"] = scene.get("aState") == "integrated"
        checks["precondition-two-active-turns"] = bool(
            scene["bTurn"].get("startedAt") and not scene["bTurn"].get("endedAt")
            and scene["cTurn"].get("startedAt")
            and not scene["cTurn"].get("endedAt"))
        wt_b, wt_c = Path(scene["bWorktree"]), Path(scene["cWorktree"])
        checks["precondition-partial-work-durable"] = (
            (wt_b / "progress-b.txt").read_text("utf-8").strip() == "B-PRE-CRASH"
            and (wt_c / "progress-c.txt").read_text("utf-8").strip()
            == "C-PRE-CRASH")

        # crash-gap filesystem baseline
        def snap_fs():
            out = {}
            for path in sorted(repo.rglob("*")):
                rel = str(path.relative_to(repo))
                if path.is_file():
                    try:
                        out[rel] = round(path.stat().st_mtime, 3)
                    except OSError:
                        pass
            return out
        fs_pre = snap_fs()

        # -- THE KILL: no cancel, no shutdown, no job termination
        os.kill(p1.pid, signal.SIGKILL)
        p1.wait()
        kill_ts = time.time()
        ev.log(f"SIGKILL sent to gateway #1 ({p1.pid})")

        time.sleep(5.0)  # crash gap: no control plane exists
        checks["gateway-sigkill-real"] = p1.returncode == -signal.SIGKILL
        app_pid = scene.get("appServerPid")
        app_dead = True
        if app_pid:
            try:
                os.kill(app_pid, 0)
                app_dead = False
            except OSError:
                app_dead = True
        checks["old-appserver-dead"] = app_dead
        if not app_dead:
            ev.log(f"PRODUCT ISSUE: app-server {app_pid} still alive after "
                   f"gateway SIGKILL (evidence kept, not killing it)")
        fs_post = snap_fs()
        late = [n for n in ("b.txt", "c.txt")
                if (wt_b / n).is_file() or (wt_c / n).is_file()]
        checks["crash-gap-no-zombie-writes"] = (
            not late and fs_post == fs_pre)
        detail["crashGap"] = {"lateFiles": late,
                              "appServerDead": app_dead}

        # -- honest durable state
        runs = repo / ".laomo" / "runs" / mid
        try:
            mj = json.loads((runs / "mission.json").read_text("utf-8"))
            sj = json.loads((runs / "state.json").read_text("utf-8"))
            pj = json.loads((runs / "plan.json").read_text("utf-8"))
            json_ok = True
        except Exception:
            mj = sj = pj = {}
            json_ok = False
        states = {u["index"]: u["state"] for u in pj.get("units", [])}
        checks["state-files-valid"] = json_ok
        checks["state-honest-after-crash"] = (
            sj.get("state") == "running" and states.get(0) == "integrated"
            and states.get(1) == "running" and states.get(2) == "running")
        detail["postCrash"] = {"mission": sj.get("state"),
                               "units": states,
                               "aIntegratedSha": None}

        # -- phase 2: a genuinely NEW control plane, recover()
        p2 = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--gate-g-child", "phase2", str(scratch), mid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result_path = gdir / "phase2-result.json"
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not result_path.is_file():
            if p2.poll() is not None:
                break
            time.sleep(0.5)
        assert result_path.is_file(), "phase2 never produced a result"
        res = json.loads(result_path.read_text("utf-8"))
        checks["recover-found-mission"] = mid in (res.get("resumed") or [])
        checks["new-control-plane"] = res.get("gatewayPid") != scene["gatewayPid"]
        checks["new-appserver"] = (res.get("appServerPid")
                                   != scene.get("appServerPid"))
        checks["mission-done"] = res.get("finalState") == "done"

        # A never replayed
        events = [json.loads(l) for l in
                  (runs / "events.ndjson").read_text("utf-8").splitlines()
                  if l.strip()]
        a_dispatches = sum(1 for e in events if e["type"] == "dispatch"
                           and isinstance(e.get("detail"), dict)
                           and e["detail"].get("unit") == 0)
        a_integrations = sum(1 for e in events if e["type"] == "integration"
                             and isinstance(e.get("detail"), dict)
                             and e["detail"].get("unit") == 0
                             and e["detail"].get("phase") == "integrated")
        checks["a-not-replayed"] = (a_dispatches == 1 and a_integrations == 1
                                    and res.get("aWorkerTurnsPhase2") == 0)
        detail["aReplay"] = {"dispatches": a_dispatches,
                             "integrations": a_integrations,
                             "phase2WorkerTurns": res.get("aWorkerTurnsPhase2")}

        # B/C: brand-new ephemeral contexts on the SAME durable worktrees
        b_new = res.get("bFirstTurn") or {}
        c_new = res.get("cFirstTurn") or {}
        checks["b-new-turn"] = bool(
            b_new.get("threadId") and scene["bTurn"].get("threadId")
            and b_new["threadId"] != scene["bTurn"]["threadId"])
        checks["c-new-turn"] = bool(
            c_new.get("threadId") and scene["cTurn"].get("threadId")
            and c_new["threadId"] != scene["cTurn"]["threadId"])
        b_cwds = res.get("bWorkerCwds") or []
        c_cwds = res.get("cWorkerCwds") or []
        checks["same-durable-worktrees"] = bool(
            b_cwds and c_cwds and b_cwds[0] == scene["bWorktree"]
            and c_cwds[0] == scene["cWorktree"])
        preserved = res.get("preservedAtRecover") or {}
        checks["partial-work-preserved"] = (
            preserved.get("progress-b.txt") == "B-PRE-CRASH"
            and preserved.get("progress-c.txt") == "C-PRE-CRASH")

        plan_final = json.loads((runs / "plan.json").read_text("utf-8"))
        checks["all-units-integrated"] = (
            [u["state"] for u in plan_final["units"]] == ["integrated"] * 3)

        integ_branch = f"laomo/{mid}/integration"
        def show(f):
            try:
                return git(repo, "show", f"{integ_branch}:{f}")
            except AssertionError:
                return ""
        checks["source-isolation"] = (
            git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before
            and "REAL-G-A" in show("a.txt")
            and "B-RECOVERED" in show("b.txt")
            and "C-RECOVERED" in show("c.txt"))

        # recovery latency (factual, no SLA)
        if b_new.get("startedAt") and res.get("recoverAt"):
            detail["recoveryLatencySec"] = round(
                b_new["startedAt"] - res["recoverAt"], 3)
        detail["phase2"] = res
        detail["scene"] = scene
    finally:
        pass

    verdict = all(checks.values())
    result = {"gate": "G", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate G verdict: {result['verdict']} checks={checks}")
    return result


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


H_UNIT = ("创建 h.txt，内容恰好为一行：REAL-H-INTEGRATION。"
          "除此以外不要创建或修改任何文件，不要执行 git 命令。")


def gate_h_child(phase: str, scratch_s: str, evdir_s: str) -> int:
    """Gate H child: phase1 freezes the process EXACTLY in the integration
    WAL window (real merge landed, plan.json still integrating/prepared) via
    a driver-side wrap of WorktreeManager.integrate; phase2 is the new
    control plane calling recover() only."""
    scratch = Path(scratch_s)
    ev = Evidence(Path(evdir_s))
    repo = scratch / "fixture-h" / "repo"
    adapter = CodexRuntimeAdapter(bin_path=GATE_BIN, default_cwd=str(repo),
                                  debug_log=ev.log)
    tap = TurnTap(ev, adapter)
    ptap = PromptTap(ev, adapter)
    mgr = MissionManager(adapter, repo)

    def appserver_pid():
        proc = adapter.process
        return proc.proc.pid if proc and proc.proc else None

    if phase == "phase1":
        from mission import worktree as wt_mod
        original = wt_mod.WorktreeManager.integrate
        wedged = threading.Event()

        def wedging(self, index, title=None, branch=None):
            res = original(self, index, title, branch)
            if res.get("ok") and not wedged.is_set():
                wedged.set()
                plan = self.store.load_plan()
                unit = next(u for u in plan["units"] if u["index"] == index)
                tx = unit.get("integration") or {}

                def g(*args):
                    return subprocess.run(
                        ["git", "-C", str(self.workspace), *args],
                        capture_output=True, text=True).stdout.strip()

                integ_head = g("rev-parse", self.integration_branch)
                ancestor = subprocess.run(
                    ["git", "-C", str(self.workspace), "merge-base",
                     "--is-ancestor", str(tx.get("unitHead")),
                     self.integration_branch]).returncode == 0
                mission = json.loads(
                    (self.store.root / "mission.json").read_text("utf-8"))
                base = mission.get("baseSha")
                events = [json.loads(l) for l in
                          (self.store.root / "events.ndjson")
                          .read_text("utf-8").splitlines() if l.strip()]
                scene = {
                    "gatewayPid": os.getpid(), "appserverPid": appserver_pid(),
                    "missionId": self.store.root.name,
                    "unitState": unit.get("state"),
                    "txPhase": tx.get("phase"),
                    "unitHead": tx.get("unitHead"), "dirty": tx.get("dirty"),
                    "unitHeadAtCrash": (
                        subprocess.run(["git", "-C", str(info_path), "rev-parse",
                                        "HEAD"], capture_output=True,
                                       text=True).stdout.strip()
                        if (info_path := str((unit.get("worktree") or {})
                                             .get("path") or "")) else None),
                    "lastVerdict": unit.get("lastVerdict"),
                    "integrationBranch": self.integration_branch,
                    "integrationHeadAtCrash": integ_head,
                    "unitHeadIsAncestor": ancestor,
                    "hTxt": g("show", f"{self.integration_branch}:h.txt"),
                    "integratedEvents": sum(
                        1 for e in events if e["type"] == "integration"
                        and isinstance(e.get("detail"), dict)
                        and e["detail"].get("phase") == "integrated"),
                    "commitCount": (int(g("rev-list", "--count",
                                           f"{base}..{integ_head}"))
                                    if base else None),
                    "workerTurns": tap.unit_turns("/u0"),
                    "evaluatorTurnCount": len(
                        [c for c in ptap.in_cwd("/u0") if c["read_only"]]),
                    "workerTurnCount": len(
                        [c for c in ptap.in_cwd("/u0") if not c["read_only"]]),
                    "ts": time.time(),
                }
                (ev.root / "wedge-scene.json").write_text(
                    json.dumps(scene, ensure_ascii=False, indent=1), "utf-8")
                ev.log("WEDGE: merge landed; frozen before plan persistence "
                       "(awaiting SIGKILL)")
                while True:
                    time.sleep(1.0)  # supervisor SIGKILLs us here
            return res

        wt_mod.WorktreeManager.integrate = wedging
        created = mgr.create(
            "单单元：创建 h.txt 并完成集成",
            cwd=str(repo), acceptance_criteria=["h.txt 内容正确"],
            verification={"requiredFiles": ["h.txt"],
                          "commands": ["grep -q REAL-H-INTEGRATION h.txt"]})
        mid = created["mission"]["id"]
        store = mgr.store_for(mid)
        h_unit = {"id": "h", "index": 0, "title": "单元H",
                  "description": H_UNIT,
                  "acceptance": ["h.txt 存在且内容包含 REAL-H-INTEGRATION"],
                  "dependencies": [], "state": "pending", "status": "pending",
                  "attempt": 0, "repairCount": 0, "conflictCount": 0,
                  "conflict": None,
                  "worktree": {"path": None, "branch": None,
                               "baseSha": None, "headSha": None},
                  "jobId": None, "delta": None, "repairDirective": None,
                  "lastVerdict": None,
                  "worker": {"startedAt": None, "finishedAt": None},
                  "integration": None}
        store.save_plan({"version": 2, "replans": 0, "gitIntegration": True,
                         "units": [h_unit]})
        store.save_state({"state": "running", "cycles": 0, "currentUnit": 0,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        mgr.start(mid)
        deadline = time.monotonic() + MISSION_TIMEOUT
        while time.monotonic() < deadline:
            st = mgr.status(mid)["mission"]
            if st.get("state") in ("done", "failed", "blocked", "cancelled"):
                ev.log(f"phase1 terminal WITHOUT wedge: {st.get('state')}")
                return 1
            time.sleep(POLL)
        return 1

    # phase2: recover() only — let the official recovery chain find the wedge
    t_recover = time.time()
    resumed = mgr.recover()
    mid = resumed[0] if resumed else ""
    state = {}
    deadline = time.monotonic() + MISSION_TIMEOUT
    while mid and time.monotonic() < deadline:
        state = mgr.status(mid)["mission"]
        if state.get("state") in ("done", "failed", "blocked", "cancelled"):
            break
        time.sleep(POLL)
    (ev.root / "phase2-result.json").write_text(json.dumps({
        "gatewayPid": os.getpid(), "appserverPid": appserver_pid(),
        "resumed": resumed, "missionId": mid,
        "finalState": state.get("state"), "recoverAt": t_recover,
        "u0WorkerTurns": [c for c in ptap.in_cwd("/u0") if not c["read_only"]],
        "u0EvaluatorTurns": [c for c in ptap.in_cwd("/u0") if c["read_only"]],
    }, ensure_ascii=False, indent=1), "utf-8")
    adapter.shutdown()
    ev.close()
    return 0 if state.get("state") == "done" else 1


def gate_h(ev: Evidence, scratch: Path) -> dict:
    ev.log("Gate H — Integration WAL Crash Reconcile (freeze in the "
           "merge-landed / plan-not-persisted window)")
    repo = scratch / "fixture-h" / "repo"
    base_sha = init_fixture(repo)
    before = porcelain(repo)
    checks: dict[str, bool] = {}
    detail: dict = {}
    child1 = None
    try:
        child1 = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--gate-h-child", "phase1", str(scratch), str(ev.root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        scene = {}
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            sp = ev.root / "wedge-scene.json"
            if sp.is_file():
                try:
                    scene = json.loads(sp.read_text("utf-8"))
                    break
                except Exception:
                    pass
            time.sleep(0.2)
        checks["real-worker-pass"] = bool(
            scene and scene.get("workerTurnCount", 0) >= 1
            and scene.get("evaluatorTurnCount", 0) >= 1
            and scene.get("lastVerdict") == "PASS")
        checks["wal-prepared"] = bool(
            scene and scene.get("unitState") == "integrating"
            and scene.get("txPhase") == "prepared")
        checks["tx-honest"] = bool(
            scene and scene.get("unitHead")
            and isinstance(scene.get("dirty"), bool)
            and scene.get("unitHeadAtCrash"))
        ancestor_recheck = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor",
             str(scene.get("unitHead")), scene.get("integrationBranch")]
        ).returncode == 0 if scene else False
        checks["git-merge-landed"] = bool(
            scene.get("unitHeadIsAncestor") and ancestor_recheck)
        checks["artifact-on-integration"] = bool(
            scene and "REAL-H-INTEGRATION" in str(scene.get("hTxt")))
        checks["plan-not-yet-integrated"] = bool(
            scene and scene.get("integratedEvents") == 0
            and scene.get("unitState") != "integrated")
        detail["wedgeScene"] = {k: scene.get(k) for k in
                                ("gatewayPid", "appserverPid", "missionId",
                                 "unitState", "txPhase", "unitHead", "dirty",
                                 "integrationHeadAtCrash", "commitCount")}
        if not scene:
            raise AssertionError("wedge scene never appeared")
        mid = scene["missionId"]
        runs = repo / ".laomo" / "runs" / mid
        ev.log(f"wedge scene ready (mid={mid}); SIGKILL gateway "
               f"{scene['gatewayPid']}")

        plan_pre = json.loads((runs / "plan.json").read_text("utf-8"))
        os.kill(child1.pid, signal.SIGKILL)
        child1.wait()
        checks["gateway-sigkill-real"] = not pid_alive(child1.pid)

        time.sleep(4.0)  # crash gap
        old_app = scene.get("appserverPid")
        plan_post = json.loads((runs / "plan.json").read_text("utf-8"))
        u0_post = plan_post["units"][0]
        integ_head_gap = git(repo, "rev-parse", scene["integrationBranch"])
        checks["crash-state-preserved"] = bool(
            old_app and not pid_alive(old_app)
            and u0_post.get("state") == "integrating"
            and (u0_post.get("integration") or {}).get("phase") == "prepared")
        checks["integration-head-stable"] = (
            integ_head_gap == scene["integrationHeadAtCrash"])
        detail["gap"] = {"appserverDead": not pid_alive(old_app),
                         "integrationHead": integ_head_gap}

        child2 = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__),
             "--gate-h-child", "phase2", str(scratch), str(ev.root)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = child2.wait(timeout=MISSION_TIMEOUT + 120)
        p2 = json.loads((ev.root / "phase2-result.json").read_text("utf-8"))
        checks["recover-found-mission"] = mid in (p2.get("resumed") or [])
        detail["phase2"] = {k: p2.get(k) for k in
                            ("gatewayPid", "appserverPid", "resumed",
                             "finalState")}

        events = [json.loads(l) for l in
                  (runs / "events.ndjson").read_text("utf-8").splitlines()
                  if l.strip()]
        adoption = next((e for e in events
                         if e["type"] == "integration"
                         and isinstance(e.get("detail"), dict)
                         and e["detail"].get("phase") == "integrated"
                         and e["detail"].get("reconciled")), None)
        provable = (scene.get("unitHead") if scene.get("dirty") is False
                    else scene.get("unitHeadAtCrash"))
        checks["reconciled-adoption-event"] = bool(
            adoption
            and adoption["detail"].get("alreadyMerged") == provable
            and adoption["detail"].get("headSha")
            == scene.get("integrationHeadAtCrash"))
        detail["adoptionEvent"] = adoption.get("detail") if adoption else None

        final_head = git(repo, "rev-parse", scene["integrationBranch"])
        final_count = int(git(repo, "rev-list", "--count",
                              f"{base_sha}..{final_head}"))
        checks["integration-head-not-duplicated"] = (
            final_head == scene["integrationHeadAtCrash"]
            and final_count == scene.get("commitCount"))
        checks["worker-not-replayed"] = len(p2.get("u0WorkerTurns") or []) == 0
        checks["evaluator-not-replayed"] = len(p2.get("u0EvaluatorTurns") or []) == 0

        plan_final = json.loads((runs / "plan.json").read_text("utf-8"))
        u0_final = plan_final["units"][0]
        checks["unit-integrated-cleaned"] = (
            u0_final.get("state") == "integrated"
            and (u0_final.get("integration") or {}).get("phase") == "cleaned")
        checks["source-isolation"] = (
            p2.get("finalState") == "done"
            and git(repo, "rev-parse", "HEAD") == base_sha
            and porcelain(repo) == before
            and not (repo / "h.txt").exists())
        detail["final"] = {"head": final_head, "commitCount": final_count,
                           "child2Rc": rc}
    finally:
        if child1 and child1.poll() is None:
            child1.kill()

    verdict = all(checks.values())
    result = {"gate": "H", "checks": checks, "detail": detail,
              "verdict": "PASS" if verdict else "FAIL"}
    ev.write("summary.json", result)
    ev.log(f"Gate H verdict: {result['verdict']} checks={checks}")
    return result


# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- registry


def gate_ev(scratch: Path, name: str) -> Evidence:
    return Evidence(scratch / ".laomo" / "gates" / "p12" / name)


GATES = {
    "A": lambda ev, scratch: gate_a(ev, scratch),
    "B": lambda ev, scratch: gate_b(ev, scratch),
    "C": lambda ev, scratch: gate_c(ev, scratch),
    "D": lambda ev, scratch: gate_d(ev, scratch),
    "E": lambda ev, scratch: gate_e(ev, scratch),
    "F": lambda ev, scratch: gate_f(ev, scratch),
    "G": lambda ev, scratch: gate_g(ev, scratch),
    "H": lambda ev, scratch: gate_h(ev, scratch),
}


def report(results: list[dict]) -> None:
    print("\nP1.2 Real Codex Gates")
    for r in results:
        print(f"Gate {r['gate']}: {r['verdict']}  checks={r['checks']}")
    print(f"\nOverall: {'ALL PASS' if all(x['verdict'] == 'PASS' for x in results) else 'FAIL'}")


def main() -> int:
    if len(sys.argv) >= 5 and sys.argv[1] == "--gate-h-child":
        return gate_h_child(sys.argv[2], sys.argv[3], sys.argv[4])
    if len(sys.argv) >= 4 and sys.argv[1] == "--gate-g-child":
        phase = sys.argv[2]
        scratch = Path(sys.argv[3]).resolve()
        if phase == "phase1":
            return child_g_phase1(scratch)
        if phase == "phase2" and len(sys.argv) >= 5:
            return child_g_phase2(scratch, sys.argv[4])
        return 2
    if len(sys.argv) < 2 or sys.argv[1] not in (*GATES, "all"):
        print(__doc__)
        return 2
    scratch = Path(sys.argv[2] if len(sys.argv) > 2
                   else f"/tmp/laomo-p12-gate-{sys.argv[1]}").resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    names = list(GATES) if sys.argv[1] == "all" else [sys.argv[1]]
    results = []
    for name in names:
        ev = gate_ev(scratch, name)
        try:
            results.append(GATES[name](ev, scratch))
        finally:
            ev.close()
    report(results)
    return 0 if all(r["verdict"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
