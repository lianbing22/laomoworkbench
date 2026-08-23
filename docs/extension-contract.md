# Extension Platform Contract（P2.0 v1）— Codex 原生 Plugin / Marketplace / MCP

老墨不发明自己的插件格式。Extension Platform v1 完全复用 Codex 原生能力：
`codex app-server` 的 plugin/marketplace/config RPC。本文件冻结 2026-08-23
在本机真实二进制（**codex-cli 0.149.0-alpha.4.1**，schema 由
`codex app-server generate-json-schema` 导出并留存于
`docs/evidence/extension-m0/`，真实探测结果见 `probe-results.json`）上核实的
协议事实。0.148.0-alpha.21 的导出**没有**这些 RPC——插件市场能力有版本下限，
代码必须 capability detect，不得 version-gate 假定。

## Capability Matrix（实测）

| RPC | 状态 | 关键参数 | 关键响应 |
| --- | --- | --- | --- |
| `plugin/list` | ✅ | `cwds?: [path]`（repo marketplace 发现）、`marketplaceKinds?: [local\|vertical\|workspace-directory\|shared-with-me\|created-by-me-remote]`、`forceRefetch?: bool` | `marketplaces: [{name, path?, interface?, plugins: [PluginSummary]}]`、`featuredPluginIds`、`marketplaceLoadErrors` |
| `plugin/installed` | ✅ | `cwds?: [path]`、`installSuggestionPluginNames?: [str]` | 同上形状（installed 子集） |
| `plugin/read` | ✅ | `pluginName` + **恰好一个** `marketplacePath` 或 `remoteMarketplaceName`（两个都缺/都给 → `-32600`） | `plugin: PluginDetail` |
| `plugin/install` | ✅ | `pluginName` + `marketplacePath?`/`remoteMarketplaceName?`（二选一） + `installAttemptId?` | `{appsNeedingAuth, authPolicy}` |
| `plugin/uninstall` | ✅ | `pluginId`（canonical id，非 name） | `{}` |
| `marketplace/add` | ✅ | `source`（git URL 等） + `refName?` + `sparsePaths?` | `{alreadyAdded, installedRoot, marketplaceName}` |
| `marketplace/remove` | ✅ | `marketplaceName` | `{marketplaceName, installedRoot?}` |
| `marketplace/upgrade` | ✅ | `marketplaceName?`（null = 全部） | `{errors, selectedMarketplaces, upgradedRoots}` |
| `config/read` | ✅ | `{}` | `config.mcp_servers` **存在**（snake_case map） |
| `config/value/write` | ✅ | `keyPath, mergeStrategy: replace\|upsert, value`（`expectedVersion?`、`filePath?` 默认用户 config.toml） | `{status: ok\|okOverridden, version, filePath}` |
| `config/mcpServer/reload` | ✅ | `{}` | `{}`（成功即 reload） |
| `mcpServerStatus/list` | ✅ | `threadId?`、`detail?: full\|toolsAndAuthOnly`、`cursor?`、`limit?` | `{data: [McpServerStatus], nextCursor?}` |
| `skills/list` | ✅ | `cwds?: [path]`（空 = 会话 cwd）、`forceReload?: bool`（绕过缓存重扫磁盘） | `{data: [{cwd, skills: [SkillEntry]}]}` |
| `skills/config/write` | ✅ | `enabled: bool` + **恰好一个** `name`（名称选择器）/ `path`（绝对路径选择器） | `{effectiveEnabled: bool}` |

未知方法 → `-32600 "unknown variant"`（JSON-RPC error，不是 HTTP 语义）。

## Plugin canonical identity（数据层铁律）

`PluginSummary.id` 是 canonical id，形如 `documents@openai-primary-runtime`
（name@marketplace）。同名插件可来自不同 marketplace（local / remote curated /
workspace-directory）并**并存**——数据层按 `id` 索引，绝不允许 `by_name` 合并。
`PluginSummary` 关键字段（实测形状）：

```
id, name, installed: bool, enabled: bool, installedAt?: int|null,
version?, localVersion?, remotePluginId?, source: {type: local|git|npm|..., ...},
installPolicy: NOT_AVAILABLE|AVAILABLE|INSTALLED_BY_DEFAULT, installPolicySource?,
authPolicy: ON_INSTALL|ON_USE, availability: AVAILABLE|DISABLED_BY_ADMIN,
disabledReason?, mustShowInstallationInterstitial?: bool|null, interface?: {...},
keywords?, eligiblePlanTypes?, shareContext?
```

