# 进展 Notes — 能力识别与延后决策

本文记录各阶段识别出的能力与决策状态。已完成项会标出落地位置，避免重做。

## 已完成

- **rename**：`thread/rename` 已接入（codex_adapter），前端可调用。
- **fork**：`thread/fork` 已接入（codex_adapter，1700 行附近）。
- **goal / 目标与计划**：Durable Mission 引擎（P1）：目标 → 多单元计划 → 后台作业
  执行 → 验收/修复闭环 → 机器验收 + fresh Final Evaluator + Evidence Manifest，
  见 `web/mission.py` 与 `docs/mission-contract.md`（P1.1 Hard Verification 已收口：
  五真跑 Gate A–E 全 PASS，门禁脚本 `scripts/gate_p11_driver.py`）。
- **queue / steering**：由 Mission 引擎的 pause/resume/cancel 与 waiting 作业状态
  拥有（Control Plane 侧），不再投影为 DSH 语义。
- **多 Runtime 选择器**：RuntimeManager + Provider Profiles（P0.5）已解耦
  model provider（baseUrl/apiKey/模型目录），见 `web/provider_profiles.py`。

## 识别待做（P1.2+ 排期）

- **P1.2 Parallel Mission Execution**：DAG 依赖调度 + Git Worktree 隔离 +
  Integration 合并/冲突路径（当前主线进行中：`MissionScheduler` / `WorktreeManager`
  / `IntegrationManager` / `ConflictResolver`）。
- **P1.3 Multi-Mission Scheduler**：多 Mission 同时运行（当前单 Mission 串/并行）。
- **P1.4 Provider Role Routing**：Planner / Worker / Evaluator 使用不同模型。
- **P1.5 Vault / Knowledge Context Layer**：Vault/Expert/Style 上下文转 Codex 可消费的
  Context Layer（thread/inject_items 或 baseInstructions）。
- **session.search / subagent.\* / settings.\* / credentials.\* / skill.list /
  agentPreset.\***：前端会调用，仍为安全 stub（Codex 侧 skills/list、agentPreset
  已存在，P1.4+ 视角色路由需要接入）。
- **图片输入端到端实测**：`_content_to_input` 已把 image 块转 data URL（协议支持），
  但未做端到端实测；历史 attachment 读取（session.attachment）未接。
- **Codex remote daemon / WebSocket transport**：官方标注 experimental，不进排期。
- **Tauri/Rust 原生后端 + codex-app-server-client crate**：消除进程边界的长期路线，
  当前 stdio 足够。
- **stale writer 自愈**：app-server 异常退出留下的 rollout 锁（docs/codex-protocol-notes.md
  坑 4），P1 可加检测+提示或自动隔离该 thread。
