"""Browser-side regressions for the 2026-09-27 live review (run under node).

The real console / monitor functions are extracted from the served HTML and
run against small stubs, so these pin behaviour, not markup.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = (ROOT / "agent_monitor" / "web" / "console.html").read_text(encoding="utf-8")
CONSOLE_SCRIPT = CONSOLE.rsplit("<script>", 1)[1].split("</script>", 1)[0]
MONITOR = (ROOT / "monitor_core" / "harness_dashboard" / "web" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _block_end(source: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise AssertionError("unterminated block")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[start - 6 : start] == "async ":
        start -= 6
    opening = source.index("{", source.index(")", start))
    return source[start : _block_end(source, opening)]


def _const_arrow(source: str, name: str) -> str:
    start = source.index(f"const {name} = ")
    line_end = source.index("\n", start)
    if source[:line_end].rstrip().endswith("{"):
        end = _block_end(source, source.rindex("{", start, line_end))
        return source[start : source.index(";", end) + 1]
    return source[start : source.index(";\n", start) + 1]


def _polling_block() -> str:
    start = CONSOLE_SCRIPT.index("const POLL_MIN_MS=")
    tick = CONSOLE_SCRIPT.index("async function tick(")
    return CONSOLE_SCRIPT[start : _block_end(CONSOLE_SCRIPT, CONSOLE_SCRIPT.index("{", tick))]


def _run(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


POLL_STUBS = r"""
let runId='r1', workspaceView='sandbox', pollTimer=null;
const calls=[], timers=[], listeners={};
globalThis.setTimeout=(fn,ms)=>{ timers.push({fn,ms}); return timers.length; };
globalThis.clearTimeout=()=>{};
const document={hidden:false,addEventListener:(type,fn)=>{ listeners[type]=fn; }};
let payload={run_id:'r1',status:'running',updated_at:'t1',agents:[],totals:{},engine:'kimi'};
globalThis.fetch=async url=>{ calls.push(url); return {ok:true,json:async()=>JSON.parse(JSON.stringify(payload))}; };
const $=()=>({textContent:''});
function setPill(){} function updateChatStatus(){} function updateProblemStrip(){} function updatePromptStrip(){}
function renderConversation(){} function renderAutoPipeline(){} function applySandboxPreview(){}
async function refreshProofPreview(){ calls.push('files'); return true; }
function loadChat(){ calls.push('chat'); }
function loadRuns(){ calls.push('runs'); }
const flush=async()=>{ for(let i=0;i<20;i++) await new Promise(r=>setImmediate(r)); };
async function fire(){ const t=timers.shift(); t.fn(); await flush(); return t.ms; }
"""


class ConsolePollingTests(unittest.TestCase):
    """Live: ~7 requests/s per open tab (400 ms, four endpoints, no backoff)."""

    def test_backoff_side_fetch_gating_and_hidden_tabs(self) -> None:
        program = POLL_STUBS + _polling_block() + r"""
(async()=>{
  const out={};
  startPolling(); await flush();
  out.first=calls.splice(0);
  out.delays=[];
  for(let i=0;i<5;i++){ out.delays.push(timers[0].ms); await fire(); }
  out.quiet=calls.splice(0);
  // A change in the run record resets the cadence and refreshes files/chat.
  payload.updated_at='t2';
  out.beforeChange=timers[0].ms; await fire();
  out.changed=calls.splice(0); out.afterChange=timers[0].ms;
  // A hidden tab fetches nothing and schedules nothing ...
  document.hidden=true; await fire();
  out.hidden=calls.splice(0); out.hiddenTimers=timers.length;
  // ... until it becomes visible again.
  document.hidden=false; listeners.visibilitychange(); await flush();
  out.resumed=calls.splice(0);
  out.resumedDelay=timers[0]&&timers[0].ms;
  // A finished run stops polling after one final refresh.
  payload.status='finished'; payload.updated_at='t3'; await fire();
  out.finishedTimers=timers.map(t=>t.ms);
  console.log(JSON.stringify(out));
})().catch(e=>{ console.error(e); process.exit(1); });
"""
        out = _run(program)
        self.assertEqual(out["first"], ["/api/run/r1", "files", "chat"])
        self.assertEqual(out["delays"], [2000, 3200, 5120, 8192, 10000])
        # Five unchanged ticks: only the run record, except the periodic
        # safety refresh on the sixth quiet tick (not reached here).
        self.assertEqual(out["quiet"], ["/api/run/r1"] * 5)
        self.assertEqual(out["beforeChange"], 10000)
        self.assertEqual(out["changed"], ["/api/run/r1", "files", "chat"])
        self.assertEqual(out["afterChange"], 2000)
        self.assertEqual(out["hidden"], [])
        self.assertEqual(out["hiddenTimers"], 0)
        self.assertEqual(out["resumed"], ["/api/run/r1"])
        self.assertEqual(out["resumedDelay"], 3200)
        # Only the 900 ms final proof refresh remains; no further poll.
        self.assertEqual(out["finishedTimers"], [900])

    def test_quiet_runs_still_get_a_periodic_side_refresh(self) -> None:
        program = POLL_STUBS + _polling_block() + r"""
