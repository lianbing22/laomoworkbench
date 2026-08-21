# P1 Notes — P0 期间识别、明确延后的能力

P0（Clean Runtime Migration）范围外的事项记录于此，避免顺手实现导致范围膨胀。

## Codex Runtime 侧

- **rename**：`thread/name/set` 已存在，P0 前端调用返回 unsupported。接入工作量小。
- **fork**：`thread/fork` 已存在，同上延后。
- **queue**：DSH 的 steering queue 语义（session.updateQueue/session/queue 帧）不应硬映射到 Codex turn；
  应由老墨自己的 Control Plane 拥有队列，再投影为 Codex turn/steer。
- **完整 Workspace Manager**：workspace CRUD/排序/迁移/最近列表。P0 只有 cwd 映射（workspace.create 改 cwd）。
- **session.search / subagent.* / goal.* / settings.* / credentials.* / skill.list / agentPreset.***：
  前端会调用，P0 一律安全 stub（空集合/ok:false）。Codex 侧其实有 skills/list、thread/goal/set 可接。
- **图片输入**：`_content_to_input` 已把 image 块转 data URL 传给 turn/start（协议支持），但未做端到端实测；
  历史 attachment 读取（session.attachment）P0 未接。
- **Codex remote daemon / WebSocket transport**：官方标注 experimental/unsupported，不进 P0。
- **多 Runtime 选择器 UI**：Mode 与 Runtime 已解耦（RuntimeManager 配置），UI 暴露留给 P1。

## 架构侧

- **knowledge → Codex**：Vault/Expert/Style 上下文转 Codex 可消费的 Context Layer（thread/inject_items 或
  baseInstructions），是 P1 的主战役。
- **Tauri/Rust 原生后端 + codex-app-server-client crate**：消除进程边界的长期路线，P0 用 stdio 足够。
- **capabilities 驱动 UI**：host.describe 已返回 capabilities{modelSelection,reasoningEffort,steer,interrupt,
  fork,queue}，前端尚未消费；P1 可按能力渲染（如 Codex 下隐藏队列按钮）。
- **turn/completed 折叠与 live 事件去重**：当前 turn/completed 会重放 items 兜底定稿，若 live 已发过
  item/completed 会产生内容重复（前端以 assistant/message 定稿覆盖渲染，实际无碍）；P1 可按 item id 去重。
- **stale writer 自愈**：app-server 异常退出留下的 rollout 锁（见 docs/codex-protocol-notes.md 坑 4），
  P1 可加检测+提示或自动隔离该 thread。
