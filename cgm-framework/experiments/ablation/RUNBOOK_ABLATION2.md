# Ablation 2 — 关掉 exploration graph + curator（运行手册）

**实验目的**：在其余全部组件与 FirstProofBench 原跑完全一致的前提下（fact graph
原样、17 个原版 worker skills、原版 verifier、consult 照常），**关掉探索图覆盖层
与 curator**：`graph_link`/`graph_annotate`/`graph_frontier` 三个工具返回
"disabled" 错误信封且不写任何东西（`CGM_DISABLE_EXPLORING_GRAPH=1`），curator
一律不启动。衡量该组件对成绩/效率的影响。

**读者**：tmux `cgm-main` 里的 main agent（每题一个全新 session）。kickoff 形如
`[ABLATION2-MAIN <project>] …`。**你一次只负责一题**；完成或判停后输出一行
`ABLATION_PROBLEM_DONE` + 简报，停下等待外层接续。不要自行开下一题。

## 与常规 main-agent 流程的差异（仅此三处）

1. **不启动 curator**（任何 curator 脚本都不要跑；`cgm-curator` tmux 不应存在）。
2. **exploring graph 已禁用**：`graph_frontier` 会返回 disabled 错误——elaboration
   时直接跳过 frontier 输入（原 skill 里"当覆盖层有内容时"的守卫自然生效）；
   worker 侧 `graph_link` 同样返回错误，worker 忽略即可，这是实验设定不是故障，
   **不要**因此重启服务或排查。
3. 事实存储是**正常 fact graph**（fact_id / predecessors / glossary 全部照旧），
   elaboration 用原版 `$elaboration` skill。完成判据 = 目标定理成为验收 fact
   （fact_graph/facts/ 中语句与题面逐字或数学等价的 fact），与原跑口径一致。

## 其余全部沿用 RUNBOOK_ABLATION1.md 的单题流程与纪律

即：`cgm new <project> --roles high:2,xhigh:3` → PROBLEM.md 逐字（绝不引入
Pre-registered Statement）→ dashboard 可选 → `$elaboration`（逐字含题面）→
consult（策略契约头、不嵌套 &、疑似中断先查 ledger）→ `cgm assign`（TASK.md
逐字含题面）→ `bin/cgm start` → 事件驱动监控 → 完成/判停（2 个战略周期无实质
进展或 4h 上限）→ graceful stop（长轮卡界时可 force）→ 停 dashboard → 输出
`ABLATION_PROBLEM_DONE` + 简报（结论/facts 数/轮次/consult/异常）。绝不联网、
不 finalize、不 push、不读 worker local_memory、不伪造成功。
