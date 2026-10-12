# Ablation 1 — fact graph → plain text store (运行手册)

**实验目的**：在其余全部组件不变的前提下，把"共享已验证结果存储"从内容寻址
fact graph（predecessors DAG / glossary / 级联撤销 / 逐 fact 文件）换成一个普通
顺序文本文件 `fact_text/facts.md`（"Fact 1, Fact 2, …"），衡量该组件对
成绩/效率的影响。**验证门禁、consult、curator、exploring graph、worker 数量、
断网软封锁全部与 FirstProofBench 原跑一致。**

**读者**：tmux `cgm-main` 里的 main agent（每题一个全新 session）。你收到的
kickoff 消息形如 `[ABLATION1-MAIN <project>] …`——先读本手册再动手。**你一次只
负责一题**；完成或判停后在会话里输出一行 `ABLATION_PROBLEM_DONE` + 简报，等
操作员侧（外层协调者）收尾并开下一题。不要自行开下一题。

## 固定设置（已由外层协调者配置好，勿改）

- `config/cgm.env` 已含 ablation 1 开关块：`CGM_FACT_TEXT_MODE=1`、
  `CGM_WORKER_CONTRACT=…/worker_text.md`、`CGM_WORKER_SKILLS=…/worker_text`、
  `VERIFY_AGENT_HOME=…/runtime/ablation/verify-agent-text`。
- verify 服务由外层保持常驻（若发现 down，`bash scripts/services.sh up verify`
  后在报告中注明）。
- consult：`gpt_pro`（gpt-5.6-sol）+ `--tools none`（env 已设）；断网软封锁全程
  生效（WebSearch/WebFetch deny、`search_arxiv_theorems` 短路、worker
  `web_search=false`）。**你自己也绝不联网。**
- roster：**high:2,xhigh:3**（与原跑一致，不再询问）。

## 与常规 main-agent 流程的差异（仅此三处）

1. **elaboration 用 `$elaboration-text` skill**（不是 `$elaboration`）；事实引用
   写 "Fact N"，不存在 fact_id。
2. 事实存储是 `runtime/projects/<p>/fact_text/facts.md`；`fact_search` 返回
   `fact_number`；完成判据是**目标定理成为该文件中的一条已验证 Fact**。
   `fact_revoke` 参数是 `fact_number`（追加撤回注记，无级联）。
3. curator 用 `bash examples/ops/curator-tmux-text.sh <project>` 启动（文本版
   启动 prompt；契约同原版）。

## 单题流程

设题号 NN ∈ {01,02,05,06,07,08,09}，项目名 `ab1_<原项目名>`（如
`ab1_fpb01_automorphism`），题面文件 `problems/FirstProofBench/fpbNN_*.txt`。

1. `cgm new <project> --roles high:2,xhigh:3`
2. `PROBLEM.md` ← 题面文件**逐字**内容（绝不引入 PDF 里的 Pre-registered
   Statement 或任何提示）。
3. `tmux kill-session -t cgm-curator 2>/dev/null; bash examples/ops/curator-tmux-text.sh <project>`
4. `bash scripts/services.sh up dashboard <project>`（观测用；文本模式下 fact
   视图为空属预期）。
5. **elaboration**（`$elaboration-text`）→ **consult**（`consult` skill）：
   - elaboration/consult 输入**必须逐字包含题面**（fpb09 教训）。
   - consult prompt 顶部加"策略契约头"：回复 <4k token、答案先行、勿在
     reasoning 里完整解题（fpb03 教训）。
   - consult 命令整条 `run_in_background`，**绝不嵌套 &**；疑似中断先查
     `spend/consult.jsonl` 再考虑重发（fpb02 教训）。
   - 回复逐字记为 `master_guidance`（gm_add，附 tokens/cost）。
6. `cgm assign` 每个 worker 一份 TASK.md（**逐字含题面** + 各自 lane），然后
   `bin/cgm start <project>`。
7. **监控**（用 /loop 自节奏，事件驱动）：`bin/cgm status <project>`、
   `gm_search`（verification traces）、`fact_text/facts.md` 增长。有真实新状态
   才重新 elaboration+consult（至多 ~2h 一次）。worker 大面积 RC=1 时先看
   worker 日志尾部（额度耗尽的教训）。
8. **完成判据**：目标定理（题面原命题，逐字或数学等价表述）成为
   `fact_text/facts.md` 中已验证的 Fact，且路径可信（与原跑同一验收口径；
   多 worker 独立达成更佳，照实记录）。达成 → `bin/cgm stop <project>`（先
   graceful）→ 杀 curator tmux → `bash scripts/services.sh down
   dashboard-<project>` → 输出 `ABLATION_PROBLEM_DONE` + 简报。
9. **判停条款**（继承原跑）：**2 个战略周期无实质进展**（无新的朝目标的已验证
   结果）或**单题超 4 小时硬上限** → 同样按第 8 步收尾，简报中如实标注
   `未闭合` + 状态快照。不要无限死磕。
10. **绝不**：`cgm finalize`、`git push`、任何联网、读 worker 的
    `local_memory/`、伪造成功。诚实汇报一切（含 verifier 拒绝率异常、服务故障）。

## 简报格式（ABLATION_PROBLEM_DONE 之后）

- 结论：闭合 / 未闭合（+ 答案一句话）
- Facts 数（`fact_text/facts.md` 条目数，扣除撤回）
- 各 worker 轮次（`cgm status` / `.status.json`）
- consult 次数与花费（`spend/consult.jsonl`）
- 异常事件（服务重启、额度告警、obstacle 等）
