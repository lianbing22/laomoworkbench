<div align="center">

# LaoMo Workbench

## 老墨工作台

**Pull the agent out of the chat box and into your local workspace.**

**A local-first AI coding harness: the agent doesn't just answer — it plans, executes, waits, verifies, and repairs until the job is actually done.**

Codex app-server driven · Durable Mission · Provider Profiles · Machine Verification · Local-first

[简体中文](README.md) · Derived from [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness)

</div>

<p align="center">
  <img src="docs/screenshots/mission-loop-running.jpg" alt="Mission Loop in action" width="900">
  <br>
  <sub>Mission running: goal → auto-planned units → worker execution → independent acceptance → repair loop → verified DONE.
  An animated hero GIF is being recorded; storyboard and asset rules live in <a href="docs/screenshots/RECORDING.md">docs/screenshots/RECORDING.md</a>.</sub>
</p>

## What it is

LaoMo Workbench is an AI coding harness and long-running mission console that runs on your own machine. It hosts no models and stores no credentials in the cloud: the agent runtime (by default Codex `app-server`, with model services configured through Provider Profiles) runs locally, and your working context and run evidence stay in your own folders.

Four things separate it from "yet another Codex GUI":

### ① Durable Mission — answering isn't finishing

```text
give it a goal
   ↓
auto-planning (units, each with acceptance criteria)
   ↓
worker execution (commands over 20s run as background jobs, WAITING → auto WAKE)
   ↓
independent unit evaluator
   ↓ NEEDS_WORK
repair loop (repair cap / no-progress detection / four-bucket time budget)
   ↓
machine verification (every check evidenced)
   ↓
fresh final evaluator re-runs every acceptance criterion
   ↓
DONE + evidence manifest
```

In one sentence: **the harness keeps driving the task until it passes acceptance — resumable across pauses, crashes, and reboots.**

### ② Codex Runtime — not a CLI wrapper

The control plane speaks the **Codex `app-server` (stdio) protocol** directly — Thread / Turn / Tool / Approval. Model selection, reasoning effort, sandbox permissions, and steer/interrupt are all protocol-level implementations, not shell string plumbing.

### ③ Provider Profiles — no hand-edited TOML

Manage model services from settings: the built-in ChatGPT/Codex service (uses your local Codex login), or any custom Responses-compatible service (base URL / API key / model list / default model / reasoning effort). Test the connection and switch instantly. API keys never leave your machine's secure storage.

### ④ Verification — the agent cannot declare itself done

```text
worker says DONE
      ✕
      ▼
unit evaluator (separate session judges acceptance)
      ▼
machine verification (command / requiredFiles / httpChecks, each evidenced)
      ▼
fresh final evaluator (brand-new context re-runs every criterion)
      ▼
     DONE (+ immutable evidence manifest: path/sha256)
```

Failed acceptance means `NEEDS_WORK` and a repair loop; contradictory acceptance criteria stop the mission in an honest `blocked` terminal state instead of faking completion.

## Product screenshots

| Agent workbench | Monitor (dark) |
| --- | --- |
| ![Agent workbench](docs/screenshots/agent-page.jpg) | ![Monitor](docs/screenshots/monitor-page-dark.jpg) |
| **Knowledge** | **AI News** |
| ![Knowledge](docs/screenshots/knowledge-page.jpg) | ![AI News](docs/screenshots/news-page.jpg) |

More real screenshots:

