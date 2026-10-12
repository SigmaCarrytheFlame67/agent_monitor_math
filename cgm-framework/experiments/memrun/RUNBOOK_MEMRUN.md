# MEMRUN — FPB 十题·连续记忆学习曲线跑（2026-09）

**协议**：与原跑同题、同顺序（fpb01→10）、同配置，唯一新变量 = **跨题记忆开启且
只限本批**：第 N 题的 worker/main 可通过 `memory_recall` 看到本跑第 1..N-1 题的
dead_end/obstacle/counterexample、已验证 fact 的 statement、以及 curator lessons；
**看不到任何历史项目**（原跑/ab1-4/IMO/odd_coprimality 对工具完全不可见，
`CGM_MEMORY_SCOPE=batch`）。测的是：积累的（合法）经验能否让后面的题更快/更省。

## 配置（外层已设好，main agent 不要动）
- `config/cgm.env`：`CGM_MEMORY_SCOPE=batch`（唯一的跨题记忆开关）
  + 原有断网软封锁行全部保留（consult `--tools none`、外部搜索短路）。
- 项目命名 `mem01_automorphism` … `mem10_vonneumann`；创建时必须
  `bin/cgm new <p> --roles high:2,xhigh:3 --batch fpb-mem`（batch 标签是
  memory_recall 的作用域边界，**漏了 --batch 该题就看不到任何记忆**）。
- consult：`gpt_pro` + 策略契约头；verify 由外层起好；curator 照常
  `examples/ops/curator-tmux.sh <p>`（lessons 注入自动生效，文件从空开始=零历史污染）。

## 零先验纪律（继承 ab4，逐字执行）
你的全部先验知识应当为零。OPERATOR.md 已盲化。**禁止**：读 `runtime/projects/` 下
非本批项目的任何文件；读 `runtime/archive/`、`experiments/*/results/`、
`runtime/baseline_codex/`；翻 git 历史找答案；`gm_search`/`fact_search` 传
`project=` 查非本批项目。跨题信息的**唯一合法通道是 `memory_recall`**（它已按批次
裁剪）。若你发现自己通过任何渠道知道了某题的答案或路线，必须在报告中如实上报。

## memory_recall 使用指引（写进每题 TASK.md 的提醒）
- 开工先 `memory_recall` 查方法学教训（dead ends、反例模式）；卡住时再查。
- **铁律**：跨题 fact 只是 awareness——要用必须把它的 statement 在本题里重新
  `fact_submit` 过验证；直接引它的 id 当 predecessor 会悬空。
- 前几题命中会很少（记忆还没积累），这是预期，不要反复重试。

## 每题流程
new（带 --batch）→ PROBLEM.md 逐字（题面 `problems/FirstProofBench/fpbNN_*.txt`，
不含 Pre-registered Statement）→ elaboration → consult → assign（TASK.md 含
memory_recall 提醒）→ start → 监控。**硬性时间上限见 kickoff，到点无论状态一律
收尾**（继承封存跳下题策略）。收尾：停 swarm、停 curator tmux；跑
`python3 scripts/curator-calibration.py --project <p>` 并把输出给 curator、请它
`lesson_add` 复盘（方法学红线见其契约）后再杀其 tmux；输出一行
`MEMRUN_PROBLEM_DONE` + 简报（含目标 fact id 列表、memory_recall 命中并实际用上的
条目）；然后停下等待，不开下一题、不 finalize、不 push。
