"""WorktreeManager: per-unit git worktrees + mission integration isolation
(P1.2/M3/M5-B).

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
edits the workspace directory itself and integration is a no-op. Conflicts
from integration are reported, not resolved — the mission blocks on the
unit's `conflict` state with the merge aborted (a resolver is deliberately
out of scope; M5 instead made integration a crash-safe transaction: see
WorktreeManager.is_merged / MissionRunner._reconcile).

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
