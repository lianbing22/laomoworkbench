# 更新日志

本文件记录 `lianbing22/laomoworkbench` 自品牌化以来的全部功能更新。协议细节见
[docs/codex-protocol-notes.md](docs/codex-protocol-notes.md)，当前阶段状态与排期见
[docs/status.md](docs/status.md)。

## 2026-08-23（日）— 模型配置 v2（快速模板 + 模型自动发现 + 选择持久化）

对标 one-api / new-api / LiteLLM / Cherry Studio / LobeChat 的供应商配置体验，
把 P0.5 Provider 模块补到「零手敲」水位（前后端契约见
[docs/provider-contract.md](docs/provider-contract.md)）：

- **快速模板**：新建服务一键预填。模板目录由后端统一维护（`GET /api/providers`
  的 `presets`），只收录 Responses 协议原生或网关可达的服务：OpenAI 官方、
  DeepSeek 官方（原生 Responses 端点）、OpenRouter（drop-in 兼容）、本机
  LiteLLM（:4000）、本机 new-api/one-api（:3000）；模板附 API Key 获取地址。
- **模型自动发现**：`POST /api/providers/discover` 调服务的 OpenAI 兼容目录
  （`GET {base}/models`），表单「↧ 从接口拉取」把模型 ID 合并进列表（保留已填
  显示名与默认选择）。已存服务用钥匙串凭证；草稿（未保存）用输入框里的 Key，
  只用于本次请求、绝不落盘。失败按 outcome 分级提示（鉴权失败/端点不可达/
  协议不兼容/参数无效）。
- **选择持久化**：模型/推理强度选择写入 host 设置 ns `model-selection`
  `{model, provider, reasoningEffort}`；`session.create` 按优先级应用
  （显式参数 > 已存默认〔provider 匹配才生效〕> 服务 defaultModel > Codex
  默认），重启不再丢、新会话自动沿用，且不会把 DeepSeek 的选择错钉到
  ChatGPT 会话。
- **Mission 模型钉选**：`POST /api/missions/create` 接受可选 `model`/`effort`，
  该 mission 所有 planner/worker/evaluator 回合固定用它；目标对话框 Mission
  模式下提供模型/强度选择（留空跟随默认）。
- **设置页清理**：模型 tab 移除 DeepSeek 专用旧凭证行与 deepseek-official 耦合，
  改为「当前模型服务 + 上次选择 + 管理入口 + 重新发现」。
- **测试**：provider 套件 47 用例（新增 presets 校验、discover 全 outcome 路径、
  session.create 优先级矩阵），mission 钉选 3 例；mock server 增加
  `GET /v1/models` OpenAI 兼容目录。

## 2026-08-23（日）— Skills 配置模块（Codex 原生 skills RPC）

设置 → Skills 从空壳只读列表升级为真实管理面。老墨不自建 skill 格式——
Skill 是 SKILL.md 目录（开放格式，同 [anthropics/skills] 的 Agent Skills
规范），由 Codex 自己发现与注入，老墨只做管理（启停交互参考
[xingkongliang/skills-manager] 与 Claude 官方设置）：

- **真实数据源**：直连 codex app-server 原生 `skills/list`（schema 留存 +
  本机实测冻结协议，见 `docs/extension-contract.md` Skills 契约与
  `docs/evidence/extension-skills/`；实测 251 个 skill，scope 由上游如实
  上报）。旧 `skill.list` 桩（永远返回空）删除，harness RPC 落到诚实降级。
- **逐个启停**：`skills/config/write`（name/path 二选一选择器）——服务端
  写后 `forceReload` 重读复核（WRITE → REFETCH → VERIFY），复核不通过
  报 POSTCONDITION_FAILED，绝不提前报成功。
- **管理面**：搜索（名称/描述）、按来源范围筛选（用户/项目/系统带计数）、
  已停用置顶、SKILL.md 路径展示与打开、强制刷新（绕过上游缓存重扫磁盘）。
- **能力降级**：运行时无 skills RPC → 如实显示能力不可用（推荐 Codex ≥
  0.149.0-alpha.4.1）；无 Codex 运行时 → 独立降级文案。
- **网关**：`/api/extensions/skills-list`、`/api/extensions/skill-toggle`
  （跟随 active workspace cwd，同 plugin inventory）；聚合 GET 不含 skills
  块，避免扩展页每次加载白付一次扫描。
