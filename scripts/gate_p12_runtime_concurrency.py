#!/usr/bin/env python3
"""P1.2 Gate 0 — Runtime Concurrency Probe (real codex app-server).

Run:  python3 scripts/gate_p12_runtime_concurrency.py <scratch-dir>
      GATE_CODEX_BIN=<codex> optional override

Gate 0 answers ONE bottom question before any Mission-level gate:

    can a single CodexProcess (one `codex app-server --stdio` channel)
    carry two threads with simultaneously ACTIVE turns — with events
    multiplexed strictly per session and completion/interrupt isolated?

It deliberately bypasses run_turn(): the driver talks thread/start /
turn/start / turn/interrupt directly through adapter._ensure_process().rpc
and taps BOTH layers of evidence:

  * raw app-server notifications  (patched adapter._on_notification tap)
  * translated event-bus frames   (adapter.subscribe)

Sub-gates:
  0A  True Turn Overlap    A_start < B_end AND B_start < A_end, overlap >= 3s
                           (timestamps are the RAW turn/started and
                           turn/completed notifications, not wall guesses)
  0B  Event Isolation      no cross-session / cross-turn notification
  0C  Completion Isolation A completed => B kept producing events and
                           finished on its own
  0D  Interrupt Isolation  interrupt(threadA, turnA-real-id): only A stops;
                           B completes normally with its own marker
  0E  Stress               >= 10 rounds alternating both modes, 0 leaks

Evidence layout (<scratch>/.laomo/gates/p12-gate0/):
  summary.json  timeline.ndjson  stdout.log  round-NN/{A.json,B.json}

Verdict: GATE 0 PASS only if 0A..0D PASS and 0E is 10/10.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))

from codex_adapter import CodexRuntimeAdapter  # noqa: E402

TURN_WAIT_TIMEOUT = 240.0   # per turn
INTERRUPT_WAIT = 60.0
REQUIRED_OVERLAP = 3.0      # seconds of true active-turn overlap (0A)
STRESS_ROUNDS = 10          # 0E (rounds 3..12; round 1 = 0A/0B/0C, round 2 = 0D)


# ---------------------------------------------------------------- evidence


class Evidence:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeline = self.root / "timeline.ndjson"
        self.stdout = self.root / "stdout.log"
        self._stdout_fh = self.stdout.open("a", encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self._stdout_fh.write(line + "\n")
        self._stdout_fh.flush()

    def raw(self, rec: dict) -> None:
        with self.timeline.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def round_dir(self, n: int) -> Path:
        d = self.root / f"round-{n:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def close(self) -> None:
        self._stdout_fh.close()


# ---------------------------------------------------------------- probe core


def note_ids(note: dict) -> tuple[str, str, str]:
    """(threadId, turnId, itemId) from one raw notification params."""
    params = note.get("params") or {}
    turn = params.get("turn") or {}
    return (str(params.get("threadId") or ""),
            str(params.get("turnId") or turn.get("id") or ""),
            str(params.get("itemId") or params.get("callId") or ""))


class RoundProbe:
    """Raw-notification tap + per-round correlation for exactly two threads."""

    def __init__(self, ev: Evidence, round_no: int, adapter: CodexRuntimeAdapter,
                 raw_tap) -> None:
        self.ev = ev
        self.round_no = round_no
        self.adapter = adapter
        self.raw_tap = raw_tap
        self.lock = threading.Lock()
        self.threads: dict[str, dict] = {}          # label -> record
        self.notes: list[dict] = []                 # raw notes this round
        self.frames: dict[str, list[dict]] = {"A": [], "B": []}  # translated
        self.unsubscribe = adapter.subscribe(self._on_frame)

    def register(self, label: str, thread_id: str, cwd: Path) -> None:
        self.threads[label] = {"label": label, "threadId": thread_id,
                               "turnId": None, "cwd": str(cwd),
                               "startedAt": None, "endedAt": None,
                               "status": None, "events": 0,
                               "firstEventAt": None, "lastEventAt": None,
                               "text": "", "turnEndReason": None}

    # -- raw tap callback (called from the codex stdout reader thread) --
    def on_raw(self, note: dict) -> None:
        method = str(note.get("method") or "")
        tid, turn_id, item_id = note_ids(note)
        rec = {
            "ts": round(time.time(), 3), "round": self.round_no,
            "method": method, "threadId": tid, "turnId": turn_id,
            "itemId": item_id,
        }
        if method == "item/agentMessage/delta":
            rec["delta"] = (note.get("params") or {}).get("delta", "")[:80]
        self.ev.raw(rec)
        with self.lock:
            self.notes.append(rec)
            for label, t in self.threads.items():
                if tid and tid == t["threadId"]:
                    t["events"] += 1
                    now = rec["ts"]
                    if t["firstEventAt"] is None:
                        t["firstEventAt"] = now
                    t["lastEventAt"] = now
                    if method == "turn/started" and t["startedAt"] is None:
                        t["startedAt"] = now
                        if turn_id:
                            t["turnId"] = turn_id
                    if method == "turn/completed":
                        turn = (note.get("params") or {}).get("turn") or {}
                        t["endedAt"] = now
                        t["status"] = turn.get("status")
                    break

    # -- translated bus (assistant final text + turn/end reason) --
    def _on_frame(self, frame: dict) -> None:
        payload = frame.get("payload") or {}
        if payload.get("type") != "session/event":
            return
        sid = payload.get("sessionId")
        event = payload.get("event") or {}
        for label, t in self.threads.items():
            if sid == t["threadId"]:
                with self.lock:
                    self.frames[label].append(
                        {"ts": round(time.time(), 3), "type": event.get("type"),
                         "data": event.get("data")})
                    if event.get("type") == "assistant/message":
                        content = ((event.get("data") or {}).get("message")
                                   or {}).get("content") or []
                        texts = [b.get("text", "") for b in content
                                 if isinstance(b, dict)]
                        if texts:
                            t["text"] = "\n".join(x for x in texts if x)
                    if event.get("type") == "turn/end":
                        t["turnEndReason"] = (event.get("data") or {}).get("reason")
                break

    def wait_turn_done(self, label: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.threads[label]["endedAt"] is not None:
                    return True
            time.sleep(0.05)
        return False

    def wait_turn_started(self, label: str, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.threads[label]["startedAt"] is not None:
                    return True
            time.sleep(0.05)
        return False

    def snapshot(self, label: str) -> dict:
        with self.lock:
            return dict(self.threads[label])

    def close(self) -> None:
        self.unsubscribe()

    # -- checks ----------------------------------------------------------
    # session-CONTENT methods: these are multiplexed into per-session event
    # streams by EventTranslator and are what the isolation contract covers.
    # Ambient infrastructure broadcasts (mcpServer/*, remoteControl/*,
    # account/*, hook/*) carry no turn/item payload, are dropped by the
    # translator ("ignored notification"), and never enter any session
    # stream — they are counted for transparency but are not leaks.
    SESSION_SCOPED_PREFIXES = ("item/", "turn/", "thread/")

    def check_isolation(self) -> dict:
        """0B: every session-scoped notification belongs to exactly one known
        thread and never carries the OTHER thread's turn id or marker
        content."""
        bad_thread, bad_turn, cross_content, ambient_foreign = [], [], [], []
        with self.lock:
            ids = {t["threadId"]: lab for lab, t in self.threads.items()}
            turn_by_thread = {lab: t["turnId"] for lab, t in self.threads.items()}
            texts = {lab: t["text"] for lab, t in self.threads.items()}
            for n in self.notes:
                tid = n.get("threadId")
                scoped = str(n.get("method") or "").startswith(
                    self.SESSION_SCOPED_PREFIXES)
                if tid and tid not in ids:
                    if scoped:
                        bad_thread.append(n)
                    else:
                        ambient_foreign.append(n)
                    continue
                label = ids.get(tid)
                if label and n.get("turnId") and turn_by_thread.get(label):
                    if n["turnId"] != turn_by_thread[label]:
                        bad_turn.append(n)
        for lab, other in (("A", "B"), ("B", "A")):
            mine = texts.get(lab) or ""
            if f"GATE_{other}_DONE" in mine:
                cross_content.append(lab)
        return {"crossThread": len(bad_thread),
                "crossTurn": len(bad_turn),
                "crossContent": cross_content,
                "ambientForeign": len(ambient_foreign),
                "ok": not bad_thread and not bad_turn and not cross_content}


PROMPT = ("依次完成三步：1) 在当前目录创建文件 marker-{tag}.txt，内容为一行 "
          "GATE0-{tag}；2) 执行 shell 命令 sleep 8 并等待它结束；"
          "3) 最后只回复 GATE_{tag}_DONE（不要其它内容）。")


def start_turn(proc_rpc, thread_id: str, cwd: Path, tag: str) -> tuple[str, float]:
    params = {"threadId": thread_id, "cwd": str(cwd),
              "input": [{"type": "text", "text": PROMPT.format(tag=tag)}],
              "sandboxPolicy": {"type": "workspaceWrite"},
              "approvalPolicy": "never"}
    sent = time.time()
    res = proc_rpc.request("turn/start", params, timeout=30) or {}
    return str(res.get("turnId") or ""), sent


def run_round(ev: Evidence, adapter: CodexRuntimeAdapter, raw_tap, n: int,
              mode: str) -> dict:
    """One round: two concurrent turns; mode 'interrupt' interrupts A mid-flight."""
    rdir = ev.round_dir(n)
    cwd_a = rdir / "cwd-A"
    cwd_b = rdir / "cwd-B"
    for d in (cwd_a, cwd_b):
        d.mkdir(parents=True, exist_ok=True)
    rpc = adapter._ensure_process().rpc

    probe = RoundProbe(ev, n, adapter, raw_tap)
    raw_tap.attach(probe)
    try:
        res_a = rpc.request("thread/start", {"cwd": str(cwd_a), "ephemeral": True},
                            timeout=30) or {}
        res_b = rpc.request("thread/start", {"cwd": str(cwd_b), "ephemeral": True},
                            timeout=30) or {}
        tid_a = str(res_a.get("threadId") or res_a.get("id")
                    or (res_a.get("thread") or {}).get("id") or "")
        tid_b = str(res_b.get("threadId") or res_b.get("id")
                    or (res_b.get("thread") or {}).get("id") or "")
        assert tid_a and tid_b and tid_a != tid_b, f"thread/start 未返回两个 id: {res_a} {res_b}"
        probe.register("A", tid_a, cwd_a)
        probe.register("B", tid_b, cwd_b)

        turn_a, sent_a = start_turn(rpc, tid_a, cwd_a, "A")
        turn_b, sent_b = start_turn(rpc, tid_b, cwd_b, "B")
        ev.log(f"round {n}: turns started A={turn_a[:8]} B={turn_b[:8]}")

        interrupted = False
        if mode == "interrupt":
            ok_start = probe.wait_turn_started("A") and probe.wait_turn_started("B")
            time.sleep(1.0)  # both provably active before the interrupt lands
            real_turn = probe.snapshot("A")["turnId"] or turn_a
            rpc.request("turn/interrupt", {"threadId": tid_a, "turnId": real_turn},
                        timeout=15)
            interrupted = True
            ev.log(f"round {n}: interrupted A with real turnId {real_turn[:8]}")

        done_a = probe.wait_turn_done("A", TURN_WAIT_TIMEOUT)
        done_b = probe.wait_turn_done("B", TURN_WAIT_TIMEOUT)
        a, b = probe.snapshot("A"), probe.snapshot("B")

        for label, snap in (("A", a), ("B", b)):
            marker = Path(snap["cwd"]) / f"marker-{label}.txt"
            out = {**snap, "completed": label == "A" and done_a or label == "B" and done_b,
                   "markerFile": marker.is_file(),
                   "markerText": marker.read_text("utf-8").strip() if marker.is_file() else None}
            (rdir / f"{label}.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=1), "utf-8")

        overlap = None
        if a["startedAt"] is not None and b["startedAt"] is not None \
                and a["endedAt"] is not None and b["endedAt"] is not None:
            overlap = round(min(a["endedAt"], b["endedAt"])
                            - max(a["startedAt"], b["startedAt"]), 3)

        iso = probe.check_isolation()

        def natural(label: str, snap: dict, done: bool) -> bool:
            marker = Path(snap["cwd"]) / f"marker-{label}.txt"
            return (done and snap["status"] == "completed" and marker.is_file()
                    and f"GATE_{label}_DONE" in (snap["text"] or ""))

        # 0C (symmetric): the FIRST completion must not disturb the other —
        # both turns complete NATURALLY (own marker + own DONE text, no
        # interrupt) while provably overlapping. Which one finishes first
        # is a race, not a contract.
        later_last = max(a["lastEventAt"] or 0, b["lastEventAt"] or 0)
        earlier_end = min(a["endedAt"] or float("inf"), b["endedAt"] or float("inf"))
        events_after_a = (later_last > earlier_end
                          and natural("A", a, done_a) and natural("B", b, done_b)
                          and (overlap or 0) > 0)

        result = {
            "round": n, "mode": mode,
            "A": {k: a[k] for k in ("threadId", "turnId", "startedAt", "endedAt",
                                    "status", "events", "text")},
            "B": {k: b[k] for k in ("threadId", "turnId", "startedAt", "endedAt",
                                    "status", "events", "text")},
            "overlap": overlap,
            "markerA": (cwd_a / "marker-A.txt").is_file(),
            "markerB": (cwd_b / "marker-B.txt").is_file(),
            "isolation": iso,
            "eventsAfterAEnd": events_after_a,
            "doneA": done_a, "doneB": done_b,
            "interrupted": interrupted,
        }
        checks = []
        if mode == "concurrent":
            checks = [
                ("overlap", overlap is not None and overlap >= REQUIRED_OVERLAP),
                ("markers", result["markerA"] and result["markerB"]),
                ("texts", "GATE_A_DONE" in (a["text"] or "")
                 and "GATE_B_DONE" in (b["text"] or "")),
                ("isolation", iso["ok"]),
                ("completion-isolation", events_after_a and done_b),
            ]
        else:  # interrupt
            a_interrupted = (a["status"] == "interrupted"
                             or bool((a.get("turnEndReason") or {}).get("interrupted"))
                             or (a["status"] is None and not result["markerA"]))
            checks = [
                ("A-interrupted", a_interrupted),
                ("B-unaffected", done_b and result["markerB"]
                 and "GATE_B_DONE" in (b["text"] or "")
                 and b["status"] != "interrupted"),
                ("isolation", iso["ok"]),
            ]
        result["checks"] = {name: bool(ok) for name, ok in checks}
        result["ok"] = all(ok for _, ok in checks)
        ev.log(f"round {n} ({mode}): {'OK' if result['ok'] else 'FAIL'} "
               f"overlap={overlap}s checks={result['checks']}")
        return result
    finally:
        raw_tap.detach()
        probe.close()
        for tid in list(probe.threads.values()):
            try:
                rpc.request("thread/delete", {"threadId": tid["threadId"]}, timeout=10)
            except Exception:
                pass


class RawTap:
    """Route raw app-server notifications to the active RoundProbe without
    touching codex_adapter code: patch the adapter INSTANCE before the first
    _ensure_process (CodexProcess binds on_notification at construction)."""

    def __init__(self, ev: Evidence, adapter: CodexRuntimeAdapter) -> None:
        self.ev = ev
        self.probe: RoundProbe | None = None
        original = adapter._on_notification

        def wrapped(note: dict) -> None:
            if self.probe is not None:
                try:
                    self.probe.on_raw(note)
                except Exception as exc:  # never break the read loop
                    ev.log(f"raw tap error: {exc}")
            original(note)

        adapter._on_notification = wrapped
        self._installed = wrapped

    def attach(self, probe: RoundProbe) -> None:
        self.probe = probe

    def detach(self) -> None:
        self.probe = None


# ---------------------------------------------------------------- main


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    scratch = Path(sys.argv[1]).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    ev = Evidence(scratch / ".laomo" / "gates" / "p12-gate0")
    ev.log(f"P1.2 Gate 0 — Runtime Concurrency Probe (scratch={scratch})")

    bin_path = os.environ.get("GATE_CODEX_BIN") or shutil.which("codex") \
        or os.path.expanduser("~/.local/bin/codex")
    version = subprocess.run([bin_path, "--version"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    ev.log(f"codex: {bin_path} ({version})")

    adapter = CodexRuntimeAdapter(bin_path=bin_path, default_cwd=str(scratch),
                                  debug_log=ev.log)
    tap = RawTap(ev, adapter)
    results: list[dict] = []
    try:
        proc = adapter._ensure_process()
        pid = proc.proc.pid if proc.proc else -1
        ev.log(f"app-server ready (pid={pid})")

        results.append(run_round(ev, adapter, tap, 1, "concurrent"))     # 0A/0B/0C
        results.append(run_round(ev, adapter, tap, 2, "interrupt"))      # 0D
        for i in range(STRESS_ROUNDS):                                    # 0E
            mode = "concurrent" if i % 2 == 0 else "interrupt"
            results.append(run_round(ev, adapter, tap, 3 + i, mode))
    finally:
        adapter.shutdown()
        ev.close()

    r1, r2 = results[0], results[1]
    stress = results[2:]
    overlap = r1["overlap"]
    max_active = 2 if (overlap or 0) >= REQUIRED_OVERLAP else (
        2 if any(r["overlap"] and r["overlap"] >= 1 for r in results) else 1)

    gate_a = bool(r1["checks"].get("overlap"))
    gate_b = all(r["isolation"]["ok"] for r in results)
    gate_c = bool(r1["checks"].get("completion-isolation"))
    gate_d = r2["ok"]
    gate_e_pass = sum(1 for r in stress if r["ok"])
    verdict = gate_a and gate_b and gate_c and gate_d and gate_e_pass == len(stress)

    a, b = r1["A"], r1["B"]
    summary = {
        "codexVersion": version, "appServerPid": pid,
        "rounds": [{k: r[k] for k in ("round", "mode", "overlap", "ok", "checks")}
                   for r in results],
        "gates": {
            "0A": {"pass": gate_a, "A": a, "B": b, "overlap": overlap,
                   "maxSimultaneousActiveTurns": max_active},
            "0B": {"pass": gate_b,
                   "crossSessionEvents": sum(r["isolation"]["crossThread"] for r in results),
                   "crossTurnEvents": sum(r["isolation"]["crossTurn"] for r in results),
                   "ambientForeignBroadcasts": sum(r["isolation"]["ambientForeign"] for r in results)},
            "0C": {"pass": gate_c, "eventsAfterAEnd": r1["eventsAfterAEnd"]},
            "0D": {"pass": gate_d, "interrupted": r2["A"],
                   "unaffected": r2["B"]},
            "0E": {"pass": gate_e_pass == len(stress),
                   "passed": gate_e_pass, "total": len(stress),
                   "flakes": [r["round"] for r in stress if not r["ok"]]},
        },
        "verdict": "PASS" if verdict else "FAIL",
    }
    out = ev.root / "summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")

    print(f"""
