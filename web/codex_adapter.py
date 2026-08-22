"""Codex Runtime adapter for LaoMo Workbench (P0 Clean Runtime Migration).

Architecture (see /goal P0 doc):
    Boujoy server HTTP/WS  ->  RuntimeManager  ->  CodexRuntimeAdapter
                                                            |
                                                    codex app-server --stdio

All Codex-specific protocol knowledge lives in this file. The server handler
layer only speaks the existing DSH-shaped RPC/event protocol the frontend
already consumes. Key pieces:

- CodexProcess      spawn/initialize handshake/JSONL reader/restart with backoff
- RpcClient         id->future map + pending server-request (approval) registry
- EventTranslator   Codex notifications -> DSH event frames (single place)
- HistoryFolder     thread/read items -> deterministic DSH history events
- SessionRegistry   sessionId == codex threadId; running/model/effort/cwd state

Protocol facts below are verified against the schema exported from the local
binary (docs/codex-schema-0.148.0-alpha.21, codex 0.148.0-alpha.21), not from
memory: TurnStartParams {threadId, input[], model?, effort?, cwd?,
clientUserMessageId?}, ThreadItem variants (userMessage/agentMessage/reasoning/
commandExecution/fileChange/mcpToolCall/...), approval decisions
(accept/acceptForSession/decline/...), TurnStatus enum (completed/interrupted/
failed/inProgress).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable

# --- DSH-shaped response envelope helpers -----------------------------------


def ok_value(value: Any) -> dict[str, Any]:
    return {"type": "server-response", "result": {"ok": True, "value": value}}


def err_value(message: str, code: str = "runtime-error") -> dict[str, Any]:
    return {"type": "server-response", "result": {"ok": False, "error": {"code": code, "message": message}}}


class AdapterUnavailable(Exception):
    """Codex runtime not usable (spawn failed / crashed / initializing)."""


# --- EventTranslator ---------------------------------------------------------


class EventTranslator:
    """Codex notifications -> DSH event frames consumed by web/app.js.

    The frontend contract (hard semantics, unit-tested):
    - every session/event carries event.seq strictly +1 per session
    - user/message must include source.kind="user" and source.rpcId
    - assistant chunks stream first, assistant/message finalizes after them
    - approvals ride the mux stream as server-request frames with rpcId
    """

    def __init__(self) -> None:
        self._seq_lock = threading.Lock()
        self._seqs: dict[str, int] = {}
        # Projections ride their own counter: the frontend gap-detects the
        # session/event stream (seq must be contiguous); interleaving
        # projection seqs there would trigger constant history repulls.
        self._proj_seqs: dict[str, int] = {}
        self._stream_state: dict[str, dict[str, Any]] = {}
        self._finalized: dict[str, set[str]] = {}  # session -> emitted item ids
        self._current_turn: dict[str, str] = {}    # session -> turn id

    # -- seq allocation ------------------------------------------------------
    def next_seq(self, session_id: str) -> int:
        with self._seq_lock:
            n = self._seqs.get(session_id, 0) + 1
            self._seqs[session_id] = n
            return n

    def set_seq_floor(self, session_id: str, floor: int) -> None:
        """After history load, keep live seq strictly above folded history."""
        with self._seq_lock:
            self._seqs[session_id] = max(self._seqs.get(session_id, 0), floor)

    def last_seq(self, session_id: str) -> int:
        with self._seq_lock:
            return self._seqs.get(session_id, 0)

    # -- stream identity -----------------------------------------------------
    def _stream(self, session_id: str, turn_id: str) -> dict[str, Any]:
        key = f"{session_id}:{turn_id}"
        st = self._stream_state.get(key)
        if st is None:
            st = {"open": set(), "chunks": 0}
            self._stream_state[key] = st
        return st

    def close_all_streams(self, session_id: str) -> None:
        for key in [k for k in self._stream_state if k.startswith(f"{session_id}:")]:
            self._stream_state.pop(key, None)

    # -- frame builders ------------------------------------------------------
    def session_event(self, session_id: str, event_type: str, data: Any) -> dict[str, Any]:
        return {
            "type": "server-request",
            "payload": {
                "type": "session/event",
                "sessionId": session_id,
                "event": {"type": event_type, "data": data, "seq": self.next_seq(session_id),
                          "time": int(time.time() * 1000)},
            },
        }

    def session_subscribed(self, session_id: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "session/subscribed",
                                                      "sessionId": session_id, "lastSeq": self.last_seq(session_id)}}

    def session_projection(self, session_id: str, key: str, value: Any) -> dict[str, Any]:
        with self._seq_lock:
            seq = self._proj_seqs.get(session_id, 0) + 1
            self._proj_seqs[session_id] = seq
        return {"type": "server-request", "payload": {
            "type": "session/projection", "sessionId": session_id,
            "key": key, "value": value, "seq": seq}}

    def set_proj_floor(self, session_id: str, floor: int) -> None:
        with self._seq_lock:
            self._proj_seqs[session_id] = max(self._proj_seqs.get(session_id, 0), floor)

    def host_status(self, session_id: str, running: bool) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "host/session-status",
                                                      "sessionId": session_id, "running": running}}

    def session_added(self, session_id: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "host/session-added", "sessionId": session_id}}

    def session_removed(self, session_id: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "host/session-removed", "sessionId": session_id}}

    def agent_error(self, message: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "host/agent-error", "message": message}}

    def approval_requested(self, rpc_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        frame = {"type": "server-request", "rpcId": rpc_id, "payload": payload}
        return frame

    def approval_resolved(self, session_id: str, approval_id: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "approval/resolved",
                                                      "sessionId": session_id, "approvalId": approval_id}}

    def question_resolved(self, session_id: str, rpc_id: str) -> dict[str, Any]:
        return {"type": "server-request", "payload": {"type": "question/resolved",
                                                      "sessionId": session_id, "questionRpcId": rpc_id}}

    # -- Codex notification translation ---------------------------------------
    def translate(self, note: dict[str, Any], ctx: "CodexRuntimeAdapter") -> list[dict[str, Any]]:
        """Return DSH frames for one Codex notification; unknown methods are
        logged and safely ignored."""
        method = note.get("method", "")
        params = note.get("params", {}) or {}
        out: list[dict[str, Any]] = []
        sid = params.get("threadId") or ""
        try:
            if method == "turn/plan/updated":
                steps = params.get("plan", []) or []
                items = [{"step": str(s.get("step", "")), "status": s.get("status", "pending")}
                         for s in steps if isinstance(s, dict)]
                ctx.registry.set_plan(sid, items)
                out.append(self.session_projection(sid, "plan", {"items": items}))
            elif method == "thread/goal/updated":
                goal = params.get("goal", {}) or {}
                view = self._goal_view(sid, goal)
                ctx.registry.set_goal(sid, view)
                out.append(self.session_projection(sid, "goal", view))
            elif method == "thread/goal/cleared":
                ctx.registry.set_goal(sid, None)
                out.append(self.session_projection(sid, "goal", {"objective": None}))
            elif method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                if delta:
                    call = params.get("callId") or params.get("itemd") or ""
                    item_id = params.get("itemId", call)
                    out.append(self._assistant_chunk(sid, item_id, "text-delta", {"text": delta}))
            elif method == "item/reasoning/textDelta" or method == "item/reasoning/summaryTextDelta":
                delta = params.get("delta", "")
                if delta:
                    out.append(self._assistant_chunk(sid, params.get("itemId", ""),
                                                     "reasoning-delta", {"text": delta}))
            elif method == "thread/started":
                t = params.get("thread", {}) or {}
                out.append(self.session_added(t.get("id", sid)))
            elif method == "thread/archived" or method == "thread/deleted" or method == "thread/closed":
                out.append(self.session_removed(sid or params.get("threadId", "")))
            elif method == "thread/status/changed":
                out.extend(self._thread_status(sid, params, ctx))
            elif method == "turn/started":
                turn = params.get("turn", {}) or {}
                turn_id = turn.get("id", "")
                ctx.registry.set_running(sid, True)
                ctx.registry.set_turn(sid, turn_id)
                self._current_turn[sid] = turn_id
                # No turn id in the notice payload: the UI prints it verbatim
                # ("回合 <id> 开始"), and Codex UUIDs are pure noise there.
                out.append(self.session_event(sid, "turn/start", {}))
                out.append(self.host_status(sid, True))
            elif method == "turn/completed":
                turn = params.get("turn", {}) or {}
                ctx.registry.set_running(sid, False)
                # Fold final items from the completed turn: this guarantees the
                # assistant/message finalization (and tool results) even when
                # item/completed notifications were missed mid-stream.
                for entry in turn.get("items", []) or []:
                    item = entry.get("item", entry) if isinstance(entry, dict) else {}
                    if isinstance(item, dict) and item.get("type") in ("agentMessage", "commandExecution", "fileChange"):
                        for frame in self._item_completed(sid, {"item": item}, ctx):
                            out.append(frame)
                out.append(self._turn_completed(sid, turn))
                out.append(self.host_status(sid, False))
                ctx._maybe_apply_pending_restart()
            elif method == "item/started":
                out.extend(self._item_started(sid, params, ctx))
            elif method == "item/completed":
                out.extend(self._item_completed(sid, params, ctx))
            elif method == "thread/tokenUsage/updated":
                out.extend(self._token_usage(sid, params, ctx))
            elif method == "error":
                out.append(self.agent_error(str(params.get("message", "Codex runtime error"))))
            elif method == "warning":
                # Codex warnings (skill budget notes, deprecations) would render
                # as retry notices in the UI; log and drop instead.
                ctx._debug_log(f"warning: {str(params.get('message', ''))[:120]}")
            elif method == "item/commandExecution/outputDelta":
                # live terminal output: append to open command stream (best effort)
                out.append(self._tool_call_update(sid, params.get("itemId", ""),
                                                  params.get("delta", ""), "terminal"))
            else:
                ctx._debug_log(f"ignored notification: {method}")
        except Exception as exc:  # translator must never crash the reader
            ctx._debug_log(f"translate error on {method}: {exc}")
        return out

    # -- translators for composite notifications ------------------------------
    def _assistant_chunk(self, sid: str, item_id: str, chunk_type: str, data: dict[str, Any]) -> dict[str, Any]:
        st = self._stream(sid, item_id or "anon")
        if chunk_type == "text-delta":
            st["chunks"] += 1
        data = dict(data)
        data["type"] = chunk_type
        data["index"] = 0
        data.setdefault("callId", item_id)
        # Stream identity ({turn}:{step}) lets the frontend correlate chunks
        # with the finalizing assistant/message and swap the plain-text
        # streaming bubble for the markdown-rendered final one.
        data.setdefault("turn", self._current_turn.get(sid, ""))
        data.setdefault("step", item_id or "0")
        return self.session_event(sid, "assistant/chunk", data)

    def _thread_status(self, sid: str, params: dict[str, Any], ctx: "CodexRuntimeAdapter") -> list[dict[str, Any]]:
        status = params.get("status", {})
        if isinstance(status, dict):
            thread_status = status.get("type")
            running = thread_status == "active"
            ctx.registry.set_running(sid, running)
            return [self.host_status(sid, running)]
        return []

    def _turn_completed(self, sid: str, turn: dict[str, Any]) -> dict[str, Any]:
        reason = {"kind": "stop"}
        err = turn.get("error")
        if err:
            reason = {"kind": "error", "error": {"message": str(err.get("message", "turn failed"))}}
        turn_status = turn.get("status")
        if turn_status == "interrupted":
            reason = {"kind": "stop", "interrupted": True}
        return self.session_event(sid, "turn/end", {"reason": reason})

    def _item_started(self, sid: str, params: dict[str, Any], ctx: "CodexRuntimeAdapter") -> list[dict[str, Any]]:
        item = params.get("item", {}) or {}
        itype = item.get("type", "")
        item_id = item.get("id", "")
        st = self._stream(sid, item_id)
        frames: list[dict[str, Any]] = []
        if itype == "commandExecution":
            st["open"].add("cmd")
            frames.append(self.session_event(sid, "tool/call", {
                "callId": item_id, "name": "shell",
                "input": {"command": item.get("command", "")},
                "view": {"title": item.get("command", "shell"), "card": "terminal", "output": ""},
            }))
        elif itype in ("fileChange",):
            st["open"].add("file")
            frames.append(self.session_event(sid, "tool/call", {
                "callId": item_id, "name": "apply_patch",
                "input": {},
                "view": {"title": "file change", "card": "diff", "diffs": []},
            }))
        elif itype in ("mcpToolCall", "dynamicToolCall", "collabAgentToolCall", "webSearch"):
            tool = item.get("tool") or item.get("action") or itype
            st["open"].add("tool")
            frames.append(self.session_event(sid, "tool/call", {
                "callId": item_id, "name": str(tool),
                "input": {"arguments": item.get("arguments", {})},
                "view": {"title": str(tool)},
            }))
        return frames

    def _item_completed(self, sid: str, params: dict[str, Any], ctx: "CodexRuntimeAdapter") -> list[dict[str, Any]]:
        item = params.get("item", {}) or {}
        itype = item.get("type", "")
        item_id = item.get("id", "")
        frames: list[dict[str, Any]] = []
        if itype == "agentMessage":
            # item/completed and the turn/completed replay both carry the same
            # item; finalize it exactly once per session.
            done = self._finalized.setdefault(sid, set())
            if item_id and item_id in done:
                return frames
            if item_id:
                done.add(item_id)
            text = item.get("text", "") or ""
            frames.append(self._assistant_message(sid, item_id, text))
        elif itype == "userMessage":
            # Live echo of the user's own message (pending-bubble confirm).
            done = self._finalized.setdefault(sid, set())
            if item_id and item_id in done:
                return frames
            if item_id:
                done.add(item_id)
            frames.append(self.session_event(sid, "user/message", {
                "content": HistoryFolder._user_content(item.get("content", "")),
                "source": {"kind": "user", "rpcId": item.get("clientId") or item_id},
                "deliveryMode": "queue",
            }))
        elif itype == "commandExecution":
            frames.append(self.session_event(sid, "tool/result", {
                "callId": item_id, "message": "",
                "view": {"title": item.get("command", "shell"), "card": "terminal",
                         "output": item.get("aggregatedOutput", "") or "",
                         "exitCode": item.get("exitCode")},
            }))
        elif itype == "fileChange":
            frames.append(self.session_event(sid, "tool/result", {
                "callId": item_id, "message": "",
                "view": {"title": "file change", "card": "diff",
                         "diffs": self._file_change_diffs(item)},
            }))
        elif itype in ("mcpToolCall", "dynamicToolCall", "collabAgentToolCall"):
            result = item.get("result") or item.get("contentItems") or ""
            err = item.get("error")
            frames.append(self.session_event(sid, "tool/result", {
                "callId": item_id,
                "message": str(err) if err else "",
                "view": {"title": str(item.get("tool", itype)),
                         "output": self._stringify(result)},
            }))
        return frames

    def _tool_call_update(self, sid: str, item_id: str, delta: str, _kind: str) -> dict[str, Any]:
        return self.session_event(sid, "assistant/chunk", {
            "type": "tool-call-delta", "text": delta, "index": 0, "callId": item_id,
        })

    def _assistant_message(self, sid: str, item_id: str, text: str) -> dict[str, Any]:
        st = self._stream(sid, item_id)
        st["open"].add("final")
        content = [{"type": "text", "text": text}] if text else []
        data = {
            "message": {"content": content, "usage": {}, "timing": {}},
            "turn": self._current_turn.get(sid, ""),
            "step": item_id or "0",
        }
        return self.session_event(sid, "assistant/message", data)

    @staticmethod
    def _goal_view(sid: str, goal: dict[str, Any]) -> dict[str, Any]:
        # DSH-shaped goal projection; ref enables the pause/complete/clear
        # action buttons in the signal card.
        return {"objective": goal.get("objective", ""),
                "phase": goal.get("status", "active"),
                "ref": {"id": sid, "revision": int(goal.get("updatedAt") or 1)}}

    def _token_usage(self, sid: str, params: dict[str, Any], ctx: "CodexRuntimeAdapter") -> list[dict[str, Any]]:
        # Schema: {tokenUsage: {last: breakdown, total: breakdown, modelContextWindow}}
        info = params.get("tokenUsage", {}) or {}
        total = info.get("total") or info.get("last") or info

        def num(key: str) -> int:
            try:
                return int(total.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        cached = num("cachedInputTokens")
        usage = {
            "uncachedInputTokens": max(0, num("inputTokens") - cached),
            "outputTokens": num("outputTokens"),
            "cacheReadTokens": cached,
            "cacheWriteTokens": num("cacheWriteInputTokens"),
        }
        pressure = {"projectedTokens": num("totalTokens"),
                    "contextWindow": info.get("modelContextWindow") or 0}
        ctx.registry.set_usage(sid, usage, pressure)
        return [
            self.session_event(sid, "assistant/chunk", {"type": "usage", "usage": usage, "index": 0}),
            self.session_projection(sid, "tokenUsage", usage),
            self.session_projection(sid, "contextPressure", pressure),
        ]

    def _file_change_diffs(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        diffs = []
        for ch in item.get("changes", []) or []:
            diffs.append({"path": ch.get("path", ""), "text": ch.get("diff") or ch.get("change", "")})
        return diffs

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)


# --- HistoryFolder -----------------------------------------------------------


class HistoryFolder:
    """Fold thread/read turns+items into DSH history events deterministically.

    Pure function of the input: same thread -> same events, same order, same
    seq (1..n). No persistence. Live events continue after seq floor.
    """

    @staticmethod
    def fold(thread: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        seq = 0

        def ev(event_type: str, data: dict[str, Any], t: int) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            return {"event": {"type": event_type, "data": data, "seq": seq, "time": t}, "view": None}

        turns = thread.get("turns", []) or []
        for turn in turns:
            turn_id = turn.get("id", "")
            started = turn.get("startedAt") or 0
            events.append(ev("turn/start", {}, started))
            # The user's message leads every turn; rollout order is normally
            # user-first already, but keep it stable defensively.
            entries = turn.get("items", []) or []
            entries = sorted(entries, key=lambda e: 0 if (e.get("item", e) or {}).get("type") == "userMessage" else 1)
            for entry in entries:
                item = entry.get("item", entry if "type" in entry else {})
                if not isinstance(item, dict) or not item:
                    continue
                events.extend(HistoryFolder._fold_item(item, ev, started, turn))
            error = turn.get("error")
            reason = {"kind": "error", "error": {"message": str(error.get("message", ""))}} if error else {"kind": "stop"}
            events.append(ev("turn/end", {"reason": reason}, turn.get("completedAt") or started))
        return events

    @staticmethod
    def _fold_item(item: dict[str, Any], ev: Callable, t: int, turn: dict[str, Any]) -> list[dict[str, Any]]:
        itype = item.get("type", "")
        out: list[dict[str, Any]] = []
        if itype == "userMessage":
            out.append(ev("user/message", {
                "content": HistoryFolder._user_content(item.get("content", "")),
                "source": {"kind": "user", "rpcId": item.get("clientId") or item.get("id", "")},
                "deliveryMode": "queue",
            }, t))
        elif itype == "agentMessage":
            text = item.get("text", "") or ""
            out.append(ev("assistant/message", {"message": {
                "content": [{"type": "text", "text": text}] if text else [], "usage": {}, "timing": {},
            }}, t))
        elif itype == "reasoning":
            summary = item.get("summary") or item.get("content") or ""
            text = HistoryFolder._reasoning_text(summary)
            if text:
                out.append(ev("assistant/message", {"message": {
                    "content": [{"type": "reasoning", "text": text}], "usage": {}, "timing": {},
                }}, t))
        elif itype == "commandExecution":
            out.append(ev("tool/call", {
                "callId": item.get("id", ""), "name": "shell",
                "input": {"command": item.get("command", "")},
                "view": {"title": item.get("command", "shell"), "card": "terminal", "output": ""},
            }, t))
            out.append(ev("tool/result", {
                "callId": item.get("id", ""), "message": "",
                "view": {"title": item.get("command", "shell"), "card": "terminal",
                         "output": item.get("aggregatedOutput", "") or "",
                         "exitCode": item.get("exitCode")},
            }, t))
        elif itype == "fileChange":
            out.append(ev("tool/call", {
                "callId": item.get("id", ""), "name": "apply_patch", "input": {},
                "view": {"title": "file change", "card": "diff", "diffs": []},
            }, t))
            out.append(ev("tool/result", {
                "callId": item.get("id", ""), "message": "",
                "view": {"title": "file change", "card": "diff",
                         "diffs": [{"path": ch.get("path", ""), "text": ch.get("diff") or ch.get("change", "")}
                                   for ch in item.get("changes", []) or []]},
            }, t))
        elif itype in ("mcpToolCall", "dynamicToolCall", "webSearch"):
            name = str(item.get("tool") or item.get("query") or itype)
            out.append(ev("tool/call", {
                "callId": item.get("id", ""), "name": name,
                "input": {"arguments": item.get("arguments", {})},
                "view": {"title": name},
            }, t))
            result = item.get("result") or item.get("results") or item.get("error") or ""
            out.append(ev("tool/result", {
                "callId": item.get("id", ""), "message": str(item.get("error") or ""),
                "view": {"title": name, "output": EventTranslator._stringify(result)},
            }, t))
        return out

    @staticmethod
    def _user_content(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, list):
            return content
        return [{"type": "text", "text": str(content or "")}]

    @staticmethod
    def _reasoning_text(summary: Any) -> str:
        if isinstance(summary, str):
            return summary
        if isinstance(summary, list):
            return "\n".join(str(s.get("text", "") if isinstance(s, dict) else s) for s in summary)
        return str(summary or "")


# --- Workbench host state (multi-project workspaces / settings / presets) ----

def _host_state_root() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return os.path.join(base, "Boujoy", "BoujoyHarness")
    return os.path.expanduser("~/Library/Application Support/Boujoy/BoujoyHarness")


class HostState:
    """DSH-host surfaces the codex runtime must carry itself: a multi-project
    workspace registry (rename/reorder/delete), writable settings namespaces
    (busyEnter etc.), and user-defined agent presets. One JSON file under the
    product state dir; the gateway is a single process, so an in-process lock
    plus atomic replace is enough."""

    def __init__(self, root: str | None = None) -> None:
        self.root = root or _host_state_root()
        self.path = os.path.join(self.root, "host-state.json")
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"workspaces": [], "settings": {}, "presets": {}}
        try:
            os.makedirs(self.root, exist_ok=True)
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, fallback in self._data.items():
                    value = loaded.get(key)
                    if isinstance(value, type(fallback)):
                        self._data[key] = value
        except (OSError, ValueError):
            pass

    def _save_locked(self) -> None:
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)

    # -- workspaces --
    def workspaces(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(ws) for ws in self._data["workspaces"]]

    def workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            for ws in self._data["workspaces"]:
                if ws.get("id") == workspace_id:
                    return dict(ws)
        return None

    def add_workspace(self, path: str, workspace_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            for ws in self._data["workspaces"]:
                if ws.get("path") == path:
                    return dict(ws)
            ws = {"id": workspace_id or f"ws-{uuid.uuid4().hex[:8]}",
                  "title": os.path.basename(path.rstrip("/")) or path,
                  "path": path, "order": len(self._data["workspaces"])}
            self._data["workspaces"].append(ws)
            self._save_locked()
            return dict(ws)

    def mutate_workspace(self, workspace_id: str, title: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            for ws in self._data["workspaces"]:
                if ws.get("id") == workspace_id:
                    if title is not None:
                        ws["title"] = title
                    self._save_locked()
                    return dict(ws)
        return None

    def delete_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._lock:
            remaining = [ws for ws in self._data["workspaces"] if ws.get("id") != workspace_id]
            if len(remaining) == len(self._data["workspaces"]):
                return None
            removed = next(ws for ws in self._data["workspaces"] if ws.get("id") == workspace_id)
            self._data["workspaces"] = remaining
            for index, ws in enumerate(remaining):
                ws["order"] = index
            self._save_locked()
            return dict(removed)

    def reorder_workspace(self, workspace_id: str, before_workspace_id: str | None) -> bool:
        with self._lock:
            items = self._data["workspaces"]
            subject = next((ws for ws in items if ws.get("id") == workspace_id), None)
            if subject is None:
                return False
            items.remove(subject)
            index = len(items)
            if before_workspace_id:
                for position, ws in enumerate(items):
                    if ws.get("id") == before_workspace_id:
                        index = position
                        break
            items.insert(index, subject)
            for position, ws in enumerate(items):
                ws["order"] = position
            self._save_locked()
            return True

    # -- settings --
    def settings_namespaces(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"ns": ns, "revision": value.get("revision", 0),
                     "data": dict(value.get("data", {}))}
                    for ns, value in self._data["settings"].items()]

    def settings_update(self, ns: str, patch: dict[str, Any],
                        expected_revision: int | None) -> dict[str, Any] | None:
        with self._lock:
            value = self._data["settings"].setdefault(ns, {"revision": 0, "data": {}})
            if expected_revision is not None and int(expected_revision) != int(value.get("revision", 0)):
                return None
            value["data"].update(patch or {})
            value["revision"] = int(value.get("revision", 0)) + 1
            self._save_locked()
            return {"ns": ns, "revision": value["revision"], "data": dict(value["data"])}

    # -- presets --
    def custom_presets(self) -> dict[str, Any]:
        with self._lock:
            return {pid: dict(doc) for pid, doc in self._data["presets"].items()}

    def put_preset(self, preset_id: str, doc: dict[str, Any]) -> None:
        with self._lock:
            self._data["presets"][preset_id] = doc
            self._save_locked()

    def delete_preset(self, preset_id: str) -> bool:
        with self._lock:
            if preset_id not in self._data["presets"]:
                return False
            del self._data["presets"][preset_id]
            self._save_locked()
            return True


# --- SessionRegistry ---------------------------------------------------------


class SessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def ensure(self, thread_id: str, cwd: str | None = None, title: str | None = None) -> dict[str, Any]:
        with self._lock:
            s = self._sessions.get(thread_id)
            if s is None:
                s = {"running": False, "turnId": "", "cwd": cwd, "title": title, "model": None, "effort": None,
                     "permission": None, "providerId": None, "agentPreset": "standard",
                     "updated": int(time.time() * 1000)}
                self._sessions[thread_id] = s
            else:
                if cwd:
                    s["cwd"] = cwd
                if title:
                    s["title"] = title
                s["updated"] = int(time.time() * 1000)
            return s

    def get(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._sessions.get(thread_id)

    def set_running(self, thread_id: str, running: bool) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["running"] = running
            s["updated"] = int(time.time() * 1000)

    def set_agent_preset(self, thread_id: str, preset_id: str) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["agentPreset"] = preset_id
            s["updated"] = int(time.time() * 1000)

    def set_usage(self, thread_id: str, usage: dict[str, Any], pressure: dict[str, Any]) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["usage"] = usage
            s["pressure"] = pressure

    def set_plan(self, thread_id: str, items: list[dict[str, Any]]) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["plan"] = {"items": items}

    def set_goal(self, thread_id: str, view: dict[str, Any] | None) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["goal"] = view

    def set_provider(self, thread_id: str, provider_id: str) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["providerId"] = provider_id

    def set_permission(self, thread_id: str, permission: str) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["permission"] = permission
            s["updated"] = int(time.time() * 1000)

    def set_loaded(self, thread_id: str, loaded: bool) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["loaded"] = loaded

    def set_turn(self, thread_id: str, turn_id: str) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            s["turnId"] = turn_id

    def set_model(self, thread_id: str, model: str | None, effort: str | None = None) -> None:
        s = self.ensure(thread_id)
        with self._lock:
            if model is not None:
                s["model"] = model
            if effort is not None:
                s["effort"] = effort

    def set_title(self, thread_id: str, title: str | None) -> None:
        if not title:
            return
        s = self.ensure(thread_id)
        with self._lock:
            s["title"] = title

    def running(self, thread_id: str) -> bool:
        s = self.get(thread_id)
        return bool(s and s.get("running"))

    def remove(self, thread_id: str) -> None:
        with self._lock:
            self._sessions.pop(thread_id, None)


# --- RpcClient ---------------------------------------------------------------


class RpcClient:
    """JSON-RPC request/response correlation + server-request (approval) registry."""

    def __init__(self, send: Callable[[str], None], debug: Callable[[str], None]) -> None:
        self._send = send
        self._debug = debug
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}  # id -> {"done","value","error"}
        self._server_requests: dict[str, dict[str, Any]] = {}  # codex request id -> descriptor

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            slot: dict[str, Any] = {"done": False}
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(json.dumps(msg, ensure_ascii=False))
        deadline = time.time() + timeout
        while not slot.get("done"):
            if time.time() > deadline:
                with self._lock:
                    self._pending.pop(rid, None)
                raise TimeoutError(f"codex rpc timeout: {method}")
            time.sleep(0.02)
        if slot.get("error") is not None:
            raise RuntimeError(f"codex rpc error: {method}: {slot['error']}")
        return slot.get("value")

    def resolve(self, rid: int, value: Any = None, error: Any = None) -> bool:
        with self._lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return False
        slot["value"] = value
        slot["error"] = error
        slot["done"] = True
        return True

    # -- server requests (approvals) --
    def register_server_request(self, codex_id: Any, descriptor: dict[str, Any]) -> str:
        boujoy_rpc_id = str(uuid.uuid4())
        with self._lock:
            self._server_requests[str(codex_id)] = {**descriptor, "boujoyRpcId": boujoy_rpc_id}
        return boujoy_rpc_id

    def lookup_server_request(self, boujoy_rpc_id: str) -> dict[str, Any] | None:
        with self._lock:
            for d in self._server_requests.values():
                if d.get("boujoyRpcId") == boujoy_rpc_id:
                    return d
            return None

    def pop_server_request(self, boujoy_rpc_id: str) -> dict[str, Any] | None:
        with self._lock:
            for k, d in list(self._server_requests.items()):
                if d.get("boujoyRpcId") == boujoy_rpc_id:
                    self._server_requests.pop(k)
                    return d
            return None

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._server_requests.values())

    def respond(self, codex_id: str, result: Any) -> None:
        msg = {"jsonrpc": "2.0", "id": codex_id, "result": result}
        self._send(json.dumps(msg, ensure_ascii=False))

    def fail(self, rid: int, code: int, message: str) -> None:
        self.resolve(rid, error={"code": code, "message": message})


# --- CodexProcess ------------------------------------------------------------


class CodexProcess:
    """Spawn `codex app-server --stdio`, handshake, read loop, restart."""

    def __init__(self, bin_path: str, debug: Callable[[str], None], on_notification: Callable[[dict[str, Any]], None],
                 on_server_request: Callable[[dict[str, Any]], None], cwd: str | None = None,
                 extra_env: dict[str, str] | None = None) -> None:
        self.bin_path = bin_path
        self.debug = debug
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.proc: subprocess.Popen | None = None
        self.rpc: RpcClient | None = None
        self.status = "stopped"  # stopped|starting|ready|degraded
        self._write_lock = threading.Lock()
        self._restarts = 0
        self._last_restart = 0.0
        self._dead = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle --
    def start(self) -> None:
        with self._lock:
            if self.status in ("starting", "ready"):
                return
            self.status = "starting"
        try:
            self.proc = subprocess.Popen(
                [self.bin_path, "app-server", "--stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd or None,
                env={**os.environ, **self.extra_env},
            )
        except OSError as exc:
            with self._lock:
                self.status = "degraded"
            raise AdapterUnavailable(f"spawn codex failed: {exc}")
        self.rpc = RpcClient(self._write_line, self.debug)
        threading.Thread(target=self._read_stdout, name="codex-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="codex-stderr", daemon=True).start()
        try:
            self.rpc.request("initialize", {
                "clientInfo": {"name": "laomo-workbench", "title": "LaoMo Workbench", "version": "0.1.0"},
            }, timeout=30)
            # initialized is a notification (no id) per the app-server protocol
            self._send_raw({"jsonrpc": "2.0", "method": "initialized"})
        except (TimeoutError, RuntimeError) as exc:
            self._mark_degraded(f"initialize failed: {exc}")
            raise AdapterUnavailable(f"codex initialize failed: {exc}")
        with self._lock:
            self.status = "ready"
            self._restarts = 0

    def stop(self) -> None:
        self._dead.set()
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        with self._lock:
            self.status = "stopped"

    def _mark_degraded(self, why: str) -> None:
        with self._lock:
            self.status = "degraded"
        self.debug(f"degraded: {why}")

    MAX_RESTARTS = 3

    def maybe_restart(self) -> bool:
        with self._lock:
            if self._restarts >= self.MAX_RESTARTS:
                return False
            if time.time() - self._last_restart < 2 * (self._restarts + 1):
                return False  # exponential backoff window
            self._restarts += 1
            self._last_restart = time.time()
        self.stop()
        self._dead.clear()
        try:
            self.start()
            return True
        except AdapterUnavailable:
            return False

    # -- io --
    def _write_line(self, line: str) -> None:
        proc = self.proc
        if not proc or not proc.stdin:
            raise AdapterUnavailable("codex process not running")
        data = (line + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AdapterUnavailable(f"codex stdin broken: {exc}")

    def _send_raw(self, obj: dict[str, Any]) -> None:
        proc = self.proc
        if not proc or not proc.stdin:
            raise AdapterUnavailable("codex process not running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AdapterUnavailable(f"codex stdin broken: {exc}")

    def send_notification(self, obj: dict[str, Any]) -> None:
        self._send_raw(obj)

    # -- readers --
    def _read_stdout(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for raw in iter(proc.stdout.readline, b""):
            if self._dead.is_set():
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.debug(f"non-json stdout: {line[:120]}")
                continue
            self._dispatch(msg)
        self._on_exit()

    def _read_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for raw in iter(proc.stderr.readline, b""):
            if self._dead.is_set():
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self.debug(f"stderr: {line[:200]}")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        rpc = self.rpc
        if rpc is None:
            return
        if "id" in msg and ("method" in msg):
            # server request (approval / user input) -> needs response later
            self.on_server_request(msg)
        elif "id" in msg and ("result" in msg or "error" in msg):
            if "result" in msg:
                rpc.resolve(int(msg["id"]), value=msg["result"])
            else:
                rpc.resolve(int(msg["id"]), error=msg["error"])
        elif "method" in msg:
            self.on_notification(msg)
        else:
            self.debug(f"unroutable message: {str(msg)[:160]}")

    def _on_exit(self) -> None:
        with self._lock:
            if self.status != "stopped":
                self.status = "degraded"
        self.debug("codex process exited")


# --- CodexRuntimeAdapter ------------------------------------------------------


class CodexRuntimeAdapter:
    """Adapter surface used by boujoy_server: rpc(mode, endpoint, body),
    subscribe(), health(), shutdown()."""

    NAME = "codex"
    CAPABILITIES = {"modelSelection": True, "reasoningEffort": True, "steer": True, "interrupt": True,
                    "fork": False, "queue": False}

    DEFAULT_EFFORTS = ["low", "medium", "high"]

    def __init__(self, bin_path: str | None = None, default_cwd: str | None = None,
                 debug_log: Callable[[str], None] | None = None,
                 providers: Any = None,
                 state_root: str | None = None) -> None:
        self.bin_path = bin_path or shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
        self.default_cwd = default_cwd or os.getcwd()
        self._debug_sink = debug_log or (lambda m: None)
        self.providers = providers  # ProviderProfileManager | None
        self.translator = EventTranslator()
        self.registry = SessionRegistry()
        self.folder = HistoryFolder()
        # DSH-host parity surfaces (multi-project registry, settings, presets).
        # Tests inject state_root; production persists under the product dir.
        self._state = HostState(state_root)
        if not self._state.workspaces():
            self._state.add_workspace(self.default_cwd, workspace_id="laomo-clean")
        self.process: CodexProcess | None = None
        self._proc_lock = threading.Lock()
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._sub_lock = threading.Lock()
        self._last_model_list: list[dict[str, Any]] | None = None
        self._workspace_cwd: str = self.default_cwd
        self._workspace_lock = threading.Lock()
        self._ws_counter = 0
        self._pending_restart = False  # provider env changed; restart when idle
        # active mission turns (run_turn registrations): the Control Plane
        # can direct a real `turn/interrupt(threadId, turnId)` at them on
        # cancel — Gate F proved that without this, a cancelled mission's
        # codex turns keep working to natural completion ("zombie writes").
        self._turns_lock = threading.Lock()
        self._active_turns: dict[str, dict] = {}  # threadId -> registration

    # -- infra --
    def _debug_log(self, msg: str) -> None:
        self._debug_sink(f"[codex] {msg}")

    def _ensure_process(self) -> CodexProcess:
        with self._proc_lock:
            proc = self.process
            if proc is not None and proc.status == "ready":
                return proc
            if proc is None:
                proc = CodexProcess(self.bin_path, self._debug_log,
                                    on_notification=self._on_notification,
                                    on_server_request=self._on_server_request,
                                    cwd=self.default_cwd,
                                    extra_env=(self.providers.env_for_process() if self.providers else None))
                self.process = proc
            if proc.status == "degraded" and not proc.maybe_restart():
                raise AdapterUnavailable("codex runtime degraded (restart budget exhausted)")
            if proc.status == "stopped":
                proc.start()
            if proc.status != "ready":
                raise AdapterUnavailable(f"codex runtime not ready: {proc.status}")
            return proc

    def note_provider_change(self) -> None:
        """Provider secret/config changed: restart the codex subprocess when
        it is safe (no active turns). Running work is never killed silently."""
        with self._proc_lock:
            proc = self.process
            if proc is None:
                return
            if any_running := any(s.get("running") for s in self.registry._sessions.values()):
                self._pending_restart = True
                self._debug_log("provider change deferred: active turns running")
                return
            proc.stop()
            self.process = None
            self._debug_log("provider changed: codex runtime stopped, will lazily restart with new env")

    def _maybe_apply_pending_restart(self) -> None:
        if not self._pending_restart:
            return
        if any(s.get("running") for s in self.registry._sessions.values()):
            return
        self._pending_restart = False
        with self._proc_lock:
            if self.process:
                self.process.stop()
                self.process = None
        self._debug_log("pending provider restart applied")

    def health(self) -> dict[str, Any]:
        proc = self.process
        status = proc.status if proc else "stopped"
        return {"runtime": self.NAME, "status": status, "capabilities": self.CAPABILITIES}

    def shutdown(self) -> None:
        with self._proc_lock:
            if self.process:
                self.process.stop()
                self.process = None

    # -- event bus --
    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._sub_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._sub_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, frame: dict[str, Any]) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(frame)
            except Exception as exc:
                self._debug_log(f"subscriber error: {exc}")

    def _on_notification(self, note: dict[str, Any]) -> None:
        for frame in self.translator.translate(note, self):
            self._emit(frame)

    def _on_server_request(self, msg: dict[str, Any]) -> None:
        """Codex asks for approval/user-input -> project as mux server-request."""
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}
        descriptor = {"method": method, "params": params, "codexId": msg.get("id")}
        rpc_id = self.process.rpc.register_server_request(msg.get("id"), descriptor) if self.process and self.process.rpc else str(uuid.uuid4())
        sid = params.get("threadId", "")
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                      "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval"):
            payload = self._approval_payload(method, params, sid)
            self._emit(self.translator.approval_requested(rpc_id, payload))
        elif method == "item/tool/requestUserInput" or method == "mcpServer/elicitation/request":
            payload = self._question_payload(method, params, sid)
            self._emit(self.translator.approval_requested(rpc_id, payload))
        else:
            self._debug_log(f"unhandled server request: {method}")

    def _approval_payload(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        # Command approvals carry `command` at the top level (schema:
        # CommandExecutionRequestApprovalParams); legacy apply/exec variants
        # nest differently, hence the fallbacks.
        if "commandExecution" in method or method == "execCommandApproval":
            command = params.get("command") or (params.get("item", {}) or {}).get("command", "")
            return {"type": "approval/requested", "sessionId": sid,
                    "approvalId": str(params.get("itemId") or params.get("callId") or params.get("approvalId") or uuid.uuid4()),
                    "toolName": "shell", "reason": command or "execute command"}
        if "fileChange" in method or method == "applyPatchApproval":
            changes = params.get("changes") or params.get("item", {}).get("changes", [])
            paths = ", ".join(ch.get("path", "") for ch in changes if isinstance(ch, dict))[:300]
            return {"type": "approval/requested", "sessionId": sid,
                    "approvalId": str(params.get("itemId") or params.get("callId") or uuid.uuid4()),
                    "toolName": "apply_patch", "reason": paths or "file change"}
        return {"type": "approval/requested", "sessionId": sid,
                "approvalId": str(params.get("itemId") or params.get("callId") or uuid.uuid4()),
                "toolName": "codex", "reason": method}

    def _question_payload(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        qs = params.get("questions") or params.get("request", {}).get("questions", []) or []
        questions = [{"id": str(q.get("id", i)), "question": str(q.get("question", "")),
                      "header": str(q.get("header", "")), "detail": str(q.get("detail", "")),
                      "options": [{"label": str(o.get("label", "")), "description": str(o.get("description", ""))}
                                  for o in q.get("options", [])],
                      "multiSelect": bool(q.get("multiSelect", False))}
                     for i, q in enumerate(qs) if isinstance(q, dict)]
        return {"type": "question/requested", "sessionId": sid,
                "questionRpcId": str(uuid.uuid4()), "questions": questions or
                [{"id": "q0", "question": str(params.get("prompt", method)), "header": "Codex",
                  "detail": "", "options": [], "multiSelect": False}]}

    # -- workspace --
    def workspace_cwd(self) -> str:
        with self._workspace_lock:
            return self._workspace_cwd

    def set_workspace_cwd(self, path: str) -> None:
        with self._workspace_lock:
            self._workspace_cwd = path

    # -- rpc surface ----------------------------------------------------------
    def rpc(self, mode: str, endpoint: str, body: dict[str, Any] | None, rpc_id: str = "") -> dict[str, Any]:
        """Translate a DSH-shaped HTTP RPC. rpc_id carries the frontend
        envelope's rpcId (the submission id) when present."""
        try:
            handler = getattr(self, f"_rpc_{endpoint.replace('/', '_').replace('.', '_')}", None)
            if handler:
                return handler(body or {}, rpc_id)
            return self._rpc_generic(endpoint, body or {})
        except AdapterUnavailable as exc:
            return err_value(str(exc), "runtime-unavailable")
        except TimeoutError as exc:
            return err_value(str(exc), "runtime-timeout")
        except RuntimeError as exc:
            return err_value(str(exc), "runtime-error")
        except Exception as exc:  # defensive: never 500 the UI
            self._debug_log(f"rpc {endpoint} failed: {exc!r}")
            return err_value(f"codex adapter error: {exc}", "adapter-error")

    # host.pickDirectory -> native macOS folder picker (choose folder).
    # The DSH native host had this capability; the codex runtime lost it and
    # the 选择新项目 button went dead. The gateway runs on the same machine
    # in the user's GUI session, so the real system dialog is available.
    # User cancel is the normal "closed the picker" path — the frontend
    # treats a cancelled error as silent.
    def _rpc_host_pickDirectory(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        if sys.platform != "darwin":
            return err_value("host.pickDirectory 仅支持 macOS")
        try:
            proc = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "选择新项目文件夹")'],
                capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            return err_value("osascript 不可用")
        except subprocess.TimeoutExpired:
            return err_value("选择超时，请重试")
        if proc.returncode != 0:
            return err_value("cancelled")
        path = proc.stdout.strip().rstrip("/")
        if not path.startswith("/"):
            return err_value("未选择有效目录")
        return ok_value({"path": path})

    # host.describe
    def _rpc_host_describe(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        cfg = {}
        try:
            cfg = proc.rpc.request("config/read", {}, timeout=15) or {}
        except (TimeoutError, RuntimeError) as exc:
            self._debug_log(f"config/read failed: {exc}")
        model = self._default_model(cfg)
        return ok_value({
            "provider": "codex", "model": model, "version": proc.bin_version if hasattr(proc, "bin_version") else "codex",
            "capabilities": {"runtime": self.NAME, **self.CAPABILITIES},
        })

    def _default_model(self, cfg: dict[str, Any]) -> str:
        if isinstance(cfg, dict):
            v = cfg.get("model") or cfg.get("value", {}).get("model")
            if isinstance(v, str) and v:
                return v
        return "gpt-5.6-luna"

    # session.list
    def _rpc_session_list(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        data = proc.rpc.request("thread/list", {"limit": 100}, timeout=30) or {}
        items = []
        for t in data.get("data", []) or []:
            tid = t.get("id", "")
            if not tid:
                continue
            reg = self.registry.ensure(tid, cwd=t.get("cwd"), title=t.get("name"))
            items.append({
                "sessionId": tid,
                "id": tid,
                "title": t.get("name") or t.get("preview", "")[:40] or "未命名会话",
                "running": bool(reg.get("running")),
                "blank": False,
                "workspaceId": self._workspace_id_for(reg.get("cwd") or t.get("cwd")),
                "updatedAt": t.get("updatedAt") or t.get("recencyAt") or 0,
                "agentPreset": reg.get("agentPreset") or "standard",
                "projection": {"permissions": self._permission_view(tid)},
            })
        return ok_value({"sessions": items, "items": items})

    # session.create -> thread/start
    def _rpc_session_create(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        provider_id = "chatgpt"
        if self.providers is not None:
            provider_id = self.providers.active_id()
            self._register_active_provider(provider_id)
        params: dict[str, Any] = {"cwd": self.workspace_cwd()}
        params.update(self._provider_thread_params(provider_id))
        model, effort = self._session_model_overrides(body.get("sessionId") or "")
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        res = proc.rpc.request("thread/start", params, timeout=30) or {}
        tid = res.get("threadId") or res.get("id") or (res.get("thread") or {}).get("id", "")
        if not tid:
            return err_value("thread/start returned no id")
        self.registry.ensure(tid, cwd=self.workspace_cwd())
        self.registry.set_loaded(tid, True)
        self.registry.set_provider(tid, provider_id)
        self._emit(self.translator.session_added(tid))
        return ok_value({"sessionId": tid, "id": tid, "providerId": provider_id})

    def run_turn(self, *, prompt: str, cwd: str | None = None, read_only: bool = False,
                 model: str | None = None, effort: str | None = None,
                 timeout: int = 900) -> dict[str, Any]:
        """One turn on a fresh ephemeral thread (mission planner/worker/
        evaluator). Completion arrives via the event bus; returns
        {ok, text, error, usage}."""
        proc = self._ensure_process()
        start: dict[str, Any] = {"cwd": cwd or self.workspace_cwd(), "ephemeral": True}
        if model:
            start["model"] = model
        if effort:
            start["effort"] = effort
        try:
            res = proc.rpc.request("thread/start", start, timeout=30) or {}
        except (TimeoutError, RuntimeError) as exc:
            return {"ok": False, "text": "", "error": f"thread/start 失败: {str(exc)[:200]}", "usage": {}}
        tid = res.get("threadId") or res.get("id") or (res.get("thread") or {}).get("id", "")
        if not tid:
            return {"ok": False, "text": "", "error": "thread/start 未返回 id", "usage": {}}
        params: dict[str, Any] = {"threadId": tid, "cwd": start["cwd"],
                                  "input": [{"type": "text", "text": prompt}]}
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if read_only:
            params["sandboxPolicy"] = {"type": "readOnly"}
            params["approvalPolicy"] = "never"
        else:
            # Mission turns are unattended: interactive approvals would hang
            # the turn until timeout. Autonomous workspace-write, never-ask.
            params["sandboxPolicy"] = {"type": "workspaceWrite"}
            params["approvalPolicy"] = "never"
        capture: dict[str, Any] = {"texts": [], "usage": {}, "error": None}
        done = threading.Event()

        def on_frame(frame: dict[str, Any]) -> None:
            payload = frame.get("payload", {})
            if payload.get("type") != "session/event" or payload.get("sessionId") != tid:
                return
            event = payload.get("event", {})
            etype = event.get("type")
            if etype == "assistant/message":
                content = ((event.get("data") or {}).get("message") or {}).get("content") or []
                capture["texts"] = [b.get("text", "") for b in content if isinstance(b, dict)]
            elif etype == "assistant/chunk" and (event.get("data") or {}).get("type") == "usage":
                capture["usage"] = (event.get("data") or {}).get("usage") or {}
            elif etype == "turn/end":
                reason = (event.get("data") or {}).get("reason") or {}
                if reason.get("kind") == "error":
                    capture["error"] = str((reason.get("error") or {}).get("message") or "turn failed")[:300]
                elif reason.get("interrupted"):
                    capture["error"] = "turn interrupted（控制平面取消）"
                done.set()

        unsubscribe = self.subscribe(on_frame)
        real_turn_id = ""
        try:
            res = proc.rpc.request("turn/start", params, timeout=30) or {}
            real_turn_id = str(res.get("turnId")
                               or (res.get("turn") or {}).get("id") or "")
            with self._turns_lock:
                self._active_turns[tid] = {"threadId": tid,
                                           "turnId": real_turn_id,
                                           "done": done,
                                           "startedAt": time.time()}
            if not done.wait(timeout):
                capture["error"] = f"turn 超时（{timeout}s）"
                try:
                    proc.rpc.request("turn/interrupt",
                                     {"threadId": tid, "turnId": real_turn_id},
                                     timeout=10)
                except (TimeoutError, RuntimeError):
                    pass
        except RuntimeError as exc:
            capture["error"] = str(exc)[:300]
        finally:
            with self._turns_lock:
                self._active_turns.pop(tid, None)
            unsubscribe()
            try:
                proc.rpc.request("thread/delete", {"threadId": tid}, timeout=10)
            except (TimeoutError, RuntimeError):
                pass
        return {"ok": capture["error"] is None,
                "text": "\n".join(t for t in capture["texts"] if t),
                "error": capture["error"], "usage": capture["usage"]}

    def interrupt_active_turns(self, max_wait: float = 10.0) -> list[dict]:
        """Directed cancellation for the Control Plane (mission cancel): send
        a REAL `turn/interrupt(threadId, turnId)` to every turn this adapter
        currently holds active, then bounded-wait for their completion.
        Gate 0 proved codex honors real-turnId interrupts and isolates them;
        Gate F proved that without this call a cancelled mission's turns
        keep running to natural completion and write files post-cancel.
        Returns per-turn outcomes ({"threadId", "turnId", "stopped"})."""
        outcomes: list[dict] = []
        try:
            proc = self._ensure_process()
        except AdapterUnavailable:
            return outcomes
        with self._turns_lock:
            active = list(self._active_turns.values())
        for rec in active:
            sent = False
            # the real turnId: the registration carries it when the
            # turn/start response did; otherwise the translator's registry
            # (fed by turn/started notifications) is the authoritative source
            turn_id = rec["turnId"]
            if not turn_id:
                reg = self.registry.get(rec["threadId"]) or {}
                turn_id = reg.get("turnId") or ""
            rec["turnId"] = turn_id
            try:
                proc.rpc.request("turn/interrupt",
                                 {"threadId": rec["threadId"],
                                  "turnId": turn_id}, timeout=10)
                sent = True
            except (TimeoutError, RuntimeError):
                pass
            self._debug_log(f"interrupt turn {rec['turnId'][:8]} "
                            f"(thread {rec['threadId'][:8]}): sent={sent}")
            outcomes.append({"threadId": rec["threadId"],
                             "turnId": rec["turnId"], "sent": sent})
        deadline = time.monotonic() + max_wait
        for rec in active:
            remaining = max(0.0, deadline - time.monotonic())
            rec["done"].wait(remaining)
        for out, rec in zip(outcomes, active):
            out["stopped"] = rec["done"].is_set()
        return outcomes

    def test_provider(self, managers: Any, provider_id: str) -> dict[str, Any]:
        """Real E2E provider validation: register, ephemeral thread, minimal
        turn, classify the outcome. Never returns secrets."""
        import tempfile
        profile = managers.get(provider_id)
        if not profile:
            return {"ok": False, "outcome": "runtime-error", "message": "Provider 不存在"}
        if profile.get("type") == "chatgpt":
            provider_id = "chatgpt"
        if profile.get("type") == "custom" and not managers.credentials.has(provider_id):
            return {"ok": False, "outcome": "auth-failed", "message": "尚未配置 API Key"}
        proc = self._ensure_process()
        if profile.get("type") == "custom":
            if not self._write_provider_config(profile):
                return {"ok": False, "outcome": "runtime-error",
                        "message": "Provider 注册失败（Codex 配置写入被拒，详见网关日志）"}
        with tempfile.TemporaryDirectory(prefix="laomo-provider-test-") as isolated:
            start: dict[str, Any] = {"cwd": isolated, "ephemeral": True}
            start.update(self._provider_thread_params(provider_id) or {})
            if provider_id != "chatgpt" and "model" not in start:
                start["model"] = profile.get("defaultModel") or (profile.get("models") or [{}])[0].get("id")
            try:
                res = proc.rpc.request("thread/start", start, timeout=30) or {}
            except RuntimeError as exc:
                return self._classify_provider_error(str(exc))
            tid = res.get("threadId") or res.get("id") or (res.get("thread") or {}).get("id", "")
            if not tid:
                return {"ok": False, "outcome": "runtime-error", "message": "测试线程创建失败"}
            # Ephemeral threads cannot be polled via thread/read; watch the
            # event bus instead — turn/end + assistant/message frames arrive
            # from the same translator pipeline as normal sessions.
            completed: dict[str, Any] = {}
            done = threading.Event()

            def on_frame(frame: dict[str, Any]) -> None:
                payload = frame.get("payload", {})
                if payload.get("type") != "session/event" or payload.get("sessionId") != tid:
                    return
                event = payload.get("event", {})
                if event.get("type") == "turn/end":
                    reason = (event.get("data") or {}).get("reason") or {}
                    completed["status"] = "failed" if reason.get("kind") == "error" else "completed"
                    completed["error"] = (reason.get("error") or {}).get("message", "")
                    done.set()
                elif event.get("type") == "assistant/message":
                    content = ((event.get("data") or {}).get("message") or {}).get("content") or []
                    completed["answered"] = any((b or {}).get("text") for b in content if isinstance(b, dict))

            unsubscribe = self.subscribe(on_frame)
            try:
                proc.rpc.request("turn/start", {
                    "threadId": tid, "cwd": isolated,
                    "input": [{"type": "text", "text": "Reply exactly: OK"}],
                }, timeout=30)
                done.wait(60)
            except RuntimeError as exc:
                return self._classify_provider_error(str(exc))
            finally:
                unsubscribe()
            try:
                proc.rpc.request("thread/delete", {"threadId": tid}, timeout=15)
            except (TimeoutError, RuntimeError):
                pass
            if not completed:
                return {"ok": False, "outcome": "timeout", "message": "测试回合 60 秒内未完成"}
            if completed.get("status") == "failed":
                return self._classify_provider_error(completed.get("error") or "turn failed")
            if not completed.get("answered"):
                return {"ok": False, "outcome": "protocol-incompatible",
                        "message": "回合完成但未返回模型回复（协议或模型不兼容）"}
            return {"ok": True, "outcome": "ok", "message": "连接与推理正常"}

    @staticmethod
    def _classify_provider_error(message: str) -> dict[str, Any]:
        msg = message.lower()
        if "401" in msg or "unauthorized" in msg or "authentication" in msg or "api key" in msg or "invalid token" in msg:
            return {"ok": False, "outcome": "auth-failed", "message": "鉴权失败：API Key 无效或过期"}
        if "404" in msg or "model not found" in msg or "unknown model" in msg:
            return {"ok": False, "outcome": "model-not-found", "message": "模型不存在：检查 Model ID"}
        if "connect" in msg or "unreachable" in msg or "refused" in msg or "dns" in msg or "timed out" in msg:
            return {"ok": False, "outcome": "unreachable", "message": "端点不可达：检查 Base URL"}
        if "responses" in msg and ("unsupported" in msg or "not found" in msg or "405" in msg):
            return {"ok": False, "outcome": "protocol-incompatible",
                    "message": "该服务不兼容当前 Codex Runtime（需 OpenAI Responses API）"}
        return {"ok": False, "outcome": "runtime-error", "message": f"Codex 运行时错误: {message[:140]}"}

    def _register_active_provider(self, provider_id: str) -> None:
        """Push custom provider definitions into the codex runtime before a
        thread binds them. Verified protocol (docs/codex-protocol-notes.md):
        config/value/write {keyPath: "model_providers.<id>", mergeStrategy:
        "upsert", value: ModelProviderInfo(snake_case)}; upsert appends a
        table and never touches unrelated config."""
        if self.providers is None or provider_id == "chatgpt":
            return
        profile = self.providers.get(provider_id)
        if not profile or profile.get("type") != "custom":
            return
        self._write_provider_config(profile)

    def _write_provider_config(self, profile: dict[str, Any]) -> bool:
        proc = self._ensure_process()
        try:
            proc.rpc.request("config/value/write", {
                "keyPath": f"model_providers.{profile.get('id')}",
                "mergeStrategy": "upsert",
                "value": {
                    "name": profile.get("name") or profile.get("id"),
                    "base_url": profile.get("baseUrl"),
                    "env_key": profile.get("envKey"),
                    "wire_api": profile.get("wireApi") or "responses",
                },
            }, timeout=15)
            return True
        except (TimeoutError, RuntimeError) as exc:
            self._debug_log(f"provider registration failed: {str(exc)[:160]}")
            return False

    def _provider_thread_params(self, provider_id: str) -> dict[str, Any]:
        if provider_id and provider_id != "chatgpt":
            profile = self.providers.get(provider_id) if self.providers else None
            if profile:
                params: dict[str, Any] = {"modelProvider": provider_id}
                if profile.get("defaultModel"):
                    params["model"] = profile["defaultModel"]
                return params
        return {}

    # session.prompt -> turn/start | turn/steer
    def _rpc_session_prompt(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        sid = str(body.get("sessionId", ""))
        if not sid:
            return err_value("sessionId required")
        input_items = self._content_to_input(body.get("content", []))
        if not input_items:
            return err_value("empty prompt content")
        reg = self.registry.ensure(sid)
        # Non-standard agent presets prepend their standing instruction to
        # every turn (transparent: the text rides with the user message).
        preset = self._preset_document(str(reg.get("agentPreset") or "standard"))
        if preset and preset.get("id") != "standard" and preset.get("content"):
            input_items = [{"type": "text", "text": str(preset["content"])}] + input_items
        if reg.get("running"):
            # Schema: steer requires expectedTurnId (active turn precondition).
            params = {"threadId": sid, "input": input_items,
                      "expectedTurnId": reg.get("turnId") or "",
                      "clientUserMessageId": rpc_id or str(uuid.uuid4())}
            try:
                res = proc.rpc.request("turn/steer", params, timeout=30) or {}
            except RuntimeError as exc:
                # The registry can lag behind a turn that just completed;
                # fall back to a fresh turn instead of failing the message.
                if "expectedTurnId" not in str(exc) and "does not match" not in str(exc):
                    raise
                self.registry.set_running(sid, False)
                res = None
            if res is not None:
                return ok_value({"accepted": True, "submissionId": res.get("turnId") or ""})
        params = {"threadId": sid, "input": input_items,
                  "clientUserMessageId": rpc_id or str(uuid.uuid4())}
        model, effort = self._session_model_overrides(sid)
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        params.update(self._sandbox_params(sid))

        def start_turn():
            try:
                return proc.rpc.request("turn/start", params, timeout=30) or {}
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "not found" in msg or "not loaded" in msg:
                    return None  # cold thread: resume then retry below
                raise

        res = start_turn()
        if res is None:
            # Cold thread (e.g. after a gateway restart). Resume tolerates the
            # idempotent conflict when the thread already holds a writer.
            try:
                proc.rpc.request("thread/resume", {"threadId": sid}, timeout=60)
            except RuntimeError as exc:
                msg = str(exc)
                if "conflict" not in msg and "already has an active" not in msg:
                    raise
            self.registry.set_loaded(sid, True)
            res = start_turn()
            if res is None:
                return err_value("thread 无法加载以开始新回合", "thread-unavailable")
        self.registry.set_loaded(sid, True)
        return ok_value({"accepted": True, "submissionId": res.get("turnId") or ""})

    # session.cancel -> turn/interrupt
    def _rpc_session_cancel(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        sid = str(body.get("sessionId", ""))
        reg = self.registry.get(sid) or {}
        # Idle session: nothing to interrupt; interrupting a completed turn id
        # is rejected by codex and would surface a spurious error in the UI.
        if not reg.get("running"):
            return ok_value({"cancelled": True})
        turn_id = reg.get("turnId") or ""
        if not turn_id:
            return ok_value({"cancelled": True})
        proc.rpc.request("turn/interrupt", {"threadId": sid, "turnId": turn_id}, timeout=15)
        self.registry.set_running(sid, False)
        return ok_value({"cancelled": True})

    # session.history -> thread/read + fold
    def _rpc_session_history(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        sid = str(body.get("sessionId", ""))
        try:
            res = proc.rpc.request("thread/read", {"threadId": sid, "includeTurns": True}, timeout=60) or {}
        except RuntimeError as exc:
            if "not materialized" not in str(exc):
                raise
            # Blank thread (no first user message yet): read metadata only.
            res = proc.rpc.request("thread/read", {"threadId": sid, "includeTurns": False}, timeout=60) or {}
        thread = res.get("thread") or res or {}
        events = self.folder.fold(thread)
        self.translator.set_seq_floor(sid, len(events))
        self.translator.set_proj_floor(sid, len(events))
        self.registry.ensure(sid, cwd=thread.get("cwd"))
        # Pagination: beforeSeq returns events strictly below that seq
        # (scroll-up pages); maxMessages caps the page size tail.
        before_seq = body.get("beforeSeq")
        if before_seq:
            events = [e for e in events if e["event"]["seq"] < int(before_seq)]
        max_messages = body.get("maxMessages")
        has_more = False
        if max_messages and len(events) > int(max_messages):
            events = events[-int(max_messages):]
            has_more = True
        projections: dict[str, Any] = {"permissions": self._permission_view(sid)}
        reg = self.registry.get(sid) or {}
        if reg.get("usage"):
            projections["tokenUsage"] = reg["usage"]
        if reg.get("pressure"):
            projections["contextPressure"] = reg["pressure"]
        if reg.get("plan"):
            projections["plan"] = reg["plan"]
        if reg.get("goal"):
            projections["goal"] = reg["goal"]
        return ok_value({"events": events, "hasMore": has_more,
                         "projections": {"asOfSeq": len(events), "values": projections}})

    # session.models -> provider-aware model catalogue
    def _rpc_session_models(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        reg = self.registry.get(sid) or {}
        # Bound sessions only ever see their own provider's models; fresh
        # sessions see the active provider's.
        provider_id = reg.get("providerId") or (self.providers.active_id() if self.providers else "chatgpt")
        profile = self.providers.get(provider_id) if self.providers else None
        if profile is None:  # no manager (tests) or unknown id -> builtin
            profile = {"id": "chatgpt", "name": "ChatGPT / Codex", "type": "chatgpt"}
        current_default: str | None = None
        if profile.get("type") == "chatgpt":
            proc = self._ensure_process()
            data = proc.rpc.request("model/list", {}, timeout=30) or {}
            cfg = {}
            try:
                cfg = proc.rpc.request("config/read", {}, timeout=15).get("config", {}) or {}
            except (TimeoutError, RuntimeError):
                pass
            models: list[dict[str, Any]] = []
            efforts = ["low", "medium", "high"]
            current_default = None
            for m in data.get("data", []) or []:
                mid = m.get("id") or m.get("model")
                if not mid:
                    continue
                sup = [e for e in m.get("supportedReasoningEfforts", []) or [] if isinstance(e, dict)]
                eff_objs = [{"name": e.get("reasoningEffort", ""), "description": e.get("description", "")} for e in sup]
                names = [e["name"] for e in eff_objs if e["name"]]
                models.append({"model": mid, "name": m.get("displayName") or mid,
                               "reasoning": {"efforts": eff_objs,
                                             "defaultEffort": m.get("defaultReasoningEffort") or (names[-1] if names else None)}})
                if mid == cfg.get("model"):
                    current_default = mid
                    if names:
                        efforts = names
                    current_default_eff = m.get("defaultReasoningEffort")
            current_model = reg.get("model") or current_default or cfg.get("model") or "gpt-5.6-luna"
            group_name = "ChatGPT / Codex"
            default_effort = current_default_eff if current_default else efforts[-1]
        else:
            models = [{"model": m.get("id"), "name": m.get("label") or m.get("id"),
                       "reasoning": {"efforts": [], "defaultEffort": None}}
                      for m in profile.get("models", []) if m.get("id")]
            current_model = reg.get("model") or profile.get("defaultModel") or (models[0]["model"] if models else None)
            group_name = profile.get("name") or provider_id
            efforts = []
            default_effort = None
        return ok_value({
            "groups": [{"id": provider_id, "name": group_name, "models": models}],
            "current": {"model": current_model, "provider": provider_id,
                        "reasoningEffort": reg.get("effort"),
                        "reasoning": {"efforts": efforts, "defaultEffort": default_effort or (efforts[-1] if efforts else None)}},
        })

    # session.selectModel
    def _rpc_session_selectModel(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        reg = self.registry.get(sid) or {}
        target_provider = str(body.get("provider") or "")
        bound_provider = reg.get("providerId")
        # Cross-provider switching on an existing session is rejected: the
        # conversation history belongs to its provider.
        if bound_provider and target_provider and target_provider != bound_provider:
            return err_value("模型服务变更将在新会话中生效", "provider-change-requires-new-session")
        self.registry.set_model(sid, body.get("model"), body.get("reasoningEffort"))
        return ok_value({"selected": True})

    # workspace.*
    def _workspace_id_for(self, cwd: str | None) -> str:
        """Group a session under the workspace whose path matches its cwd;
        unknown cwd lands in the first (default) workspace."""
        for ws in self._state.workspaces():
            if cwd and ws.get("path") == cwd:
                return str(ws["id"])
        workspaces = self._state.workspaces()
        return str(workspaces[0]["id"]) if workspaces else "laomo-clean"

    def _rpc_workspace_list(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        items = []
        with self.registry._lock:
            sessions_by_cwd: dict[str, list[str]] = {}
            for tid, reg in self.registry._sessions.items():
                sessions_by_cwd.setdefault(str(reg.get("cwd") or ""), []).append(tid)
        for ws in self._state.workspaces():
            items.append({"workspaceId": ws["id"], "id": ws["id"],
                          "title": ws.get("title") or ws.get("path"),
                          "path": ws.get("path"),
                          "sessionIds": sessions_by_cwd.get(str(ws.get("path")), []),
                          "archivedSessionIds": []})
        return ok_value({"items": items})

    def _rpc_workspace_create(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        path = str(body.get("path", "")).strip()
        if not path:
            return err_value("path required")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return err_value(f"not a directory: {path}")
        self.set_workspace_cwd(path)
        ws = self._state.add_workspace(path)
        return ok_value({"workspaceId": ws["id"], "id": ws["id"],
                         "title": ws.get("title"), "path": path})

    def _rpc_workspace_rename(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        workspace_id = str(body.get("workspaceId", ""))
        title = str(body.get("title", "")).strip()
        if not title:
            return err_value("title required")
        ws = self._state.mutate_workspace(workspace_id, title=title)
        if ws is None:
            return err_value("workspace not found")
        return ok_value({"workspaceId": workspace_id, "title": title})

    def _rpc_workspace_delete(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        workspace_id = str(body.get("workspaceId", ""))
        ws = self._state.workspace(workspace_id)
        if ws is None:
            return err_value("workspace not found")
        if len(self._state.workspaces()) <= 1:
            return err_value("至少保留一个项目")
        self._state.delete_workspace(workspace_id)
        # deleting the active project falls back to the first remaining one
        if ws.get("path") and ws["path"] == self.workspace_cwd():
            fallback = self._state.workspaces()[0]
            self.set_workspace_cwd(str(fallback["path"]))
        return ok_value({"deleted": workspace_id})

    def _rpc_workspace_insertBefore(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        workspace_id = str(body.get("workspaceId", ""))
        before = body.get("beforeWorkspaceId")
        if not self._state.reorder_workspace(workspace_id, str(before) if before else None):
            return err_value("workspace not found")
        return ok_value({"reordered": workspace_id})

    def _rpc_workspace_archiveSession(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        try:
            proc = self._ensure_process()
            proc.rpc.request("thread/archive", {"threadId": str(body.get("sessionId", ""))}, timeout=15)
        except (AdapterUnavailable, TimeoutError, RuntimeError) as exc:
            self._debug_log(f"archive failed (ignored): {exc}")
        return ok_value({"archived": True})

    # goal.* -> thread/goal/set|clear (objective tracking with phase buttons)
    def _goal_set(self, body: dict[str, Any], status: str | None = None) -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        if not sid:
            return err_value("sessionId required")
        objective = str(body.get("objective", "")).strip()
        reg = self.registry.get(sid) or {}
        current = reg.get("goal") or {}
        if not objective:
            objective = str(current.get("objective", "") or "")
        if not objective:
            return err_value("objective required")
        proc = self._ensure_process()
        params: dict[str, Any] = {"threadId": sid, "objective": objective}
        if status:
            params["status"] = status
        proc.rpc.request("thread/goal/set", params, timeout=20)
        view = {"objective": objective, "phase": status or str(current.get("phase", "active")),
                "ref": {"id": sid, "revision": int(time.time() * 1000)}}
        self.registry.set_goal(sid, view)
        self._emit(self.translator.session_projection(sid, "goal", view))
        return ok_value({"goal": view})

    def _rpc_goal_create(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return self._goal_set(body, status="active")

    def _rpc_goal_edit(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return self._goal_set(body)

    def _rpc_goal_pause(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return self._goal_set(body, status="paused")

    def _rpc_goal_resume(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return self._goal_set(body, status="active")

    def _rpc_goal_complete(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return self._goal_set(body, status="complete")

    def _rpc_goal_clear(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        if not sid:
            return err_value("sessionId required")
        proc = self._ensure_process()
        proc.rpc.request("thread/goal/clear", {"threadId": sid}, timeout=20)
        self.registry.set_goal(sid, None)
        self._emit(self.translator.session_projection(sid, "goal", {"objective": None}))
        return ok_value({"cleared": True})

    # commands/execute -> /permission <level> (the only line the UI sends)
    _SANDBOX_MAP = {
        "read-only": {"type": "readOnly"},
        "workspace-write": {"type": "workspaceWrite"},
        # 全自动: same workspace sandbox as missions, but the user is present —
        # interactive approvals would still stall every command, so never-ask.
        "full-auto": {"type": "workspaceWrite"},
        "danger-full-access": {"type": "dangerFullAccess"},
    }

    def _rpc_commands_execute(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        args = body.get("args") or {}
        line = str(args.get("line") or body.get("line") or "")
        m = re.fullmatch(r"\s*/permission\s+(read-only|workspace-write|full-auto|danger-full-access)\s*", line)
        sid = str(args.get("agentId") or body.get("agentId") or "")
        if not m:
            return ok_value({"ok": False, "output": f"codex runtime 不支持该命令: {line.strip()[:60]}"})
        value = m.group(1)
        if not sid:
            return err_value("agentId required")
        self.registry.set_permission(sid, value)
        # Instant readback: the UI confirms by the permissions projection.
        self._emit(self.translator.session_projection(sid, "permissions", {"currentValue": value}))
        return ok_value({"ok": True, "output": f"permission -> {value}"})

    def _permission_view(self, sid: str) -> dict[str, Any]:
        reg = self.registry.get(sid) or {}
        return {"currentValue": reg.get("permission") or "workspace-write"}

    def _sandbox_params(self, sid: str) -> dict[str, Any]:
        reg = self.registry.get(sid) or {}
        perm = reg.get("permission")
        if not perm or perm not in self._SANDBOX_MAP:
            return {}
        params: dict[str, Any] = {"sandboxPolicy": self._SANDBOX_MAP[perm]}
        # Keep interactive approvals on unless the user asked for an unattended
        # level: full-auto (sandboxed, never-ask) or danger-full-access.
        params["approvalPolicy"] = "never" if perm in ("full-auto", "danger-full-access") else "on-request"
        return params

    # respond -> answer pending codex server request
    def _rpc_respond(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        proc = self._ensure_process()
        rpc_id = str(body.get("rpcId", ""))
        descriptor = proc.rpc.pop_server_request(rpc_id)
        if descriptor is None:
            return err_value("not pending", "not-pending")
        value = (body.get("result") or {}).get("value") or {}
        outcome = value.get("outcome") or value.get("decision") or ""
        answers = value.get("answer", {}).get("answers") if isinstance(value.get("answer"), dict) else None
        result_obj = self._approval_result(descriptor.get("method", ""), outcome, answers)
        proc.rpc.respond(descriptor.get("codexId"), result_obj)
        sid = str((descriptor.get("params") or {}).get("threadId", ""))
        self._emit(self.translator.approval_resolved(sid, rpc_id))
        return ok_value({"accepted": True})

    def _approval_result(self, method: str, outcome: str, answers: list | None) -> Any:
        if answers is not None:  # question/input flow
            selected = []
            for a in answers:
                for s in a.get("selected", []) or []:
                    selected.append(s if isinstance(s, str) else s.get("label", ""))
            if "elicitation" in method:
                return {"action": selected[0] if selected else "cancel", "content": {"type": "text", "text": ""}}
            return {"answers": selected, "decision": selected[0] if selected else "decline"}
        if outcome in ("allowed-once", "allow", "accepted", "accept"):
            return {"decision": "accept"}
        if outcome in ("allowed-always", "allowed-session"):
            return {"decision": "acceptForSession"}
        return {"decision": "decline"}

    # --- stubs (P0: safely empty) ---
    def _rpc_session_rename(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        title = str(body.get("title", "")).strip()
        if not sid or not title:
            return err_value("sessionId 和 title 必填")
        try:
            proc = self._ensure_process()
            proc.rpc.request("thread/name/set", {"threadId": sid, "name": title}, timeout=15)
        except (AdapterUnavailable, TimeoutError, RuntimeError) as exc:
            return err_value(f"重命名失败: {exc}")
        self.registry.set_title(sid, title)
        return ok_value({"sessionId": sid, "title": title})

    def _rpc_session_fork(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        if not sid:
            return err_value("sessionId required")
        try:
            proc = self._ensure_process()
            res = proc.rpc.request("thread/fork", {"threadId": sid}, timeout=30) or {}
        except (AdapterUnavailable, TimeoutError, RuntimeError) as exc:
            return err_value(f"分叉失败: {exc}")
        new_id = res.get("threadId") or res.get("id") or (res.get("thread") or {}).get("id", "")
        if not new_id:
            return err_value("thread/fork 未返回新会话 ID")
        self.registry.ensure(new_id)
        self._emit(self.translator.session_added(new_id))
        return ok_value({"sessionId": new_id, "id": new_id})

    def _rpc_session_search(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        query = str(body.get("query", "")).strip().lower()
        if not query:
            return ok_value({"items": []})
        try:
            proc = self._ensure_process()
            data = proc.rpc.request("thread/list", {"limit": 100}, timeout=30) or {}
        except (AdapterUnavailable, TimeoutError, RuntimeError):
            return ok_value({"items": []})
        hits = []
        for t in data.get("data", []) or []:
            hay = f"{t.get('name') or ''} {t.get('preview') or ''}".lower()
            if query in hay:
                hits.append({"sessionId": t.get("id", ""), "title": t.get("name") or (t.get("preview") or "")[:60]})
        return ok_value({"items": hits})

    def _rpc_session_updateQueue(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return err_value("queue is owned by the workbench, not codex (P0)", "unsupported")

    def _rpc_session_attachment(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return err_value("attachments are not supported on the codex runtime in P0", "unsupported")

    def _generic_empty_ok(self, body: dict[str, Any]) -> dict[str, Any]:
        return ok_value({"items": [], "supported": False})

    # host.openPath -> reveal in Finder (native-host parity)
    def _rpc_host_openPath(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        if sys.platform != "darwin":
            return err_value("host.openPath 仅支持 macOS")
        path = os.path.abspath(os.path.expanduser(str(body.get("path", ""))))
        if not os.path.exists(path):
            return err_value(f"路径不存在: {path}")
        subprocess.Popen(["open", "-R", path])
        return ok_value({"opened": path})

    # settings.* -> writable namespaces (busyEnter …). The old stub silently
    # dropped the update while the UI toasted 已更新.
    def _rpc_settings_describe(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        namespaces = self._state.settings_namespaces()
        if not any(ns["ns"] == "ui-conversation" for ns in namespaces):
            namespaces.insert(0, {"ns": "ui-conversation", "revision": 0, "data": {}})
        return ok_value({"namespaces": namespaces, "path": self._state.path})

    def _rpc_settings_update(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        ns = str(body.get("ns", ""))
        patch = body.get("patch")
        if ns != "ui-conversation" or not isinstance(patch, dict):
            return err_value("仅支持 ui-conversation 命名空间")
        expected = body.get("expectedRevision")
        result = self._state.settings_update(ns, patch,
                                             None if expected is None else int(expected))
        if result is None:
            return err_value("设置版本冲突，请刷新页面后重试", "revision-conflict")
        return ok_value(result)

    def _rpc_settings_openDocument(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        if sys.platform != "darwin" or not os.path.exists(self._state.path):
            return err_value("设置文件尚未生成，先修改一次设置")
        subprocess.Popen(["open", "-R", self._state.path])
        return ok_value({"opened": self._state.path})

    # credentials.* -> the provider CredentialStore (macOS Keychain)
    def _credential_store(self):
        return getattr(self.providers, "credentials", None) if self.providers else None

    def _rpc_credentials_describe(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        store = self._credential_store()
        out: dict[str, Any] = {}
        for ref in body.get("refs") or []:
            ref = str(ref)
            if store is None:
                out[ref] = {"configured": False, "source": "运行时未启用凭证库"}
            else:
                out[ref] = {"configured": store.has(ref), "source": store.storage_description()}
        return ok_value({"credentials": out})

    def _rpc_credentials_set(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        store = self._credential_store()
        if store is None:
            return err_value("运行时未启用凭证库")
        ref = str(body.get("ref", "")).strip()
        if not ref:
            return err_value("ref required")
        try:
            store.set(ref, str(body.get("value", "")))
        except Exception as exc:  # ProviderError surfaces its own message
            return err_value(str(exc))
        return ok_value({"ref": ref, "configured": True})

    def _rpc_credentials_unset(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        store = self._credential_store()
        if store is None:
            return err_value("运行时未启用凭证库")
        ref = str(body.get("ref", "")).strip()
        store.delete(ref)
        return ok_value({"ref": ref, "configured": False})

    # llm.* -> provider profiles + the codex model catalogue
    def _rpc_llm_providers(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        if self.providers is None:
            items = [{"provider": "chatgpt", "displayName": "ChatGPT / Codex（内置）", "active": True}]
            return ok_value({"items": items, "providers": items, "activeProviderId": "chatgpt"})
        listed = self.providers.public_list()
        active = listed.get("activeProviderId") or "chatgpt"
        items = []
        for profile in listed.get("providers", []):
            items.append({"provider": profile.get("id"),
                          "displayName": profile.get("name") or profile.get("id"),
                          "active": profile.get("id") == active,
                          "builtin": profile.get("type") == "chatgpt",
                          "secretConfigured": bool(profile.get("secretConfigured"))})
        return ok_value({"items": items, "providers": items, "activeProviderId": active,
                         "secretStorage": listed.get("secretStorage")})

    def _rpc_llm_models(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        # Same catalogue session.models serves (provider-aware groups).
        return self._rpc_session_models({})

    def _rpc_llm_discoverModels(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        # For codex, model/list IS the discovery: no API-key probing needed.
        catalogue = self._rpc_session_models({})
        value = catalogue.get("result", {}).get("value") or {}
        models = [model for group in value.get("groups", []) for model in group.get("models", [])]
        return ok_value({"models": models, "source": "codex model/list"})

    # agentPreset.* — builtin + user presets; non-standard presets prepend a
    # standing instruction to every prompt (transparent, like the work modes).
    BUILTIN_PRESETS = [
        {"id": "standard", "name": "标准模式", "trust": "builtin", "isDefault": True,
         "description": "均衡的默认执行风格（无附加指令）。",
         "content": "（标准模式：不附加额外指令。）"},
        {"id": "concise", "name": "简洁执行", "trust": "builtin",
         "description": "少解释、多交付，先给结果。",
         "content": "【执行风格】回复保持简洁：先给结果与关键改动，再给最少必要的说明；不重复已知背景。"},
        {"id": "planner", "name": "计划先行", "trust": "builtin",
         "description": "先给出分步计划，确认后再执行。",
         "content": "【执行风格】对任何实质性改动：先给出分步计划（要动什么、怎么验证），等我确认后再执行。"},
    ]

    def _preset_catalogue(self) -> list[dict[str, Any]]:
        items = [dict(p) for p in self.BUILTIN_PRESETS]
        for pid, doc in sorted(self._state.custom_presets().items()):
            items.append({"id": pid, "name": doc.get("name") or pid, "trust": "user",
                          "description": doc.get("description") or "自定义预设",
                          "content": doc.get("content", "")})
        return items

    def _preset_document(self, preset_id: str) -> dict[str, Any] | None:
        for preset in self._preset_catalogue():
            if preset["id"] == preset_id:
                return preset
        return None

    def _rpc_agentPreset_list(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return ok_value({"presets": self._preset_catalogue()})

    def _rpc_agentPreset_select(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        sid = str(body.get("sessionId", ""))
        preset_id = str(body.get("agentPreset", ""))
        if not sid:
            return err_value("sessionId required")
        if self._preset_document(preset_id) is None:
            return err_value(f"未知预设: {preset_id}")
        self.registry.set_agent_preset(sid, preset_id)
        self._emit(self.translator.session_projection(sid, "agentPreset", preset_id))
        return ok_value({"sessionId": sid, "agentPreset": preset_id})

    def _rpc_agentPreset_read(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        preset = self._preset_document(str(body.get("agentPreset", "")))
        if preset is None:
            return err_value("未知预设")
        return ok_value({"name": preset["name"], "content": preset.get("content", ""),
                         "trust": preset.get("trust", "builtin")})

    def _rpc_agentPreset_copy(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        source = self._preset_document(str(body.get("from", "")))
        if source is None:
            return err_value("来源预设不存在")
        preset_id = str(body.get("agentPreset", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", preset_id):
            return err_value("预设 ID 只能是英文、数字或连字符")
        if any(p["id"] == preset_id for p in self._preset_catalogue()):
            return err_value(f"预设 ID 已存在: {preset_id}")
        doc = {"name": str(body.get("name") or preset_id),
               "description": f"复制自 {source['name']}", "content": source.get("content", "")}
        self._state.put_preset(preset_id, doc)
        return ok_value({"agentPreset": preset_id, "name": doc["name"]})

    def _rpc_agentPreset_remove(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        preset_id = str(body.get("agentPreset", ""))
        if not self._state.delete_preset(preset_id):
            return err_value("只能删除自定义预设")
        return ok_value({"removed": preset_id})

    def _rpc_agentPreset_openDocument(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return err_value("codex 运行时没有原生文档窗口，请用「查看」阅读预设内容")

    # subagent.* — codex has no subagent runtime; be honest instead of the
    # old stub that made 已发送给子代理 toast on a no-op.
    def _rpc_subagent_list(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return ok_value({"items": [], "supported": False})

    def _rpc_subagent_history(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return ok_value({"events": [], "supported": False})

    def _rpc_subagent_prompt(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return err_value("codex 运行时暂不支持子代理")

    def _rpc_subagent_interrupt(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return err_value("codex 运行时暂不支持子代理")

    def _rpc_skill_list(self, body: dict[str, Any], rpc_id: str = "") -> dict[str, Any]:
        return ok_value({"skills": [], "supported": False})

    def _rpc_generic(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        self._debug_log(f"stubbed rpc: {endpoint}")
        return ok_value({"supported": False, "items": []})

    # helpers
    def _session_model_overrides(self, sid: str) -> tuple[str | None, str | None]:
        reg = self.registry.get(sid) or {}
        return reg.get("model"), reg.get("effort")

    @staticmethod
    def _prompt_text(content: list[Any]) -> str:
        return " ".join(str(b.get("text", "")) for b in content or []
                        if isinstance(b, dict) and b.get("type") == "text").strip()

    @staticmethod
    def _content_to_input(content: list[Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                items.append({"type": "text", "text": str(block["text"])})
            elif block.get("type") == "image" and block.get("data"):
                items.append({"type": "image", "url": f"data:{block.get('mediaType', 'image/png')};base64,{block['data']}"})
        return items
