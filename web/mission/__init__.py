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

from .manager import *  # noqa: F401,F403  (MissionRunner + MissionManager)
from .models import *  # noqa: F401,F403  (constants/helpers/StopPolicy)
from .store import *  # noqa: F401,F403  (MissionStore)
from .jobs import *  # noqa: F401,F403  (JobWatcher + process helpers)
from .verification import *  # noqa: F401,F403  (VerificationRunner)

__all__ = ['ACTIVE_STATES', 'AGENT_PHASES', 'ALL_PHASES', 'DEFAULT_STOP_POLICY', 'EVALUATOR_TURN_TIMEOUT', 'JOB_POLL_INTERVAL', 'JOB_STATES', 'JOB_TERMINATE_GRACE', 'JOB_WAKE_GRACE', 'JobWatcher', 'MissionError', 'MissionManager', 'MissionRunner', 'MissionStore', 'RUNS_DIRNAME', 'StopPolicy', 'TERMINAL_STATES', 'VERIFY_CMD_TIMEOUT', 'VERIFY_TAIL', 'VerificationRunner', 'WORKER_TURN_TIMEOUT', 'job_log_tail', 'parse_json_marker']
