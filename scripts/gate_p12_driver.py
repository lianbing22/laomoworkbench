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
        self._fh.write(line + "\n")
        self._fh.flush()

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


# ---------------------------------------------------------------- registry


def gate_ev(scratch: Path, name: str) -> Evidence:
    return Evidence(scratch / ".laomo" / "gates" / "p12" / name)


GATES = {
    "A": lambda ev, scratch: gate_a(ev, scratch),
}


def report(results: list[dict]) -> None:
    print("\nP1.2 Real Codex Gates")
    for r in results:
        print(f"Gate {r['gate']}: {r['verdict']}  checks={r['checks']}")
    print(f"\nOverall: {'ALL PASS' if all(x['verdict'] == 'PASS' for x in results) else 'FAIL'}")


def main() -> int:
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
