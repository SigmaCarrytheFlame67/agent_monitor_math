from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_monitor import console_server, lean_verify


HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = SCRIPT.index(f"function {name}(")
    start = (
        function_start - 6
        if SCRIPT[function_start - 6 : function_start] == "async "
        else function_start
    )
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


def _run_node(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run(
        [NODE, "-e", program],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class FormalLeanApiTests(unittest.TestCase):
    def test_preferred_engine_uses_recorded_engine_only_when_available(self) -> None:
        engines = [
            {"id": "hermes", "available": False},
            {"id": "codex", "available": True},
            {"id": "openclaw", "available": True},
        ]
        self.assertEqual(
            console_server._preferred_formal_harness_engine(
                {"engine": "openclaw"}, engines
            ),
            "openclaw",
        )
        self.assertEqual(
            console_server._preferred_formal_harness_engine(
                {"engine": "hermes"}, engines
            ),
            "codex",
        )
        self.assertEqual(
            console_server._preferred_formal_harness_engine(
                {"engine": "stale-engine"}, engines
            ),
            "codex",
        )
        self.assertEqual(
            console_server._preferred_formal_harness_engine(
                {"engine": "codex"}, [{"id": "codex", "available": False}]
            ),
            "",
        )

    def test_get_returns_fail_closed_per_run_engine_preference(self) -> None:
        run_record = {
            "run_id": "formal-ui-run",
            "owner_id": 7,
            "auth_route": "api_key",
            "engine": "unavailable-recorded",
            "model": "model-a",
        }
        engines = [
            {"id": "unavailable-recorded", "available": False},
            {"id": "codex", "available": True},
        ]
        handler = object.__new__(console_server.Handler)
        handler.path = "/api/run/formal-ui-run/lean_verify"
        handler._resolve_path = lambda: "/api/run/formal-ui-run/lean_verify"
        # The operator: members are offered no harness engine at all.
        handler._require_user = lambda _path: {"id": 7, "is_admin": True}
        handler._owns_run = lambda _rid, _user: True
        sent: list[tuple[int, dict]] = []
        handler._send = lambda code, payload, *args, **kwargs: sent.append(
            (code, json.loads(payload))
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(console_server, "_workspace_for_run", return_value=Path(td)),
            patch.object(console_server, "_run_record_for", return_value=run_record),
            patch.object(lean_verify, "load_cached", return_value=None),
            patch.object(
                lean_verify,
                "harness_model_options",
                return_value={
                    "auth_route": "api_key",
                    "models": ["model-a"],
                    "selected": "model-a",
                },
            ),
            patch.object(lean_verify, "harness_engines", return_value=engines),
            patch.object(lean_verify, "harness_job", return_value=None),
        ):
            handler.do_GET()

        self.assertEqual(sent[0][0], 200)
        self.assertEqual(sent[0][1]["status"], "none")
        self.assertEqual(sent[0][1]["harness_engine"], "codex")
        self.assertEqual(sent[0][1]["harness_model"], "model-a")

    def test_live_harness_job_does_not_expose_internal_control_objects(self) -> None:
        class LiveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            with lean_verify._HARNESS_LOCK:
                key = lean_verify._harness_key(workspace)
                lean_verify._HARNESS_JOBS[key] = {
                    "engine": "openclaude",
                    "label": "OpenClaude",
                    "started_at": 123.5,
                    "state": "running",
                    "stopping": False,
                    "thread": LiveThread(),
                    "proc": object(),
                    "operation_token": object(),
                }
            try:
                public = lean_verify.harness_job(workspace)
            finally:
                with lean_verify._HARNESS_LOCK:
                    lean_verify._HARNESS_JOBS.pop(key, None)

        self.assertNotIn("operation_token", public)
        json.dumps(public)


class FormalLeanUiTests(unittest.TestCase):
    def test_terminal_payloads_are_displayable_without_lean_source(self) -> None:
        source = _js_function("leanPayloadHasDisplay")
        output = _run_node(
            source
            + """
const cases={
  none:leanPayloadHasDisplay({status:'none'}),
  failed:leanPayloadHasDisplay({status:'failed'}),
  missing:leanPayloadHasDisplay({status:'toolchain_missing'}),
  stopped:leanPayloadHasDisplay({status:'stopped'}),
  note:leanPayloadHasDisplay({status:'none',notes:'route unavailable'}),
  source:leanPayloadHasDisplay({status:'none',lean:'theorem main : True := by trivial'})
};
console.log(JSON.stringify(cases));
"""
        )
        self.assertFalse(output["none"])
        for key in ("failed", "missing", "stopped", "note", "source"):
            self.assertTrue(output[key], key)

    def test_response_parser_preserves_json_and_non_json_http_errors(self) -> None:
        source = _js_function("leanResponseJson")
        output = _run_node(
            source
            + """
(async()=>{
  const response=(ok,status,body)=>({ok,status,text:async()=>body});
  const out={};
  try{await leanResponseJson(response(false,400,JSON.stringify({error:'No proof text yet'})),'Verify');}
  catch(e){out.json=e.message;}
  try{await leanResponseJson(response(false,502,'upstream gateway exploded'),'Verify');}
  catch(e){out.text=e.message;}
  out.ok=(await leanResponseJson(response(true,200,JSON.stringify({status:'failed'})),'Verify')).status;
  console.log(JSON.stringify(out));
})();
"""
        )
        self.assertEqual(output["json"], "No proof text yet")
        self.assertIn("HTTP 502", output["text"])
        self.assertIn("upstream gateway exploded", output["text"])
        self.assertEqual(output["ok"], "failed")

    def test_harness_selector_is_isolated_per_run_and_fails_closed(self) -> None:
        source = _js_function("viewerIsAdmin") + "\n" + _js_function("viewerHasFullAccess") + "\n" + _js_function("fillLeanEngines")
        output = _run_node(
            """
let leanEngines=[];
let runId='run-a';
let CURRENT_USER={id:7,guest:false,is_admin:true};
const leanEngineByRun=new Map();
const select={value:'',_html:'',set innerHTML(v){this._html=v;},get innerHTML(){return this._html;}};
function $(id){return id==='lean-engine'?select:null;}
function escapeHtml(value){return String(value);}
function syncLeanControls(){}
"""
            + source
            + """
const engines=[
  {id:'codex',label:'Codex',available:true,auth_detail:'route'},
  {id:'openclaw',label:'OpenClaw',available:true,auth_detail:'route'}
];
fillLeanEngines(engines,'codex');
const firstA=select.value;
leanEngineByRun.set('run-a','openclaw');
runId='run-b'; fillLeanEngines(engines,'codex');
const firstB=select.value;
runId='run-a'; fillLeanEngines(engines,'codex');
const restoredA=select.value;
runId='run-c'; fillLeanEngines([{id:'codex',label:'Codex',available:false}], 'codex');
const closed=select.value, html=select.innerHTML;
CURRENT_USER={id:8,guest:true}; runId='run-g';
fillLeanEngines(engines,'codex');
const guest=select.value, guestEngines=leanEngines.length, guestMapped=leanEngineByRun.has('run-g');
// A guest flagged with full access still gets none; a member does only when given full access.
CURRENT_USER={id:8,guest:true,full_access:true}; runId='run-g2'; fillLeanEngines(engines,'codex');
const guestFull=leanEngines.length;
CURRENT_USER={id:9,guest:false,is_admin:false}; runId='run-m'; fillLeanEngines(engines,'codex');
const member=leanEngines.length;
CURRENT_USER={id:10,guest:false,is_admin:false,full_access:true}; runId='run-f'; fillLeanEngines(engines,'codex');
const full=[select.value,leanEngines.length];
console.log(JSON.stringify({firstA,firstB,restoredA,closed,html,guest,guestEngines,guestMapped,guestFull,member,full}));
"""
        )
        self.assertEqual(output["firstA"], "codex")
        self.assertEqual(output["firstB"], "codex")
        self.assertEqual(output["restoredA"], "openclaw")
        self.assertEqual(output["closed"], "")
        # Guests never get a coding-agent Harness, even when one is offered.
        self.assertEqual(output["guest"], "")
        self.assertEqual(output["guestEngines"], 0)
        self.assertFalse(output["guestMapped"])
        self.assertEqual(output["guestFull"], 0)
        # A member gets none either, unless an administrator gave the account full access.
        self.assertEqual(output["member"], 0)
        self.assertEqual(output["full"], ["codex", 2])

    def test_pipeline_visual_status_uses_proof_outcome_not_lifecycle(self) -> None:
        source = _js_function("pipelineStageVisualStatus")
        output = _run_node(
            source
            + """
const stage=status=>({status:'done',result:{status}});
console.log(JSON.stringify({
  verified:pipelineStageVisualStatus('lean',stage('verified')),
  incomplete:pipelineStageVisualStatus('lean',stage('incomplete')),
  unfaithful:pipelineStageVisualStatus('lean',stage('unfaithful')),
  failed:pipelineStageVisualStatus('lean',stage('failed')),
  dag:pipelineStageVisualStatus('formal_dag',stage('failed')),
  structural:pipelineStageVisualStatus('formal_dag',{status:'done',outcome:'attention',result:{certificate:false}}),
  blocked:pipelineStageVisualStatus('coverage',{status:'skipped',outcome:'blocked'})
}));
"""
        )
        self.assertEqual(output["verified"], "done")
        self.assertEqual(output["incomplete"], "warning")
        self.assertEqual(output["unfaithful"], "warning")
        self.assertEqual(output["failed"], "error")
        self.assertEqual(output["dag"], "done")
        self.assertEqual(output["structural"], "warning")
        self.assertEqual(output["blocked"], "skipped")

    def test_pipeline_error_and_incomplete_copy_never_claim_completion(self) -> None:
        source = "\n".join(
            (
                _js_function("viewerIsAdmin"),
                _js_function("viewerHasFullAccess"),
                _js_function("pipelineStageVisualStatus"),
                _js_function("syncLeanPipelineEmptyState"),
            )
        )
        output = _run_node(
            """
const elements={
  'lean-progress-title':{textContent:''},
  'lean-progress-detail':{textContent:''}
};
let badge='',meta='';
let CURRENT_USER={id:7,guest:false,is_admin:true};
function $(id){return elements[id]||null;}
function setLeanBadge(value){badge=value;}
function leanStatus(value){meta=value;}
"""
            + source
            + """
const run=stage=>syncLeanPipelineEmptyState({stages:{lean:stage}});
run({status:'done',detail:'Lean incomplete',result:{status:'incomplete'}});
const incomplete={badge,meta,title:elements['lean-progress-title'].textContent,detail:elements['lean-progress-detail'].textContent};
run({status:'done',detail:'Lean failed',result:{status:'failed'}});
const failed={badge,meta,title:elements['lean-progress-title'].textContent,detail:elements['lean-progress-detail'].textContent};
run({status:'error',detail:'node exited 127'});
const startup={badge,meta,title:elements['lean-progress-title'].textContent,detail:elements['lean-progress-detail'].textContent};
CURRENT_USER={id:8,guest:true};
run({status:'error',detail:'node exited 127'});
const guestStartup={badge,title:elements['lean-progress-title'].textContent,detail:elements['lean-progress-detail'].textContent};
CURRENT_USER={id:9,guest:false,is_admin:false,full_access:true};
run({status:'error',detail:'node exited 127'});
const fullStartup=elements['lean-progress-detail'].textContent;
console.log(JSON.stringify({incomplete,failed,startup,guestStartup,fullStartup}));
"""
        )
        self.assertEqual(output["incomplete"]["badge"], "incomplete")
        self.assertIn("not a verified theorem", output["incomplete"]["detail"])
        self.assertEqual(output["failed"]["badge"], "failed")
        self.assertIn("did not verify", output["failed"]["detail"])
        self.assertEqual(output["startup"]["title"], "Formal translation failed")
        self.assertIn("No Proof.lean was produced", output["startup"]["detail"])
        self.assertIn("Retry Verify", output["startup"]["detail"])
        self.assertIn("choose an available Harness", output["startup"]["detail"])
        # Guests have no Harness, so their copy must not offer one.
        self.assertEqual(output["guestStartup"]["badge"], "failed")
        self.assertEqual(output["guestStartup"]["title"], "Formal translation failed")
        self.assertIn("Retry Verify", output["guestStartup"]["detail"])
        self.assertNotIn("Harness", output["guestStartup"]["detail"])
        # An account given full access may choose a Harness, as the operator may.
        self.assertIn("choose an available Harness", output["fullStartup"])

    def test_run_switch_resets_old_pipeline_and_formal_dag_is_disclaimed(self) -> None:
        self.assertIn("lastSandboxRun=null;\n  leanRefreshSeq++", HTML)
        self.assertIn("structure only — see Lean status for verification", HTML)
        self.assertIn("if(leanPayloadHasDisplay(d)){ renderLean(d); return; }", HTML)

    def test_settings_refresh_keeps_the_selected_runs_lean_models(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "selectedAccountRuntime",
                "runtimeDisplayName",
                "runtimeModelOptions",
                "fillLeanModels",
                "syncRuntimeModelControls",
            )
        )
        output = _run_node(
            """
let SETTINGS={account_runtime:'codex',codex_connected:true,models_by_runtime:{
  api:['claude-opus-4-8','gpt-6-astra'],codex:['gpt-6-astra','gpt-5.6-sol'],claude:['sonnet','opus']}};
let CURRENT_USER={id:7,guest:false}, runId='plain_api_run';
const LIB_ESC=s=>String(s??'');
const lean={options:[],current:'',disabled:false,
  set innerHTML(html){this.options=[...html.matchAll(/value="([^"]*)"/g)].map(m=>m[1]);this.current=this.options[0]??'';},
  get value(){return this.current;}, set value(v){this.current=this.options.includes(v)?v:'';}};
function $(id){return id==='lean-model'?lean:null;}
"""
            + source
            + """
// The Lean view lists the selected API run's route (refreshLeanView).
fillLeanModels(['claude-opus-4-8','gpt-6-astra'],'claude-opus-4-8');
// Opening, cancelling or saving Settings syncs the launcher's runtime models.
syncRuntimeModelControls(); syncRuntimeModelControls(true);
const run={value:lean.value,options:lean.options};
runId=null; syncRuntimeModelControls();
console.log(JSON.stringify({run,noRun:{value:lean.value,options:lean.options}}));
"""
        )
        # Verify and Revise keep sending the run's own route and model.
        self.assertEqual(output["run"], {"value": "claude-opus-4-8", "options": ["claude-opus-4-8", "gpt-6-astra"]})
        # Without a selected run the list follows the account runtime.
        self.assertEqual(output["noRun"], {"value": "gpt-6-astra", "options": ["gpt-6-astra", "gpt-5.6-sol"]})


if __name__ == "__main__":
    unittest.main()
