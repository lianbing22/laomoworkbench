"""Model Provider Profiles for LaoMo Workbench (P0.5).

Control-plane layer that lets users configure model-service providers
(ChatGPT login or custom OpenAI Responses-compatible endpoints) without
editing Codex config files by hand. Relationship:

    Mode -> Runtime -> ProviderProfile -> Model -> Session/Thread

Pieces:
- ProfileStore       provider definitions (JSON, no secrets ever)
- CredentialStore    secrets in macOS Keychain; session-only fallback
- ProviderProfileManager  CRUD/activate/env-injection/redaction
- CodexProviderConfig     non-destructive registration into the Codex runtime
                          (strategy depends on the verified protocol; see
                          docs/codex-protocol-notes.md)

Hard rules enforced here:
- secrets never persisted in profiles JSON / logs / API responses
- empty secret on update keeps the previous value
- the built-in "chatgpt" profile is undeletable and needs no configuration
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

SECRET_ENV_PREFIX = "LAOMO_CODEX_PROVIDER"
BUILTIN_CHATGPT_ID = "chatgpt"
# Current Codex only accepts the Responses wire protocol (verified against
# 0.148.0-alpha.21; the legacy "chat" wire api has been removed upstream).
SUPPORTED_WIRE_APIS = ("responses",)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return base or f"provider-{uuid.uuid4().hex[:6]}"


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# --- CredentialStore ---------------------------------------------------------


class CredentialStore:
    """Secrets via macOS Keychain (`security` CLI). If the Keychain is
    unavailable the secret lives in process memory only — and callers MUST
    surface that "session-only" fact; we never silently persist plaintext."""

    SERVICE = "laomo-workbench-provider"

    def __init__(self) -> None:
        self._memory: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def persistent(self) -> bool:
        return os.uname().sysname == "Darwin"

    # -- keychain plumbing --
    def _kc(self, args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(["security", *args], capture_output=True, text=True, timeout=10)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def set(self, profile_id: str, secret: str) -> None:
        secret = secret.strip()
        if not secret:
            raise ProviderError("secret 不能为空")
        with self._lock:
            self._memory[profile_id] = secret
        if self.persistent:
            # delete-then-add keeps updates idempotent
            self._kc(["delete-generic-password", "-s", self.SERVICE, "-a", profile_id])
            code, out = self._kc(["add-generic-password", "-U", "-s", self.SERVICE,
                                  "-a", profile_id, "-w", secret])
            if code != 0:
                # Keep the in-memory copy so the running session still works,
                # but surface the non-persistence through has()/describe.
                raise ProviderError(f"钥匙串写入失败，本次运行内仍有效: {out.strip()[:80]}", "keychain")

    def get(self, profile_id: str) -> str | None:
        with self._lock:
            if profile_id in self._memory:
                return self._memory[profile_id]
        if self.persistent:
            code, out = self._kc(["find-generic-password", "-s", self.SERVICE, "-a", profile_id, "-w"])
            if code == 0:
                secret = out.strip()
                with self._lock:
                    self._memory[profile_id] = secret
                return secret
        return None

    def has(self, profile_id: str) -> bool:
        return self.get(profile_id) is not None

    def delete(self, profile_id: str) -> None:
        with self._lock:
            self._memory.pop(profile_id, None)
        if self.persistent:
            self._kc(["delete-generic-password", "-s", self.SERVICE, "-a", profile_id])

    def storage_description(self) -> str:
        return "API Key 仅保存在本机安全存储（macOS 钥匙串）" if self.persistent \
            else "当前平台仅保存至本次运行"


# --- ProfileStore -------------------------------------------------------------


class ProfileStore:
    """Provider definitions on disk. Secrets live ONLY in CredentialStore."""

    def __init__(self, root: Path) -> None:
        self.path = root / "providers.json"
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"schema": 1, "activeProviderId": BUILTIN_CHATGPT_ID, "providers": []}

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.path)


# --- Manager -------------------------------------------------------------------


class ProviderProfileManager:
    def __init__(self, state_root: Path, credentials: CredentialStore | None = None) -> None:
        self.store = ProfileStore(state_root)
        self.credentials = credentials or CredentialStore()
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str], None]] = []

    def on_change(self, callback: Callable[[str], None]) -> None:
        """callback(event) where event in {"saved","deleted","activated"}."""
        with self._lock:
            self._listeners.append(callback)

    def _emit(self, event: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                pass

    # -- builtin --
    @staticmethod
    def builtin_profile() -> dict[str, Any]:
        return {
            "id": BUILTIN_CHATGPT_ID,
            "name": "ChatGPT / Codex",
            "type": "chatgpt",
            "baseUrl": None,
            "wireApi": "responses",
            "envKey": None,
            "models": [],          # dynamic: from codex model/list
            "defaultModel": None,
            "enabled": True,
            "builtin": True,
            "createdAt": 0,
        }

    # -- queries --
    def _data(self) -> dict[str, Any]:
        return self.store.load()

    def list(self) -> list[dict[str, Any]]:
        data = self._data()
        out = [self.builtin_profile()]
        out.extend(p for p in data.get("providers", []) if isinstance(p, dict))
        return out

    def get(self, profile_id: str) -> dict[str, Any] | None:
        for p in self.list():
            if p.get("id") == profile_id:
                return p
        return None

    def active_id(self) -> str:
        active = self._data().get("activeProviderId")
        if active and self.get(active):
            return active
        return BUILTIN_CHATGPT_ID

    def active(self) -> dict[str, Any]:
        return self.get(self.active_id()) or self.builtin_profile()

    # -- redaction: the only shape ever returned to the UI --
    def public(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": profile.get("id"),
            "name": profile.get("name"),
            "type": profile.get("type"),
            "baseUrl": profile.get("baseUrl"),
            "wireApi": profile.get("wireApi") or "responses",
            "envKey": profile.get("envKey"),
            "models": profile.get("models") or [],
            "defaultModel": profile.get("defaultModel"),
            "enabled": bool(profile.get("enabled", True)),
            "builtin": bool(profile.get("builtin")),
            "secretConfigured": profile.get("type") == "chatgpt" or self.credentials.has(profile.get("id", "")),
        }

    def public_list(self) -> dict[str, Any]:
        return {"ok": True,
                "providers": [self.public(p) for p in self.list()],
                "activeProviderId": self.active_id(),
                "secretStorage": self.credentials.storage_description()}

    # -- CRUD --
    _URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        ptype = str(payload.get("type") or "custom")
        if ptype not in ("chatgpt", "custom"):
            raise ProviderError("type 必须是 chatgpt 或 custom")
        if ptype == "chatgpt":
            # the builtin profile is managed, not editable
            raise ProviderError("内置 ChatGPT 配置不可编辑", "builtin")
        if not name:
            raise ProviderError("名称不能为空")
        base_url = str(payload.get("baseUrl") or "").strip().rstrip("/")
        if not base_url or not self._URL_RE.match(base_url):
            raise ProviderError("Base URL 必须是合法的 http(s) 地址")
        wire_api = str(payload.get("wireApi") or "responses")
        if wire_api not in SUPPORTED_WIRE_APIS:
            raise ProviderError(f"当前 Codex Runtime 仅支持 Responses 协议（wire_api={wire_api} 不兼容）",
                                "protocol-incompatible")
        models = []
        for m in payload.get("models") or []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "").strip()
            if mid:
                models.append({"id": mid, "label": str(m.get("label") or mid).strip() or mid})
        default_model = str(payload.get("defaultModel") or "").strip()
        if default_model and models and not any(m["id"] == default_model for m in models):
            raise ProviderError("默认模型必须在模型列表中")
        if not models and not default_model:
            raise ProviderError("至少配置一个模型（Model ID）")

        data = self._data()
        profiles = data.setdefault("providers", [])
        existing = next((p for p in profiles if p.get("id") == profile_id), None) if profile_id else None
        if existing is None:
            profile_id = profile_id or _slug(name)
            base = profile_id
            index = 2
            while any(p.get("id") == profile_id for p in profiles) or profile_id == BUILTIN_CHATGPT_ID:
                profile_id = f"{base}-{index}"
                index += 1
            existing = {"id": profile_id, "createdAt": _now_ms()}
            profiles.append(existing)
        existing.update({
            "name": name, "type": ptype, "baseUrl": base_url, "wireApi": wire_api,
            "envKey": f"{SECRET_ENV_PREFIX}_{re.sub(r'[^A-Z0-9_]', '_', profile_id.upper())}_KEY",
            "models": models, "defaultModel": default_model or (models[0]["id"] if models else None),
            "enabled": bool(payload.get("enabled", True)),
            "builtin": False, "updatedAt": _now_ms(),
        })
        self.store.save(data)

        secret = payload.get("secret")
        if isinstance(secret, str) and secret.strip():
            self.credentials.set(profile_id, secret)
        # empty/missing secret keeps the stored value (hard rule)
        if not self.credentials.has(profile_id):
            raise ProviderError(f"已保存配置，但尚未配置 API Key（{self.credentials.storage_description()}）",
                                "missing-key")

        self._emit("saved")
        return self.public(self.get(profile_id) or existing)

    def delete_profile(self, profile_id: str) -> None:
        if profile_id == BUILTIN_CHATGPT_ID:
            raise ProviderError("内置 ChatGPT 配置不可删除", "builtin")
        data = self._data()
        profiles = data.get("providers", [])
        remaining = [p for p in profiles if p.get("id") != profile_id]
        if len(remaining) == len(profiles):
            raise ProviderError("Provider 不存在", "not-found")
        data["providers"] = remaining
        if data.get("activeProviderId") == profile_id:
            data["activeProviderId"] = BUILTIN_CHATGPT_ID
        self.store.save(data)
        self.credentials.delete(profile_id)
        self._emit("deleted")

    def set_secret(self, profile_id: str, secret: str) -> None:
        if not self.get(profile_id) or profile_id == BUILTIN_CHATGPT_ID:
            raise ProviderError("Provider 不存在", "not-found")
        self.credentials.set(profile_id, secret)

    def activate(self, profile_id: str) -> str:
        profile = self.get(profile_id)
        if not profile:
            raise ProviderError("Provider 不存在", "not-found")
        if not profile.get("enabled", True):
            raise ProviderError("该 Provider 已停用")
        if profile.get("type") == "custom" and not self.credentials.has(profile_id):
            raise ProviderError("先配置该 Provider 的 API Key", "missing-key")
        data = self._data()
        data["activeProviderId"] = profile_id
        self.store.save(data)
        self._emit("activated")
        return profile_id

    # -- runtime bridge --
    def env_for_process(self) -> dict[str, str]:
        """Environment injection for the codex subprocess: every configured
        custom profile exposes its secret under its envKey."""
        env: dict[str, str] = {}
        for profile in self.list():
            env_key = profile.get("envKey")
            if profile.get("type") == "custom" and env_key:
                secret = self.credentials.get(profile.get("id", ""))
                if secret:
                    env[env_key] = secret
        return env

    def codex_provider_definitions(self) -> list[dict[str, Any]]:
        """Custom providers in Codex model_providers TOML shape."""
        out = []
        for profile in self.list():
            if profile.get("type") != "custom":
                continue
            out.append({
                "id": profile.get("id"),
                "baseUrl": profile.get("baseUrl"),
                "envKey": profile.get("envKey"),
                "wireApi": profile.get("wireApi") or "responses",
            })
        return out
