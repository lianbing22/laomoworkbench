<div align="center">

# LAOMO WORKBENCH

<p><strong>Local-first Agent Workbench · Dual Runtime · Mission Orchestration · Fully Auditable</strong></p>

<p>
  <a href="README.md">简体中文</a> ·
  <a href="docs/mission-contract.md">Mission Contract</a> ·
  <a href="docs/provider-contract.md">Provider Contract</a> ·
  <a href="docs/codex-protocol-notes.md">Codex Protocol Notes</a> ·
  <a href="SECURITY.md">Security</a>
</p>

<p>Turn one-shot agent chats into a local execution workflow that is <strong>trackable, recoverable, verifiable, and replayable</strong>.</p>

</div>

<p align="center">
  <img src="docs/screenshots/mission-loop-running.jpg" alt="Mission Loop in action" width="920">
</p>

> Laomo Workbench is not a chat wrapper or a cloud platform. It runs on your own Mac: model credentials live in the Keychain, mission state lives on disk, Git integration never touches your checked-out branch, and every piece of execution evidence can be audited offline.

---

## Highlights

| Capability | What it does |
| --- | --- |
| **Dual runtime** | Knowledge mode (DSH, wired to your Markdown second brain) and Clean mode (Codex `app-server`, project-isolated) switch in one tap; mode and runtime are decoupled (`RuntimeManager`). |
| **Mission engine** | Objective → auto-planned multi-unit DAG → parallel workers (`maxParallelWorkers`) → unit evaluation → Git integration transactions → conflict resolution → machine verification → final evaluation → DONE. Durable and recoverable at every step. |
| **Permission levels** | Read-only / workspace-write (ask) / **full-auto (never-ask, sandboxed)** / danger-full-access. Mission turns default to the unattended contract. |
| **Work modes** | Chat / Plan (plan-only, forces read-only) / Auto (run to completion, forces full-auto). |
| **Multi-project registry** | Native folder picker (⌘O), rename/reorder/remove, sessions auto-grouped by directory, reveal in Finder. |
| **Provider management** | Built-in ChatGPT plus custom OpenAI-compatible endpoints; secrets only ever enter the macOS Keychain; connection testing; per-model reasoning effort. |
| **Observable runs** | Goal auto-drive, plan projection, token/context pressure, tool traces, background jobs with wait–wake (LAOMO_JOB protocol). |
| **Artifact preview** | `/api/preview?path=…` serves agent-built pages under a sandbox CSP — pasting an absolute path after the origin works too. |
| **Local services** | Vault search, expert/style libraries, AIHOT news aggregation — all cached locally. |

## Quick Start

Requirements: macOS (experimental Windows launchers included), Python 3.9+ (the system `/usr/bin/python3` works — zero third-party dependencies), [Codex CLI](https://github.com/openai/codex) ≥ 0.149 signed in via `codex login`, and optionally a DSH knowledge engine on port 3080.

```bash
git clone https://github.com/lianbing22/laomoworkbench.git
cd laomoworkbench

/usr/bin/python3 web/boujoy_server.py --port 8766 --vault vault --static web --clean-runtime codex
```

Open <http://127.0.0.1:8766/>. For knowledge mode, start DSH on 3080 first (`pnpm dsh web --host 127.0.0.1 --port 3080` from a deepseek-harness checkout); without it the workbench auto-falls back to clean mode.

First steps: pick a project folder (⌘O), open the composer's advanced panel and set permission to 全自动 (full-auto), chat directly for small tasks — or use Plan mode to draft, then Auto to execute. For long unattended chains, create a Mission (natural-language objective + acceptance criteria).

### How a Mission runs

The control plane plans 3–6 parallel units on a DAG (`maxParallelWorkers=2`), executes each in its own worktree branch `laomo/<mission_id>/u<N>`, integrates through the transaction branch `laomo/<mission_id>/integration` (**your checked-out branch is never touched**), resolves content conflicts in a dedicated Conflict Resolver turn (git writes forbidden for the model; the control plane concludes the merge), runs machine verification per check, and only reaches DONE after a fresh final evaluation passes. Evidence (diffs, results, event streams) lands under `.laomo/runs/<id>/`. Full contract: [docs/mission-contract.md](docs/mission-contract.md).

## Architecture

```
Browser (web/ static frontend)
   │  HTTP + WebSocket (same-origin, loopback)
   ▼
web/boujoy_server.py  local gateway (Python stdlib only)
   ├─ RuntimeManager          mode ⇄ runtime decoupling
   │    ├─ knowledge → DSH HTTP proxy (127.0.0.1:3080)
   │    └─ clean     → web/codex_adapter.py → codex app-server --stdio
   ├─ mission/       Mission control plane (plan/schedule/integrate/verify/recover)
   ├─ provider_profile.py    provider profiles + Keychain credentials
   └─ /api/preview   sandboxed artifact preview
```

On-disk state: mission runs under `<workspace>/.laomo/runs/<id>/`; host state (projects/settings/presets) under `~/Library/Application Support/Boujoy/BoujoyHarness/host-state.json`; provider profiles beside it, secrets only in the Keychain (service `laomo-workbench-provider`).

## API surface (loopback only)

- `GET /api/health` — gateway + dual-runtime health (DSH origins probed honestly)
- `POST /api/harness/{knowledge|clean}/<method>` — DSH-shaped RPC surface
- `POST /api/missions/{create|start|pause|resume|cancel|status|list}` — mission control plane
- `GET /api/preview?path=…` — sandboxed file preview with directory index
- `GET /api/providers…` — provider CRUD/activate/test

## Testing & gates

**Automated tests (CI-runnable, no real model needed):** 166 green — pytest is a dev dependency (`python3 -m pytest tests/ -q`); the workbench runtime itself uses only the Python standard library, zero third-party dependencies. **Real-runtime certification (manual / local, requires a real Codex login):** `scripts/gate_p12_driver.py`, `scripts/gate_p12_runtime_concurrency.py` — Gates 0/A–J plus the Usability acceptance run, all PASS: parallel workers, dependency barriers, conflict resolution, long-job wait–wake, pause/resume, cancel/interrupt, SIGKILL crash recovery, integration-WAL wedge recovery, machine-verify repair, final-evaluation ordering, evidence manifests.

## Status

P0/P0.5/P1/P1.1/P1.2 complete — verdict: LAOMO WORKBENCH — USABLE. Currently in the real-projects (dogfood) phase; Gate K stress and H2 follow once it stabilizes. Stage ledger, test counts, and per-gate results live in [docs/status.md](docs/status.md) — the single source of truth for status.

## Security model

Loopback-only listener; no CORS for arbitrary web origins. Remote (phone) access requires an access code. Secrets never leave the Keychain and are never echoed. Previewed pages run in a `Content-Security-Policy: sandbox` null origin and cannot reach the gateway API. Git isolation protects your checkout and honors `index.lock` ownership. Details: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). Rebranded and heavily rebuilt from Boujoy Harness; upstream acknowledgements in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