- **Mission blocked terminal state** (`blocked` is not failure — it's an honest "cannot be satisfied" verdict): [agent-mission-blocked.jpg](docs/screenshots/agent-mission-blocked.jpg)
- **Provider list / new-provider form**: [provider-list.jpg](docs/screenshots/provider-list.jpg) · [provider-form.jpg](docs/screenshots/provider-form.jpg)
- Monitor page in light theme: [monitor-page-light.jpg](docs/screenshots/monitor-page-light.jpg)

## Architecture

```text
        Workbench UI (sessions · plan · tool activity · monitor · news)
                          │  HTTP/WebSocket · 127.0.0.1
        local gateway web/boujoy_server.py
          ├─ RuntimeManager ────── Codex app-server --stdio (clean mode, default)
          │                     └─ DeepSeek Harness (knowledge mode)
          ├─ Mission control plane ── Planner / Worker / Evaluator / Verification / JobWatcher
          │                    (web/mission/ package; state & evidence under <workspace>/.laomo/runs/)
          └─ Provider Profiles ─ model service center (keys in OS keychain)
                          │
              local workspace + Markdown vault
              (git · shell · files · directories you own)
```

Once P1.2 lands, the mission control plane gains parallel worker scheduling and integration/conflict paths (see roadmap; **nothing goes in this diagram before it exists in code**).

## Quick start

### Simplest: clean mode + Codex

Prerequisites: Python 3 and the [Codex CLI](https://github.com/openai/codex) installed and logged in.

```bash
git clone https://github.com/lianbing22/laomoworkbench.git
cd laomoworkbench

mkdir -p vault

python3 web/boujoy_server.py \
  --port 8766 \
  --vault vault \
  --static web \
  --clean-runtime codex

# open http://127.0.0.1:8766/
```

First run: pick a workspace on the left → describe a task in the composer, or click "set goal" and check **run as Mission**, then watch it plan, execute, verify, and repair its way to DONE.

### Advanced setup

<details>
<summary><b>Knowledge mode (DeepSeek Harness + vault)</b></summary>

Requires a locally built DeepSeek Harness (an executable `node_modules/.bin/dsh`) and a local Markdown vault:

```bash
export BOUJOY_DSH_ROOT="$HOME/src/deepseek-harness"
export BOUJOY_VAULT_DIR="$HOME/BoujoyVault"
export BOUJOY_PYTHON_BIN="$(command -v python3)"

python3 web/boujoy_server.py --port 8766 --vault vault --static web
```

Knowledge mode serves context on demand via indexing and relevant cards; it never dumps the whole vault into the model.
</details>

<details>
<summary><b>macOS native shell</b></summary>

macOS 13+ Apple Silicon; replaces the browser with a WKWebView host:

```bash
./macos/build-app.command --install
```

For portable packages, launch via the launcher script rather than double-clicking the app.
</details>

<details>
<summary><b>Windows browser host (Beta)</b></summary>

Windows 10/11 x64: see the scripts and docs in [windows/](windows/).
</details>

## How the Mission Loop works

1. **Plan** — the planner splits the goal into units, each carrying its own acceptance criteria; implicit requirements get promoted into their own units.
2. **Execute** — each unit runs in a worker (a Codex session); commands expected to exceed 20 seconds are handed to the gateway via the `LAOMO_JOB` protocol, the card shows WAITING, and the job's exit (or overdue deadline) wakes the mission.
3. **Accept** — a unit evaluator judges PASS / NEEDS_WORK / BLOCKED against the criteria from a separate context.
4. **Repair** — NEEDS_WORK enters a repair loop bounded by four circuit breakers: repair cap, no-progress signature, max cycles, and wall-clock budget (four-bucket time accounting: active + waiting = elapsed, paused time tracked separately).
5. **Final gate** — once all units pass, the machine verification gate runs command / requiredFile / httpChecks items and writes results to disk; then a **fresh final evaluator** re-runs every acceptance criterion in a brand-new context.
6. **DONE** — requires all three: all units passed + machine verification passed + final evaluator passed; an immutable evidence manifest (path + sha256) is written.
7. **Honest terminal states** — logically unsatisfiable criteria stop at `blocked`; after a crash the mission recovers by process identity with PID-reuse detection and never mistakes another process for its own.

Run records (plan / verdicts / checkpoints / events / evidence) land under `<workspace>/.laomo/runs/<mission-id>/`, auditable with any editor.

## Clean mode vs. knowledge mode

| Mode | Best for | What it never does |
| --- | --- | --- |
| Clean (default) | Coding tasks, ad-hoc questions, experiments | Never reads your Markdown vault; driven by Codex app-server. |
| Knowledge | Tasks that reuse project background, docs, prompts, past decisions | Never dumps the whole vault into the model. |

No personal vault ships with the source; start from an empty directory.

## Roadmap

- **P0 shipped** — runtime decoupling: clean mode driven by Codex app-server (12+2 live acceptance checks passed); knowledge mode stays on DeepSeek Harness.
- **P0.5 shipped** — Provider Profiles: model service center (base URL / API key / model catalog / reasoning effort, switchable independently of the runtime).
- **P1 shipped** — Durable Mission: multi-unit auto planning, background-job lifecycle, acceptance/repair loop (8 gates run for real).
- **P1.1 shipped** — Reliability & Hard Verification: `blocked` terminal state, waiting pause/resume, crash recovery (process identity + PID reuse detection), four-bucket time accounting, machine verification gate, three-condition DONE, immutable evidence manifest; live Gate A–E all PASS (`scripts/gate_p11_driver.py`).
- **P1.2 in progress** — Parallel Mission Execution. Merged so far (M0–M3): `web/mission/` package refactor, plan.json v2 (unit ids / dependencies / DAG validation + dependency-aware scheduling), UnitRunner extraction, WorktreeManager (per-unit git worktrees + serial integration). In flight: parallel worker scheduling and IntegrationManager / ConflictResolver conflict paths — **this README will only advertise them once they exist**.
- **Next** — P1.3 Multi-Mission Scheduler → P1.4 Provider Role Routing (different models for Planner/Worker/Evaluator) → P1.5 Vault/Knowledge Context Layer.

## FAQ

**Why does it say runtime components are missing?** Verify the agent runtime, vault, and Python paths exist on this machine. For portable packages, launch via the launcher script, not by double-clicking the app.

**Why does startup hang for a while?** First runs wait on gateway/runtime health checks rather than blindly loading. On failure, check the local runtime, Python, and provider config.

**Why is the agent not replying?** This project hosts no model balance or API keys. Check the model provider, balance, network, and permissions from your agent runtime.

**Does a broken knowledge preview break chat?** No. The knowledge preview is optional; the main agent UI keeps working without it.

**Why does the mission card show "blocked"?** By design: when the evaluator decides the acceptance criteria cannot be met, the mission enters the `blocked` terminal state with reasons recorded in checkpoints and events — far more valuable than a fake DONE.

**Is this an official DeepSeek or OpenAI product?** No. LaoMo Workbench is an independent, unofficial open-source layer.

## Privacy & network boundary

- Vault contents, session state, credentials, and mission evidence stay on your machine; none of it is in this repository.
- Without an access code the gateway binds 127.0.0.1 only; LAN access requires an explicitly configured PIN.
- API keys live in your machine's secure storage (macOS keychain), never in config files or git.
- The AI news page fetches the public RSS feeds listed in `web/boujoy_server.py`; no analytics, no telemetry.
- Never commit personal vaults, session logs, credentials, or platform runtimes.

See [SECURITY.md](SECURITY.md) for details.

## Verification & development

A static smoke test (16 checks) runs without any model account:

~~~bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
~~~

With a running local instance (including an agent runtime):

~~~bash
python3 tests/smoke_test.py --live-origin http://127.0.0.1:8766
~~~

Module regressions cover the runtime adapter, providers, the mission engine, DAG scheduling, and worktrees (FakeAdapter-driven, no model account needed):

~~~bash
for t in codex_adapter_test provider_test mission_test dag_test worktree_test; do
  PYTHONDONTWRITEBYTECODE=1 python3 "tests/$t.py"
done
~~~

The P1.1 live-gate driver (needs a real Codex login; runs on dedicated ports — never against your daily instance): `scripts/gate_p11_driver.py`.

## Repository layout

~~~text
macos/      native WKWebView host and build scripts
web/        local gateway, web UI, and assets; web/mission/ is the mission control-plane package
windows/    Windows browser-host Beta scripts and docs
tests/      runtime / provider / mission / DAG / worktree regression tests and smoke test
scripts/    P1.1 live Codex gate driver (Gates A–E)
docs/       contracts & protocol notes (mission-contract.md / provider-contract.md) plus docs/screenshots/
assets/     icons and visual assets
~~~

## License & credits

This repository derives from [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness) (MIT License) and follows its license terms; the upstream itself builds on [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). Font attribution and third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

"LaoMo Workbench / 老墨工作台" is this project's own brand, unaffiliated with and not endorsed by DeepSeek AI or OpenAI.
