"""Console contract for the Informal · CGM (Explore / Facts) presentation.

These tests run the real console functions under node with a tiny DOM stub, so
they pin behaviour (status styling, lanes, honest copy, escaping) rather than
exact markup.
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
# What the Ansatz CGM descriptions say about the written-proof round.
WRITEUP_COPY = "by default, accepted runs end with a written proof, checked as a whole"
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = function_start - 6 if SCRIPT[function_start - 6 : function_start] == "async " else function_start
    opening = SCRIPT.index("{", SCRIPT.index(")", function_start))
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


def _js_const(name: str) -> str:
    start = SCRIPT.index(f"const {name}=")
    depth = 0
    for index in range(start + len(f"const {name}="), len(SCRIPT)):
        char = SCRIPT[index]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == ";" and depth == 0:
            return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript const: {name}")


def _run_node(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


CONSTS = (
    "DAG_KIND_COLOR",
    "LEAN_KIND_COLOR",
    "CGM_KIND_LABEL",
    "CGM_LANES",
    "CGM_STATUS",
    "CGM_VERDICT_COLOR",
    "CGM_EDGE_CLASS",
    "CGM_AND_COLORS",
)
HELPERS = (
    "escapeHtml",
    "dagLookup",
    "dagKindColor",
    "dagIsCgmEngine",
    "isCompilerFactsGraph",
    "formatLeanStm",
    "dagStatus",
    "setDagObservation",
    "dagLayers",
    "dagCgmLane",
    "dagCgmLanes",
    "dagEdgeRoutes",
    "dagEdgeGeometry",
    "dagCubicPoint",
    "dagPathObstacles",
    "dagRouteAround",
    "dagEdgeSvg",
    "dagAndGroupColor",
    "dagScore",
    "dagGoalSuperseded",
    "dagMathSafeClip",
    "dagSharedPrefixLengths",
    "dagCardTitle",
    "coverageStatementAudited",
    "dagFormalCheckHtml",
    "dagNodeVisual",
    "dagEngineMeaning",
    "renderDagLegend",
    "renderDag",
    "dagHighlight",
    "closeDagDetail",
    "selectDagNode",
    "dagDetailText",
    "renderDagDetail",
    "dagRebuildReloadsOnly",
    "syncDagRebuild",
)

DOM_STUB = r"""
class El {
  constructor(tag,id){ this.tagName=tag; this.id=id||''; this.children=[]; this.style={setProperty:(k,v)=>{this.style[k]=v;}}; this.dataset={};
    this.attrs={}; this.hidden=false; this.disabled=false; this.title=''; this._text=''; this._html=null; this.offsetHeight=80; this.clientWidth=1200;
    const self=this; this.classList={set:new Set(),add(c){this.set.add(c);},remove(c){this.set.delete(c);},toggle(c,on){ if(on===undefined?!this.set.has(c):on)this.set.add(c); else this.set.delete(c); },contains(c){return this.set.has(c);}}; }
  set innerHTML(v){ this._html=String(v); this.children=[]; }
  get innerHTML(){ return this._html===null?'':this._html; }
  insertAdjacentHTML(pos,html){ this._html=(this._html||'')+html; }
  set textContent(v){ this._text=String(v); this.children=[]; this._html=null; }
  get textContent(){ return this._text+this.children.map(c=>c.textContent).join(''); }
  set className(v){ this.classList.set=new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className(){ return [...this.classList.set].join(' '); }
  setAttribute(k,v){ this.attrs[k]=String(v); }
  append(...nodes){ this.children.push(...nodes); }
  replaceChildren(...nodes){ this.children=nodes; this._html=null; this._text=''; }
  querySelectorAll(){ return []; }
  querySelector(){ return null; }
}
const elements={};
const $=id=>elements[id]||(elements[id]=new El('div',id));
const document={getElementById:id=>id?$(id):null,querySelectorAll:()=>[],createElement:tag=>new El(tag)};
['dag-meta','dag-observation-note','dag-empty','dag-stage','dag-nodes','dag-svg','dag-scroll','dag-legend','dag-detail','dag-rebuild'].forEach($);
function renderMathIn(){}
function dagCoverageForNode(){ return null; }
let runId='run-1', dagMode='engine', dagEngineView='workflow', dagRunKey='', dagDisplayedRunId='', dagDisplayedMode='', dagDisplayedEngineView='';
let lastSandboxRun=null, dagView=null, dagSelectedNodeId=null, dagRebuildBusy=false, CURRENT_USER=null;
"""


def _program(body: str) -> str:
    source = "\n".join(
        [DOM_STUB]
        + [_js_const(name) for name in CONSTS]
        + [_js_function(name) for name in HELPERS]
    )
    return source + "\n" + body


def _explore_payload(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {
        "kind": "engine",
        "graph_role": "workflow",
        "status": "complete",
        "source_sha256": "a" * 64,
        "selection": {"target_ids": ["goal:root"]},
        "provenance": {"engine": "math_harness", "pipeline": "cgm", "attestation": "unattested"},
        "graph": {"title": "t", "nodes": nodes, "edges": edges or []},
    }


def _node(node_id: str, kind: str, **meta: object) -> dict:
    return {
        "id": node_id,
        "kind": "claim",
        "label": node_id,
        "statement": f"statement of {node_id}",
        "citations": [],
        "meta": {"workflow_kind": kind, **meta},
    }


class CgmVisualTests(unittest.TestCase):
    def test_workflow_kinds_status_score_technique_and_group_are_rendered(self) -> None:
        nodes = [
            _node("goal:root", "goal", status="closed", score=1.0),
            _node("plan-a", "plan", status="dead", score=0, technique="reduction",
                  decomposition_technique="plain", and_group="plan-2"),
            _node("goal:p1-s1", "goal", status="blocked", score=0.4, technique="plain", and_group="plan-1"),
            _node("att-1", "proof_attempt", status="unverified"),
            _node("att-2", "proof_attempt", status="verified"),
            _node("ver-1", "verification", status="supported", verdict="correct"),
            _node("obs-1", "obstacle", status="open"),
            _node("dead-1", "dead_end", status="open"),
            _node("cx-1", "counterexample", status="unverified"),
            _node("ex-1", "example", status="unverified"),
            _node("con-1", "conclusion", status="unverified"),
            _node("odd", "goal", status="constructor", score="__proto__"),
            _node("big", "goal", status="active", score=7),
        ]
        program = _program(
            "const view={isEngine:true,isLean:false,isCompiler:false,role:'workflow',roots:new Set(['goal:root'])};\n"
            + "const nodes=%s;\n" % json.dumps(nodes)
            + "console.log(JSON.stringify(nodes.map(n=>[n.id,dagNodeVisual(n,view)])));"
        )
        visuals = dict(_run_node(program))

        root = visuals["goal:root"]
        self.assertEqual(root["badge"], "Problem")
        self.assertIn("cgm-root", root["classes"])
        self.assertIn("cgm-closed", root["classes"])
        self.assertEqual(root["score"], 1)

        plan = visuals["plan-a"]
        self.assertEqual(plan["badge"], "Plan")
        self.assertIn("cgm-dead", plan["classes"])
        self.assertEqual(plan["status"], {"label": "dead", "tone": "bad"})
        self.assertEqual(plan["score"], 0)
        chip_text = [chip["text"] for chip in plan["chips"]]
        self.assertIn("⚙ reduction", chip_text)
        self.assertIn("AND plan-2", chip_text)

        self.assertIn("cgm-blocked", visuals["goal:p1-s1"]["classes"])
        self.assertIn("cgm-unverified", visuals["att-1"]["classes"])
        # An attempt's "verified" is the model verifier's verdict, never Lean's.
        self.assertEqual(visuals["att-2"]["status"], {"label": "model-verified", "tone": "ok"})
        self.assertIn("verdict correct", [c["text"] for c in visuals["ver-1"]["chips"]])
        for node_id, kind in (("obs-1", "obstacle"), ("dead-1", "dead_end"),
                              ("cx-1", "counterexample"), ("ex-1", "example"),
                              ("con-1", "conclusion")):
            self.assertNotEqual(visuals[node_id]["color"], "#64748b", kind)
        self.assertEqual(visuals["dead-1"]["badge"], "Dead end")
        self.assertEqual(visuals["cx-1"]["badge"], "Counterexample")
        # Prototype-shaped statuses and non-numeric scores never style a card.
        self.assertIsNone(visuals["odd"]["status"])
        self.assertEqual(visuals["odd"]["classes"], [])
        self.assertIsNone(visuals["odd"]["score"])
        self.assertEqual(visuals["big"]["score"], 1)
        self.assertIn("cgm-active", visuals["big"]["classes"])

    def test_facts_nodes_show_target_label_and_verifier_acceptance(self) -> None:
        nodes = [
            {"id": "f1", "kind": "lemma", "label": "a", "statement": "s", "citations": [],
             "meta": {"label": "L1", "depth": 0, "is_target": False, "verifier_accepted": True}},
            {"id": "t", "kind": "conclusion", "label": "b", "statement": "s", "citations": [],
             "meta": {"label": "THEOREM", "depth": 1, "is_target": True, "verifier_accepted": True}},
            {"id": "u", "kind": "lemma", "label": "c", "statement": "s", "citations": [],
             "meta": {"is_target": False, "verifier_accepted": False}},
        ]
        program = _program(
            "const view={isEngine:true,isLean:false,isCompiler:false,role:'fact-dependency',roots:new Set()};\n"
            + "const nodes=%s;\n" % json.dumps(nodes)
            + "console.log(JSON.stringify(nodes.map(n=>dagNodeVisual(n,view))));"
        )
        fact, target, unattested = _run_node(program)
        self.assertEqual(fact["badge"], "Fact")
        self.assertEqual(fact["status"]["label"], "✓ model-verifier")
        self.assertIn("L1", [c["text"] for c in fact["chips"]])
        self.assertEqual(target["badge"], "Target")
        self.assertIn("cgm-target", target["classes"])
        self.assertEqual(unattested["status"]["label"], "unattested")
        self.assertIn("cgm-unattested", unattested["classes"])

    def test_explore_lanes_follow_the_cgm_pipeline_and_wrap(self) -> None:
        nodes = [
            _node("fact-1", "fact"),
            _node("goal:root", "goal"),
            _node("obs", "obstacle"),
            *[_node(f"goal:s{i}", "goal") for i in range(5)],
            _node("plan-1", "plan"),
            _node("ver", "verification"),
            _node("att", "proof_attempt"),
            _node("__proto__", "conclusion"),
        ]
        edges = [{"from": "plan-1", "to": f"goal:s{i}", "label": "decomposes-into"} for i in range(5)]
        program = _program(
            "const graph={nodes:%s,edges:%s};\n" % (json.dumps(nodes), json.dumps(edges))
            + "const rows=dagCgmLanes(graph,['goal:root'],3);\n"
            + "console.log(JSON.stringify(rows.map(r=>[r.lane?r.lane.id:null,r.nodes.map(n=>n.id)])));"
        )
        rows = _run_node(program)
        self.assertEqual(
            [lane for lane, _ in rows],
            ["root", "plan", "goal", None, "proof_attempt", "verification", "fact", "finding"],
        )
        self.assertEqual(rows[0][1], ["goal:root"])
        self.assertEqual(len(rows[2][1]) + len(rows[3][1]), 5)
        self.assertEqual(rows[-1][1], ["obs", "__proto__"])

    def test_edges_route_up_down_and_within_a_row(self) -> None:
        program = _program(
            """
const a={x:0,y:0,w:100,h:50}, b={x:0,y:200,w:100,h:50}, c={x:300,y:0,w:100,h:50};
const route={curveOffset:0,labelOffset:0};
console.log(JSON.stringify({
  down:dagEdgeGeometry(a,b,route).d,
  up:dagEdgeGeometry(b,a,route).d,
  row:dagEdgeGeometry(a,c,route).d
}));
"""
        )
        geometry = _run_node(program)
        self.assertTrue(geometry["down"].startswith("M 50 50 "))
        self.assertTrue(geometry["down"].endswith(", 50 200"))
        self.assertTrue(geometry["up"].startswith("M 50 200 "))
        self.assertTrue(geometry["up"].endswith(", 50 50"))
        self.assertTrue(geometry["row"].startswith("M 50 0 C 50 -24"))

    def test_facts_view_never_shows_an_execution_trace_and_empty_states_are_honest(self) -> None:
        program = _program(
            _js_function("dagPayloadForMode")
            + "\n"
            + _js_function("dagUnavailableState")
            + """
const trace={kind:'engine',graph_role:'execution',provenance:{engine:'claude'},graph:{nodes:[{id:'a'}],edges:[]}};
const facts={kind:'engine',graph_role:'fact-dependency',graph:{nodes:[{id:'f'}],edges:[]}};
console.log(JSON.stringify({
  traceAsFacts:dagPayloadForMode(trace,'engine','facts'),
  traceAsExplore:dagPayloadForMode(trace,'engine','workflow')?.graph_role,
  factsAsFacts:dagPayloadForMode(facts,'engine','facts')?.graph_role,
  cgmFacts:dagUnavailableState('engine',trace,'facts','kimi'),
  otherFacts:dagUnavailableState('engine',trace,'facts','claude'),
  cgmExplore:dagUnavailableState('engine',null,'workflow','math_harness'),
  meaningFacts:dagEngineMeaning({provenance:{attestation:'harness-gate-receipt'}},'fact-dependency'),
  meaningUnattested:dagEngineMeaning({provenance:{attestation:'unattested'}},'fact-dependency'),
  meaningMissing:dagEngineMeaning({provenance:{attestation:'unattested',receipt_status:'missing'}},'fact-dependency'),
  meaningInvalid:dagEngineMeaning({provenance:{attestation:'unattested',receipt_status:'invalid'}},'fact-dependency'),
  meaningPartial:dagEngineMeaning({provenance:{attestation:'unattested',receipt_status:'verifier-partial'}},'fact-dependency'),
  meaningTrace:dagEngineMeaning({provenance:{engine:'claude'}},'execution'),
  meaningCgmTrace:dagEngineMeaning({provenance:{engine:'kimi'}},'execution')
}));
"""
        )
        output = _run_node(program)
        self.assertIsNone(output["traceAsFacts"])
        self.assertEqual(output["traceAsExplore"], "execution")
        self.assertEqual(output["factsAsFacts"], "fact-dependency")
        self.assertEqual(output["cgmFacts"]["status"], "no verifier-accepted facts yet")
        self.assertIn("never a Lean certificate", output["cgmFacts"]["html"])
        self.assertEqual(output["otherFacts"]["status"], "no CGM facts for this harness")
        self.assertIn("Facts are published only by Ansatz CGM runs", output["otherFacts"]["html"])
        self.assertIn("Neither is a Lean certificate", output["otherFacts"]["html"])
        self.assertEqual(output["cgmExplore"]["status"], "CGM exploration graph not published yet")
        self.assertEqual(
            output["meaningFacts"],
            "model-verifier-accepted fact dependencies (not a Lean certificate)",
        )
        self.assertIn("unattested", output["meaningUnattested"])
        self.assertIn("no valid verifier receipt", output["meaningMissing"])
        self.assertIn("no valid verifier receipt", output["meaningInvalid"])
        # A valid receipt whose target was not accepted: the lemmas still were.
        self.assertEqual(
            output["meaningPartial"],
            "lemmas individually accepted by the model verifier; target theorem not accepted (not a Lean certificate)",
        )
        self.assertIn("agent execution — not proof logic", output["meaningTrace"])
        self.assertIn("CGM Facts/Explore come only from Ansatz CGM runs", output["meaningTrace"])
        self.assertIn("no CGM graph published", output["meaningCgmTrace"])

    def test_render_escapes_model_strings_and_never_uses_them_as_dom_ids(self) -> None:
        hostile = '<img src=x onerror="alert(1)">'
        nodes = [
            _node("goal:root", "goal", status="closed", comment=hostile, technique=hostile, and_group=hostile),
            {**_node('"><svg onload=alert(2)>', "plan", status="open", technique=hostile), "label": hostile, "statement": hostile},
            _node("constructor", "__proto__", status="toString", verdict="constructor"),
        ]
        edges = [{"from": '"><svg onload=alert(2)>', "to": "goal:root", "label": hostile}]
        payload = _explore_payload(nodes, edges)
        outline = {
            "kind": "informal",
            "model": hostile,
            "generated_at": hostile,
            "graph": {
                "title": hostile,
                "nodes": [
                    {"id": '"><b id=x>', "kind": "constructor", "label": hostile, "statement": hostile, "citations": [hostile]},
                    {"id": "toString", "kind": "claim", "label": "ok", "statement": "", "citations": []},
                ],
                "edges": [{"from": '"><b id=x>', "to": "toString", "label": hostile}],
            },
        }
        program = _program(
            "renderDag(%s);\n" % json.dumps(payload)
            + "const html=elements['dag-nodes'].innerHTML+elements['dag-svg'].innerHTML+elements['dag-legend'].innerHTML;\n"
            + "const ids=[...html.matchAll(/ id=\"([^\"]*)\"/g)].map(m=>m[1]);\n"
            + "selectDagNode(%s);\n" % json.dumps(nodes[1])
            + "const detail=elements['dag-detail'];\n"
            + "const walk=el=>[el.tagName,...el.children.flatMap(walk)];\n"
            + "const detailState={hidden:detail.hidden,text:detail.textContent,tags:walk(detail),html:detail._html};\n"
            + "dagMode='informal'; dagRunKey='';\n"
            + "renderDag(%s);\n" % json.dumps(outline)
            + "const outlineHtml=elements['dag-nodes'].innerHTML+elements['dag-svg'].innerHTML+elements['dag-legend'].innerHTML;\n"
            + "console.log(JSON.stringify({html,ids,detailHidden:detailState.hidden,detailText:detailState.text,detailTags:detailState.tags,detailHtml:detailState.html,outlineHtml}));"
        )
        output = _run_node(program)
        html = output["html"]
        self.assertNotIn("<img", html)
        self.assertNotIn("<svg onload", html)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", html)
        self.assertTrue(all(re.fullmatch(r"dagn-\d+", value) or value == "dag-arrow" for value in output["ids"]), output["ids"])
        # Unknown kinds / statuses fall back to fixed colours and add no classes.
        self.assertNotIn("function", html)
        self.assertFalse(output["detailHidden"])
        self.assertIn(hostile, output["detailText"])
        self.assertIsNone(output["detailHtml"])
        self.assertNotIn("img", output["detailTags"])
        outline_html = output["outlineHtml"]
        self.assertNotIn("<img", outline_html)
        self.assertNotIn("<b id=x>", outline_html)
        self.assertIn('class="dag-edge-label"', outline_html)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</text>", outline_html)
        self.assertNotIn("function", outline_html)
        outline_ids = re.findall(r' id="([^"]*)"', outline_html)
        self.assertTrue(outline_ids)
        self.assertTrue(all(re.fullmatch(r"dagn-\d+|dag-arrow", value) for value in outline_ids), outline_ids)
        for name in ("renderDagDetail", "dagDetailText"):
            self.assertNotIn("innerHTML", _js_function(name))

    def test_rendered_explore_has_lanes_legend_and_honest_status(self) -> None:
        nodes = [
            _node("goal:root", "goal", status="closed"),
            _node("plan-1", "plan", status="open", technique="extremal"),
            _node("fact-1", "fact"),
        ]
        edges = [
            {"from": "plan-1", "to": "goal:root", "label": "addresses"},
            {"from": "fact-1", "to": "goal:root", "label": "supports"},
        ]
        program = _program(
            "renderDag(%s);\n" % json.dumps(_explore_payload(nodes, edges))
            + "console.log(JSON.stringify({nodes:elements['dag-nodes'].innerHTML,svg:elements['dag-svg'].innerHTML,legend:elements['dag-legend'].innerHTML,legendHidden:elements['dag-legend'].hidden,meta:elements['dag-meta'].textContent,note:elements['dag-observation-note'].textContent,stage:[...elements['dag-stage'].classList.set]}));"
        )
        output = _run_node(program)
        self.assertIn("dag-lane-label", output["nodes"])
        self.assertIn("Strategist", output["nodes"])
        self.assertIn("e-support", output["svg"])
        self.assertNotIn("dag-edge-label", output["svg"])
        self.assertFalse(output["legendHidden"])
        self.assertIn("Plan", output["legend"])
        self.assertIn("AND plan-k", output["legend"])
        self.assertIn("not proof logic", output["meta"])
        self.assertIn("never a Lean certificate", output["note"])
        self.assertIn("cgm-workflow", output["stage"])


class CgmCopyAndControlsTests(unittest.TestCase):
    def test_modes_keep_one_button_each_and_cgm_is_the_default(self) -> None:
        start = HTML.index('id="dag-mode"')
        modes = HTML[start : HTML.index("</div>", start)]
        for mode in ("analysis", "paper-map", "engine", "informal", "formal", "compiler"):
            self.assertEqual(modes.count(f'data-mode="{mode}"'), 1)
        self.assertIn('class="on" data-mode="engine"', modes)
        # The manuscript modes (an analysis, a readable version) are shown only for their runs.
        self.assertIn('data-mode="paper-map" hidden', modes)
        self.assertIn(">Informal · CGM</button>", modes)
        self.assertIn(">Outline</button>", modes)
        self.assertIn('id="dag-detail"', HTML)
        self.assertIn('id="dag-legend"', HTML)
        self.assertNotIn("critic-accepted", HTML)
        self.assertIn("model-verifier-accepted fact dependencies (not a Lean certificate)", SCRIPT)

    def test_launcher_lists_cgm_engines_first_with_descriptions(self) -> None:
        program = (
            _js_function("dagIsCgmEngine")
            + "\n"
            + _js_function("engineMenuOrder")
            + "\n"
            + _js_const("CGM_ENGINE_COPY")
            + """
const ids=list=>engineMenuOrder(list).map(e=>e.id);
const order=ids([{id:'hermes'},{id:'plain'},{id:'math_harness'},{id:'codex'},{id:'kimi'}]);
const guest=ids([{id:'hermes',requires_login:true},{id:'math_harness',requires_login:true},{id:'plain'},{id:'kimi'}]);
const loginOnlyKimi=ids([{id:'kimi',requires_login:true},{id:'math_harness'},{id:'hermes'}]);
console.log(JSON.stringify({order,guest,loginOnlyKimi,kimi:CGM_ENGINE_COPY.kimi,mh:CGM_ENGINE_COPY.math_harness}));
"""
        )
        output = _run_node(program)
        # The Kimi K3 default leads the CGM group; other engines keep registry order.
        self.assertEqual(output["order"], ["kimi", "math_harness", "hermes", "plain", "codex"])
        # A guest never sees the sign-in-only CGM harness above their default.
        self.assertEqual(output["guest"], ["kimi", "math_harness", "hermes", "plain"])
        # An engine usable in this session comes before a sign-in-only one.
        self.assertEqual(output["loginOnlyKimi"], ["math_harness", "kimi", "hermes"])
        self.assertIn("strategist → workers → independent verifier → curator", output["kimi"])
        self.assertIn("Kimi K3", output["kimi"])
        self.assertIn("own Codex, Claude Code or API route", output["mh"])
        # Accepted runs also end with a written proof (unless the operator turned the round off),
        # said early enough to be read: the menu shows three lines of an Ansatz CGM description
        # (125 to 137 characters of these in Chromium, with the console's CSS and font).
        for copy in (output["kimi"], output["mh"]):
            self.assertLessEqual(copy.index(WRITEUP_COPY) + len(WRITEUP_COPY), 130)
        self.assertIn(
            ".choice-scroll .cgm-engine .opt-desc { white-space:normal; display:-webkit-box; "
            "-webkit-box-orient:vertical; -webkit-line-clamp:3; line-clamp:3; }",
            HTML,
        )
        self.assertIn("engine='kimi';", SCRIPT)
        self.assertIn("if(u.guest)engine=GUEST_POLICY?.default_engine||'kimi';", SCRIPT)

    def test_ansatz_cgm_is_one_launcher_choice_backed_by_both_engines(self) -> None:
        program = (
            "\n".join(
                _js_function(name)
                for name in (
                    "selectedAccountRuntime", "runtimeDisplayName", "engineRuntimeIssue",
                    "engineModelMode", "modelsForEngine", "dagIsCgmEngine", "engineMenuOrder",
                    "cgmEngineUsable", "cgmModels", "cgmEngineFor", "engineMenuEntries", "engineView",
                    "cgmMaxAvailable",
                )
            )
            + "\n"
            + "\n".join(
                _js_const(name)
                for name in (
                    "ENGINE_MARK", "ENGINE_TONE", "CGM_ENGINE_COPY", "CGM_FAMILY", "ENGINE_AUTH_LABELS",
                    "CGM_MAX_MODEL",
                )
            )
            + """
let CURRENT_USER={id:1,guest:false}, GUEST_POLICY=null, engine='kimi';
let SETTINGS={account_runtime:'api',api_models:['kimi-k3','gpt-6-astra','gpt-6.1-sol'],sponsored_kimi_available:true,kimi_user_key_set:false,sponsored_kimi_engines:['kimi','plain','math_harness']};
const kimi={id:'kimi',label:'Ansatz CGM · Kimi K3',available:true,auth_modes:['api_key'],supported_models:['kimi-k3']};
const harness={id:'math_harness',label:'Ansatz CGM harness',available:true,auth_modes:['codex_subscription','claude_subscription','api_key']};
const plain={id:'plain',label:'Plain (no harness)',available:true,auth_modes:['codex_subscription','claude_subscription','api_key']};
let ENGINES=[plain,harness,kimi];
const ids=list=>list.map(e=>e.id), out={};
const view=engineView(kimi);
out.admin={entries:ids(engineMenuEntries(ENGINES)),label:view.label,auth:view.auth,usable:view.usable,copy:view.copy,models:cgmModels(),forKimi:cgmEngineFor('kimi-k3'),forAstra:cgmEngineFor('gpt-6-astra')};
engine='math_harness'; out.adminOnHarness=ids(engineMenuEntries(ENGINES)); engine='kimi';
// A member: /api/engines advertises the harness on the API route, own keys only.
ENGINES=[plain,{...harness,auth_modes:['api_key'],own_key_only:true},kimi];
out.member={entries:ids(engineMenuEntries(ENGINES)),models:cgmModels(),forAstra:cgmEngineFor('gpt-6-astra'),forKimi:cgmEngineFor('kimi-k3'),auth:engineView(kimi).auth,copy:engineView(kimi).copy};
// A site that narrowed members to kimi,plain: the harness is administrator-only.
ENGINES=[plain,{...harness,available:false,requires_admin:true},kimi];
out.narrowed={entries:ids(engineMenuEntries(ENGINES)),models:cgmModels(),forAstra:cgmEngineFor('gpt-6-astra'),auth:engineView(kimi).auth,copy:engineView(kimi).copy};
ENGINES=[plain,harness,kimi];
SETTINGS={...SETTINGS,account_runtime:'codex',codex_connected:true,codex_models:['gpt-6-astra','gpt-6-sol']};
out.codex={models:cgmModels(),forKimi:cgmEngineFor('kimi-k3'),usable:engineView(kimi).usable};
// Max: offered first when both accounts are linked, and it runs on the harness.
SETTINGS={...SETTINGS,cgm_max:{available:true,advisor:{model:'fable'},worker:{model:'gpt-6-astra'}}};
out.max={models:cgmModels(),forMax:cgmEngineFor('max')};
SETTINGS={...SETTINGS,cgm_max:{available:false,reason:'connect both'}};
out.noMax=cgmModels();
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(program)
        self.assertEqual(out["admin"]["entries"], ["kimi", "plain"])
        self.assertEqual(out["admin"]["label"], "Ansatz CGM")
        self.assertEqual(out["admin"]["auth"], "Codex / Claude Code / API")
        self.assertTrue(out["admin"]["usable"])
        self.assertEqual(out["admin"]["models"], ["kimi-k3", "gpt-6-astra", "gpt-6.1-sol"])
        self.assertEqual((out["admin"]["forKimi"], out["admin"]["forAstra"]), ("kimi", "math_harness"))
        self.assertIn("Kimi K3 included or your own Codex", out["admin"]["copy"])
        self.assertEqual(out["adminOnHarness"], ["math_harness", "plain"])
        # A member runs Kimi K3 on `kimi` and its own-key models on the harness,
        # still as one Ansatz CGM entry, and never on a subscription.
        self.assertEqual(out["member"]["entries"], ["kimi", "plain"])
        self.assertEqual(out["member"]["models"], ["kimi-k3", "gpt-6-astra", "gpt-6.1-sol"])
        self.assertEqual((out["member"]["forKimi"], out["member"]["forAstra"]), ("kimi", "math_harness"))
        self.assertEqual(out["member"]["auth"], "API")
        self.assertIn("Kimi K3 included or models from your own API keys", out["member"]["copy"])
        for copy in (out["admin"]["copy"], out["member"]["copy"], out["narrowed"]["copy"]):
            self.assertLessEqual(copy.index(WRITEUP_COPY) + len(WRITEUP_COPY), 130)
        self.assertNotIn("Codex", out["member"]["copy"])
        # Narrowed to kimi,plain, the one entry is Kimi K3 only.
        self.assertIn("Ansatz CGM on Kimi K3", out["narrowed"]["copy"])
        self.assertEqual(out["narrowed"]["entries"], ["kimi", "plain"])
        self.assertEqual(out["narrowed"]["models"], ["kimi-k3"])
        self.assertEqual(out["narrowed"]["forAstra"], "kimi")
        self.assertEqual(out["narrowed"]["auth"], "API")
        # On a Codex subscription only the harness can run, with Codex models.
        self.assertEqual(out["codex"]["models"], ["gpt-6-astra", "gpt-6-sol"])
        self.assertEqual(out["codex"]["forKimi"], "math_harness")
        self.assertTrue(out["codex"]["usable"])
        self.assertEqual(out["max"]["models"], ["max", "gpt-6-astra", "gpt-6-sol"])
        self.assertEqual(out["max"]["forMax"], "math_harness")
        self.assertNotIn("max", out["noMax"])

