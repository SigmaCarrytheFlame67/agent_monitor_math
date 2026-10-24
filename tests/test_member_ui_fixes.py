"""The console for a project member opening another member's run.

Project membership grants read access only: the server refuses every change
to someone else's run with 403 (``_deny_run_change``: the owner, or the
operator, and for a legacy run with no owner the operator alone).  The real
console functions run in node with small DOM stubs.
"""
from __future__ import annotations

import re
import unittest

from test_cgm_ui import HTML, _js_const, _js_function, _program, _run_node

_ACCESS_PAGE = r"""
const classes=new Set();
const document={documentElement:{classList:{toggle(c,on){ if(on)classes.add(c); else classes.delete(c); },contains:c=>classes.has(c)}}};
const els={};
const $=id=>els[id]||(els[id]={id,value:'',readOnly:false,style:{},scrollTop:0});
let runId=null, jobId=null, lastProofMtime=0, lastChatKey='', convoKey='', dagRunKey='', leanRunKey='', leanDirty=false;
let autoPipelineKey='', leanRefreshSeq=0, leanPollTimer=null, workspaceView='sandbox', lastSandboxRun=null, CURRENT_USER=null;
const convoOpen=new Set(), RUN_KIND_BY_ID=new Map(), chatAttachments={clear(){}};
function syncLeanControls(){} function resetProofView(){} function loadRuns(){} function syncRunToMonitor(){} function loadChat(){}
function updateChatStatus(){} function applyRunKindView(){} function refreshConversation(){} function syncDagRebuild(){}
function refreshDagView(){} function refreshLeanView(){} function startPolling(){}
const readOnly=()=>classes.has('read-only-run');
"""


