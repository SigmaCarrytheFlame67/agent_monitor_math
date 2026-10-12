# Ablation 4 — 关掉战略 consult（运行手册）

**实验目的**：在其余全部组件不变的前提下，**关掉高智能战略 consult**
（`CGM_CONSULT_TRANSPORT=off`）——不再有外部顶级模型给方向，`master_guidance`
改由**你自己（main agent）推理产出**。衡量这条"占全系统 0.6% token、却决定 96%
劳动流向哪里"的杠杆到底值多少。

fact graph、独立 verifier、curator + exploring graph、worker skills、roster、
断网软封锁全部与 FirstProofBench 原跑一致。

**读者**：tmux `cgm-main` 里的 main agent（每题一个全新 session）。kickoff 形如
`[ABLATION4-MAIN <project>] …`。**你一次只负责一题**；完成或判停后输出一行
`ABLATION_PROBLEM_DONE` + 简报，停下等待外层接续。不要自行开下一题。

## 固定设置（已由外层协调者配置好，勿改）

- `config/cgm.env` 已设 `CGM_CONSULT_TRANSPORT=off`。
- verify 服务由外层保持常驻（若发现 down，`bash scripts/services.sh up verify`
  后在报告中注明）。
- 断网软封锁全程生效（WebSearch/WebFetch deny、`search_arxiv_theorems` 短路、
  worker `web_search=false`）。**你自己也绝不联网。**
- roster：**high:2,xhigh:3**（与原跑一致，不再询问）。

## 零先验纪律（本臂新增，2026-08-22 起——第一次跑因此作废重来）

`OPERATOR.md` 的基准逐题结果段落**已被隐去**（完整版备份在
`runtime/OPERATOR.md.full.bak`），因为它会随 `@OPERATOR.md` 自动进入你的会话，
而它记着这些题的原跑答案、构造与证明路线。第一次跑 ab4 时它确实泄漏了：某题的
cycle-0 `master_guidance` 把原跑路线逐符号照搬（连"唯一难桥是那条整值不等式"都一样）
并写成"我自己验算过的"，swarm 于是 24 分钟闭合、每个 worker 只跑了 1 轮——这让
"main agent 能否顶替 consult"这个测量彻底失效。

**因此，你对本题的先验知识必须为零：**

- 不读 `runtime/projects/` 下**其他任何**项目（尤其是同题的非 ab4 版本，如
  `fpb06_treelattice` / `ab1_*` / `ab2_*` / `ab3_*`）的 fact graph、global memory、
  TASK.md、PROBLEM.md 之外的任何东西。
- 不读 `runtime/baseline_codex/`、`runtime/archive/`、`experiments/ablation/results/`。
- `gm_search` / `fact_search` **只查你自己的 project**，绝不传别的 project 名。
- 不去翻 git 历史、旧 commit、旧 OPERATOR.md 找答案。
- 如果你发现自己"已经知道"这题的答案或路线，**如实在简报里写出来**（写明你从哪看到的），
  不要假装是自己推的。诚实记录比干净数据更重要——数据脏了可以重跑，谎报没法补救。

你唯一的信息源：本项目的 `PROBLEM.md`（逐字题面）+ 你自己的推理 + 本项目 swarm 产出的
findings/facts。

## 与常规 main-agent 流程的差异（仅此两处）

1. **不做 consult。** `consult` skill 在 `off` 传输下会直接返回空回复、零花费；
   **不要**为了绕过它去改 env、去换 transport、去手动调任何外部 API。看到
   `transport: "off"`、`reply: ""`、`cost_usd: 0.0` 是预期，不是故障。
2. **`master_guidance` 由你自己写。** 每个战略周期：照常跑 `$elaboration`
   （它读 global memory + fact graph + `graph_frontier`），然后**你自己**基于
   elaboration 做战略判断——关键分解、下一步方向、每个 worker 的 lane——把结论
   写成 `master_guidance` 用 `gm_add` 记录（注明 `source: main-agent self-reasoning
   (consult disabled)`），再 `cgm assign`。质量尽你所能，别因为"没有 consult"
   就敷衍或跳过这一步：本实验测的就是你这一步能不能顶上。

## 单题流程

本臂只跑 4 题：`fpb01_automorphism`、`fpb06_treelattice`（原跑已闭合）、
`fpb03_bernoulli`、`fpb10_vonneumann`（原跑未闭合/部分交付）。项目名
`ab4_<原项目名>`，题面文件 `problems/FirstProofBench/<原项目名>.txt`。

1. `cgm new <project> --roles high:2,xhigh:3`
2. `PROBLEM.md` ← 题面文件**逐字**内容（绝不引入 PDF 里的 Pre-registered
   Statement 或任何提示）。
3. `tmux kill-session -t cgm-curator 2>/dev/null; bash examples/ops/curator-tmux.sh <project>`
4. `bash scripts/services.sh up dashboard <project>`（观测用，可选）。
5. **elaboration**（`$elaboration`，输入**必须逐字包含题面**）→ **你自己的战略
   推理** → `gm_add` 记 `master_guidance`。
6. `cgm assign` 每个 worker 一份 TASK.md（**逐字含题面** + 各自 lane），然后
   `bin/cgm start <project>`。
7. **监控**（用 /loop 自节奏，事件驱动）：`bin/cgm status <project>`、
   `gm_search`（verification traces）、fact 数增长。有真实新状态才重新
   elaboration + 自主再决策。worker 大面积 RC=1 时先看 worker 日志尾部。
8. **时间上限（本实验硬性，防烧穿）**：
   - `ab4_fpb01_automorphism` / `ab4_fpb06_treelattice`：**60 分钟**
   - `ab4_fpb03_bernoulli` / `ab4_fpb10_vonneumann`：**120 分钟**
   计时从 `bin/cgm start` 起。到点无论状态一律收尾（先 graceful stop；有 worker
   卡在超长轮到不了轮界时用 `--force`，并在简报里注明被 force 的 worker 与轮次）。
   外层还有一道 backstop 会替你停，但**以你自己的上限为准**。
9. **完成 / 判停**：目标定理成为验收 fact（语句与题面逐字或数学等价，路径可信）
   → 立刻收尾；或到时间上限 → 收尾。收尾 = `bin/cgm stop <project>` → 杀
   curator tmux → `bash scripts/services.sh down dashboard-<project>` → 输出
   `ABLATION_PROBLEM_DONE` + 简报。
10. **绝不**：`cgm finalize`、`git push`、任何联网、读 worker 的
    `local_memory/`、伪造成功、绕过 consult 禁用。诚实汇报一切。

## 简报格式（ABLATION_PROBLEM_DONE 之后）

- 结论：闭合 / 未闭合（+ 答案一句话）
- **目标 fact 的 fact_id 列表**（哪几条是题面逐字/数学等价的目标定理）
- Facts 数（扣除撤回）
- 你写了几版 `master_guidance`（自主战略周期数），每版一句话要点
- 各 worker 轮次
- consult：应为 0 次 $0（如非零，说明出了问题，如实报告）
- 是否触及时间上限、有无 force stop
- 异常事件
