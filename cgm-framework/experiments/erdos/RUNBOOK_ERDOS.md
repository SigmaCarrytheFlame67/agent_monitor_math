# ERDRUN — Erdős 加性基问题·连续记忆跑（2026-09）

**协议**：与 MEMRUN（FPB 十题学习曲线跑）同配置，题源换成 `problems/Erdos/`
（7 道 Erdős 加性基问题，逐字题面）。唯一跨题变量 = **跨题记忆开启且只限本批**
（`CGM_MEMORY_SCOPE=batch`，batch 标签 `erdos-mem`）：第 N 题的 worker/main 可通过
`memory_recall` 看到本跑第 1..N-1 题的 dead_end/obstacle/counterexample、已验证 fact 的
statement、以及 curator lessons；**看不到任何历史项目**。本轮先只跑第 1 题。

## 配置（外层已设好，main agent 不要动）
- `config/cgm.env`：`CGM_MEMORY_SCOPE=batch`；codex 模型 `gpt-6-astra`@`xhigh`
  （worker/verify 统一）；consult `gpt_pro`（gpt-5.6-sol）+ `--tools none`；
  `CGM_DISABLE_EXTERNAL_SEARCH=1`。**全程断网**：题目在网上有公开答案，禁止任何
  形式的联网检索（WebSearch/WebFetch 已 deny；worker/verify codex `web_search=false`；
  不许用 curl/wget/pip 等 shell 命令访问外网）。
- 项目命名 `erd01_repsum` … ；创建时必须
  `bin/cgm new <p> --roles high:2,xhigh:3 --batch erdos-mem`（batch 标签是
  memory_recall 的作用域边界，**漏了 --batch 该题就看不到任何记忆**）。
- consult：`gpt_pro` + 策略契约头（回复 <4k token、答案先行、勿在 reasoning 内试图
  完整解题）；**consult 命令整条 run_in_background，不嵌套 &**；疑似被杀先查
  `runtime/projects/<p>/spend/consult.jsonl` 再重发。verify 由外层起好；curator 照常
  `examples/ops/curator-tmux.sh <p>`。

## 研究模式（2026-09-12 起，操作员为 erd06 第四段开启；仅限操作员明确开启的项目）
`config/cgm.env`：`CGM_CODEX_WEB_SEARCH=true`（worker + verifier 的 codex web_search）、
`CGM_CONSULT_TOOLS` 取消 none（consult 带 web_search + code_interpreter）、
`CGM_DISABLE_EXTERNAL_SEARCH` 取消（search_arxiv_theorems 恢复）、`.claude/settings.json`
不再 deny WebSearch/WebFetch。main agent 必须在 TASK.md 与 consult prompt 里写明"操作员已开启
web search"；网上取得的内容必须重推导并过 fact_submit 或作为可核查外部引用，来源记 gm_add。
回到基准模式：恢复 cgm.env 的两行、设 CGM_CODEX_WEB_SEARCH=false、恢复 deny 列表。

## 零先验纪律（逐字执行）
你的全部先验知识应当为零。OPERATOR.md 已盲化。**禁止**：读 `runtime/projects/` 下
非本批项目的任何文件；读 `runtime/archive/`、`experiments/*/results/`、
`runtime/baseline_codex/`、`runtime/memory-quarantine/`；翻 git 历史找答案；
`gm_search`/`fact_search` 传 `project=` 查非本批项目。跨题信息的**唯一合法通道是
`memory_recall`**（已按批次裁剪）。若你发现自己通过任何渠道知道了某题的答案、
路线或该结论在文献中的名字，必须在报告中如实上报。

## 验收标准提醒（写进 TASK.md）
- 目标定理必须**自包含地证明**：把一个与目标等价或直接蕴含目标的"已知定理"当黑箱
  引用（"by a classical theorem of …"）不算证明；用到的经典工具（如 Parseval、
  Cauchy–Schwarz、生成函数基本性质）可以引用但要写清楚陈述。
- 题面若是问句（"Can there exist…?"），目标 fact 要给出明确的 YES/NO 答案 + 完整证明。

## memory_recall 使用指引（写进 TASK.md）
- 开工先 `memory_recall` 查方法学教训；卡住时再查。第 1 题命中为零是预期，不要重试。
- **铁律**：跨题 fact 只是 awareness——要用必须把它的 statement 在本题里重新
  `fact_submit` 过验证；直接引它的 id 当 predecessor 会悬空。

## 每题流程
new（带 --batch）→ PROBLEM.md 逐字（题面 `problems/Erdos/NN_*.txt`；文件名里的
标签/奖金/状态是元数据，不进 PROBLEM.md）→ elaboration → consult → assign（TASK.md
含上述两段提醒）→ start → 监控。**硬性时间上限见 kickoff，到点无论状态一律收尾**。
收尾：停 swarm、停 curator tmux；跑 `python3 scripts/curator-calibration.py --project <p>`
并把输出给 curator、请它 `lesson_add` 复盘后再杀其 tmux；输出一行
`ERDRUN_PROBLEM_DONE` + 简报（含答案、目标 fact id 列表、memory_recall 命中并实际用上的
条目、任何先验泄漏自查结果）；然后停下等待，不开下一题、不 finalize、不 push。
