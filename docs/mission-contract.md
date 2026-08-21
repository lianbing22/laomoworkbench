# P1.1 Mission Reliability & 硬验证 接口契约

Mission 状态由老墨 Control Plane 持有（磁盘 `.laomo/runs/`），Codex 只是无状态
Worker/Evaluator。本文件是前后端与测试的三方契约。

## HTTP 端点（网关本地 /api/missions*）

- `GET /api/missions` → `{"ok":true,"missions":[{id,objective,state,phase,currentTask,
  cycles,waiting,elapsedSec,verifyResult,time{...},stopReason?,createdAt,updatedAt}],
  "activeId":"...|null"}`
- `POST /api/missions/create` `{objective, cwd?, acceptanceCriteria?: [string],
  options?{maxRepairPerTask?, maxNoProgressCycles?, maxMissionCycles?, maxWallTimeSec?},
  verification?{commands?:[string], requiredFiles?:[string], httpChecks?:[{url,expectStatus?}]}}`
  → `{"ok":true,"mission":{...}}`（初始 state=draft）
- `POST /api/missions/start` `{id}` → 调度执行（已有 active mission 时 400）
- `POST /api/missions/pause` `{id}` / `resume` / `cancel`（幂等，终态拒绝 409）
- `GET /api/missions/status?id=` → 完整状态：`{ok, mission:{..., plan:{units:[
  {index,title,description,acceptance,status,repairCount}], ...}, lastVerdict,
  lastCheckpoint, waiting:{jobId,command,startedAt,expectedWakeAt}|null,
  time:{wallElapsedMs,agentActiveMs,waitingMs,pausedMs},
  verifyResult?, jobs:[...], verification:{...latest...}, evidence:{...}|null,
  events:[...尾 40 条]}`
- 事件同时经现有 mux 事件桥以 `mission/update` 帧广播（payload `{missionId,state,...}`），
  前端可实时刷新。

## 磁盘布局（mission.cwd 下）

```
.laomo/runs/<mission-id>/
  mission.json     # objective/options/cwd/verification/createdAt（不可变字段）
  state.json       # {state, phase, currentUnit, cycles, noProgress, progressSignature,
                   #  waitingJobId, stopReason?, repairDirective?, verifyResult?,
                   #  wallElapsedMs, agentActiveMs, waitingMs, pausedMs, phaseStartedAt}
                   # —— 每次转换原子写；终态后不再变化
  plan.json        # {units:[{index,title,description,acceptance,status,
                   #   repairCount, lastVerdict?}], replans}
  events.ndjson    # {ts, type, detail} 追加式审计
  progress.md      # 人读进度（每单元 verdict 后更新）
  handoff.md       # ≤2000 字滚动交接摘要（builder 每轮输出 HANDOFF 段合并）
  checkpoints/<cycle>-<unit>.md
  evidence/        # evaluator 引用的命令输出等
  verdicts/<cycle>-<unit>.json  +  verdicts/final.json   # Final Evaluator 判定
  repairs/<cycle>-<unit>-<n>.md   # RepairDirective
  jobs/<jobId>.json + <jobId>.log # BackgroundJob 注册与日志
  verification/results.json       # 机器验收最近一次结果（覆盖写）
  verification/results-<startedAt>.json  # 带时间戳存证（不被覆盖）
  verification/raw/<kind>-<n>.stdout/.stderr # 原始输出尾部
  .laomo/index/<mission-id>.path  # run 目录索引（跨 cwd 恢复用）
```

## 状态机

`draft → planning → (per unit: running ⇄ waiting → evaluating → repairing)* →
verification → verifying → done`；任意非终态可 `paused`（resume 回到暂停前相位）；
**`blocked`/`failed`/`cancelled` 是真正的终态**——终态后 start/pause/resume/cancel
一律 409 拒绝，`recover()` 跳过，不再有任何 runner/watcher 线程。