- **测试**：10 项 service 测试 + 4 项网关测试（scripted transport 用实测
  形状）；独立端口真机 E2E 通过（列表 → 停用 → 复核 → 启用，净零还原）。

[anthropics/skills]: https://github.com/anthropics/skills
[xingkongliang/skills-manager]: https://github.com/xingkongliang/skills-manager

## 2026-08-23（日）— 主题配置模块重构（next-themes 架构移植）

参照 [pacocoursey/next-themes](https://github.com/pacocoursey/next-themes) 的成熟架构，
把明暗二态切换升级为完整主题模块（原生 JS 移植，无构建依赖）：

- **首帧防闪**：`index.html` `<head>` 内联同步 bootstrap 在首绘前把存储的模式解析为
  `html[data-theme]`，浅色用户不再先看到一帧暗色；JS 被禁用时保持品牌默认暗色。
- **三态模式**：跟随系统 / 浅色 / 深色（`localStorage["boujoy-theme"]` 存模式而非解析值，
  旧存的 `light`/`dark` 语义不变；首次运行跟随 OS——OS 未明确偏好浅色时解析为暗）。
- **系统跟随**：`system` 模式下监听 `prefers-color-scheme` 变化实时切换。
- **跨标签页同步**：`storage` 事件采纳兄弟页选择、绝不回写（避免事件循环）。
- **原生控件同步**：CSS `color-scheme` + `html.style.colorScheme`（暗 `:root`/浅
  `html[data-theme="light"]`），滚动条与表单控件随主题；`meta[name=theme-color]` 与
  manifest 的 `theme_color` 对齐真实色板（`#080a0d` 暗 / `#f1eadc` 浅），且切换时动态更新。
- **入口**：主题按钮改为三态循环（◐ 跟随系统 → ☼ 浅色 → ☾ 深色，title/aria 同步）；
  命令面板提供三个显式主题命令。
- **测试**：`tests/smoke_test.py` 新增静态契约断言（bootstrap 存在、三态模块标记、
  跨标签页去回写、遗留二态初始化移除）。

## 2026-08-22（二）— P0.5 Model Provider Profiles（多 agent 并行实施）

在 P0 基础上产品化模型服务配置：用户不碰 Codex 配置文件，即可配置/验证/启用
模型服务 Provider，新会话真正绑定 Provider + Model。

- **ProviderProfileManager**（`web/provider_profile.py`）：ProfileStore（JSON、
  永不含 secret）+ CredentialStore（macOS 钥匙串；不可用时仅本次运行并明示）
  + CRUD/激活/env 注入/公开视图只回 `secretConfigured`；空 secret 保留旧值；
  内置不可删的 ChatGPT Profile（零配置沿用现有登录）。
- **Codex 集成**（协议实测驱动）：`config/value/write` upsert 注册
  ModelProviderInfo（snake_case、追加式不碰用户其它配置）；`thread/start.modelProvider`
  线程级绑定；`LAOMO_CODEX_PROVIDER_<ID>_KEY` 环境变量注入子进程；Provider 变更
  空闲时优雅重启（运行中回合绝不静默杀）；`provider test` 真实 ephemeral E2E
  （事件总线等完成，6s 实测）+ 六类错误分类。
- **会话语义**：`session.create` 绑定 providerId；`session.models` Provider 化
  投影（已绑定会话只见本 Provider 模型）；`selectModel` 跨 Provider 拦截
  （`provider-change-requires-new-session`）。
- **前端**：⚙"服务"入口 + Provider 管理 Drawer（状态徽章/编辑表单/测试连接/
  激活，错误按 outcome 映射中文，Key 安全提示）。
- **协议边界（诚实声明）**：当前 Codex `wire_api` 仅支持 `responses`——
  自定义服务必须实现 OpenAI Responses API；Chat Completions 兼容服务会被明确
  判定"协议不兼容"，不做协议转换。
- **测试**：`tests/provider_test.py` 34 项（secret 零泄漏/钥匙串零触碰 tripwire/
  注册参数断言/错误分类表/跨 Provider 拦截等）+ `tests/mock_responses_server.py`
  （最小 Responses API mock，SSE 可选）；Gate A（ChatGPT 零回归）与
  Gate B（Mock Provider 全链路：真实 Codex 请求打到 Mock、鉴权与模型正确、
  回合返回）双 PASS；smoke 16/16、codex 单测 28 保持。

## 2026-08-22（一）— P0 Clean Runtime Migration 及后续增强

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
