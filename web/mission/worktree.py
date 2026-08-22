"""WorktreeManager: per-unit git worktrees + mission integration isolation
(P1.2/M3/M5-B/M5-C).

The user's checked-out branch is NEVER touched. Every work unit builds in
its own git worktree inside the mission's namespace
(<runs-root>/.laomo/worktrees/<mission_id>/u<index>, branch
laomo/<mission_id>/u<index>) based on the mission's integration branch
head, so units build on already-integrated work. After a unit's evaluator
says PASS the control plane integrates the unit branch with a plain git
merge into laomo/<mission_id>/integration — executed inside a dedicated
integration worktree, never in the user's working copy — and the
integration branch deliberately survives mission DONE: the user merges it
back explicitly when they choose to. Non-git workspaces (or a workspace
whose repo is unavailable) fall back to the P1.1 behavior: the worker
edits the workspace directory itself and integration is a no-op. A content
conflict aborts the merge so the integration branch stays clean, and M5-C
adds the git primitives the CONTROL PLANE uses to repair automatically
(merge into unit — non-destructive by design): merge_integration_into_unit
materializes the conflict INSIDE the unit worktree and deliberately leaves
it in progress so the resolver worker edits the real conflicted files and
keeps every byte of the unit's PASSed work; unit_merge_state classifies a
crashed unit worktree; has_unresolved_markers judges the resolver's WORKING
files (the worker edits content but never runs git, so the control plane
concludes a resolved merge itself via conclude_unit_merge);
abort_unit_merge backs an exhausted redo out. The
policy — when to repair, how often, when to give up — deliberately stays
in the control plane (this module only serves git truth; see
WorktreeManager.is_merged / MissionRunner._reconcile for the crash-safe
transaction side).

Stale-lock cleanup is ownership-aware (M5-B): only index.lock files inside
THIS mission's worktrees root may be removed; a lock anywhere else (e.g.
the user's main repo) is reported as external and must never be deleted —
the caller fails closed on it.

All operations are idempotent: a crashed run may leave worktrees behind
and `ensure`/`ensure_integration` reuse them (never reset a worktree that
has in-flight commits).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .store import MissionStore


class WorktreeManager:
    """Creates/reuses/integrates one git worktree per work unit index.

    Isolation model (M5-B): the user repo's checked-out branch is read-only
    for the mission. All mission writes land on
    `laomo/<mission_id>/integration` (checked out in its own worktree);
    unit branches `laomo/<mission_id>/u<index>` build on that branch's head.
    """

    COMMIT_USER = ("-c", "user.name=laomo", "-c", "user.email=laomo@local")

    def __init__(self, workspace: str | Path, store: MissionStore,
                 mission_id: str, timeout: int = 60) -> None:
        self.workspace = Path(workspace)
        self.store = store
        self.mission_id = mission_id
        self.timeout = timeout
        self._repo: Path | None = None
        self._available: bool | None = None

    # -- git plumbing ---------------------------------------------------------

    def _git(self, cwd: Path, *args: str) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True, text=True, timeout=self.timeout)
            return proc.returncode == 0, proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)

    @property
    def available(self) -> bool:
        """True when the mission workspace is inside a git repository."""
        if self._available is None:
            ok, top = self._git(self.workspace, "rev-parse", "--show-toplevel")
            self._repo = Path(top) if ok else None
            self._available = self._repo is not None
        return self._available

    def _run(self, *args: str) -> tuple[bool, str]:
        return self._git(self._repo or self.workspace, *args)

    def _worktrees_root(self) -> Path:
        return self.store.root.parent.parent / "worktrees" / self.mission_id

    def _worktree_dir(self, index: int) -> Path:
        return self._worktrees_root() / f"u{index}"

    def integration_dir(self) -> Path:
        return self._worktrees_root() / "integration"

    def _branch(self, index: int) -> str:
        return f"laomo/{self.mission_id}/u{index}"

    @property
    def integration_branch(self) -> str:
        return f"laomo/{self.mission_id}/integration"

    def rev(self, cwd: Path, ref: str = "HEAD") -> str | None:
        ok, sha = self._git(cwd, "rev-parse", ref)
        return sha if ok else None

    # -- integration probes (M5 reconcile) -----------------------------------

    def is_merged(self, sha: str) -> bool:
        """True when `sha` is already reachable from the INTEGRATION branch
        head (the merge landed). Used by crash reconcile to distinguish
        crash-before-merge from crash-after-merge. False when the
        integration branch does not exist (nothing was ever merged)."""
        if not sha:
            return False
        ok, _ = self._run("rev-parse", "-q", "--verify", self.integration_branch)
        if not ok:
            return False
        ok, _ = self._run("merge-base", "--is-ancestor", sha,
                          self.integration_branch)
        return ok

    def merge_in_progress(self) -> bool:
        path = self.integration_dir()
        if not path.is_dir():
            return False
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        return ok

    def abort_merge(self) -> None:
        path = self.integration_dir()
        if not path.is_dir():
            return
        self._git(path, "merge", "--abort")

    def is_dirty(self, index: int) -> bool:
        """True when the unit worktree has uncommitted changes at prepare
        time. A dirty tree means the pre-merge unitHead alone cannot prove
        the merge landed — reconcile must replay the idempotent integrate
        instead of trusting the ancestry probe."""
        ok, out = self._git(self._worktree_dir(index), "status", "--porcelain")
        return bool(ok and out.strip())

    # -- ownership-aware lock handling (M5-B) ---------------------------------

    def lock_report(self, paths: list[Path]) -> dict[str, list[str]]:
        """Classify existing git index.lock files by ownership. A lock is
        "ours" only when its worktree lives under THIS mission's worktrees
        root (unit/integration worktrees the control plane owns); anything
        else — e.g. the user's main repo — is "external" and must NEVER be
        deleted: the caller fails closed on it."""
        ours: list[str] = []
        external: list[str] = []
        root = self._worktrees_root()
        try:
            root = root.resolve()
        except OSError:
            pass
        for path in paths:
            ok, git_dir = self._git(Path(str(path)),
                                    "rev-parse", "--absolute-git-dir")
            if not ok:
                continue
            lock = Path(git_dir) / "index.lock"
            if not lock.is_file():
                continue
            (ours if self._under(path, root) else external).append(str(lock))
        return {"ours": ours, "external": external}

    @staticmethod
    def _under(path: Path, root: Path) -> bool:
        try:
            resolved = Path(path).resolve()
        except OSError:
            return False
        return resolved == root or root in resolved.parents

    def clear_stale_locks(self, paths: list[Path]) -> dict[str, list[str]]:
        """Remove the git index.lock files a hard crash left behind — but
        only the ones this mission owns (under its worktrees root). Locks
        held anywhere else (the user's main repo) are reported as "external"
        and NEVER deleted; the caller must fail closed on them. Returns
        {"removed": [lockpaths], "external": [lockpaths]}."""
        report = self.lock_report(paths)
        removed: list[str] = []
        for lock in report["ours"]:
            try:
                Path(lock).unlink()
                removed.append(lock)
            except OSError:
                pass
        return {"removed": removed, "external": report["external"]}

    # -- integration worktree (M5-B) -------------------------------------------

    def ensure_integration(self,
                           source_sha: str | None = None) -> dict[str, Any] | None:
        """Return {path,branch,baseSha,headSha} for the mission's dedicated
        integration worktree, creating it from `source_sha` (or the user
        repo HEAD) when missing. An existing worktree is reused as-is and
        NEVER reset — in-flight integration history must survive crashes.
        Returns None when the workspace is not a git repo (P1.1 fallback)."""
        if not self.available:
            return None
        path = self.integration_dir()
        if path.is_dir() and self.rev(path):
            # crash reuse: never reset a worktree holding in-flight history
            # (baseSha is reported metadata — source_sha when known — never
            # a reset target)
            head = self.rev(path)
            return {"path": str(path), "branch": self.integration_branch,
                    "baseSha": source_sha or head, "headSha": head}
        start = source_sha or self.rev(self._repo or self.workspace)
        if not start:
            return None
        ok, _ = self._run("worktree", "add", "-B", self.integration_branch,
                          str(path), start)
        if not ok:
            return None
        return {"path": str(path), "branch": self.integration_branch,
                "baseSha": start, "headSha": self.rev(path)}

    # -- per-unit worktree ------------------------------------------------------

    def ensure(self, index: int, title: str | None = None,
               info: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return {path,branch,baseSha,headSha} for the unit's worktree,
        creating it from the INTEGRATION branch head when needed (units
        build on already-integrated work, never on the user's branch).
        Returns None when the workspace is not a git repo (P1.1 fallback)."""
        if not self.available:
            return None
        path = self._worktree_dir(index)
        base = {
            "path": None, "branch": None, "baseSha": None, "headSha": None,
        }
        if path.is_dir() and self.rev(path):
            # crash reuse: never reset a worktree holding in-flight commits
            ok, branch = self._git(path, "rev-parse", "--abbrev-ref", "HEAD")
            head = self.rev(path)
            return {**base, "path": str(path),
                    "branch": branch if ok else self._branch(index),
                    "baseSha": (info or {}).get("baseSha") or head,
                    "headSha": head}
        integ = self.ensure_integration()
        head = (integ or {}).get("headSha")
        if not head:
            return None
        ok, _ = self._run("worktree", "add", "-B", self._branch(index),
                          str(path), head)
        if not ok:
            return None
        return {**base,
                "path": str(path), "branch": self._branch(index),
                "baseSha": head, "headSha": self.rev(path)}

    def refresh_head(self, info: dict[str, Any] | None) -> dict[str, Any] | None:
        if not info or not info.get("path"):
            return info
        head = self.rev(Path(str(info["path"])))
        if head:
            info["headSha"] = head
        return info

    # -- integration -----------------------------------------------------------

    def integrate(self, index: int, title: str | None = None,
                  branch: str | None = None) -> dict[str, Any]:
        """Commit any pending unit-worktree changes and merge the unit branch
        into the mission INTEGRATION branch. The merge runs with
        `git -C <integration_dir>` so it lands on the integration branch —
        the user's checked-out branch is never touched. Returns
        {ok, headSha?, conflict?, reason?}. A content conflict aborts the
        merge (in the integration worktree) so the integration branch stays
        clean; the report says conflict=True and the unit goes to
        `conflict`. Idempotent: replaying after a crash re-commits nothing
        (clean tree) and re-merging an already-merged branch is a no-op
        "Already up to date"."""
        integ = self.ensure_integration()
        if integ is None:
            return {"ok": False, "conflict": False,
                    "reason": "integration worktree 不可用"}
        idir = self.integration_dir()
        path = self._worktree_dir(index)
        branch = branch or self._branch(index)
        if not path.is_dir() or not self.rev(path):
            return {"ok": False, "conflict": False, "reason": "worktree 不存在"}
        # commit pending changes so the branch carries the unit's work
        self._git(path, "add", "-A")
        ok, quiet = self._git(path, "diff", "--cached", "--quiet")
        if not ok:  # non-empty staged tree => commit
            ok, out = self._git(path, *self.COMMIT_USER, "commit",
                                f"-m laomo: unit #{index + 1}", f"-m {title or ''}")
            if not ok:
                return {"ok": False, "conflict": False,
                        "reason": f"worktree 提交失败: {out[:200]}"}
        head = self.rev(path)
        ok, out = self._git(idir, "merge", "--no-edit",
                            f"-m laomo: unit #{index + 1} {title or ''}", branch)
        if ok:
            return {"ok": True, "headSha": head}
        # failure: distinguish a content conflict from other errors
        ok, _ = self._git(idir, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        conflict = ok
        if conflict:
            self._git(idir, "merge", "--abort")
        return {"ok": False, "conflict": conflict,
                "reason": out[:400] or "merge failed"}

    # -- conflict repair primitives (M5-C) -------------------------------------

    def _unmerged_files(self, path: Path) -> list[dict[str, Any]]:
        """Both sides of every unmerged path in a conflicted worktree:
        ours = stage 2 (the unit's PASSed work), theirs = stage 3 (the
        integration side). Blob shas, not content — the resolver worker
        reads the marked-up files in its own worktree."""
        _, out = self._git(path, "diff", "--name-only", "--diff-filter=U")
        files: list[dict[str, Any]] = []
        for name in (ln for ln in out.splitlines() if ln.strip()):
            ours = theirs = None
            _, lsout = self._git(path, "ls-files", "-u", "--", name)
            for line in lsout.splitlines():
                parts = line.partition("\t")[0].split()
                if len(parts) < 3:
                    continue
                if parts[2] == "2":
                    ours = parts[1]
                elif parts[2] == "3":
                    theirs = parts[1]
            files.append({"path": name, "oursSha": ours, "theirsSha": theirs})
        return files

    def merge_integration_into_unit(self, index: int) -> dict[str, Any]:
        """Merge the integration branch INTO the unit worktree so the repair
        keeps every byte of the unit's PASSed work (non-destructive by
        design — no reset, no rebase). On a content conflict the merge is
        deliberately LEFT IN PROGRESS in the unit worktree: the resolver
        worker edits the real conflicted files (markers and all) and
        concludes with a plain `git commit`, after which integrate() merges
        the unit branch fast-forward. Idempotent for the resume path: when
        the worktree already has an unconcluded merge (MERGE_HEAD present —
        a crash or a replay), no new `git merge` runs (git would refuse);
        the CURRENT unmerged state is reported with the exact same shape so
        the manager can refresh its directive without burning the conflict
        budget. Returns {ok, conflict, integrationHead, unitHead, mergeBase,
        files, reason}; ok=True only for a cleanly completed merge (files
        empty), ok=False + conflict=False when the worktree / integration
        branch is missing or git itself errored."""
        path = self._worktree_dir(index)
        shape = {"ok": False, "conflict": False, "integrationHead": None,
                 "unitHead": None, "mergeBase": None,
                 "files": [], "reason": ""}
        if not path.is_dir() or not self.rev(path):
            return {**shape, "reason": "worktree 不存在"}
        # during an unconcluded merge HEAD is still the pre-merge unit head
        unit_head = self.rev(path)
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if ok:  # resume/replay: report the in-progress merge as-is
            merge_head = self.rev(path, "MERGE_HEAD")
            _, merge_base = self._git(path, "merge-base", "HEAD", "MERGE_HEAD")
            return {**shape, "conflict": True, "integrationHead": merge_head,
                    "unitHead": unit_head, "mergeBase": merge_base or None,
                    "files": self._unmerged_files(path),
                    "reason": "merge 尚未结论（崩溃/重放）：报告当前未合并状态"}
        integ_head = self.rev(path, self.integration_branch)
        if not integ_head:
            return {**shape, "unitHead": unit_head, "reason": "集成分支不可用"}
        _, merge_base = self._git(path, "merge-base", "HEAD",
                                  self.integration_branch)
        ok, out = self._git(path, *self.COMMIT_USER, "merge", "--no-edit",
                            "-m", f"laomo: merge integration into unit "
                                  f"#{index + 1}",
                            self.integration_branch)
        if ok:
            return {**shape, "ok": True, "integrationHead": integ_head,
                    "unitHead": unit_head, "mergeBase": merge_base or None,
                    "reason": ""}
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if not ok:  # a plain git error, not a content conflict
            return {**shape, "integrationHead": integ_head,
                    "unitHead": unit_head, "mergeBase": merge_base or None,
                    "reason": out[:400] or "merge failed"}
        # content conflict: keep the merge in progress in the unit worktree
        return {**shape, "conflict": True, "integrationHead": integ_head,
                "unitHead": unit_head, "mergeBase": merge_base or None,
                "files": self._unmerged_files(path),
                "reason": out[:400] or "content conflict"}

    def unit_merge_state(self, index: int) -> str:
        """Crash-classification probe for the unit worktree: \"conflicted\"
        (MERGE_HEAD present — resolution incomplete, whether or not the
        worker already edited the files), \"merged\" (no MERGE_HEAD and HEAD
        is a merge commit that contains the integration branch — the
        resolution was committed), \"clean\" (nothing in progress),
        \"missing\" (worktree gone). Ancestry alone cannot mean \"merged\"
        because every unit branch is cut FROM the integration head and thus
        always contains it; the merge-commit shape (2+ parents) is the
        discriminator, so a normal unit with plain commits stays \"clean\".
        Classification is against the CURRENT integration branch (a merge
        concluded against an older integration head that has since advanced
        reads as \"clean\" — the manager's tx record holds that history)."""
        path = self._worktree_dir(index)
        if not path.is_dir() or not self.rev(path):
            return "missing"
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if ok:
            return "conflicted"
        ok, parents = self._git(path, "rev-list", "--parents", "-n", "1", "HEAD")
        if ok and len(parents.split()) >= 3:  # HEAD is a merge commit
            ok, _ = self._git(path, "merge-base", "--is-ancestor",
                              self.integration_branch, "HEAD")
            if ok:
                return "merged"
        return "clean"

    def has_unresolved_markers(self, index: int) -> bool:
        """True when the unit worktree has an unconcluded merge AND at least
        one unmerged path's WORKING-FILE still carries conflict markers —
        the resolver worker has not actually finished. The worker edits
        files but never runs git (the control plane owns git), so the index
        stages may still record conflicts even after a finished resolution:
        judge the file CONTENT, not the index. Only the unambiguous marker
        starts are checked (`<<<<<<<` / `>>>>>>>`); a bare `=======` line is
        legal content (e.g. markdown setext headings) and must not trip
        this probe."""
        path = self._worktree_dir(index)
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if not ok:
            return False
        ok, out = self._git(path, "diff", "--name-only", "--diff-filter=U")
        if not ok:
            return False
        for rel in out.splitlines():
            if not rel.strip():
                continue
            try:
                text = (path / rel).read_text("utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith(("<<<<<<<", ">>>>>>>")):
                    return True
        return False

    def conclude_unit_merge(self, index: int) -> dict[str, Any]:
        """Control-plane conclusion of a RESOLVED merge: the resolver worker
        is forbidden from git, so the control plane stages the edited files
        and commits — during MERGE_HEAD this creates the merge commit (the
        prepared MERGE_MSG), after which integrate() fast-forwards the
        integration branch. Returns {ok, headSha, reason}; never raises."""
        path = self._worktree_dir(index)
        ok, _ = self._git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
        if not ok:
            return {"ok": False, "headSha": None, "reason": "无进行中的 merge"}
        self._git(path, "add", "-A")
        ok, out = self._git(path, *self.COMMIT_USER, "commit", "--no-edit")
        head = self.rev(path)
        if not ok or not head:
            return {"ok": False, "headSha": head,
                    "reason": (out or "commit failed")[:300]}
        return {"ok": True, "headSha": head, "reason": ""}

    def abort_unit_merge(self, index: int) -> bool:
        """`git merge --abort` in the unit worktree — used when the redo
        budget is exhausted so the unit branch/worktree stay pristine at
        their pre-merge head for a human. False when there is no worktree
        or no merge in progress to abort."""
        path = self._worktree_dir(index)
        if not path.is_dir():
            return False
        return self._git(path, "merge", "--abort")[0]

    def cleanup(self, index: int, branch: str | None = None) -> None:
        """Remove the unit worktree and its branch (after a successful
        integration). The integration worktree/branch are deliberately NOT
        removed — they survive mission DONE until the user merges the
        integration branch back explicitly. Harmless when nothing exists."""
        path = self._worktree_dir(index)
        if path.is_dir():
            self._run("worktree", "remove", "--force", str(path))
        self._run("branch", "-D", branch or self._branch(index))
        self._run("worktree", "prune")
