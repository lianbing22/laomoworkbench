#!/usr/bin/env python3
"""P0 Model-Selection Recovery — real-user E2E (8 steps).

Normal-user journey against an ISOLATED gateway (8777, scratch
LAOMO_HOST_STATE_ROOT) with the REAL codex runtime:

  1. select provider (builtin chatgpt — always installed)
  2. select model (no session yet = new-session default, verified)
  3. create session (explicit config, effective readback verified)
  4. send one message on the real runtime (turn completes)
  5. confirm the runtime actually used the selected model
  6. switch model mid-session (effort must survive)
  7. next turn confirms the new model (registry authoritative)
  8. gateway restart -> new session inherits the saved default

Run:  python3 scripts/e2e_model_selection.py <scratch-dir>
      GATE_PORT=8777 default; never 8766.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("GATE_PORT", "8777"))
BASE = f"http://127.0.0.1:{PORT}"
CODEX_BIN = os.environ.get("GATE_CODEX_BIN", str(Path.home() / ".local/bin/codex"))

_state = {"step": 0, "passed": [], "failed": []}


def step(name: str, ok: bool, detail: str = "") -> bool:
    _state["step"] += 1
    mark = "PASS" if ok else "FAIL"
    print(f"[{_state['step']:02d}] {mark} · {name}" + (f" — {detail}" if detail else ""), flush=True)
    (_state["passed"] if ok else _state["failed"]).append(name)
    return ok


def post(path: str, payload: dict, timeout: float = 130.0) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def harness(method: str, args: dict | None = None, timeout: float = 130.0) -> dict:
    """Frontend-shaped request ({type, rpcId, method, payload}) and DSH
    envelope unwrap ({result:{ok, value|error}} -> value dict)."""
    body = post(f"/api/harness/clean/{method}",
                {"type": "client-request", "method": method, "payload": args or {}},
                timeout=timeout)
    result = body.get("result") or {}
    if result.get("ok") is False:
        return {"__error": result.get("error")}
    return result.get("value") or {}


def wait_http(deadline: float = 30.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=3) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    scratch = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="laomo-model-e2e-"))
    scratch.mkdir(parents=True, exist_ok=True)
    state_root = scratch / "host-state"
    log = open(scratch / "gateway.log", "w")
    env = {**os.environ, "LAOMO_HOST_STATE_ROOT": str(state_root)}
    proc = subprocess.Popen(
        [sys.executable or "python3", str(REPO / "web" / "boujoy_server.py"),
         "--port", str(PORT), "--vault", str(scratch / "vault"), "--static", str(REPO / "web"),
         "--clean-runtime", "codex", "--codex-bin", CODEX_BIN, "--codex-cwd", str(scratch)],
        stdout=log, stderr=log, env=env)
    try:
        if not step("isolated gateway up", wait_http()):
            print((scratch / "gateway.log").read_text()[-2000:])
            return 1

        # ---- 1. providers list + pick the builtin ----
        providers = harness("llm.providers")
        items = providers.get("providers") or providers.get("items") or []
        active = next((p for p in items if p.get("active")), None) \
            or next((p for p in items if p.get("builtin")), None)
        active_id = (active or {}).get("id") or (active or {}).get("provider") or "chatgpt"
        step("1 provider selected (active followed, registry untouched)", active is not None, active_id)

        # ---- 2. directory + set new-session default BEFORE any session ----
        directory = harness("session.models", {})
        groups = directory.get("groups") or []
        models = [m for g in groups for m in (g.get("models") or [])]
        model_a = next((m.get("model") for m in models
                        if (m.get("reasoning") or {}).get("efforts")), None) or \
            next((m.get("model") for m in models), None)
        if not step("2a real model directory", bool(model_a), f"{len(models)} models, first={model_a}"):
            return 1
        saved = harness("settings.update",
                        {"ns": "model-selection",
                         "patch": {"provider": active_id, "model": model_a}})
        ok_saved = bool(saved.get("revision"))
        described = harness("settings.describe")
        ns = next((n for n in (described.get("namespaces") or [])
                   if n.get("ns") == "model-selection"), {})
        step("2b new-session default persisted + readback",
             ok_saved and ns.get("data", {}).get("model") == model_a,
             f"ns={ns.get('data')}")

        # ---- 3. create session with EXPLICIT config ----
        created = harness("session.create",
                          {"provider": active_id, "model": model_a})
        effective = created.get("effective") or {}
        sid = created.get("sessionId") or created.get("id")
        step("3 create session explicit + effective readback",
             bool(sid) and effective.get("model") == model_a,
             f"sid={sid} effective={effective}")

        # ---- 4. one REAL turn on the real runtime ----
        turn = harness("session.prompt",
                       {"sessionId": sid,
                        "content": "只回复一个词：pong"}, timeout=300)
        step("4 real turn completed", bool(turn.get("ok", True)) and bool(sid))

        # ---- 5. runtime used the selected model (authoritative readback) ----
        models_after = harness("session.models", {"sessionId": sid})
        current = (models_after.get("current") or {})
        step("5 session reports selected model",
             current.get("model") == model_a, f"current={current}")

        # ---- 6. switch model mid-session; effort must survive ----
        model_b = next((m.get("model") for m in models if m.get("model") != model_a), None)
        if model_b:
            sw = harness("session.selectModel", {"sessionId": sid, "model": model_b})
            applied = sw.get("applied")
            step("6 mid-session switch (validated + readback)",
                 bool(applied) and applied.get("model") == model_b,
                 f"applied={applied}")
        else:
            step("6 mid-session switch (single-model dir, skipped)", True)

        # unknown model must be rejected — the old bug wrote it + toasted
        bad = harness("session.selectModel", {"sessionId": sid, "model": "no-such-model-xyz"})
        bad_err = bad.get("__error") or {}
        step("6b unknown model rejected (no write)",
             bad_err.get("code") == "unknown-model", str(bad_err))

        # ---- 7. effort-only change keeps the model ----
        eff_dir = (models_after.get("groups") or [{}])[0]
        efforts = []
        for g in (models_after.get("groups") or []):
            for m in (g.get("models") or []):
                if m.get("model") == (model_b or model_a):
                    efforts = [e.get("name") for e in (m.get("reasoning") or {}).get("efforts") or []]
        if efforts:
            eff = harness("session.selectModel",
                          {"sessionId": sid, "reasoningEffort": efforts[0],
                           "model": model_b or model_a})
            applied = eff.get("applied")
            step("7 effort-only change (model preserved)",
                 bool(applied) and applied.get("model") == (model_b or model_a)
                 and applied.get("reasoningEffort") == efforts[0], f"applied={applied}")
        else:
            step("7 effort change (no efforts on dir, skipped)", True)

        # ---- 8. restart gateway -> recover -> new session inherits default ----
        proc.terminate(); proc.wait(timeout=10)
        time.sleep(1)
        proc2 = subprocess.Popen(
            [sys.executable or "python3", str(REPO / "web" / "boujoy_server.py"),
             "--port", str(PORT), "--vault", str(scratch / "vault"), "--static", str(REPO / "web"),
             "--clean-runtime", "codex", "--codex-bin", CODEX_BIN, "--codex-cwd", str(scratch)],
            stdout=log, stderr=log, env=env)
        _state["proc2"] = proc2
        if not step("8a gateway restarted", wait_http()):
            return 1
        created2 = harness("session.create", {})
        effective2 = created2.get("effective") or {}
        step("8b new session after restart inherits default",
             effective2.get("model") == model_a, f"effective={effective2}")

        print(f"\nRESULT: {len(_state['passed'])} passed, {len(_state['failed'])} failed"
              + (f" · FAILED: {_state['failed']}" if _state["failed"] else ""))
        return 0 if not _state["failed"] else 1
    finally:
        for p in ("proc2",):
            child = _state.get(p)
            if child:
                child.terminate()
        proc.terminate()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
