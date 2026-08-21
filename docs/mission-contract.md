# P0.6 Durable Mission Loop 接口契约

Mission 状态由老墨 Control Plane 持有（磁盘 `.laomo/runs/`），Codex 只是无状态
Worker/Evaluator。本文件是前后端与测试的三方契约。

## HTTP 端点（网关本地 /api/missions*）

- `GET /api/missions` → `{"ok":true,"missions":[{id,objective,state,phase,currentTask,
  cycles,waiting,elapsedSec,createdAt,updatedAt}], "activeId": "...|null"}`
- `POST /api/missions/create` `{objective, cwd?, acceptanceCriteria?: [string], options?{
  maxRepairPerTask?, maxNoProgressCycles?, maxMissionCycles?, maxWallTimeSec?}}`
  → `{"ok":true,"mission":{...}}`（初始 state=draft）
- `POST /api/missions/start` `{id}` → 调度执行（已有 active mission 时 400）
- `POST /api/missions/pause` `{id}` / `resume` / `cancel`（幂等，终态拒绝）
- `GET /api/missions/status?id=` → 完整状态：`{ok, mission:{..., plan:{units:[
  {index,title,description,acceptance,status,repairCount}], ...}, lastVerdict,
  lastCheckpoint, waiting:{jobId,command,startedAt,expectedWakeAt}|null,
  events:[...尾 40 条]}`
- 事件同时经现有 mux 事件桥以 `mission/update` 帧广播（payload `{missionId,state,...}`），
  前端可实时刷新。

## 磁盘布局（mission.cwd 下）

```
.laomo/runs/<mission-id>/
  mission.json     # objective/options/cwd/createdAt（不可变字段）
  state.json       # {state, phase, currentUnit, cycles, noProgress, activeSince,
                   #  waitingJobId, stopReason?} —— 每次转换原子写
  plan.json        # {units:[{index,title,description,acceptance,status,
                   #   repairCount, lastVerdict?}], replans}
  events.ndjson    # {ts, type, detail} 追加式审计
  progress.md      # 人读进度（每单元 verdict 后更新）
  handoff.md       # ≤2000 字滚动交接摘要（builder 每轮输出 HANDOFF 段合并）
  checkpoints/<cycle>-<unit>.md
  evidence/        # evaluator 引用的命令输出等
  verdicts/<cycle>-<unit>.json
  repairs/<cycle>-<unit>-<n>.md   # RepairDirective
  jobs/<jobId>.json + <jobId>.log # BackgroundJob 注册与日志
```

## 状态机

`draft → planning → (per unit: running ⇄ waiting → evaluating → repairing)* →
verifying → done`；任意非终态可 `paused`（resume 回到暂停前相位）；
`blocked/failed/cancelled` 为终态；StopPolicy 触发 → `failed(stopReason)`。

- **planning**：Planner turn 产 plan.json（≥1 work unit，每个含 acceptance）
- **running**：Worker turn（新鲜线程）只做当前 unit
- **waiting**：Worker 注册了 BackgroundJob；JobWatcher 唤醒后回 running（带 delta）
- **evaluating**：Evaluator turn（新鲜线程 + readOnly 沙箱）
- **repairing**：NEEDS_WORK → RepairDirective 再 Worker turn（unit.repairCount+1）
- **replanning**：全单元后仍 blocked/或 planner 补充缺口（replans 计入 StopPolicy）
- **verifying**：Final Evaluator（全部 AC + 回归，fresh 线程）三重条件全过才 done

## Worker/Evaluator 线程协议（提示词契约）

**Builder prompt**（新鲜线程，逐单元）：goal + 当前 unit(title/desc/AC) + handoff.md
全文 + delta（自上次 wake：作业结果/上次 verdict.repair）。要求：只做当前单元；
>20s 的命令不得等待，在回复末尾输出：

```
<<<LAOMO_JOB
{"command":"...","cwd":"...","reason":"...","expectedSeconds":600}
LAOMO_JOB>>>
```

然后结束本轮。完工输出含 `HANDOFF:` 段（≤300 字交接摘要）。

**Evaluator prompt**（新鲜线程，sandboxPolicy=readOnly，approvalPolicy=never）：
只读工作区与 evidence/，按 AC 判定，末尾必须输出：

```
<<<LAOMO_VERDICT
{"verdict":"PASS|NEEDS_WORK|BLOCKED","reasons":["..."],
 "repair":"NEEDS_WORK 时必填的具体修复指令"}
LAOMO_VERDICT>>>
```

**default-fail 契约**：标记块缺失/不可解析/超时/线程错误 → 一律按
`NEEDS_WORK(reasons:["evaluator 输出不可解析"])` 或 `failed` 处理，绝不默认通过。

## BackgroundJob / WaitRegistry / JobWatcher

- 注册字段：`jobId/pid/command/cwd/logPath/startedAt/expectedWakeAt/completionCondition`
- Control Plane 自己 spawn（`subprocess.Popen`，stdout/stderr → logPath），Worker 的
  turn 已结束——**模型不轮询**。
- JobWatcher 线程每 2s 查 `os.kill(pid,0)`（OS 级检测，允许）；退出即 wake；
  超过 `expectedWakeAt` + 300s 缓冲也 wake（delta 标注超时，default-fail 交给
  下一轮 evaluator 判定）。
- wake delta = exit code + log 尾部 80 行（截断，不整灌旧日志）。
- crash-resume：waiting 态恢复时重查 pid；进程已消失 → 直接 wake（delta 注明
  "网关重启期间作业已结束"）。

## StopPolicy（默认，create 可覆盖）

```
maxRepairPerTask=3    # 单元修复超限 → 该单元 failed → mission failed
maxNoProgressCycles=2 # 一个完整循环后 (单元状态图+checkpoint hash) 无变化 → failed
maxMissionCycles=40   # planner/worker 轮次总数
maxWallTimeSec=14400  # activeSince 累计墙钟（paused 不计时）
tokenBudget?          # 观测到 tokenUsage 时生效（可选）
```

DONE 三重条件（缺一不可）：全部 units=passed + Final Regression PASS
（verifying 首轮）+ Final Evaluator PASS。Builder 自述"完成"无任何效力。

## 前端（Agent F 范围，最小改造）

现有信号卡"当前目标"区域升级：存在 mission 时显示 Mission 视图——
阶段徽章、当前 Task 标题、循环次数、等待状态（含后台作业命令）、最近
verdict（PASS/NEEDS_WORK/BLOCKED 徽章）、最后 checkpoint 摘要、运行时长；
操作：启动/暂停/继续/取消；goal 对话框加"作为 Mission 运行（持久循环）"开关。
不做 DAG UI。订阅 `mission/update` 帧实时刷新。

## 测试（Agent T 范围）

tests/mission_test.py：FakeAdapter（run_turn 可编程脚本化返回/标记块注入/
超时模拟）覆盖：状态机全路径、default-fail（无可解析 verdict）、repair 上限
熔断、no-progress 熔断、墙钟熔断、pause/resume/cancel 幂等、waiting 注册与
wake delta、crash-resume（重建 MissionManager 后 waiting 恢复）、DONE 三重
条件（builder 自称完成但 evaluator FAIL 不进 done）、plan.json/handoff/checkpoint
落盘形状。不得触碰真实 codex（真实验证由主线程 Gate 负责）。
