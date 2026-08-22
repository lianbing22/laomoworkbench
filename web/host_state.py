"""Workbench host state: multi-project workspaces, settings, agent presets.

Host-layer capability owned by the gateway product itself — deliberately
independent of any runtime adapter (Codex today, others later). One JSON
file under the product state dir; the gateway is a single process, so an
in-process lock plus atomic replace is enough.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any


def _host_state_root() -> str:
    # LAOMO_HOST_STATE_ROOT isolates gate drivers and test harnesses from the
    # user's real product state (a polluted seed once shipped a temp dir into
    # the live workspace registry).
    override = os.environ.get("LAOMO_HOST_STATE_ROOT")
    if override:
        return override
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
