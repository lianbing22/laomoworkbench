"""P1.2/M3 tests: per-unit git worktrees + serial integration.

Real git repositories are created inside tmp dirs; the same FakeAdapter
role-dispatch style as mission_test.py drives the mission (no codex).
Covered contract:

* every unit builds in its own git worktree (path/branch/baseSha recorded)
  and worker/evaluator/background-job cwd all point into that worktree
* after a unit's evaluator PASSes, the control plane integrates the unit
  branch into the mission branch (serial), so the next unit's base is the
  previous unit's integrated head; main HEAD only ever contains integrated
  work; worktrees are cleaned up after integration
* an integration conflict marks the unit `conflict` and blocks the mission
  (merge is aborted; the mission branch stays clean — ConflictResolver is M5)
* non-git workspaces keep the P1.1 behavior (no worktree, no integration)
* crash-resume never re-runs a passed unit's worker turn (integrated unit
  hits the scheduler's finish-fast-path)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

MISSION_IMPORT_ERROR = None
try:
    from mission import MissionError, MissionManager, WorktreeManager  # noqa: E402
except Exception as _exc:  # mainline module not merged yet
    MISSION_IMPORT_ERROR = _exc

    class MissionError(Exception):  # type: ignore[no-redef]
        pass

    class MissionManager:  # type: ignore[no-redef]  placeholder; cases skip
        pass

from mission_test import (  # noqa: E402  (role dispatch + scripted adapter)
    FakeAdapter, detect_role, handoff_text, job_block, plan_block,
    sample_units, verdict_block,
)

POLL_INTERVAL = 0.05
POLL_TIMEOUT = 15.0
TERMINAL = {"done", "failed", "cancelled", "blocked"}


# ------------------------------------------------------------------ git helpers


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"git {args} 失败: {proc.stderr[:300]}")
    return proc.stdout.strip()


def git_ok(repo: Path, *args: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def init_repo(repo: Path) -> str:
    """git init + config + an initial commit; returns the initial sha."""
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@local")
    git(repo, "config", "user.name", "test")
    (repo / "base.txt").write_text("base\n", "utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return git(repo, "rev-parse", "HEAD")


# ------------------------------------------------------------------ adapters


class WritingAdapter(FakeAdapter):
    """Worker turns write a file into their cwd (the unit worktree); an
    optional on_worker hook runs after each worker turn. Set `shared` to make
    every worker also write shared.txt (used by the conflict test — a same
    file touched by parallel-ish writers)."""

    def __init__(self, on_worker=None):
        super().__init__()
        self.worker_count = 0
        self.on_worker = on_worker  # callable(prompt, cwd, n) or None
        self.shared = False

    def run_turn(self, *, prompt, cwd=None, read_only=False, model=None,
                 effort=None, timeout=600):
        result = super().run_turn(prompt=prompt, cwd=cwd, read_only=read_only,
                                  model=model, effort=effort, timeout=timeout)
        call = self.calls[-1]
        if call["role"] == "worker" and call["cwd"]:
            self.worker_count += 1
            Path(call["cwd"], f"feature-{self.worker_count}.txt").write_text(
                f"from unit worker {self.worker_count}\n", "utf-8")
            if self.shared:
                Path(call["cwd"], "shared.txt").write_text(
                    f"from unit worker {self.worker_count}\n", "utf-8")
            if self.on_worker is not None:
                self.on_worker(call["prompt"], call["cwd"], self.worker_count)
        return result


UNIT_PLAN_2 = [
    {"id": "a", "title": "单元A", "description": "实现单元A",
     "acceptance": ["单元A 产出 feature 文件"], "dependencies": []},
    {"id": "b", "title": "单元B", "description": "实现单元B",
     "acceptance": ["单元B 产出 feature 文件"], "dependencies": ["a"]},
]


# ------------------------------------------------------------------ base class


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        if MISSION_IMPORT_ERROR is not None:
            self.skipTest("mission 包不可导入 (%r)" % (MISSION_IMPORT_ERROR,))
        ws = tempfile.mkdtemp(prefix="laomo-wt-test-")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        self.root = Path(ws)
        self.repo = self.root / "repo"
        self.initial_sha = init_repo(self.repo)
        self.adapter = WritingAdapter()
        self.mgr = MissionManager(self.adapter, self.root)
        self.tracked = []

    def tearDown(self):
        for mgr, mid in self.tracked:
            try:
                state = str((mgr.status(mid).get("mission") or {}).get("state", "")).lower()
                if state and state not in TERMINAL:
                    mgr.cancel(mid)
            except Exception:
                pass
        time.sleep(0.2)

    def create(self, cwd=None, verification=None):
        mid = (self.mgr.create("目标：产出两个 feature",
                               cwd=str(cwd or self.repo),
                               acceptance_criteria=[],
                               verification=verification)
               .get("mission", {}).get("id"))
        self.assertTrue(mid)
        self.tracked.append((self.mgr, mid))
        return mid

    def wait_terminal(self, mid, timeout=POLL_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.mgr.status(mid).get("mission") or {}
            if payload.get("state") in TERMINAL:
                return payload.get("state")
            time.sleep(POLL_INTERVAL)
        raise AssertionError("mission 未在 %ss 内到达终态: %r"
                             % (timeout, self.mgr.status(mid)))

    def mdir(self, mid):
        candidates = [self.repo / ".laomo" / "runs" / mid]
        if (self.root / "nongit").is_dir():
            candidates.append(self.root / "nongit" / ".laomo" / "runs" / mid)
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]

    def plan(self, mid):
        return json.loads((self.mdir(mid) / "plan.json").read_text("utf-8"))

    def events(self, mid):
        out = []
        for line in (self.mdir(mid) / "events.ndjson").read_text("utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def script_two_units(self):
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.defaults["worker"] = handoff_text(note="单元完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])


# ------------------------------------------------------------------ cases


class SerialWorktreeTest(WorktreeTest):
    def test_units_build_in_worktrees_and_integrate_serial(self):
        self.script_two_units()
        mid = self.create()
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        plan = self.plan(mid)
        u0, u1 = plan["units"][0], plan["units"][1]
        self.assertEqual([u["state"] for u in plan["units"]], ["integrated", "integrated"])
        for u in plan["units"]:
            self.assertTrue(u["worktree"]["path"], "worktree 路径应被记录")
            self.assertTrue(u["worktree"]["branch"].startswith("laomo/u"))
            self.assertEqual(u["worktree"]["baseSha"], self.initial_sha
                             if u["index"] == 0 else u0["worktree"]["headSha"])
        # worker/evaluator cwd 都在单元工作树中，而不是 mission 工作区
        worker_cwds = [c["cwd"] for c in self.adapter.calls_for("worker")]
        self.assertEqual(len(worker_cwds), 2)
        self.assertTrue(worker_cwds[0].endswith(f"/u0"), worker_cwds)
        self.assertTrue(worker_cwds[1].endswith("/u1"), worker_cwds)
        eval_cwds = [c["cwd"] for c in self.adapter.calls_for("evaluator")]
        self.assertTrue(eval_cwds[0].endswith("/u0"), eval_cwds)
        self.assertTrue(eval_cwds[1].endswith("/u1"), eval_cwds)
        # 集成后 main 分支只含集成结果，且工作树被清理
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), u1["worktree"]["headSha"])
        self.assertEqual((self.repo / "feature-1.txt").read_text("utf-8"), "from unit worker 1\n")
        self.assertEqual((self.repo / "feature-2.txt").read_text("utf-8"), "from unit worker 2\n")
        log = git(self.repo, "log", "--oneline", "-3")
        self.assertIn("laomo: unit #1", log)
        self.assertIn("laomo: unit #2", log)
        for u in plan["units"]:
            self.assertFalse(
                Path(u["worktree"]["path"]).is_dir(),
                f"集成后工作树应被清理: {u['worktree']['path']}")
        kinds = [(e["type"], (e["detail"] or {}).get("unit"), (e["detail"] or {}).get("phase"))
                 for e in self.events(mid) if e["type"] == "integration"]
        self.assertEqual(kinds, [("integration", 0, "start"), ("integration", 0, "integrated"),
                                 ("integration", 1, "start"), ("integration", 1, "integrated")])

    def test_background_job_defaults_cwd_to_worktree(self):
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.script("worker", job_block("touch job-marker.txt"),
                            handoff_text(note="作业完成。"))
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        mid = self.create()
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        jobs_dir = self.mdir(mid) / "jobs"
        jobs = [json.loads(f.read_text("utf-8")) for f in jobs_dir.glob("*.json")]
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["cwd"].endswith("/u0"), jobs[0]["cwd"])
        # 作业产物随单元集成进 main
        self.assertEqual((self.repo / "job-marker.txt").exists(), True)

    def test_non_git_workspace_keeps_p11_behavior(self):
        # mission cwd 是非 git 目录：无 worktree、无集成事件、直跑 workspace
        self.script_two_units()
        mid = self.create(cwd=self.nongit_dir())
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")
        for u in self.plan(mid)["units"]:
            self.assertEqual(u["worktree"]["path"], None)
            self.assertIn(u["state"], ("passed", "integrated"))
        self.assertTrue(all(e["type"] != "integration" for e in self.events(mid)))

    def nongit_dir(self):
        d = self.root / "nongit"
        d.mkdir(parents=True, exist_ok=True)
        return d


class WorktreeConflictTest(WorktreeTest):
    def test_conflicting_integration_blocks_mission_and_marks_unit(self):
        def on_worker(prompt, cwd, n):
            # 单元 #2（index 1）开工后，主分支被外部提交改动同一文件
            if "单元 #2" in (prompt or ""):
                with open(self.repo / "shared.txt", "a", encoding="utf-8") as fh:
                    fh.write("external\n")
                git(self.repo, "add", "-A")
                git(self.repo, "commit", "-q", "-m", "external change")

        self.adapter.shared = True
        self.adapter.on_worker = on_worker
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.script("worker", handoff_text(note="单元完成。"),
                            handoff_text(note="单元完成。"))
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        mid = self.create()
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "blocked")

        u0, u1 = self.plan(mid)["units"]
        self.assertEqual(u0["state"], "integrated")
        self.assertEqual(u1["state"], "conflict")
        conflicts = [e for e in self.events(mid)
                     if e["type"] == "integration"
                     and (e["detail"] or {}).get("phase") == "conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["detail"]["unit"], 1)
        # merge 已 abort：主分支无冲突中状态，main 保持外部提交内容
        self.assertFalse(git_ok(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertNotIn("from unit worker 2", (self.repo / "shared.txt").read_text("utf-8"))


class WorktreeCrashResumeTest(WorktreeTest):
    def test_resume_never_reruns_integrated_unit(self):
        # 手工构造“崩溃后”的磁盘状态：unit0 已集成，mission=working u1
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        self.assertTrue(wm.available)
        info = wm.ensure(0, "单元A")
        Path(info["path"], "feature-1.txt").write_text("done\n", "utf-8")
        res = wm.integrate(0, "单元A", branch=info["branch"])
        self.assertTrue(res["ok"])
        wm.cleanup(0, branch=info["branch"])

        plan = {
            "version": 2, "replans": 0,
            "units": [
                {"id": "a", "index": 0, "title": "单元A", "description": "",
                 "acceptance": ["x"], "dependencies": [],
                 "state": "integrated", "status": "integrated",
                 "attempt": 1, "repairCount": 0,
                 "worktree": info, "jobId": None, "delta": None,
                 "repairDirective": None, "lastVerdict": "PASS",
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "b", "index": 1, "title": "单元B", "description": "",
                 "acceptance": ["y"], "dependencies": ["a"],
                 "state": "pending", "status": "pending",
                 "attempt": 0, "repairCount": 0,
                 "worktree": {"path": None, "branch": None,
                              "baseSha": None, "headSha": None},
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": None, "worker": {"startedAt": None, "finishedAt": None}},
            ],
        }
        store.save_plan(plan)
        store.save_state({"state": "running", "cycles": 2, "currentUnit": 1,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        self.adapter.script("worker", handoff_text(note="单元B 完成。"))
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        worker_prompts = self.adapter.prompts_for("worker")
        self.assertEqual(len(worker_prompts), 1, "不得重跑已集成单元的 worker")
        self.assertIn("单元 #2", worker_prompts[0])
        self.assertEqual(self.plan(mid)["units"][0]["state"], "integrated")
        self.assertEqual(self.plan(mid)["units"][1]["state"], "integrated")
        self.assertEqual((self.repo / "feature-1.txt").read_text("utf-8"), "from unit worker 1\n")


class IntegrationReconcileTest(WorktreeTest):
    """P1.2/M5: integration is a write-ahead transaction; a crash between
    `git merge` and the plan.json write is reconciled from git truth.

    * git merge landed + crash -> plan says `integrating` -> restart adopts
      the merge (no duplicate commits, no re-merge)
    * crash before the merge -> replay the idempotent integrate
    * crash mid-conflicted-merge (MERGE_HEAD left behind) -> abort first,
      replay, land on `conflict` honestly
    * stale git index.lock after a hard crash -> cleared, mission proceeds
    """

    def craft_crash(self, mid, unit0_state, tx, worktree_info):
        """Hand-write the post-crash disk state: unit0 wedged mid-integration
        (with its transaction record), unit1 still pending on `a`."""
        store = self.mgr.store_for(mid)
        plan = {
            "version": 2, "replans": 0,
            "units": [
                {"id": "a", "index": 0, "title": "单元A", "description": "",
                 "acceptance": ["x"], "dependencies": [],
                 "state": unit0_state, "status": unit0_state,
                 "attempt": 1, "repairCount": 0,
                 "worktree": worktree_info, "jobId": None, "delta": None,
                 "repairDirective": None, "lastVerdict": "PASS",
                 "integration": tx,
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "b", "index": 1, "title": "单元B", "description": "",
                 "acceptance": ["y"], "dependencies": ["a"],
                 "state": "pending", "status": "pending",
                 "attempt": 0, "repairCount": 0,
                 "worktree": {"path": None, "branch": None,
                              "baseSha": None, "headSha": None},
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": None,
                 "worker": {"startedAt": None, "finishedAt": None}},
            ],
        }
        store.save_plan(plan)
        store.save_state({"state": "running", "cycles": 2, "currentUnit": 1,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        self.adapter.script("worker", handoff_text(note="单元B 完成。"))
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])

    def integ_events(self, mid, unit):
        return [e["detail"] for e in self.events(mid)
                if e["type"] == "integration"
                and isinstance(e.get("detail"), dict)
                and e["detail"].get("unit") == unit]

    def test_crash_after_merge_adopts_git_truth(self):
        """M5 核心：git merge 已成功 → 进程崩溃 → plan.json 仍写 integrating。
        重启后以 git 为真相直接采纳合并结果（不重放、不产生新提交）。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "feature-1.txt").write_text("from unit worker 1\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit 0 work")
        unit_head = wm.rev(wt)
        # 崩溃的进程：已把 u0 分支合入 main（FF），但没来得及写回 plan.json
        git(self.repo, "merge", "--no-edit", info["branch"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), unit_head)
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": unit_head, "dirty": False, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, {**info, "headSha": unit_head})

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        plan = self.plan(mid)
        u0 = plan["units"][0]
        self.assertEqual(u0["state"], "integrated")
        self.assertEqual(u0["integration"].get("phase"), "cleaned")
        self.assertTrue(u0["integration"].get("reconciled"))
        adopted = [d for d in self.integ_events(mid, 0)
                   if d.get("phase") == "integrated" and d.get("reconciled")]
        self.assertEqual(len(adopted), 1, "应恰好一次 reconcile 采纳: %r" % self.integ_events(mid, 0))
        self.assertEqual(adopted[0].get("alreadyMerged"), unit_head)
        # 采纳而非重放：feature-1 在 main 历史中只有一个提交
        self.assertEqual(
            len(git(self.repo, "log", "--oneline", "--", "feature-1.txt").splitlines()), 1)
        self.assertFalse(wt.is_dir(), "reconcile 后 u0 worktree 应清理")
        self.assertEqual(plan["units"][1]["state"], "integrated")
        self.assertEqual((self.repo / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")

    def test_crash_before_merge_replays_idempotently(self):
        """prepare 已落盘（dirty 工作树）、merge 从未执行 → 重放幂等集成。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "feature-1.txt").write_text("from unit worker 1\n", "utf-8")  # 未提交
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": wm.rev(wt), "dirty": True, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, info)

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        plan = self.plan(mid)
        u0 = plan["units"][0]
        self.assertEqual(u0["state"], "integrated")
        self.assertEqual(u0["integration"].get("phase"), "cleaned")
        self.assertIn(("replayed"), [d.get("phase") for d in self.integ_events(mid, 0)])
        self.assertEqual((self.repo / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        self.assertEqual(
            len(git(self.repo, "log", "--oneline", "--", "feature-1.txt").splitlines()), 1,
            "重放不得产生重复提交")
        self.assertEqual(plan["units"][1]["state"], "integrated")

    def test_crashed_conflicted_merge_aborted_then_blocks(self):
        """崩溃发生在冲突 merge 中途（MERGE_HEAD 残留）：先 abort 保持 main
        干净，再重放 → 冲突如实落 conflict，mission blocked。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "shared.txt").write_text("from unit worker 1\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit 0 work")
        unit_head = wm.rev(wt)
        (self.repo / "shared.txt").write_text("external\n", "utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "external change")
        proc = subprocess.run(["git", "-C", str(self.repo), "merge",
                               "--no-edit", info["branch"]],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "外部提交后合并应冲突")
        self.assertTrue(git_ok(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                        "崩溃现场应残留 MERGE_HEAD")
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": unit_head, "dirty": False, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, {**info, "headSha": unit_head})

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "blocked")

        u0 = self.plan(mid)["units"][0]
        self.assertEqual(u0["state"], "conflict")
        self.assertFalse(git_ok(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                         "reconcile 后不得残留 MERGE_HEAD")
        self.assertEqual((self.repo / "shared.txt").read_text("utf-8"), "external\n")
        phases = [d.get("phase") for d in self.integ_events(mid, 0)]
        self.assertIn("aborted-stale-merge", phases)
        self.assertIn("conflict", phases)
        state = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertIn("集成冲突", str(state.get("stopReason")))

    def test_stale_index_lock_cleared_on_reconcile(self):
        """硬崩溃残留 index.lock（worktree + 主仓库）→ reconcile 清锁后继续。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "feature-1.txt").write_text("from unit worker 1\n", "utf-8")
        wt_lock = Path(git(wt, "rev-parse", "--absolute-git-dir"), "index.lock")
        wt_lock.write_text("", "utf-8")
        Path(self.repo / ".git" / "index.lock").write_text("", "utf-8")
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": wm.rev(wt), "dirty": True, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, info)

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        cleared = [d for d in self.integ_events(mid, 0)
                   if d.get("phase") == "cleared-stale-locks"]
        self.assertEqual(len(cleared), 1)
        self.assertEqual(len(cleared[0].get("locks") or []), 2,
                         "worktree 与主仓库的锁都应被清除")
        self.assertEqual(self.plan(mid)["units"][0]["state"], "integrated")
        self.assertEqual((self.repo / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")


if __name__ == "__main__":
    unittest.main()
