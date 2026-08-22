"""CodexPluginClient: plugin + marketplace surface over the adapter's
low-level codex_request channel. All mutations follow the postcondition
contract (WRITE -> REFETCH -> VERIFY -> SUCCESS) frozen in
docs/extension-contract.md.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .models import (CapabilityUnavailable, ExtensionError, PostconditionFailed,
                     plugin_canonical_id, unsupported_block)

# Marketplace kinds the schema allows (plugin/list filter). v1 queries local
# marketplaces (the default) — kinds are passed through verbatim when given.
MARKETPLACE_KINDS = {"local", "vertical", "workspace-directory",
                     "shared-with-me", "created-by-me-remote"}

_UPSTREAM_UNKNOWN_HINTS = ("unknown variant", "unknown method", "unrecognized")


def _is_capability_error(exc: BaseException) -> bool:
    return any(h in str(exc) for h in _UPSTREAM_UNKNOWN_HINTS)


class CodexPluginClient:
    """Thin, honest wrapper. `transport` is CodexRuntimeAdapter.codex_request
    (or a test double with the same signature)."""

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

    # -- read side ----------------------------------------------------------

    def list_marketplaces(self, cwd: str | None,
                          marketplace_kinds: list[str] | None = None,
                          force_refetch: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwds"] = [cwd]
        if marketplace_kinds:
            unknown = [k for k in marketplace_kinds if k not in MARKETPLACE_KINDS]
            if unknown:
                raise ExtensionError(f"未知的 marketplace kind: {unknown}", "invalid-argument")
            params["marketplaceKinds"] = marketplace_kinds
        if force_refetch:
            params["forceRefetch"] = True
        return self._rpc("plugin/list", params, timeout=60.0)

    def installed(self, cwd: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd:
            params["cwds"] = [cwd]
        return self._rpc("plugin/installed", params, timeout=30.0)

    def read_plugin(self, plugin_name: str, marketplace_path: str | None = None,
                    remote_marketplace_name: str | None = None) -> dict[str, Any]:
        """plugin/read requires EXACTLY ONE of marketplacePath /
        remoteMarketplaceName (verified live: both-missing errors -32600)."""
        if bool(marketplace_path) == bool(remote_marketplace_name):
            raise ExtensionError(
                "plugin/read 需要 marketplacePath 或 remoteMarketplaceName 恰好一个",
                "invalid-argument")
        params: dict[str, Any] = {"pluginName": plugin_name}
        if marketplace_path:
            params["marketplacePath"] = marketplace_path
        if remote_marketplace_name:
            params["remoteMarketplaceName"] = remote_marketplace_name
        result = self._rpc("plugin/read", params, timeout=30.0)
        plugin = (result or {}).get("plugin")
        if not isinstance(plugin, dict):
            raise ExtensionError("plugin/read 未返回插件详情", "upstream-error")
        return plugin

    # -- inventory aggregation (canonical identity, per-marketplace) ---------

    def inventory(self, cwd: str | None) -> dict[str, Any]:
        """available + installed in one canonical-identity-indexed structure.
        Same-name plugins from different marketplaces NEVER merge: the index
        key is PluginSummary.id (name@marketplace)."""
        available = self.list_marketplaces(cwd)
        installed = self.installed(cwd)
        marketplaces: list[dict[str, Any]] = []
        available_by_name: dict[str, dict[str, Any]] = {}
        for mp in (available.get("marketplaces") or []):
            entry = {
                "name": mp.get("name"),
                "kind": "remote" if not mp.get("path") else "local",
                "path": mp.get("path"),
                "interface": mp.get("interface"),
                "plugins": [],
            }
            available_by_name[str(mp.get("name"))] = entry
            for summary in (mp.get("plugins") or []):
                enriched = dict(summary)
                enriched["_marketplace"] = mp.get("name")
                enriched["_canonicalId"] = plugin_canonical_id(enriched)
                entry["plugins"].append(self._plugin_view(enriched))
            marketplaces.append(entry)
        installed_ids: set[str] = set()
        installed_marketplaces: list[dict[str, Any]] = []
        for mp in (installed.get("marketplaces") or []):
            entry = {"name": mp.get("name"), "plugins": []}
            for summary in (mp.get("plugins") or []):
                if not summary.get("installed"):
                    continue
                enriched = dict(summary)
                enriched["_marketplace"] = mp.get("name")
                try:
                    cid = plugin_canonical_id(enriched)
                except ExtensionError:
                    continue  # an installed row without identity cannot be managed
                installed_ids.add(cid)
                entry["plugins"].append(self._plugin_view(enriched))
            if entry["plugins"]:
                installed_marketplaces.append(entry)
        for mp in marketplaces:
            for plugin in mp["plugins"]:
                plugin["installed"] = plugin["id"] in installed_ids \
                    or plugin.get("installed")
        return {
            "marketplaces": marketplaces,
            "installed": installed_marketplaces,
            "loadErrors": list(available.get("marketplaceLoadErrors") or []),
        }

    def _plugin_view(self, summary: dict[str, Any]) -> dict[str, Any]:
        canonical = summary.get("_canonicalId") or plugin_canonical_id(summary)
        return {
            "id": canonical,
            "name": summary.get("name"),
            "marketplace": summary.get("_marketplace"),
            "installed": bool(summary.get("installed")),
            "enabled": bool(summary.get("enabled")),
            "version": summary.get("localVersion") or summary.get("version"),
            "installPolicy": summary.get("installPolicy"),
            "authPolicy": summary.get("authPolicy"),
            "availability": summary.get("availability"),
            "mustShowInstallationInterstitial":
                bool(summary.get("mustShowInstallationInterstitial")),
            "description": ((summary.get("interface") or {}) or {}).get("shortDescription"),
            "marketplacePath": (summary.get("source") or {}).get("path")
                if (summary.get("source") or {}).get("type") == "local" else None,
        }

    # -- mutations (postcondition-verified) ----------------------------------
    # Postcondition scans are workspace-aware: they re-read plugin/installed
    # with the SAME cwds scope the inventory used. A home-scope-only scan
    # would miss project/workspace-scoped plugins (false
    # POSTCONDITION_FAILED) or see stale same-source rows.

    def install(self, plugin_name: str, marketplace_path: str | None = None,
                remote_marketplace_name: str | None = None,
                cwd: str | None = None) -> dict[str, Any]:
        if bool(marketplace_path) == bool(remote_marketplace_name):
            raise ExtensionError(
                "install 需要 marketplacePath 或 remoteMarketplaceName 恰好一个",
                "invalid-argument")
        params: dict[str, Any] = {"pluginName": plugin_name,
                                  "installAttemptId": f"laomo-{int(time.time()*1000)}"}
        if marketplace_path:
            params["marketplacePath"] = marketplace_path
        if remote_marketplace_name:
            params["remoteMarketplaceName"] = remote_marketplace_name
        result = self._rpc("plugin/install", params, timeout=120.0)
        # postcondition: the canonical plugin shows up installed (same cwd scope)
        state = self._find_installed(plugin_name, marketplace_path,
                                     remote_marketplace_name, cwd)
        if state is None:
            raise PostconditionFailed(
                f"Codex 接受了请求，但插件 {plugin_name} 未出现在已安装列表。")
        return {"installResult": result or {}, "installed": state}

    def uninstall(self, canonical_id: str, cwd: str | None = None) -> dict[str, Any]:
        """canonical_id is PluginSummary.id (name@marketplace), NOT a display
        name — uninstall targets exactly one canonical plugin."""
        name = canonical_id.split("@", 1)[0]
        self._rpc("plugin/uninstall", {"pluginId": canonical_id}, timeout=60.0)
        if self._find_installed_by_id(canonical_id, cwd) is not None:
            raise PostconditionFailed(
                f"Codex 接受了请求，但插件 {canonical_id} 仍在已安装列表。")
        return {"uninstalled": canonical_id, "name": name}

    def _installed_scan(self, cwd: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for mp in (self.installed(cwd).get("marketplaces") or []):
            for summary in (mp.get("plugins") or []):
                if summary.get("installed"):
                    rows.append(summary)
        return rows

    def _find_installed(self, plugin_name: str, marketplace_path: str | None,
                        remote_marketplace_name: str | None,
                        cwd: str | None = None) -> dict[str, Any] | None:
        target_mp = remote_marketplace_name or self._mp_name_from_path(marketplace_path)
        for summary in self._installed_scan(cwd):
            if summary.get("name") == plugin_name:
                mp = str(summary.get("id", "")).split("@", 1)
                summary_mp = mp[1] if len(mp) > 1 else None
                if target_mp is None or summary_mp == target_mp \
                        or str(summary.get("id", "")).endswith(f"@{target_mp}"):
                    enriched = dict(summary)
                    enriched["_marketplace"] = summary_mp
                    try:
                        enriched["_canonicalId"] = plugin_canonical_id(enriched)
                    except ExtensionError:
                        return None
                    return self._plugin_view(enriched)
        return None

    def _find_installed_by_id(self, canonical_id: str,
                              cwd: str | None = None) -> dict[str, Any] | None:
        for summary in self._installed_scan(cwd):
            if str(summary.get("id") or "") == canonical_id:
                return summary
        return None

    def _mp_name_from_path(self, path: str | None) -> str | None:
        # marketplacePath ".../<name>/.agents/plugins/marketplace.json" — the
        # marketplace name is the directory above .agents
        if not path:
            return None
        parts = [p for p in str(path).split("/") if p]
        for i, part in enumerate(parts):
            if part == ".agents" and i > 0:
                return parts[i - 1]
        return None

    # -- marketplace mutations ----------------------------------------------

    def market_add(self, source: str, ref_name: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"source": source}
        if ref_name:
            params["refName"] = ref_name
        result = self._rpc("marketplace/add", params, timeout=180.0) or {}
        added = result.get("marketplaceName")
        # postcondition: the marketplace appears in plugin/list
        listing = self.list_marketplaces(None)
        names = [mp.get("name") for mp in (listing.get("marketplaces") or [])]
        if added not in names and not result.get("alreadyAdded"):
            raise PostconditionFailed(
                f"Codex 接受了请求，但市场源 {added!r} 未出现在列表。")
        return {"added": added or source, "alreadyAdded": bool(result.get("alreadyAdded")),
                "installedRoot": result.get("installedRoot")}

    def market_remove(self, marketplace_name: str) -> dict[str, Any]:
        self._rpc("marketplace/remove", {"marketplaceName": marketplace_name},
                  timeout=60.0)
        listing = self.list_marketplaces(None)
        names = [mp.get("name") for mp in (listing.get("marketplaces") or [])]
        if marketplace_name in names:
            raise PostconditionFailed(
                f"Codex 接受了请求，但市场源 {marketplace_name} 仍在列表。")
        return {"removed": marketplace_name}

    def market_upgrade(self, marketplace_name: str | None = None) -> dict[str, Any]:
        """NOTE — contract honesty (P2.0.1): unlike install/uninstall/add/remove,
        upgrade is UPSTREAM-RESULT VALIDATION, not a strong refetch
        postcondition. "Up to date" has no stable authoritative field to
        re-verify against, so we validate the upstream's own result (errors
        empty, or upgradedRoots non-empty) instead of WRITE->REFETCH->VERIFY.
        docs/extension-contract.md states this distinction explicitly."""
        params: dict[str, Any] = {}
        if marketplace_name:
            params["marketplaceName"] = marketplace_name
        result = self._rpc("marketplace/upgrade", params, timeout=300.0) or {}
        errors = result.get("errors") or []
        if errors and not result.get("upgradedRoots"):
            raise ExtensionError(
                "升级失败：" + "; ".join(str(e) for e in errors[:3]), "upgrade-failed")
        return {"upgradedRoots": result.get("upgradedRoots") or [],
                "selected": result.get("selectedMarketplaces") or [],
                "errors": errors}