(async()=>{
  startPolling(); await flush(); calls.splice(0);
  for(let i=0;i<6;i++) await fire();
  console.log(JSON.stringify(calls.filter(c=>c!=='/api/run/r1')));
})().catch(e=>{ console.error(e); process.exit(1); });
"""
        self.assertEqual(_run(program), ["files", "chat"])

    def test_restarting_while_a_tick_is_in_flight_keeps_one_loop(self) -> None:
        program = POLL_STUBS + _polling_block() + r"""
(async()=>{
  startPolling(); startPolling(); await flush();
  console.log(JSON.stringify({timers:timers.length}));
})().catch(e=>{ console.error(e); process.exit(1); });
"""
        self.assertEqual(_run(program), {"timers": 1})

    def test_the_runs_list_refresh_skips_hidden_tabs(self) -> None:
        self.assertIn("setInterval(()=>{ if(!document.hidden)loadRuns()", CONSOLE_SCRIPT)
        self.assertNotIn("setTimeout(loop, 400)", CONSOLE_SCRIPT)


class LeanAttemptLabelTests(unittest.TestCase):
    """Live: 'r0/statement: verified · sorry 4' for a sorry-bearing skeleton."""

    def test_rows_show_what_the_round_established(self) -> None:
        program = "\n".join(
            _function(CONSOLE_SCRIPT, name)
            for name in ("leanAttemptStatus", "leanAttemptLabel", "leanAttemptAxioms")
        ) + r"""
const d={status:'incomplete'};
const rows=[
  {round:0,action:'statement',sorry_count:4,check:{ok:true,status:'verified'}},
  {round:1,action:'prove',sorry_count:0,check:{ok:true,status:'verified',axioms:{ok:true,decl:'main',axioms:['propext']}}},
  {round:5,action:'check',sorry_count:1,check:{ok:true,status:'verified'}},
  {round:10,action:'compile',sorry_count:1,check:{ok:true,status:'verified'}},
  {round:2,action:'sorry-repair',sorry_count:3,check:{ok:false,status:'error'}},
  {round:3,action:'check',sorry_count:0,check:{ok:true,status:'verified',reports_sorry:true}},
  {round:4,action:'check',sorry_count:0,result_status:'unfaithful',check:{ok:true,status:'verified'}},
];
console.log(JSON.stringify({
  labels:rows.map(a=>leanAttemptLabel(a,d)),
  axioms:rows.map(a=>leanAttemptAxioms(a)),
}));
"""
        out = _run(program)
        self.assertEqual(
            out["labels"],
            ["skeleton compiled", "verified", "compiled with sorry", "compiled", "error",
             "compiled with sorry", "unfaithful"],
        )
        self.assertEqual(out["axioms"][1], "main depends on [propext]")
        self.assertEqual(out["axioms"][0], "")


DAG_STUBS = r"""
function dagLookup(table,key){ return Object.prototype.hasOwnProperty.call(table,String(key))?table[String(key)]:undefined; }
function dagKindColor(){ return '#64748b'; }
function dagAndGroupColor(){ return '#8b5cf6'; }
function dagScore(){ return null; }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const CGM_KIND_LABEL={goal:'Goal',plan:'Plan'};
const CGM_VERDICT_COLOR={};
const LEAN_KIND_COLOR={};
const CGM_STATUS={open:{cls:'',tone:'',label:'open'},closed:{cls:'cgm-closed',tone:'ok',label:'closed'},dead:{cls:'cgm-dead',tone:'bad',label:'dead'}};
"""


class DagPresentationTests(unittest.TestCase):
    def _program(self, body: str) -> str:
        names = (
            "dagMathSafeClip", "dagSharedPrefixLengths", "dagCardTitle", "dagGoalSuperseded",
            "dagNodeVisual", "coverageStatementAudited", "dagFormalCheckHtml",
            "dagEdgeGeometry", "dagCubicPoint", "dagPathObstacles", "dagRouteAround",
        )
        return DAG_STUBS + "\n".join(_function(CONSOLE_SCRIPT, n) for n in names) + "\n" + body

    def test_fact_titles_never_cut_inside_math_and_drop_the_shared_preamble(self) -> None:
        preamble = (
            "Definition: an integer m is even if and only if there exists an integer t "
            "with m = 2t. "
        )
        nodes = [
            {"id": "L2", "label": (preamble + "For all natural numbers $n")[:119] + "…",
             "statement": preamble + "For all natural numbers $n$, the number $n(n+1)$ is even."},
            {"id": "L3", "label": (preamble + "For all natural numbers $n")[:119] + "…",
             "statement": preamble + "For all natural numbers $n$ and $k$, $2nk$ is even."},
            {"id": "T", "label": "Prove that the sum of two odd integers is even.",
             "statement": "Prove that the sum of two odd integers is even."},
        ]
        out = _run(self._program(r"""
