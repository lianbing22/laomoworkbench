"""Extension Platform models: errors, validation, canonical identity helpers.

Contract: docs/extension-contract.md (frozen against codex 0.149.0-alpha.4.1).
"""
from __future__ import annotations

import re
from typing import Any

# MCP names become keyPath segments (mcp_servers.<name>) — confine them to a
# flat safe charset; anything path-like or controlling is rejected.
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# env keys that look like they carry a raw secret (value written in cleartext
# to ~/.codex/config.toml — warn, and steer to env-var references instead)
SECRETISH_KEY_RE = re.compile(
    r"(token|secret|password|api[_-]?key|\bkey\b|credential)", re.IGNORECASE)
# a value that references an environment variable rather than storing one
ENV_REF_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


# upstream's unknown-method wording (JSON-RPC -32600 "unknown variant ...");
# any of these hints means the installed codex runtime lacks the RPC — a
# capability state, not an error
_UPSTREAM_UNKNOWN_HINTS = ("unknown variant", "unknown method", "unrecognized")


def is_capability_error(exc: BaseException) -> bool:
    return any(h in str(exc) for h in _UPSTREAM_UNKNOWN_HINTS)


class ExtensionError(Exception):
    """Normalized extension-layer error. `code` is machine-readable and maps
    1:1 onto the gateway's HTTP error payload."""

    def __init__(self, message: str, code: str = "extension-error") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CapabilityUnavailable(ExtensionError):
    """The upstream codex runtime does not expose this capability (method
    missing / errored as unknown). Rendered as a capability state, not an
    error toast."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="CAPABILITY_UNAVAILABLE")


class PostconditionFailed(ExtensionError):
    """Upstream accepted the mutation but the authoritative re-read did not
    show the expected state change."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="POSTCONDITION_FAILED")


def validate_mcp_name(name: Any) -> str:
    if not isinstance(name, str) or not MCP_NAME_RE.fullmatch(name):
        raise ExtensionError(
            f"无效的 MCP 名称 {name!r}：仅允许 1–64 个字母/数字/下划线/连字符", "invalid-name")
    return name


def secret_risk_warnings(env: dict[str, str] | None) -> list[dict[str, str]]:
    """Metadata (never a blocker): env entries whose KEY looks secret-ish and
    whose VALUE stores the secret itself instead of referencing an env var."""
    warnings: list[dict[str, str]] = []
    for key, value in (env or {}).items():
        if SECRETISH_KEY_RE.search(str(key)) and not ENV_REF_RE.fullmatch(str(value)):
            warnings.append({
                "field": f"env.{key}",
                "risk": "secret-in-config",
                "message": "该值会明文写入 ~/.codex/config.toml；建议改为引用环境变量名"
                           "（值填环境变量名，如 MY_SERVER_TOKEN）。",
            })
    return warnings


def plugin_canonical_id(summary: dict[str, Any]) -> str:
    """Canonical identity = PluginSummary.id (name@marketplace). Never merge
    plugins by display name — same names from different marketplaces coexist."""
    pid = str(summary.get("id") or "")
    if pid:
        return pid
    # defensive: upstream always sends id today; a missing id must not silently
    # collapse distinct plugins
    mp = str(summary.get("marketplaceName") or summary.get("_marketplace") or "?")
    raise ExtensionError(f"插件缺少 canonical id: {summary.get('name')!r} @ {mp}",
                         "missing-canonical-id")


def block(supported: bool = True, **fields: Any) -> dict[str, Any]:
    """One aggregate block. A failing upstream RPC degrades to
    {"supported": false, "error": ...} without sinking the whole response."""
    out: dict[str, Any] = {"supported": supported}
    out.update(fields)
    return out


def unsupported_block(exc: BaseException) -> dict[str, Any]:
    msg = str(exc)
    return block(False, error=msg[:300])
