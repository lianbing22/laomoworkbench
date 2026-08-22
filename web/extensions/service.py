"""ExtensionService: the aggregate facade behind /api/extensions.

Owns capability detection, per-block degradation, mutation postconditions,
and error normalization. Deliberately independent of CodexRuntimeAdapter's
business layer — it only sees the adapter's `codex_request` transport (and
`codex_available` liveness). It never touches Mission/UnitRunner/Worktree
and never spawns its own app-server.
"""
from __future__ import annotations

from typing import Any, Protocol

from .codex_plugins import CodexPluginClient
from .mcp_config import McpConfigService
from .models import CapabilityUnavailable, ExtensionError, block, unsupported_block


class CodexTransport(Protocol):
    """What the service needs from a runtime adapter (CodexRuntimeAdapter
    satisfies this; tests may bring their own)."""

    def codex_request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = 60.0) -> Any: ...

    def codex_available(self) -> bool: ...


class ExtensionService:
    NAME = "extensions"

    def __init__(self, adapter: CodexTransport | None) -> None:
        self._adapter = adapter
        self._plugins = CodexPluginClient(adapter.codex_request) if adapter else None
        self._mcp = McpConfigService(adapter.codex_request) if adapter else None

    # -- aggregate read ------------------------------------------------------

    def overview(self, cwd: str | None) -> dict[str, Any]:
        """GET /api/extensions payload. Every block degrades independently —
        one dead upstream RPC must never sink the whole response."""
        out: dict[str, Any] = {"ok": True}
        if self._adapter is None or not self._adapter.codex_available():
            return {**out, "capability": block(False, reason="CODEX_RUNTIME_REQUIRED",
                                               message="扩展市场需要 Codex 运行时")}
        out["capability"] = block(True, runtime="codex")
        out["workspace"] = {"cwd": cwd}
        # plugins block (available + installed in one structure)
        try:
            out["plugins"] = block(True, **self._plugins.inventory(cwd))
        except CapabilityUnavailable as exc:
            out["plugins"] = unsupported_block(exc)
        except ExtensionError as exc:
            out["plugins"] = block(True, supported=True,
                                   error=f"{exc.code}: {exc.message}",
                                   marketplaces=[], installed=[], loadErrors=[])
        # mcp block: configured + runtime status are separate layers
        try:
            out["mcp"] = block(True, configured=self._mcp.configured(),
                               runtime=self._mcp.runtime_status())
        except CapabilityUnavailable as exc:
            out["mcp"] = unsupported_block(exc)
        except ExtensionError as exc:
            out["mcp"] = block(False, error=f"{exc.code}: {exc.message}")
        return out

    # -- plugin detail (risk preview) ---------------------------------------

    def plugin_detail(self, plugin_name: str, marketplace_path: str | None = None,
                      remote_marketplace_name: str | None = None) -> dict[str, Any]:
        self._require()
        plugin = self._plugins.read_plugin(plugin_name, marketplace_path,
                                           remote_marketplace_name)
        summary = plugin.get("summary") or {}
        counts = {
            "skills": len(plugin.get("skills") or []),
            "hooks": len(plugin.get("hooks") or []),
            "apps": len(plugin.get("apps") or []),
            "appTemplates": len(plugin.get("appTemplates") or []),
            "mcpServers": list(plugin.get("mcpServers") or []),
            "scheduledTasks": len(plugin.get("scheduledTasks") or []),
        }
        # anything beyond plain skills warrants an explicit confirm dialog
        requires_confirmation = any((counts["hooks"], counts["apps"],
                                     counts["appTemplates"],
                                     counts["scheduledTasks"])) or bool(counts["mcpServers"])
        return {
            "ok": True,
            "plugin": {
                "name": summary.get("name") or plugin_name,
                "canonicalId": summary.get("id"),
                "marketplaceName": plugin.get("marketplaceName"),
                "description": plugin.get("description"),
                "shareUrl": plugin.get("shareUrl"),
                **counts,
                "installPolicy": summary.get("installPolicy"),
                "authPolicy": summary.get("authPolicy"),
                "availability": summary.get("availability"),
                "mustShowInstallationInterstitial":
                    bool(summary.get("mustShowInstallationInterstitial")),
            },
            "requiresConfirmation": requires_confirmation or
                bool(summary.get("mustShowInstallationInterstitial")),
        }

    # -- plugin mutations ----------------------------------------------------

    def plugin_install(self, plugin_name: str, marketplace_path: str | None = None,
                       remote_marketplace_name: str | None = None) -> dict[str, Any]:
        self._require()
        return {"ok": True, **self._plugins.install(
            plugin_name, marketplace_path, remote_marketplace_name)}

    def plugin_uninstall(self, canonical_id: str) -> dict[str, Any]:
        self._require()
        if not canonical_id or "@" not in str(canonical_id):
            raise ExtensionError(
                "uninstall 需要 canonical id（name@marketplace）", "invalid-argument")
        return {"ok": True, **self._plugins.uninstall(str(canonical_id))}

    # -- marketplace mutations -------------------------------------------------

    def market_add(self, source: str, ref_name: str | None = None) -> dict[str, Any]:
        self._require()
        if not isinstance(source, str) or not source.strip():
            raise ExtensionError("source 不能为空", "invalid-argument")
        return {"ok": True, **self._plugins.market_add(source.strip(), ref_name)}

    def market_remove(self, marketplace_name: str) -> dict[str, Any]:
        self._require()
        if not isinstance(marketplace_name, str) or not marketplace_name.strip():
            raise ExtensionError("marketplaceName 不能为空", "invalid-argument")
        return {"ok": True, **self._plugins.market_remove(marketplace_name.strip())}

    def market_upgrade(self, marketplace_name: str | None = None) -> dict[str, Any]:
        self._require()
        return {"ok": True, **self._plugins.market_upgrade(marketplace_name)}

    # -- mcp -------------------------------------------------------------------

    def mcp_save(self, entry: dict[str, Any]) -> dict[str, Any]:
        self._require()
        return {"ok": True, **self._mcp.save(entry)}

    def mcp_delete(self, name: str) -> dict[str, Any]:
        self._require()
        return {"ok": True, **self._mcp.delete(name)}

    def _require(self) -> None:
        if self._adapter is None or not self._adapter.codex_available():
            raise ExtensionError("扩展市场需要 Codex 运行时", "CODEX_RUNTIME_REQUIRED")
