# Security Policy

## Reporting a vulnerability

Do not disclose API keys, access codes, vault content, screenshots containing
private data, or potential vulnerabilities in a public issue.

Report privately via the contact channel listed on the repository profile.
Include a minimal reproduction, the affected version (commit), and any
mitigation already tested. The maintainer acknowledges reports and coordinates
a fix before public disclosure where practical.

## What this product is

LAOMO WORKBENCH is a **local-first agent workbench for a trusted macOS
account**. It runs agents with real shell and filesystem access on your
machine, stores model credentials in your Keychain, and drives git in your
projects. The security model is built for that deployment; it is **not** a
multi-tenant or internet-facing service and must not be exposed to untrusted
networks or shared machines.

## Trust boundaries

```
Browser (your UI session)
   │  HTTP + WebSocket, loopback origin
Local Gateway (web/boujoy_server.py)
   │  ┌─ LAOMO Control Plane (mission engine, machine verification)
   │  └─ Runtime Adapter ──► Codex app-server ──► Model provider
   ▼
Shell + Filesystem + Git (your projects)
```

- **Browser → Gateway**: same-origin, loopback by default. Arbitrary web
  origins receive no CORS headers — a malicious page cannot read your
  knowledge base or drive write APIs from your browser.
- **Gateway → Runtime/Model**: provider credentials are attached server-side;
  the browser never sees keys.
- **Runtime → System**: an agent turn executes with the sandbox level you
  granted (below). Everything the agent produces is recorded as evidence.

## Permission levels (what an agent turn can do)

| Level | Sandbox | Approval | Meaning |
| --- | --- | --- | --- |
| `read-only` | readOnly | ask | Inspect only; no writes anywhere |
| `workspace-write` | workspaceWrite | ask (on-request) | Writes confined to the workspace; approval prompts for commands |
| `full-auto` | workspaceWrite | **never** | Same workspace sandbox, **no approval prompts** — built for unattended missions and Auto mode |
| `danger-full-access` | dangerFullAccess | never | No sandbox. Full user-level access: any file, any command |

**`full-auto` is not a "safe mode".** It keeps the *workspace sandbox* but
removes the human from the loop: any command that does not require elevated
permissions runs without asking. Use it with the same care as any autonomous
execution, and prefer missions (bounded by StopPolicy + machine verification +
evidence) over raw unattended turns.

`danger-full-access` grants the agent everything your user account can do,
including writes outside the workspace and network operations. It exists for
explicit, supervised cases only.

Missions always run worker turns unattended; their blast radius is bounded by
worktree isolation (per-unit branches, never your checked-out branch), the
machine verification gate, and per-mission stop budgets.

## Key controls

- **Listener**: loopback (`127.0.0.1`) only. Binding to `0.0.0.0` happens
  solely when you pass `--access-code` (LAN phone view); that code is a
  shared-secret gate, **not** authentication of a user account, and the LAN
  view should not be exposed to untrusted networks.
- **Credentials**: provider API keys live in the macOS Keychain (service
  `laomo-workbench-provider`). If the Keychain is unavailable the secret is
  session-only (process memory, never written to disk) and the UI says so.
  APIs never echo key material back.
- **Artifact preview** (`/api/preview?path=…` and pasted absolute paths):
  serves files **inside the workbench working directory only** (resolved-path
  confinement), with `Content-Security-Policy: sandbox` — a generated page
  renders and its scripts run in a **null origin** that cannot reach the
  gateway API, your knowledge base, or any other origin.
- **Git isolation**: mission units work in dedicated worktrees
  (`laomo/<mission_id>/u<N>`) and integration lands on a mission integration
  branch; your checked-out branch is never touched, and `index.lock`
  ownership is respected (only the mission's own stale locks are reported).
- **Mission jobs**: background jobs are spawned by the control plane in their
  own session; process identity (pid + start time + process group) is
  verified before attach/kill so a recycled PID is never signaled. A
  cancelled mission leaves no live managed jobs.
- **Crash consistency**: mission state transitions are atomic writes;
  terminal missions never resurrect; evidence manifests are immutable after
  DONE.

## What is out of scope / known limits

- No sandbox survives `danger-full-access`; treat agent output as
  user-privileged code.
- The access code protects the LAN phone view only; it is not TLS, not an
  account system, and not rate-limited authentication. Loopback use is the
  default posture.
- Preview confinement is scoped to the gateway working directory; anything
  outside it is refused (403).
- The optional DSH knowledge runtime is a separate local service; the gateway
  health-probes it and reports ready/down honestly, but does not harden it.

## Automated checks

`tests/boujoy_preview_test.py` locks the preview contract (cwd confinement,
CSP sandbox, absolute-path handling); `tests/provider_test.py` locks the
never-echo / Keychain-only credential contract; `tests/worktree_test.py`
locks git isolation. CI runs them on every push (see
`.github/workflows/tests.yml`).
