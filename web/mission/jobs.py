"""BackgroundJob lifecycle: process identity, termination, JobWatcher."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import (JOB_POLL_INTERVAL, JOB_TERMINATE_GRACE, JOB_WAKE_GRACE,
                     _now_ms)


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


