# Dogfood #2–#3 Failure Taxonomy（2026-08-23，真实项目驱动）

来源：2 个真实项目（CodexPlusPlus 新增功能 / wisp 加固修复）、5 次 mission
运行、全部结论来自 `.laomo/runs/` 工件审计（报告见 df2-report.md /
df3-report.md）。

| 类别 | count | severity | project | 证据 | productBug | fixed | recurring |
| --- | --- | --- | --- | --- | --- | --- | --- |
| REPAIR（评估误归因） | 2 次运行 / 9 verdicts | **P1** | #2,#3 | df2 verdicts 3/5/7/9-0；df3-run1 verdicts 连续"只读沙箱无法执行测试" | 是（评估提示词把环境限制教成工作缺陷） | ✅ `21ba586`（test_51 锁契约） | 跨项目复发后止于修复 |
| RUNTIME（provider restart） | 1 | **P1** | #3-run2 | 日志 `pending provider restart applied → codex process exited`；单元 CRASH "codex process not running" | 是（busy 检查只看聊天会话，漏 mission turns） | ✅ `926d1cc`（3 tests） | 单发 |
| SCHEDULER（终态收割） | 1 | **P1** | #2-run1 | failed 后 turn 续跑 2h52m、11:58 仍写文件、12:48 合成结果被弃 | 是（policy 终态不 interrupt in-flight unit 线程） | ✅ `16f24a5`（test_50） | 单发；**生产验证**：df3-run1 `terminal-reap {threads:1, leaked:0}` |
| UPSTREAM_CODEX（病理长 turn） | 1 观测 | P2 | #2-run1 | 单 turn 4h58m、无 commit、仅写未提交文件 | 否（模型循环或完成事件丢失，无法确诊——见 EVIDENCE） | 未修（未复发：后续 3 次运行 43 turns 全部正常） | 未复发 |
| EVIDENCE（丢弃 turn 丢失统计/正文） | 1 | P2 | #2-run1 | `turn {ok:True, elapsed:0s, tokens:None, discarded}` | 是（与已知 default-fail 同族） | 记录未修 | 待观察 |
| EVIDENCE（指标手工提取负担） | 4 次 | P2 | 全部 | 4 份临时提取脚本（时间线/重叠/turn 统计各写一遍） | 设计债 | **触发 run-summary 条件成立**（重复+耗时+易错，两项目均证） | 是 |
| PLANNER（单元边界吸收 mission 目标） | 1 | P2 | #2-run2 | verdict 3-0"整体目标尚未闭环"（CLI 注册属单元 1） | 否（任务/模型性质） | — | 单发；#3 三次拆分均 GOOD |
| OPERATOR_UX（验收配置负担） | 2 观测 | P2 | #2 MEDIUM / #3 LOW | #2：worktree-vs-editable 需 canary 实验确证；#3：make test 冷构建 19s | 否 | — | — |
| VERIFICATION（机器门禁） | 首次实战 | — | #3-run3 | 集成树真实执行 `make test` + 禁触 diff，一次 PASS | —（设计验证 ✓） | — | — |
| FINAL_EVALUATOR | 首次实战 | — | #3-run3 | 终评 PASS 且理由逐条可对账（160 测试/API/保护文件/diff 范围） | 校准 GOOD | — | — |
| WAIT_WAKE | 首次实战 | — | #3-run3 | 单元 4 用 LAOMO_JOB 跑 make test，waiting 52s 后正常唤醒 | 设计验证 ✓ | — | — |
| WORKTREE / INTEGRATION / CONFLICT / CONTEXT / SCHEDULER(依赖屏障) | 0 失败 | — | — | 5 次运行零 worktree 污染、零错误集成、零冲突（任务无重叠编辑面）、用户 checkout 全程零污染 | — | — | — |

## 汇总

- 真实失败 → 确认产品 bug：**3 个 P1，全部当日修复 + 回归锁定 + 生产/后续运行验证**（166→225 tests）
- #3-run3 完整闭环 DONE（零人工介入）证明修复链有效
- Run-summary 只读提取器触发条件**已成立**（EVIDENCE 行）：建议作为下一个
  小里程碑，只读提取，不改 runtime
- 两个定性基线：Verification Setup Burden = MEDIUM(#2)/LOW(#3)；
  Evidence Audit Experience = ACCEPTABLE（数据完整，提取靠手写脚本）
