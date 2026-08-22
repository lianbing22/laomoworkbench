"""WorktreeManager: per-unit git worktrees (P1.2/M3).

Every work unit builds in its own git worktree, created at the recorded base
commit of the mission branch; after the unit's evaluator says PASS the
control plane integrates the unit branch (a plain git merge) back into the
mission branch, so the next unit starts from an updated base. Non-git
workspaces (or a workspace whose repo is unavailable) fall back to the P1.1
behavior: the worker edits the workspace directory itself and integration is
a no-op. Conflicts from integration are reported, not resolved — the
ConflictResolver arrives in M5.

All operations are idempotent: a crashed run may leave a worktree behind and
`ensure` reuses it (never resets a worktree that has in-flight commits).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .store import MissionStore


class WorktreeManager:
    """Creates/reuses/integrates one git worktree per work unit index."""

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

    def _worktree_dir(self, index: int) -> Path:
        return self.store.root.parent.parent / "worktrees" / self.mission_id / f"u{index}"

    def _branch(self, index: int) -> str:
        return f"laomo/u{index}"

    def rev(self, cwd: Path, ref: str = "HEAD") -> str | None:
        ok, sha = self._git(cwd, "rev-parse", ref)
        return sha if ok else None

    # -- per-unit worktree ------------------------------------------------------

    def ensure(self, index: int, title: str | None = None,
               info: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return {path,branch,baseSha,headSha} for the unit's worktree,
        creating it from the current mission HEAD if needed. Returns None
        when the workspace is not a git repo (P1.1 fallback)."""
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
        head = self.rev(self._repo or self.workspace)
        if head is None:
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
        """Commit any pending worktree changes and merge the unit branch into
        the mission branch (the mission branch is the repo's current branch).
        Returns {ok, headSha?, conflict?, reason?}. A content conflict aborts
        the merge so the mission branch stays clean for the ConflictResolver
        (M5); the report says conflict=True and the unit goes to `conflict`."""
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
        ok, out = self._run("merge", "--no-edit",
                            f"-m laomo: unit #{index + 1} {title or ''}", branch)
        if ok:
            return {"ok": True, "headSha": head}
        # failure: distinguish a content conflict from other errors
        conflict = False
        ok, _ = self._run("rev-parse", "-q", "--verify", "MERGE_HEAD")
        conflict = ok
        if conflict:
            self._run("merge", "--abort")
        return {"ok": False, "conflict": conflict,
                "reason": out[:400] or "merge failed"}

    def cleanup(self, index: int, branch: str | None = None) -> None:
        """Remove the unit worktree and its branch (after a successful
        integration). Harmless when nothing exists."""
        path = self._worktree_dir(index)
        if path.is_dir():
            self._run("worktree", "remove", "--force", str(path))
        self._run("branch", "-D", branch or self._branch(index))
        self._run("worktree", "prune")
