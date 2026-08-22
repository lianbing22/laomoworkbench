<div align="center">

# 老墨工作台

## LAOMO WORKBENCH

**把 Agent 从聊天框里拽出来，接进你的本地工作区。**

**一个本地优先的 AI Coding Harness：让 Agent 不只回答问题，而是持续规划、执行、等待、验收、修复，直到任务真正完成。**

Codex app-server 驱动 · Durable Mission · Provider Profiles · Machine Verification · Local-first

[English](README_EN.md) · 基于 [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness) 二次开发

</div>

<p align="center">
  <img src="docs/screenshots/mission-loop-running.jpg" alt="Mission Loop 执行现场" width="900">
  <br>
  <sub>Mission 运行中：目标 → 自动规划单元 → Worker 执行 → 独立验收 → 修复循环 → 终验 DONE。
  动态演示 GIF 录制中，分镜与规范见 <a href="docs/screenshots/RECORDING.md">docs/screenshots/RECORDING.md</a>。</sub>
</p>

## 它是什么

老墨工作台是一个跑在你本机的 AI Coding Harness + 长周期 Mission 控制台。它不托管模型、不把凭据存到云端：Agent Runtime（默认 Codex `app-server`，模型服务由 Provider Profiles 配置）跑在你自己的机器上，工作上下文和运行存证留在你自己的文件夹里。

它和「又一个 Codex GUI」的区别在四件事：

### ① Durable Mission —— 不是回答完就结束

```text
给一个目标
   ↓
自动规划（拆成带验收标准的单元）
   ↓
Worker 执行（>20s 的命令走后台作业，WAITING → 自动 WAKE）
   ↓
独立 Unit Evaluator 验收
   ↓ NEEDS_WORK
修复循环（修复上限 / 无进展检测 / 四桶时间预算）
   ↓
Machine Verification（逐项存证）
   ↓
Fresh Final Evaluator 复跑全部验收标准
   ↓
DONE + Evidence Manifest
```

一句话：**不是 Agent 回答完就结束，而是 Harness 持续驱动任务直到通过验收；断电、暂停、崩溃都可恢复。**

### ② Codex Runtime —— 不是 shell 包装 CLI

控制面直接对接 **Codex `app-server`（stdio）**的 Thread / Turn / Tool / Approval 协议，模型选择、推理等级、沙箱权限、steer/interrupt 都是协议级实现，不是命令行拼字符串。

### ③ Provider Profiles —— 不用手编 TOML

设置里直接管理模型服务：ChatGPT/Codex 内置服务（用你本机的 Codex 登录凭证），或任意 Custom Responses 服务（Base URL / API Key / 模型列表 / 默认模型 / 推理等级），可测试连接、即切即用。API Key 只存本机安全存储。

### ④ Verification —— Agent 没有权限宣布自己完成

```text
Worker says DONE
      ✕
      ▼
Unit Evaluator（独立会话验收）
      ▼
Machine Verification（command / requiredFiles / httpChecks 逐项存证）
      ▼
Fresh Final Evaluator（全新上下文复跑每一条验收标准）
      ▼
     DONE（+ 不可变 Evidence Manifest：path/sha256）
```

验收不通过就是 `NEEDS_WORK` 进修复循环；验收条件自相矛盾时直接进入 `blocked` 真终态，而不是假装完成。

## 产品界面

| Agent 工作台 | 监控（暗色） |
| --- | --- |
| ![Agent 工作台](docs/screenshots/agent-page.jpg) | ![监控](docs/screenshots/monitor-page-dark.jpg) |
| **知识库** | **AI News** |
| ![知识库](docs/screenshots/knowledge-page.jpg) | ![AI News](docs/screenshots/news-page.jpg) |

更多实测截图：

- **Mission 受阻终态**（`blocked` 不是失败，是诚实的不可完成判定）：[agent-mission-blocked.jpg](docs/screenshots/agent-mission-blocked.jpg)
- **模型服务列表 / 新建服务表单**：[provider-list.jpg](docs/screenshots/provider-list.jpg) · [provider-form.jpg](docs/screenshots/provider-form.jpg)
- 监控页亮色主题：[monitor-page-light.jpg](docs/screenshots/monitor-page-light.jpg)

## 架构

