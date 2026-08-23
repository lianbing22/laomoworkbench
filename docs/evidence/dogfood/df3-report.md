# DOGFOOD #3 — wisp v0.3 核心加固（真实项目，重构/修复+测试性质）

Outcome: **FAILED**（单元修复超限；无数据丢失、用户 WIP 文件零触碰、
终态零线程泄漏）

- Repo: ~/Documents/wisp（main @ 4b2e892，139 tests 基线；4 个用户 WIP 脏文件列入禁触名单）
- Mission: MemorySearch / MemoryDistiller / TurnMetrics 边界测试加固 + 修复
- 参数: maxParallelWorkers=2, maxWallTimeSec=7200
- 运行: `20260823-135406-435970` — failed @ wall 2670s（单元 #2 修复 3/3 超限）
- 注: 运行尾段用户要求重启网关（1 次人工介入）；事件流证明失败 verdict
  先于重启自然发生，恢复路径干净（Gate G 行为）

## Mission Metrics

| 指标 | 值 |
| --- | --- |
| finalState | failed（repair cap） |
| totalWallTime / active / waiting / paused | 2670s / 2670s / 0 / 0 |
| unitCount / dependencyEdges | 5 / 4（3 个无依赖兄弟 + 汇总依赖全部） |
| replanCount | 0 |
| workerTurns / evaluatorTurns | 10 / 9（19 turns，最后一个被终态丢弃） |
| repairTurns / conflictResolverTurns | 6 / 0 |
| totalRepair / maxPerUnit / conflict | 6 / 3 / 0（unit0=3, unit1=3） |
| verificationRuns / finalEvaluator | 未到达（连续第二次死在机器门禁之前） |
| humanInterventions | 1（用户要求重启网关；非失败原因） |
| turn tokens | 21k–122k，全部记录（对照 #2-run1 的丢失） |

## Parallel Benefit（首次真实测量）

- Planner 产出 3 个无依赖兄弟单元 → unit0 × unit1 真实并发窗口 **948s（15.8min）**
- 粗估串行 critical path：两单元窗口 35→983 与 35→1166，重叠 948s ≈
  至少节省 ~15min（对比全串行）；maxParallelWorkers=2 名义利用率高
- 结论：**任务有自然并行结构时，调度器兑现了并行**（#2 的 0% 是任务性质）

## 定性评估

**Planner Split Quality: GOOD**
- 5 单元边界清晰（审阅基线 / 三模块各自加固 / 全量验收），禁触约束被
  planner 正确吸收进单元描述；真实并行结构 + 汇总依赖，无假拆分

**Parallel Benefit: 已证**（948s 重叠）

**Repair: 6 次全耗在 unit0/unit1，同一签名**
- 每轮 verdict 的主导理由：**"只读沙箱拒绝测试写临时目录 / 无法真实
  执行验证"** ——Swift 测试必须写 .build，评估器永远跑不了，worker 无法
  修复（其一处 verdict 甚至先肯定了"测试与修复已存在、API 未变"再打回）
- 与 #2-run2 同签名 → **系统性 P1 确认复发**（2/2 项目）

**Verification Setup Burden: LOW**
- make test 是项目自有命令，冷构建实测 19s，机器门禁完全兼容；
  禁触名单直接写进 objective 即被遵守

**Final Evaluator Calibration: N/A（未到达）**
**Unit Evaluator: TOO_STRICT（环境误归因，系统性）**

**Evidence Audit Experience: ACCEPTABLE**
- events/verdicts/state/turn-tokens 完整回答全部问题；并行重叠、
  repair 链、终态收割全部可从工件重演（本报告数据 100% 工件来源）
- 手写提取脚本的成本在上升（第 3 次）——run-summary 触发条件证据累积中

## Observed Failures → 分类

| # | 现象 | 分类 | severity |
| --- | --- | --- | --- |
| F6 | 只读评估沙箱无法执行需写盘的项目测试（pytest tmp / Swift .build），被提示词指引计为 NEEDS_WORK → repair 预算耗尽 → 死在机器门禁前（#2-run2 与 #3 连续复发） | **LAOMO PRODUCT BUG（提示词设计）** | **P1 确认复发，本轮修** |
| F7 | （对照）终态收割在真实失败中首次实战：`terminal-reap {threads:1, leaked:0}`——上午的 P1 修复被生产验证 | 修复验证 ✓ | — |
| F8 | unit0 终态时刻的 in-flight evaluator 被切，verdict 记 default-fail（"输出不可解析"）——终态竞态的诚实表现，非缺陷 | 预期行为 | — |

## Confirmed LAOMO Product Bugs（本轮）
- F6：`_phase_evaluator` 提示词原文"测试若有写行为导致失败就按 NEEDS_WORK
  记录"把评估环境的只读限制教成工作缺陷。修复方向：只读沙箱导致的
  测试不可执行不作为 NEEDS_WORK 依据；改用代码/文件证据裁决并如实备注
  "执行验证由机器验收负责"；default-fail 契约不变、三重 DONE 门不变。