const nodes=%s;
const view={role:'fact-dependency'}; view.sharedPrefix=dagSharedPrefixLengths(nodes);
console.log(JSON.stringify({
  titles:nodes.map(n=>dagCardTitle(n,view)),
  clip:dagMathSafeClip('For all natural numbers $n \\geq 1$ and all $k$',30),
  serverCut:dagMathSafeClip('abc $n…',120),
  whole:dagMathSafeClip('short $x$',120),
}));
""" % json.dumps(nodes)))
        l2, l3, target = out["titles"]
        self.assertNotEqual(l2, l3)
        for title in (l2, l3):
            self.assertTrue(title.startswith("… For all natural numbers $n$"), title)
            self.assertEqual(title.count("$") % 2, 0, title)
        self.assertEqual(target, "Prove that the sum of two odd integers is even.")
        self.assertEqual(out["clip"], "For all natural numbers…")
        self.assertEqual(out["serverCut"], "abc…")
        self.assertEqual(out["whole"], "short $x$")

    def test_and_members_left_open_after_the_target_closed_read_not_needed(self) -> None:
        out = _run(self._program(r"""
const goal={id:'S2',kind:'goal',meta:{workflow_kind:'goal',status:'open',and_group:'plan-1'}};
const root={id:'goal:root',kind:'goal',meta:{workflow_kind:'goal',status:'closed'}};
const closedView={role:'workflow',roots:new Set(['goal:root']),rootClosed:true};
const openView={role:'workflow',roots:new Set(['goal:root']),rootClosed:false};
const a=dagNodeVisual(goal,closedView), b=dagNodeVisual(goal,openView), r=dagNodeVisual(root,closedView);
console.log(JSON.stringify({a:[a.status.label,a.classes], b:b.status.label, r:r.status.label,
  chip:a.chips.find(c=>c.text==='AND plan-1').title}));
"""))
        self.assertEqual(out["a"], ["not needed", ["cgm-superseded"]])
        self.assertEqual(out["b"], "open")
        self.assertEqual(out["r"], "closed")
        self.assertNotIn("every subgoal of this plan must close", out["chip"])
        legend = CONSOLE_SCRIPT[CONSOLE_SCRIPT.index("function renderDagLegend(") :][:3000]
        self.assertNotIn("every subgoal of the plan must close", legend)

    def test_only_audited_statements_get_the_solid_lean_check(self) -> None:
        out = _run(self._program(r"""
console.log(JSON.stringify({
  lemma:dagFormalCheckHtml({status:'verified',statement_audited:false,lean_declarations:['int_add_closure']}),
  main:dagFormalCheckHtml({status:'verified',statement_audited:true,lean_declarations:['main']}),
  legacyLemma:coverageStatementAudited({reason:'Explicitly linked (`Informal:` doc line) to a verified Lean declaration'}),
  legacyConclusion:coverageStatementAudited({reason:'Faithful Lean conclusion'}),
  legacyMain:coverageStatementAudited({reason:'Explicitly linked',lean_declarations:['main']},{formal_verified:true}),
  legacyMainUntrusted:coverageStatementAudited({reason:'Explicitly linked',lean_declarations:['main']},{formal_verified:false}),
}));
"""))
        self.assertIn("Lean · compiled", out["lemma"])
        self.assertIn("statement not audited", out["lemma"])
        self.assertNotIn("✓ Lean", out["lemma"])
        self.assertIn("✓ Lean", out["main"])
        self.assertFalse(out["legacyLemma"])
        self.assertTrue(out["legacyConclusion"])
        self.assertTrue(out["legacyMain"])
        self.assertFalse(out["legacyMainUntrusted"])

    def test_skip_layer_edges_detour_around_intermediate_cards(self) -> None:
        out = _run(self._program(r"""
