# DOGFOOD #3 — wisp v0.3 核心加固（真实项目，重构/修复+测试性质）

Outcome: **PASS**（第三次运行 DONE；前两次失败各暴露并修复一个真实产品 bug）

- Repo: ~/Documents/wisp（main @ 4b2e892，139 tests 基线；4 个用户 WIP 脏文件列入禁触名单）
- Mission: MemorySearch / MemoryDistiller / TurnMetrics 边界测试加固 + 修复
- 参数: maxParallelWorkers=2, maxWallTimeSec=7200
- run1 `20260823-135406-435970` — failed @ 2670s（repair cap → 暴露 F6）
- run2 `20260823-144924-645963` — failed @ 917s（provider restart 杀 turn → 暴露 F9）
- **run3 `20260823-151550-91044d` — DONE @ 4252s**（两个修复生效后）

## run3 最终指标（DONE）

| 指标 | 值 |
| --- | --- |
| finalState | **done**（verifyResult=pass + final PASS） |
| wall / agentActive / waiting / paused | 4252s / 4200s / 52s（make test 后台作业）/ 0 |
| unitCount / dependencyEdges | 5 / 4（3 无依赖兄弟 + 汇总） |
| replanCount | 0 |
| workerTurns+evaluatorTurns | 24 turns，**1,847,141 tokens** 全程记录 |
| repairTurns / totalRepair / perUnit | 2 / 2 / 0-1-0-1-0 |
| conflictResolverTurns / conflict | 0 / 0 |
| verificationRuns / firstPass | 1 / **PASS**（make test + 禁触 diff 双命令全过） |
| finalEvaluatorVerdict | **PASS**（"基线139+新增21=160 全过；三模块边界覆盖完整并修复真实缺陷；公开API未变；保护文件未触碰；diff 仅授权范围"） |
| humanInterventions | 0（run3 全程零介入） |
| **parallelOverlap** | **859s**（unit1×unit2，worker 窗口 519→1378/1523） |
| 交付 | 集成分支 +399/-11：MemorySearchTests 103 行 + CoreV3 +126 行 + VoiceMemoryTests +130 行（21 个新测试）+ MemorySearch/TurnMetrics/MemoryDistiller 真实缺陷修复；用户 checkout 零污染（仅原有 4 个 WIP 文件 + .laomo） |

## 并行收益（run3 实测）

- unit1 × unit2 真实并发 **859s（14.3min）**；两单元窗口 519→1378 与 519→1523
- 串行执行同两单元需 859+1004=1863s，实际 1004s → **节省约 14 分钟（省 ~46%）**
- run1 的 948s 重叠 + run3 的 859s：两次可测，调度器在有自然并行结构的任务上稳定兑现

## 定性评估（以 run3 为准，run1/2 记录为失败样本）

**Planner Split Quality: GOOD**（三次运行一致：边界清晰、禁触约束被吸收、真实并行结构、无假拆分）

**Repair: 健康**——run3 全程仅 2 次 repair，且 NEEDS_WORK 理由是**真实缺陷**
（TurnMetrics 对 Double? 直接 Int 强转的 NaN/Infinity/溢出崩溃风险等），
修复后一次过。对照 run1（6 次 repair 全为沙箱误归因）：F6 修复后评估器
把执行不可行正确归因给机器验收，只对工作本身缺陷打回。

**Final Evaluator Calibration: GOOD**——机器 PASS + AC 全满足 + final PASS；
终评理由逐条可对账（160 测试数、API 未变、保护文件、diff 范围）。

**Verification Setup Burden: LOW**（make test 项目自有命令，冷构建 19s）；
**Evidence Audit Experience: ACCEPTABLE**——verdicts/verification/manifest
/git diff 一次审计即闭环；提取指标仍需手写脚本（第 4 次，见 taxonomy）。

## Observed Failures → 分类（三次运行合计）

| # | 现象 | 分类 | severity | 结局 |
| --- | --- | --- | --- | --- |
| F6 | 评估器把只读沙箱无法执行测试计为 NEEDS_WORK（#2-run2 + #3-run1 连续复发） | LAOMO PRODUCT BUG（提示词） | P1 | **已修** `21ba586`（test_51） |
| F9 | deferred provider restart 只看聊天会话标志，在 mission turn 进行中停掉 codex（run2 崩溃） | LAOMO PRODUCT BUG | P1 | **已修** `926d1cc`（3 tests） |
| F7 | （#3-run1）终态收割修复生产首验：threads:1 leaked:0 | 修复验证 ✓ | — | — |
| F10 | run2 死后 codex 退出路径留下 stopped 状态，下一次 run3 冷启动正常（lazy restart 兜底有效） | 预期行为 | — | — |
| F5/#2-run1 | 病理长 turn（5h）+ 丢弃 turn 证据丢失 | UPSTREAM 观测 + HARNESS 证据缺口 | P2 | 记录（未复发于 run3：24 turns 全记录） |

## Dogfood #3 结论

真实软件任务在两次产品修复后**完整自主交付**：规划 → 并行加固 → 评估修复 →
集成 → 机器验收 → 终评 → DONE，零人工介入，交付物是真实测试资产与真实
缺陷修复，用户环境零污染。#3 的两次失败各自转化为一项 P1 修复——
失败驱动研发的完整闭环样本。