P1.2 Gate 0 — Runtime Concurrency Probe

Codex version: {version}
App-server PID: {pid}

0A True Turn Overlap:       {'PASS' if gate_a else 'FAIL'}
A threadId: {a['threadId']}
A turnId: {a['turnId']}
A start: {a['startedAt']}
A end: {a['endedAt']}

B threadId: {b['threadId']}
B turnId: {b['turnId']}
B start: {b['startedAt']}
B end: {b['endedAt']}

Overlap: {overlap}s (required >= {REQUIRED_OVERLAP}s)
Max simultaneous active turns: {max_active}

0B Event Isolation:         {'PASS' if gate_b else 'FAIL'}
Cross-session events: {summary['gates']['0B']['crossSessionEvents']}
Cross-turn events: {summary['gates']['0B']['crossTurnEvents']}
(ambient infra broadcasts w/ foreign threadId, not session content:
 {summary['gates']['0B']['ambientForeignBroadcasts']})

0C Completion Isolation:    {'PASS' if gate_c else 'FAIL'}
(first completion leaves the other running; both natural: {r1['eventsAfterAEnd']})

0D Interrupt Isolation:     {'PASS' if gate_d else 'FAIL'}
Interrupted: A status={r2['A']['status']} turnEndReason={r1['A'].get('status') and r2['A'].get('status')}
Unaffected turn result: B status={r2['B']['status']} text_has_marker={'GATE_B_DONE' in (r2['B']['text'] or '')}

0E Stress {len(stress)} rounds:        {gate_e_pass}/{len(stress)}
Flakes: {summary['gates']['0E']['flakes'] or 'none'}

Verdict:
GATE 0 {'PASS' if verdict else 'FAIL'}

Evidence: {ev.root}
Summary: {out}
""")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
