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

    def integ_events(self, mid, unit):
        return [e["detail"] for e in self.events(mid)
                if e["type"] == "integration"
                and isinstance(e.get("detail"), dict)
                and e["detail"].get("unit") == unit]

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




def resolve_conflict_file(path):
    """Test stand-in for the resolver worker: concatenate both sides of git
    conflict markers (ours then theirs). Returns True when markers were
    found and resolved."""
    try:
        text = Path(path).read_text("utf-8")
    except OSError:
        return False
    if "<<<<<<<" not in text:
        return False
    out, mode, ours, theirs = [], None, [], []
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            mode = "ours"
            continue
        if line.startswith("=======") and mode == "ours":
            mode = "theirs"
            continue
        if line.startswith(">>>>>>>") and mode == "theirs":
            out.extend(ours)
            out.extend(theirs)
            ours, theirs, mode = [], [], None
            continue
        if mode == "ours":
            ours.append(line)
        elif mode == "theirs":
            theirs.append(line)
        else:
            out.append(line)
    Path(path).write_text("\n".join(out) + "\n", "utf-8")
    return True


class WorktreeConflictTest(WorktreeTest):
    """M5-C: an integration conflict no longer blocks the mission — it is
    materialized INTO the unit worktree (merge left in progress), the
    resolver worker resolves there, the unit evaluator must PASS again, and
    only then does integration re-run (clean fast-forward)."""

    def test_conflicting_integration_auto_resolves_and_completes(self):
        self.adapter.shared = True
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.script("worker", handoff_text(note="单元完成。"),
                            handoff_text(note="单元完成。"),
                            handoff_text(note="冲突已解决。"))
        self.adapter.defaults["worker"] = handoff_text(note="冲突已解决。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        mid = self.create()
        integ = self.intdir(mid)
        resolved = {"n": 0}

        def on_worker(prompt, cwd, n):
            p2 = "单元 #2" in (prompt or "")
            # 单元 #2 首轮开工后，集成分支被外部提交改动同一文件
            if p2 and "集成冲突" not in (prompt or ""):
                with open(integ / "shared.txt", "a", encoding="utf-8") as fh:
                    fh.write("external\n")
                git(integ, "add", "-A")
                git(integ, "commit", "-q", "-m", "external change")
            # resolver 轮：真实 worker 会就地解决冲突标记；WritingAdapter
            # 会整文件覆写 shared.txt，这里直接写出合并结果代替
            if p2 and "集成冲突" in (prompt or ""):
                base = (integ / "shared.txt").read_text("utf-8")
                (Path(cwd) / "shared.txt").write_text(
                    base + "from unit worker 2\n", "utf-8")
                resolved["n"] += 1

        self.adapter.on_worker = on_worker
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done")

        plan = self.plan(mid)
        u0, u1 = plan["units"][0], plan["units"][1]
        self.assertEqual(u0["state"], "integrated")
        self.assertEqual(u1["state"], "integrated")
        self.assertEqual(int(u1.get("conflictCount") or 0), 1,
                         "恰好一次冲突自动解决")
        self.assertEqual(resolved["n"], 1, "resolver worker 应解决一次标记")
        conflict = u1.get("conflict") or {}
        self.assertTrue(conflict.get("integrationHead"))
        self.assertTrue(conflict.get("mergeBase"))
        self.assertEqual([f.get("path") for f in conflict.get("files") or []],
                         ["shared.txt"], "冲突记录应精确到文件与两侧 blob")
        phases = [d.get("phase") for d in self.integ_events(mid, 1)]
        self.assertIn("conflict-resolve", phases)
        # 集成分支无 MERGE_HEAD 残留；双方内容都保留
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        merged = self.show(mid, "shared.txt")
        self.assertIn("external", merged, "集成侧内容必须保留")
        self.assertIn("from unit worker 2", merged, "单元侧内容必须保留")
        # Resolver 之后必须重新过 unit evaluator（gate 5）
        u1_evals = [c for c in self.adapter.calls_for("evaluator")
                    if str(c.get("cwd") or "").endswith("/u1")]
        self.assertEqual(len(u1_evals), 2,
                         "冲突解决后必须重新验收：实际 %d 次" % len(u1_evals))
        # M5-C.1：冲突解决不消耗 evaluator 修复预算
        self.assertEqual(int(u1.get("repairCount") or 0), 0,
                         "conflictCount 与 repairCount 必须真正独立")
        # M5-C.1：resolver 轮使用专用 prompt（git 禁令，无普通 worker 的
        # “改动请提交在该分支上”矛盾指令）
        u1_workers = [c for c in self.adapter.calls_for("worker")
                      if str(c.get("cwd") or "").endswith("/u1")]
        resolver_prompts = [c for c in u1_workers
                            if "集成冲突" in (c.get("prompt") or "")]
        self.assertTrue(resolver_prompts, "resolver 轮应存在")
        self.assertIn("Conflict Resolver", resolver_prompts[0]["prompt"])
        self.assertIn("禁止执行", resolver_prompts[0]["prompt"])
        self.assertNotIn("改动请提交在该分支上", resolver_prompts[0]["prompt"])
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

    def test_crashed_conflicted_merge_aborted_then_auto_resolves(self):
        """崩溃发生在冲突 merge 中途（集成工作树残留 MERGE_HEAD）：先 abort
        保持集成分支干净，重放遇到冲突 → M5-C 自动解决（非破坏性）→ DONE。"""
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

        # M5-C：崩溃恢复重放遇到冲突不再 blocked —— 冲突物化进单元工作树，
        # resolver 轮把两侧内容合并写入，重新验收后干净集成
        def on_worker(prompt, cwd, n):
            if "集成冲突" in (prompt or ""):
                (Path(cwd) / "shared.txt").write_text(
                    "external\nfrom unit worker 1\n", "utf-8")
        self.adapter.on_worker = on_worker

        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done")

        u0 = self.plan(mid)["units"][0]
        self.assertEqual(u0["state"], "integrated")
        self.assertEqual(int(u0.get("conflictCount") or 0), 1)
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                         "reconcile 后不得残留 MERGE_HEAD")
        # 双方内容都保留在集成分支上
        self.assertEqual(self.show(mid, "shared.txt"),
                         "external\nfrom unit worker 1")
        phases = [d.get("phase") for d in self.integ_events(mid, 0)]
        self.assertIn("aborted-stale-merge", phases)
        self.assertIn("conflict-resolve", phases)
        self.assertIn("integrated", phases)
        state = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertFalse(state.get("stopReason"), "自动解决后不应有 stopReason")
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


