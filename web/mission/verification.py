"""Harness Verification Gate: machine-only checks, evidence persisted."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from .models import VERIFY_CMD_TIMEOUT, VERIFY_TAIL, _atomic_write, _now_ms, _tail


# --- Harness Verification Gate --------------------------------------------------------


class VerificationRunner:
    """Machine verification executed by the Control Plane (no model involved).

    Config (from mission.json -> verification):
      commands:      list of shell commands; passed iff exitCode == 0
      requiredFiles: list of paths (relative to mission cwd); passed iff file exists
      httpChecks:    list of {url, expectStatus?}; passed iff status == expectStatus (default 200)

    Every check is persisted under <run>/verification/ with full fields
    (kind/name/command/exitCode/stdoutTail/stderrTail/startedAt/endedAt/resultHash)
    plus raw stdout/stderr. The gate itself is resumable and re-runnable —
    it only ever reads the workspace and writes under the run dir.
    """

    def __init__(self, store: MissionStore, config: dict[str, Any], cwd: str) -> None:
        self.store = store
        self.config = config or {}
        self.cwd = cwd

    def run(self) -> dict[str, Any]:
        started = _now_ms()
        checks = (self._run_commands() + self._check_files() + self._check_http())
        passed = all(c.get("passed") for c in checks)
        summary = {"passed": passed, "checks": checks,
                   "startedAt": started, "endedAt": _now_ms(),
                   # M5-B: prove WHICH tree the gate ran against (the
                   # integration workspace for git missions, mission cwd
                   # otherwise) — verifiable evidence, not a guess.
                   "cwd": self.cwd}
        summary["resultHash"] = hashlib.sha256(json.dumps(
            [c.get("resultHash") or "" for c in checks], sort_keys=True).encode()).hexdigest()[:16]
        self.store.verification_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(summary, ensure_ascii=False, indent=1)
        _atomic_write(self.store.verification_dir / "results.json", text)
        # keep every gate run: evidence must survive a later PASS overwrite
        _atomic_write(self.store.verification_dir / f"results-{started}.json", text)
        return summary

    def _record(self, kind: str, name: str, check: dict[str, Any],
                stdout: str = "", stderr: str = "") -> dict[str, Any]:
        result = {
            "kind": kind, "name": name, "passed": bool(check.get("passed")),
            "startedAt": check.get("startedAt"), "endedAt": check.get("endedAt"),
            "error": check.get("error"),
            "stdoutTail": _tail(stdout), "stderrTail": _tail(stderr),
        }
        if "command" in check:
            result["command"] = check["command"]
        if "exitCode" in check:
            result["exitCode"] = check["exitCode"]
        if "url" in check:
            result["url"] = check["url"]
        result["resultHash"] = hashlib.sha256(json.dumps(
            {k: v for k, v in result.items() if k not in ("startedAt", "endedAt")},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        raw_dir = self.store.verification_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{len(list(raw_dir.glob('*'))) // 2}-{name}")[:80]
        try:
            (raw_dir / f"{slug}.stdout").write_text(stdout, "utf-8")
            (raw_dir / f"{slug}.stderr").write_text(stderr, "utf-8")
        except OSError:
            pass
        return result

    def _run_commands(self) -> list[dict[str, Any]]:
        out = []
        for cmd in (self.config.get("commands") or []):
            started = _now_ms()
            entry: dict[str, Any] = {"command": str(cmd), "startedAt": started}
            try:
                proc = subprocess.run(["/bin/zsh", "-lc", str(cmd)], cwd=self.cwd,
                                      capture_output=True, text=True, timeout=VERIFY_CMD_TIMEOUT)
                entry["exitCode"] = proc.returncode
                entry["passed"] = proc.returncode == 0
                stdout, stderr = proc.stdout or "", proc.stderr or ""
            except subprocess.TimeoutExpired as exc:
                entry["exitCode"] = None
                entry["passed"] = False
                entry["error"] = f"timeout>{VERIFY_CMD_TIMEOUT}s"
                stdout, stderr = (exc.stdout or ""), (exc.stderr or "")
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", "replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
            except OSError as exc:
                entry["exitCode"] = None
                entry["passed"] = False
                entry["error"] = f"spawn: {exc}"
                stdout, stderr = "", ""
            entry["endedAt"] = _now_ms()
            out.append(self._record("command", str(cmd)[:80], entry, stdout, stderr))
        return out

    def _check_files(self) -> list[dict[str, Any]]:
        out = []
        for rel in (self.config.get("requiredFiles") or []):
            started = _now_ms()
            path = Path(str(rel))
            if not path.is_absolute():
                path = Path(self.cwd) / path
            exists = path.is_file()
            out.append(self._record(
                "file", str(rel)[:80],
                {"passed": exists, "startedAt": started, "endedAt": _now_ms(),
                 "error": None if exists else "missing"}))
        return out

    def _check_http(self) -> list[dict[str, Any]]:
        out = []
        for spec in (self.config.get("httpChecks") or []):
            started = _now_ms()
            url = str(spec.get("url") or "")
            expect = int(spec.get("expectStatus") or 200)
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    status = resp.status
                passed = status == expect
                error = None if passed else f"status {status} != {expect}"
            except OSError as exc:
                status, passed, error = None, False, f"{type(exc).__name__}: {exc}"
            out.append(self._record(
                "http", url[:80],
                {"passed": passed, "url": url, "startedAt": started,
                 "endedAt": _now_ms(), "error": error,
                 "status": status, "expectStatus": expect}))
        return out


def _git_diff_summary(cwd: str, base_ref: str | None = None) -> str | None:
    """Uncommitted diff of `cwd` (P1.1 evidence), or — with base_ref (M5-B
    integration missions) — the committed diff base_ref..HEAD, which is the
    honest summary of what the mission's integration branch carries."""
    args = ["git", "-C", cwd, "diff", "--stat"]
    if base_ref:
        args.append(f"{base_ref}..HEAD")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout or "").strip()
    return text or None


