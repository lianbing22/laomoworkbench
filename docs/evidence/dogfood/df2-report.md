# DOGFOOD #2 — CodexPlusPlus stats 子命令（真实项目，新增功能性质）

Outcome: **FAILED**（两次运行均未到 DONE；无数据丢失、用户 checkout 零污染、
无错误 Git 集成、无取消后写入）

- Repo: ~/Desktop/CodexPlusPlus（main @ 9a84e83，215 tests 基线，干净 checkout）
- Mission: `codex_session_delete stats` 子命令（会话统计 + --json）
- 参数: maxParallelWorkers=2, maxWallTimeSec=7200
- 运行 1: `20260823-073955-5c03aa` — failed @ wall 8160s（maxWallTime）
- 运行 2: `20260823-125910-f4e31e` — failed @ wall 2788s（单元修复超限 3/3）

## Mission Metrics（两次运行）

| 指标 | run1 | run2 |
| --- | --- | --- |
| finalState | failed (maxWallTime) | failed (repair cap) |
| totalWallTime | 8160s (+僵尸 10320s) | 2788s |
| agentActive / waiting / paused | 8160 / 0 / 0 | 2788 / 0 / 0 |
| unitCount / dependencyEdges | 4 / 3（全串行链） | 4 / 3（全串行链） |
| replanCount | 0 | 0 |
| workerTurns | 2（planner 外）| 4 |
| evaluatorTurns | 1 | 4 |
| repairTurns / conflictResolverTurns | 0 / 0 | 3 / 0 |
| totalRepair / maxPerUnit / conflict | 0 / 0 / 0 | 3 / 3 / 0 |
| verificationRuns / firstPass | 未到达 | 未到达 |
| finalEvaluator | 未到达 | 未到达 |
| humanInterventions | **1**（重启网关部署 P1 修复；两轮间） | **0** |
| turn tokens | 21k/132k/95k + 丢失 | 18k…135k，9 turns 全记录 |

## 定性评估

**Planner Split Quality: ACCEPTABLE**（两次）
- 4 单元、边界大体合理（契约→实现→CLI→测试），无假并行、无拆分表演
- 缺陷：run2 单元 0 的验收把"整体闭环（CLI 注册）"塞进了契约单元——
  单元边界吸收了 mission 级目标（verdict 3-0 原话"整体目标尚未闭环"）

**Parallel Benefit: 0%（真实）**
- 两次 plan 都是纯串行 DAG；maxParallelWorkers=2 从未产生重叠
- 这是任务性质（单一子命令 feature）而非调度失败；不得宣称并行收益

**Repair: 3 次全耗在单元 0，原因三类**
- CODE_DEFECT（真）: 文件名时间回退边界缺失；rglob 误扫 backup 目录
- ENVIRONMENT 误归因: 4 次 verdict 里 3 次引用"只读沙箱无 tmp 目录 /
  缺 requests 依赖，pytest 无法运行"——评估环境不可跑该项目的测试，
  worker 无法修复，却持续消耗 repair 预算（run2 失败主因之一）
- BAD_UNIT_BOUNDARY: 契约单元被要求对 mission 闭环负责

**Verification Setup Burden: MEDIUM**
- pytest 作为机器门禁显而易见，但"验收在 integration worktree 里测的是
  worktree 代码而非用户 editable 安装"这一隐患需要 copy+canary 实验才能
  确认（naive 操作者不会察觉）
- Mission 侧：AC 要求 tmp-fixture pytest → 与只读评估沙箱天然冲突
  （dogfood 驱动侧教训：验收标准必须评估沙箱可执行）

**Final Evaluator Calibration: N/A（未到达终评）**
**Unit Evaluator: 偏严（TOO_STRICT 倾向）**
- 真缺陷抓得对（rglob/文件名回退）；但把自身环境限制（无 tmp、缺依赖）
  按提示词指引记为 NEEDS_WORK 理由——fail-closed 指令被忠实执行，
  产物是"不可修复的债"循环消耗预算

**Evidence Audit Experience: ACCEPTABLE**
- events.ndjson/verdicts/state 完整回答了"发生了什么"（本报告全部结论
  来自工件而非印象）；run1 僵尸 turn 的 elapsed/tokens 丢失（P2 债）
- 痛点：需要手写 python 逐事件拼时间线（run-summary 提取器候选证据 #1）

## Observed Failures → 分类

| # | 现象 | 分类 | severity |
| --- | --- | --- | --- |
| F1 | run1 单元 1 turn 病理 5h（07:50→12:48），无 commit，仅写未提交文件 | UPSTREAM/MODEL 循环 or 完成事件丢失（E2 债阻断确诊）| P2 观测 |
| F2 | run1 failed 后僵尸 turn 续跑 2h52m、向死亡 mission worktree 写文件 | **LAOMO PRODUCT BUG**（终态不收割 unit 线程） | **P1 已修** `16f24a5` |
| F3 | run2 评估环境跑不了项目 pytest（无 tmp + 缺 requests），3/4 verdict 引用 | HARNESS 设计缺口（评估沙箱能力 × 提示词 fail-closed 指引） | **P1 记录待设计决策** |
| F4 | run2 单元 0 验收含 mission 级闭环要求 | PLANNER 边界缺陷（偶发，能完成任务的范畴） | P2 |
| F5 | run1 僵尸 turn 的 elapsed=0/tokens=None，正文丢失 | HARNESS 证据缺口（与已知 default-fail 同族） | P2 |

## Confirmed LAOMO Product Bugs
- F2（P1）已修复并回归锁定（test_50：无修复 FAIL/有修复 PASS，218/218，
  CI 绿）：终态收割 in-flight unit turn（镜像 Gate F 定向 interrupt + 有界
  join + terminal-reap 审计事件）

## Deferred UX/Design Debt（不修，进 taxonomy）
- F3 评估沙箱 × fail-closed 指引的设计张力（改提示词有 TOO_LENIENT 漂移
  风险，需设计决策）
- F5 丢弃 turn 的证据保存
- run-summary 只读提取器：两次运行已产生真实重复提取成本（候选触发条件
  记录，等 #3 结束后综合判断）
