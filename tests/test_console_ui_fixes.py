"""Console regressions: a failed run's reason and the Claude/Codex sign-in cards.

The real console functions run in node with small DOM, timer and fetch stubs.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"

# What Codex 0.160 prints when the ChatGPT plan's usage limit is reached.
USAGE_LIMIT = (
    "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at 10:28 PM."
)


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = function_start - 6 if SCRIPT[function_start - 6 : function_start] == "async " else function_start
    opening = SCRIPT.index("{", function_start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        char = SCRIPT[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run(program: str):
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "node is unavailable")
class FailedRunStatusTests(unittest.TestCase):
    def test_a_failed_run_states_its_run_error_in_full(self) -> None:
        program = f"""
const els={{}};
const $=id=>els[id]||(els[id]={{id,textContent:'',title:'',className:'',classList:{{add(){{}},remove(){{}}}}}});
const STAGE_LABELS={{}}, ANALYZE_STAGES=[];
const syncAnalyzeRunUi=()=>{{}}, updateLiveChips=()=>{{}}, updateSessionButtons=()=>{{}}, updateProcLog=()=>{{}}, renderAnalysisStages=()=>{{}};
let lastProcText='';
{_js_function("isAnalysisRun")}
{_js_function("updateChatStatus")}
const show=run=>{{ updateChatStatus(run); return {{stage:$('chat-stage').textContent,detail:$('chat-detail').textContent,title:$('chat-detail').title,box:$('chat-status').className}}; }};
const limit={json.dumps(USAGE_LIMIT)};
const out={{}};
// The parser echoes the reason as the log's last line; run.error has it in full.
out.limit=show({{run_id:'r1',status:'failed',error:'codex exited with code 1: '+limit,
  agents:[{{stage_name:'prove',output:'\\u25e6 session 01a\\nerror \\u00b7 '+limit}}]}});
// A typed reason that only the run error carries.
out.reconnect=show({{run_id:'r2',status:'failed',error:'Claude Code exited with code 1: Claude Code is signed out. Reconnect Claude Code in Settings.',
  agents:[{{stage_name:'prove',output:'Reading the problem statement'}}]}});
// Without a run error, the last log line is still the best explanation.
out.noError=show({{run_id:'r3',status:'failed',agents:[{{stage_name:'prove',output:'[agent-monitor] timeout after 600s \\u2014 terminated'}}]}});
out.long=show({{run_id:'r4',status:'failed',error:'Claude Code exited with code 1: '+'x'.repeat(2000),agents:[]}});
out.running=show({{run_id:'r5',status:'running',agents:[{{stage_name:'prove',output:'thinking'}}]}});
console.log(JSON.stringify(out));
"""
        out = _run(program)
        full = "codex exited with code 1: " + USAGE_LIMIT
        self.assertEqual(
            out["limit"],
            {"stage": "Failed", "detail": full, "title": full, "box": "chat-status on err"},
        )
        self.assertIn("try again at 10:28 PM.", out["limit"]["detail"])
        self.assertEqual(
            out["reconnect"]["detail"],
            "Claude Code exited with code 1: Claude Code is signed out. Reconnect Claude Code in Settings.",
        )
        self.assertEqual(out["noError"]["detail"], "[agent-monitor] timeout after 600s — terminated")
        # A very long reason is shortened on screen; the tooltip keeps all of it.
        self.assertTrue(out["long"]["detail"].startswith("Claude Code exited with code 1: xxx"))
        self.assertTrue(out["long"]["detail"].endswith("…"))
        self.assertLess(len(out["long"]["detail"]), 400)
        self.assertEqual(len(out["long"]["title"]), len("Claude Code exited with code 1: ") + 2000)
        # The next status a run shows does not keep the failure's tooltip.
        self.assertEqual(
            {key: out["running"][key] for key in ("detail", "title", "box")},
            {"detail": "thinking", "title": "", "box": "chat-status on"},
        )

    def test_a_failed_runs_reason_wraps_instead_of_being_cut_off(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", HTML.split("<style>", 1)[1].split("</style>", 1)[0], flags=re.S)
        rules = {selector.strip(): body for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}
        self.assertIn("white-space: nowrap", rules[".chat-status .detail"])
        self.assertIn("white-space: normal", rules.get(".chat-status.err .detail", ""))
        self.assertIn("align-items: baseline", rules.get(".chat-status.err", ""))


if __name__ == "__main__":
    unittest.main()