class DependencyBarrierDurableTest(WorktreeTest):
    """Gate B hardening: while a PASSED unit awaits integration (a durable
    in-between state — e.g. held by a test hook or an exotic crash window),
    an idle scheduler must keep waiting, never declare the DAG
    unsatisfiable; once it integrates, the dependent proceeds."""

    def test_passed_awaiting_integration_does_not_block_dag(self):
        from mission import manager as manager_mod
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        (Path(info["path"]) / "deps").mkdir(exist_ok=True)
        (Path(info["path"]) / "deps" / "a.txt").write_text("REAL-A\n", "utf-8")
        self.assertTrue(wm.integrate(0, "单元A", branch=info["branch"])["ok"])
        wm.cleanup(0, branch=info["branch"])
        info_b = wm.ensure(1, "单元B")
        (Path(info_b["path"]) / "deps").mkdir(exist_ok=True)
        (Path(info_b["path"]) / "deps" / "b.txt").write_text("REAL-B\n", "utf-8")
        plan = {
            "version": 2, "replans": 0, "gitIntegration": True,
            "units": [
                {"id": "a", "index": 0, "title": "产物A", "description": "",
                 "acceptance": ["x"], "dependencies": [],
                 "state": "integrated", "status": "integrated",
                 "attempt": 1, "repairCount": 0, "conflictCount": 0,
                 "conflict": None,
                 "worktree": info, "jobId": None, "delta": None,
                 "repairDirective": None, "lastVerdict": "PASS",
                 "integration": {"phase": "cleaned"},
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "b", "index": 1, "title": "产物B", "description": "",
                 "acceptance": ["y"], "dependencies": [],
                 "state": "passed", "status": "passed",
                 "attempt": 1, "repairCount": 0, "conflictCount": 0,
                 "conflict": None,
                 "worktree": info_b,
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": "PASS", "integration": None,
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "c", "index": 2, "title": "合成C", "description": "产出 c.txt",
                 "acceptance": ["c.txt 存在"], "dependencies": ["a", "b"],
                 "state": "pending", "status": "pending",
                 "attempt": 0, "repairCount": 0, "conflictCount": 0,
                 "conflict": None,
                 "worktree": {"path": None, "branch": None,
                              "baseSha": None, "headSha": None},
                 "jobId": None, "delta": None, "repairDirective": None,
                 "lastVerdict": None, "integration": None,
                 "worker": {"startedAt": None, "finishedAt": None}},
            ],
        }
        store.save_plan(plan)
        store.save_state({"state": "running", "cycles": 2, "currentUnit": 2,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})
        orig = manager_mod.MissionRunner._integrate_harvested
        hold = {"on": True}

        def holding(self, state, index):
            if index == 1 and hold["on"]:
                return "ok"  # keep B passed: the durable in-between window
            return orig(self, state, index)

        manager_mod.MissionRunner._integrate_harvested = holding
        try:
            self.mgr.start(mid)
            deadline = time.time() + 6
            while time.time() < deadline:
                st = (self.mgr.status(mid).get("mission") or {}).get("state")
                if st in ("blocked", "failed", "cancelled"):
                    break
                time.sleep(0.1)
            st = (self.mgr.status(mid).get("mission") or {}).get("state")
            self.assertEqual(st, "running",
                             "B=passed 等待集成期间不得宣判 DAG 死锁（终态=%s）" % st)
            self.assertEqual(self.plan(mid)["units"][2]["state"], "pending",
                             "C 在 B 未 integrated 前必须停在 pending")
            hold["on"] = False  # release: next pass integrates B for real
            self.assertEqual(self.wait_terminal(mid, timeout=30), "done")
            final = [u["state"] for u in self.plan(mid)["units"]]
            self.assertEqual(final, ["integrated"] * 3)
        finally:
            manager_mod.MissionRunner._integrate_harvested = orig


