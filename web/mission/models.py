"""Models: constants, markers, helpers, StopPolicy (Mission core)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any


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
    "maxParallelWorkers": 2,
}

MAX_PARALLEL_WORKERS = 4  # hard cap: a mission may never spawn more

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
    # Unique per-call tmp name: parallel unit threads write artifacts
    # (progress.md, handoff) concurrently; a fixed tmp path would race —
    # one thread's tmp.replace() then crashes with FileNotFoundError.
    # os.replace() is atomic, so the last writer wins without corruption.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
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


# --- StopPolicy --------------------------------------------------------------------


class StopPolicy:
    def __init__(self, options: dict[str, Any] | None) -> None:
        opts = {**DEFAULT_STOP_POLICY, **(options or {})}

        self.max_repair = int(opts["maxRepairPerTask"])
        self.max_no_progress = int(opts["maxNoProgressCycles"])
        self.max_cycles = int(opts["maxMissionCycles"])
        self.max_wall_sec = float(opts["maxWallTimeSec"])
        self.token_budget = opts.get("tokenBudget")
        self.max_parallel = max(1, min(int(opts.get("maxParallelWorkers") or 2),
                                       MAX_PARALLEL_WORKERS))

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


