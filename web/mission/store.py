"""MissionStore: all durable state for one mission run."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .models import _atomic_write, _load_json, _now_ms


def _clone(data: Any) -> Any:
    """Deep copy of JSON-safe data. Loads return fresh dicts: concurrent
    threads must never mutate the same object, and each read-modify-write
    starts from a private snapshot."""
    return json.loads(json.dumps(data)) if data is not None else data


# --- MissionStore ----------------------------------------------------------------


class MissionStore:
    """All durable state for one mission, under .laomo/runs/<id>/.

    `lock` serializes state/plan/job read-modify-write across the scheduler
    thread, per-unit worker threads and watcher threads. Callers doing a
    multi-step RMW must hold it for the whole span; single calls are safe.
    """

    def __init__(self, run_root: Path) -> None:
        self.root = run_root
        self.lock = threading.RLock()
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
        with self.lock:
            _atomic_write(self.mission_file, json.dumps(data, ensure_ascii=False, indent=1))

    def load_mission(self) -> dict[str, Any]:
        with self.lock:
            return _clone(_load_json(self.mission_file, {}) or {})

    # -- state --
    def load_state(self) -> dict[str, Any]:
        with self.lock:
            return _clone(_load_json(self.state_file, {}) or {})

    def save_state(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = _now_ms()
        with self.lock:
            _atomic_write(self.state_file, json.dumps(state, ensure_ascii=False, indent=1))

    # -- plan --
    def load_plan(self) -> dict[str, Any]:
        with self.lock:
            return _clone(_load_json(self.plan_file, {"units": [], "replans": 0})
                          or {"units": [], "replans": 0})

    def save_plan(self, plan: dict[str, Any]) -> None:
        with self.lock:
            _atomic_write(self.plan_file, json.dumps(plan, ensure_ascii=False, indent=1))

    def write_progress_md(self) -> None:
        """Render progress.md from the current plan (unit statuses)."""
        plan = self.load_plan()
        rows = [f"- [{u['status']}] #{u['index'] + 1} {u['title']}（repair×{u.get('repairCount', 0)}，"
                f"最后判定 {u.get('lastVerdict') or '—'}）" for u in plan["units"]]
        self.write_progress(
            f"# Mission 进度\n\n更新：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n" + "\n".join(rows))

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
        with self.lock:
            _atomic_write(self.jobs_dir / f"{job['jobId']}.json",
                          json.dumps(job, ensure_ascii=False, indent=1))

    def load_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return _clone(_load_json(self.jobs_dir / f"{job_id}.json", {}) or {})

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