    def test_limit_responses_explain_the_wait_without_failing_the_view(self) -> None:
        program = (
            _js_function("limitMessage")
            + "\n"
            + _js_function("leanResponseJson")
            + """
(async()=>{
  const response=(status,body,retry)=>({ok:false,status,text:async()=>JSON.stringify(body),headers:{get:name=>name==='Retry-After'?retry:null}});
  const out={};
  try{ await leanResponseJson(response(429,{error:'This guest session has reached its hourly Formal and DAG limit'},'1740'),'Verify'); }
  catch(e){ out.hourly={message:e.message,limit:e.limit,status:e.status}; }
  try{ await leanResponseJson(response(429,{error:'Guest Formal and DAG capacity is currently full'},'20'),'Verify'); }
  catch(e){ out.busy=e.message; }
  try{ await leanResponseJson(response(409,{error:'Wait for the automatic Formal and DAG pipeline to finish'},null),'Verify'); }
  catch(e){ out.pipeline={message:e.message,limit:e.limit}; }
  try{ await leanResponseJson({ok:false,status:429,text:async()=>'{}'},'Graph rebuild'); }
  catch(e){ out.bare=e.message; }
  try{ await leanResponseJson(response(403,{error:'Sign in to access the local formal harness'},null),'Harness'); }
  catch(e){ out.denied={message:e.message,limit:!!e.limit}; }
  console.log(JSON.stringify(out));
})();
"""
        )
        output = _run_node(program)
        self.assertEqual(
            output["hourly"]["message"],
            "This guest session has reached its hourly Formal and DAG limit. Try again in about 29 min.",
        )
        self.assertTrue(output["hourly"]["limit"])
        self.assertEqual(output["hourly"]["status"], 429)
        self.assertEqual(
            output["busy"],
            "Guest Formal and DAG capacity is currently full. Try again in about 20 s.",
        )
        self.assertEqual(
            output["pipeline"]["message"],
            "Wait for the automatic Formal and DAG pipeline to finish.",
        )
        self.assertTrue(output["pipeline"]["limit"])
        self.assertIn("HTTP 429", output["bare"])
        self.assertEqual(output["denied"], {"message": "Sign in to access the local formal harness", "limit": False})

