# Ablation 3 — 拿掉独立验证器，改为 worker 自评（运行手册）

**实验目的**：在其余全部组件不变的前提下，把**写入门禁的判官**从"独立冷启动
codex 判卷"换成"**worker 用同一个 session 复核自己的草稿**"，衡量*验证的独立性*
对成绩/效率的影响。**评审标准（rubric）保持不变**：worker 拿到的正是原版
verifier 用的三个验证 skill 与 `agents/contracts/verifier.md` 里的 Hard
Prohibitions，判据仍是"无 critical error 且无 gap ⇒ correct"。唯一变量是
**谁来跑这套标准**：一个没有上下文的冷启动判官，还是写出这份证明的那个 session。

fact graph、consult、curator + exploring graph、roster、断网软封锁全部与
FirstProofBench 原跑一致。

**读者**：tmux `cgm-main` 里的 main agent（每题一个全新 session）。kickoff 形如
`[ABLATION3-MAIN <project>] …`。**你一次只负责一题**；完成或判停后输出一行
`ABLATION_PROBLEM_DONE` + 简报，停下等待外层接续。不要自行开下一题。

## 固定设置（已由外层协调者配置好，勿改）

- `config/cgm.env` 已含 ablation 3 开关块：`CGM_SELF_VERIFY_GATE=1`、
  `CGM_WORKER_CONTRACT=…/worker_selfverify.md`、
  `CGM_WORKER_SKILLS=…/worker_selfverify`。
- consult：`gpt_pro`（gpt-5.6-sol）+ `--tools none`（env 已设）；断网软封锁全程
  生效（WebSearch/WebFetch deny、`search_arxiv_theorems` 短路、worker
  `web_search=false`）。**你自己也绝不联网。**
- roster：**high:2,xhigh:3**（与原跑一致，不再询问）。
- verify 服务由外层保持常驻，但**本实验中 worker 不会调用它**（`fact_submit`
  不再发 HTTP）。它常驻是给外层跑事后判卷用的。**看到 verify 空闲、verifier
  token 为 0 是预期现象，不是故障，不要排查、不要重启。**

## 与常规 main-agent 流程的差异（仅此三处）

1. **`fact_submit` 多两个必填参数**：`self_verdict`（严格 `"correct"` /
   `"wrong"`）与 `self_review_report`（worker 自己跑完三个验证 skill 写出的报告，
   ≥200 字符）。这只影响 worker，你不提交 fact（main 无 `fact_submit`）。
2. **`fact_search` / `gm_search` 的 `verification` trace 里 `gate="self_verify"`**，
   `verdict` 是 worker 自己的判词。**读这些 trace 时要记得：它不是独立判卷。**
   在 elaboration 与 consult 材料里如实标注"本轮事实由自评门禁写入"。
3. **完成判据照旧但要加一句标注**：目标定理成为 fact graph 中的 fact（语句与题面
   逐字或数学等价），路径可信 ⇒ 视为本臂"闭合"。**但你必须在简报里明确写出：
   该闭合是自评门禁下的闭合，独立判卷由外层事后进行。** 不要自己去调 verify 服务
   补判——那是外层的事，你调了会污染 token 记账。

## 单题流程

本臂只跑 4 题：`fpb01_automorphism`、`fpb06_treelattice`（原跑已闭合）、
`fpb03_bernoulli`、`fpb10_vonneumann`（原跑未闭合/部分交付）。项目名
`ab3_<原项目名>`，题面文件 `problems/FirstProofBench/<原项目名>.txt`。

1. `cgm new <project> --roles high:2,xhigh:3`
2. `PROBLEM.md` ← 题面文件**逐字**内容（绝不引入 PDF 里的 Pre-registered
   Statement 或任何提示）。
3. `tmux kill-session -t cgm-curator 2>/dev/null; bash examples/ops/curator-tmux.sh <project>`
4. `bash scripts/services.sh up dashboard <project>`（观测用，可选）。
5. **elaboration**（`$elaboration`）→ **consult**（`consult` skill）：
   - elaboration/consult 输入**必须逐字包含题面**（fpb09 教训）。
   - consult prompt 顶部加"策略契约头"：回复 <4k token、答案先行、勿在
     reasoning 里完整解题（fpb03 教训）。
   - consult 命令整条 `run_in_background`，**绝不嵌套 &**；疑似中断先查
     `spend/consult.jsonl` 再考虑重发（fpb02 教训）。
   - 回复逐字记为 `master_guidance`（gm_add，附 tokens/cost）。
6. `cgm assign` 每个 worker 一份 TASK.md（**逐字含题面** + 各自 lane），然后
   `bin/cgm start <project>`。
7. **监控**（用 /loop 自节奏，事件驱动）：`bin/cgm status <project>`、
   `gm_search`（verification traces）、fact 数增长。有真实新状态才重新
   elaboration+consult。worker 大面积 RC=1 时先看 worker 日志尾部（额度耗尽的教训）。
8. **时间上限（本实验硬性，防烧穿）**：
   - `ab3_fpb01_automorphism` / `ab3_fpb06_treelattice`：**60 分钟**
   - `ab3_fpb03_bernoulli` / `ab3_fpb10_vonneumann`：**120 分钟**
   计时从 `bin/cgm start` 起。到点无论状态一律收尾（先 graceful stop；有 worker
   卡在超长轮到不了轮界时用 `--force`，并在简报里注明被 force 的 worker 与轮次）。
   外层还有一道 backstop 会替你停，但**以你自己的上限为准**。
9. **完成 / 判停**：目标定理成为 fact（见上）→ 立刻收尾；或到时间上限 → 收尾。
   收尾 = `bin/cgm stop <project>` → 杀 curator tmux → `bash scripts/services.sh
   down dashboard-<project>` → 输出 `ABLATION_PROBLEM_DONE` + 简报。
10. **绝不**：`cgm finalize`、`git push`、任何联网、读 worker 的
    `local_memory/`、伪造成功、手动调 verify 服务。诚实汇报一切。

## 简报格式（ABLATION_PROBLEM_DONE 之后）

- 结论：闭合（自评门禁下）/ 未闭合（+ 答案一句话）
- **目标 fact 的 fact_id 列表**（外层事后判卷要用；哪几条是"题面逐字/数学等价的
  目标定理"，逐条写清楚）—— 这一条最重要，别漏
- Facts 数（扣除撤回）
- 自评 `wrong` 的次数（`gm_search` verification traces 里 `verdict=="wrong"` 的
  条数）—— 即 worker 自己拒了自己多少次
- 各 worker 轮次
- consult 次数与花费（`spend/consult.jsonl`）
- 是否触及时间上限、有无 force stop
- 异常事件（服务故障、额度告警、obstacle 等）
