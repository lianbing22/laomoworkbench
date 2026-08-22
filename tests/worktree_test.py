"""P1.2/M3/M5-B tests: per-unit git worktrees + mission integration isolation.

Real git repositories are created inside tmp dirs; the same FakeAdapter
role-dispatch style as mission_test.py drives the mission (no codex).
Covered contract:

* every unit builds in its own git worktree (path/branch/baseSha recorded,
  branch laomo/<mission_id>/u<index>) and worker/evaluator/background-job
  cwd all point into that worktree
* the user's checked-out branch is NEVER touched: all merges land on the
  mission integration branch laomo/<mission_id>/integration inside a
  dedicated integration worktree; after DONE the integration branch and
  worktree survive (the user merges them back explicitly later)
* units base on the integration branch head, so the next unit's base is
  the previous unit's integrated head (first unit: the initial sha)
* an integration conflict (interference on the integration branch) marks
  the unit `conflict` and blocks the mission (merge aborted in the
  integration worktree; the integration branch stays clean)
* non-git workspaces keep the P1.1 behavior (no worktree, no integration)
* crash-resume never re-runs a passed unit's worker turn (integrated unit
  hits the scheduler's finish-fast-path); the four reconcile cases adopt
  from integration-branch git truth (crashed merges land there too)
* stale-lock cleanup is ownership-aware: locks inside the mission's
  worktrees root are removed; a lock in the user's main repo is external
  and NEVER deleted
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

    def wroot(self, mid):
        """The mission's worktrees root: <repo>/.laomo/worktrees/<mid>/."""
        return self.repo / ".laomo" / "worktrees" / mid

    def intdir(self, mid):
        """The mission's dedicated integration worktree directory."""
        return self.wroot(mid) / "integration"

    def intbranch(self, mid):
        return f"laomo/{mid}/integration"

    def show(self, mid, relpath):
        """File content as committed on the integration branch (stripped)."""
        return git(self.repo, "show", f"{self.intbranch(mid)}:{relpath}")

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
        self.assertEqual(u0["worktree"]["branch"], f"laomo/{mid}/u0")
        self.assertEqual(u1["worktree"]["branch"], f"laomo/{mid}/u1")
        # 单元基于集成分支头创建：首个单元基于初始提交，后续基于已集成头
        self.assertEqual(u0["worktree"]["baseSha"], self.initial_sha)
        self.assertEqual(u1["worktree"]["baseSha"], u0["worktree"]["headSha"])
        # worker/evaluator cwd 都在单元工作树中，而不是 mission 工作区
        worker_cwds = [c["cwd"] for c in self.adapter.calls_for("worker")]
        self.assertEqual(len(worker_cwds), 2)
        self.assertTrue(worker_cwds[0].endswith(f"/u0"), worker_cwds)
        self.assertTrue(worker_cwds[1].endswith("/u1"), worker_cwds)
        eval_cwds = [c["cwd"] for c in self.adapter.calls_for("evaluator")]
        self.assertTrue(eval_cwds[0].endswith("/u0"), eval_cwds)
        self.assertTrue(eval_cwds[1].endswith("/u1"), eval_cwds)
        # 用户分支隔离：主仓库 HEAD 保持初始提交，工作树不被写入
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha,
                         "用户当前分支绝不能被 mission 改动")
        self.assertFalse((self.repo / "feature-1.txt").exists(),
                         "用户工作树不得出现单元产物")
        # 集成结果落在集成分支/集成工作树上
        integ = self.intdir(mid)
        self.assertTrue(integ.is_dir(), "集成工作树应存在")
        self.assertEqual((integ / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        self.assertEqual((integ / "feature-2.txt").read_text("utf-8"),
                         "from unit worker 2\n")
        self.assertEqual(git(self.repo, "rev-parse", self.intbranch(mid)),
                         u1["worktree"]["headSha"])
        self.assertEqual(self.show(mid, "feature-2.txt"), "from unit worker 2")
        log = git(self.repo, "log", "--oneline", self.intbranch(mid), "-3")
        self.assertIn("laomo: unit #1", log)
        self.assertIn("laomo: unit #2", log)
        # 单元工作树在集成后被清理
        for u in plan["units"]:
            self.assertFalse(
                Path(u["worktree"]["path"]).is_dir(),
                f"集成后工作树应被清理: {u['worktree']['path']}")
        kinds = [(e["type"], (e["detail"] or {}).get("unit"), (e["detail"] or {}).get("phase"))
                 for e in self.events(mid) if e["type"] == "integration"]
        self.assertEqual(kinds, [("integration", 0, "start"), ("integration", 0, "integrated"),
                                 ("integration", 1, "start"), ("integration", 1, "integrated")])

    def test_user_branch_never_touched(self):
        self.script_two_units()
        mid = self.create()
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        # 用户分支从未被碰：HEAD 不动、历史不增、工作树干净
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "1")
        self.assertFalse((self.repo / "feature-1.txt").exists())
        self.assertFalse((self.repo / "feature-2.txt").exists())
        # DONE 后集成分支与集成工作树保留，等待用户显式合并回去
        branches = git(self.repo, "branch", "--list", "laomo/*")
        self.assertIn(self.intbranch(mid), branches,
                      f"DONE 后集成分支应保留: {branches!r}")
        self.assertNotIn(f"laomo/{mid}/u0", branches, "单元分支应已清理")
        self.assertNotIn(f"laomo/{mid}/u1", branches, "单元分支应已清理")
        self.assertTrue(self.intdir(mid).is_dir(), "集成工作树应在 DONE 后保留")
        # 集成分支承载全部成果，用户可随时显式合并
        self.assertEqual(self.show(mid, "feature-1.txt"), "from unit worker 1")
        self.assertEqual(self.show(mid, "feature-2.txt"), "from unit worker 2")

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
        # 作业产物随单元集成到集成分支（用户主仓库不动）
        self.assertTrue((self.intdir(mid) / "job-marker.txt").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

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
        self.adapter.shared = True
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.script("worker", handoff_text(note="单元完成。"),
                            handoff_text(note="单元完成。"))
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        mid = self.create()
        integ = self.intdir(mid)

        def on_worker(prompt, cwd, n):
            # 单元 #2（index 1）开工后，集成分支被外部提交改动同一文件
            if "单元 #2" in (prompt or ""):
                with open(integ / "shared.txt", "a", encoding="utf-8") as fh:
                    fh.write("external\n")
                git(integ, "add", "-A")
                git(integ, "commit", "-q", "-m", "external change")

        self.adapter.on_worker = on_worker
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
        # merge 已 abort：集成工作树无冲突残留，集成分支保持外部提交内容
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertNotIn("from unit worker 2", (integ / "shared.txt").read_text("utf-8"))
        self.assertNotIn("from unit worker 2", self.show(mid, "shared.txt"))
        # 用户主仓库全程未被触碰
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)
        self.assertFalse((self.repo / "shared.txt").exists())


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
        # 集成成果在集成分支上，用户主仓库不动
        self.assertEqual((self.intdir(mid) / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)


class IntegrationReconcileTest(WorktreeTest):
    """P1.2/M5: integration is a write-ahead transaction; a crash between
    `git merge` (into the INTEGRATION branch, in the integration worktree)
    and the plan.json write is reconciled from git truth.

    * git merge landed on the integration branch + crash -> plan says
      `integrating` -> restart adopts the merge (no duplicate commits, no
      re-merge)
    * crash before the merge -> replay the idempotent integrate
    * crash mid-conflicted-merge (MERGE_HEAD left in the integration
      worktree) -> abort first, replay, land on `conflict` honestly
    * stale git index.lock after a hard crash -> mission-owned locks are
      cleared; a lock in the user's main repo is external and survives
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
        """M5 核心：git merge 已成功合入集成分支 → 进程崩溃 → plan.json 仍写
        integrating。重启后以 git 为真相直接采纳合并结果（不重放、不产生新
        提交）。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "feature-1.txt").write_text("from unit worker 1\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit 0 work")
        unit_head = wm.rev(wt)
        # 崩溃的进程：已把 u0 分支合入集成分支（FF），但没来得及写回 plan.json
        integ = wm.integration_dir()
        git(integ, "merge", "--no-edit", info["branch"])
        self.assertEqual(git(integ, "rev-parse", "HEAD"), unit_head)
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
        # 采纳而非重放：feature-1 在集成分支历史中只有一个提交
        self.assertEqual(
            len(git(self.repo, "log", "--oneline", wm.integration_branch,
                    "--", "feature-1.txt").splitlines()), 1)
        self.assertFalse(wt.is_dir(), "reconcile 后 u0 worktree 应清理")
        self.assertEqual(plan["units"][1]["state"], "integrated")
        self.assertEqual((integ / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        # 用户主仓库 HEAD 全程未动
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

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
        integ = wm.integration_dir()
        self.assertEqual((integ / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        self.assertEqual(
            len(git(self.repo, "log", "--oneline", wm.integration_branch,
                    "--", "feature-1.txt").splitlines()), 1,
            "重放不得产生重复提交")
        self.assertEqual(plan["units"][1]["state"], "integrated")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

    def test_crashed_conflicted_merge_aborted_then_blocks(self):
        """崩溃发生在冲突 merge 中途（集成工作树残留 MERGE_HEAD）：先 abort
        保持集成分支干净，再重放 → 冲突如实落 conflict，mission blocked。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "shared.txt").write_text("from unit worker 1\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit 0 work")
        unit_head = wm.rev(wt)
        # 外部干扰提交到集成分支，崩溃进程在集成工作树里的 merge 冲突中断
        integ = wm.integration_dir()
        (integ / "shared.txt").write_text("external\n", "utf-8")
        git(integ, "add", "-A")
        git(integ, "commit", "-q", "-m", "external change")
        proc = subprocess.run(["git", "-C", str(integ), "merge",
                               "--no-edit", info["branch"]],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "外部提交后合并应冲突")
        self.assertTrue(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                        "崩溃现场应残留 MERGE_HEAD")
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": unit_head, "dirty": False, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, {**info, "headSha": unit_head})

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "blocked")

        u0 = self.plan(mid)["units"][0]
        self.assertEqual(u0["state"], "conflict")
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                         "reconcile 后不得残留 MERGE_HEAD")
        self.assertEqual((integ / "shared.txt").read_text("utf-8"), "external\n")
        self.assertEqual(self.show(mid, "shared.txt"), "external")
        phases = [d.get("phase") for d in self.integ_events(mid, 0)]
        self.assertIn("aborted-stale-merge", phases)
        self.assertIn("conflict", phases)
        state = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertIn("集成冲突", str(state.get("stopReason")))
        # 用户主仓库全程未被触碰
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)
        self.assertFalse((self.repo / "shared.txt").exists())

    def test_stale_index_lock_cleared_on_reconcile(self):
        """硬崩溃残留 index.lock：mission 自有 worktree 锁被清除；用户主
        仓库的锁是 external —— fail closed：不删除、集成等待（带退避重试），
        锁释放后自动继续完成。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "feature-1.txt").write_text("from unit worker 1\n", "utf-8")
        wt_lock = Path(git(wt, "rev-parse", "--absolute-git-dir"), "index.lock")
        wt_lock.write_text("", "utf-8")
        repo_lock = self.repo / ".git" / "index.lock"
        repo_lock.write_text("", "utf-8")
        tx = {"phase": "prepared", "branch": info["branch"],
              "unitHead": wm.rev(wt), "dirty": True, "startedAt": 1}
        self.craft_crash(mid, "integrating", tx, info)

        self.mgr.start(mid)
        # 外部锁存在期间：fail closed —— external-lock 事件出现、锁未被删、
        # 单元停在 integrating、mission 不终结
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(d.get("phase") == "external-lock"
                   for d in self.integ_events(mid, 0)):
                break
            time.sleep(POLL_INTERVAL)
        self.assertTrue(
            any(d.get("phase") == "external-lock" for d in self.integ_events(mid, 0)),
            "外部锁应触发 external-lock（fail closed）: %r" % self.integ_events(mid, 0))
        self.assertTrue(repo_lock.exists(), "用户主仓库的锁绝不能被删除")
        self.assertFalse(wt_lock.exists(), "mission 自有的 worktree 锁应被清除")
        self.assertEqual(self.plan(mid)["units"][0]["state"], "integrating")
        self.assertEqual(
            (self.mgr.status(mid).get("mission") or {}).get("state"), "running",
            "外部锁等待期间 mission 保持存活（不终结）")
        cleared = [d for d in self.integ_events(mid, 0)
                   if d.get("phase") == "cleared-stale-locks"]
        self.assertTrue(cleared, "应记录锁分类事件")
        first = cleared[0]
        self.assertEqual(len(first.get("removed") or []), 1,
                         "只清除 mission 自有的 worktree 锁: %r" % (first,))
        self.assertEqual(len(first.get("external") or []), 1,
                         "主仓库锁应报告为 external: %r" % (first,))
        # 用户侧锁消失（其它工具操作完成）→ 退避重试后自动续上并完成
        repo_lock.unlink()
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done")
        self.assertEqual(self.plan(mid)["units"][0]["state"], "integrated")
        integ = wm.integration_dir()
        self.assertEqual((integ / "feature-1.txt").read_text("utf-8"),
                         "from unit worker 1\n")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)


class LockOwnershipTest(WorktreeTest):
    """M5-B lock policy: mission-owned locks are removable, everything else
    (above all the user's main repo) is external and must survive."""

    def test_user_repo_lock_is_external_and_survives(self):
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        integ = wm.integration_dir()

        wt_lock = Path(git(wt, "rev-parse", "--absolute-git-dir"), "index.lock")
        wt_lock.write_text("", "utf-8")
        integ_lock = Path(git(integ, "rev-parse", "--absolute-git-dir"), "index.lock")
        integ_lock.write_text("", "utf-8")
        # 用 git 自己解析的绝对 git-dir 定位主仓库锁（macOS /var 符号链接一致）
        repo_lock = Path(git(self.repo, "rev-parse", "--absolute-git-dir"),
                         "index.lock")
        repo_lock.write_text("", "utf-8")

        report = wm.lock_report([wt, integ, self.repo])
        self.assertEqual(sorted(report["ours"]),
                         sorted([str(wt_lock), str(integ_lock)]),
                         "mission 工作树（单元+集成）的锁属于 ours: %r" % (report,))
        self.assertEqual(report["external"], [str(repo_lock)],
                         "用户主仓库的锁必须归类 external: %r" % (report,))

        cleared = wm.clear_stale_locks([wt, integ, self.repo])
        self.assertEqual(sorted(cleared["removed"]),
                         sorted([str(wt_lock), str(integ_lock)]))
        self.assertEqual(cleared["external"], [str(repo_lock)])
        self.assertFalse(wt_lock.exists(), "mission 工作树锁应被清除")
        self.assertFalse(integ_lock.exists(), "集成工作树锁应被清除")
        self.assertTrue(repo_lock.exists(), "用户主仓库的锁绝不能被删除")


class VerificationWorkspaceTest(WorktreeTest):
    """M5-B ⑤: final machine verification and the fresh final evaluator run
    against the INTEGRATION workspace for git missions; a git repo that
    cannot produce it fails closed; a pre-existing integration worktree is
    reused (never recreated) after a crash."""

    def test_machine_gate_runs_in_integration_workspace(self):
        self.script_two_units()
        mid = self.create(verification={
            "commands": ["test -f feature-1.txt", "test -f feature-2.txt"],
            "requiredFiles": ["feature-1.txt"],
        })
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done")

        st = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertEqual(st.get("verifyResult"), "pass")
        results = json.loads((self.mdir(mid) / "verification" / "results.json")
                             .read_text("utf-8"))
        integ = self.intdir(mid)
        self.assertEqual(Path(str(results.get("cwd"))).resolve(), integ.resolve(),
                         "机器门禁必须在集成工作树上执行（证据化 cwd）: %r" % results.get("cwd"))
        # 用户工作树上没有产物 —— 门禁若跑错树必然失败
        self.assertFalse((self.repo / "feature-1.txt").exists())
        self.assertTrue((integ / "feature-1.txt").is_file())
        self.assertTrue(results.get("passed"))

    def test_final_evaluator_runs_in_integration_workspace(self):
        self.script_two_units()
        mid = self.create()
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid), "done")

        calls = self.adapter.calls_for("evaluator")
        self.assertTrue(calls, "final evaluator 应已运行")
        final = calls[-1]
        self.assertTrue(final["cwd"].endswith("/integration"),
                        "final evaluator cwd 应为集成工作树: %r" % final["cwd"])
        self.assertEqual(Path(final["cwd"]).resolve(),
                         self.intdir(mid).resolve())
        self.assertNotEqual(Path(final["cwd"]).resolve(),
                            Path(str(self.repo)).resolve(),
                            "final evaluator 绝不跑在用户工作区上")
        self.assertIn("工作区：" + final["cwd"], final["prompt"])
        self.assertTrue(json.loads((self.mdir(mid) / "verdicts" / "final.json")
                                   .read_text("utf-8")).get("verdict") == "PASS")

    def _craft_verification_state(self, mid):
        """Hand-write a post-crash disk state: both units integrated, mission
        already in the verification phase."""
        store = self.mgr.store_for(mid)
        plan = {
            "version": 2, "replans": 0, "gitIntegration": True,
            "units": [
                {"id": "a", "index": 0, "title": "单元A", "description": "",
                 "acceptance": ["x"], "dependencies": [],
                 "state": "integrated", "status": "integrated",
                 "attempt": 1, "repairCount": 0,
                 "worktree": {"path": None, "branch": None,
                              "baseSha": None, "headSha": None},
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": "PASS", "integration": {"phase": "cleaned"},
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "b", "index": 1, "title": "单元B", "description": "",
                 "acceptance": ["y"], "dependencies": ["a"],
                 "state": "integrated", "status": "integrated",
                 "attempt": 1, "repairCount": 0,
                 "worktree": {"path": None, "branch": None,
                              "baseSha": None, "headSha": None},
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": "PASS", "integration": {"phase": "cleaned"},
                 "worker": {"startedAt": 1, "finishedAt": 2}},
            ],
        }
        store.save_plan(plan)
        store.save_state({"state": "verification", "cycles": 2, "currentUnit": 1,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        self.adapter.defaults["worker"] = handoff_text(note="单元完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])

    def test_verification_fails_closed_without_integration_worktree(self):
        """git 仓库但 ensure_integration 失败：机器门禁与 final evaluator 都
        拒绝执行（fail closed），绝不退回用户工作区。"""
        mid = self.create()
        self._craft_verification_state(mid)
        orig = WorktreeManager.ensure_integration
        WorktreeManager.ensure_integration = \
            lambda self, source_sha=None: None
        try:
            self.mgr.start(mid)
            state = self.wait_terminal(mid, timeout=30)
        finally:
            WorktreeManager.ensure_integration = orig
        self.assertEqual(state, "failed")
        st = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertIn("集成工作区不可用", str(st.get("stopReason")))
        self.assertNotIn("verifyResult", st, "门禁绝不在用户工作树上偷偷执行")
        self.assertFalse((self.mdir(mid) / "verification" / "results.json").exists())
        self.assertFalse((self.mdir(mid) / "verdicts" / "final.json").exists())

    def test_crashed_verification_reuses_integration_worktree(self):
        """崩溃后重入 verification：已存在的集成工作树被复用（不重建、不
        重置），门禁与 final evaluator 在同一棵树上完成。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        first = wm.ensure_integration(self.initial_sha)
        self.assertTrue(first and first["path"])
        integ = Path(first["path"])
        (integ / "probe.txt").write_text("survives\n", "utf-8")
        self._craft_verification_state(mid)

        self.mgr.start(mid)
        self.assertEqual(
            self.wait_terminal(mid, timeout=30), "done",
            "重入 verification 应照常完成: %r" % self.mgr.status(mid))

        again = wm.ensure_integration(self.initial_sha)
        self.assertEqual(Path(again["path"]).resolve(), integ.resolve(),
                         "集成工作树必须被复用而不是重建")
        self.assertEqual((integ / "probe.txt").read_text("utf-8"), "survives\n",
                         "复用不得重置工作树内容")
        self.assertEqual(git(integ, "rev-parse", "--abbrev-ref", "HEAD"),
                         self.intbranch(mid))
        st = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertEqual(st.get("verifyResult"), "pass")
        self.assertTrue((self.mdir(mid) / "verdicts" / "final.json").exists())
        results = json.loads((self.mdir(mid) / "verification" / "results.json")
                             .read_text("utf-8"))
        self.assertEqual(Path(str(results.get("cwd"))).resolve(), integ.resolve())

    def test_evidence_manifest_reads_integration_tree(self):
        self.script_two_units()
        mid = self.create(verification={"requiredFiles": ["feature-1.txt"]})
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done")

        # manifest snapshots state.json itself, so it lands milliseconds
        # AFTER the done transition — wait for it instead of racing the write
        manifest_path = self.mdir(mid) / "evidence" / "manifest.json"
        deadline = time.time() + 10
        while time.time() < deadline and not manifest_path.is_file():
            time.sleep(POLL_INTERVAL)
        self.assertTrue(manifest_path.is_file(), "DONE 后应落盘 evidence manifest")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        entry = (manifest.get("entries") or {}).get("artifact/feature-1.txt")
        self.assertTrue(entry, "manifest 应收录 artifact: %r" % manifest.get("entries", {}))
        self.assertFalse(entry.get("missing"))
        self.assertEqual(Path(entry["path"]).resolve(),
                         (self.intdir(mid) / "feature-1.txt").resolve(),
                         "artifact 必须从集成工作树解析")
        self.assertEqual(len(entry.get("sha256") or ""), 64)
        diff = self.mdir(mid) / "evidence" / "git-diff.txt"
        self.assertTrue(diff.is_file(), "应产出 git-diff 证据")
        text = diff.read_text("utf-8")
        self.assertIn("feature-1.txt", text, "diff 应体现集成分支携带的工作")


if __name__ == "__main__":
    unittest.main()