    def test_guest_lean_and_dag_actions_follow_the_policy(self) -> None:
        sync = _js_function("syncLeanControls")
        # Harness selector and button stay the operator's, never a guest's ...
        self.assertIn("const harness=viewerIsAdmin();", sync)
        self.assertIn("btn.disabled=!harness||", sync)
        self.assertIn("btn.style.display=harness?'':'none'", sync)
        self.assertIn("engineSelect.disabled=!harness", sync)
        # ... while Verify / Compile / Check / Audit / feedback stay available to guests.
        self.assertIn("['lean-gen','lean-compile','lean-check','lean-audit','lean-chat-send']", sync)
        self.assertIn("el.disabled=!!pending||!!running||pipelineRunning;", sync)
        action = _js_function("leanAction")
        self.assertIn("if(CURRENT_USER?.guest&&!['verify','compile','check','audit','revise'].includes(action))return;", action)
        self.assertIn("err?.limit", action)
        rebuild = _js_function("rebuildDag")
        self.assertIn("'/proof_graph'", rebuild)
        self.assertIn("err?.limit", rebuild)
        self.assertIn("leanResponseJson(r,'Graph rebuild')", rebuild)
        # Engine graphs reload with a GET (see test_refresh_* below), so a
        # guest never spends a Formal/DAG action on a no-op engine rebuild.
        self.assertNotIn("kind:'engine'", rebuild)

