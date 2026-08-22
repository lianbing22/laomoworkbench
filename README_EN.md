<div align="center">

# Boujoy Harness

<p><strong>Local Agent Workbench · Mission Control Plane · Knowledge Base · Observable Runs</strong></p>

<p>
  <a href="README.md">简体中文</a> ·
  <a href="docs/mission-contract.md">Mission Contract</a> ·
  <a href="docs/provider-contract.md">Provider Contract</a> ·
  <a href="SECURITY.md">Security</a>
</p>

<p>Move Agent work from one-shot chat to a traceable, recoverable, reviewable local workflow.</p>

</div>

<p align="center">
  <img src="docs/screenshots/mission-loop-running.jpg" alt="Mission Loop in action" width="920">
</p>

> Boujoy Harness is a local-first Agent workbench. It is not a generic chat wrapper and not a hosted multi-tenant platform. It puts project context, expert methods, output styles, Mission plans, execution state, and run cost in one work surface.

## What this is

Boujoy Harness is for individual developers and small teams who use Agents repeatedly and need more than a chat transcript.

It currently brings together:

- Agent workbench: sessions, projects, model calls, task input, and output.
- Local knowledge base: Markdown project material, knowledge cards, and working context.
- Experts and styles: reusable calling roles and output constraints.
- Monitor: token usage, context, reasoning effort, trajectory, and run state.
- AI news and tools: a lightweight external information surface, not the core execution system.
- Mission control plane: split a plan into dependent Units, run them through Workers, and integrate the results.

## Core capabilities

| Capability | What it does today | Useful for |
| --- | --- | --- |
| Agent workbench | Local sessions, project context, model calls, task input, and output | Daily development and complex task execution |
| Knowledge | Markdown search, knowledge cards, and project context | Reusing existing project knowledge |
| Experts | Create, edit, duplicate, delete, and call expert profiles | Repeatable review, architecture, writing, or debugging methods |
| Styles | Create and switch output styles | Stable tone, structure, and formatting |
| Monitor | Token, context, reasoning effort, and run trajectory | Finding stalls and unexpected cost |
| Mission | plan.json, DAG, UnitRunner, Worktree, parallel scheduling, and integration recovery | Turning long work into traceable execution units |
| News | Manually refresh public RSS news | Following current AI tools and model changes |

## Page map

The left workbench is organized into six entries:

1. AGENT: the execution surface for sessions and tasks.
2. Knowledge: project material, cards, and working context.
3. Experts: callable roles and methods.
4. Styles: output tone, structure, and format constraints.
5. Monitor: run metrics and trajectories.
6. News: AI updates and tool entry points.

The engine status, theme switch, and settings entry in the top-right are global controls.

## Product screenshots

### Execution and monitoring

| Agent workbench | Monitor |
| --- | --- |
| ![Agent workbench](docs/screenshots/agent-page.jpg) | ![Monitor](docs/screenshots/monitor-page-dark.jpg) |

### Knowledge and information

| Knowledge base | AI news and tools |
| --- | --- |
| ![Knowledge base](docs/screenshots/knowledge-page.jpg) | ![AI news and tools](docs/screenshots/news-page.jpg) |

### Configuration and terminal states

- Mission blocked terminal state: [agent-mission-blocked.jpg](docs/screenshots/agent-mission-blocked.jpg)
- Provider list: [provider-list.jpg](docs/screenshots/provider-list.jpg)
- New provider form: [provider-form.jpg](docs/screenshots/provider-form.jpg)
- Monitor page in light theme: [monitor-page-light.jpg](docs/screenshots/monitor-page-light.jpg)
- Full UI iteration assets: [docs/screenshots/ui-refinement-1.0/](docs/screenshots/ui-refinement-1.0/)
- Existing demo animation: [docs/assets/harness-demo.gif](docs/assets/harness-demo.gif)

## Quick Start

### Requirements

- macOS, Linux, or Windows
- Python 3
- Git
- Codex installed and callable locally if you want to use the Codex clean runtime
- The basic local path does not require a frontend package manager or a database

### Start the local workbench

~~~bash
git clone https://github.com/lianbing22/laomoworkbench.git
cd laomoworkbench

python3 web/boujoy_server.py \
  --port 8766 \
  --vault vault \
  --static web \
  --clean-runtime dsh
~~~

Open:

~~~text
http://127.0.0.1:8766
~~~

`--vault` can point to any local Markdown knowledge-base directory. If it does not exist yet, create it first:

~~~bash
mkdir -p vault
~~~

### Use the Codex clean runtime

~~~bash
python3 web/boujoy_server.py \
  --port 8766 \
  --vault vault \
  --static web \
  --clean-runtime codex
~~~

`dsh` is the default local runtime. `codex` requires the local Codex runtime and its app-server capability. Both modes use the local gateway for page requests; Provider configuration is managed through the settings page and local configuration.

### Windows

See:

- [Windows usage guide](windows/README-Windows.zh-CN.md)
- [Windows release status](windows/WINDOWS-RELEASE-STATUS.md)

You can use `启动 Boujoy Harness.cmd` to start and `关闭 Boujoy Harness.cmd` to stop the workbench.

### LAN access

You can add a lightweight access gate with `--access-code`:

~~~bash
python3 web/boujoy_server.py \
  --port 8766 \
  --vault vault \
  --static web \
  --clean-runtime dsh \
  --access-code your-code
~~~

When omitted, the service is intended for local use. An access code is not a full multi-user identity system; do not expose the workbench directly to the public internet.

## Runtime modes

| Mode | Default | Use | Note |
| --- | --- | --- | --- |
| dsh | Yes | Default local execution path for quick start and development | Requires a usable local DSH runtime |
| codex | No | Execute through the Codex app-server adapter | Requires a local Codex runtime |

