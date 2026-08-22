# 项目状态 — 单一事实源

本页是阶段状态、测试计数与门禁结论的**唯一权威来源**。README 只做产品介绍，
不在此维护版本状态；契约细节见 [mission-contract.md](mission-contract.md) /
[provider-contract.md](provider-contract.md)。

更新时间：2026-08-23（`fca0d13` 后）

## 总判定

**LAOMO WORKBENCH — USABLE（真实项目阶段）**

P0 / P0.5 / P1 / P1.1 / P1.2 全部完成；真实 Codex 门禁 Gate 0/A–J +
Usability 验收全部 PASS。当前阶段：3 个真实项目试运行（dogfood），之后再
毕业压测（Gate K）与 H2。

## 阶段台账

| 阶段 | 内容 | 结论 |
| --- | --- | --- |
| P0 / P0.5 | 双运行时 RuntimeManager、Provider Profiles、钥匙串凭据 | 完成 |
| P1 | Durable Mission 引擎（目标→计划→作业→验收修复闭环→机器验收→终评→Evidence） | 完成（Gate A–E PASS） |
| P1.1 | Mission Reliability：blocked 终态、作业全权生命周期、PID 复用检测、四桶时间账、机器验收门禁、manifest 不可变 | 完成（Gate A–E PASS，`scripts/gate_p11_driver.py`） |
| P1.2 | 并行 DAG 调度 + Git worktree 隔离 + 集成事务 + 冲突解决 + 集成树终验（M1–M5-C） | 完成（Gate 0/A–J + Usability 全 PASS，`aaf2751`，`scripts/gate_p12_driver.py` + `scripts/gate_p12_runtime_concurrency.py`） |
| 真实项目 #1 | full-auto 权限、`/api/preview` 产物预览、工作模式 Chat/Plan/Auto、native folder picker、Host 能力补齐（`85d1f88`–`f5c950c`）、状态隔离 `LAOMO_HOST_STATE_ROOT`（`373072a`） | 完成 |
| 真实项目 #2–#3 | dogfood 试运行（参数锁定：`maxParallelWorkers=2`、3–6 单元、30min–2h、机器验收必选、禁生产/部署/迁移权限、DONE 后人工复核） | **进行中** |

## 排期（dogfood 稳定后）

- **Gate K**：压力重复门禁（真实 codex，重复跑 P1.2 场景量稳定性）
- **H2**：冲突进行中崩溃的真实门禁
- 两者都在 3 个真实项目跑完、failure taxonomy 整理之后再做——dogfood 比
  synthetic fixture 更容易撞到真正影响体验的问题

## 暂缓（避免重新扩大复杂度）

- **P1.3 Multi-Mission**：硬前置条件是 `ActiveTurn{missionId,unitId,role,
  threadId,turnId}` 归属化——当前 cancel 链路是 adapter-wide 的
  `interrupt_active_turns()`，建立在 single-active-mission 不变量上；多 Mission
  并行后取消 A 会误伤 B/C 的 turn。必须先改为按 mission 归属的中断
- **P1.4 Provider Role Routing**（Planner/Worker/Evaluator 分模型）
- **P1.5 Vault / Knowledge Context Layer**
- Multi-Mission / Vault / 新 Agent 系统：全部等真实项目阶段收口

## 自动化测试（166 项 + 26 subtests）

运行：`python3 -m pytest tests/ -q`（仓库根；pytest 为开发依赖，运行时本体
零第三方依赖）。

| 文件 | 计数 | 覆盖 |
| --- | --- | --- |
| `tests/mission_test.py` | 42 | 状态机全路径、租约、default-fail、熔断、暂停恢复、作业身份/PID 复用、四桶时间账、manifest |
| `tests/dag_test.py` | 13 | plan v2 归一化、依赖解析、成环打断、依赖门（integrated 才放行） |
| `tests/worktree_test.py` | 30 | worktree 创建/分支、集成事务、冲突物化、幂等重放、楔死恢复 |
| `tests/codex_adapter_test.py` | 39 | Host 能力（settings/credentials/preset/workspace/provider/RPC 翻译）、状态隔离 |
| `tests/provider_test.py` | 34 | Provider Profiles、钥匙串、连接测试 |
| `tests/boujoy_preview_test.py` | 8 | 预览沙箱（cwd 限制、CSP、绝对路径） |

## 真实运行时认证（manual / local，不进普通 CI）

真实 codex + 真实网关。结论：**Gate 0/A–J + Usability 验收全部 PASS**。
逐门禁提交：`57716db`（Gate 0）→ `69fe165`（A）→ `85c56ae`（B）→
`2c0a6ba`（C）→ `23f612e`（D）→ `5561c90`（E）→ `d692d18`（F）→
`f1c908c`（G）→ `38abe1a`（H）→ `efee945`（I）→ `63bd7b9`（J）→
`aaf2751`（Usability）。

关键实证（写进契约的教训）：

- Gate A：worktree 创建竞态曾静默回退用户工作区——现在禁止回退、诚实失败
- Gate D：单元描述内嵌标记语法会污染 evaluator——描述必须按数据隔离
- Gate E：pause 语义 = quiesce-not-interrupt（不停真实 codex 进程，零 builder
  重放、墙钟预算真冻结）

## dogfood 记录方式

真实项目产物**不进主仓库**（首会话产物已在 `fca0d13` 清理，gate 残留
job-done/seed 已 ignore）。后续如需留存，按结构化运行数据（时长/单元数/
turn 数/修复次数/冲突/人工干预/终判）单独归档，不提交项目源码本身。
