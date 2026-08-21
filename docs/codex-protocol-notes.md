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

## Provider 配置（P0.5 实测，独立 CODEX_HOME 副本验证）

1. `config/value/write` params：`{keyPath, mergeStrategy: "replace"|"upsert", value, expectedVersion?}`。
   写 `model_providers.<id>` 时 value 必须是 **ModelProviderInfo 对象（snake_case）**：
   `{name, base_url, env_key, wire_api, env_key_instructions?, experimental_bearer_token?,
   auth?{command,...}, query_params?, http_headers?, env_http_headers?, ...}`（18 字段）。
   upsert 追加式落盘 `[model_providers.<id>]` 表，不破坏其它配置；有 expectedVersion 乐观锁。
2. `wire_api` 枚举**只有 `"responses"`**（`chat` 已移除，报错附迁移提示）。自定义 Provider 必须实现
   OpenAI Responses API，请求 URL = `base_url + "/responses"`（实测抓到该 URL 的连接错误）。
3. **`thread/start` 有 `modelProvider`(string|null)，`turn/start` 没有**——Provider 是线程级静态属性，
   只能切 model 不能切 provider。
4. `env_key` 运行时读取：缺失时 turn 立即失败，消息 `Missing environment variable: \`X\``（可驱动 UI）。
5. `model/list` **不含自定义 Provider 的模型**，需手工 Model ID；未收录模型有
   `Model metadata not found` 警告但 turn 正常。`config.read` 顶层 `model_catalog_json` 是预留注入点（后续调研）。
6. provider id 含点时 keyPath 需 TOML 引号转义（`model_providers."my.vendor"`）；老墨侧限制 id 为
   `[A-Za-z0-9_-]+` 规避。
7. 内联凭证可走 `experimental_bearer_token`（不推荐，违反 P0.5 安全 Gate）；老墨一律 env_key。

## 已知坑（真实踩过）

1. `turn/started` 的 turn id 在 `params.turn.id`，不是 `params.turnId`——取错会导致 interrupt 静默失败。
2. `thread/status/changed` 的运行态枚举是 `active`，不是 busy/working。
3. 对未加载线程 `turn/start` 报 `thread not found`；必须先 `thread/resume`（幂等冲突要吞掉）。
4. app-server 进程被 `kill -9` 中断 thread 持久化初始化时，会在 rollout 留下 stale writer 标记，
   该 thread 后续 resume 永远 conflict（单线程损伤，规避方式：走网关的优雅退出）。
5. 未知通知/字段必须防御性解析——alpha 版本通知集合会增减（已见 `hook/started`、`remoteControl/status/changed` 等）。
6. 空线程（还没有首条用户消息）调 `thread/read includeTurns:true` 报 "not materialized yet"；
   需降级为 `includeTurns:false`。
7. 线程在首轮对话前不落盘，`thread/list` 看不到它——UI 侧用本地占位符 + `host/session-added` 帧补位。
8. 沙箱策略变体是 camelCase：`readOnly` / `workspaceWrite` / `dangerFullAccess` / `externalSandbox`；
   `turn/start` 的 `sandboxPolicy` + `approvalPolicy`（untrusted/on-request/never）按"本回合及后续"语义生效。