    def test_partial_receipt_facts_header_agrees_with_the_cards(self) -> None:
        payload = {
            "kind": "engine",
            "graph_role": "fact-dependency",
            "status": "partial",
            "source_sha256": "b" * 64,
            "selection": {"target_ids": []},
            "provenance": {"engine": "kimi", "pipeline": "cgm", "attestation": "unattested",
                           "receipt_status": "verifier-partial"},
            "graph": {"title": "t", "nodes": [
                {"id": "f1", "kind": "lemma", "label": "a", "statement": "s", "citations": [],
                 "meta": {"label": "L1", "depth": 0, "is_target": False, "verifier_accepted": True}},
            ], "edges": []},
        }
        program = _program(
            "dagEngineView='facts';\n"
            + "renderDag(%s);\n" % json.dumps(payload)
            + "console.log(JSON.stringify({meta:elements['dag-meta'].textContent,note:elements['dag-observation-note'].textContent,nodes:elements['dag-nodes'].innerHTML}));"
        )
        output = _run_node(program)
        self.assertIn("lemmas individually accepted by the model verifier", output["meta"])
        self.assertIn("target theorem not accepted", output["meta"])
        self.assertNotIn("no valid verifier receipt", output["meta"])
        self.assertIn("target not accepted", output["note"])
        self.assertIn("never a Lean certificate", output["note"])
        self.assertIn("✓ model-verifier", output["nodes"])

