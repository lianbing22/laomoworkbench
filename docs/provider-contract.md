# P0.5 Provider Profile 接口契约（前后端与测试的共同依据）

## HTTP 端点（网关本地，Control Plane，loopback）

所有端点 JSON；`secret` 只进不出（响应中永远只有 `secretConfigured: bool`）。

### GET /api/providers
```json
{"ok": true,
 "providers": [{
   "id": "chatgpt",              // 内置不可删
   "name": "ChatGPT / Codex",
   "type": "chatgpt",            // chatgpt | custom
   "baseUrl": null,              // chatgpt 类型为 null
   "wireApi": "responses",       // responses（当前 Codex 仅支持此协议）
   "envKey": null,               // custom 类型的环境变量名
   "models": [{"id": "gpt-5.6-luna", "label": "GPT-5.6-Luna"}],  // custom 手工配置；chatgpt 动态
   "defaultModel": null,
   "enabled": true,
   "secretConfigured": true,
   "builtin": true
 }],
 "activeProviderId": "chatgpt",
 "presets": [{                   // 新建表单的快速模板（只读数据，全部 Responses 兼容）
   "id": "deepseek", "name": "DeepSeek 官方",
   "baseUrl": "https://api.deepseek.com/v1",
   "keyUrl": "https://platform.deepseek.com/api_keys",
   "note": "原生 Responses 端点（deepseek-chat / deepseek-reasoner）。"
 }]}
```

### POST /api/providers/save
请求：`{id?, name, type, baseUrl?, models?, defaultModel?, enabled?, secret?}`
- 新建：无 id 或 id 不存在 → 创建（id 由 name 生成 slug）
- 更新：带 id；`secret` 为空串/缺省 = 保留原值；只有显式传 `secret: "xxx"` 才覆盖
- 响应：`{"ok": true, "provider": {...无 secret...}}`；非法 baseUrl → 400

### POST /api/providers/delete
`{id}` → `{"ok": true}`；内置 chatgpt → 400

### POST /api/providers/activate
`{id}` → `{"ok": true, "activeProviderId": id}`
- 只影响之后新建的会话；已有会话绑定不变

### POST /api/providers/discover
从服务的 OpenAI 兼容目录接口拉取模型列表（`GET {baseUrl}/models`），免去手敲 Model ID。
请求（二选一）：
- `{id}`：已保存的服务 → 使用已存凭证
- `{baseUrl, secret?}`：表单草稿（未保存也能试）；草稿 secret 只用于本次请求，绝不落盘

响应：
```json
{"ok": true, "models": ["deepseek-chat", "deepseek-reasoner"]}
```
失败时 HTTP 200 + `{"ok": false, "outcome": "...", "message": "中文原因"}`，
outcome ∈ `auth-failed | unreachable | protocol-incompatible | runtime-error | invalid`
（message 绝不包含 secret）。

### POST /api/providers/test
`{id}` → 真实 ephemeral E2E：
```json
{"ok": true, "outcome": "ok",
 "message": "回复正常"}
```
outcome ∈ `ok | auth-failed | unreachable | protocol-incompatible | model-not-found | timeout | runtime-error`
（message 中文，绝不包含 secret）

## 会话侧语义

- `session.create`：使用 activeProviderId 绑定新会话（SessionRegistry.providerId）
- `session.create`：模型选择优先级 = 显式参数 > host 设置命名空间 `model-selection`
  （`{model, provider, reasoningEffort}`，且仅当其 provider 与当前 active provider 一致才应用，
  防止把 DeepSeek 的模型钉在 ChatGPT 会话上）> 服务 defaultModel > Codex 默认
- `session.models`：
  - 已绑定会话 → 只返回该 Provider 的模型组，`current.provider = <providerId>`
  - 新会话/未绑定 → active provider 的组
- `session.selectModel`：跨 Provider 切换 → 错误 `provider-change-requires-new-session`，
  message "模型服务变更将在新会话中生效"
- ChatGPT Profile：models 从 `model/list` 动态取；custom：手工配置列表（可用 discover 拉取）
- 前端选择持久化：`selectModel` / 推理强度变更成功后写 `settings.update`
  ns=`model-selection`（clean 模式）；重启后 `session.create` 自动带回

## Mission 侧语义

- `POST /api/missions/create` 接受可选 `model` / `effort`：钉住该 mission 所有
  planner/worker/evaluator 回合的模型与推理强度（空白串视为未填）

## 前端 UI 要求（Agent B 范围）

入口：模型选择器旁"模型服务"按钮（⚙ 或"服务"字样）+ 设置对话框内入口。
Drawer/Modal（复用 boujoy-dialog 风格）：
- Provider 列表（名称/类型徽章/状态点：未配置·已配置·测试中·可用·失败）
- 新建/编辑（自定义字段：名称、Base URL、API Key(password, 留空保留)、
  Model ID 列表可增删（id+显示名）、默认模型；高级折叠：Wire API(只读 responses)、envKey 只读展示）
- 新建表单顶部「快速模板」下拉（数据来自 `GET /api/providers` 的 presets）：
  选中即预填名称/Base URL/API Key 获取地址提示；自定义选项 = 全手动
- 模型列表头部「从接口拉取」按钮：调 `/api/providers/discover`，把返回的模型 ID
  合并进现有行（保留已填显示名，不重复）；失败按 outcome 分级显示中文原因
- 启用开关、删除（内置禁用）、设为当前（activate）、测试连接（按钮态→测试中，结果徽章+错误信息）
- 错误信息按 outcome 分级显示（鉴权失败/端点不可达/协议不兼容/模型不存在/超时/运行时错误）
- 底部提示："API Key 仅保存在本机安全存储（macOS 钥匙串）"
- 切换 Provider 后：toast "模型服务已切换，将在新会话中生效"

样式沿用 app.css 现有 dialog/setting-card/boujoy 类，不做大改版。

## 测试要求（Agent C 范围）

- tests/mock_responses_server.py：最小 OpenAI Responses API mock（/v1/responses：校验
  Authorization、model，返回 SSE 或非流式 response 事件——以本机 Codex 实际请求格式为准，
  可在实现时抓包调整；默认端口 18652）；GET /v1/models 返回 OpenAI 兼容目录
- tests/provider_test.py：单测（fake secret，绝不写真实 Key）；
  presets 目录校验（必填字段/URL 合法/可过 save_profile 校验）；
  discover_models 全 outcome 路径（happy/无 Key 401/无 /models 404/不可达/草稿 secret 不落盘）
