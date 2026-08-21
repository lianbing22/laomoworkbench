<div align="center">

# LaoMo Workbench

## 老墨工作台

**Pull the agent out of the chat box and into your local workspace.**

A local-first agent workbench: sessions, tasks, knowledge, and run signals — your files, your machine.

[简体中文](README.md) · Derived from [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness)

</div>

<p align="center">
  <img src="docs/assets/harness-demo.gif" alt="LaoMo Workbench UI demo" width="900">
</p>

## What it is

LaoMo Workbench is an agent workbench that runs on your own machine. It hosts no models and stores no credentials in the cloud: the agent runtime (by default [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)) runs locally, and your working context stays in your own folders.

One-line division of labor:

- **Agent runtime** makes the agent act (model calls, tools, event streams).
- **LaoMo Workbench** gives those actions a workspace, a visual surface, and a recoverable desktop experience.
- **Markdown vault** keeps reusable context in files you own.

~~~text
your one-line task
        │
        ▼
LaoMo UI ── local gateway ── agent runtime ── your models / tools
        │
        └────────────── local Markdown vault
                       projects · knowledge · prompts · content
~~~

## Core capabilities

| Capability | Notes |
| --- | --- |
| Local agent workbench | Sessions, run signals, tool activity, and approval interactions, all in a local UI. |
| Markdown workspace | Projects, knowledge, prompts, and content live in folders you own, not a cloud database. |
| Long-conversation usability | Paged history, streaming projection separated from user scroll — no jumpiness during long generations. |
| Task & interrupt handling | Queued confirmations/inputs/approvals; stale responses settle automatically, dialogs never stick. |
| Local first | Loopback-only binding without an access code; optional PIN-protected LAN access for phone pairing; no telemetry. |
| Two modes | Knowledge mode connects your personal vault; clean mode runs the agent without touching work data. |
| Cross-platform | macOS 13+ Apple Silicon native host; Windows 10/11 x64 browser host (Beta). |

## Building from source (macOS)

Prerequisites: macOS 13+ Apple Silicon, Python 3, a separately built DeepSeek Harness (an executable `node_modules/.bin/dsh`), and a local Markdown vault directory.

~~~bash
git clone https://github.com/lianbing22/laomoworkbench.git
cd laomoworkbench

# Point at your own local dependencies; never commit these values.
export BOUJOY_DSH_ROOT="$HOME/src/deepseek-harness"
export BOUJOY_VAULT_DIR="$HOME/BoujoyVault"
export BOUJOY_PYTHON_BIN="$(command -v python3)"

./macos/build-app.command --install
~~~

To skip the native shell and run the web edition directly (clean mode works out of the box):

~~~bash
mkdir -p vault
python3 web/boujoy_server.py --port 8766 --vault vault --static web
# open http://127.0.0.1:8766/
~~~

First run: pick or create a workspace on the left → connect your Markdown vault (skip in clean mode) → describe a task in the composer. Models, providers, and tool permissions are decided by the agent runtime you configure.

## Knowledge mode vs. clean mode

| Mode | Best for | What it never does |
| --- | --- | --- |
| Knowledge | Tasks that reuse project background, docs, prompts, or past decisions | Never dumps the whole vault into the model; context is served via indexing and relevant cards. |
| Clean | Ad-hoc questions, experiments, anything unrelated to your work data | Never reads your Markdown vault. |

No personal vault ships with the source; start from an empty directory.

## Roadmap

The current architecture is `UI → local gateway → agent runtime`. Ongoing work abstracts the runtime layer into pluggable adapters, with OpenAI Codex `app-server` as the first candidate:

~~~text
             LaoMo Workbench
                    │
        ┌───────────┴───────────┐
  control plane             workbench UI
        │
  RuntimeAdapter
        │
  ┌─────┼─────────────┐
  │     │             │
Codex  DeepSeek     Claude/GLM
~~~

Mode (knowledge/clean) and runtime become decoupled config: clean defaults to Codex, knowledge stays on DeepSeek Harness for now, with one flag to switch back.

## FAQ

**Why does it say runtime components are missing?** Verify the agent runtime, vault, and Python paths exist on this machine. For portable packages, launch via the launcher script, not by double-clicking the app.

**Why does startup hang for a while?** First runs wait on gateway/runtime health checks rather than blindly loading. On failure, check the local runtime, Python, and provider config.

**Why is the agent not replying?** This project hosts no model balance or API keys. Check the model provider, balance, network, and permissions from your agent runtime.

**Does a broken knowledge preview break chat?** No. The knowledge preview is optional; the main agent UI keeps working without it.

**Is this an official DeepSeek or OpenAI product?** No. LaoMo Workbench is an independent, unofficial open-source layer.

## Privacy & network boundary

- Vault contents, session state, and credentials stay on your machine; none of it is in this repository.
- Without an access code the gateway binds 127.0.0.1 only; LAN access requires an explicitly configured PIN.
- The AI news page fetches the public RSS feeds listed in `web/boujoy_server.py`; no analytics, no telemetry.
- Never commit personal vaults, session logs, credentials, or platform runtimes.

See [SECURITY.md](SECURITY.md) for details.

## Verification & development

A static smoke test runs without any model account:

~~~bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
~~~

With a running local instance (including an agent runtime):

~~~bash
python3 tests/smoke_test.py --live-origin http://127.0.0.1:8766
~~~

## Repository layout

~~~text
macos/      native WKWebView host and build scripts
web/        local gateway, web UI, and assets
windows/    Windows browser-host Beta scripts and docs
tests/      model-free smoke tests
assets/     icons, font attribution, and visual assets
~~~

## License & credits

This repository derives from [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness) (MIT License) and follows its license terms; the upstream itself builds on [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). Font attribution and third-party notices: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

"LaoMo Workbench / 老墨工作台" is this project's own brand, unaffiliated with and not endorsed by DeepSeek AI or OpenAI.