class ReadOnlyRunTests(unittest.TestCase):
    """Controls the server refuses are not offered on a run the viewer may only read."""

    def test_a_run_is_read_only_exactly_when_the_server_refuses_its_changes(self) -> None:
        program = (
            _ACCESS_PAGE
            + "\n".join(
                _js_function(name)
                for name in ("viewerIsAdmin", "canChangeRun", "syncRunAccess", "updateProblemStrip", "selectRun")
            )
            + r"""
const out={};
CURRENT_USER={id:4,email:'member@example.com',is_admin:false,guest:false};
// Projects > Open run: unknown, so unchanged, until the run's record loads.
selectRun('r-admin',null); out.loading=readOnly();
updateProblemStrip({run_id:'r-admin',owner_id:1,owner:'admin@example.com',status:'finished'});
out.adminRun=[readOnly(),$('lean-code').readOnly];
// The next run starts unknown, and a poll answered for the previous run never decides it.
selectRun('r-own',null); out.switched=readOnly();
updateProblemStrip({run_id:'r-admin',owner_id:1}); out.stale=readOnly();
updateProblemStrip({run_id:'r-own',owner_id:4}); out.ownRun=[readOnly(),$('lean-code').readOnly];
// A legacy run with no owner belongs to the operator.
selectRun('legacy',null); updateProblemStrip({run_id:'legacy'}); out.memberLegacy=readOnly();
CURRENT_USER={id:1,is_admin:true,guest:false};
updateProblemStrip({run_id:'legacy',owner_id:null}); out.adminLegacy=readOnly();
// The operator may change any member's run (only Share stays the owner's).
selectRun('r-member',null); updateProblemStrip({run_id:'r-member',owner_id:4}); out.adminOnMemberRun=readOnly();
// A stale admin bit never makes a guest the operator; a guest owns its own runs.
CURRENT_USER={id:9,is_admin:true,guest:true};
selectRun('r-guest',null); updateProblemStrip({run_id:'r-guest',owner_id:4}); out.guestAdminBit=readOnly();
updateProblemStrip({run_id:'r-guest',owner_id:9}); out.guestOwn=readOnly();
// Only a change of access moves the Lean source: one locked by another view stays locked.
selectRun('r-locked',null); $('lean-code').readOnly=true; updateProblemStrip({run_id:'r-locked',owner_id:9});
out.lockedElsewhere=$('lean-code').readOnly;
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(program)
        self.assertFalse(out["loading"])
        self.assertEqual(out["adminRun"], [True, True])
        self.assertFalse(out["switched"])
        self.assertFalse(out["stale"])
        self.assertEqual(out["ownRun"], [False, False])
        self.assertTrue(out["memberLegacy"])
        self.assertFalse(out["adminLegacy"])
        self.assertFalse(out["adminOnMemberRun"])
        self.assertTrue(out["guestAdminBit"])
        self.assertFalse(out["guestOwn"])
        self.assertTrue(out["lockedElsewhere"])

    def test_a_read_only_run_hides_every_change_control_and_says_why(self) -> None:
        css = re.sub(r"/\*.*?\*/", "", "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", HTML, re.S)), flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        hidden = {
            selector.strip()
            for selectors, body in rules
            if re.search(r"display:\s*none\s*!important", body)
            for selector in selectors.split(",")
        }
        for control in (
            "#sess-stop", "#sess-cont",
            "#proof-edit-btn", "#proof-save-btn", "#proof-done-btn",
            ".chat-compose", "#sel-pop", ".lean-chat-compose",
            "#lean-model-label", "#lean-model", "#lean-compile", "#lean-check", "#lean-audit",
            "#lean-engine", "#lean-harness", "#lean-gen", ".run-change",
        ):
            self.assertIn(f"html.read-only-run {control}", hidden)
        # What a reader still uses stays: the run list, the download, the views.
        for kept in ("#pdf-dl-btn", "#lean-code", "#dag-rebuild", "#dag-mode", ".seg-control", ".run-item"):
            self.assertNotIn(f"html.read-only-run {kept}", hidden)
        self.assertIn('<span id="lean-model-label"', HTML)
        note = re.search(r'<div class="run-access-note"[^>]*>(.*?)</div>', HTML)
        self.assertIsNotNone(note)
        self.assertIn("only the member who started it can change it", note.group(1))
        self.assertRegex(css, r"\.run-access-note \{ display: none;")
        self.assertRegex(css, r"html\.read-only-run \.run-access-note \{ display: flex; \}")

    def test_dag_refresh_stays_for_readers_and_rebuild_is_a_change(self) -> None:
        program = _program(
            r"""
const btn=elements['dag-rebuild'];
const out={};
for(const [mode,model] of [['engine',null],['analysis',null],['informal','cgm-fact-graph'],['informal','kimi-k3'],['formal',null]]){
  dagMode=mode; dagView=model?{runId:'run-1',mode,payload:{model}}:null;
  syncDagRebuild(); out[mode+(model?':'+model:'')]=[btn.textContent,btn.classList.contains('run-change')];
}
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(program)
        self.assertEqual(out["engine"], ["↻ Refresh", False])
        self.assertEqual(out["analysis"], ["↻ Refresh", False])
        self.assertEqual(out["informal:cgm-fact-graph"], ["↻ Refresh", False])
        self.assertEqual(out["informal:kimi-k3"], ["↻ Rebuild", True])
        self.assertEqual(out["formal"], ["↻ Rebuild", True])

    def test_analysis_actions_mark_re_check_and_formalize_as_changes(self) -> None:
        program = (
            r"""
const made=[];
function analyzeEl(tag,cls,text){ const el={tag,className:cls||'',textContent:text==null?'':String(text),attrs:{},kids:[],
  setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}}; made.push(el); return el; }
function analysisRunState(){ return {run:{run_id:'r1',analyze:{}},running:false,recheck:null}; }
function viewerIsGuest(){ return false; } function analyzePolicy(){ return {}; } function dagDetailText(v){ return String(v); }
function analysisSourceFile(){ return 'paper.tex'; }
function readabilityUiReady(){ return true; } function manuscriptAsks(){ return []; }
function manuscriptAskReason(){ return ''; } function manuscriptAskOff(){ return ''; } function askAboutNode(){}
const ANALYZE_VERDICT_LABEL={};
"""
            + _js_function("analysisDetailActions")
            + r"""
analysisDetailActions({id:'n1',label:'Lemma 1'},{checkable:true},{file:'paper.tex',lines:[3,9]});
console.log(JSON.stringify(made.filter(el=>el.tag==='button').map(el=>[el.textContent,el.className])));
"""
        )
        self.assertEqual(
            _run_node(program),
            [["↻ Re-check", "run-change"], ["∎ Formalize statement", "run-change"],
             ["Ask about this result", "run-change"], ["Open in source", ""]],
        )


_LEAN_ACTION_PAGE = r"""
let leanActionSeq=0, statuses=[], diagnostics=[], refreshes=0, leanRunKey='stored';
const leanRunningRuns=new Map();
function setLeanBusy(){}
function prepareLeanHarnessStart(){ setLeanBadge('running'); }
function leanEditorText(){ return 'theorem t : True := trivial'; }
function leanStatus(text){ statuses.push(text); }
function renderLean(){ throw new Error('a refusal must not render a Lean result'); }
// As the real one does: a made-up failed result over whatever was on screen.
function renderLeanDiagnostic(message){ diagnostics.push(message); setLeanBadge('failed'); }
// The stored (verified) result, redrawn in full once its key was reset.
async function refreshLeanView(){ refreshes++; if(leanRunKey===''){ leanRunKey='stored'; setLeanBadge('verified'); leanStatus('gpt-5 · verify'); } }
for(const [id,tag] of [['lean-model','select'],['lean-engine','select'],['lean-badge','span'],['lean-empty','div'],['lean-progress-title','div'],['lean-progress-detail','div']])elements[id]=new El(tag,id);
elements['lean-model'].value='gpt-5'; elements['lean-engine'].value='codex'; elements['lean-empty'].style.display='none';
const reply=(status,error)=>({ok:false,status,text:async()=>JSON.stringify({error}),headers:{get:()=>null}});
let next=null;
async function fetch(){ return next; }
const badge=()=>elements['lean-badge'].textContent;
"""


class LeanRefusalTests(unittest.TestCase):
    """A refused Lean action says why without replacing the stored result."""

    def _program(self, body: str) -> str:
        return _program(
            "\n".join(
                [_js_const("LEAN_STATUS_LABEL")]
                + [_js_function(name) for name in ("setLeanBadge", "limitMessage", "leanResponseJson", "leanAction")]
            )
            + _LEAN_ACTION_PAGE
            + body
        )

    def test_every_lean_error_carries_its_http_status(self) -> None:
        program = _js_function("limitMessage") + "\n" + _js_function("leanResponseJson") + r"""
(async()=>{
  const out=[];
  for(const [status,body] of [[403,{error:'Only the member who started this run can run Lean actions on it.'}],[401,{error:'not signed in'}],[500,{}]]){
    try{ await leanResponseJson({ok:false,status,text:async()=>JSON.stringify(body),headers:{get:()=>null}},'Compile'); }
    catch(e){ out.push([e.message,e.status,!!e.limit]); }
  }
  console.log(JSON.stringify(out));
})();
"""
        self.assertEqual(
            _run_node(program),
            [
                ["Only the member who started this run can run Lean actions on it.", 403, False],
                ["not signed in", 401, False],
                ["Compile failed with HTTP 500", 500, False],
            ],
        )

    def test_a_refused_lean_action_keeps_the_verified_result(self) -> None:
        out = _run_node(self._program(r"""
(async()=>{
  CURRENT_USER={id:4,is_admin:false,guest:false};
  const out={};
  next=reply(403,'Only the member who started this run can run Lean actions on it.');
  for(const action of ['compile','check','audit','verify']){ setLeanBadge('verified'); await leanAction(action); out[action]=badge(); }
  out.status=statuses.at(-1);
  // A refused harness start redraws the result its start view replaced, and still says why.
  next=reply(403,'The local formal harness is limited to the site administrator');
  setLeanBadge('verified'); await leanAction('harness');
  out.harness=[badge(),refreshes,statuses.at(-1)];
  next=reply(401,'not signed in'); setLeanBadge('verified'); await leanAction('check'); out.signedOut=badge();
  next=reply(404,'run not found'); setLeanBadge('verified'); await leanAction('compile'); out.gone=badge();
  out.refusedDiagnostics=diagnostics.slice();
  // A real failure still explains itself in the Lean view.
  next=reply(500,'lake build crashed'); setLeanBadge('verified'); await leanAction('compile');
  out.failure=[badge(),diagnostics.slice()];
  console.log(JSON.stringify(out));
})();
"""))
        for action in ("compile", "check", "audit", "verify"):
            self.assertEqual(out[action], "Verified", action)
        self.assertEqual(out["status"], "✗ Only the member who started this run can run Lean actions on it.")
        self.assertEqual(
            out["harness"], ["Verified", 1, "✗ The local formal harness is limited to the site administrator"]
        )
        self.assertEqual((out["signedOut"], out["gone"]), ("Verified", "Verified"))
        self.assertEqual(out["refusedDiagnostics"], [])
        self.assertEqual(out["failure"], ["Failed", ["lake build crashed"]])

    def test_an_action_the_server_does_not_start_keeps_the_stored_result(self) -> None:
        out = _run_node(self._program(r"""
(async()=>{
  CURRENT_USER={id:4,is_admin:false,guest:false};
  next=reply(400,'No proof text yet — wait for the run to produce proof.md');
  setLeanBadge('verified'); await leanAction('verify');
  await new Promise(resolve=>setTimeout(resolve,10));
  console.log(JSON.stringify([badge(),diagnostics,refreshes,statuses.at(-1)]));
})();
"""))
        # The stored result is redrawn, and the reason stays on the status line.
        self.assertEqual(out, ["Verified", [], 1, "✗ No proof text yet — wait for the run to produce proof.md"])

    def test_a_refusal_before_any_lean_result_explains_itself_in_the_empty_view(self) -> None:
        out = _run_node(self._program(r"""
(async()=>{
  CURRENT_USER={id:4,is_admin:false,guest:false};
  elements['lean-empty'].style.display='flex'; setLeanBadge('none');
  next=reply(403,'Only the member who started this run can run Lean actions on it.');
  await leanAction('verify');
  console.log(JSON.stringify([badge(),elements['lean-empty'].style.display,elements['lean-progress-title'].textContent,elements['lean-progress-detail'].textContent,diagnostics]));
})();
"""))
        self.assertEqual(
            out,
            ["—", "flex", "Action refused", "Only the member who started this run can run Lean actions on it.", []],
        )


_VIEWERS = (
    "[['admin',{id:1,is_admin:true,guest:false}],['member',{id:4,is_admin:false,guest:false}],['guest',{id:9,is_admin:false,guest:true}],"
    "['full',{id:5,is_admin:false,guest:false,full_access:true}],['guest_full',{id:10,is_admin:false,guest:true,full_access:true}]]"
)


class MemberFormalHarnessTests(unittest.TestCase):
    """The coding-agent Formal harness is the operator's, and an account's an
    administrator gave full access: the server refuses it to other members
    (403) as well as to guests (even one flagged with full access)."""

    def test_only_the_operator_and_full_access_accounts_are_offered_the_formal_harness(self) -> None:
        program = (
            r"""
let CURRENT_USER=null, runId=null, lastSandboxRun=null, leanEngines=[];
const leanPendingRuns=new Map(), leanRunningRuns=new Map(), leanEngineByRun=new Map();
const els={};
function $(id){ return els[id]||(els[id]={id,textContent:'',title:'',disabled:false,style:{},dataset:{},value:'',innerHTML:''}); }
function escapeHtml(v){ return String(v); }
function fillLeanModels(){}
"""
            + "\n".join(_js_function(name) for name in ("viewerIsAdmin", "viewerHasFullAccess", "syncLeanControls", "fillLeanEngines"))
            + r"""
const engines=[{id:'codex',label:'Codex CLI',available:true,auth_detail:'OpenAI API key'}];
const out={};
for(const [label,user] of """ + _VIEWERS + r"""){
  CURRENT_USER=user; runId='run-'+label;
  fillLeanEngines(engines,'codex');
  out[label]={harness:[$('lean-harness').style.display,$('lean-harness').disabled],engine:[$('lean-engine').style.display,$('lean-engine').disabled],
    offered:leanEngines.length,mapped:leanEngineByRun.has(runId),
    others:['lean-gen','lean-compile','lean-check','lean-audit','lean-chat-send'].map(id=>$(id).disabled)};
}
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(program)
        for viewer in ("admin", "full"):
            self.assertEqual(
                out[viewer],
                {"harness": ["", False], "engine": ["", False], "offered": 1, "mapped": True, "others": [False] * 5},
                viewer,
            )
        for viewer in ("member", "guest", "guest_full"):
            self.assertEqual(
                out[viewer],
                {"harness": ["none", True], "engine": ["none", True], "offered": 0, "mapped": False, "others": [False] * 5},
                viewer,
            )

    def test_a_formal_failure_suggests_the_harness_only_to_the_operator_and_full_access_accounts(self) -> None:
        program = (
            r"""
const els={}; function $(id){ return els[id]||(els[id]={textContent:''}); }
let CURRENT_USER=null; function setLeanBadge(){} function leanStatus(){}
"""
            + "\n".join(_js_function(name) for name in ("viewerIsAdmin", "viewerHasFullAccess", "pipelineStageVisualStatus", "syncLeanPipelineEmptyState"))
            + r"""
const out={};
for(const [label,user] of """ + _VIEWERS + r"""){
  CURRENT_USER=user; syncLeanPipelineEmptyState({stages:{lean:{status:'error',detail:'node exited 127'}}});
  out[label]=$('lean-progress-detail').textContent;
}
console.log(JSON.stringify(out));
"""
        )
        out = _run_node(program)
        for viewer in ("admin", "full"):
            self.assertTrue(out[viewer].endswith(" Retry Verify, or choose an available Harness."), out[viewer])
        for viewer in ("member", "guest", "guest_full"):
            self.assertTrue(out[viewer].endswith(" Retry Verify."), out[viewer])
            self.assertNotIn("Harness", out[viewer])

    def test_only_the_operator_and_full_access_accounts_read_openai_models_as_codex_ready(self) -> None:
        program = (
            "let CURRENT_USER=null, CGM_MAX_MODEL='max';\n"
            + _js_function("viewerIsAdmin")
            + "\n"
            + _js_function("viewerHasFullAccess")
            + "\n"
            + _js_function("modelVisual")
            + r"""
const out={};
for(const [label,user] of """ + _VIEWERS + r"""){ CURRENT_USER=user; out[label]=modelVisual('gpt-5').provider; }
console.log(JSON.stringify(out));
"""
        )
        self.assertEqual(
            _run_node(program),
            {"admin": "OpenAI · Codex-ready", "member": "OpenAI", "guest": "OpenAI", "full": "OpenAI · Codex-ready", "guest_full": "OpenAI"},
        )


if __name__ == "__main__":
    unittest.main()