    def test_detail_panel_names_attempt_verdicts_as_model_verified(self) -> None:
        nodes = [
            _node("goal:root", "goal", status="closed"),
            _node("att-1", "proof_attempt", status="verified", entry_status="unverified"),
        ]
        program = _program(
            "const payload=%s;\n" % json.dumps(_explore_payload(nodes))
            + "renderDag(payload);\n"
            + "selectDagNode(payload.graph.nodes[1]);\n"
            + "console.log(JSON.stringify({text:elements['dag-detail'].textContent,hidden:elements['dag-detail'].hidden}));"
        )
        output = _run_node(program)
        self.assertFalse(output["hidden"])
        self.assertIn("Statusmodel-verified", output["text"])
        self.assertIn("Entry statusunverified", output["text"])
        self.assertNotIn("Statusverified", output["text"])

    def test_refresh_reloads_engine_graphs_without_spending_a_guest_action(self) -> None:
        program = _program(
            _js_function("limitMessage")
            + "\n"
            + _js_function("leanResponseJson")
            + "\n"
            + _js_function("rebuildDag")
            + r"""
let refreshes=0, posts=[], nextResponse=null;
async function refreshDagView(){ refreshes++; }
async function fetch(url,options){ posts.push({url,method:options?.method,body:options?.body?JSON.parse(options.body):null}); return nextResponse; }
const ok={ok:true,status:200,text:async()=>'{"status":"complete"}',headers:{get:()=>null}};
const limited={ok:false,status:429,text:async()=>JSON.stringify({error:'This guest session has reached its hourly Formal and DAG limit'}),headers:{get:name=>name==='Retry-After'?'600':null}};
const btn=elements['dag-rebuild'];
const snap=()=>({refreshes,posts:posts.slice(),text:btn.textContent,title:btn.title,disabled:btn.disabled,status:elements['dag-meta'].textContent});
(async()=>{
  CURRENT_USER={guest:true};
  const out={};
  dagMode='engine'; syncDagRebuild(); await rebuildDag(); out.engine=snap();
  dagMode='informal'; dagView={runId:'run-1',mode:'informal',payload:{model:'cgm-fact-graph'}}; syncDagRebuild(); await rebuildDag(); out.cgmOutline=snap();
  dagView={runId:'run-1',mode:'informal',payload:{model:'kimi-k3'}}; nextResponse=ok; syncDagRebuild(); await rebuildDag(); out.modelOutline=snap();
  dagMode='formal'; nextResponse=limited; syncDagRebuild(); await rebuildDag(); out.formalLimited=snap();
  lastSandboxRun={run_id:'run-1',auto_pipeline:{status:'running'}};
  syncDagRebuild(); out.formalDuringPipeline=snap();
  dagMode='engine'; syncDagRebuild(); out.engineDuringPipeline=snap();
  console.log(JSON.stringify(out));
})();
"""
        )
        output = _run_node(program)
        engine = output["engine"]
        self.assertEqual(engine["posts"], [])
        self.assertEqual(engine["refreshes"], 1)
        self.assertEqual(engine["text"], "↻ Refresh")
        self.assertIn("no Formal/DAG action used", engine["title"])
        cgm_outline = output["cgmOutline"]
        self.assertEqual(cgm_outline["posts"], [])
        self.assertEqual(cgm_outline["refreshes"], 2)
        model_outline = output["modelOutline"]
        self.assertEqual(
            model_outline["posts"],
            [{"url": "/api/run/run-1/proof_graph", "method": "POST", "body": {"kind": "informal"}}],
        )
        self.assertEqual(model_outline["refreshes"], 3)
        self.assertEqual(model_outline["text"], "↻ Rebuild")
        self.assertIn("uses one guest Formal/DAG action", model_outline["title"])
        limited = output["formalLimited"]
        self.assertEqual(limited["posts"][-1]["body"], {"kind": "formal"})
        self.assertEqual(limited["refreshes"], 3)
        self.assertEqual(
            limited["status"],
            "⏸ This guest session has reached its hourly Formal and DAG limit. Try again in about 10 min.",
        )
        self.assertTrue(output["formalDuringPipeline"]["disabled"])
        self.assertFalse(output["engineDuringPipeline"]["disabled"])