`PluginDetail`（plugin/read）：`summary + description? + marketplaceName +
marketplacePath? + skills[] + hooks[] + apps[] + appTemplates[] +
mcpServers: [str] + scheduledTasks?[] + shareUrl?`。

## 插件安装风险预览（必须展示）

plugin/read 返回的 `hooks/apps/mcpServers/scheduledTasks` 数量 +
`installPolicy/authPolicy/availability/mustShowInstallationInterstitial`。
包含 Hook / MCP / Scheduled Task / App 的插件**必须**走安装确认对话框。
`mustShowInstallationInterstitial=true` 时上游强制 interstitial——老墨不得绕过。

## Marketplace 事实（实测本机）

- `openai-primary-runtime`（local，path 有）：5 插件
- `openai-bundled`（local，path 有）：9 插件
- `openai-curated-remote`（**remote，无 path**）：3073 插件——`plugin/read`
  对它必须传 `remoteMarketplaceName: "openai-curated-remote"`
- `marketplace/upgrade` / `marketplace/remove` 只适用于 Git-backed marketplace；
  Codex 内置 local 源和 remote 目录是只读目录，调用前必须先做能力判断。
- `plugin/list` 不传 `cwds` 只看 home-scope；**必须**传 active workspace cwd
  才能发现 repo marketplace（workspace-aware）

## MCP 配置契约（snake_case，仅真实 schema 字段）

`config/read → config.mcp_servers` 是 map；写走 `config/value/write`
keyPath=`mcp_servers.<name>`，**禁止**自己编辑 TOML 文本。实测两种 transport：

**STDIO**：`command, args?, cwd?, env? {str:str}, enabled, startup_timeout_sec?`
**HTTP**（streamable）：`url, bearer_token_env_var?, enabled`（无
`env_vars`/`env_http_headers`/`http_headers` 字段——schema 没有，不发明）

读回时上游补 `environment_id`/`tool_timeout_sec`（默认值，只读，不回写）。
`type` 键在 TOML 中持久化（`type="stdio"`）但读回时省略——写时带上、读时忽略。

- `mcp_save`：validate → `config/value/write {keyPath, mergeStrategy:"upsert",
  value: snake_case}` → `config/mcpServer/reload` → `config/read` 复核 → postcondition
- `mcp_delete`：`config/value/write {keyPath, mergeStrategy:"replace", value:null}`
  → reload → 复核条目不存在 → postcondition
- name 校验 `^[A-Za-z0-9_-]{1,64}$`（进入 keyPath，禁止路径/控制字符）

## Skills 契约（P2.0.3，实测冻结）

老墨不自建 skill 格式。Skill = SKILL.md 目录（开放格式，同
anthropics/skills 的 Agent Skills 规范），由 Codex 自己发现与注入；老墨
只做管理面（列表 / 搜索 / 逐个启停）。实测本机（251 skills，scope
user 245 / system 6；探测脚本与原始输出见 `docs/evidence/extension-skills/`）：

- `SkillEntry`：`{name, description, path (…/SKILL.md), scope:
  user|project|system, enabled: bool}`——scope 是上游自己的词，不合成
- `skills/list` 返回按 cwd 分组的 `data[]`；**必须传 active workspace
  cwd**（与 plugin/list 同理，才能发现 project-scope skills）
- 上游有 `skillsChanged` 通知（本地 skill 文件变更信号）→ 处理方式：
  重新 `skills/list`（v1 用显式「强制刷新」按钮触发 `forceReload`，
  不挂通知监听）
- **启停是强 postcondition**：`skills/config/write` → `skills/list
  {forceReload: true, 同 cwds}` → 校验目标条目 `enabled` 已翻转。上游
  返回的 `effectiveEnabled` 只随响应携带，**绝不单独采信**
- `skills/extraRoots/set {extraRoots: [abs]}`（schema 已有）v1 不暴露
- `plugin/skill/read {remoteMarketplaceName, remotePluginId, skillName}`
  只服务远程市场插件的 skill 详情，v1 不用（PluginDetail 的 skills
  计数已够风险预览）
