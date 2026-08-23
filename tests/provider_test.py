"""P0.5 Model Provider Profiles unit tests.

Rules honoured throughout:
- NEVER a real API key: every secret is an obvious fake ("sk-fake-...")
- NEVER the macOS Keychain: ProviderProfileManager is always constructed
  with a FakeCredentialStore that overrides persistent/get/set/has/delete,
  so no `security` subprocess can run
- no real codex process: the adapter is exercised without _ensure_process()
  (or with a FakeProcess capturing rpc requests)

Run:  PYTHONDONTWRITEBYTECODE=1 python3 tests/provider_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
sys.path.insert(0, os.path.dirname(__file__))

import provider_profile as pp  # noqa: E402
import mock_responses_server  # noqa: E402
from provider_profile import (  # noqa: E402
    BUILTIN_CHATGPT_ID,
    CredentialStore,
    ProfileStore,
    ProviderError,
    ProviderProfileManager,
)
from codex_adapter import CodexRuntimeAdapter  # noqa: E402

FAKE_SECRET = "sk-fake-unit-test-never-real"


# --- fakes ---------------------------------------------------------------------


class FakeCredentialStore(CredentialStore):
    """Memory-only CredentialStore: overrides the Keychain surface entirely
    (persistent/get/set/has/delete) so tests never touch `security`."""

    def __init__(self, persistent: bool = False) -> None:
        super().__init__()
        self._persistent = persistent
        self.set_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    @property
    def persistent(self) -> bool:
        return self._persistent

    def set(self, profile_id: str, secret: str) -> None:
        secret = secret.strip()
        if not secret:
            raise ProviderError("secret 不能为空")
        self.set_calls.append((profile_id, secret))
        with self._lock:
            self._memory[profile_id] = secret

    def get(self, profile_id: str) -> str | None:
        with self._lock:
            return self._memory.get(profile_id)

    def has(self, profile_id: str) -> bool:
        return self.get(profile_id) is not None

    def delete(self, profile_id: str) -> None:
        self.delete_calls.append(profile_id)
        with self._lock:
            self._memory.pop(profile_id, None)


class FakeRpc:
    """Captures rpc requests instead of talking to a codex process."""

    def __init__(self):
        self.requests: list[dict] = []

    def request(self, method, params=None, timeout=60.0):
        self.requests.append({"method": method, "params": params, "timeout": timeout})
        return {"ok": True}


class FakeProcess:
    """Stand-in for CodexProcess: 'ready' so _ensure_process returns it."""

    status = "ready"

    def __init__(self):
        self.rpc = FakeRpc()


# --- shared helpers --------------------------------------------------------------


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="laomo-provider-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.creds = FakeCredentialStore()
        self.mgr = ProviderProfileManager(self.root, self.creds)

    def payload(self, **overrides) -> dict:
        base = {
            "name": "Mock Provider",
            "type": "custom",
            "baseUrl": "http://127.0.0.1:18652/v1",
            "models": [
                {"id": "mock-1", "label": "Mock One"},
                {"id": "mock-2", "label": "Mock Two"},
            ],
            "defaultModel": "mock-1",
            "secret": FAKE_SECRET,
        }
        base.update(overrides)
        return base

    def save_mock(self, **overrides) -> dict:
        return self.mgr.save_profile(self.payload(**overrides))


# --- 1. Profile CRUD ---------------------------------------------------------------


class TestProfileCrud(ProviderTestCase):
    def test_create_list_update_delete(self):
        # create
        pub = self.save_mock()
        self.assertEqual(pub["id"], "mock-provider")
        self.assertFalse(pub["builtin"])
        self.assertTrue(pub["secretConfigured"])
        # list includes the builtin chatgpt plus the new profile
        ids = [p["id"] for p in self.mgr.list()]
        self.assertIn(BUILTIN_CHATGPT_ID, ids)
        self.assertIn("mock-provider", ids)
        self.assertEqual([p["id"] for p in self.mgr.list() if p.get("builtin")],
                         [BUILTIN_CHATGPT_ID])
        # update keeps the id, changes the fields
        pub2 = self.save_mock(id="mock-provider", name="Mock Renamed",
                              models=[{"id": "other-1", "label": "Other"}],
                              defaultModel="other-1")
        self.assertEqual(pub2["id"], "mock-provider")
        stored = self.mgr.get("mock-provider")
        self.assertEqual(stored["name"], "Mock Renamed")
        self.assertEqual([m["id"] for m in stored["models"]], ["other-1"])
        # delete removes it (and its credential)
        self.mgr.delete_profile("mock-provider")
        self.assertIsNone(self.mgr.get("mock-provider"))
        self.assertNotIn("mock-provider", [p["id"] for p in self.mgr.list()])
        self.assertEqual(self.creds.delete_calls, ["mock-provider"])
        # deleting a non-existent profile is rejected
        with self.assertRaises(ProviderError):
            self.mgr.delete_profile("mock-provider")

    def test_duplicate_name_gets_unique_slug(self):
        self.save_mock()
        second = self.save_mock(secret="sk-fake-2")
        self.assertEqual(second["id"], "mock-provider-2")


# --- 2. builtin protection -----------------------------------------------------------


class TestBuiltinProtection(ProviderTestCase):
    def test_chatgpt_cannot_be_deleted(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.delete_profile(BUILTIN_CHATGPT_ID)
        self.assertEqual(ctx.exception.code, "builtin")
        self.assertIsNotNone(self.mgr.get(BUILTIN_CHATGPT_ID))

    def test_chatgpt_cannot_be_edited(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.save_profile({"id": BUILTIN_CHATGPT_ID, "name": "Hacked",
                                   "type": "chatgpt"})
        self.assertEqual(ctx.exception.code, "builtin")
        # the builtin profile is untouched
        self.assertEqual(self.mgr.get(BUILTIN_CHATGPT_ID)["name"], "ChatGPT / Codex")

    def test_cannot_hijack_builtin_id_with_custom_type(self):
        # saving a custom profile that asks for id "chatgpt" must be renamed
        pub = self.save_mock(id=BUILTIN_CHATGPT_ID)
        self.assertNotEqual(pub["id"], BUILTIN_CHATGPT_ID)
        self.assertEqual(pub["id"], "chatgpt-2")
        self.assertTrue(self.mgr.get(BUILTIN_CHATGPT_ID)["builtin"])


# --- 3. secret retention on update ---------------------------------------------------


class TestSecretRetention(ProviderTestCase):
    def test_empty_secret_keeps_old_value(self):
        self.save_mock()
        # update with empty string -> old secret kept
        self.save_mock(id="mock-provider", secret="")
        self.assertEqual(self.creds.get("mock-provider"), FAKE_SECRET)
        # update with the key absent entirely -> also keeps the old value
        p = self.payload(id="mock-provider", name="Again")
        p.pop("secret")
        self.mgr.save_profile(p)
        self.assertEqual(self.creds.get("mock-provider"), FAKE_SECRET)

    def test_explicit_secret_overrides(self):
        self.save_mock()
        self.save_mock(id="mock-provider", secret="sk-fake-rotated")
        self.assertEqual(self.creds.get("mock-provider"), "sk-fake-rotated")
        # and env injection reflects the new value
        env = self.mgr.env_for_process()
        self.assertEqual(env, {"LAOMO_CODEX_PROVIDER_MOCK_PROVIDER_KEY": "sk-fake-rotated"})


# --- 4. secrets never leak -------------------------------------------------------------


class TestSecretNeverLeaks(ProviderTestCase):
    def test_public_shapes_contain_no_secret(self):
        self.save_mock(secret="sk-top-secret-value-42")
        profile = self.mgr.get("mock-provider")
        pub = self.mgr.public(profile)
        self.assertNotIn("secret", pub)
        self.assertTrue(pub["secretConfigured"])
        listing = self.mgr.public_list()
        for entry in listing["providers"]:
            self.assertNotIn("secret", entry)
        blob = json.dumps(pub) + json.dumps(listing) + json.dumps(profile)
        self.assertNotIn("sk-top-secret-value-42", blob)
        # chatgpt entry never carries a secret field either
        chatgpt = [e for e in listing["providers"] if e["id"] == BUILTIN_CHATGPT_ID][0]
        self.assertNotIn("secret", chatgpt)
        self.assertTrue(chatgpt["secretConfigured"])  # login-based, needs no key


# --- 5. activate ---------------------------------------------------------------------


class TestActivate(ProviderTestCase):
    def test_activate_success(self):
        self.save_mock()
        self.assertEqual(self.mgr.active_id(), BUILTIN_CHATGPT_ID)  # default
        returned = self.mgr.activate("mock-provider")
        self.assertEqual(returned, "mock-provider")
        self.assertEqual(self.mgr.active_id(), "mock-provider")
        self.assertEqual(self.mgr.public_list()["activeProviderId"], "mock-provider")
        # chatgpt remains activatable (no key required)
        self.assertEqual(self.mgr.activate(BUILTIN_CHATGPT_ID), BUILTIN_CHATGPT_ID)

    def test_activate_unknown_id_rejected(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.activate("no-such-provider")
        self.assertEqual(ctx.exception.code, "not-found")

    def test_activate_without_configured_key_rejected(self):
        self.save_mock()
        # simulate a restart that lost session-only credentials
        restarted = ProviderProfileManager(self.root, FakeCredentialStore())
        self.assertIsNotNone(restarted.get("mock-provider"))
        with self.assertRaises(ProviderError) as ctx:
            restarted.activate("mock-provider")
        self.assertEqual(ctx.exception.code, "missing-key")

    def test_deleting_active_provider_falls_back_to_chatgpt(self):
        self.save_mock()
        self.mgr.activate("mock-provider")
        self.assertEqual(self.mgr.active_id(), "mock-provider")
        self.mgr.delete_profile("mock-provider")
        self.assertEqual(self.mgr.active_id(), BUILTIN_CHATGPT_ID)
        self.assertIsInstance(self.mgr.active(), dict)
        self.assertEqual(self.mgr.active()["id"], BUILTIN_CHATGPT_ID)


# --- 6. env injection ------------------------------------------------------------------


class TestEnvInjection(ProviderTestCase):
    def test_env_for_process_maps_envkey_to_secret(self):
        self.assertEqual(self.mgr.env_for_process(), {})  # builtin only: nothing
        self.save_mock()
        self.assertEqual(self.mgr.env_for_process(),
                         {"LAOMO_CODEX_PROVIDER_MOCK_PROVIDER_KEY": FAKE_SECRET})
        # a second profile contributes its own envKey
        self.save_mock(name="Second Svc", secret="sk-fake-second",
                       baseUrl="http://127.0.0.1:18652/v1",
                       models=[{"id": "s1"}], defaultModel="s1")
        env = self.mgr.env_for_process()
        self.assertEqual(len(env), 2)
        self.assertEqual(env["LAOMO_CODEX_PROVIDER_SECOND_SVC_KEY"], "sk-fake-second")
        # secrets without a stored credential are simply omitted
        lost = ProviderProfileManager(self.root, FakeCredentialStore())
        self.assertEqual(lost.env_for_process(), {})


# --- 7. validation ----------------------------------------------------------------------


class TestValidation(ProviderTestCase):
    def test_invalid_payloads_rejected(self):
        cases = [
            ("bad base url", self.payload(baseUrl="not-a-url")),
            ("ftp scheme", self.payload(baseUrl="ftp://example.com/v1")),
            ("empty base url", self.payload(baseUrl="   ")),
            ("wire api chat", self.payload(wireApi="chat")),
            ("empty name", self.payload(name="   ")),
            ("default model not in list", self.payload(defaultModel="ghost-model")),
            ("no models at all", self.payload(models=[], defaultModel="")),
            ("bad type", self.payload(type="anthropic")),
        ]
        for label, bad in cases:
            with self.subTest(label=label), self.assertRaises(ProviderError):
                self.mgr.save_profile(bad)
        # nothing was persisted by any rejected payload
        self.assertEqual(self.mgr.list(), [self.mgr.builtin_profile()])

    def test_wire_api_chat_error_code(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.save_profile(self.payload(wireApi="chat"))
        self.assertEqual(ctx.exception.code, "protocol-incompatible")

    def test_default_model_autofills_from_models(self):
        pub = self.save_mock(defaultModel="")  # falls back to first model
        self.assertEqual(pub["defaultModel"], "mock-1")


# --- 8. codex registration shape -----------------------------------------------------------


class TestCodexProviderRegistration(ProviderTestCase):
    def test_provider_definitions_shape(self):
        self.assertEqual(self.mgr.codex_provider_definitions(), [])  # chatgpt excluded
        self.save_mock()
        defs = self.mgr.codex_provider_definitions()
        self.assertEqual(len(defs), 1)
        d = defs[0]
        self.assertEqual(d["id"], "mock-provider")
        self.assertEqual(d["baseUrl"], "http://127.0.0.1:18652/v1")
        self.assertEqual(d["envKey"], "LAOMO_CODEX_PROVIDER_MOCK_PROVIDER_KEY")
        self.assertEqual(d["wireApi"], "responses")

    def test_write_provider_config_snake_case_params(self):
        self.save_mock()
        adapter = CodexRuntimeAdapter(default_cwd=str(self.root), providers=self.mgr, state_root=str(self.root / "host-state"))
        fake_proc = FakeProcess()
        adapter.process = fake_proc  # ready -> _ensure_process returns it
        profile = self.mgr.get("mock-provider")
        self.assertTrue(adapter._write_provider_config(profile))

        self.assertEqual(len(fake_proc.rpc.requests), 1)
        req = fake_proc.rpc.requests[0]
        self.assertEqual(req["method"], "config/value/write")
        params = req["params"]
        self.assertEqual(params["keyPath"], "model_providers.mock-provider")
        self.assertEqual(params["mergeStrategy"], "upsert")
        self.assertEqual(params["value"], {
            "name": "Mock Provider",
            "base_url": "http://127.0.0.1:18652/v1",
            "env_key": "LAOMO_CODEX_PROVIDER_MOCK_PROVIDER_KEY",
            "wire_api": "responses",
        })
        # the registration payload never contains the secret itself
        self.assertNotIn(FAKE_SECRET, json.dumps(params))


# --- 9. error classification -----------------------------------------------------------------


class TestClassifyProviderError(unittest.TestCase):
    TABLE = [
        ("stream error 401 Unauthorized", "auth-failed"),
        ("invalid token supplied", "auth-failed"),
        ("authentication required", "auth-failed"),
        ("bad api key", "auth-failed"),
        ("HTTP 404: model not found: mock-9", "model-not-found"),
        ("unknown model requested", "model-not-found"),
        ("connect ECONNREFUSED 127.0.0.1:18652", "unreachable"),
        ("dns error: getaddrinfo failed", "unreachable"),
        ("request timed out after 30000ms", "unreachable"),
        ("host unreachable", "unreachable"),
        ("the /responses endpoint returned 405 method not allowed", "protocol-incompatible"),
        ("responses API unsupported by this server", "protocol-incompatible"),
        ("something exploded mid-turn", "runtime-error"),
        ("", "runtime-error"),
    ]

    def test_classification_table(self):
        for message, expected in self.TABLE:
            with self.subTest(message=message):
                out = CodexRuntimeAdapter._classify_provider_error(message)
                self.assertFalse(out["ok"])
                self.assertEqual(out["outcome"], expected)
                self.assertTrue(out["message"])  # human-readable, zh
                self.assertNotIn(FAKE_SECRET, out["message"])


# --- 10. cross-provider selectModel guard -------------------------------------------------------


class TestSelectModelProviderGuard(unittest.TestCase):
    def _adapter(self):
        return CodexRuntimeAdapter(default_cwd="/tmp", state_root=tempfile.mkdtemp(prefix="laomo-host-state-"))  # no process needed

    def test_cross_provider_change_rejected(self):
        adapter = self._adapter()
        adapter.registry.set_provider("s1", "p1")
        res = adapter.rpc("clean", "session.selectModel",
                          {"sessionId": "s1", "model": "whatever", "provider": "p2"})
        self.assertFalse(res["result"]["ok"])
        self.assertEqual(res["result"]["error"]["code"], "provider-change-requires-new-session")
        self.assertIn("新会话", res["result"]["error"]["message"])

    def test_same_provider_and_unbound_sessions_allowed(self):
        adapter = self._adapter()
        adapter.registry.set_provider("s1", "p1")
        same = adapter.rpc("clean", "session.selectModel",
                           {"sessionId": "s1", "model": "m2", "provider": "p1"})
        self.assertTrue(same["result"]["ok"])
        unbound = adapter.rpc("clean", "session.selectModel",
                              {"sessionId": "fresh", "model": "m1", "provider": "pX"})
        self.assertTrue(unbound["result"]["ok"])
        model, effort = adapter._session_model_overrides("fresh")
        self.assertEqual(model, "m1")


# --- 11. thread params per provider --------------------------------------------------------------


class TestProviderThreadParams(ProviderTestCase):
    def test_custom_provider_params(self):
        adapter = CodexRuntimeAdapter(default_cwd=str(self.root), providers=self.mgr, state_root=str(self.root / "host-state"))
        self.save_mock()
        self.assertEqual(adapter._provider_thread_params("mock-provider"),
                         {"modelProvider": "mock-provider", "model": "mock-1"})

    def test_chatgpt_and_unknown_providers_return_empty(self):
        adapter = CodexRuntimeAdapter(default_cwd=str(self.root), providers=self.mgr, state_root=str(self.root / "host-state"))
        self.assertEqual(adapter._provider_thread_params(BUILTIN_CHATGPT_ID), {})
        self.assertEqual(adapter._provider_thread_params("never-saved"), {})
        self.assertEqual(adapter._provider_thread_params(""), {})


# --- 12. ProfileStore persistence round-trip -------------------------------------------------------


class TestProfileStore(ProviderTestCase):
    def test_round_trip(self):
        data = {
            "schema": 1,
            "activeProviderId": "custom-x",
            "providers": [{
                "id": "custom-x", "name": "X", "type": "custom",
                "baseUrl": "http://127.0.0.1:18652/v1", "wireApi": "responses",
                "envKey": "LAOMO_CODEX_PROVIDER_CUSTOM_X_KEY",
                "models": [{"id": "m1", "label": "M1"}], "defaultModel": "m1",
                "enabled": True, "builtin": False,
            }],
        }
        ProfileStore(self.root).save(data)
        # a fresh store over the same directory reads the same data back
        self.assertEqual(ProfileStore(self.root).load(), data)
        # atomic write left no tmp file behind
        self.assertTrue((self.root / "providers.json").exists())
        self.assertFalse((self.root / "providers.json.tmp").exists())
        # JSON on disk never contains a secret field
        text = (self.root / "providers.json").read_text("utf-8")
        self.assertNotIn("secret", text.lower())

    def test_missing_file_returns_default(self):
        loaded = ProfileStore(self.root).load()
        self.assertEqual(loaded, {"schema": 1, "activeProviderId": BUILTIN_CHATGPT_ID,
                                  "providers": []})

    def test_corrupt_file_falls_back_to_default(self):
        (self.root / "providers.json").write_text("{not json at all", "utf-8")
        loaded = ProfileStore(self.root).load()
        self.assertEqual(loaded["activeProviderId"], BUILTIN_CHATGPT_ID)
        self.assertEqual(loaded["providers"], [])
        # valid JSON but the wrong shape also falls back
        (self.root / "providers.json").write_text(json.dumps([1, 2, 3]), "utf-8")
        self.assertEqual(ProfileStore(self.root).load()["schema"], 1)


# --- 13. CredentialStore session-only semantics -----------------------------------------------------


class TestCredentialStoreFake(unittest.TestCase):
    def test_session_only_semantics(self):
        creds = FakeCredentialStore(persistent=False)
        self.assertFalse(creds.persistent)
        self.assertIn("本次运行", creds.storage_description())
        self.assertIsNone(creds.get("p1"))
        self.assertFalse(creds.has("p1"))
        creds.set("p1", "  sk-fake-one  ")  # stripped like the real store
        self.assertEqual(creds.get("p1"), "sk-fake-one")
        self.assertTrue(creds.has("p1"))
        with self.assertRaises(ProviderError):
            creds.set("p1", "   ")  # empty secrets rejected
        creds.delete("p1")
        self.assertFalse(creds.has("p1"))
        self.assertIsNone(creds.get("p1"))

    def test_persistent_description(self):
        creds = FakeCredentialStore(persistent=True)
        self.assertTrue(creds.persistent)
        self.assertIn("钥匙串", creds.storage_description())

    def test_no_keychain_subprocess_is_ever_invoked(self):
        creds = FakeCredentialStore()
        tmp = tempfile.TemporaryDirectory(prefix="laomo-creds-test-")
        self.addCleanup(tmp.cleanup)
        mgr = ProviderProfileManager(Path(tmp.name), creds)
        with mock.patch.object(pp.subprocess, "run",
                               side_effect=AssertionError("keychain touched")):
            mgr.save_profile({
                "name": "No Keychain", "type": "custom",
                "baseUrl": "http://127.0.0.1:18652/v1",
                "models": [{"id": "m1"}], "defaultModel": "m1",
                "secret": "sk-fake-nokeychain",
            })
            self.assertTrue(mgr.credentials.has("no-keychain"))
            mgr.activate("no-keychain")
            self.assertEqual(mgr.env_for_process(),
                             {"LAOMO_CODEX_PROVIDER_NO_KEYCHAIN_KEY": "sk-fake-nokeychain"})
            mgr.public_list()
            mgr.delete_profile("no-keychain")


# --- mock Responses server (in-process, ephemeral port) ----------------------------------------------


class TestMockResponsesServer(unittest.TestCase):
    server = None
    sse_server = None

    @classmethod
    def setUpClass(cls):
        cls.server = mock_responses_server.make_server("127.0.0.1", 0)
        cls.sse_server = mock_responses_server.make_server("127.0.0.1", 0, sse=True)
        import threading
        for srv in (cls.server, cls.sse_server):
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.sse_base = f"http://127.0.0.1:{cls.sse_server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.sse_server.shutdown()
        cls.sse_server.server_close()
        mock_responses_server.clear_log()

    def _post(self, base, path, body, auth="Bearer sk-fake-mock"):
        req = urllib.request.Request(base + path, method="POST",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        if auth is not None:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _get(self, base, path):
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_ok_non_streaming_shape(self):
        mock_responses_server.clear_log()
        status, headers, raw = self._post(self.base, "/v1/responses",
                                          {"model": "mock-1", "input": "hi"})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("application/json"))
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(body["id"], "resp_mock")
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["output"][0]["role"], "assistant")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(body["output"][0]["content"][0]["text"], "OK")
        self.assertIn("usage", body)
        # alias path without /v1 prefix behaves identically
        status2, _, raw2 = self._post(self.base, "/responses", {"model": "mock-1"})
        self.assertEqual(status2, 200)
        self.assertEqual(json.loads(raw2.decode("utf-8"))["id"], "resp_mock")

    def test_authorization_enforced(self):
        for label, auth in [("missing", None), ("wrong scheme", "Basic abc"),
                            ("empty value", ""), ("empty bearer", "Bearer   ")]:
            with self.subTest(label=label):
                status, _, raw = self._post(self.base, "/v1/responses",
                                            {"model": "mock-1"}, auth=auth)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(raw.decode("utf-8"))["error"]["message"],
                                 "Invalid API key")

    def test_model_validation(self):
        status, _, raw = self._post(self.base, "/v1/responses", {"input": "no model"})
        self.assertEqual(status, 400)
        status, _, raw = self._post(self.base, "/v1/responses", {"model": "missing-model"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw.decode("utf-8"))["error"]["message"], "model not found")

    def test_sse_stream_mode(self):
        # per-request stream:true on the plain server
        status, headers, raw = self._post(self.base, "/v1/responses",
                                          {"model": "mock-1", "stream": True})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/event-stream"))
        text = raw.decode("utf-8")
        self.assertIn("event: response.created", text)
        self.assertIn("event: response.output_text.delta", text)
        self.assertIn('"delta": "OK"', text)
        self.assertIn("event: response.completed", text)
        # --sse server streams even without stream:true
        status, headers, raw = self._post(self.sse_base, "/v1/responses", {"model": "mock-1"})
        self.assertEqual(status, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/event-stream"))
        self.assertIn("event: response.completed", raw.decode("utf-8"))
        # auth/model rules still apply in SSE mode
        status, _, _ = self._post(self.sse_base, "/v1/responses",
                                  {"model": "mock-1"}, auth=None)
        self.assertEqual(status, 401)

    def test_request_log_summary(self):
        mock_responses_server.clear_log()
        self._post(self.base, "/v1/responses", {"model": "mock-1"})
        self._post(self.base, "/v1/responses", {"model": "missing-model"})
        self._post(self.base, "/v1/responses", {"model": "mock-1"}, auth="wrong")
        status, payload = self._get(self.base, "/__test_log")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 3)
        entries = payload["requests"]
        by_status = {e["status"]: e for e in entries}
        self.assertEqual(set(by_status), {200, 404, 401})
        ok_entry = by_status[200]
        self.assertEqual((ok_entry["method"], ok_entry["path"], ok_entry["model"]),
                         ("POST", "/v1/responses", "mock-1"))
        self.assertTrue(ok_entry["authOk"])
        self.assertIn("Bearer", ok_entry["auth"])  # redacted, token never logged
        self.assertNotIn("sk-fake-mock", json.dumps(entries))
        self.assertEqual(by_status[404]["model"], "missing-model")
        self.assertFalse(by_status[401]["authOk"])


# --- 11. quick-start presets ---------------------------------------------------------


class TestPresets(ProviderTestCase):
    def test_public_list_carries_preset_catalogue(self):
        listing = self.mgr.public_list()
        presets = listing.get("presets") or []
        self.assertTrue(presets, "preset catalogue must not be empty")
        ids = [p["id"] for p in presets]
        self.assertEqual(len(ids), len(set(ids)), "preset ids must be unique")
        for preset in presets:
            for field in ("id", "name", "baseUrl", "note"):
                self.assertTrue(str(preset.get(field) or "").strip(), field)
            self.assertRegex(preset["baseUrl"], r"^https?://")
        # presets are data only: the returned copies never mutate the catalog
        listing["presets"][0]["name"] = "tampered"
        self.assertNotEqual(self.mgr.public_list()["presets"][0]["name"], "tampered")

    def test_every_preset_base_url_passes_profile_validation(self):
        # A preset baseUrl that save_profile would reject is a bug: the form
        # pre-fills it and the user should never hit a validation error on it.
        for preset in self.mgr.public_list()["presets"]:
            try:
                self.mgr.save_profile({
                    "name": f"Preset {preset['id']}",
                    "type": "custom",
                    "baseUrl": preset["baseUrl"],
                    "models": [{"id": "m-1", "label": "M1"}],
                    "secret": FAKE_SECRET,
                })
            except ProviderError as exc:
                self.fail(f"preset {preset['id']} baseUrl rejected: {exc}")


# --- 12. model discovery (GET {base}/models) ----------------------------------------


class TestDiscoverModels(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = mock_responses_server.make_server("127.0.0.1", 0)
        import threading
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="laomo-discover-test-")
        self.addCleanup(tmp.cleanup)
        self.creds = FakeCredentialStore()
        self.mgr = ProviderProfileManager(Path(tmp.name), self.creds)

    def _closed_port_base(self) -> str:
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{sock.getsockname()[1]}"

    def test_happy_path_from_draft_form(self):
        result = self.mgr.discover_models(base_url=f"{self.base}/v1",
                                          secret="sk-fake-mock")
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["mock-one", "mock-two", "missing-model"])
        # the catalogue request carried the bearer token, path /v1/models
        status, payload = self._get_log()
        entry = payload["requests"][-1]
        self.assertEqual((entry["method"], entry["path"], entry["status"]),
                         ("GET", "/v1/models", 200))
        self.assertNotIn("sk-fake-mock", json.dumps(payload))

    def _get_log(self):
        with urllib.request.urlopen(f"{self.base}/__test_log", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_auth_failure_without_secret(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(base_url=f"{self.base}/v1")
        self.assertEqual(ctx.exception.code, "auth-failed")

    def test_missing_models_endpoint_is_protocol_incompatible(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(base_url=f"{self.base}/nope", secret="sk-fake-mock")
        self.assertEqual(ctx.exception.code, "protocol-incompatible")

    def test_unreachable_endpoint(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(base_url=self._closed_port_base(), secret="sk-x")
        self.assertEqual(ctx.exception.code, "unreachable")

    def test_invalid_base_url_rejected(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(base_url="not-a-url")
        self.assertEqual(ctx.exception.code, "invalid")

    def test_saved_profile_uses_stored_credential(self):
        pub = self.mgr.save_profile({
            "name": "Discover Me", "type": "custom",
            "baseUrl": f"{self.base}/v1",
            "models": [{"id": "placeholder", "label": "Placeholder"}],
            "secret": "sk-fake-mock",
        })
        result = self.mgr.discover_models(profile_id=pub["id"])  # no secret passed
        self.assertEqual(result["models"], ["mock-one", "mock-two", "missing-model"])
        # the candidate secret is never persisted by discovery
        self.assertEqual(self.creds.set_calls, [(pub["id"], "sk-fake-mock")])

    def test_builtin_and_unknown_profiles_rejected(self):
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(profile_id=BUILTIN_CHATGPT_ID)
        self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(ProviderError) as ctx:
            self.mgr.discover_models(profile_id="no-such-provider")
        self.assertEqual(ctx.exception.code, "invalid")


# --- 13. session.create model precedence --------------------------------------------


class _CreateFakeRpc:
    """FakeRpc, but thread/start answers with a usable threadId."""

    def __init__(self):
        self.requests: list[dict] = []

    def request(self, method, params=None, timeout=60.0):
        self.requests.append({"method": method, "params": params})
        return {"threadId": "t-create-1"}


class TestSessionCreateModelDefaults(unittest.TestCase):
    def _adapter(self):
        tmp = tempfile.TemporaryDirectory(prefix="laomo-create-test-")
        self.addCleanup(tmp.cleanup)
        adapter = CodexRuntimeAdapter(default_cwd=tmp.name, state_root=os.path.join(tmp.name, "host-state"))
        proc = FakeProcess()
        proc.rpc = _CreateFakeRpc()
        adapter.process = proc
        return adapter

    def _save_selection(self, adapter, **data):
        updated = adapter._state.settings_update("model-selection", data, None)
        self.assertIsNotNone(updated)

    def test_host_default_applied_when_provider_matches(self):
        adapter = self._adapter()
        self._save_selection(adapter, model="gpt-5.6-luna", provider="chatgpt",
                             reasoningEffort="high")
        res = adapter.rpc("clean", "session.create", {})
        self.assertTrue(res["result"]["ok"])
        params = adapter.process.rpc.requests[0]["params"]
        self.assertEqual(params.get("model"), "gpt-5.6-luna")
        self.assertEqual(params.get("effort"), "high")
        # registry mirrors it, so session.models reports the right current
        sid = res["result"]["value"]["sessionId"]
        self.assertEqual(adapter._session_model_overrides(sid), ("gpt-5.6-luna", "high"))

    def test_host_default_ignored_for_other_provider(self):
        adapter = self._adapter()
        self._save_selection(adapter, model="deepseek-chat", provider="deepseek",
                             reasoningEffort="low")
        adapter.rpc("clean", "session.create", {})
        params = adapter.process.rpc.requests[0]["params"]
        self.assertNotIn("model", params)
        self.assertNotIn("effort", params)

    def test_explicit_body_overrides_host_default(self):
        adapter = self._adapter()
        self._save_selection(adapter, model="saved-model", provider="chatgpt",
                             reasoningEffort="high")
        adapter.rpc("clean", "session.create", {"model": "explicit-model",
                                                "reasoningEffort": "low"})
        params = adapter.process.rpc.requests[0]["params"]
        self.assertEqual(params.get("model"), "explicit-model")
        self.assertEqual(params.get("effort"), "low")

    def test_no_saved_default_leans_on_provider_params(self):
        adapter = self._adapter()
        adapter.rpc("clean", "session.create", {})
        params = adapter.process.rpc.requests[0]["params"]
        self.assertNotIn("model", params)
        self.assertNotIn("effort", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