    def test_limited_verify_keeps_the_lean_badge(self) -> None:
        program = _program(
            _js_const("LEAN_STATUS_LABEL")
            + "\n"
            + _js_function("setLeanBadge")
            + "\n"
            + _js_function("limitMessage")
            + "\n"
            + _js_function("leanResponseJson")
            + "\n"
            + _js_function("leanAction")
            + r"""
let leanActionSeq=0, statuses=[], diagnostics=[], onFetch=null;
const leanRunningRuns=new Map();
function setLeanBusy(){}
function prepareLeanHarnessStart(){}
function leanEditorText(){ return ''; }
function leanStatus(text){ statuses.push(text); }
function renderLean(){ throw new Error('a limit must not render a Lean result'); }
function renderLeanDiagnostic(message){ diagnostics.push(message); }
function refreshLeanView(){}
elements['lean-model']=new El('select','lean-model'); elements['lean-model'].value='kimi-k3';
elements['lean-badge']=new El('span','lean-badge');
const limited=()=>({ok:false,status:429,text:async()=>JSON.stringify({error:'This guest session has reached its hourly Formal and DAG limit'}),headers:{get:name=>name==='Retry-After'?'120':null}});
async function fetch(){ if(onFetch)onFetch(); return limited(); }
const badge=()=>({cls:elements['lean-badge'].className,text:elements['lean-badge'].textContent});
(async()=>{
  CURRENT_USER={guest:true};
  const out={};
  setLeanBadge('verified');
  await leanAction('verify'); out.kept=badge();
  setLeanBadge('verified');
  onFetch=()=>setLeanBadge('incomplete');
  await leanAction('verify'); out.newer=badge();
  out.statuses=statuses; out.diagnostics=diagnostics;
  console.log(JSON.stringify(out));
})();
"""
        )
        output = _run_node(program)
        self.assertEqual(output["kept"], {"cls": "lean-badge verified", "text": "Verified"})
        # A refresh that landed during the request wins over the saved badge.
        self.assertEqual(output["newer"], {"cls": "lean-badge incomplete", "text": "Incomplete (sorry)"})
        self.assertEqual(output["diagnostics"], [])
        self.assertTrue(all(text.startswith("⏸ ") for text in output["statuses"]), output["statuses"])


class GuestLeanStatusTests(unittest.TestCase):
    """Guests read the (host-neutral) Lean toolchain status like members."""

