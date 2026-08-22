"""McpConfigService: MCP server configuration over Codex config RPCs.

Never edits ~/.codex/config.toml as text — every mutation goes through
config/value/write (+ config/mcpServer/reload) and is postcondition-verified
by re-reading config/read. Field names are strictly the upstream snake_case
schema (docs/extension-contract.md): stdio {command, args, cwd, env, enabled,
startup_timeout_sec} and streamable HTTP {url, bearer_token_env_var, enabled}.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .models import (CapabilityUnavailable, ExtensionError, PostconditionFailed,
                     secret_risk_warnings, validate_mcp_name)

_URL_RE = re.compile(r"^https?://[^\s]+$")
_UPSTREAM_UNKNOWN_HINTS = ("unknown variant", "unknown method", "unrecognized")

# keys the upstream adds on read (defaults) — ours to ignore when diffing
_UPSTREAM_DEFAULT_KEYS = {"environment_id", "tool_timeout_sec"}


def _is_capability_error(exc: BaseException) -> bool:
    return any(h in str(exc) for h in _UPSTREAM_UNKNOWN_HINTS)


class McpConfigService:
    def __init__(self, transport: Callable[..., Any]) -> None:
        self._call = transport

    def _rpc(self, method: str, params: dict[str, Any] | None = None,
             timeout: float = 30.0) -> Any:
        try:
            return self._call(method, params, timeout=timeout)
        except (TimeoutError, RuntimeError) as exc:
            if _is_capability_error(exc):
                raise CapabilityUnavailable(f"{method}: {exc}") from exc
            raise ExtensionError(f"{method}: {exc}", "upstream-error") from exc

    # -- read ----------------------------------------------------------------

    def configured(self) -> dict[str, Any]:
        """The CONFIGURED layer (global config), snake_case as upstream
        returns it. Read-only; no runtime status fabrication here."""
        result = self._rpc("config/read", {}, timeout=15.0)
        servers = ((result or {}).get("config") or {}).get("mcp_servers") or {}
        if not isinstance(servers, dict):
            servers = {}
        return servers

    def runtime_status(self, thread_id: str | None = None) -> dict[str, Any]:
        """The RUNTIME layer — upstream's own words only (authStatus, tools,
        serverInfo). Never synthesized."""
        params: dict[str, Any] = {"detail": "toolsAndAuthOnly"}
        if thread_id:
            params["threadId"] = thread_id
        result = self._rpc("mcpServerStatus/list", params, timeout=30.0) or {}
        data = result.get("data") or []
        return {"servers": data, "nextCursor": result.get("nextCursor"),
                "threadBound": bool(thread_id)}

    # -- validation ----------------------------------------------------------

    def validate_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Validate + serialize to the upstream snake_case schema. Returns the
        exact value object for config/value/write. Rejects unknown transports
        and invents no fields."""
        if not isinstance(entry, dict):
            raise ExtensionError("MCP 条目必须是对象", "invalid-argument")
        name = validate_mcp_name(entry.get("name"))
        transport = str(entry.get("transport") or "").lower()
        enabled = bool(entry.get("enabled", True))
        value: dict[str, Any] = {"type": transport, "enabled": enabled}
        if transport == "stdio":
            command = str(entry.get("command") or "").strip()
            if not command:
                raise ExtensionError("stdio MCP 需要 command", "invalid-argument")
            value["command"] = command
            args = entry.get("args")
            if args is not None:
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ExtensionError("args 必须是字符串数组", "invalid-argument")
                value["args"] = args
            cwd = entry.get("cwd")
            if cwd:
                value["cwd"] = str(cwd)
            env = entry.get("env")
            if env:
                if not isinstance(env, dict) or \
                        not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                    raise ExtensionError("env 必须是 {字符串: 字符串}", "invalid-argument")
                value["env"] = env
            startup_timeout = entry.get("startupTimeoutSec")
            if startup_timeout is not None:
                try:
                    value["startup_timeout_sec"] = max(1, int(startup_timeout))
                except (TypeError, ValueError):
                    raise ExtensionError("startupTimeoutSec 必须是整数", "invalid-argument")
        elif transport == "http":
            url = str(entry.get("url") or "").strip()
            if not _URL_RE.fullmatch(url):
                raise ExtensionError("HTTP MCP 需要合法的 http(s) URL", "invalid-argument")
            value["url"] = url
            bearer = entry.get("bearerTokenEnvVar")
            if bearer:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(bearer)):
                    raise ExtensionError("bearerTokenEnvVar 必须是环境变量名", "invalid-argument")
                value["bearer_token_env_var"] = str(bearer)
        else:
            raise ExtensionError(
                f"不支持的 transport {transport!r}（v1 支持 stdio / http）", "invalid-argument")
        return {"name": name, "value": value,
                "warnings": secret_risk_warnings(entry.get("env"))}

    # -- mutations (postcondition-verified) -----------------------------------

    def _write(self, name: str, merge_strategy: str, value: Any) -> None:
        self._rpc("config/value/write",
                  {"keyPath": f"mcp_servers.{name}",
                   "mergeStrategy": merge_strategy, "value": value},
                  timeout=30.0)
        self._rpc("config/mcpServer/reload", {}, timeout=30.0)

    def save(self, entry: dict[str, Any]) -> dict[str, Any]:
        validated = self.validate_entry(entry)
        name, value = validated["name"], validated["value"]
        self._write(name, "upsert", value)
        # postcondition: entry present (upstream defaults ignored in compare)
        servers = self.configured()
        stored = servers.get(name)
        if not isinstance(stored, dict):
            raise PostconditionFailed(
                f"Codex 接受了请求，但 mcp_servers.{name} 未出现在配置中。")
        for key, want in value.items():
            if key == "type":
                continue  # upstream omits type on read-back (verified live)
            got = stored.get(key)
            if isinstance(want, dict) and isinstance(got, dict):
                if {k: str(v) for k, v in want.items()} != {k: str(v) for k, v in got.items()}:
                    raise PostconditionFailed(f"mcp_servers.{name}.{key} 写入后与预期不一致。")
            elif got != want and not (want is True and got is None):
                raise PostconditionFailed(f"mcp_servers.{name}.{key} 写入后与预期不一致。")
        return {"saved": name, "transport": value.get("type"),
                "warnings": validated["warnings"]}

    def delete(self, name: str) -> dict[str, Any]:
        validate_mcp_name(name)
        self._write(name, "replace", None)
        servers = self.configured()
        if name in servers:
            raise PostconditionFailed(
                f"Codex 接受了请求，但 mcp_servers.{name} 仍存在于配置。")
        return {"deleted": name}