The server entry point is `web/boujoy_server.py`. The frontend is `web/index.html`, `web/app.js`, and `web/app.css`. The page does not require a separate frontend development server.

## Mission status

The current main line includes the P1.2 M0–M5 foundation:

| Milestone | Implemented | Status |
| --- | --- | --- |
| M0 | Mission package, models, storage, and base contracts | Complete |
| M1 | plan.json v2, Unit ids, dependencies, DAG validation, and dependency-aware scheduling | Complete |
| M2 | UnitRunner execution layer | Complete |
| M3 | Per-Unit Git worktrees and serial Integration merge | Complete |
| M4 | Dependency readiness, Lease, parallel Worker scheduling, hard cap of 4 workers | Complete |
| M4.1 | Per-jobId Condition mailbox to reduce polling and wake-up races | Complete |
| M5 | Write-ahead Integration records, crash recovery, and plan.json/Git reconciliation | Complete |

### Explicitly not complete

The README does not turn a roadmap item into a product claim:

- Merge conflicts can be detected, aborted, and surfaced as a blocked Mission; an automatic Conflict Resolver is not connected.
- The workbench currently has a single Mission control plane; a Multi-Mission Scheduler is not connected.
- Planner, Worker, and Evaluator Provider role routing is not yet a complete multi-role orchestration product.
- Full Vault, expert, and style context injection into the Codex clean runtime still needs to be closed out.
- Remote daemon, WebSocket, multi-user permissions, and production-grade audit are outside the current default delivery scope.
- Real live-runtime verification depends on the local environment; static tests passing is not the same as an end-to-end Mission run.

`blocked` is an explicit execution terminal state. It means the current task cannot satisfy its conditions and should remain visible for diagnosis.

## Architecture

~~~text
Browser
  |
  v
web/index.html + app.js + app.css
  |
  v
web/boujoy_server.py
  |-- local file and vault access
  |-- provider and clean-runtime adapters
  |-- mission API
  |
  v
web/mission/
  |-- models.py       plan and unit models
  |-- dag.py          dependency validation
  |-- store.py        plan persistence
  |-- unit_runner.py  one-unit execution
  |-- jobs.py         job lifecycle and mailbox
  |-- manager.py      scheduler and integration reconcile
  |-- worktree.py     isolated Git worktrees and recovery
  |-- verification.py result checks
~~~

Design principles:

- Local-first: the page, gateway, task state, and knowledge base default to local execution.
- Explainable: every Unit has state, dependencies, attempts, and results.
- Recoverable: Integration has write-ahead records and a reconcile path after interruption.
- Verifiable: plan validation, state transitions, worktrees, and run results are checked separately.
- Honest boundaries: remote orchestration, multi-user access, and automatic conflict resolution are not presented as implemented.

## Local data and security boundaries

- `vault` is a local knowledge-base directory for Markdown and material you allow the local Agent to read.
- Keep Provider configuration and credentials in local settings or environment variables. Never commit keys or put them in screenshots and logs.
- The AI News page requests the public RSS feeds listed by the server. It is not the project knowledge base and does not imply that every item has been fact-checked.
- `access-code` is only a lightweight entry gate, not full authentication, authorization, audit, or network isolation.
- With third-party model Providers, what leaves the machine depends on your model configuration and task input. Review the Provider data policy before sending sensitive material.
- See [SECURITY.md](SECURITY.md).

## Development and verification

Syntax check:

~~~bash
node --check web/app.js
~~~

Offline smoke test:

~~~bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
~~~

Git diff check:

~~~bash
git diff --check
~~~

For a real Mission runtime check, also verify:

1. The server starts successfully.
2. The page loads and reads local state.
3. A Unit moves from queued to running, completed, or blocked.
4. Worktree and Integration Git state matches plan.json.
5. The real Provider or clean runtime returns an identifiable result.

Do not claim a Mission run from HTTP 200, file existence, or static tests alone.

## Repository structure

~~~text
web/
  boujoy_server.py       local gateway
  index.html             page skeleton
  app.js                 frontend interactions and API calls
  app.css                visual system
  mission/               Mission control plane

docs/
  mission-contract.md    Mission contract
  provider-contract.md   Provider contract
  screenshots/           measured screenshots and UI iterations
  assets/                demo assets

tests/                    smoke, DAG, Provider, Codex, and Mission tests
macos/                    macOS app and portable runtime notes
windows/                  Windows startup and packaging scripts
assets/                   icons, fonts, and visual resources
~~~

## Roadmap

The next delivery priorities are:

1. Close the live-runtime verification and long-run recovery evidence for Mission.
2. Add a controlled conflict workflow before evaluating an automatic Conflict Resolver.
3. Connect Planner, Worker, and Evaluator role routing to the Provider contract.
4. Improve context injection and source markers for knowledge, experts, and styles.
5. Consider remote daemon and multi-user features only after authentication, permissions, audit, and deployment boundaries are explicit.

## FAQ

### Is this an online SaaS?

No. The default is a local workbench and the local gateway owns page requests and task state. Whether model calls go through a third-party Provider depends on your local configuration.

### Which runtime should I use?

Use the default dsh mode for a quick start. Specify clean-runtime codex when you need the Codex app-server adapter.

### Why does a Mission become blocked?

Blocked means dependencies, execution conditions, verification, or Integration conflicts prevent the Mission from continuing. Inspect the task detail and run trajectory instead of treating it as a generic crash.

### Is this a team collaboration platform?

Not yet. It is suitable for local individual and small-team workflows. Multi-user access, permissions, remote scheduling, and production audit are not default delivered capabilities.

## License

Project code follows [LICENSE](LICENSE). Fonts and third-party assets follow the license notices in their respective directories.