```text
        Workbench UI（会话 · 计划 · 工具活动 · 监控 · 新闻）
                          │  HTTP/WebSocket · 127.0.0.1
        本地网关 web/boujoy_server.py
          ├─ RuntimeManager ────── Codex app-server --stdio（纯净模式，默认）
          │                     └─ DeepSeek Harness（知识模式）
          ├─ Mission 控制面 ── Planner / Worker / Evaluator / Verification / JobWatcher
          │                    （web/mission/ 包；状态与存证落在 <workspace>/.laomo/runs/）
          └─ Provider Profiles ─ 模型服务配置中心（本机钥匙串保存密钥）
                          │
              本地工作区 + Markdown Vault
              （Git · Shell · 文件 · 你拥有的目录）
```

P1.2 完成后，Mission 控制面将补上并行 Worker 调度与 Integration 合并/冲突路径（见路线图；**不在代码里的能力不写进本图**）。

## 快速开始

### 最简单：Clean + Codex

前置要求：Python 3、[Codex CLI](https://github.com/openai/codex) 已安装并已完成登录。

```bash
git clone https://github.com/lianbing22/laomoworkbench.git
cd laomoworkbench

mkdir -p vault

python3 web/boujoy_server.py \
  --port 8766 \
  --vault vault \
  --static web \
  --clean-runtime codex

# 打开 http://127.0.0.1:8766/
```

首次使用：左侧选一个工作区 → 在输入框描述任务，或点「设定目标」并勾选 **作为 Mission 运行**，观察它自己规划、执行、验收、修复到 DONE。

### 高级配置

<details>
<summary><b>知识模式（DeepSeek Harness + Vault）</b></summary>

需要本机已构建 DeepSeek Harness（存在可执行的 `node_modules/.bin/dsh`）和一个本地 Markdown Vault：

```bash
export BOUJOY_DSH_ROOT="$HOME/src/deepseek-harness"
export BOUJOY_VAULT_DIR="$HOME/BoujoyVault"
export BOUJOY_PYTHON_BIN="$(command -v python3)"

python3 web/boujoy_server.py --port 8766 --vault vault --static web
```

知识模式由索引和相关卡片按需提供上下文，不会把整个 Vault 塞给模型。
</details>

<details>
<summary><b>macOS 原生壳</b></summary>

macOS 13+ Apple Silicon，用 WKWebView 宿主代替浏览器：

```bash
./macos/build-app.command --install
```

便携包请从「启动」脚本启动，不要直接双击 App。
</details>

<details>
<summary><b>Windows 浏览器宿主（Beta）</b></summary>

Windows 10/11 x64：见 [windows/](windows/) 目录内的脚本与说明。
</details>

## Mission Loop 工作原理

1. **规划**：Planner 把目标拆成多个单元，每个单元自带验收标准；隐藏的隐含要求会被提升为独立单元。
2. **执行**：每个单元交给 Worker（Codex 会话）；超过 20 秒的命令通过 `LAOMO_JOB` 协议转交网关后台执行，卡片显示 WAITING，作业退出或超时自动唤醒。
3. **验收**：Unit Evaluator 在独立上下文里对照验收标准给出 PASS / NEEDS_WORK / BLOCKED。
4. **修复**：NEEDS_WORK 进入修复循环，受四重熔断约束——修复次数上限、无进展签名、最大循环数、墙钟时间（四桶时间账：active + waiting = elapsed，暂停单独记账）。
5. **终验**：全部单元 PASS 后，机器验证门禁逐项执行 command / requiredFiles / httpChecks 并落盘结果；随后 **fresh Final Evaluator** 用全新上下文复跑每一条验收标准。
6. **DONE**：三重条件同时满足——单元全 PASS + 机器验证 PASS + Final Evaluator PASS；生成不可变 Evidence Manifest（文件路径 + sha256）。
7. **诚实终态**：验收条件逻辑矛盾等情况直接 `blocked`，永不伪装成完成；崩溃后按进程身份 + PID 复用检测恢复，绝不认错进程。

运行记录（plan / verdicts / checkpoints / events / evidence）全部落在 `<workspace>/.laomo/runs/<mission-id>/`，可用任何编辑器审计。

## 知识模式与纯净模式

| 模式 | 适合 | 不做什么 |
| --- | --- | --- |
| 纯净模式（默认） | 编码任务、临时问答、实验 | 不读取你的 Markdown Vault；由 Codex app-server 驱动。 |
| 知识模式 | 要复用项目背景、文档、提示词、历史决策的任务 | 不会把整个 Vault 无差别塞给模型。 |

公开源码不附带个人 Vault，从空目录开始即可。

## 路线图

- **P0 已落地**：Runtime 层解耦——纯净模式由 Codex app-server 驱动（12+2 项真实验收全过），知识模式保持 DSH。
- **P0.5 已落地**：Provider Profiles——模型服务配置中心（baseUrl/APIKey/模型目录/推理等级，独立于 Runtime 切换）。
- **P1 已落地**：Durable Mission——目标自动规划多单元、后台作业生命周期、验收/修复闭环（真跑 8 个 Gate）。
- **P1.1 已落地**：Reliability & Hard Verification——`blocked` 终态、waiting pause/resume、崩溃恢复（进程身份+PID 复用检测）、四桶时间账、机器验收门禁、DONE 三重条件、Evidence Manifest；真跑 Gate A–E 全 PASS（`scripts/gate_p11_driver.py`）。
- **P1.2 进行中**：Parallel Mission Execution。已合入 M0–M3：`web/mission/` 包化重构、plan.json v2（unit id / dependencies / DAG 校验 + 依赖感知调度）、UnitRunner 单元执行层、WorktreeManager（每单元独立 git 工作树 + 串行集成）。进行中：并行 Worker 调度、IntegrationManager / ConflictResolver 冲突路径——**完成后才会在 README 宣传**。
- **随后**：P1.3 Multi-Mission Scheduler → P1.4 Provider Role Routing（Planner/Worker/Evaluator 可配不同模型）→ P1.5 Vault/Knowledge Context Layer。

## 常见问题

**为什么提示缺少运行组件？** 确认本机的 Agent Runtime、Vault 和 Python 路径真实存在。使用便携包时从「启动」脚本启动，不要直接双击 App。

**为什么启动页停留较久？** 首次运行要等本地网关和 Runtime 的健康检查，不是卡死。失败时检查本地 runtime、Python 和 Provider 配置。

**为什么 Agent 没有回复？** 本项目不托管模型余额或 API Key，从你的 Agent Runtime 侧检查模型 Provider、余额、网络与权限。

**知识库预览不可用会影响聊天吗？** 不会。知识预览是可选服务，缺失时主 Agent 界面继续可用。

**Mission 卡片为什么显示「受阻」？** 这是设计行为：Evaluator 判定验收标准无法满足时进入 `blocked` 真终态，理由写在检查点和事件记录里，比假 DONE 有价值得多。

**这是 DeepSeek 或 OpenAI 的官方产品吗？** 不是。老墨工作台是独立的非官方开源产品层。

## 隐私与网络边界

- Vault 内容、会话状态、凭据和 Mission 存证留在本机；本仓库不包含这些数据。
- 未配置访问码时本地网关只绑定 127.0.0.1；局域网访问需显式配置访问码。
- API Key 保存在本机安全存储（macOS 钥匙串），不进配置文件、不进 Git。
- AI 新闻页面请求 `web/boujoy_server.py` 中列出的公开 RSS；无分析、无遥测。
- 永远不要提交个人 Vault、会话记录、凭据或平台 runtime。

详细安全说明见 [SECURITY.md](SECURITY.md)。

## 验证与开发

不需要模型账户即可运行静态 smoke test（16 项检查）：

~~~bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
~~~

若本机已有运行中的实例（含 Agent Runtime）：

~~~bash
python3 tests/smoke_test.py --live-origin http://127.0.0.1:8766
~~~

模块回归覆盖 Runtime 适配器、Provider、Mission 引擎、DAG 调度与 Worktree（FakeAdapter 驱动，无需模型账户）：

~~~bash
for t in codex_adapter_test provider_test mission_test dag_test worktree_test; do
  PYTHONDONTWRITEBYTECODE=1 python3 "tests/$t.py"
done
~~~

P1.1 门禁真跑驱动（需真实 Codex 登录，跑在独立端口，勿用日常实例）：`scripts/gate_p11_driver.py`。

## 仓库内容

~~~text
macos/      macOS 原生 WKWebView 宿主与构建脚本
web/        本地网关、Web UI 与资源；web/mission/ 为 Mission 控制面包
windows/    Windows 浏览器宿主 Beta 脚本与说明
tests/      Runtime / Provider / Mission / DAG / Worktree 回归测试与 smoke test
scripts/    P1.1 真实 Codex 门禁驱动（Gate A–E）
docs/       契约与协议说明（mission-contract.md / provider-contract.md）与 docs/screenshots/ 实测截图
assets/     图标与视觉资源
~~~

## 许可与致谢

本仓库基于 [Boujoy Harness](https://github.com/asen-goat-mine/boujoy-harness)（MIT License）二次开发，遵循其许可证条款；上游又基于 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 构建。字体归属与第三方信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

「老墨工作台 / LAOMO WORKBENCH」为本项目自己的品牌，与 DeepSeek AI、OpenAI 无关联、不受其背书。
