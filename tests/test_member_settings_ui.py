"""Settings and composer behavior that self-registered members rely on.

Members may not sign in with a Codex or Claude subscription: their Settings
cards explain why, with no Connect button, and point at the API-provider key
that works instead (the server also answers a login start with 403).  Ansatz
CGM runs Kimi K3 on the sponsored ``kimi`` engine and any model from the
member's own keys on the ``math_harness`` engine.  The UI must report all of
this plainly: a refused login is not a missing CLI, and own-key models are
offered where they can run.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from agent_monitor import account_policy

HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"

# What the server answers a member's login start (and a stale page shows).
REFUSAL = account_policy.CODEX_SUBSCRIPTION_NOTE


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


def _run(program: str) -> list:
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


# A minimal DOM: the card element plus whichever of its buttons the last
# render produced, so a test can click them like a user.
_CARD_DOM = """
const LIB_ESC=value=>String(value);
let SETTINGS=null;
const selectedAccountRuntime=()=>'api';
const document={activeElement:null};
const confirm=()=>true;
const els={};
const $=id=>{
  if(id.endsWith('-card'))return els[id]||(els[id]={innerHTML:''});
  const card=Object.values(els).find(el=>typeof el.innerHTML==='string'&&el.innerHTML.includes(`id="${id}"`));
  return card?(els[id]||(els[id]={id,value:''})):null;
};
let responses=[];
const fetch=async()=>{ const next=responses.shift(); if(next instanceof Error)throw next; return next; };
const reply=(status,body)=>({ok:status>=200&&status<300,status,json:async()=>body});
"""

# The composer's model picker, with menu rendering and run-mode side effects
# stubbed; re-rendering the launcher re-renders the models, as in the page.
_COMPOSER_DOM = """
const LIB_ESC=value=>String(value);
const els={};
const $=id=>els[id]||(els[id]={id,value:'',innerHTML:'',textContent:'',disabled:false,style:{setProperty(){}},querySelectorAll:()=>[]});
const modelVisual=()=>({glyph:'',tone:'',mark:'',provider:'OpenAI'});
let footer=''; const renderChoiceMenu=(id,title,options,custom='')=>{footer=custom;};
const renderRunTuning=()=>{}, renderRunRuntime=()=>{}, renderEngines=()=>renderModels();
const CURRENT_USER=null, GUEST_POLICY=null;
"""


def _composer_source() -> str:
    max_model = SCRIPT[SCRIPT.index("const CGM_MAX_MODEL=") :]
    return max_model[: max_model.index(";") + 1] + "\n" + "\n".join(
        _js_function(name)
        for name in (
            "selectedAccountRuntime", "runtimeDisplayName", "engineRuntimeIssue",
            "engineModelMode", "modelsForEngine", "currentModel", "dagIsCgmEngine",
            "cgmEngineUsable", "cgmModels", "cgmEngineFor", "manuscriptSponsoredOnly", "renderModels",
            "cgmMaxAvailable", "isCgmMax", "cgmMaxSummary", "runAuthRoute",
        )
    )


@unittest.skipUnless(NODE, "node is unavailable")
class MemberSettingsUiTests(unittest.TestCase):
    def test_refused_codex_login_shows_the_reason_not_a_missing_cli(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in ("safeExternalAuthUrl", "failedLoginStatus", "codexCardBody", "renderCodexCard", "applyCodexStatus")
        )
        program = f"""
{_CARD_DOM}
let LAST_CODEX_STATUS={{status:'loading',running:false,connected:false,codex_installed:true}};
let polling=false; const startCodexPoll=()=>{{polling=true;}};
{source}
(async()=>{{
  const out=[];
  renderCodexCard({{status:'idle',running:false,connected:false,codex_installed:true}});
  responses.push(reply(403,{{ok:false,error:{json.dumps(REFUSAL)}}}));
  await $('codex-connect-btn').onclick();
  const refused=els['codex-card'].innerHTML;
  out.push(refused.includes({json.dumps(REFUSAL)}),refused.includes('not installed'),refused.includes('id="codex-connect-btn"'),LAST_CODEX_STATUS.codex_installed,polling);
  responses.push(new Error('offline'));
  await $('codex-connect-btn').onclick();
  const offline=els['codex-card'].innerHTML;
  out.push(offline.includes('Could not reach the Codex login service'),offline.includes('not installed'));
  renderCodexCard({{status:'idle',running:false,connected:false,codex_installed:false}});
  out.push(els['codex-card'].innerHTML.includes('Codex CLI not installed on the server'));
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        self.assertEqual(_run(program), [True, False, True, True, False, True, False, True])

    def test_refused_claude_requests_keep_the_install_and_connection_state(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in ("safeExternalAuthUrl", "failedLoginStatus", "claudeCardBody", "renderClaudeCard", "applyClaudeStatus")
        )
        refusal = account_policy.CLAUDE_SUBSCRIPTION_NOTE
        program = f"""
{_CARD_DOM}
let LAST_CLAUDE_STATUS={{status:'loading',running:false,connected:false,reauth_required:false,claude_installed:true,models:[]}};
let polling=false; const startClaudePoll=()=>{{polling=true;}};
{source}
(async()=>{{
  const out=[];
  await applyClaudeStatus({{status:'idle',running:false,connected:false,claude_installed:true,models:[]}});
  responses.push(reply(403,{{ok:false,error:{json.dumps(refusal)}}}));
  await $('claude-connect-btn').onclick();
  const refused=els['claude-card'].innerHTML;
  out.push(refused.includes({json.dumps(refusal)}),refused.includes('not installed'),LAST_CLAUDE_STATUS.claude_installed,polling);
  await applyClaudeStatus({{status:'idle',running:false,connected:true,claude_installed:true,models:['sonnet']}});
  responses.push(reply(401,{{error:'Sign in again'}}));
  await $('claude-logout-btn').onclick();
  const kept=els['claude-card'].innerHTML;
  out.push(kept.includes('not installed'),LAST_CLAUDE_STATUS.connected,LAST_CLAUDE_STATUS.claude_installed);
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        self.assertEqual(_run(program), [True, False, True, False, False, True, True])

    def test_kimi_harness_points_to_another_harness_for_own_key_models(self) -> None:
        program = f"""
{_COMPOSER_DOM}
// A member on a site that narrowed members to kimi,plain: the own-route CGM
// harness is administrator-only there.
const ENGINES=[{{id:'kimi',label:'Ansatz CGM · Kimi K3',available:true,auth_modes:['api_key'],supported_models:['kimi-k3']}},{{id:'math_harness',label:'Ansatz CGM harness',available:false,requires_admin:true,auth_modes:['codex_subscription','claude_subscription','api_key']}},{{id:'plain',label:'Plain (no harness)',available:true,auth_modes:['codex_subscription','claude_subscription','api_key']}}];
let engine='kimi';
let SETTINGS={{account_runtime:'api',api_models:['kimi-k3','gpt-6-astra','chat-latest'],configured_providers:['openai'],default_model:'kimi-k3',sponsored_kimi_available:true}};
{_composer_source()}
const out=[];
renderModels();
out.push($('model-select').innerHTML.includes('gpt-6-astra'),footer.includes('runs on Kimi K3 only'),footer.includes('gpt-6-astra'),footer.includes('Plain'));
engine='plain';
renderModels();
out.push($('model-select').innerHTML.includes('gpt-6-astra'),footer.includes('runs on Kimi K3 only'));
engine='kimi';
SETTINGS={{...SETTINGS,api_models:['kimi-k3'],configured_providers:[]}};
renderModels();
out.push(footer.includes('Your selection applies to new runs.'));
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), [False, True, True, True, True, False, True])

    def test_choosing_a_model_under_ansatz_cgm_picks_the_engine_that_runs_it(self) -> None:
        program = f"""
{_COMPOSER_DOM}
// An administrator: Ansatz CGM runs Kimi K3 on `kimi` and other models on `math_harness`.
const ENGINES=[{{id:'kimi',label:'Ansatz CGM · Kimi K3',available:true,auth_modes:['api_key'],supported_models:['kimi-k3']}},{{id:'math_harness',label:'Ansatz CGM harness',available:true,auth_modes:['codex_subscription','claude_subscription','api_key']}},{{id:'plain',label:'Plain (no harness)',available:true,auth_modes:['codex_subscription','claude_subscription','api_key']}}];
let engine='kimi';
let SETTINGS={{account_runtime:'api',api_models:['kimi-k3','gpt-6-astra'],configured_providers:['openai'],default_model:'kimi-k3',sponsored_kimi_available:true,sponsored_kimi_engines:['kimi','plain','math_harness']}};
{_composer_source()}
const out=[];
renderModels();
out.push(engine,$('model-select').innerHTML.includes('gpt-6-astra'),footer.includes('runs on Kimi K3 only'));
$('model-select').value='gpt-6-astra'; renderModels(); out.push(engine);
$('model-select').value='kimi-k3'; renderModels(); out.push(engine);
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), ["kimi", True, False, "math_harness", "kimi"])

    def test_member_subscription_cards_explain_the_policy_without_a_connect_button(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "safeExternalAuthUrl", "failedLoginStatus", "subscriptionPolicyBody", "focusApiProvider",
                "codexCardBody", "renderCodexCard", "applyCodexStatus", "refreshCodexCard",
                "claudeCardBody", "renderClaudeCard", "applyClaudeStatus", "refreshClaudeCard",
            )
        )
        notes = account_policy.subscription_notes()
        program = f"""
const LIB_ESC=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let SETTINGS=null;
const selectedAccountRuntime=()=>'api';
const document={{activeElement:null}};
const confirm=()=>true;
const els={{}};
const group={{open:false,focused:'',scrolled:'',querySelector(selector){{
  const key=(/data-key="([^"]+)"/.exec(selector)||[])[1]||'first key';
  return {{closest:()=>({{scrollIntoView(){{group.scrolled=key;}}}}),focus(){{group.focused=key;}}}};
}}}};
const $=id=>{{
  if(id==='api-provider-group')return group;
  if(id.endsWith('-card'))return els[id]||(els[id]={{innerHTML:''}});
  const card=Object.values(els).find(el=>typeof el.innerHTML==='string'&&el.innerHTML.includes(`id="${{id}}"`));
  if(!card)return null;
  const provider=(new RegExp(`id="${{id}}"[^>]*data-api-provider="([^"]+)"`).exec(card.innerHTML)||[])[1];
  return els[id]||(els[id]={{id,value:'',dataset:{{apiProvider:provider}}}});
}};
const fetches=[]; const responses=[];
const fetch=async url=>{{ fetches.push(url); return responses.shift(); }};
const reply=(status,body)=>({{ok:status>=200&&status<300,status,json:async()=>body}});
const renderRuntimeSelector=()=>{{}}, syncRuntimeModelControls=()=>{{}}, renderEngines=()=>{{}}, loadSettings=async()=>{{}};
const startCodexPoll=()=>{{}}, startClaudePoll=()=>{{}};
let LAST_CODEX_STATUS={{status:'loading',running:false,connected:false,codex_installed:true}};
let LAST_CLAUDE_STATUS={{status:'loading',running:false,connected:false,reauth_required:false,claude_installed:true,models:[]}};
{source}
(async()=>{{
  const out={{}};
  // A member: each card is the policy note, and no login is looked up.
  SETTINGS={{subscription_policy:{{claude:'admin_only',codex:'admin_only'}},subscription_notes:{json.dumps(notes)},
    providers:[{{id:'openai',key_var:'OPENAI_API_KEY'}},{{id:'anthropic',key_var:'ANTHROPIC_API_KEY'}}]}};
  await refreshCodexCard(); await refreshClaudeCard();
  const codex=els['codex-card'].innerHTML, claude=els['claude-card'].innerHTML;
  out.member={{fetches:[...fetches],codexNote:codex.includes({json.dumps(notes["codex"])}),claudeNote:claude.includes({json.dumps(notes["claude"])}),
    connect:/connect-btn|Connect via/.test(codex+claude),badges:codex.includes('administrator only')&&claude.includes('administrator only')}};
  $('claude-api-key-btn').onclick();
  out.claudeKey={{open:group.open,focused:group.focused,scrolled:group.scrolled}};
  group.open=false;
  $('codex-api-key-btn').onclick();
  out.codexKey={{open:group.open,focused:group.focused}};
  // The administrator's cards are unchanged, with a working Connect button.
  SETTINGS={{subscription_policy:{{claude:'available',codex:'available'}}}};
  responses.push(reply(200,{{status:'idle',running:false,connected:false,codex_installed:true}}));
  responses.push(reply(200,{{status:'idle',running:false,connected:false,claude_installed:true,models:[]}}));
  await refreshCodexCard(); await refreshClaudeCard();
  const adminCodex=els['codex-card'].innerHTML, adminClaude=els['claude-card'].innerHTML;
  out.admin={{fetches:fetches.slice(out.member.fetches.length),codexConnect:adminCodex.includes('id="codex-connect-btn"'),
    claudeConnect:adminClaude.includes('id="claude-connect-btn"'),note:/site administrator/.test(adminCodex+adminClaude)}};
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        out = _run(program)
        self.assertEqual(
            out["member"],
            {"fetches": [], "codexNote": True, "claudeNote": True, "connect": False, "badges": True},
        )
        self.assertEqual(
            out["claudeKey"],
            {"open": True, "focused": "ANTHROPIC_API_KEY", "scrolled": "ANTHROPIC_API_KEY"},
        )
        self.assertEqual(out["codexKey"], {"open": True, "focused": "OPENAI_API_KEY"})
        self.assertEqual(
            out["admin"],
            {
                "fetches": ["/api/settings/codex/status", "/api/settings/claude/status"],
                "codexConnect": True,
                "claudeConnect": True,
                "note": False,
            },
        )

    def test_member_can_remove_a_sign_in_stored_before_the_policy(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "safeExternalAuthUrl", "failedLoginStatus", "subscriptionPolicyBody", "removeStoredSignIn",
                "focusApiProvider", "codexCardBody", "renderCodexCard", "claudeCardBody", "renderClaudeCard",
            )
        )
        notes = account_policy.subscription_notes()
        program = f"""
{_CARD_DOM}
const alert=message=>{{throw new Error(message);}};
SETTINGS={{subscription_policy:{{claude:'admin_only',codex:'admin_only'}},subscription_notes:{json.dumps(notes)},
  subscription_stored:{{claude:false,codex:true}}}};
const loadSettings=async()=>{{ SETTINGS={{...SETTINGS,subscription_stored:{{claude:false,codex:false}}}}; }};
let LAST_CODEX_STATUS={{status:'loading',running:false,connected:false,codex_installed:true}};
let LAST_CLAUDE_STATUS={{status:'loading',running:false,connected:false,reauth_required:false,claude_installed:true,models:[]}};
{source}
(async()=>{{
  const out=[];
  renderCodexCard(); renderClaudeCard();
  out.push(els['codex-card'].innerHTML.includes('id="codex-remove-stored-btn"'),els['claude-card'].innerHTML.includes('remove-stored-btn'));
  responses.push(reply(200,{{ok:true}}));
  await $('codex-remove-stored-btn').onclick();
  out.push(els['codex-card'].innerHTML.includes('remove-stored-btn'),els['codex-card'].innerHTML.includes('id="codex-connect-btn"'));
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        self.assertEqual(_run(program), [True, False, False, False])
        # The control reuses the existing logout endpoint.
        self.assertIn("'/api/settings/claude/logout':'/api/settings/codex/logout'", SCRIPT)

    def test_member_cannot_select_a_subscription_runtime(self) -> None:
        source = "\n".join(
            _js_function(name)
            for name in (
                "selectedAccountRuntime", "runtimeUiSpec", "runtimeCardState",
                "renderRuntimeSelector", "chooseAccountRuntime",
            )
        )
        program = f"""
const LIB_ESC=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let SETTINGS={{account_runtime:'api',subscription_policy:{{claude:'admin_only',codex:'admin_only'}},
  configured_providers:['anthropic'],sponsored_kimi_available:true,claude_connected:false,codex_connected:false}};
const CURRENT_USER={{guest:false}};
let RUNTIME_SWITCH_PENDING='', RUNTIME_SWITCH_STATUS=null, SETTINGS_SAVE_PENDING=false;
const host={{innerHTML:'',querySelectorAll:()=>[]}};
const $=id=>id==='runtime-selector'?host:null;
const requests=[]; const fetch=async(...args)=>{{requests.push(args);return {{ok:true,json:async()=>({{}})}};}};
{source}
const cards=()=>Object.fromEntries(host.innerHTML.split('<button').slice(1).map(button=>{{
  const tag=button.slice(0,button.indexOf('>'));
  return [/data-account-runtime="(\\w+)"/.exec(tag)[1],{{disabled:/ disabled\\b/.test(tag),admin:button.includes('Site administrator only')}}];
}}));
(async()=>{{
  const out={{}};
  renderRuntimeSelector(); out.member=cards();
  await chooseAccountRuntime('claude'); await chooseAccountRuntime('codex');
  out.after={{requests:requests.length,runtime:SETTINGS.account_runtime}};
  SETTINGS={{...SETTINGS,subscription_policy:{{claude:'available',codex:'available'}},codex_connected:true}};
  renderRuntimeSelector(); out.admin=cards();
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        out = _run(program)
        self.assertEqual(
            out["member"],
            {
                "claude": {"disabled": True, "admin": True},
                "codex": {"disabled": True, "admin": True},
                "api": {"disabled": False, "admin": False},
            },
        )
        self.assertEqual(out["after"], {"requests": 0, "runtime": "api"})
        self.assertEqual(
            out["admin"],
            {
                "claude": {"disabled": False, "admin": False},
                "codex": {"disabled": False, "admin": False},
                "api": {"disabled": False, "admin": False},
            },
        )

    def test_member_ansatz_cgm_runs_own_key_models_on_the_harness(self) -> None:
        program = f"""
{_COMPOSER_DOM}
// A member: /api/engines advertises the harness on the API route, own keys only.
const ENGINES=[{{id:'kimi',label:'Ansatz CGM · Kimi K3',available:true,auth_modes:['api_key'],supported_models:['kimi-k3']}},{{id:'math_harness',label:'Ansatz CGM harness',available:true,auth_modes:['api_key'],own_key_only:true}},{{id:'plain',label:'Plain (no harness)',available:true,auth_modes:['api_key']}}];
let engine='kimi';
let SETTINGS={{account_runtime:'api',api_models:['kimi-k3','claude-opus-4-8','claude-sonnet-5'],configured_providers:['anthropic'],default_model:'kimi-k3',sponsored_kimi_available:true,sponsored_kimi_engines:['kimi','plain','math_harness'],kimi_user_key_set:false}};
{_composer_source()}
const out=[];
renderModels();
out.push(engine,$('model-select').innerHTML.includes('claude-opus-4-8'),footer.includes('runs on Kimi K3 only'),modelsForEngine(ENGINES[1]));
$('model-select').value='claude-opus-4-8'; renderModels(); out.push(engine);
$('model-select').value='kimi-k3'; renderModels(); out.push(engine);
// With the member's own Kimi key too, Kimi K3 stays on the sponsored engine.
SETTINGS={{...SETTINGS,kimi_user_key_set:true,configured_providers:['anthropic','kimi']}};
out.push(modelsForEngine(ENGINES[1]));
console.log(JSON.stringify(out));
"""
        self.assertEqual(
            _run(program),
            [
                "kimi", True, False, ["claude-opus-4-8", "claude-sonnet-5"],
                "math_harness", "kimi", ["claude-opus-4-8", "claude-sonnet-5"],
            ],
        )

    def test_only_the_operator_and_full_access_accounts_are_offered_a_provider_base_url(self) -> None:
        # A member's runs use the provider's standard endpoint whatever it
        # stores (the server refuses a custom base), so it gets no field; an
        # account an administrator gave full access may set one, as the operator may.
        source = "\n".join(_js_function(name) for name in ("viewerIsAdmin", "viewerHasFullAccess", "renderSettingsModal"))
        program = f"""
const LIB_ESC=value=>String(value);
const els={{}};
const $=id=>els[id]||(els[id]={{id,innerHTML:'',querySelectorAll:()=>[]}});
let CURRENT_USER=null, GUEST_POLICY=null;
const runtimeModelOptions=()=>[], providerVisual=()=>({{tone:'',mark:'',description:''}});
const renderRuntimeSelector=()=>{{}}, syncRuntimeModelControls=()=>{{}}, syncApiProviderGroup=()=>{{}};
const refreshCodexCard=()=>{{}}, refreshClaudeCard=()=>{{}};
const provider=(id,base_var,base_url)=>({{id,label:id,key_var:id.toUpperCase()+'_API_KEY',base_var,base_url,default_base:base_url,models:[],settings:[],api_key_set:true}});
const SETTINGS={{providers:[provider('openai','OPENAI_BASE_URL','https://api.openai.com/v1'),provider('kimi','KIMI_API_BASE','https://kimi.example/v1')]}};
{source}
const out=[];
for(const user of [{{is_admin:false}},{{is_admin:true}},{{is_admin:false,guest:true}},{{is_admin:false,full_access:true}},{{is_admin:false,guest:true,full_access:true}}]){{
  CURRENT_USER=user; renderSettingsModal();
  const html=els['settings-body'].innerHTML;
  out.push([html.includes('data-key="OPENAI_BASE_URL"'),html.includes('data-key="KIMI_API_BASE"'),html.includes('data-key="OPENAI_API_KEY"')]);
}}
console.log(JSON.stringify(out));
"""
        self.assertEqual(
            _run(program),
            [[False, False, True], [True, True, True], [False, False, True], [True, True, True], [False, False, True]],
        )

    def test_continue_is_sent_once_and_reports_an_already_running_run(self) -> None:
        pending = SCRIPT[SCRIPT.index("const CONTINUE_PENDING=") :]
        program = f"""
{pending[: pending.index(";") + 1]}
{_js_function("continueRun")}
let runId='run-1';
const els={{}}; const $=id=>els[id]||(els[id]={{id,disabled:false}});
const alerts=[], pills=[]; let polls=0, loads=0;
globalThis.alert=message=>alerts.push(String(message));
const setPill=(_state,text)=>pills.push(text), startPolling=()=>{{polls++;}}, loadRuns=()=>{{loads++;}}, selectRun=()=>{{}};
let resolveFetch=null, fetches=0;
globalThis.fetch=()=>{{fetches++; return new Promise(resolve=>{{resolveFetch=resolve;}});}};
(async()=>{{
  const out={{}};
  // A double click sends one request; the button waits for the answer.
  const first=continueRun('run-1'), second=continueRun('run-1');
  out.pending={{fetches,disabled:$('sess-cont').disabled}};
  resolveFetch({{ok:true,status:200,json:async()=>({{ok:false,status:'already_running',run_id:'run-1'}})}});
  await Promise.all([first,second]);
  out.running={{alerts:[...alerts],pills:[...pills],polls,loads,disabled:$('sess-cont').disabled,pending:CONTINUE_PENDING.size}};
  const third=continueRun('run-1');
  resolveFetch({{ok:true,status:200,json:async()=>({{ok:true,status:'continuing',run_id:'run-1',job_id:'job-2'}})}});
  await third;
  out.started={{fetches,pills:[...pills],alerts:alerts.length}};
  console.log(JSON.stringify(out));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
        out = _run(program)
        self.assertEqual(out["pending"], {"fetches": 1, "disabled": True})
        self.assertEqual(
            out["running"],
            {
                "alerts": ["This run is already running."],
                "pills": [],
                "polls": 1,
                "loads": 1,
                "disabled": False,
                "pending": 0,
            },
        )
        self.assertEqual(out["started"], {"fetches": 2, "pills": ["continuing…"], "alerts": 1})


if __name__ == "__main__":
    unittest.main()
