# Boujoy Harness

Boujoy Harness is an unofficial macOS product shell for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). It keeps DeepSeek Harness as the Agent runtime and adds a local Markdown knowledge workspace, while preserving the Harness event and RPC protocol.

This repository contains only Boujoy's source: the native macOS shell, local gateway, Web UI, tests, and first-party assets. It deliberately excludes user vaults, sessions, credentials, local runtime state, built app bundles, and DeepSeek Harness dependencies.

## Status and scope

- macOS 13+ / Apple Silicon (arm64)
- Windows 10/11 x64 browser-hosted adapter (beta): same UI, local PowerShell service host; see [Windows setup](windows/README-Windows.zh-CN.md).
- Developer preview; upstream Harness releases may introduce compatibility-breaking changes.
- Not affiliated with, endorsed by, or supported by DeepSeek AI.
- Built against DeepSeek Harness `0.1.0-rc.6` during this release cycle.

## What is included

- Native `WKWebView` macOS host and controlled self-restart flow
- Local-only Markdown vault browser, search, records, and recoverable deletion
- Transparent WebSocket and RPC bridge for DeepSeek Harness
- Conversation paging, streaming, and scroll-stability safeguards
- Isolated smoke tests for protocol, path containment, access controls, and portable-runtime normalization

## Build from source

1. Install DeepSeek Harness separately. Follow its official source-build guide, then build it so `node_modules/.bin/dsh` exists.
2. Create a local Markdown vault directory and make sure a usable `python3` is available.
3. Point the build at those local paths. Do not commit these environment values.

```bash
git clone <your-fork-url> Boujoy-Harness
cd Boujoy-Harness

export BOUJOY_DSH_ROOT="$HOME/src/deepseek-harness"
export BOUJOY_VAULT_DIR="$HOME/BoujoyVault"
export BOUJOY_PYTHON_BIN="$(command -v python3)"

./macos/build-app.command --install
```

The app is installed to `~/Desktop/Boujoy Harness.app`. The generated `dist/` app is intentionally ignored by Git. For an existing portable package layout, use `./macos/build-app.command --portable-root <package-root>`.

## Portable macOS packages

An unsigned `.app` opened directly from a downloaded ZIP can be put in macOS App Translocation, where it cannot reliably see sibling `vault/` and `runtime/` directories. A portable build therefore writes `启动 Boujoy Harness.command` into the package root.

Recipients should unzip the complete package, keep `Boujoy Harness.app`, `vault/`, and `runtime/` together, then double-click `启动 Boujoy Harness.command`. It supplies the package root explicitly and avoids the relative-path failure. If someone opens the `.app` directly, Boujoy detects the translocated launch and offers a one-time folder picker instead of exposing temporary system paths.

This is a usability fallback, not notarization. Public releases still need a Developer ID signature and Apple notarization for ordinary Gatekeeper behavior.

## Windows adapter

The Windows adapter intentionally keeps the product UI in the browser and changes only the local host process. `windows/Start-Boujoy.ps1` launches the knowledge and clean Harness modes, serves Boujoy on loopback, opens Edge in app mode when available, and owns a safe restart signal for the in-product restart command.

It is not valid to copy a macOS `runtime/` folder into Windows: DeepSeek Harness includes platform-native dependencies. Prepare the runtime on a real Windows x64 machine with `windows/Prepare-Windows-Runtime.ps1`, then verify that package before distributing it. The repository includes `windows/Build-Windows-Portable.ps1` to assemble a scaffold without silently copying a private vault or macOS runtime.

## Verification

```bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
```

The isolated suite does not contact a model provider or spend balance. It verifies the local gateway and source contracts. A live Harness check is optional and requires a running local instance:

```bash
python3 tests/smoke_test.py --live-origin http://127.0.0.1:8766
```

## Privacy and network behavior

- Vault content, app state, and credentials remain on the local machine. Model requests are handled by the separately configured Harness/provider.
- The desktop app creates a random six-digit access code before enabling the optional phone view. Launching the gateway without `--access-code` binds it to loopback only.
- AI news refreshes contact the public RSS feeds listed in `web/boujoy_server.py`; Boujoy configures no analytics or telemetry endpoint.
- Never commit `boujoy-config.json`, a vault, credentials, sessions, or a generated `dist/` app bundle.

## License and notices

Boujoy-authored code and artwork are released under the [MIT License](LICENSE). DeepSeek Harness is a separate MIT-licensed dependency with its own notices. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), including the required Fusion Pixel Font attribution.

## Security

Do not publish security reports or credentials in public issues. Follow [SECURITY.md](SECURITY.md).
