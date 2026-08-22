"""Extension Platform tests (P2.0 v1): capability detection, canonical
identity, postcondition verification, MCP serialization/validation.

The upstream codex app-server is faked via a scripted transport with the
EXACT shapes recorded in docs/extension-contract.md (probed live against
codex-cli 0.149.0-alpha.4.1; see docs/evidence/extension-m0/). No real
codex process is spawned here — real-runtime certification is the M5
read-only E2E.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

from extensions import (CapabilityUnavailable, ExtensionError,  # noqa: E402
                        PostconditionFailed, ExtensionService,
                        validate_mcp_name, secret_risk_warnings)


def summary(pid, name, marketplace, installed=False, enabled=True, **extra):
    """PluginSummary with the exact fields the 0.149 probe returned."""
    out = {"id": pid, "name": name, "installed": installed, "enabled": enabled,
           "installPolicy": "AVAILABLE", "authPolicy": "ON_USE",
           "availability": "AVAILABLE", "mustShowInstallationInterstitial": None,
           "localVersion": "1.0", "version": None, "source": {"type": "local",
           "path": f"/fake/{marketplace}/{name}"}, "remotePluginId": None,
           "interface": {"shortDescription": f"{name} plugin"},
           "installedAt": None, "keywords": [], "eligiblePlanTypes": None,
           "shareContext": None, "installPolicySource": None, "disabledReason": None}
    out.update(extra)
    return out


def marketplace(name, plugins, path=None):
    return {"name": name, "path": path or f"/fake/{name}/marketplace.json",
            "interface": None, "plugins": plugins}


class FakeTransport:
    """Scripted codex_request double. Handlers: method -> callable(params) or
    exception class. Unhandled methods raise unknown-variant like codex."""

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.calls = []

    def codex_request(self, method, params=None, timeout=60.0):
        self.calls.append({"method": method, "params": params})
        handler = self.handlers.get(method)
        if handler is None:
            raise RuntimeError(
                f"codex rpc error: {method}: {{'code': -32600, 'message': "
                f"'Invalid request: unknown variant `{method}`'}}")
        if isinstance(handler, type) and issubclass(handler, Exception):
            raise handler(f"{method}: boom")
        return handler(params) if callable(handler) else handler

    def codex_available(self):
        return True


class UnavailableTransport(FakeTransport):
    def codex_available(self):
        return False


def plugin_list_response(mps):
    return {"marketplaces": mps, "featuredPluginIds": [], "marketplaceLoadErrors": []}


class TestCapabilityDetection(unittest.TestCase):
    def test_overview_requires_codex_runtime(self):
        svc = ExtensionService(None)
        out = svc.overview("/tmp/ws")
        self.assertFalse(out["capability"]["supported"])
        self.assertEqual(out["capability"]["reason"], "CODEX_RUNTIME_REQUIRED")

    def test_overview_adapter_unavailable(self):
        svc = ExtensionService(UnavailableTransport())
        self.assertFalse(svc.overview("/tmp/ws")["capability"]["supported"])

    def test_plugin_list_unsupported_degrades_block_only(self):
        # plugin/list unknown -> plugins block unsupported, mcp still live
        t = FakeTransport({
            "config/read": {"config": {"mcp_servers": {}}},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        })
        out = ExtensionService(t).overview("/tmp/ws")
        self.assertFalse(out["plugins"]["supported"])
        self.assertIn("unknown variant", out["plugins"]["error"])
        self.assertTrue(out["mcp"]["supported"])  # partial degradation holds

    def test_mcp_status_unsupported_degrades_block_only(self):
        t = FakeTransport({
            "plugin/list": plugin_list_response([]),
            "plugin/installed": plugin_list_response([]),
            "config/read": {"config": {"mcp_servers": {"a": {"command": "x"}}}},
        })
        out = ExtensionService(t).overview("/tmp/ws")
        self.assertTrue(out["plugins"]["supported"])
        self.assertFalse(out["mcp"]["supported"])  # status RPC dead -> mcp block down

    def test_mutation_when_unsupported_raises_capability(self):
        t = FakeTransport()  # everything unknown-variant
        with self.assertRaises(ExtensionError) as ctx:
            ExtensionService(t).market_add("https://example.invalid/mp")
        # transport-level unknown surfaces as capability in read paths; for
        # mutations the service pre-checks liveness and normalizes upstream
        self.assertIn(ctx.exception.code, ("CAPABILITY_UNAVAILABLE", "upstream-error"))

    def test_runtime_required_for_mutation(self):
        with self.assertRaises(ExtensionError) as ctx:
            ExtensionService(None).mcp_save({"name": "x", "transport": "stdio",
                                             "command": "echo"})
        self.assertEqual(ctx.exception.code, "CODEX_RUNTIME_REQUIRED")


class TestInventory(unittest.TestCase):
    def setUp(self):
        # same NAME from two marketplaces + one installed from each — the
        # canonical-identity no-merge contract
        self.mps = [
            marketplace("local-a", [
                summary("docs@local-a", "documents", "local-a", installed=True),
                summary("docs@local-b", "documents", "local-b"),  # same name!
            ]),
            marketplace("curated", [
                summary("documents@curated", "documents", "curated"),  # same name!
            ]),
        ]
        self.installed = [
            marketplace("local-a", [summary("docs@local-a", "documents", "local-a",
                                            installed=True)]),
        ]
        self.t = FakeTransport({
            "plugin/list": plugin_list_response(self.mps),
            "plugin/installed": plugin_list_response(self.installed),
        })
        self.svc = ExtensionService(self.t)

    def test_cwd_passed_to_both_reads(self):
        self.svc.overview("/Users/demo/proj")
        for call in self.t.calls:
            if call["method"] in ("plugin/list", "plugin/installed"):
                self.assertEqual(call["params"].get("cwds"), ["/Users/demo/proj"])

    def test_same_name_different_marketplace_never_merges(self):
        inv = ExtensionService(self.t)._plugins.inventory(None)
        ids = [p["id"] for mp in inv["marketplaces"] for p in mp["plugins"]]
        self.assertEqual(len(ids), 3)  # all three survive
        self.assertEqual(sorted(set(ids)), sorted(ids))  # no collapse
        self.assertIn("documents@curated", ids)

    def test_installed_marked_via_canonical_id(self):
        inv = ExtensionService(self.t)._plugins.inventory(None)
        for mp in inv["marketplaces"]:
            for p in mp["plugins"]:
                if p["id"] == "docs@local-a":
                    self.assertTrue(p["installed"])
                if p["id"] == "documents@curated":
                    self.assertFalse(p["installed"])
        self.assertEqual(len(inv["installed"]), 1)

    def test_remote_marketplace_flagged(self):
        remote = dict(marketplace("curated-remote", [summary("x@curated-remote", "x",
                                                             "curated-remote")]))
        remote["path"] = None  # remote marketplaces have no local path
        t = FakeTransport({
            "plugin/list": plugin_list_response([remote]),
            "plugin/installed": plugin_list_response([]),
        })
        inv = ExtensionService(t)._plugins.inventory(None)
        self.assertEqual(inv["marketplaces"][0]["kind"], "remote")


class TestPluginMutationPostcondition(unittest.TestCase):
    def _transport(self, installed_before, installed_after):
        state = {"installed": installed_before}

        def installed(_):
            return plugin_list_response(
                [marketplace("mp", state["installed"])])
        return FakeTransport({
            "plugin/list": lambda _: plugin_list_response([marketplace("mp", state["installed"])]),
            "plugin/installed": installed,
            "plugin/install": lambda p: (state.__setitem__("installed", installed_after),
                                         {"appsNeedingAuth": [], "authPolicy": "ON_USE"})[1],
            "plugin/uninstall": lambda p: (state.__setitem__("installed", installed_after),
                                           {})[1],
        })

    def test_install_postcondition_pass(self):
        target = [summary("docs@mp", "documents", "mp", installed=True)]
        t = self._transport([], target)
        out = ExtensionService(t).plugin_install("documents", "/fake/mp/marketplace.json")
        self.assertTrue(out["ok"])
        self.assertEqual(out["installed"]["id"], "docs@mp")

    def test_install_postcondition_fail_when_state_unchanged(self):
        t = self._transport([], [])  # upstream "succeeds" but nothing installs
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).plugin_install("documents", "/fake/mp/marketplace.json")

    def test_uninstall_postcondition_pass(self):
        target = [summary("docs@mp", "documents", "mp", installed=True)]
        t = self._transport(target, [])
        out = ExtensionService(t).plugin_uninstall("docs@mp")
        self.assertEqual(out["uninstalled"], "docs@mp")

    def test_uninstall_postcondition_fail_when_still_present(self):
        target = [summary("docs@mp", "documents", "mp", installed=True)]
        t = self._transport(target, target)  # no state change
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).plugin_uninstall("docs@mp")

    def test_uninstall_rejects_display_name(self):
        with self.assertRaises(ExtensionError) as ctx:
            ExtensionService(self._transport([], [])).plugin_uninstall("documents")
        self.assertEqual(ctx.exception.code, "invalid-argument")

    def test_install_requires_exactly_one_marketplace_ref(self):
        svc = ExtensionService(self._transport([], []))
        with self.assertRaises(ExtensionError):
            svc.plugin_install("documents")  # neither
        with self.assertRaises(ExtensionError):
            svc.plugin_install("documents", marketplace_path="/a",
                               remote_marketplace_name="b")  # both


class TestMarketplaceMutation(unittest.TestCase):
    def test_add_postcondition_pass(self):
        state = {"mps": []}

        def listing(_):
            return plugin_list_response(state["mps"])
        t = FakeTransport({
            "plugin/list": listing,
            "marketplace/add": lambda p: (state["mps"].append(marketplace(p["source"], [])),
                                          {"alreadyAdded": False,
                                           "installedRoot": "/fake/x",
                                           "marketplaceName": p["source"]})[1],
        })
        out = ExtensionService(t).market_add("my-market")
        self.assertEqual(out["added"], "my-market")
        self.assertFalse(out["alreadyAdded"])

    def test_add_postcondition_fail_when_missing(self):
        t = FakeTransport({
            "plugin/list": lambda _: plugin_list_response([]),
            "marketplace/add": {"alreadyAdded": False, "installedRoot": "/x",
                                "marketplaceName": "ghost"},
        })
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).market_add("my-market")

    def test_remove_postcondition_fail_when_still_listed(self):
        t = FakeTransport({
            "plugin/list": lambda _: plugin_list_response([marketplace("keep", [])]),
            "marketplace/remove": {"marketplaceName": "keep"},
        })
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).market_remove("keep")

    def test_upgrade_reports_errors_honestly(self):
        t = FakeTransport({
            "plugin/list": lambda _: plugin_list_response([]),
            "marketplace/upgrade": {"errors": [{"marketplaceName": "x", "message": "nope"}],
                                    "selectedMarketplaces": ["x"], "upgradedRoots": []},
        })
        with self.assertRaises(ExtensionError) as ctx:
            ExtensionService(t).market_upgrade("x")
        self.assertEqual(ctx.exception.code, "upgrade-failed")


class TestPluginDetail(unittest.TestCase):
    def test_detail_counts_and_confirmation(self):
        detail = {
            "summary": summary("docs@mp", "documents", "mp",
                               mustShowInstallationInterstitial=False),
            "description": "doc plugin", "marketplaceName": "mp",
            "skills": [{"name": "s1"}], "hooks": [{"name": "h1"}],
            "apps": [], "appTemplates": [], "mcpServers": ["srv"],
            "scheduledTasks": [], "shareUrl": None, "marketplacePath": None,
        }
        t = FakeTransport({
            "plugin/read": lambda p: {"plugin": detail} if
            p.get("marketplacePath") else (_ for _ in ()).throw(AssertionError(
                "marketplacePath must be passed through")),
        })
        out = ExtensionService(t).plugin_detail("documents", "/fake/mp/marketplace.json")
        self.assertEqual(out["plugin"]["skills"], 1)
        self.assertEqual(out["plugin"]["hooks"], 1)
        self.assertEqual(out["plugin"]["mcpServers"], ["srv"])
        self.assertTrue(out["requiresConfirmation"])  # hooks + mcp present

    def test_detail_interstitial_forces_confirmation(self):
        detail = {"summary": summary("x@mp", "x", "mp",
                                     mustShowInstallationInterstitial=True),
                  "description": None, "marketplaceName": "mp", "skills": [],
                  "hooks": [], "apps": [], "appTemplates": [], "mcpServers": [],
                  "scheduledTasks": [], "shareUrl": None, "marketplacePath": None}
        t = FakeTransport({"plugin/read": {"plugin": detail}})
        t = FakeTransport({"plugin/read": {"plugin": detail}})
        out = ExtensionService(t).plugin_detail("x", "/mp")
        self.assertTrue(out["requiresConfirmation"])

    def test_plain_skills_only_needs_no_hard_confirm(self):
        detail = {"summary": summary("x@mp", "x", "mp"), "description": None,
                  "marketplaceName": "mp", "skills": [{"n": 1}], "hooks": [],
                  "apps": [], "appTemplates": [], "mcpServers": [],
                  "scheduledTasks": [], "shareUrl": None, "marketplacePath": None}
        t = FakeTransport({"plugin/read": {"plugin": detail}})
        self.assertFalse(ExtensionService(t).plugin_detail("x", "/mp")["requiresConfirmation"])


class TestMcpSerialization(unittest.TestCase):
    def setUp(self):
        self.state = {"servers": {}}

        def read(_):
            import copy
            return {"config": {"mcp_servers": copy.deepcopy(self.state["servers"])}}
        self.t = FakeTransport({
            "config/read": read,
            "config/value/write": self._write,
            "config/mcpServer/reload": {},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        })
        self.svc = ExtensionService(self.t)

    def _write(self, params):
        # emulate upstream: upsert sets (adds defaults), replace-null deletes
        name = params["keyPath"].split(".", 1)[1]
        if params["mergeStrategy"] == "replace" and params["value"] is None:
            self.state["servers"].pop(name, None)
        else:
            stored = dict(params["value"])
            stored.pop("type", None)  # upstream omits type on read-back
            stored["environment_id"] = "local"
            stored["tool_timeout_sec"] = None
            self.state["servers"][name] = stored
        return {"status": "ok", "version": "sha256:fake", "filePath": "/fake/config.toml"}

    def test_stdio_save_serializes_snake_case_and_verifies(self):
        out = self.svc.mcp_save({"name": "my-srv", "transport": "stdio",
                                 "command": "/bin/echo", "args": ["-x"],
                                 "cwd": "/tmp", "env": {"FLAG": "1"},
                                 "enabled": True, "startupTimeoutSec": 30})
        self.assertTrue(out["ok"])
        write = next(c for c in self.t.calls
                     if c["method"] == "config/value/write")
        self.assertEqual(write["params"]["keyPath"], "mcp_servers.my-srv")
        self.assertEqual(write["params"]["mergeStrategy"], "upsert")
        self.assertEqual(write["params"]["value"],
                         {"type": "stdio", "enabled": True, "command": "/bin/echo",
                          "args": ["-x"], "cwd": "/tmp", "env": {"FLAG": "1"},
                          "startup_timeout_sec": 30})
        reloads = [c for c in self.t.calls if c["method"] == "config/mcpServer/reload"]
        self.assertEqual(len(reloads), 1)  # reload fired

    def test_http_save_fields_exactly_schema(self):
        out = self.svc.mcp_save({"name": "remote", "transport": "http",
                                 "url": "https://api.example.com/mcp",
                                 "bearerTokenEnvVar": "MY_TOKEN", "enabled": True})
        self.assertTrue(out["ok"])
        write = next(c for c in self.t.calls if c["method"] == "config/value/write")
        self.assertEqual(write["params"]["value"],
                         {"type": "http", "enabled": True,
                          "url": "https://api.example.com/mcp",
                          "bearer_token_env_var": "MY_TOKEN"})

    def test_delete_uses_null_replace_then_verifies_gone(self):
        self.svc.mcp_save({"name": "gone", "transport": "stdio", "command": "x"})
        out = self.svc.mcp_delete("gone")
        self.assertTrue(out["ok"])
        writes = [c for c in self.t.calls if c["method"] == "config/value/write"]
        self.assertEqual(writes[-1]["params"],
                         {"keyPath": "mcp_servers.gone", "mergeStrategy": "replace",
                          "value": None})

    def test_save_postcondition_fail_when_not_stored(self):
        t = FakeTransport({
            "config/read": {"config": {"mcp_servers": {}}},  # never reflects writes
            "config/value/write": {"status": "ok", "version": "v", "filePath": "/f"},
            "config/mcpServer/reload": {},
        })
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).mcp_save({"name": "ghost", "transport": "stdio",
                                          "command": "x"})

    def test_delete_postcondition_fail_when_still_there(self):
        t = FakeTransport({
            "config/read": {"config": {"mcp_servers": {"stub": {"command": "x"}}}},
            "config/value/write": {"status": "ok", "version": "v", "filePath": "/f"},
            "config/mcpServer/reload": {},
        })
        with self.assertRaises(PostconditionFailed):
            ExtensionService(t).mcp_delete("stub")


class TestMcpValidationSecurity(unittest.TestCase):
    def setUp(self):
        self.t = FakeTransport({
            "config/read": {"config": {"mcp_servers": {}}},
            "config/value/write": {"status": "ok", "version": "v", "filePath": "/f"},
            "config/mcpServer/reload": {},
            "plugin/list": plugin_list_response([]),
            "plugin/installed": plugin_list_response([]),
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        })

    def svc_with(self, servers=None):
        import copy
        state = {"servers": copy.deepcopy(servers or {})}

        def read(_):
            return {"config": {"mcp_servers": copy.deepcopy(state["servers"])}}

        def write(params):
            name = params["keyPath"].split(".", 1)[1]
            if params["value"] is None:
                state["servers"].pop(name, None)
            else:
                stored = dict(params["value"]); stored.pop("type", None)
                stored["environment_id"] = "local"; stored["tool_timeout_sec"] = None
                state["servers"][name] = stored
            return {"status": "ok", "version": "v", "filePath": "/f"}
        return ExtensionService(FakeTransport({
            "config/read": read, "config/value/write": write,
            "config/mcpServer/reload": {}, "plugin/list": plugin_list_response([]),
            "plugin/installed": plugin_list_response([]),
            "mcpServerStatus/list": {"data": [], "nextCursor": None}}))

    def test_invalid_names_rejected(self):
        for bad in ("a b", "a/b", "../escape", "a.b", "", "x" * 65, "a:b", "a\\b",
                    None, 123, "a;b", "a$b", "设备", "a中b"):
            with self.assertRaises(ExtensionError, msg=repr(bad)):
                validate_mcp_name(bad)
        for good in ("a", "my-server", "srv_1", "A9", "-x", "_y", "n" * 64):
            self.assertEqual(validate_mcp_name(good), good)

    def test_keypath_cannot_inject_path_segments(self):
        with self.assertRaises(ExtensionError):
            self.svc_with().mcp_delete("foo/../../config")

    def test_unknown_transport_rejected(self):
        with self.assertRaises(ExtensionError):
            self.svc_with().mcp_save({"name": "x", "transport": "websocket",
                                      "url": "wss://x"})

    def test_http_requires_valid_url(self):
        with self.assertRaises(ExtensionError):
            self.svc_with().mcp_save({"name": "x", "transport": "http", "url": "ftp://bad"})
        with self.assertRaises(ExtensionError):
            self.svc_with().mcp_save({"name": "x", "transport": "http"})

    def test_stdio_requires_command(self):
        with self.assertRaises(ExtensionError):
            self.svc_with().mcp_save({"name": "x", "transport": "stdio"})

    def test_secret_looking_env_gets_warning_metadata(self):
        out = self.svc_with().mcp_save({"name": "s", "transport": "stdio",
                                        "command": "x",
                                        "env": {"API_KEY": "sk-live-12345",
                                                "MY_TOKEN": "TOKEN_REF",
                                                "PLAIN_FLAG": "hello"}})
        fields = [w["field"] for w in out["warnings"]]
        self.assertIn("env.API_KEY", fields)      # secret-ish key + raw value
        self.assertNotIn("env.MY_TOKEN", fields)  # value is an env-var reference
        self.assertNotIn("env.PLAIN_FLAG", fields)

    def test_runtime_status_not_fabricated_without_thread(self):
        out = self.svc_with().overview(None)
        rt = out["mcp"]["runtime"]
        self.assertFalse(rt["threadBound"])
        self.assertEqual(rt["servers"], [])  # whatever upstream said, verbatim


if __name__ == "__main__":
    unittest.main()
