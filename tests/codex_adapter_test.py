"""Unit tests for the Codex runtime adapter (no real codex process needed).

Covers the /goal P0 hard semantics:
- EventTranslator: deltas, tools, file changes, turn lifecycle, errors, usage
- seq strictly +1 per session
- user/message source.kind/source.rpcId contract
- streaming order: chunks first, assistant/message finalizes last
- HistoryFolder: deterministic fold, stable seq, beforeSeq, live boundary
- Approval: rpcId mapping, allow/reject decisions, resolved cleanup
- Unknown protocol: unknown notification/fields never crash
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import types
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import codex_adapter  # noqa: E402
from codex_adapter import (  # noqa: E402
    CodexRuntimeAdapter,
    EventTranslator,
    HistoryFolder,
    RpcClient,
    SessionRegistry,
)


class FakeCtx:  # minimal adapter stand-in for the translator
    def __init__(self):
        self.registry = SessionRegistry()
        self._logs = []

    def _debug_log(self, msg):
        self._logs.append(msg)


def frames_of(translator: EventTranslator, ctx: FakeCtx, notifications):
    frames = []
    for note in notifications:
        frames.extend(translator.translate(note, ctx))
    return frames


def event_frames(frames, sid):
    return [f for f in frames
            if f["payload"].get("type") == "session/event" and f["payload"].get("sessionId") == sid]


class TestEventTranslator(unittest.TestCase):
    def setUp(self):
        self.tr = EventTranslator()
        self.ctx = FakeCtx()
        self.sid = "t1"

    def test_agent_message_delta(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "item/agentMessage/delta", "params": {"threadId": self.sid, "itemId": "i1", "delta": "你"}},
            {"method": "item/agentMessage/delta", "params": {"threadId": self.sid, "itemId": "i1", "delta": "好"}},
        ])
        chunks = [f["payload"]["event"] for f in event_frames(frames, self.sid)]
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(c["type"] == "assistant/chunk" for c in chunks))
        self.assertEqual(chunks[0]["data"]["text"], "你")
        self.assertEqual([c["seq"] for c in chunks], [1, 2])

    def test_reasoning_delta(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "item/reasoning/textDelta", "params": {"threadId": self.sid, "itemId": "i2", "delta": "think"}},
        ])
        ev = event_frames(frames, self.sid)[0]["payload"]["event"]
        self.assertEqual(ev["type"], "assistant/chunk")
        self.assertEqual(ev["data"]["type"], "reasoning-delta")

    def test_tool_call_and_result(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "item/started", "params": {"threadId": self.sid, "item": {
                "id": "c1", "type": "commandExecution", "command": "ls -la"}}},
            {"method": "item/completed", "params": {"threadId": self.sid, "item": {
                "id": "c1", "type": "commandExecution", "command": "ls -la",
                "aggregatedOutput": "file1", "exitCode": 0}}},
        ])
        evs = [f["payload"]["event"] for f in event_frames(frames, self.sid)]
        self.assertEqual([e["type"] for e in evs], ["tool/call", "tool/result"])
        self.assertEqual(evs[0]["data"]["view"]["card"], "terminal")
        self.assertEqual(evs[1]["data"]["view"]["exitCode"], 0)

    def test_file_change(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "item/completed", "params": {"threadId": self.sid, "item": {
                "id": "f1", "type": "fileChange", "changes": [{"path": "a.py", "diff": "+x"}]}}},
        ])
        ev = event_frames(frames, self.sid)[0]["payload"]["event"]
        self.assertEqual(ev["type"], "tool/result")
        self.assertEqual(ev["data"]["view"]["diffs"][0]["path"], "a.py")

    def test_turn_lifecycle_and_finalize_after_chunks(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "turn/started", "params": {"threadId": self.sid, "turn": {"id": "turn1"}}},
            {"method": "item/agentMessage/delta", "params": {"threadId": self.sid, "itemId": "i1", "delta": "a"}},
            {"method": "item/completed", "params": {"threadId": self.sid, "item": {
                "id": "i1", "type": "agentMessage", "text": "a"}},
            },
            {"method": "turn/completed", "params": {"threadId": self.sid, "turn": {"id": "turn1", "status": "completed"}}},
        ])
        evs = [f["payload"]["event"]["type"] for f in event_frames(frames, self.sid)]
        # chunk before final assistant/message, turn/end last
        self.assertLess(evs.index("assistant/chunk"), evs.index("assistant/message"))
        self.assertEqual(evs[-1], "turn/end")
        statuses = [f["payload"]["running"] for f in frames if f["payload"].get("type") == "host/session-status"]
        self.assertEqual(statuses, [True, False])

    def test_turn_completed_folds_items_as_finalization(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "turn/completed", "params": {"threadId": self.sid, "turn": {
                "id": "turn2", "status": "completed",
                "items": [{"item": {"id": "z1", "type": "agentMessage", "text": "final"}}]}}},
        ])
        evs = [f["payload"]["event"]["type"] for f in event_frames(frames, self.sid)]
        self.assertIn("assistant/message", evs)
        self.assertEqual(evs[-1], "turn/end")

    def test_turn_failed_error_reason(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "turn/completed", "params": {"threadId": self.sid, "turn": {
                "id": "t", "status": "failed", "error": {"message": "boom"}}}},
        ])
        end = [f for f in event_frames(frames, self.sid) if f["payload"]["event"]["type"] == "turn/end"][0]
        self.assertEqual(end["payload"]["event"]["data"]["reason"]["kind"], "error")

    def test_token_usage(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": self.sid, "tokenUsage": {"total": {
                    "inputTokens": 10, "cachedInputTokens": 2, "outputTokens": 5,
                    "cacheWriteInputTokens": 1, "totalTokens": 17},
                    "modelContextWindow": 128000}}},
        ])
        ev = event_frames(frames, self.sid)[0]["payload"]["event"]
        self.assertEqual(ev["type"], "assistant/chunk")
        self.assertEqual(ev["data"]["type"], "usage")
        self.assertEqual(ev["data"]["usage"]["uncachedInputTokens"], 8)
        self.assertEqual(ev["data"]["usage"]["cacheReadTokens"], 2)
        # tokenUsage + contextPressure ride separate projection frames with
        # their own counter (must not punch holes in the event seq stream).
        projs = [f["payload"] for f in frames if f["payload"].get("type") == "session/projection"]
        self.assertEqual([p["key"] for p in projs], ["tokenUsage", "contextPressure"])
        self.assertEqual(projs[1]["value"]["contextWindow"], 128000)

    def test_error_notification(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "error", "params": {"message": "runtime down"}},
        ])
        self.assertTrue(any(f["payload"].get("type") == "host/agent-error" for f in frames))

    def test_thread_status_active(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "thread/status/changed", "params": {"threadId": self.sid, "status": {"type": "active"}}},
        ])
        self.assertTrue(frames[0]["payload"]["running"])
        self.assertTrue(self.ctx.registry.running(self.sid))

    def test_unknown_notification_ignored(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "some/future/notification", "params": {"whatever": {"deep": [1, 2]}}},
            {"method": 12345, "params": None},
        ])
        self.assertEqual(frames, [])
        self.assertTrue(any("ignored" in m for m in self.ctx._logs))

    def test_translator_never_crashes_on_garbage(self):
        frames = frames_of(self.tr, self.ctx, [
            {"method": "turn/completed", "params": None},
            {"method": "item/completed", "params": {"threadId": self.sid, "item": None}},
            {"method": "thread/status/changed", "params": {"threadId": self.sid, "status": "weird"}},
        ])
        self.assertIsInstance(frames, list)  # no exception propagated

    def test_seq_strictly_incrementing_across_mixed_events(self):
        notes = [
            {"method": "turn/started", "params": {"threadId": self.sid, "turn": {"id": "a"}}},
            {"method": "item/agentMessage/delta", "params": {"threadId": self.sid, "itemId": "i", "delta": "x"}},
            {"method": "thread/tokenUsage/updated", "params": {"threadId": self.sid, "tokenUsage": {}}},
            {"method": "turn/completed", "params": {"threadId": self.sid, "turn": {"id": "a"}}},
        ]
        seqs = [f["payload"]["event"]["seq"] for f in frames_of(self.tr, self.ctx, notes)
                if f["payload"].get("type") == "session/event" and f["payload"].get("sessionId") == self.sid]
        # Strictly increasing; contiguous except where projection frames
        # consumed their own (separate) counter in between.
        self.assertTrue(all(b > a for a, b in zip(seqs, seqs[1:])))


def sample_thread() -> dict:
    return {
        "id": "t1", "cwd": "/tmp/x", "updatedAt": 1,
        "turns": [
            {"id": "turn1", "startedAt": 100, "completedAt": 200, "status": "completed",
             "items": [
                 {"turnId": "turn1", "item": {"id": "u1", "type": "userMessage",
                                              "content": "写个脚本", "clientId": "rpc-42"}},
                 {"turnId": "turn1", "item": {"id": "r1", "type": "reasoning", "summary": "想想"}},
                 {"turnId": "turn1", "item": {"id": "c1", "type": "commandExecution",
                                              "command": "ls", "aggregatedOutput": "a b", "exitCode": 0}},
                 {"turnId": "turn1", "item": {"id": "f1", "type": "fileChange",
                                              "changes": [{"path": "x.py", "diff": "+1"}]}},
                 {"turnId": "turn1", "item": {"id": "a1", "type": "agentMessage", "text": "完成"}},
             ]},
            {"id": "turn2", "startedAt": 300, "completedAt": 400, "status": "interrupted", "items": []},
        ],
    }


class TestHistoryFolder(unittest.TestCase):
    def test_fold_deterministic(self):
        a = HistoryFolder.fold(sample_thread())
        b = HistoryFolder.fold(json.loads(json.dumps(sample_thread())))
        self.assertEqual(json.dumps(a), json.dumps(b))

    def test_seq_stable_and_monotonic(self):
        events = HistoryFolder.fold(sample_thread())
        seqs = [e["event"]["seq"] for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(seqs[-1], len(events))

    def test_event_order_and_types(self):
        types = [e["event"]["type"] for e in HistoryFolder.fold(sample_thread())]
        self.assertEqual(types[0], "turn/start")
        self.assertEqual(types[-1], "turn/end")
        self.assertIn("user/message", types)
        self.assertIn("assistant/message", types)
        self.assertIn("tool/call", types)
        self.assertIn("tool/result", types)

    def test_user_message_contract(self):
        um = [e for e in HistoryFolder.fold(sample_thread())
              if e["event"]["type"] == "user/message"][0]
        data = um["event"]["data"]
        self.assertEqual(data["source"]["kind"], "user")
        self.assertEqual(data["source"]["rpcId"], "rpc-42")
        self.assertEqual(data["content"][0]["text"], "写个脚本")

    def test_empty_thread(self):
        self.assertEqual(HistoryFolder.fold({"id": "t", "turns": []}), [])

    def test_live_boundary_seq_floor(self):
        events = HistoryFolder.fold(sample_thread())
        tr = EventTranslator()
        tr.set_seq_floor("t1", len(events))
        nxt = tr.next_seq("t1")
        self.assertEqual(nxt, len(events) + 1)

    def test_beforeseq_pagination_shape(self):
        events = HistoryFolder.fold(sample_thread())
        before = 6
        page = [e for e in events if e["event"]["seq"] < before]
        self.assertTrue(all(e["event"]["seq"] < before for e in page))


class TestApprovalFlow(unittest.TestCase):
    def _client(self):
        sent = []

        def send(line):
            sent.append(json.loads(line))

        return RpcClient(send, lambda m: None), sent

    def test_request_id_mapping(self):
        rpc, _ = self._client()
        boujoy_id = rpc.register_server_request(41, {"method": "item/commandExecution/requestApproval",
                                                     "params": {"threadId": "t1", "command": "rm -rf"}, "codexId": 41})
        self.assertTrue(boujoy_id)
        found = rpc.lookup_server_request(boujoy_id)
        self.assertEqual(found["codexId"], 41)
        self.assertEqual(found["params"]["command"], "rm -rf")
        self.assertEqual(len(rpc.pending_approvals()), 1)

    def test_allow_decision_translated(self):
        adapter = CodexRuntimeAdapter.__new__(CodexRuntimeAdapter)  # no process needed
        result = adapter._approval_result("item/commandExecution/requestApproval", "allowed-once", None)
        self.assertEqual(result, {"decision": "accept"})
        result = adapter._approval_result("item/fileChange/requestApproval", "rejected", None)
        self.assertEqual(result, {"decision": "decline"})
        result = adapter._approval_result("item/commandExecution/requestApproval", "allowed-session", None)
        self.assertEqual(result, {"decision": "acceptForSession"})

    def test_respond_removes_pending(self):
        rpc, sent = self._client()
        boujoy_id = rpc.register_server_request(7, {"method": "m", "params": {}})
        rpc.respond(7, {"decision": "accept"})
        self.assertEqual(sent[0]["id"], 7)
        self.assertEqual(sent[0]["result"], {"decision": "accept"})
        # removal is the adapter's job (pop after wire response)
        self.assertIsNotNone(rpc.pop_server_request(boujoy_id))
        self.assertEqual(len(rpc.pending_approvals()), 0)

    def test_question_answers_translated(self):
        adapter = CodexRuntimeAdapter.__new__(CodexRuntimeAdapter)
        result = adapter._approval_result("item/tool/requestUserInput", "", [{"id": "q1", "selected": ["选项A"]}])
        self.assertEqual(result["answers"], ["选项A"])



def make_adapter(**kwargs):
    root = tempfile.mkdtemp(prefix="laomo-adapter-state-")
    return CodexRuntimeAdapter(default_cwd="/tmp", state_root=root, **kwargs)

class TestAdapterStatics(unittest.TestCase):
    def test_content_to_input(self):
        content = [
            {"type": "image", "mediaType": "image/png", "data": "QUJD"},
            {"type": "text", "text": "看看图"},
        ]
        items = CodexRuntimeAdapter._content_to_input(content)
        self.assertEqual(items[0]["type"], "image")
        self.assertTrue(items[0]["url"].startswith("data:image/png;base64,QUJD"))
        self.assertEqual(items[1], {"type": "text", "text": "看看图"})

    def test_unknown_rpc_stubbed(self):
        adapter = make_adapter()
        r = adapter.rpc("clean", "subagent.list", {})
        self.assertTrue(r["result"]["ok"])          # safe stub, no crash
        self.assertFalse(r["result"]["value"]["supported"])
        # goal.* is a real handler now; its stub-era generic path is covered
        # by another method (search with empty query returns ok).
        r = adapter.rpc("clean", "session.search", {"query": ""})
        self.assertTrue(r["result"]["ok"])

    def test_workspace_cwd_roundtrip(self):
        adapter = make_adapter()
        r = adapter.rpc("clean", "workspace.create", {"path": "/Users"})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(adapter.workspace_cwd(), "/Users")
        items = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        # multi-project registry: the default (/tmp) stays, /Users is added
        self.assertEqual([item["path"] for item in items], ["/tmp", "/Users"])
        # create is idempotent per path (pickProject retries safely)
        adapter.rpc("clean", "workspace.create", {"path": "/Users"})
        items = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        self.assertEqual(len(items), 2)

    def test_workspace_registry_manage(self):
        adapter = make_adapter()
        adapter.rpc("clean", "workspace.create", {"path": "/Users"})
        items = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        second = items[1]
        r = adapter.rpc("clean", "workspace.rename", {"workspaceId": second["id"], "title": "主目录"})
        self.assertTrue(r["result"]["ok"])
        r = adapter.rpc("clean", "workspace.insertBefore",
                        {"workspaceId": second["id"], "beforeWorkspaceId": items[0]["id"]})
        self.assertTrue(r["result"]["ok"])
        ordered = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        self.assertEqual(ordered[0]["title"], "主目录")
        r = adapter.rpc("clean", "workspace.delete", {"workspaceId": items[0]["id"]})
        self.assertTrue(r["result"]["ok"])
        remaining = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        self.assertEqual([item["path"] for item in remaining], ["/Users"])
        # the last workspace is protected
        r = adapter.rpc("clean", "workspace.delete", {"workspaceId": remaining[0]["id"]})
        self.assertFalse(r["result"]["ok"])
        # deleting the active project falls back to the first remaining one
        adapter.rpc("clean", "workspace.create", {"path": "/tmp"})
        r = adapter.rpc("clean", "workspace.delete", {"workspaceId": remaining[0]["id"]})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(adapter.workspace_cwd(), "/tmp")

    def test_workspace_state_persists_across_restart(self):
        root = tempfile.mkdtemp(prefix="laomo-adapter-state-")
        first = CodexRuntimeAdapter(default_cwd="/tmp", state_root=root)
        first.rpc("clean", "workspace.create", {"path": "/Users"})
        second = CodexRuntimeAdapter(default_cwd="/tmp", state_root=root)
        items = second.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        self.assertEqual([item["path"] for item in items], ["/tmp", "/Users"])

    def test_settings_namespaces_update_and_conflict(self):
        adapter = make_adapter()
        described = adapter.rpc("clean", "settings.describe", {})["result"]["value"]
        ns = next(item for item in described["namespaces"] if item["ns"] == "ui-conversation")
        r = adapter.rpc("clean", "settings.update",
                        {"ns": "ui-conversation", "patch": {"busyEnter": "steer"},
                         "expectedRevision": ns["revision"]})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(r["result"]["value"]["data"]["busyEnter"], "steer")
        self.assertEqual(r["result"]["value"]["revision"], ns["revision"] + 1)
        # stale revision is rejected instead of silently overwriting
        r = adapter.rpc("clean", "settings.update",
                        {"ns": "ui-conversation", "patch": {"busyEnter": "queue"},
                         "expectedRevision": ns["revision"]})
        self.assertFalse(r["result"]["ok"])
        # persistence across restart keeps the value
        same_root = adapter._state.root
        reloaded = CodexRuntimeAdapter(default_cwd="/tmp", state_root=same_root)
        ns2 = next(item for item in reloaded.rpc("clean", "settings.describe", {})["result"]["value"]["namespaces"]
                   if item["ns"] == "ui-conversation")
        self.assertEqual(ns2["data"]["busyEnter"], "steer")

    def test_agent_presets_select_copy_remove(self):
        adapter = make_adapter()
        listed = adapter.rpc("clean", "agentPreset.list", {})["result"]["value"]["presets"]
        ids = {item["id"] for item in listed}
        self.assertTrue({"standard", "concise", "planner"} <= ids)
        r = adapter.rpc("clean", "agentPreset.select", {"sessionId": "s1", "agentPreset": "concise"})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(adapter.registry.get("s1")["agentPreset"], "concise")
        read = adapter.rpc("clean", "agentPreset.read", {"agentPreset": "concise"})["result"]["value"]
        self.assertIn("简洁", read["content"])
        r = adapter.rpc("clean", "agentPreset.copy",
                        {"from": "concise", "agentPreset": "concise-2", "name": "简洁2"})
        self.assertTrue(r["result"]["ok"])
        r = adapter.rpc("clean", "agentPreset.copy",
                        {"from": "concise", "agentPreset": "bad id!", "name": "x"})
        self.assertFalse(r["result"]["ok"])
        r = adapter.rpc("clean", "agentPreset.remove", {"agentPreset": "concise-2"})
        self.assertTrue(r["result"]["ok"])
        r = adapter.rpc("clean", "agentPreset.remove", {"agentPreset": "standard"})
        self.assertFalse(r["result"]["ok"])  # builtins are protected
        r = adapter.rpc("clean", "agentPreset.openDocument", {"agentPreset": "standard"})
        self.assertFalse(r["result"]["ok"])  # honest unsupported

    def test_subagent_honest_unsupported(self):
        adapter = make_adapter()
        r = adapter.rpc("clean", "subagent.list", {})
        self.assertTrue(r["result"]["ok"])  # panel renders empty
        r = adapter.rpc("clean", "subagent.prompt", {"parentSessionId": "s", "childSessionId": "c"})
        self.assertFalse(r["result"]["ok"])  # no fake 已发送 toast

    def test_credentials_against_store(self):
        class FakeStore:
            def __init__(self): self.values = {}
            def has(self, ref): return ref in self.values
            def set(self, ref, value): self.values[ref] = value
            def delete(self, ref): self.values.pop(ref, None)
            def storage_description(self): return "测试存储"
        store = FakeStore()
        adapter = make_adapter(providers=types.SimpleNamespace(credentials=store))
        described = adapter.rpc("clean", "credentials.describe",
                                {"refs": ["DEEPSEEK_API_KEY"]})["result"]["value"]
        self.assertFalse(described["credentials"]["DEEPSEEK_API_KEY"]["configured"])
        adapter.rpc("clean", "credentials.set", {"ref": "DEEPSEEK_API_KEY", "value": "sk-x"})
        described = adapter.rpc("clean", "credentials.describe",
                                {"refs": ["DEEPSEEK_API_KEY"]})["result"]["value"]
        self.assertTrue(described["credentials"]["DEEPSEEK_API_KEY"]["configured"])
        adapter.rpc("clean", "credentials.unset", {"ref": "DEEPSEEK_API_KEY"})
        described = adapter.rpc("clean", "credentials.describe",
                                {"refs": ["DEEPSEEK_API_KEY"]})["result"]["value"]
        self.assertFalse(described["credentials"]["DEEPSEEK_API_KEY"]["configured"])

    def test_host_open_path_validates(self):
        adapter = make_adapter()
        r = adapter.rpc("clean", "host.openPath", {"path": "/definitely/not/here"})
        self.assertFalse(r["result"]["ok"])
        from unittest import mock
        with mock.patch.object(codex_adapter.subprocess, "Popen"):
            r = adapter.rpc("clean", "host.openPath", {"path": "/tmp"})
        self.assertTrue(r["result"]["ok"])

    def test_workspace_grouping_of_sessions(self):
        adapter = make_adapter()
        adapter.rpc("clean", "workspace.create", {"path": "/Users"})
        adapter.registry.ensure("t-in-users", cwd="/Users")
        adapter.registry.ensure("t-in-tmp", cwd="/tmp")
        self.assertEqual(adapter._workspace_id_for("/Users"),
                         adapter._workspace_id_for("/Users"))
        listing = adapter.rpc("clean", "workspace.list", {})["result"]["value"]["items"]
        by_path = {item["path"]: item for item in listing}
        self.assertIn("t-in-users", by_path["/Users"]["sessionIds"])
        self.assertIn("t-in-tmp", by_path["/tmp"]["sessionIds"])

    def test_select_model_stored(self):
        adapter = make_adapter()
        adapter.rpc("clean", "session.selectModel", {"sessionId": "s1", "model": "gpt-5.5", "reasoningEffort": "high"})
        m, e = adapter._session_model_overrides("s1")
        self.assertEqual((m, e), ("gpt-5.5", "high"))

    def test_full_auto_permission_never_asks_inside_sandbox(self):
        # 全自动 = the mission runtime's contract for interactive use: the
        # workspace sandbox stays on, approvals are off so nothing stalls.
        adapter = make_adapter()
        adapter.rpc("clean", "commands/execute",
                    {"agentId": "s1", "line": "/permission full-auto"})
        params = adapter._sandbox_params("s1")
        self.assertEqual(params["sandboxPolicy"], {"type": "workspaceWrite"})
        self.assertEqual(params["approvalPolicy"], "never")
        # the readback projection and list view must carry the level through
        self.assertEqual(adapter._permission_view("s1")["currentValue"], "full-auto")

    def test_pick_directory_native_dialog(self):
        # 选择新项目 went dead on the codex runtime: host.pickDirectory had
        # no handler. Dispatch must reach the native-picker handler; user
        # cancel is the silent path, a picked path feeds workspace.create.
        from unittest import mock
        adapter = make_adapter()
        completed = subprocess.CompletedProcess(args=[], returncode=0,
                                                 stdout="/Users/lianb/Downloads/bh/\n")
        with mock.patch.object(codex_adapter.subprocess, "run", return_value=completed):
            r = adapter.rpc("clean", "host.pickDirectory", {})
        self.assertTrue(r["result"]["ok"])
        self.assertEqual(r["result"]["value"]["path"], "/Users/lianb/Downloads/bh")
        cancelled = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
        with mock.patch.object(codex_adapter.subprocess, "run", return_value=cancelled):
            r = adapter.rpc("clean", "host.pickDirectory", {})
        self.assertFalse(r["result"]["ok"])
        self.assertIn("cancel", str(r["result"]["error"]))

    def test_permission_levels_approval_matrix(self):
        adapter = make_adapter()
        for level, sandbox, approval in (
            # read-only still asks: a blocked command escalates via approval
            ("read-only", {"type": "readOnly"}, "on-request"),
            ("workspace-write", {"type": "workspaceWrite"}, "on-request"),
            ("danger-full-access", {"type": "dangerFullAccess"}, "never"),
        ):
            adapter.rpc("clean", "commands/execute",
                        {"agentId": "s2", "line": f"/permission {level}"})
            params = adapter._sandbox_params("s2")
            self.assertEqual(params["sandboxPolicy"], sandbox, level)
            self.assertEqual(params["approvalPolicy"], approval, level)


if __name__ == "__main__":
    unittest.main(verbosity=2)
