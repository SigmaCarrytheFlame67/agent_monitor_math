"""The console for a project member opening another member's run.

Project membership grants read access only: the server refuses every change
to someone else's run with 403 (``_deny_run_change``: the owner, or the
operator, and for a legacy run with no owner the operator alone).  The real
console functions run in node with small DOM stubs.
"""
from __future__ import annotations

import re
import unittest

from test_cgm_ui import HTML, _js_function, _program, _run_node

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
            [["↻ Re-check", "run-change"], ["∎ Formalize statement", "run-change"], ["Open in source", ""]],
        )


if __name__ == "__main__":
    unittest.main()
