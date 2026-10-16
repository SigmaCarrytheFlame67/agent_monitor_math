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


def _sign_in_cards() -> str:
    """The page's Codex and Claude sign-in cards, with their state and polls."""
    start = SCRIPT.index("/* ── ChatGPT/Codex subscription shared by supported harnesses ── */")
    return SCRIPT[start : SCRIPT.index("async function saveSettings()", start)]


# The Settings cards in node: a DOM whose card bodies are rebuilt on every
# render (an input loses its value, as in the page), timers that only fire when
# a test says a poll period has passed, and requests the test answers itself,
# in any order.
_CARDS_DOM = r"""
const LIB_ESC=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let SETTINGS=null;
const document={activeElement:null};
const confirm=()=>true, alert=()=>{}, navigator={};
const cards={}, kids={};
const $=id=>{
  if(id.endsWith('-card')){
    if(!cards[id]){ let html=''; cards[id]={get innerHTML(){return html;},set innerHTML(value){html=value;for(const key in kids)if(kids[key].owner===id)delete kids[key];}}; }
    return cards[id];
  }
  if(kids[id])return kids[id];
  const owner=Object.keys(cards).find(key=>cards[key].innerHTML.includes(`id="${id}"`));
  if(!owner)return null;
  const tag=(new RegExp(`<[^>]*id="${id}"[^>]*>`).exec(cards[owner].innerHTML)||[''])[0];
  return kids[id]={id,owner,value:'',dataset:{},disabled:/\sdisabled[\s>]/.test(tag),focus(){document.activeElement=this;}};
};
const cardText=id=>$(id).innerHTML.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim();
let timers=[], timerId=0;
const setTimeout=(fn,ms)=>{const id=++timerId;timers.push({id,fn,ms,repeat:false});return id;};
const setInterval=(fn,ms)=>{const id=++timerId;timers.push({id,fn,ms,repeat:true});return id;};
const clearTimeout=id=>{timers=timers.filter(timer=>timer.id!==id);};
const clearInterval=clearTimeout;
const settle=async()=>{ for(let i=0;i<30;i++)await new Promise(resolve=>setImmediate(resolve)); };
// One poll period passes: every pending timer fires once.
const tick=async()=>{ const due=[...timers]; timers=timers.filter(timer=>timer.repeat); for(const timer of due)timer.fn(); await settle(); };
const requests=[];
const fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const reply=(status,body)=>({ok:status>=200&&status<300,status,json:async()=>body});
const proxyError=status=>({ok:false,status,json:async()=>{throw new SyntaxError('Unexpected token < in JSON');}});
const answer=async(request,response)=>{ requests.splice(requests.indexOf(request),1); if(response instanceof Error)request.reject(response); else request.resolve(response); await settle(); };
let loads=0;
const renders={selector:0,models:0,engines:0};
const renderRuntimeSelector=()=>{renders.selector++;}, syncRuntimeModelControls=()=>{renders.models++;}, renderEngines=()=>{renders.engines++;};
"""


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


@unittest.skipUnless(NODE, "node is unavailable")
class AccountConnectionSettingsTests(unittest.TestCase):
    def test_connecting_or_disconnecting_an_account_reloads_the_settings_it_changes(self) -> None:
        # /api/settings derives Max and each runtime's thinking controls from
        # the accounts linked when it answers; the fake server does the same.
        program = (
            _CARDS_DOM
            + _js_function("selectedAccountRuntime")
            + "\n"
            + _sign_in_cards()
            + r"""
const server={claude:false,codex:true,runtime:'claude'};
const serverSettings=()=>{
  const claude=server.claude?['sonnet','opus','fable']:[], codex=server.codex?['gpt-6-astra']:[];
  const tuning=models=>Object.fromEntries(models.map(model=>[model,{reasoning:[{id:'high'},{id:'max'}],speed:[{id:'standard'}]}]));
  return {account_runtime:server.runtime,subscription_policy:{claude:'available',codex:'available'},
    claude_connected:server.claude,codex_connected:server.codex,claude_models:claude,codex_models:codex,
    models_by_runtime:{api:[],claude,codex},run_tuning_by_runtime:{api:{},claude:tuning(claude),codex:tuning(codex)},
    cgm_max:{available:server.claude&&server.codex}};
};
let reachable=true;
const loadSettings=async()=>{ loads++; if(reachable)SETTINGS=serverSettings(); };
const view=()=>({loads,max:!!SETTINGS.cgm_max.available,claudeTuning:Object.keys(SETTINGS.run_tuning_by_runtime.claude),
  codexModels:SETTINGS.codex_models,codexRuntimeModels:SETTINGS.models_by_runtime.codex});
const claude=connected=>({status:connected?'success':'idle',running:false,connected,reauth_required:false,claude_installed:true,models:connected?['sonnet','opus','fable']:[]});
const codex=connected=>({status:connected?'success':'idle',running:false,connected,reauth_required:false,codex_installed:true});
(async()=>{
  const out={};
  SETTINGS=serverSettings();
  // The page loaded while Claude was not linked; it is connected in Settings.
  server.claude=true;
  await applyClaudeStatus(claude(true)); out.claudeConnected=view();
  // The card shows that state again (a poll, Settings reopened): no reload.
  await applyClaudeStatus(claude(true)); out.unchanged=view();
  server.claude=false;
  await applyClaudeStatus(claude(false)); out.claudeDisconnected=view();
  // Codex is disconnected and the reload fails: its models still go.
  server.claude=true; await loadSettings(); loads=0;
  server.codex=false; reachable=false;
  await applyCodexStatus(codex(false)); out.codexDisconnected=view();
  reachable=true; server.codex=true;
  await applyCodexStatus(codex(true)); out.codexConnected=view();
  await applyCodexStatus(codex(true)); out.codexUnchanged=view();
  console.log(JSON.stringify(out));
})().catch(error=>{console.error(error);process.exit(1);});
"""
        )
        out = _run(program)
        self.assertEqual(
            out["claudeConnected"],
            {"loads": 1, "max": True, "claudeTuning": ["sonnet", "opus", "fable"],
             "codexModels": ["gpt-6-astra"], "codexRuntimeModels": ["gpt-6-astra"]},
        )
        self.assertEqual(out["unchanged"]["loads"], 1)
        self.assertEqual(out["claudeDisconnected"], {**out["claudeConnected"], "loads": 2, "max": False, "claudeTuning": []})
        self.assertEqual(
            out["codexDisconnected"],
            {"loads": 1, "max": True, "claudeTuning": ["sonnet", "opus", "fable"], "codexModels": [], "codexRuntimeModels": []},
        )
        self.assertEqual(
            out["codexConnected"],
            {"loads": 2, "max": True, "claudeTuning": ["sonnet", "opus", "fable"],
             "codexModels": ["gpt-6-astra"], "codexRuntimeModels": ["gpt-6-astra"]},
        )
        self.assertEqual(out["codexUnchanged"]["loads"], 2)


if __name__ == "__main__":
    unittest.main()