    def test_guest_formal_view_reports_the_toolchain_status(self) -> None:
        program = (
            _js_function("refreshLeanView")
            + r"""
let runId='r1', leanRefreshSeq=0, leanEngines=[], leanRunKey='', leanDirty=false, CURRENT_USER={guest:true};
let lastSandboxRun={auto_pipeline:null}, statuses=[], urls=[], onStatus=null, engineFills=[];
const leanRunningRuns=new Map();
const el={style:{}};
function $(){ return el; }
async function leanResponseJson(response){ return response; }
function fillLeanEngines(list){ engineFills.push(list); leanEngines=CURRENT_USER?.guest?[]:list; }
function fillLeanModels(){}
function leanPayloadHasDisplay(){ return false; }
function renderLean(){ throw new Error('nothing to render'); }
function renderLeanDiagnostic(message){ throw new Error(message); }
function syncLeanControls(){}
function setLeanBadge(){}
function leanStatus(text){ statuses.push(text); }
function renderLeanProgress(){}
function syncLeanPipelineEmptyState(){ return false; }
async function fetch(url){
  urls.push(url);
  if(url==='/api/lean/status'){
    if(onStatus)onStatus();
    return {available:false,mathlib_available:false,version:null,engines:[]};
  }
  return {status:'none'};
}
(async()=>{
  const out={};
  await refreshLeanView();
  out.selected={urls:[...urls], last:statuses[statuses.length-1]};
  urls.length=0; statuses.length=0; runId='';
  await refreshLeanView();
  out.none={urls:[...urls], last:statuses[statuses.length-1]};
  // A newer refresh started while the status was loading wins.
  urls.length=0; statuses.length=0; runId='r1';
  onStatus=()=>{ runId='r2'; };
  await refreshLeanView();
  out.stale={urls:[...urls], statuses:[...statuses]};
  out.engineFills=engineFills;
  console.log(JSON.stringify(out));
})();
"""
        )
        output = _run_node(program)
        self.assertEqual(output["selected"]["urls"], ["/api/run/r1/lean_verify", "/api/lean/status"])
        self.assertEqual(output["selected"]["last"], "Lean toolchain not installed on server")
        self.assertEqual(output["none"]["urls"], ["/api/lean/status"])
        self.assertEqual(output["none"]["last"], "select a run")
        self.assertIn("/api/lean/status", output["stale"]["urls"])
        self.assertNotIn("Lean toolchain not installed on server", output["stale"]["statuses"])
        self.assertTrue(all(fill == [] for fill in output["engineFills"]))


class WrittenProofMarkupTests(unittest.TestCase):
    """The written-proof switch that the console and the shared page script against.

    Its behaviour (defaults, banner, sanitizing, polls) is pinned in
    tests/test_cgm_writeup_ui.mjs.
    """

