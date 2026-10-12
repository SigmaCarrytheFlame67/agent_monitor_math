#!/usr/bin/env bash
# blind-operator.sh do|undo — strip per-problem answers/routes from OPERATOR.md
# for a blinded rerun (the ab4 lesson: those sections are worth a whole problem
# to a fresh main agent). Reversible; backup kept in runtime/.
set -euo pipefail
cd "$(dirname "$0")/.."
BAK="runtime/OPERATOR.md.full.bak"
case "${1:?usage: blind-operator.sh do|undo}" in
  do)
    [ -f "$BAK" ] && { echo "already blinded (backup exists) — undo first"; exit 1; }
    cp OPERATOR.md "$BAK"
    python3 - <<'PY'
import re
s = open("OPERATOR.md", encoding="utf-8").read()
# keep the operator profile + standing preferences + git/codex notes; drop every
# per-problem results block (they record answers and routes verbatim)
head, sep, tail = s.partition("## Per-project pointers")
blinded = head + sep + """
_(BLINDED for a rerun in progress: the per-problem history of this deployment —
answers, routes, spend — has been removed so a fresh main agent starts with zero
prior knowledge of the benchmark problems. Full version: runtime/OPERATOR.md.full.bak;
restore with `bash scripts/blind-operator.sh undo`.)_

- **你的先验知识应当为零。** 若你通过任何渠道发现自己知道某题的答案或路线，
  必须在报告中如实上报。
- Git: Use the operator-selected working branch. Do not push without authorization.
- codex 后端: ChatGPT 订阅登录（CODEX_BACKEND=chatgpt），worker 模型 gpt-6-astra，effort xhigh。
- curator: tmux 会话（examples/ops/curator-tmux.sh <project>），不归 services.sh 管。
"""
open("OPERATOR.md", "w", encoding="utf-8").write(blinded)
PY
    echo "blinded. backup: $BAK"
    ;;
  undo)
    [ -f "$BAK" ] || { echo "no backup at $BAK"; exit 1; }
    mv "$BAK" OPERATOR.md
    echo "restored."
    ;;
esac
