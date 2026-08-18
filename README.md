<div align="center">

# Boujoy Harness

## 把 Agent 从聊天框里拽出来，接进你的本地工作区。

一个基于 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的本地 Agent 工作台：保留上游 Harness 的事件与 RPC 协议，把它装进有任务、对话、知识库和运行信号的 Boujoy 界面。

**不是又一个聊天壳。是让 Agent 读得懂工作区、跑得住长任务、还能回到你手上的本地工作台。**

[English](README_EN.md) · [观看完整演示](https://github.com/asen-goat-mine/boujoy-harness/releases/download/demo-2026-08-19/Boujoy-Harness-Demo.mp4) · [DeepSeek Harness 上游项目](https://github.com/deepseek-ai/deepseek-harness)

</div>

<p align="center">
  <a href="https://github.com/asen-goat-mine/boujoy-harness/releases/download/demo-2026-08-19/Boujoy-Harness-Demo.mp4">
    <img src="docs/assets/harness-demo.gif" alt="Boujoy Harness UI 动态演示。点击观看完整视频。" width="900">
  </a>
</p>

<p align="center"><sub>README 内自动播放 UI 演示；点击即可打开完整 49 秒视频。</sub></p>

## 它解决什么

DeepSeek Harness 是强大的 Agent runtime，但它原生更像一台裸引擎：模型、工具和事件流已经就绪，长期资料、工作区和桌面体验还需要自己拼。

Boujoy Harness 做的是中间这层产品化工作：

1. **不替换 Agent runtime。** DeepSeek Harness 仍负责模型、工具、事件帧和 RPC。
2. **让工作上下文留在本地。** 可连接一个 Markdown Vault，让项目卡、知识卡、提示词和资料仍是普通文件。
3. **把长对话做得能用。** 历史分页、流式投影、滚动稳定和断线恢复边界，减少长任务时界面乱跳、抢滚动条或吞掉上文。
4. **让本地启动可控。** macOS 有原生宿主与受控重启；Windows 提供浏览器宿主适配器（Beta）。

> 源代码仓库不提供模型，也不附带 DeepSeek Harness 运行时；它不会包含你的 Vault、会话或凭据。便携包是否包含运行时取决于发布者在对应平台上完成的打包与验收。

## 核心能力

| 能力 | 你会感受到什么 |
| --- | --- |
| 原生 Agent 连接 | 保持 DeepSeek Harness 的 WebSocket、事件帧与 RPC 语义，不重新发明不兼容的 Agent 协议。 |
| 本地 Markdown 工作区 | 项目、知识、提示词与内容资料都能留在你拥有的文件夹，而不是锁进云端数据库。 |
| 对话稳定性 | 长历史按页加载，流式文本与用户滚动分离，减少生成时抢滚动、闪烁和旧消息消失。 |
| 任务与中断交互 | 对需要确认、输入或批准的 Agent RPC 做队列化处理；过期响应会收口，弹窗不会永久卡住。 |
| 本地优先 | 未配置访问码时仅绑定本机回环地址；macOS 手机配对可启用受访问码保护的局域网访问；没有遥测。 |
| 可恢复启动 | 对启动健康检查、App Translocation、路径选择和可选知识服务缺失做降级处理。 |
| 跨平台路线 | macOS 13+ Apple Silicon 原生桌面宿主；Windows 10/11 x64 为浏览器宿主 Beta。 |

## 工作方式

~~~text
你的一句话任务
        │
        ▼
Boujoy UI ── 本地网关 ── DeepSeek Harness ── 你配置的模型 / 工具
        │
        └────────────── 本地 Markdown Vault
                             项目 · 知识 · 提示词 · 内容
~~~

- **DeepSeek Harness** 负责让 Agent 真正行动。
- **Boujoy Harness** 负责让行动有工作区、有可视化、有可恢复的桌面体验。
- **Markdown Vault** 负责把值得长期复用的上下文留在你自己的文件里。

## 5 分钟从源码启动（macOS）

### 前置条件

- macOS 13+，Apple Silicon（arm64）
- 已单独安装并从源码构建好 DeepSeek Harness；需要存在可执行的 node_modules/.bin/dsh
- 一个本地 Markdown Vault 目录
- 可用的 Python 3

### 构建

~~~bash
git clone https://github.com/asen-goat-mine/boujoy-harness.git
cd boujoy-harness

# 指向你自己的、本机上的依赖；不要把这些值提交进 Git。
export BOUJOY_DSH_ROOT="$HOME/src/deepseek-harness"
export BOUJOY_VAULT_DIR="$HOME/BoujoyVault"
export BOUJOY_PYTHON_BIN="$(command -v python3)"

./macos/build-app.command --install
~~~

构建完成后，应用会安装到桌面上的 Boujoy Harness.app。首次启动后：

1. 在左侧选择或创建一个工作区。
2. 选择知识模式时，连接你自己的 Markdown Vault；选择纯净模式时，只运行 Harness，不读取 Vault。
3. 在输入框描述任务；模型、Provider 与工具权限仍由你已经配置好的 DeepSeek Harness 决定。
4. Agent 请求确认或输入时，使用弹窗继续；如果请求已经超时或被取消，界面会自动收口并进入下一项。

### 日常使用建议

- 将稳定项目资料放进 Vault，而不是只留在聊天记录里。
- 需要延续项目时，先让 Agent 读取当前项目上下文，再开始具体任务。
- 长任务生成时可以自由上翻查看历史；只有当你位于底部并主动跟随时，界面才会自动滚到底部。
- 不确定某次运行是否完成时，看右侧运行记录和状态，而不要根据输入框是否仍显示加载状态猜测。

## 知识模式与纯净模式

| 模式 | 适合什么 | 不会做什么 |
| --- | --- | --- |
| 知识模式 | 有项目背景、文档、提示词或历史决策需要复用的任务 | 不会把整个 Vault 无差别塞给模型；应由索引和相关卡片按需提供上下文。 |
| 纯净模式 | 临时问答、实验、无关任务或你不希望使用工作资料的场景 | 不会读取你的 Markdown Vault。 |

公开源码不附带个人 Vault。你可以从空目录开始，也可以连接自己的本地 Markdown 知识库。

## 便携包与 App Translocation

macOS 对从 ZIP 直接打开的未签名 App 可能启用 App Translocation：系统会把 App 放进临时、只读目录，导致它看不到同级的 vault 和 runtime。

因此，便携包必须保持完整目录结构，并双击包根目录的 启动 Boujoy Harness.command，而不是直接双击 App：

~~~text
你的便携包/
├── Boujoy Harness.app
├── runtime/
├── vault/
└── 启动 Boujoy Harness.command   ← 从这里启动
~~~

启动器会把包根目录显式传给 App。若用户仍直接打开 App，Boujoy 会识别异常启动路径并引导选择正确目录，而不是把临时系统路径暴露出来。

这是未签名分发的兼容处理；面向普通用户的正式 macOS 发布，仍建议使用 Apple Developer ID 签名与 notarization。

## Windows 适配器（Beta）

Windows 版本保留同一套 Web UI，但用本地 PowerShell 服务宿主，并在可用时以 Edge 应用模式打开。

它目前是 **Windows 10/11 x64 Beta**：

1. 必须在真实 Windows x64 机器上执行 windows/Prepare-Windows-Runtime.ps1，准备该平台对应的 DeepSeek Harness runtime。
2. 不可以把 macOS 的 runtime 直接复制到 Windows；其中存在平台原生依赖。
3. 使用 windows/Start-Boujoy.ps1 启动；站内重启会通过本地重启信号交回宿主处理。
4. 详情见 [Windows 说明](windows/README-Windows.zh-CN.md) 与 [发布状态](windows/WINDOWS-RELEASE-STATUS.md)。

## 常见问题

### 为什么应用提示缺少运行组件？

先确认本机的 DeepSeek Harness、Vault 和 Python 路径都真实存在。若你使用下载的便携包，请从 启动 Boujoy Harness.command 启动，而不要直接打开 App。

### 为什么启动页停留较久？

首次运行需要启动本地网关和 Harness。Boujoy 会等待健康检查，而不是盲目加载尚未就绪的页面。若最终失败，请检查本地 runtime、Python 和 Provider 配置，而不是反复刷新浏览器。

### 为什么 Agent 没有回复？

Boujoy 不托管模型余额或 API Key。请从 DeepSeek Harness 本身检查模型 Provider、余额、网络、权限与运行日志。

### 知识库预览不可用会影响聊天吗？

不会。知识预览是可选服务；缺失时主 Agent 界面应继续可用。知识模式能否提供上下文，取决于你的 Vault 与 Harness 配置。

### 这是 DeepSeek 官方产品吗？

不是。Boujoy Harness 是独立的非官方开源产品层，不受 DeepSeek AI 支持或背书。

## 隐私与网络边界

- Vault 内容、会话状态和凭据留在你的本机；本仓库不会包含这些数据。
- 未提供访问码时，本地网关只绑定 127.0.0.1；macOS 手机配对会启用受访问码保护的局域网访问。
- 模型请求可经本机网关转交给你自行配置的 DeepSeek Harness 或 Provider；Boujoy 不运营远端中转，也不以 Boujoy 服务的形式持久化 API Key。
- AI 新闻页面会请求 web/boujoy_server.py 中列出的公开 RSS；Boujoy 不配置分析或遥测端点。
- 永远不要提交 boujoy-config.json、Vault、会话、凭据、生成的 dist App 或平台 runtime。

详细安全说明见 [SECURITY.md](SECURITY.md)。

## 验证与开发

不需要模型账户即可运行静态 smoke test：

~~~bash
env PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke_test.py --skip-live
~~~

若本机已有正在运行的实例，可额外执行：

~~~bash
python3 tests/smoke_test.py --live-origin http://127.0.0.1:8766
~~~

测试会验证网关契约、路径边界、访问控制和便携运行时归一化；不会调用模型，也不会消耗余额。

## 仓库内容

~~~text
macos/      macOS 原生 WKWebView 宿主与构建脚本
web/        本地网关、Boujoy UI 与资源
windows/    Windows 浏览器宿主 Beta 脚本与说明
tests/      不依赖模型的 smoke test
assets/     Boujoy 一方拥有的图标、字体归属与视觉资源
~~~

## 许可与致谢

Boujoy 自研代码与图形使用 [MIT License](LICENSE) 发布。DeepSeek Harness 是独立的 MIT 依赖，适用其自身的许可证与声明；字体归属与第三方信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