- **planning**：Planner turn 产 plan.json（≥1 work unit，每个含 acceptance）
- **running**：Worker turn（新鲜线程）只做当前 unit
- **waiting**：Worker 注册了 BackgroundJob；JobWatcher 唤醒后回 running（带 delta）
- **evaluating**：Evaluator turn（新鲜线程 + readOnly 沙箱）
- **repairing**：NEEDS_WORK/机器验收未过 → RepairDirective 再 Worker turn
- **replanning**：全单元后仍 blocked/或 planner 补充缺口（replans 计入 StopPolicy）
- **verification**：Harness 机器验收（纯机器，无模型轮）
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
BLOCKED 终态同理 fail-closed：blocked 是终态，不可通过 resume 复活。

## BackgroundJob 生命周期（Control Plane 全权持有）

- 注册字段：`jobId/pid/pgid/command/cwd/logPath/startedAt/expectedWakeAt/
  completionCondition/unitIndex/status/startIdentity/commandHash`
  （`startIdentity` = `ps -o lstart=` 启动时刻；`commandHash` = sha256(command)[:16]；
  `pgid = pid`，spawn 时 `start_new_session=True`，作业自成会话）
- Control Plane 自己 spawn（stdout/stderr → logPath），Worker 的 turn 已结束——
  **模型不轮询**。
- JobWatcher 线程每 2s 探测：进程已死（ps 无此 pid 或 state=Z）即 wake；超过
  `expectedWakeAt` + 300s 缓冲也 wake（delta 标注超时）。
- **进程身份契约**：attach/kill/恢复前先做身份校验——`state=Z 或空` 视为已死；
  `pgid` 不一致或 `lstart` 与 `startIdentity` 不一致视为 **PID 复用**，绝不 kill
  陌生进程，作业标记 `orphaned(orphanReason=pid-reused|dead|gone|no-pid)` 并立即
  wake mission。
- **作业终态**：`running / completed / failed(exitCode) / cancelled / orphaned`。
  终结判定：`completed` 仅在**已知退出码为 0** 时写入；退出码用 shell 约定
  （正常为 0，被信号杀死为 `128+signal`，如 SIGKILL=137）。Control Plane 重启
  后重挂的 watcher 没有 Popen 句柄，用 `waitpid` 收取真实退出码（本进程仍是
  父进程）；若进程不是本进程的孩子（控制面迁移/身份断裂），退出码诚实记为
  `null` 且 `exitUnknown=true`，**绝不**当成成功——未知退出一律 `failed`。
- **cancel → 终止**：先停并 join 该 mission 的 watcher（防止 watcher 抢先写状态），
  然后逐作业 SIGTERM → `JOB_TERMINATE_GRACE=6s` → 仍未死则 SIGKILL；结果写
  `terminateMode: term|kill`，作业标记 `cancelled`。**被取消的 mission 不得留下任何
  存活托管作业**。

## waiting 的 pause/resume 语义

- `pause`（waiting 中）：**不 kill 后台作业**——仅仅停 runner/watcher。作业继续
  跑；paused 期间无任何线程探测，不会有自动推进（app 线程/模型轮全部停摆）。
- `resume`（回到 waiting）：若作业仍存活 → **原样重挂 watcher（attach-on-resume）**，
  作业的 pid 不变；若作业在 pause 期间已被杀掉 → 立即标记 `orphaned` 并唤醒
  mission（delta 注明），绝不干等一个不会到来的 wake。
- 恢复前同样过身份校验：PID 复用或已死均按上节处理。

## 崩溃恢复（进程身份 + PID 复用检测）

- 网关重启后第一次 `/api/missions` 触发 `recover()`：扫描 `.laomo/runs/` 与
  index，对每个非终态 mission 重建 MissionRunner。
- **waiting 分支**：作业仍存活（身份校验通过）→ 新建 JobWatcher 重挂 + 重跑
  runner，事件 `recover/waiting: job alive, rewatching`；作业已死（含 PID 复用）
  → 标记 `orphaned`、清 `waitingJobId`、回 running 并带 delta 唤醒，交给 Worker
  修复。
- **paused 分支**：跳过（保持 paused）；resume 由用户触发。
- **终态分支**：跳过——不会复活。

## StopPolicy（默认，create 可覆盖）

```
maxRepairPerTask=3    # 单元修复超限 → 该单元 failed → mission failed
maxNoProgressCycles=2 # 一个完整循环后 (单元状态图+checkpoint hash) 无变化 → failed
maxMissionCycles=40   # planner/worker 轮次总数
maxWallTimeSec=14400  # 对照 wallElapsedMs 检查
```

