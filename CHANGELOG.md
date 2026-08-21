# 更新日志

本文件记录 `lianbing22/laomoworkbench` 自品牌化以来的全部功能更新。协议细节见
[docs/codex-protocol-notes.md](docs/codex-protocol-notes.md)，P1 待办见 [P1_NOTES.md](P1_NOTES.md)。

## 2026-08-22 — P0 Clean Runtime Migration 及后续增强

### 核心架构：Codex 接管纯净模式（`2b39173`）

- **Runtime 解耦**：`RuntimeManager` 按模式解析后端——知识模式 → DSH（原路径零改动），
  纯净模式 → `CodexRuntimeAdapter` → `codex app-server --stdio`（本机 0.148.0-alpha.21）。
  启动参数 `--clean-runtime codex|dsh` 一键切换，默认 dsh 可随时回退。
- **适配器**（`web/codex_adapter.py`）：进程懒启动/握手/指数退避重启、RPC 关联、
  事件翻译、确定性历史折叠、会话注册表全部收敛于此；网关业务层零 Codex 知识。
- **验收**：12+2 项全过（对话闭环/审批/打断/崩溃恢复/重连恢复/模型能力/隔离/回退），
  单元测试 28 项，上游冒烟 16/16 保持。

### 对话体验

- 事件流对齐前端硬语义（`3e0eb7f`）：用户消息回显携带可匹配的 `source.rpcId`
  （问题不再跑到答案后面）；定稿按 item id 去重（回答不重复）；chunk/定稿携带
  turn/step 流身份（**完成后自动切换 Markdown 富文本渲染**）。
- 回合提示净化（`fa40a50`）：去掉原始 UUID，显示为"回合开始/结束"。
- 运行中发消息（steer）：补 `expectedTurnId` 前置参数 + 回合竞态降级（`493e795`）。

### 模型能力

- 推理等级接入 Codex 真实目录（`a648c55`）：low/medium/high/**xhigh/max** 五档 +
  各模型默认档，来自 `model/list` 的 `supportedReasoningEfforts`。
- 输入框旁新增推理强度选择器（`eaab1d2`）：跟随所选模型动态刷新档位，即选即生效。

### 权限系统（`cfe599b`）

- `/permission read-only|workspace-write|danger-full-access` 映射到 Codex 沙箱
  （`readOnly`/`workspaceWrite`/`dangerFullAccess`），作用于后续回合；
  完全访问自动免审批，其余保持按需弹窗。
- 三路回读（实时投影帧 / session.list / session.history），修复"回读权限为未知"。

### 信号面板（`493e795`）

- **目标**：设定/编辑/暂停/继续/完成/清除全链路（`thread/goal/set|clear`），
  Codex goal 会自动驱动 Agent 执行。
- **计划**：`turn/plan/updated` → 步骤投影（pending/inProgress/completed 实时勾选），
  链路就绪，模型发出计划即显示。
- **运行记录**：工具调用/完成卡片实时展开（含终端输出与 diff）。

### 交互矩阵补齐（`f4ef9dd`）

- 会话重命名（`thread/name/set`）、分叉（`thread/fork`）、搜索（标题/预览过滤）。
- 监控页数据源：`tokenUsage` + `contextPressure` 双投影（实时 + 历史回读）。
- 关键修复：投影帧独立 seq 计数器，避免触发前端缺口检测导致常驻历史重拉。
- 空闲会话停止不再报错；Codex 模式删除会话修复 400。

### 页面与内容生态（`38666a1`）

- **全页面解锁**：纯净模式下 02-06 页面（知识库/专家/风格/监控/新闻）全部可用——
  它们是网关本地功能，与 Agent 引擎无关。
- **新闻源切换 AIHOT**（aihot.virxact.com）：动态列 `feed/all.xml` + 工具列
  `feed/daily.xml`（中文、自动去重打分），保留量子位/HN/OpenAI/TechCrunch 补充。
- 知识库/专家/风格：Vault Markdown 全链路实测（列表/搜索/读取/CRUD/回收站），
  修复 `id:null` 生成 `None.md` 的边界 bug（`2bc934f`）。

### 稳定性

- 模式持久化（`06e1a35`）：记住上次选择的模式；默认进入纯净模式（本部署知识引擎未起）。
- 空线程 `thread/read` 降级（`not materialized` 时改 `includeTurns:false`）。
- Codex 子进程崩溃自动重启（3.4s 实测恢复），网关退出优雅回收。

### 已知边界（P1）

- Vault → Codex 上下文注入（知识库/专家卡参与 Codex 推理）。
- 任务队列（应由老墨控制层拥有）、子代理面板、会话内图片附件读取。
- Codex 实验特性（WebSocket transport、remote daemon）不接。
