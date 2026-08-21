"""Durable Mission Loop for LaoMo Workbench (P0.6).

The workbench Control Plane owns the mission state machine; Codex threads are
stateless Workers/Evaluators. Every transition is persisted under
.laomo/runs/<id>/ and crash-resumable. See docs/mission-contract.md for the
three-party contract (backend / frontend / tests).

Design invariants:
- no model-side polling: long commands become BackgroundJobs owned by the
  control plane; the worker turn ENDS and is woken later with a compact delta
- default-fail: an unparseable/absent evaluator verdict is never a PASS
- the builder can never declare the mission DONE; only three conditions do:
  all units passed + final regression PASS + final evaluator PASS
- stop discipline: repair cap, no-progress cap, cycle cap, wall-clock cap
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

RUNS_DIRNAME = ".laomo/runs"
TERMINAL_STATES = {"done", "failed", "cancelled", "blocked"}
ACTIVE_STATES = {"planning", "running", "waiting", "evaluating", "repairing",
                 "replanning", "verification", "verifying", "paused"}
JOB_STATES = {"running", "completed", "failed", "cancelled", "orphaned"}
ALL_PHASES = ACTIVE_STATES | TERMINAL_STATES | {"draft"}

DEFAULT_STOP_POLICY = {
    "maxRepairPerTask": 3,
    "maxNoProgressCycles": 2,
    "maxMissionCycles": 40,
    "maxWallTimeSec": 14400,
}

WORKER_TURN_TIMEOUT = 1800     # idle-tolerant: legit build turns run long
EVALUATOR_TURN_TIMEOUT = 600   # 10 min
JOB_POLL_INTERVAL = 2.0
JOB_WAKE_GRACE = 300           # seconds past expectedWakeAt before forced wake
JOB_TERMINATE_GRACE = 6.0      # SIGTERM -> SIGKILL window for managed jobs
VERIFY_CMD_TIMEOUT = 120       # per-command timeout in the machine gate
VERIFY_TAIL = 2000             # characters kept per stdout/stderr tail


class MissionError(Exception):
    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# --- marker parsing ------------------------------------------------------------

_JOB_RE = re.compile(r"<<<LAOMO_JOB\s*(\{.*?\})\s*LAOMO_JOB>>>", re.S)
_VERDICT_RE = re.compile(r"<<<LAOMO_VERDICT\s*(\{.*?\})\s*LAOMO_VERDICT>>>", re.S)
_PLAN_RE = re.compile(r"<<<LAOMO_PLAN\s*(\[.*?\]|\{.*?\})\s*LAOMO_PLAN>>>", re.S)
_HANDOFF_RE = re.compile(r"HANDOFF:\s*(.+?)(?:\n\n|\Z)", re.S)


def parse_json_marker(text: str, regex: re.Pattern) -> dict[str, Any] | list[Any] | None:
    match = regex.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


# --- MissionStore ----------------------------------------------------------------


class MissionStore:
    """All durable state for one mission, under .laomo/runs/<id>/."""

    def __init__(self, run_root: Path) -> None:
        self.root = run_root
        self.mission_file = run_root / "mission.json"
        self.state_file = run_root / "state.json"
        self.plan_file = run_root / "plan.json"
        self.events_file = run_root / "events.ndjson"
        self.progress_file = run_root / "progress.md"
        self.handoff_file = run_root / "handoff.md"
        self.checkpoints_dir = run_root / "checkpoints"
        self.evidence_dir = run_root / "evidence"
        self.verdicts_dir = run_root / "verdicts"
        self.repairs_dir = run_root / "repairs"
        self.jobs_dir = run_root / "jobs"
        self.verification_dir = run_root / "verification"

    def ensure_dirs(self) -> None:
        for d in (self.checkpoints_dir, self.evidence_dir, self.verdicts_dir,
                  self.repairs_dir, self.jobs_dir, self.verification_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- mission (immutable) --
    def save_mission(self, data: dict[str, Any]) -> None:
        _atomic_write(self.mission_file, json.dumps(data, ensure_ascii=False, indent=1))

    def load_mission(self) -> dict[str, Any]:
        return _load_json(self.mission_file, {}) or {}

    # -- state --
    def load_state(self) -> dict[str, Any]:
        return _load_json(self.state_file, {}) or {}

    def save_state(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = _now_ms()
        _atomic_write(self.state_file, json.dumps(state, ensure_ascii=False, indent=1))

    # -- plan --
    def load_plan(self) -> dict[str, Any]:
        return _load_json(self.plan_file, {"units": [], "replans": 0}) or {"units": [], "replans": 0}

    def save_plan(self, plan: dict[str, Any]) -> None:
        _atomic_write(self.plan_file, json.dumps(plan, ensure_ascii=False, indent=1))

    # -- events --
    def event(self, kind: str, detail: Any = None) -> None:
        entry = {"ts": _now_ms(), "type": kind, "detail": detail}
        with open(self.events_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def events_tail(self, count: int = 40) -> list[dict[str, Any]]:
        try:
            lines = self.events_file.read_text("utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-count:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- artifacts --
    def write_checkpoint(self, name: str, text: str) -> None:
        _atomic_write(self.checkpoints_dir / f"{name}.md", text)

    def write_verdict(self, name: str, verdict: dict[str, Any]) -> None:
        _atomic_write(self.verdicts_dir / f"{name}.json", json.dumps(verdict, ensure_ascii=False, indent=1))

    def write_repair(self, name: str, directive: str) -> None:
        _atomic_write(self.repairs_dir / f"{name}.md", directive)

    def save_handoff(self, text: str) -> None:
        _atomic_write(self.handoff_file, text[:2000])

    def load_handoff(self) -> str:
        try:
            return self.handoff_file.read_text("utf-8")
        except OSError:
            return ""

    def write_progress(self, text: str) -> None:
        _atomic_write(self.progress_file, text)

    # -- jobs --
    def save_job(self, job: dict[str, Any]) -> None:
        _atomic_write(self.jobs_dir / f"{job['jobId']}.json", json.dumps(job, ensure_ascii=False, indent=1))

    def load_job(self, job_id: str) -> dict[str, Any]:
        return _load_json(self.jobs_dir / f"{job_id}.json", {}) or {}

    def job_log(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.log"

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_dir.is_dir():
            return []
        out = []
        for p in sorted(self.jobs_dir.glob("*.json")):
            job = _load_json(p, None)
            if job:
                out.append(job)
        return out

    def evidence_manifest(self) -> dict[str, Any] | None:
        return _load_json(self.evidence_dir / "manifest.json", None)

    def write_evidence_manifest(self, manifest: dict[str, Any]) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.evidence_dir / "manifest.json",
                      json.dumps(manifest, ensure_ascii=False, indent=1))

    def verification_results(self) -> dict[str, Any] | None:
        return _load_json(self.verification_dir / "results.json", None)


# --- process identity -------------------------------------------------------------


def _ps_probe(pid: int) -> dict[str, str]:
    """One ps call: -> {'state': 'S'|'Z'|'R'..., 'lstart': '...'}.
    Empty values mean "no such process"; Z means zombie (dead, unreaped)."""
    try:
        out = subprocess.run(["ps", "-o", "state=,lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        line = out.stdout.strip()
        if out.returncode != 0 or not line:
            return {"state": "", "lstart": ""}
        parts = line.split(None, 1)
        return {"state": parts[0] if parts else "",
                "lstart": parts[1] if len(parts) > 1 else ""}
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "", "lstart": ""}


def _ps_start_identity(pid: int) -> str:
    """macOS/Linux per-process start identity (lstart). Empty when unknown."""
    return _ps_probe(pid)["lstart"]


def _process_identity(job: dict[str, Any]) -> dict[str, Any]:
    """Verify a managed job's process is really the one we started.

    os.kill(pid, 0) alone is not enough: the pid may have been recycled, or
    the process may be an unreaped zombie. We check the ps state + process
    group + start identity (lstart). A mismatch is treated as 'not our job' —
    callers must NOT attach a watcher or kill.
    """
    pid = int(job.get("pid") or 0)
    if not pid:
        return {"alive": False, "reason": "no-pid"}
    probe = _ps_probe(pid)
    if not probe["state"]:
        return {"alive": False, "reason": "dead"}
    if probe["state"].startswith("Z"):
        return {"alive": False, "reason": "dead"}  # zombie: exited, unreaped
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return {"alive": False, "reason": "gone"}
    expected_pgid = int(job.get("pgid") or 0)
    if expected_pgid and pgid != expected_pgid:
        return {"alive": False, "reason": "pgid-mismatch"}
    lstart = probe["lstart"]
    expected = str(job.get("startIdentity") or "")
    if expected and lstart and expected != lstart:
        return {"alive": False, "reason": "pid-reused"}
    if expected and not lstart:
        return {"alive": False, "reason": "start-identity-unreadable"}
    return {"alive": True, "pgid": pgid, "startIdentity": lstart}


def _terminate_job_process(job: dict[str, Any], *,
                           proc: subprocess.Popen | None = None,
                           grace: float = JOB_TERMINATE_GRACE) -> dict[str, Any]:
    """SIGTERM the process group, escalate to SIGKILL after grace. Returns a
    summary; the caller decides how to persist the job status."""
    pid = int(job.get("pid") or 0)
    pgid = int(job.get("pgid") or 0) or pid
    ident = _process_identity(job)
    if not ident["alive"]:
        return {"killed": False, "reason": ident["reason"]}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError as exc:
        return {"killed": False, "reason": f"sigterm {exc}"}
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return {"killed": True, "mode": "term"}
        elif not _process_identity(job)["alive"]:
            return {"killed": True, "mode": "term"}
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError as exc:
        return {"killed": False, "reason": f"sigkill {exc}"}
    for _ in range(15):
        if proc is not None:
            if proc.poll() is not None:
                return {"killed": True, "mode": "kill"}
        elif not _process_identity(job)["alive"]:
            return {"killed": True, "mode": "kill"}
        time.sleep(0.2)
    return {"killed": True, "mode": "kill", "linger": True}


# --- JobWatcher -------------------------------------------------------------------


def _reap_exit_code(pid: int) -> int:
    """Reap our own child and return its shell-convention exit code
    (128+signal when signaled). Raises ValueError when the code is
    unobtainable: not our child (e.g. control-plane restart) or no
    exit status yet."""
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        raise ValueError("not our child")
    if wpid != pid:
        raise ValueError("no exit status yet")
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    raise ValueError("no exit status")


class JobWatcher(threading.Thread):
    """Watch one BackgroundJob at the OS level and wake the runner when it
    exits (or overstays expectedWakeAt). The model never polls. Exit is
    persisted (status/exitCode/finishedAt) BEFORE the wake so a crash or a
    pause between exit and wake cannot lose the result."""

    def __init__(self, job: dict[str, Any], store: MissionStore,
                 on_wake: Callable[[dict[str, Any]], None],
                 proc: subprocess.Popen | None = None) -> None:
        super().__init__(name=f"mission-job-{job['jobId'][:8]}", daemon=True)
        self.job = job
        self.store = store
        self.on_wake = on_wake
        # Hold the Popen when the job is our child: poll() reaps the zombie
        # (a reaped exit flips kill(pid,0) too), and os.kill alone cannot see
        # through an unreaped zombie child.
        self.proc = proc
        self._stopped = threading.Event()

    def _alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        return _process_identity(self.job)["alive"]

    def _mark_exit(self, exit_code: int | None, unknown: bool = False) -> dict[str, Any]:
        job = self.store.load_job(self.job.get("jobId") or "")
        if not job:
            # run dir already gone (mission deleted); nothing to persist
            return self.job
        job.update({
            # completed ONLY with known exit code 0; an unreapable exit is
            # never claimed as success (default-fail, honest evidence)
            "status": "completed" if (not unknown and exit_code == 0) else "failed",
            "exitCode": exit_code,
            "exitKind": "exited",
            "exitUnknown": unknown,
            "finishedAt": _now_ms(),
        })
        self.store.save_job(job)
        return job

    def run(self) -> None:
        pid = int(self.job.get("pid") or 0)
        expected_wake = float(self.job.get("expectedWakeAt") or 0)
        while not self._stopped.is_set():
            if not self._alive():
                code: int | None
                unknown = False
                if self.proc is not None:
                    code = self.proc.returncode
                    if code is not None and code < 0:
                        # Popen convention (-SIG) -> shell convention (128+SIG)
                        code = 128 - code
                else:
                    # re-attached watcher (pause/resume): we are still the
                    # parent, so waitpid reaps the real exit status instead of
                    # turning a healthy exit into a bogus failure. When the
                    # process is not our child (restarted control plane) the
                    # code is honestly unknown and never claimed as success.
                    try:
                        code = _reap_exit_code(pid)
                    except ValueError:
                        if self._alive():
                            continue  # race between probe and waitpid
                        code = None
                        unknown = True
                job = self._mark_exit(code, unknown=unknown)
                self.on_wake({**job, "exitKind": "exited", "exitCode": code, "exitUnknown": unknown})
                return
            if expected_wake and time.time() > expected_wake + JOB_WAKE_GRACE:
                self.on_wake({**self.job, "exitKind": "overdue"})
                return
            self._stopped.wait(JOB_POLL_INTERVAL)

    def stop(self) -> None:
        self._stopped.set()


def job_log_tail(path: Path, lines: int = 80) -> str:
    try:
        return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(日志不可读)"


# --- StopPolicy --------------------------------------------------------------------


class StopPolicy:
    def __init__(self, options: dict[str, Any] | None) -> None:
        opts = {**DEFAULT_STOP_POLICY, **(options or {})}

        self.max_repair = int(opts["maxRepairPerTask"])
        self.max_no_progress = int(opts["maxNoProgressCycles"])
        self.max_cycles = int(opts["maxMissionCycles"])
        self.max_wall_sec = float(opts["maxWallTimeSec"])
        self.token_budget = opts.get("tokenBudget")

    def check(self, state: dict[str, Any]) -> str | None:
        if state.get("cycles", 0) >= self.max_cycles:
            return f"maxMissionCycles 达到上限（{self.max_cycles}）"
        # wallElapsedMs = mission wall clock EXCLUDING paused time (paused is
        # deliberate inactivity; pausing must stop the budget). Doc'd contract.
        wall_ms = float(state.get("wallElapsedMs") or state.get("activeMs") or 0)
        if wall_ms / 1000.0 >= self.max_wall_sec:
            return f"maxWallTime 达到上限（{int(self.max_wall_sec)}s，不含暂停）"
        if state.get("noProgress", 0) >= self.max_no_progress:
            return f"连续 {self.max_no_progress} 个循环无进展"
        if self.token_budget and int(state.get("tokensUsed", 0)) >= int(self.token_budget):
            return f"token 预算耗尽（{self.token_budget}）"
        return None


# --- wall-clock accounting ---------------------------------------------------------
# Four persisted buckets: wallElapsedMs (mission wall time EXCLUDING paused),
# agentActiveMs (model turns + phases), waitingMs (background jobs), pausedMs.
# maxWallTimeSec is checked against wallElapsedMs — pausing a mission genuinely
# freezes its wall budget; waiting does NOT (the mission is still advancing).

AGENT_PHASES = {"planning", "running", "evaluating", "repairing",
                "replanning", "verification", "verifying"}


def _accrue_state(state: dict[str, Any]) -> None:
    now = _now_ms()
    started = int(state.get("phaseStartedAt") or 0)
    if started:
        delta = now - started
        if delta > 0:
            phase = str(state.get("state"))
            if phase == "paused":
                state["pausedMs"] = int(state.get("pausedMs", 0)) + delta
            elif phase == "waiting":
                state["waitingMs"] = int(state.get("waitingMs", 0)) + delta
            elif phase in AGENT_PHASES:
                state["agentActiveMs"] = int(state.get("agentActiveMs", 0)) + delta
    state["phaseStartedAt"] = now
    state["wallElapsedMs"] = int(state.get("agentActiveMs", 0)) + int(state.get("waitingMs", 0))
    state["activeMs"] = state["wallElapsedMs"]  # legacy alias


def _file_sha256(path: Path, cap: int = 64 * 1024 * 1024) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
                if fh.tell() > cap:
                    return ""
        return h.hexdigest()
    except OSError:
        return ""


def _tail(text: str, limit: int = VERIFY_TAIL) -> str:
    return text[-limit:] if len(text) > limit else text


# --- Harness Verification Gate --------------------------------------------------------


class VerificationRunner:
    """Machine verification executed by the Control Plane (no model involved).

    Config (from mission.json -> verification):
      commands:      list of shell commands; passed iff exitCode == 0
      requiredFiles: list of paths (relative to mission cwd); passed iff file exists
      httpChecks:    list of {url, expectStatus?}; passed iff status == expectStatus (default 200)

    Every check is persisted under <run>/verification/ with full fields
    (kind/name/command/exitCode/stdoutTail/stderrTail/startedAt/endedAt/resultHash)
    plus raw stdout/stderr. The gate itself is resumable and re-runnable —
    it only ever reads the workspace and writes under the run dir.
    """

    def __init__(self, store: MissionStore, config: dict[str, Any], cwd: str) -> None:
        self.store = store
        self.config = config or {}
        self.cwd = cwd

    def run(self) -> dict[str, Any]:
        started = _now_ms()
        checks = (self._run_commands() + self._check_files() + self._check_http())
        passed = all(c.get("passed") for c in checks)
        summary = {"passed": passed, "checks": checks,
                   "startedAt": started, "endedAt": _now_ms()}
        summary["resultHash"] = hashlib.sha256(json.dumps(
            [c.get("resultHash") or "" for c in checks], sort_keys=True).encode()).hexdigest()[:16]
        self.store.verification_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(summary, ensure_ascii=False, indent=1)
        _atomic_write(self.store.verification_dir / "results.json", text)
        # keep every gate run: evidence must survive a later PASS overwrite
        _atomic_write(self.store.verification_dir / f"results-{started}.json", text)
        return summary

    def _record(self, kind: str, name: str, check: dict[str, Any],
                stdout: str = "", stderr: str = "") -> dict[str, Any]:
        result = {
            "kind": kind, "name": name, "passed": bool(check.get("passed")),
            "startedAt": check.get("startedAt"), "endedAt": check.get("endedAt"),
            "error": check.get("error"),
            "stdoutTail": _tail(stdout), "stderrTail": _tail(stderr),
        }
        if "command" in check:
            result["command"] = check["command"]
        if "exitCode" in check:
            result["exitCode"] = check["exitCode"]
        if "url" in check:
            result["url"] = check["url"]
        result["resultHash"] = hashlib.sha256(json.dumps(
            {k: v for k, v in result.items() if k not in ("startedAt", "endedAt")},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        raw_dir = self.store.verification_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{len(list(raw_dir.glob('*'))) // 2}-{name}")[:80]
        try:
            (raw_dir / f"{slug}.stdout").write_text(stdout, "utf-8")
            (raw_dir / f"{slug}.stderr").write_text(stderr, "utf-8")
        except OSError:
            pass
        return result

    def _run_commands(self) -> list[dict[str, Any]]:
        out = []
        for cmd in (self.config.get("commands") or []):
            started = _now_ms()
            entry: dict[str, Any] = {"command": str(cmd), "startedAt": started}
            try:
                proc = subprocess.run(["/bin/zsh", "-lc", str(cmd)], cwd=self.cwd,
                                      capture_output=True, text=True, timeout=VERIFY_CMD_TIMEOUT)
                entry["exitCode"] = proc.returncode
                entry["passed"] = proc.returncode == 0
                stdout, stderr = proc.stdout or "", proc.stderr or ""
            except subprocess.TimeoutExpired as exc:
                entry["exitCode"] = None
                entry["passed"] = False
                entry["error"] = f"timeout>{VERIFY_CMD_TIMEOUT}s"
                stdout, stderr = (exc.stdout or ""), (exc.stderr or "")
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", "replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
            except OSError as exc:
                entry["exitCode"] = None
                entry["passed"] = False
                entry["error"] = f"spawn: {exc}"
                stdout, stderr = "", ""
            entry["endedAt"] = _now_ms()
            out.append(self._record("command", str(cmd)[:80], entry, stdout, stderr))
        return out

    def _check_files(self) -> list[dict[str, Any]]:
        out = []
        for rel in (self.config.get("requiredFiles") or []):
            started = _now_ms()
            path = Path(str(rel))
            if not path.is_absolute():
                path = Path(self.cwd) / path
            exists = path.is_file()
            out.append(self._record(
                "file", str(rel)[:80],
                {"passed": exists, "startedAt": started, "endedAt": _now_ms(),
                 "error": None if exists else "missing"}))
        return out

    def _check_http(self) -> list[dict[str, Any]]:
        out = []
        for spec in (self.config.get("httpChecks") or []):
            started = _now_ms()
            url = str(spec.get("url") or "")
            expect = int(spec.get("expectStatus") or 200)
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    status = resp.status
                passed = status == expect
                error = None if passed else f"status {status} != {expect}"
            except OSError as exc:
                status, passed, error = None, False, f"{type(exc).__name__}: {exc}"
            out.append(self._record(
                "http", url[:80],
                {"passed": passed, "url": url, "startedAt": started,
                 "endedAt": _now_ms(), "error": error,
                 "status": status, "expectStatus": expect}))
        return out


def _git_diff_summary(cwd: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, "diff", "--stat"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or "").strip()
    return text or None


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
        self.wake_event = threading.Event()
        self.wake_payload: dict[str, Any] | None = None
        self._control = threading.Event()  # set => stop loop (cancel/fail)
        self._last_progress_sig: str | None = None
        # no-progress compare must survive crashes: seed from persisted value
        disk = self.store.load_state()
        self._last_progress_sig = str(disk.get("progressSignature") or "") or None

    # -- thread plumbing --
    def request_stop(self) -> None:
        self._control.set()
        self.wake_event.set()

    def wake(self, payload: dict[str, Any]) -> None:
        self.wake_payload = payload
        self.wake_event.set()

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
              read_only: bool = False, timeout: int = WORKER_TURN_TIMEOUT) -> dict[str, Any]:
        """Run one codex turn on a fresh thread. Counters always accumulate on
        a fresh disk copy: a pause/cancel landing mid-turn must never be
        clobbered by a stale in-memory dict. Mutates the caller's state dict
        in place afterwards so its counters stay in sync."""
        mission = self.store.load_mission()
        started = _now_ms()
        result = self.manager.adapter.run_turn(
            prompt=prompt, cwd=mission.get("cwd"), read_only=read_only,
            model=mission.get("model"), effort=mission.get("effort"), timeout=timeout)
        elapsed = _now_ms() - started
        usage = result.get("usage") or {}
        tokens = int(usage.get("uncachedInputTokens") or 0) + int(usage.get("outputTokens") or 0)
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
            elif current in ("running", "repairing"):
                if not self._phase_worker(state, repair=(current == "repairing")):
                    return
            elif current == "waiting":
                if not self._phase_waiting(state):
                    return
            elif current == "evaluating":
                if not self._phase_evaluating(state):
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

    # -- phases --
    def _phase_planning(self, state: dict[str, Any]) -> bool:
        mission = self.store.load_mission()
        prompt = (
            "你是 Mission Planner。把下面的长期目标拆解为可独立验收的工作单元。\n"
            f"目标：{mission.get('objective')}\n"
            + (f"总验收标准：\n- " + "\n- ".join(mission.get("acceptanceCriteria") or []) + "\n"
               if mission.get("acceptanceCriteria") else "")
            + "要求：2-6 个单元；每个单元给出 title、description、acceptance（可勾选的验收标准列表）。\n"
              "只在回复末尾输出标记块（JSON 数组，不要其它格式）：\n"
              "<<<LAOMO_PLAN\n"
              '[{"title":"...","description":"...","acceptance":["..."]}]'
              "\nLAOMO_PLAN>>>"
        )
        result = self._turn(state, prompt)
        units = parse_json_marker(result.get("text") or "", _PLAN_RE)
        if not isinstance(units, list) or not units:
            # default-fail: unparseable plan retries once via replan cycle cap
            state["planningFailures"] = int(state.get("planningFailures", 0)) + 1
            if state["planningFailures"] >= 2:
                self._transition(state, "failed", stopReason="planner 输出不可解析")
                return False
            self.store.event("planning", "unparseable plan output")
            return True
        plan = {"units": [], "replans": 0}
        for i, u in enumerate(units):
            if not isinstance(u, dict) or not u.get("title"):
                continue
            plan["units"].append({
                "index": len(plan["units"]),
                "title": str(u.get("title"))[:120],
                "description": str(u.get("description") or "")[:600],
                "acceptance": [str(a) for a in (u.get("acceptance") or [])][:8],
                "status": "pending", "repairCount": 0,
            })
        if not plan["units"]:
            self._transition(state, "failed", stopReason="planner 未产出有效单元")
            return False
        self.store.save_plan(plan)
        self.store.event("planning", {"units": len(plan["units"])})
        self._transition(state, "running", currentUnit=0)
        return True

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

    def _phase_worker(self, state: dict[str, Any], *, repair: bool) -> bool:
        index = int(state.get("currentUnit") or 0)
        unit = self._unit(index)
        if unit is None:
            self._transition(state, "failed", stopReason=f"工作单元 {index} 不存在")
            return False
        mission = self.store.load_mission()
        if repair:
            directive = state.get("repairDirective") or "修复验收未通过的问题"
            unit["repairCount"] = int(unit.get("repairCount", 0)) + 1
            self._save_unit(unit)
            self.store.write_repair(
                f"{state.get('cycles')}-{index}-{unit['repairCount']}",
                f"# RepairDirective\n\n{directive}\n\n## verdict\n{json.dumps(state.get('lastVerdict') or {}, ensure_ascii=False)}")
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
        result = self._turn(state, prompt)
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
        state.pop("delta", None)
        self._transition(state, "evaluating")
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
            self._transition(state, "evaluating")
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
        self.store.event("job", {"jobId": job_id, "pid": proc.pid,
                                 "command": command[:120], "expectedWakeAt": expected})
        self._transition(state, "waiting", waitingJobId=job_id)

    def _on_job_wake(self, woken: dict[str, Any]) -> None:
        self.wake(woken)

    def _phase_waiting(self, state: dict[str, Any]) -> bool:
        # Event-driven: no model polling. The job watcher sets wake_event.
        # request_stop sets BOTH events — check control first so a stop is
        # never mistaken for a job completion wake.
        while not self.wake_event.wait(timeout=0.5):
            if self._control.is_set():
                return False
            state = self._state()
            if self._paused(state):
                self.store.event("runner", "exit: paused while waiting")
                return False
        if self._control.is_set():
            return False
        self.wake_event.clear()
        woken = self.wake_payload or {}
        self.wake_payload = None
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
        self.store.event("wake", {"jobId": job_id, "exitKind": exit_kind})
        self._transition(state, "running")
        return True

    def _phase_evaluating(self, state: dict[str, Any]) -> bool:
        index = int(state.get("currentUnit") or 0)
        unit = self._unit(index)
        if unit is None:
            self._transition(state, "failed", stopReason="evaluating: 单元缺失")
            return False
        mission = self.store.load_mission()
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
        result = self._turn(state, prompt, read_only=True, timeout=EVALUATOR_TURN_TIMEOUT)
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

        # no-progress signature: unit status map + handoff hash. Persisted so
        # a crash between cycles cannot reset the counter (crash-resumable).
        plan = self.store.load_plan()
        sig = hashlib.sha1(json.dumps({
            "statuses": [u.get("status") for u in plan["units"]],
            "handoff": hashlib.sha1(self.store.load_handoff().encode()).hexdigest()[:12],
        }, sort_keys=True).encode()).hexdigest()
        if sig == self._last_progress_sig:
            no_progress = int(state.get("noProgress", 0)) + 1
        else:
            no_progress = 0
        self._last_progress_sig = sig
        disk = self.store.load_state()
        disk["noProgress"] = no_progress
        disk["progressSignature"] = sig
        self.store.save_state(disk)
        state.update({"noProgress": no_progress, "progressSignature": sig})

        v = verdict.get("verdict")
        if v == "PASS":
            unit["status"] = "passed"
            unit["lastVerdict"] = "PASS"
            self._save_unit(unit)
            self._checkpoint(state, index, unit, verdict)
            # reload: the stale pre-save copy would re-run the just-passed unit
            nxt = self._next_pending(plan_index=self.store.load_plan(), current=index)
            self._update_progress_md()
            if nxt is None:
                self._transition(state, "verification")
            else:
                state.pop("repairDirective", None)
                self._transition(state, "running", currentUnit=nxt)
            return True
        if v == "BLOCKED":
            unit["status"] = "blocked"
            unit["lastVerdict"] = "BLOCKED"
            self._save_unit(unit)
            self._checkpoint(state, index, unit, verdict)
            self._update_progress_md()
            self._transition(state, "blocked",
                              stopReason="evaluator 判定 BLOCKED：" + "; ".join(verdict.get("reasons") or [])[:200])
            return False
        # NEEDS_WORK
        unit["lastVerdict"] = "NEEDS_WORK"
        self._save_unit(unit)
        if int(unit.get("repairCount", 0)) >= self.policy.max_repair:
            self._transition(state, "failed",
                             stopReason=f"单元 #{index + 1} 修复次数超限（{self.policy.max_repair}）")
            return False
        state["repairDirective"] = str(verdict.get("repair") or "; ".join(verdict.get("reasons") or []))[:2000]
        self._transition(state, "repairing")
        return True

    @staticmethod
    def _next_pending(plan_index: dict[str, Any], current: int) -> int | None:
        for u in plan_index["units"]:
            if u["status"] == "pending" and u["index"] != current:
                return u["index"]
        for u in plan_index["units"]:
            if u["status"] == "pending":
                return u["index"]
        return None

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
                f"最后判定 {u.get('lastVerdict', '—')}）" for u in plan["units"]]
        self.store.write_progress(
            f"# Mission 进度\n\n更新：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n" + "\n".join(rows))

    def _phase_replanning(self, state: dict[str, Any]) -> bool:
        plan = self.store.load_plan()
        if int(plan.get("replans", 0)) >= self.policy.max_no_progress:
            self._transition(state, "failed", stopReason="replan 次数超限")
            return False
        mission = self.store.load_mission()
        gaps = [u for u in plan["units"] if u.get("status") != "passed"]
        prompt = (
            "你是 Mission Planner（补缺口轮）。以下单元尚未通过验收，给出修正后的后续单元计划。\n"
            f"目标：{mission.get('objective')}\n"
            "未通过单元：\n"
            + "\n".join(f"- #{u['index'] + 1} {u['title']} 状态 {u['status']} 最后判定 {u.get('lastVerdict')}"
                        for u in gaps)
            + "\n\n输出（只输出标记块）：\n<<<LAOMO_PLAN\n"
              '[{"title":"...","description":"...","acceptance":["..."]}]'
              "\nLAOMO_PLAN>>>"
        )
        result = self._turn(state, prompt)
        units = parse_json_marker(result.get("text") or "", _PLAN_RE)
        if not isinstance(units, list) or not units:
            self._transition(state, "failed", stopReason="replanner 输出不可解析")
            return False
        next_index = max((u["index"] for u in plan["units"]), default=-1) + 1
        for u in units:
            if isinstance(u, dict) and u.get("title"):
                plan["units"].append({
                    "index": next_index, "title": str(u.get("title"))[:120],
                    "description": str(u.get("description") or "")[:600],
                    "acceptance": [str(a) for a in (u.get("acceptance") or [])][:8],
                    "status": "pending", "repairCount": 0,
                })
                next_index += 1
        plan["replans"] = int(plan.get("replans", 0)) + 1
        self.store.save_plan(plan)
        self.store.event("replanning", {"added": next_index - max((u["index"] for u in plan["units"]), default=0)})
        self._transition(state, "running", currentUnit=next_index - 1 if next_index else 0)
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
            # triple gate: all units passed + final regression (the evaluator ran
            # the checks above) + final evaluator PASS
            all_passed = all(u.get("status") == "passed" for u in plan["units"])
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
        self._watchers: dict[str, JobWatcher] = {}
        # Watchers stopped while their job is still running. They keep the
        # Popen handle referenced: a re-attached watcher reuses it so proc.poll()
        # reaps the real exit status (a dropped Popen would let Python's
        # subprocess._cleanup() reap the zombie before os.waitpid sees it).
        self._stopped_watchers: dict[str, JobWatcher] = {}

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
        bases = [self.workspace_root, Path(os.getcwd())]
        for base in bases:
            root = base / RUNS_DIRNAME
            if root.is_dir():
                roots.extend(p for p in root.iterdir() if p.is_dir() and (p / "mission.json").is_file())
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
            watcher = self._watchers.pop(mission_id, None)
            if watcher is not None:
                self._stopped_watchers.setdefault(mission_id, watcher)
        if watcher:
            watcher.stop()

    def attach_watcher(self, mission_id: str, watcher: JobWatcher) -> None:
        with self._lock:
            old = self._watchers.get(mission_id)
            self._watchers[mission_id] = watcher
            if old is not None:
                self._stopped_watchers[mission_id] = old
        if old is not None:
            old.stop()

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
            old = self._stopped_watchers.pop(mission_id, None)
            proc = old.proc if old is not None else None
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
        return {"ok": True, "mission": self._summary(store)}

    def terminate_mission_jobs(self, mission_id: str, store: MissionStore,
                               mark: str = "cancelled") -> list[dict[str, Any]]:
        """SIGTERM -> grace -> SIGKILL every managed job still running.
        The mission's watcher is stopped and joined FIRST: the Control Plane
        owns the job lifecycle, and a watcher racing the termination could
        persist a different status (e.g. failed) after we persist the mark.
        Returns per-job outcomes; terminal jobs are left untouched."""
        with self._lock:
            watcher = self._watchers.pop(mission_id, None)
        if watcher:
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
                store.event("recover", f"resuming from {name}")
            self._ensure_not_active_quiet(mission_id)
            runner = MissionRunner(self, mission_id)
            with self._lock:
                self._runners[mission_id] = runner
            runner.start()
            resumed.append(mission_id)
        return resumed

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