const a={x:0,y:0,w:230,h:80}, c={x:0,y:144,w:230,h:80}, b={x:0,y:288,w:230,h:80};
const posXY=new Map([['L1',a],['L3',c],['T',b]]);
const route={curveOffset:0,labelOffset:0};
const g=dagRouteAround(a,b,route,posXY);
const n=g.d.match(/-?\d+(?:\.\d+)?/g).map(Number);
// M x y C 3 points L x y C 3 points
const segs=[[[n[0],n[1]],[n[2],n[3]],[n[4],n[5]],[n[6],n[7]]],[[n[6],n[7]],[n[6],n[7]],[n[8],n[9]],[n[8],n[9]]],[[n[8],n[9]],[n[10],n[11]],[n[12],n[13]],[n[14],n[15]]]];
const direct=dagRouteAround(a,c,route,posXY);
console.log(JSON.stringify({hits:dagPathObstacles(segs,[c]).length, skip:g.skipLayer,
  directHasOneCurve:(direct.d.match(/C/g)||[]).length, straightHits:dagPathObstacles([[[115,80],[115,184],[115,184],[115,288]]],[c]).length}));
"""))
        self.assertEqual(out["straightHits"], 1)
        self.assertEqual(out["hits"], 0)
        self.assertFalse(out["skip"])
        self.assertEqual(out["directHasOneCurve"], 1)


class IdlePillTests(unittest.TestCase):
    """Live: a second tab showed 'idle' while the account's run was running."""

    def test_pill_reflects_running_runs_when_nothing_is_selected(self) -> None:
        program = r"""
let runId=null; const pill={textContent:'idle',classList:{has:false,contains(){return this.has;}}};
const $=()=>pill;
function setPill(s,t){ pill.textContent=t; pill.classList.has=s==='running'; }
""" + _function(CONSOLE_SCRIPT, "runningRunCount") + _function(CONSOLE_SCRIPT, "syncIdlePill") + r"""
const out=[];
const jobs=[{run_id:'a',status:'running'}], manifest=[{run_id:'a',status:'running'},{run_id:'b',status:'finished'}];
syncIdlePill(runningRunCount(jobs,manifest)); out.push(pill.textContent);
syncIdlePill(0); out.push(pill.textContent);
pill.textContent='starting…'; syncIdlePill(2); out.push(pill.textContent);
runId='x'; pill.textContent='finished · kimi'; syncIdlePill(3); out.push(pill.textContent);
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), ["1 run running", "idle", "starting…", "finished · kimi"])


class MonitorUsageDisplayTests(unittest.TestCase):
    """Live: '$0.0' top agents, 'in 0 · out 0' nodes, 'Pipeline stages 1–9'."""

    def test_costs_usage_and_stage_count(self) -> None:
        program = "\n".join(
            [
                _const_arrow(MONITOR, "fmtN"),
                _const_arrow(MONITOR, "fmtUsd"),
                _function(MONITOR, "agentUsageKnown"),
                _function(MONITOR, "agentMonitor"),
                _function(MONITOR, "agentTokenLine"),
                _function(MONITOR, "pipelineStageNote"),
            ]
        ) + r"""
const marker={input_tokens:null,output_tokens:null,total_input_tokens:0,monitor:{cost_tier:'critical'}};
const call={input_tokens:1644,output_tokens:2381,total_input_tokens:1644,cost_usd:0.02792,monitor:{cost_tier:'critical'}};
console.log(JSON.stringify({
  usd:[fmtUsd(0),fmtUsd(0.004),fmtUsd(0.02792),fmtUsd(0.176),fmtUsd(12.3456),fmtUsd(250.4)],
  tiers:[agentMonitor(marker).cost_tier,agentMonitor(call).cost_tier],
  lines:[agentTokenLine(marker),agentTokenLine(call)],
  note:[pipelineStageNote({pipeline:[1,2,3,4,5]}),pipelineStageNote({})],
}));
"""
        out = _run(program)
        self.assertEqual(out["usd"], ["$0", "<$0.01", "$0.03", "$0.18", "$12.35", "$250"])
        self.assertEqual(out["tiers"], ["untracked", "critical"])
        self.assertIn("no usage of its own", out["lines"][0])
        self.assertIn("in <b>1.6k</b>", out["lines"][1])
        self.assertIn("$0.03", out["lines"][1])
        self.assertEqual(out["note"], ["Pipeline stages 1–5.", ""])
        self.assertNotIn("'Pipeline stages 1–9.'", MONITOR)


if __name__ == "__main__":
    unittest.main()
