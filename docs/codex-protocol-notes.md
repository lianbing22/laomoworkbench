# Codex app-server 协议要点（P0 适配依据）

本机已验证版本：`codex 0.148.0-alpha.21`（`~/.local/bin/codex`，ChatGPT 登录）。
完整 JSON Schema 可随时再生（约 3.7MB，已 gitignore）：

```bash
codex app-server generate-json-schema --out docs/codex-schema-0.148.0-alpha.21
```

以下是适配器（`web/codex_adapter.py`）依赖的、经真实运行验证的协议事实。

## 传输与握手

- `codex app-server --stdio`：stdin/stdout，逐行 JSON（JSONL），JSON-RPC 2.0 但线上省略 `jsonrpc` 字段。
- 握手顺序：客户端发 `initialize` 请求（带 `clientInfo`）→ 收响应 → 发 `initialized` **通知**（无 id）。
- 请求 `{id, method, params}`；响应 `{id, result}` 或 `{id, error:{code,message}}`（已见 `-32600`）。
- 服务端反向请求（审批等）：`{id, method, params}`，客户端必须回 `{id, result}`。

## 关键方法（客户端 → 服务端）

| 方法 | 要点参数 | 实测行为 |
| --- | --- | --- |
| `thread/start` | `cwd`、`model`、`effort`、`approvalPolicy` | 返回含新 thread id；线程即为 loaded |
| `thread/list` | `limit`、`cursor`、`archived`、`cwd` 过滤 | 返回 `{data:[Thread], nextCursor}`；列表里的线程**未加载** |
| `thread/resume` | `threadId` | 加载旧线程；已加载时报 `thread-store conflict: already has an active writer`（幂等可吞） |
| `thread/read` | `threadId`、`includeTurns:true` | 返回 `{thread:{turns:[{id,status,items:[{turnId,item}]}]}}` |
| `thread/archive` | `threadId` | 归档 |
| `turn/start` | `threadId`、`input[]`、`model`、`effort`、`cwd`、`clientUserMessageId` | 未加载线程上报 `thread not found`（需先 resume） |
| `turn/steer` | `threadId`、`input[]` | 运行中追加引导 |
| `turn/interrupt` | `threadId`、`turnId` | turnId 来自 `turn/started` 的 `turn.id` |
| `model/list` | — | 返回 `{data:[{id, displayName, ...}]}` |
| `config/read` / `account/read` | — | 当前配置/账号 |

`input[]` 块：`{type:"text", text}` 与 `{type:"image", url:"data:<mime>;base64,..."}`。
`ReasoningEffort` 是非空字符串（本机模型报 low/medium/high）。
`ThreadStatus.type` 枚举：`notLoaded | idle | systemError | active`（**active = 运行中**）。
`TurnStatus`：`completed | interrupted | failed | inProgress`。

## ThreadItem 变体（thread/read 返回）

`userMessage{content, clientId}`、`agentMessage{text}`、`reasoning{summary}`、
`commandExecution{command, aggregatedOutput, exitCode, status}`、`fileChange{changes[{path, diff}]}`、
`mcpToolCall{tool, arguments, result, error}`、`dynamicToolCall`、`webSearch{query, results}`、
`plan{text}`、`contextCompaction`、`collabAgentToolCall`、`subAgentActivity` 等 18 种；未知变体适配器安全忽略。

## 关键通知（服务端 → 客户端）

`turn/started{turn}`、`turn/completed{turn}`（**turn 对象在 `params.turn`，id 在 `turn.id`**）、
`thread/started{thread}`、`thread/status/changed{status}`、`thread/tokenUsage/updated{tokenUsage}`、
`item/started{item}`、`item/completed{item}`、`item/agentMessage/delta{itemId, delta}`、
`item/reasoning/textDelta|summaryTextDelta`、`item/commandExecution/outputDelta`、
`error{message}`、`warning{message}`。

## 审批（服务端请求 → 应答）

请求方法：`item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、
`item/permissions/requestApproval`、`item/tool/requestUserInput`、`mcpServer/elicitation/request`，
另有旧式 `applyPatchApproval`、`execCommandApproval`。

`commandExecution` 审批的 `params.command` 在**顶层**（非 item 内）。应答格式：

```json
{"decision": "accept"}            // = 允许一次
{"decision": "acceptForSession"}  // = 本次会话同类免批
{"decision": "decline"}           // = 拒绝
```

## 已知坑（真实踩过）

1. `turn/started` 的 turn id 在 `params.turn.id`，不是 `params.turnId`——取错会导致 interrupt 静默失败。
2. `thread/status/changed` 的运行态枚举是 `active`，不是 busy/working。
3. 对未加载线程 `turn/start` 报 `thread not found`；必须先 `thread/resume`（幂等冲突要吞掉）。
4. app-server 进程被 `kill -9` 中断 thread 持久化初始化时，会在 rollout 留下 stale writer 标记，
   该 thread 后续 resume 永远 conflict（单线程损伤，规避方式：走网关的优雅退出）。
5. 未知通知/字段必须防御性解析——alpha 版本通知集合会增减（已见 `hook/started`、`remoteControl/status/changed` 等）。