class ConflictResolverGatesTest(WorktreeTest):
    """M5-C gates: second race during resolve, budget exhaustion, crash
    during the resolver — the resolver always works in the UNIT worktree
    and never loses non-conflicting work."""

    def test_second_race_during_resolve_enters_round_two(self):
        """Gate 6：B 解决冲突期间集成分支再次前进（C 已集成）→ B 再集成时
        二次冲突 → 第二轮解决 → 双方全部保留。同时锁定 Gate 4：resolver
        只发生在单元工作树，集成工作树内容在解决期间不被改动。"""
        mid = self.create()
        integ = self.intdir(mid)
        snapshots = []

        def on_worker(prompt, cwd, n):
            p2 = "单元 #2" in (prompt or "")
            if not p2:
                return
            if "集成冲突" not in (prompt or ""):
                with open(integ / "shared.txt", "a", encoding="utf-8") as fh:
                    fh.write("external-1\n")
                git(integ, "add", "-A")
                git(integ, "commit", "-q", "-m", "external 1")
                return
            if "第 1/2 次" in (prompt or ""):
                # B 在解决期间，集成分支又前进了（第二轮竞争）
                with open(integ / "shared.txt", "a", encoding="utf-8") as fh:
                    fh.write("external-2\n")
                git(integ, "add", "-A")
                git(integ, "commit", "-q", "-m", "external 2")
            # Gate 4：resolver 只允许在单元工作树；记录集成侧快照
            snapshots.append((integ / "shared.txt").read_text("utf-8"))
            base = (integ / "shared.txt").read_text("utf-8")
            (Path(cwd) / "shared.txt").write_text(
                base + "from unit worker 2\n", "utf-8")
            self.assertTrue(str(cwd).endswith("/u1"),
                            "resolver 必须发生在单元工作树: %s" % cwd)

        self.adapter.shared = True
        self.adapter.on_worker = on_worker
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.defaults["worker"] = handoff_text(note="完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=40), "done")

        plan = self.plan(mid)
        u1 = plan["units"][1]
        self.assertEqual(u1["state"], "integrated")
        self.assertEqual(int(u1.get("conflictCount") or 0), 2,
                         "二次竞争必须进入第二轮解决: %r" % u1.get("conflict"))
        self.assertEqual(int(u1.get("repairCount") or 0), 0,
                         "两轮冲突解决都不得消耗 repairCount")
        phases = [d.get("phase") for d in self.integ_events(mid, 1)]
        self.assertEqual(phases.count("conflict-resolve"), 2)
        merged = self.show(mid, "shared.txt")
        for piece in ("external-1", "external-2", "from unit worker 2"):
            self.assertIn(piece, merged, "三方内容都必须保留: %r" % merged)
        # Gate 3：集成分支无 MERGE_HEAD 残留
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

    def test_budget_exhausted_blocks_with_evidence_preserved(self):
        """Gate 7：每轮都产生新冲突 → conflictCount 超过预算 → 单元诚实落
        conflict、mission blocked；单元工作树/分支/冲突证据全部保留。"""
        mid = self.create()
        integ = self.intdir(mid)

        def on_worker(prompt, cwd, n):
            if "单元 #2" not in (prompt or ""):
                return
            # 每一轮都在集成分支上追加外部改动，确保永远冲突
            with open(integ / "shared.txt", "a", encoding="utf-8") as fh:
                fh.write("external-%d\n" % n)
            git(integ, "add", "-A")
            git(integ, "commit", "-q", "-m", "external %d" % n)

        self.adapter.shared = True
        self.adapter.on_worker = on_worker
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.defaults["worker"] = handoff_text(note="完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=40), "blocked")

        u1 = self.plan(mid)["units"][1]
        self.assertEqual(u1["state"], "conflict")
        self.assertEqual(int(u1.get("conflictCount") or 0), 2,
                         "恰好两次自动解决尝试；第三次冲突不再记账直接停止")
        conflict = u1.get("conflict") or {}
        self.assertTrue(conflict.get("files"), "冲突证据必须保留")
        # 预算耗尽后单元工作树被还原到冲突前状态（无标记、无 MERGE_HEAD）
        wt = Path(u1["worktree"]["path"])
        self.assertTrue(wt.is_dir(), "单元工作树必须保留给人接管")
        self.assertFalse(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        shared = (wt / "shared.txt").read_text("utf-8")
        self.assertNotIn("<<<<<<<", shared, "不得残留冲突标记")
        self.assertFalse(git_ok(integ, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        # 集成分支保持外部内容；用户主仓库未被触碰
        self.assertIn("external", self.show(mid, "shared.txt"))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)
        state = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertIn("集成冲突", str(state.get("stopReason")))

    def test_binary_conflict_fails_closed_for_human(self):
        """M5-C.1 P0：二进制冲突没有文本标记，绝不能被解释成“已解决”而
        自动 commit——不支持的冲突类型 fail closed：还原单元现场、保留证
        据、诚实 blocked 等人接管。"""
        mid = self.create()
        integ = self.intdir(mid)

        def on_worker(prompt, cwd, n):
            if "单元 #2" not in (prompt or ""):
                return
            if "集成冲突" in (prompt or ""):
                # resolver 轮：二进制冲突无从下手（真实 worker 也会卡住）
                return
            # 单元B 写二进制文件；外部同时在集成分支写不同二进制
            (Path(cwd) / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nunit-side")
            with open(integ / "logo.png", "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\nexternal-side")

            def g(*a):
                pass
            git(integ, "add", "-A")
            git(integ, "commit", "-q", "-m", "external binary")

        self.adapter.on_worker = on_worker
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.defaults["worker"] = handoff_text(note="完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=40), "blocked")

        u1 = self.plan(mid)["units"][1]
        self.assertEqual(u1["state"], "conflict")
        self.assertEqual((u1.get("integration") or {}).get("phase"),
                         "conflict-unsupported")
        phases = [d.get("phase") for d in self.integ_events(mid, 1)]
        self.assertIn("conflict-unsupported", phases)
        # fail closed：单元现场还原（无 MERGE_HEAD、二进制仍是单元版本）
        wt = Path(u1["worktree"]["path"])
        self.assertTrue(wt.is_dir())
        self.assertFalse(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertEqual((wt / "logo.png").read_bytes(),
                         b"\x89PNG\r\n\x1a\nunit-side")
        # 集成分支绝未被自动接受单元侧二进制（读集成工作树字节对比）
        self.assertEqual((integ / "logo.png").read_bytes(),
                         b"\x89PNG\r\n\x1a\nexternal-side")
        state = json.loads((self.mdir(mid) / "state.json").read_text("utf-8"))
        self.assertIn("集成冲突", str(state.get("stopReason")))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

    def test_conclude_failure_keeps_resolving_then_completes(self):
        """M5-C.1 P1：conclude_unit_merge 失败不得被下一层 git 操作兜底——
        停留 resolving（同一尝试），恢复后照常完成。"""
        mid = self.create()
        integ = self.intdir(mid)
        calls = {"n": 0}
        orig = WorktreeManager.conclude_unit_merge

        def flaky(self, index):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "headSha": None, "reason": "模拟 git 锁"}
            return orig(self, index)

        def on_worker(prompt, cwd, n):
            p2 = "单元 #2" in (prompt or "")
            if p2 and "集成冲突" not in (prompt or ""):
                (integ / "shared.txt").write_text("external\n", "utf-8")
                git(integ, "add", "-A")
                git(integ, "commit", "-q", "-m", "external")
            if p2 and "集成冲突" in (prompt or ""):
                base = (integ / "shared.txt").read_text("utf-8")
                (Path(cwd) / "shared.txt").write_text(
                    base + "from unit worker 2\n", "utf-8")

        self.adapter.shared = True
        self.adapter.on_worker = on_worker
        self.adapter.script("planner", plan_block(UNIT_PLAN_2))
        self.adapter.defaults["worker"] = handoff_text(note="完成。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        WorktreeManager.conclude_unit_merge = flaky
        try:
            self.mgr.start(mid)
            self.assertEqual(self.wait_terminal(mid, timeout=40), "done",
                             "conclude 一次失败后应继续解决并完成: %r"
                             % self.mgr.status(mid))
        finally:
            WorktreeManager.conclude_unit_merge = orig
        self.assertGreaterEqual(calls["n"], 2, "失败后必须重试 conclude")
        u1 = self.plan(mid)["units"][1]
        self.assertEqual(u1["state"], "integrated")
        merged = self.show(mid, "shared.txt")
        self.assertIn("external", merged)
        self.assertIn("from unit worker 2", merged)

    def test_crash_during_resolver_resumes_and_completes(self):
        """Gate 8：网关死在 resolver 中途（单元 resolving、工作树停在冲突
        merge 状态）→ 重启后从现场继续解决，不从头开发。"""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        # 手工构造崩溃现场：u0 已集成；u1 已完成自己的工作并进入冲突解决
        info0 = wm.ensure(0, "单元A")
        (Path(info0["path"]) / "feature-1.txt").write_text("u0\n", "utf-8")
        self.assertTrue(wm.integrate(0, "单元A", branch=info0["branch"])["ok"])
        wm.cleanup(0, branch=info0["branch"])
        info1 = wm.ensure(1, "单元B")
        u1wt = Path(info1["path"])
        (u1wt / "shared.txt").write_text("from unit worker 2\n", "utf-8")
        git(u1wt, "add", "-A")
        git(u1wt, "commit", "-q", "-m", "unit 1 work")
        integ = wm.integration_dir()
        (integ / "shared.txt").write_text("external\n", "utf-8")
        git(integ, "add", "-A")
        git(integ, "commit", "-q", "-m", "external change")
        mres = wm.merge_integration_into_unit(1)
        self.assertTrue(mres["conflict"], "现场应停在冲突 merge 状态")
        plan = {
            "version": 2, "replans": 0, "gitIntegration": True,
            "units": [
                {"id": "a", "index": 0, "title": "单元A", "description": "",
                 "acceptance": ["x"], "dependencies": [],
                 "state": "integrated", "status": "integrated",
                 "attempt": 1, "repairCount": 0, "conflictCount": 0,
                 "conflict": None,
                 "worktree": info0, "jobId": None, "delta": None,
                 "repairDirective": None, "lastVerdict": "PASS",
                 "integration": {"phase": "cleaned"},
                 "worker": {"startedAt": 1, "finishedAt": 2}},
                {"id": "b", "index": 1, "title": "单元B", "description": "",
                 "acceptance": ["y"], "dependencies": ["a"],
                 "state": "resolving", "status": "resolving",
                 "attempt": 1, "repairCount": 0, "conflictCount": 1,
                 "conflict": {"attempt": 1, "files": mres["files"],
                              "integrationHead": mres["integrationHead"],
                              "unitHead": mres["unitHead"],
                              "mergeBase": mres["mergeBase"]},
                 "worktree": info1, "jobId": None, "delta": None,
                 "repairDirective": "集成冲突：请解决 shared.txt 的冲突标记。",
                 "lastVerdict": "PASS",
                 "integration": {"phase": "conflict-resolve"},
                 "worker": {"startedAt": 1, "finishedAt": 2}},
            ],
        }
        store.save_plan(plan)
        store.save_state({"state": "running", "cycles": 2, "currentUnit": 1,
                          "noProgress": 0, "progressSignature": "",
                          "tokensUsed": 0, "wallElapsedMs": 0,
                          "agentActiveMs": 0, "waitingMs": 0, "pausedMs": 0,
                          "phaseStartedAt": 0})

        def on_worker(prompt, cwd, n):
            if "集成冲突" in (prompt or ""):
                # 真实冲突标记在现场文件里：就地合并两侧
                self.assertTrue(resolve_conflict_file(Path(cwd) / "shared.txt"),
                                "崩溃现场应保留真实冲突标记")
        self.adapter.on_worker = on_worker
        self.adapter.defaults["worker"] = handoff_text(note="冲突已解决。")
        self.adapter.defaults["evaluator"] = verdict_block("PASS", ["条件满足"])
        self.mgr.start(mid)
        self.assertEqual(self.wait_terminal(mid, timeout=30), "done",
                         "重启后应从冲突现场继续完成: %r" % self.mgr.status(mid))

        u1 = self.plan(mid)["units"][1]
        self.assertEqual(u1["state"], "integrated")
        self.assertEqual(int(u1.get("conflictCount") or 0), 1,
                         "崩溃恢复不得额外烧冲突预算")
        merged = self.show(mid, "shared.txt")
        self.assertIn("external", merged)
        self.assertIn("from unit worker 2", merged)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)


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


class WorktreeConflictHelpersTest(WorktreeTest):
    """M5-C: non-destructive conflict repair — the merge is materialized
    INSIDE the unit worktree so the resolver worker keeps every byte of the
    unit's PASSed work.

    * merge_integration_into_unit: conflict=True leaves the merge in
      progress (MERGE_HEAD present, markers in the files) and reports the
      unmerged paths with both sides' blob shas; non-conflicting files stay
      byte-identical
    * a plain resolve+commit concludes the merge (unit_merge_state
      "merged") and wm.integrate then lands fast-forward
    * abort_unit_merge returns the worktree to its pre-merge head
    * unit_merge_state classifies clean / merged / missing
    * the report is idempotent: replaying against an unconcluded merge
      returns the same shape without running a second git merge
    """

    def make_divergent_unit(self):
        """u0 commits unit work (base.txt edit + 4 new files); the
        integration branch then gets an external commit editing base.txt
        DIFFERENTLY — exactly one conflicting file, base.txt. Returns
        (wm, mid, info, wt, integ)."""
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        (wt / "base.txt").write_text("unit base\n", "utf-8")
        for i in range(1, 5):
            (wt / f"f{i}.txt").write_text(f"unit file {i}\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit 0 work")
        integ = self.intdir(mid)
        (integ / "base.txt").write_text("integ base\n", "utf-8")
        git(integ, "add", "-A")
        git(integ, "commit", "-q", "-m", "external change")
        return wm, mid, info, wt, integ

    def test_merge_into_unit_conflict_reports_both_blob_shas(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        unit_head = git(wt, "rev-parse", "HEAD")
        integ_sha = git(self.repo, "rev-parse", self.intbranch(mid))
        before = {f"f{i}.txt": (wt / f"f{i}.txt").read_bytes()
                  for i in range(1, 5)}

        res = wm.merge_integration_into_unit(0)
        self.assertFalse(res["ok"])
        self.assertTrue(res["conflict"])
        self.assertEqual(res["unitHead"], unit_head)
        self.assertEqual(res["integrationHead"], integ_sha)
        self.assertEqual(res["mergeBase"], self.initial_sha)
        ours = git(self.repo, "rev-parse", f"{info['branch']}:base.txt")
        theirs = git(self.repo, "rev-parse", f"{self.intbranch(mid)}:base.txt")
        self.assertNotEqual(ours, theirs, "两侧 blob 必须不同才构成冲突")
        self.assertEqual(res["files"],
                         [{"path": "base.txt", "oursSha": ours,
                           "theirsSha": theirs}])
        # 冲突留在单元工作树里：MERGE_HEAD 在、真实文件带冲突标记
        self.assertTrue(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertIn("<<<<<<<", (wt / "base.txt").read_text("utf-8"))
        # 非冲突文件逐字节未动（单元已 PASS 的工作不被破坏）
        for name, blob in before.items():
            self.assertEqual((wt / name).read_bytes(), blob,
                             f"非冲突文件不得被改动: {name}")
        # 失败形态：不存在的单元 worktree / 缺少集成分支
        miss = wm.merge_integration_into_unit(42)
        self.assertFalse(miss["ok"])
        self.assertFalse(miss["conflict"])
        self.assertTrue(miss.get("reason"))

    def test_resolved_unit_merge_completes_and_integrates(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        res = wm.merge_integration_into_unit(0)
        self.assertTrue(res["conflict"])
        # resolver worker 编辑真实冲突文件后普通提交（结论化 merge）
        (wt / "base.txt").write_text("resolved\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "resolve conflict")
        self.assertFalse(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        self.assertEqual(wm.unit_merge_state(0), "merged")
        # 集成成为干净的 fast-forward（集成分支已被单元包含）
        integ_res = wm.integrate(0, "单元A", branch=info["branch"])
        self.assertTrue(integ_res.get("ok"), integ_res)
        self.assertEqual(self.show(mid, "base.txt"), "resolved")
        self.assertEqual(self.show(mid, "f1.txt"), "unit file 1")
        # 用户主仓库全程未被触碰
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha)

    def test_abort_unit_merge_restores_pre_merge_state(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        pre = git(wt, "rev-parse", "HEAD")
        res = wm.merge_integration_into_unit(0)
        self.assertTrue(res["conflict"])

        self.assertTrue(wm.abort_unit_merge(0))
        self.assertFalse(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                         "abort 后不得残留 MERGE_HEAD")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), pre,
                         "abort 应回到 merge 前的单元头")
        self.assertEqual((wt / "base.txt").read_text("utf-8"), "unit base\n",
                         "冲突文件应恢复为单元一侧的内容")
        self.assertEqual(wm.unit_merge_state(0), "clean")
        # 不存在的单元 → False（无可 abort）
        self.assertFalse(wm.abort_unit_merge(42))

    def test_unit_merge_state_classifications(self):
        mid = self.create()
        store = self.mgr.store_for(mid)
        wm = WorktreeManager(str(self.repo), store, mid)
        info = wm.ensure(0, "单元A")
        wt = Path(info["path"])
        # 全新单元（恰在集成头上）与带普通提交的单元都是 clean：
        # 单元分支本来就切自集成头，包含集成分支不等于“已结论 merge”
        self.assertEqual(wm.unit_merge_state(0), "clean")
        (wt / "f1.txt").write_text("unit file 1\n", "utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "unit work")
        self.assertEqual(wm.unit_merge_state(0), "clean")
        # 工作树不存在 → missing
        self.assertEqual(wm.unit_merge_state(9), "missing")
        # 集成分支前进（不相交文件）→ 干净合并成一个 merge commit → merged
        integ = self.intdir(mid)
        (integ / "integ-extra.txt").write_text("extra\n", "utf-8")
        git(integ, "add", "-A")
        git(integ, "commit", "-q", "-m", "external extra")
        unit_head = git(wt, "rev-parse", "HEAD")
        res = wm.merge_integration_into_unit(0)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["conflict"])
        self.assertEqual(res["files"], [])
        self.assertEqual(res["unitHead"], unit_head)
        self.assertTrue((wt / "integ-extra.txt").exists(),
                        "干净合并应带入集成侧内容")
        self.assertEqual(wm.unit_merge_state(0), "merged")

    def test_merge_report_is_idempotent_on_unconcluded_merge(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        first = wm.merge_integration_into_unit(0)
        self.assertTrue(first["conflict"])
        # 第二次调用（resume 路径）：不得再跑 git merge（git 也会拒绝），
        # 而是以完全相同的形状报告当前未合并状态
        second = wm.merge_integration_into_unit(0)
        self.assertFalse(second["ok"])
        self.assertTrue(second["conflict"])
        for key in ("unitHead", "integrationHead", "mergeBase", "files"):
            self.assertEqual(second[key], first[key], key)
        self.assertTrue(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                        "重放后 merge 仍应在进行中")
        self.assertEqual(wm.unit_merge_state(0), "conflicted")

    def test_marker_probe_and_control_plane_conclusion(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        # 无 merge 进行中 → 无未解决标记
        self.assertFalse(wm.has_unresolved_markers(0))
        res = wm.merge_integration_into_unit(0)
        self.assertTrue(res["conflict"])
        self.assertTrue(wm.has_unresolved_markers(0),
                        "带冲突标记的工作文件应被判为未解决")
        # resolver 只编辑文件、不跑 git：内容已解决但 merge 仍未结论
        (wt / "base.txt").write_text("resolved\n", "utf-8")
        self.assertFalse(wm.has_unresolved_markers(0),
                         "工作文件已无标记 → 已解决（index 仍报 unmerged 也不影响）")
        self.assertTrue(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                        "结论前 MERGE_HEAD 应仍在")
        self.assertEqual(wm.unit_merge_state(0), "conflicted")
        # 控制面代为结论：add -A + commit --no-edit（用 MERGE_MSG 成 merge 提交）
        done = wm.conclude_unit_merge(0)
        self.assertTrue(done["ok"], done)
        self.assertTrue(done["headSha"])
        self.assertFalse(git_ok(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"),
                         "结论后 MERGE_HEAD 应消失")
        parents = git(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertGreaterEqual(len(parents), 3, "结论后 HEAD 应为 merge commit")
        self.assertEqual(parents[0], done["headSha"])
        self.assertEqual(wm.unit_merge_state(0), "merged")
        # 结论之后的集成干净通过（集成分支被单元包含 → fast-forward）
        integ_res = wm.integrate(0, "单元A", branch=info["branch"])
        self.assertTrue(integ_res.get("ok"), integ_res)
        self.assertEqual(self.show(mid, "base.txt"), "resolved")
        self.assertEqual(self.show(mid, "f1.txt"), "unit file 1")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.initial_sha,
                         "用户主仓库绝不能被触碰")

    def test_conclude_unit_merge_fails_without_merge_in_progress(self):
        wm, mid, info, wt, integ = self.make_divergent_unit()
        done = wm.conclude_unit_merge(0)
        self.assertFalse(done["ok"])
        self.assertIsNone(done["headSha"])
        self.assertTrue(done.get("reason"))
        # 不存在的单元 worktree 同样 ok=False（永不抛异常）
        miss = wm.conclude_unit_merge(42)
        self.assertFalse(miss["ok"])
        self.assertIsNone(miss["headSha"])
        self.assertTrue(miss.get("reason"))
        self.assertFalse(wm.has_unresolved_markers(42))


if __name__ == "__main__":
    unittest.main()
