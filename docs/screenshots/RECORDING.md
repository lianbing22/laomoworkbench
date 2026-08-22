# 视觉素材规范 & Hero Demo 录制分镜

本目录（`docs/screenshots/`）是 README 引用的唯一截图来源。所有素材必须来自**真实运行的产品**，不允许拼摆拍、不允许给未实现的能力配图。

## 命名约定

```text
docs/screenshots/
├── agent-page.jpg             # Agent 工作台全景
├── knowledge-page.jpg         # 知识库页
├── monitor-page-light.jpg     # 监控页（亮色）
├── monitor-page-dark.jpg      # 监控页（暗色）
├── news-page.jpg              # AI 新闻页
├── mission-loop-planning.jpg  # Mission：规划/拆单元
├── mission-loop-running.jpg   # Mission：Worker 执行中
├── mission-loop-repair.jpg    # Mission：NEEDS_WORK → 修复中
├── mission-loop-done.jpg      # Mission：DONE + Final Evaluator PASS
├── mission-events.jpg         # Mission 事件审计弹窗
├── provider-list.jpg          # 模型服务列表
├── provider-form.jpg          # 新建/编辑模型服务表单
└── hero-mission.gif           # ★ 首屏演示（待录制，见下方分镜）
```

约定：

- 截图统一 1440×900 视口，浏览器直出，不做修饰。
- 明暗主题：产品默认主题为主；监控页保留明暗两张。
- 单张 ≤ 500KB；超过就用 `sips -s format jpeg -s formatOptions 80` 压一道。
- GIF 是特例放这里；MP4 不进仓库，作为 Release asset 外链。

## Hero Mission Demo 分镜（25 秒）

录制前检查：Dock/图标/关于页不能残留 Boujoy 痕迹（换牌收口后再录，避免重录）。

| 时间 | 画面 | 说明 |
| --- | --- | --- |
| 00–03s | Agent 页输入目标：「完成这个项目的登录模块并自行测试修复」 | 一句话任务 |
| 03–07s | Mission 卡片进入「规划中」，计划条出现 Unit 1/2/3 | 自动拆分 |
| 07–12s | 「执行中」+ 运行记录面板实时展开 Shell/文件修改 | Codex Worker 干活 |
| 12–15s | 后台作业 WAITING → 自动 WAKE | LAOMO_JOB 协议 |
| 15–19s | Evaluator 判 NEEDS_WORK → 卡片切「修复中」 | 验收不放过 |
| 19–23s | 终验中 → Machine Verification PASS | 可信 DONE |
| 23–25s | Final Evaluator PASS，卡片「已完成」，判定行 PASS 通过 | 循环闭环 |

录制方式任选：

1. `tab.recording.start()` 输出 WebM → `ffmpeg -i demo.webm -vf "fps=12,scale=1200:-1:flags=lanczos" -loop 0 hero-mission.gif`
2. macOS 截屏录制 → 同上转 GIF。帧率 10–14fps，宽度 ≤ 1200px，体积控制在 **≤ 6MB**（当前旧 GIF 7MB 偏大，不要超）。

## 替换规则

- `hero-mission.gif` 就位后，替换两份 README 首屏 `<img>` 的 `src` 即可，其余结构不动。
- 任何一张界面截图与当前版本 UI 不符时，直接重拍覆盖同名文件，README 不需要改。
