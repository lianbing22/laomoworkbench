"""Extension Platform (P2.0 v1): Codex-native plugins, marketplaces and MCP.

Contract: docs/extension-contract.md. The service layer never touches Mission
internals and never spawns its own app-server — it speaks through the runtime
adapter's codex_request transport only.
"""
from .models import (CapabilityUnavailable, ExtensionError, PostconditionFailed,
                     plugin_canonical_id, secret_risk_warnings, validate_mcp_name)
from .codex_plugins import CodexPluginClient
from .mcp_config import McpConfigService
from .service import ExtensionService

__all__ = ["CapabilityUnavailable", "ExtensionError", "PostconditionFailed",
           "plugin_canonical_id", "secret_risk_warnings", "validate_mcp_name",
           "CodexPluginClient", "McpConfigService", "ExtensionService"]