    def _switch(self, markup: str, switch_id: str) -> list[tuple[str, str, str]]:
        match = re.search(rf'<div ([^>]*\bid="{switch_id}"[^>]*)>(.*?)</div>', markup, re.S)
        self.assertIsNotNone(match, switch_id)
        opening, body = match.groups()
        self.assertIn('role="radiogroup"', opening)
        self.assertIn('aria-label="Proof document"', opening)
        # Hidden until the run has a write-up.
        self.assertRegex(opening, r"\bhidden\b")
        return re.findall(
            r'<button type="button" role="radio" data-proof-doc="(\w+)" aria-checked="(\w+)"[^>]*>(.*?)</button>', body
        )

    def test_the_console_proof_toolbar_offers_both_documents(self) -> None:
        toolbar = HTML.split('<div class="proof-toolbar">', 1)[1].split('<div class="prompt-strip', 1)[0]
        self.assertEqual(
            self._switch(toolbar, "proof-doc-switch"),
            [
                ("writeup", "false", 'Written proof<span class="writeup-chip" id="writeup-chip"></span>'),
                ("facts", "true", "Verified facts"),
            ],
        )
        self.assertLess(toolbar.index('id="proof-doc-switch"'), toolbar.index('id="proof-edit-btn"'))

    def test_the_shared_page_offers_both_documents_without_inline_script(self) -> None:
        viewer = (Path(__file__).parents[1] / "agent_monitor" / "web" / "run-viewer.html").read_text(encoding="utf-8")
        self.assertEqual(
            self._switch(viewer, "rv-proof-doc-switch"),
            [
                ("writeup", "false", 'Written proof<span id="rv-writeup-chip" class="rv-writeup-chip"></span>'),
                ("facts", "true", "Verified facts"),
            ],
        )
        self.assertIn('<div id="rv-writeup-banner" class="rv-writeup-banner" role="note" hidden></div>', viewer)
        # The shared page's CSP runs only same-origin script files.
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", viewer))
        self.assertIsNone(re.search(r"\son[a-z]+=", viewer))


if __name__ == "__main__":
    unittest.main()
