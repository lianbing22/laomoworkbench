"""CodexSkillsClient: native skills surface (skills/list + skills/config/write)
over the adapter's codex_request channel. 老墨不发明自己的 skill 格式 —
skills are SKILL.md directories discovered by Codex itself
(docs/extension-contract.md "Skills" section, probed live against
codex-cli 0.149.0-alpha.4.1).

Probed protocol facts this module relies on:
  skills/list {cwds?: [path], forceReload?: bool}
    -> {data: [{cwd: str, skills: [{name, description, path, scope, enabled}]}]}
  skills/config/write {enabled: bool, name?: str, path?: str}
    -> {effectiveEnabled: bool}
  unknown method -> -32600 "unknown variant ..." (capability detection)
"""
from __future__ import annotations

from typing import Any, Callable

from .models import (CapabilityUnavailable, ExtensionError, PostconditionFailed,
                     is_capability_error)

# light sanity bounds on selector strings we forward into a config write;
# codex owns the real format — these only stop obvious garbage
MAX_NAME_LEN = 128
MAX_PATH_LEN = 1024


def _no_control_chars(value: str) -> bool:
    return not any(ch in value for ch in "\x00\r\n\t")


class CodexSkillsClient:
    """Thin, honest wrapper. `transport` is CodexRuntimeAdapter.codex_request
    (or a test double with the same signature)."""

    def __init__(self, transport: Callable[..., Any]) -> None:
        self._call = transport

    def _rpc(self, method: str, params: dict[str, Any] | None = None,
             timeout: float = 30.0) -> Any:
        try:
            return self._call(method, params, timeout=timeout)
        except (TimeoutError, RuntimeError) as exc:
            if is_capability_error(exc):
                raise CapabilityUnavailable(f"{method}: {exc}") from exc
            raise ExtensionError(f"{method}: {exc}", "upstream-error") from exc

    # -- read side ----------------------------------------------------------

    def list(self, cwd: str | None, force_reload: bool = False) -> dict[str, Any]:
        """skills/list flattened for the UI. Same-cwd groups merge; every
        entry keeps its origin cwd. Scope values are upstream's own
        (user/project/system) — never synthesized."""
        params: dict[str, Any] = {}
        if cwd:
            params["cwds"] = [cwd]
        if force_reload:
            params["forceReload"] = True
        result = self._rpc("skills/list", params, timeout=60.0)
        data = (result or {}).get("data") or []
        skills: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        by_scope: dict[str, int] = {}
        enabled_count = 0
        for group in data:
            if not isinstance(group, dict):
                continue
            group_cwd = group.get("cwd")
            entries = [e for e in (group.get("skills") or []) if isinstance(e, dict)]
            for entry in entries:
                scope = str(entry.get("scope") or "user")
                enabled = bool(entry.get("enabled", True))
                by_scope[scope] = by_scope.get(scope, 0) + 1
                enabled_count += int(enabled)
                skills.append({
                    "name": entry.get("name"),
                    "description": entry.get("description"),
                    "path": entry.get("path"),
                    "scope": scope,
                    "enabled": enabled,
                    "cwd": group_cwd,
                })
            groups.append({"cwd": group_cwd, "count": len(entries)})
        # disabled entries float to the top (they are the ones the user last
        # acted on / may want to revisit), then plain alphabetical
        skills.sort(key=lambda s: (s["enabled"], str(s["name"] or "")))
        return {
            "skills": skills,
            "groups": groups,
            "counts": {"total": len(skills), "enabled": enabled_count,
                       "byScope": by_scope},
        }

    # -- mutation (postcondition-verified) ------------------------------------
    # WRITE (skills/config/write) -> REFETCH (skills/list forceReload, same
    # cwds scope) -> VERIFY the entry shows the requested state. Upstream's
    # own effectiveEnabled is reported alongside but never trusted alone.

    def set_enabled(self, enabled: bool, name: str | None = None,
                    path: str | None = None,
                    cwd: str | None = None) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ExtensionError("enabled 必须是布尔值", "invalid-argument")
        if bool(name) == bool(path):
            raise ExtensionError(
                "需要 name 或 path 恰好一个作为选择器", "invalid-argument")
        params: dict[str, Any] = {"enabled": enabled}
        if name:
            if len(name) > MAX_NAME_LEN or not _no_control_chars(name):
                raise ExtensionError("无效的 skill name", "invalid-argument")
            params["name"] = name
        else:
            assert path is not None
            if len(path) > MAX_PATH_LEN or not _no_control_chars(path) \
                    or not path.startswith("/"):
                raise ExtensionError("无效的 skill path（需要绝对路径）",
                                     "invalid-argument")
            params["path"] = path
        result = self._rpc("skills/config/write", params, timeout=30.0) or {}
        effective = result.get("effectiveEnabled")
        refreshed = self.list(cwd, force_reload=True)
        entry = self._find(refreshed["skills"], name, path)
        if entry is None:
            raise PostconditionFailed(
                f"Codex 接受了请求，但 skill {name or path} 未出现在列表。")
        if entry["enabled"] != enabled:
            note = ""
            if isinstance(effective, bool) and effective != enabled:
                note = f"（effectiveEnabled={effective}）"
            raise PostconditionFailed(
                f"Codex 接受了请求，但 skill {entry['name']} 的状态没有变化。{note}")
        return {"skill": entry, "effectiveEnabled": entry["enabled"],
                "counts": refreshed["counts"]}

    @staticmethod
    def _find(skills: list[dict[str, Any]], name: str | None,
              path: str | None) -> dict[str, Any] | None:
        for entry in skills:
            if name is not None and entry.get("name") == name:
                return entry
            if path is not None and entry.get("path") == path:
                return entry
        return None