- 写选择器校验：`name` ≤128 字符无控制符；`path` 必须绝对路径 ≤1024
- 网关：`GET 聚合不含 skills 块`（避免每次扩展页加载白付一次扫描）；
  独立路由 `/api/extensions/skills-list`（`forceReload?`）与
  `/api/extensions/skill-toggle`（`{name|path, enabled}`）

## Configured vs Runtime Status（语义分离）

- **CONFIGURED** = `config/read` 的 `mcp_servers`（全局配置层）
- **RUNTIME STATUS** = `mcpServerStatus/list`（真实运行态：`authStatus:
  unknown|unsupported|notLoggedIn|bearerToken|oAuth`、`tools` map、
  `serverInfo?`、`pluginId?`——插件带来的 MCP 会标注来源 plugin）
- `McpServerStartupState`（starting/ready/failed/cancelled）出现在通知/
  thread 绑定上下文；无 threadId 时**不伪造** runtime 状态，显示
  "运行状态暂不可用"。实测 `mcpServerStatus/list` 无 threadId 也能返回
  全局聚合状态（`detail:"toolsAndAuthOnly"`）——如实上报，不夸大。

## Mutation Postcondition Contract

所有 mutation：WRITE → REFETCH AUTHORITATIVE STATE → VERIFY → SUCCESS。
上游返回成功但状态未变 → `ok=false, code=POSTCONDITION_FAILED`，UI 文案
"Codex 接受了请求，但插件状态未发生预期变化。" 绝不提前 toast 成功。

**强 postcondition（WRITE→REFETCH→VERIFY）**：

- install → 重 `plugin/installed`（**同 workspace cwds scope**），canonical id 出现且 `installed=true`
- uninstall → 重 `plugin/installed`（**同 workspace cwds scope**），id 消失（或 upstream 定义的等价终态）
- market add → 重 `plugin/list`，marketplaceName 出现
- market remove → 重 `plugin/list`，marketplaceName 消失
- mcp save/delete → reload 后 `config/read` 复核

**Upstream-result validation（非强 postcondition，诚实分级）**：

- market upgrade → 校验 upstream 自己的结果（`errors` 为空，或
  `upgradedRoots` 非空）。"已是最新" 没有可复核的稳定权威字段，所以
  不做 refetch-verify——这是与 install/uninstall/add/remove 的**有意
  区别**，不是遗漏。

## Workspace scope

`plugin/list`/`plugin/installed` 必须传 active workspace cwd（`cwds`）。
ExtensionService 从 RuntimeManager 获取当前 workspace cwd，不自行维护第二份。
**mutation postcondition 同样 workspace-aware**：install/uninstall 的复核
扫描带与 inventory 相同的 cwds——只用 home scope 会漏掉 project/workspace
scoped 插件（误报 POSTCONDITION_FAILED）或看到同源陈旧记录（误判成功）。

## 不支持/降级语义

- 单块 RPC 失败（capability 缺失**或**普通 upstream error）→ 该块
  `{"supported": false, "error": "..."}`（空 payload），其余块照常
  （绝不让整个聚合 500）。`supported=true + error` 是暧昧语义，不使用
  ——块的 `supported` 就是对"这块能不能用"的唯一回答
- clean runtime ≠ codex → 网关层 `CODEX_RUNTIME_REQUIRED`
- 上游 evolve（experimental RPC）：capability detect（探测方法是否存在/
  错误形状），不用版本号猜测；README 写"推荐 Codex ≥ 0.149.0-alpha.4.1"

## 安全边界（SECURITY.md 同步项）

- 老墨不托管/不审核第三方插件；插件可携带 Skills/Hooks/MCP/Apps/
  Scheduled Tasks 等系统能力
- MCP 写入 `~/.codex/config.toml`，影响所有 Codex 会话（含 Mission worker）
- `env` 是明文进 TOML——UI 必须警示"该值会写入 ~/.codex/config.toml，
  不要直接保存 API Token/Password"，优先引导 `bearer_token_env_var`/env 引用；
  secret 模式 KEY/TOKEN/SECRET/PASSWORD/API_KEY 且非 env 引用 → 高风险 warning