**时间预算（4 桶）**：`wallElapsedMs = agentActiveMs + waitingMs`；`pausedMs` 在
pause→resume 边界入账，**暂停时间不消耗预算**；waiting 消耗预算（mission 仍在
推进——作业在跑）。`maxWallTimeSec` 只对照 wallElapsedMs：暂停一个 mission 是真正
冻结它的墙钟预算，等待后台作业不是。
**无进展签名**：`state.json` 持久化 `noProgress` 计数器与 `progressSignature`
（单元状态图+checkpoint hash）；崩溃/重启后计数延续，不会清零重试。

## Harness Verification Gate（机器验收，Control Plane 执行）

- `verification = {commands:[string], requiredFiles:[string],
  httpChecks:[{url, expectStatus?}]}`；`VerificationRunner` 纯机器执行，无需任何
  模型轮：commands 经 `/bin/zsh -lc`（`VERIFY_CMD_TIMEOUT=120s`），requiredFiles
  做存在性检查，httpChecks 用 urllib 按 expectStatus（默认 200）。
- 每个检查结果带完整字段并落盘到 `verification/results.json`（覆盖写）**以及**
  带时间戳的 `results-<startedAt>.json`（存证不被后续覆盖）；原始输出尾部存
  `verification/raw/`：
  `{kind, name, passed, command?, exitCode?, stdoutTail, stderrTail,
  startedAt, endedAt, resultHash?}` / `{kind:"file", error:"missing"}` /
  `{kind:"http", statusCode, expectStatus}`。
- **fail-closed**：任一检查未过 → `state.verifyResult=fail`，产生 repairDirective
  （"Harness 机器验收未通过…"）打回 repairing；机器门禁永远失败时 mission 最终
  `failed`，**绝无 DONE**。

## DONE 三重条件（缺一不可，机器证据）

1. 全部 units = passed（各单元 Evaluator PASS）
2. Harness 机器门禁 PASS（verifyResult=pass）
3. Final Evaluator（fresh 线程）PASS → `verdicts/final.json`

任何 Builder 的自述"完成"无效力；verifyResult != pass 时不允许进入 final
evaluator（verifying 会直接打回 verification）。

## Evidence Manifest（证据清单，DONE 后不可变）

`_transition` 到 done 且状态落盘后立即在 `evidence/manifest.json` 生成：
`{missionId, verdict, verification{...}, entries:[{path, sha256, generatedAt}],
 artifacts, git: {diffSummary}, generatedAt}`——列出 verdict、verification
 输出、checkpoint、git diff 摘要与 artifacts，每条带相对 path + sha256 +
 generatedAt。**manifest 是 DONE 的机器证据；DONE 后不可变**（已存在则直接返回，
不重写）。

## 前端（Agent F 范围，最小改造）

现有信号卡"当前目标"区域升级：存在 mission 时显示 Mission 视图——
阶段徽章、当前 Task 标题、循环次数、等待状态（含后台作业命令）、最近
verdict（PASS/NEEDS_WORK/BLOCKED 徽章）、最后 checkpoint 摘要、运行时长；
操作：启动/暂停/继续/取消；goal 对话框加"作为 Mission 运行（持久循环）"开关。
不做 DAG UI。订阅 `mission/update` 帧实时刷新。

## 测试（Agent T 范围）

tests/mission_test.py（90 用例）：FakeAdapter（run_turn 可编程脚本化返回/标记块
注入/超时模拟）覆盖：状态机全路径、default-fail（无可解析 verdict）、repair 上限
熔断、no-progress 熔断、墙钟熔断、pause/resume/cancel 幂等、waiting 注册与 wake
delta、crash-resume（重建 MissionManager 后 waiting 恢复）、DONE 三重条件、以及
P1.1 新增：blocked 终态、作业生命周期全权（cancel 终止+真死/终态、failed exitCode、
paused 期死亡 → orphaned）、PID 复用检测、四桶时间账、no-progress 重启延续、
机器验收门禁（失败无 DONE/每字段存证/HTTP 检查）、evidence manifest（sha256+
不可变）、关停门禁（终态拒绝一切动作）。不得触碰真实 codex（真实验证由主线程
Gate 负责）。
