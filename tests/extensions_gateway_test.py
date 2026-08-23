"""Gateway API tests for /api/extensions/* (P2.0 M3).

Real HTTP against the real gateway handler with a stubbed RUNTIMES adapter:
runtime guard (no codex -> 503 CODEX_RUNTIME_REQUIRED), aggregate GET,
mutation POST routes, validation 400s, and the postcondition/ capability
error mapping. ExtensionService itself is covered in extensions_test.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import boujoy_server  # noqa: E402


class StubAdapter:
    """Satisfies the ExtensionService transport protocol."""

    NAME = "codex"

    def __init__(self, handlers=None, available=True):
        self.handlers = dict(handlers or {})
        self.available = available
        self.default_cwd = "/tmp/laomo-gw-test"
        self._workspace_cwd = "/tmp/laomo-gw-test"

    def workspace_cwd(self):
        return self._workspace_cwd

    def codex_available(self):
        return self.available

    def codex_request(self, method, params=None, timeout=60.0):
        handler = self.handlers.get(method)
        if handler is None:
            raise RuntimeError(
                f"codex rpc error: {method}: unknown variant `{method}`")
        if isinstance(handler, Exception):
            raise RuntimeError(str(handler))
        return handler(params) if callable(handler) else handler


def plugin_list(mps):
    return {"marketplaces": mps, "featuredPluginIds": [], "marketplaceLoadErrors": []}


class ExtensionsGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="laomo-ext-gw-")
        root = Path(cls._tmp.name)
        (root / "vault").mkdir()
        (root / "static").mkdir()
        config = boujoy_server.AppConfig(root / "vault", root / "static")
        cls.httpd = boujoy_server.BoujoyServer(("127.0.0.1", 0), config)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls._prev_runtimes = boujoy_server.RUNTIMES
        cls._prev_svc = boujoy_server._EXTENSIONS

    @classmethod
    def tearDownClass(cls):
        boujoy_server.RUNTIMES = cls._prev_runtimes
        boujoy_server._EXTENSIONS = cls._prev_svc
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._tmp.cleanup()

    def _install(self, adapter):
        class Runtimes:
            clean_runtime = "codex" if adapter else "dsh"
            codex_adapter = adapter

            def adapter_for(self, mode):
                return self.codex_adapter if mode == "clean" else None

        boujoy_server.RUNTIMES = Runtimes()
        boujoy_server._EXTENSIONS = None  # rebuild for the new adapter

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    # -- runtime guard --------------------------------------------------------

    def test_no_codex_runtime_get_degrades_not_503(self):
        self._install(None)
        status, body = self._get("/api/extensions")
        # read aggregate degrades honestly (200 + capability block) so the UI
        # can render the unavailable state; only mutations hard-fail
        self.assertEqual(status, 200)
        self.assertFalse(body["capability"]["supported"])
        self.assertEqual(body["capability"]["reason"], "CODEX_RUNTIME_REQUIRED")
        status, body = self._post("/api/extensions/mcp-save",
                                  {"entry": {"name": "x", "transport": "stdio",
                                             "command": "echo"}})
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "CODEX_RUNTIME_REQUIRED")

    def test_codex_unavailable_degrades_get(self):
        self._install(StubAdapter(available=False))
        status, body = self._get("/api/extensions")
        self.assertEqual(status, 200)
        self.assertFalse(body["capability"]["supported"])

    # -- aggregate ------------------------------------------------------------

    def test_aggregate_get_passes_workspace_cwd(self):
        seen = {}

        def on_list(p):
            seen["cwds"] = p.get("cwds")
            return plugin_list([])
        adapter = StubAdapter({
            "plugin/list": on_list,
            "plugin/installed": lambda p: (seen.update(inst=p.get("cwds")),
                                           plugin_list([]))[1],
            "config/read": {"config": {"mcp_servers": {}}},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        })
        adapter._workspace_cwd = "/Users/demo/project"
        self._install(adapter)
        status, body = self._get("/api/extensions")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["workspace"]["cwd"], "/Users/demo/project")
        self.assertTrue(body["plugins"]["supported"])
        self.assertTrue(body["mcp"]["supported"])
        self.assertEqual(seen.get("cwds"), ["/Users/demo/project"])
        self.assertEqual(seen.get("inst"), ["/Users/demo/project"])

    def test_partial_degradation_does_not_500(self):
        self._install(StubAdapter({
            "config/read": {"config": {"mcp_servers": {"a": {"command": "x"}}}},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
            # plugin/list + plugin/installed unknown-variant -> plugins block down
        }))
        status, body = self._get("/api/extensions")
        self.assertEqual(status, 200)
        self.assertFalse(body["plugins"]["supported"])
        self.assertTrue(body["mcp"]["supported"])

    # -- mutations ------------------------------------------------------------

    def test_mcp_save_roundtrip_route(self):
        state = {"servers": {}}

        def read(_):
            return {"config": {"mcp_servers": dict(state["servers"])}}

        def write(p):
            if p["value"] is None:
                state["servers"].pop(p["keyPath"].split(".", 1)[1], None)
            else:
                stored = dict(p["value"]); stored.pop("type", None)
                stored["environment_id"] = "local"; stored["tool_timeout_sec"] = None
                state["servers"][p["keyPath"].split(".", 1)[1]] = stored
            return {"status": "ok", "version": "v", "filePath": "/f"}
        self._install(StubAdapter({
            "config/read": read, "config/value/write": write,
            "config/mcpServer/reload": {},
            "plugin/list": plugin_list([]), "plugin/installed": plugin_list([]),
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        }))
        status, body = self._post("/api/extensions/mcp-save", {"entry": {
            "name": "demo", "transport": "stdio", "command": "/bin/echo"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["saved"], "demo")

    def test_mcp_validation_error_maps_to_400(self):
        self._install(StubAdapter({
            "config/read": {"config": {"mcp_servers": {}}},
            "config/value/write": {"status": "ok", "version": "v", "filePath": "/f"},
            "config/mcpServer/reload": {},
        }))
        status, body = self._post("/api/extensions/mcp-save", {"entry": {
            "name": "bad name!", "transport": "stdio", "command": "x"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid-name")

    def test_postcondition_failure_maps_to_502_with_machine_code(self):
        self._install(StubAdapter({
            "config/read": {"config": {"mcp_servers": {}}},  # never reflects
            "config/value/write": {"status": "ok", "version": "v", "filePath": "/f"},
            "config/mcpServer/reload": {},
        }))
        status, body = self._post("/api/extensions/mcp-save", {"entry": {
            "name": "ghost", "transport": "stdio", "command": "x"}})
        self.assertEqual(status, 502)
        self.assertEqual(body["code"], "POSTCONDITION_FAILED")

    def test_capability_unavailable_mutation_maps_to_501(self):
        self._install(StubAdapter({
            "config/read": {"config": {"mcp_servers": {}}},
            "config/mcpServer/reload": {},
            "plugin/list": plugin_list([]), "plugin/installed": plugin_list([]),
        }))  # config/value/write unknown -> capability on the write path
        status, body = self._post("/api/extensions/mcp-save", {"entry": {
            "name": "x", "transport": "stdio", "command": "x"}})
        self.assertIn(status, (400, 501, 502))
        self.assertIn(body.get("code"), ("CAPABILITY_UNAVAILABLE", "upstream-error"))

    def test_unknown_action_404(self):
        self._install(StubAdapter({
            "plugin/list": plugin_list([]), "plugin/installed": plugin_list([]),
            "config/read": {"config": {"mcp_servers": {}}},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        }))
        status, _ = self._post("/api/extensions/nonsense", {})
        self.assertEqual(status, 404)

    def test_malformed_entry_400(self):
        self._install(StubAdapter({
            "plugin/list": plugin_list([]), "plugin/installed": plugin_list([]),
            "config/read": {"config": {"mcp_servers": {}}},
            "mcpServerStatus/list": {"data": [], "nextCursor": None},
        }))
        status, _ = self._post("/api/extensions/mcp-save", {"entry": "not-an-object"})
        self.assertEqual(status, 400)

    # -- skills routes ----------------------------------------------------------

    def _skills_adapter(self, entries):
        state = {"entries": [dict(e) for e in entries]}

        def on_list(p):
            if p.get("forceReload") and state.get("flip"):
                state["entries"] = [dict(e, enabled=state["flip"].get(e["name"], e["enabled"]))
                                    for e in state["entries"]]
            return {"data": [{"cwd": p.get("cwds", [None])[0],
                              "skills": [dict(e) for e in state["entries"]]}]}

        def on_write(p):
            target = next((e for e in state["entries"] if e["name"] == p.get("name")), None)
            if target is not None:
                state.setdefault("flip", {})[target["name"]] = bool(p["enabled"])
            return {"effectiveEnabled": bool(p["enabled"])}
        return StubAdapter({"skills/list": on_list, "skills/config/write": on_write})

    def test_skills_list_route(self):
        self._install(self._skills_adapter([
            {"name": "a", "description": "d", "path": "/s/a/SKILL.md",
             "scope": "user", "enabled": True}]))
        status, body = self._post("/api/extensions/skills-list", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"]["total"], 1)
        self.assertEqual(body["skills"][0]["name"], "a")

    def test_skill_toggle_route_roundtrip(self):
        self._install(self._skills_adapter([
            {"name": "a", "description": "d", "path": "/s/a/SKILL.md",
             "scope": "user", "enabled": True}]))
        status, body = self._post("/api/extensions/skill-toggle",
                                  {"name": "a", "enabled": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["skill"]["enabled"])
        # follow-up list (forceReload) reflects the verified state
        status, body = self._post("/api/extensions/skills-list",
                                  {"forceReload": True})
        self.assertFalse(body["skills"][0]["enabled"])

    def test_skill_toggle_without_selector_400(self):
        self._install(self._skills_adapter([]))
        status, body = self._post("/api/extensions/skill-toggle",
                                  {"enabled": True})
        self.assertEqual(status, 400)

    def test_skills_routes_capability_501(self):
        self._install(StubAdapter())  # no skills handlers -> unknown variant
        status, body = self._post("/api/extensions/skills-list", {})
        self.assertEqual(status, 501)
        self.assertEqual(body["code"], "CAPABILITY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
