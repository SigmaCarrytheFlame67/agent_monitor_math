"""Browser-side regressions for the merged Monitor view and the runtime cards.

* The monitor no longer has a "Final Proof" panel.
* The console's separate Conversation tab is gone: the Monitor view shows the
  dashboard frame (sized to its content) with the conversation directly below.
* Selecting a connected runtime card saves it at once; each card says whether
  it is in use, connected but not in use, or not connected; the launcher says
  which runtime a new run uses.

Real functions are extracted from the served HTML and run under node against
small stubs, so these pin behaviour, not markup.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = (ROOT / "agent_monitor" / "web" / "console.html").read_text(encoding="utf-8")
SCRIPT = CONSOLE.rsplit("<script>", 1)[1].split("</script>", 1)[0]
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


def _functions(*names: str, source: str = SCRIPT) -> str:
    return "\n".join(_function(source, name) for name in names)


def _run(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=False, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class FinalProofPanelRemovedTests(unittest.TestCase):
    def test_monitor_has_no_final_proof_panel_or_its_helpers(self) -> None:
        for gone in (
            "Final Proof",
            "Final compiled output and which agents contributed text",
            "renderFinalProofSection",
            "loadFinalTexSource",
            "ensureProofSourceLoaded",
            "proof-md-render",
            "data-proof-tab",
            "prov-table",
            "finalTexCache",
            "proofTab",
            "/final.tex",
            "/final.pdf",
            "Agent trace'",
        ):
            self.assertNotIn(gone, MONITOR)

    def test_monitor_no_longer_loads_markdown_or_math_renderers(self) -> None:
        # The proof panel was the dashboard's only markdown/KaTeX consumer.
        self.assertNotIn("marked.min.js", MONITOR)
        self.assertNotIn("katex", MONITOR.lower())
        self.assertNotIn("innerHTML = safeMarkdown", MONITOR)


class MergedMonitorViewTests(unittest.TestCase):
    def test_conversation_tab_is_gone_and_the_transcript_sits_under_the_monitor(self) -> None:
        self.assertNotIn('data-view="conversation"', CONSOLE)
        self.assertNotIn('id="view-conversation"', CONSOLE)
        tabs = re.findall(r'<button[^>]*data-view="([a-z]+)"', CONSOLE.split('<div class="seg-control">', 1)[1].split("</div>", 1)[0])
        self.assertEqual(tabs, ["sandbox", "lean", "monitor", "dag"])
        view = CONSOLE.split('<div id="view-monitor">', 1)[1].split('<div id="view-dag">', 1)[0]
        self.assertLess(view.index('id="monitor-frame"'), view.index('id="convo-col"'))
        self.assertIn("f.src=pcUrl('/monitor?layout=flow')", SCRIPT)

    def test_legacy_conversation_view_maps_to_the_monitor_view(self) -> None:
        program = r"""
const panes={};
for(const id of ['view-sandbox','view-monitor','view-dag','view-lean','view-session','view-landing'])panes[id]={id,style:{display:'none'},classList:{add(){},remove(){}},offsetWidth:1};
const buttons=['sandbox','lean','monitor','dag'].map(v=>({dataset:{view:v},classList:{on:false,toggle(_c,on){this.on=on;}}}));
const document={querySelectorAll:()=>buttons,documentElement:{dataset:{theme:'light'}}};
const $=id=>panes[id];
const calls=[];
let screen='landing', workspaceView='sandbox', runId='r1';
function syncThemeToMonitor(){calls.push('theme');} function syncRunToMonitor(){calls.push('run');}
function refreshConversation(force){calls.push('convo:'+force);} function scheduleMonitorFrameSize(){calls.push('size');}
function syncDagRebuild(){} function refreshDagView(){} function refreshLeanView(){}
function swapMain(){}
""" + _functions("goSession", "setWorkspaceView") + r"""
const out={};
setWorkspaceView('conversation');
out.view=workspaceView;
out.display=Object.fromEntries(['view-sandbox','view-monitor','view-dag','view-lean'].map(id=>[id,panes[id].style.display]));
out.active=buttons.filter(b=>b.classList.on).map(b=>b.dataset.view);
out.calls=calls.splice(0);
goSession('sandbox'); goSession('conversation');
out.afterGoSession=workspaceView;
console.log(JSON.stringify(out));
"""
        out = _run(program)
        self.assertEqual(out["view"], "monitor")
        self.assertEqual(
            out["display"],
            {"view-sandbox": "none", "view-monitor": "block", "view-dag": "none", "view-lean": "none"},
        )
        self.assertEqual(out["active"], ["monitor"])
        self.assertEqual(out["calls"], ["theme", "run", "convo:true", "size"])
        self.assertEqual(out["afterGoSession"], "monitor")

    def test_frame_takes_its_content_height_only_while_shown(self) -> None:
        program = r"""
let flow=true, contentHeight=1834.2, display='block', rects=1;
const rootStyle={props:{},getPropertyValue(k){return this.props[k]||'';},setProperty(k,v){this.props[k]=v;}};
const root={classList:{contains:c=>c==='flow-embed'&&flow},style:rootStyle,getBoundingClientRect:()=>({height:contentHeight})};
const frame={style:{height:''},contentDocument:{documentElement:root}};
const view={style:{get display(){return display;}},getClientRects:()=>({length:rects}),clientHeight:812};
const $=id=>id==='monitor-frame'?frame:id==='view-monitor'?view:null;
let rafs=[]; globalThis.requestAnimationFrame=fn=>{rafs.push(fn);return rafs.length;};
""" + "\n".join(
            [
                SCRIPT[SCRIPT.index("const MONITOR_FRAME_MIN=") : SCRIPT.index("function sizeMonitorFrame(")],
                _functions("sizeMonitorFrame", "scheduleMonitorFrameSize", "watchMonitorFrame"),
            ]
        ) + r"""
const out={};
sizeMonitorFrame(); out.tall=[frame.style.height, rootStyle.props['--host-vh']];
contentHeight=90; sizeMonitorFrame(); out.short=frame.style.height;
display='none'; contentHeight=5000; sizeMonitorFrame(); out.hidden=frame.style.height;
display='block'; rects=0; sizeMonitorFrame(); out.noLayout=frame.style.height;
rects=1; flow=false; sizeMonitorFrame(); out.nonFlow=frame.style.height;
flow=true;
// The frame's own ResizeObserver drives re-measurement on live redraws.
let observed=null, callback=null;
frame.contentWindow={ResizeObserver:class{constructor(cb){callback=cb;} observe(el){observed=el;} disconnect(){}}};
rafs=[]; watchMonitorFrame(); out.observedRoot=observed===root; out.scheduled=rafs.length;
rafs.shift()(); out.afterWatch=frame.style.height;
contentHeight=2400; callback(); callback(); out.coalesced=rafs.length; rafs.shift()(); out.afterRedraw=frame.style.height;
console.log(JSON.stringify(out));
"""
        out = _run(program)
        self.assertEqual(out["tall"], ["1835px", "812px"])
        self.assertEqual(out["short"], "240px")
        self.assertEqual(out["hidden"], "240px")
        self.assertEqual(out["noLayout"], "240px")
        self.assertEqual(out["nonFlow"], "240px")
        self.assertTrue(out["observedRoot"])
        self.assertEqual(out["scheduled"], 1)
        self.assertEqual(out["afterWatch"], "5000px")
        self.assertEqual(out["coalesced"], 1)
        self.assertEqual(out["afterRedraw"], "2400px")

    def test_frame_messages_never_size_the_frame(self) -> None:
        handler = SCRIPT[SCRIPT.index("window.addEventListener('message',e=>{") :]
        handler = handler[: _block_end(handler, handler.index("{"))]
        self.assertNotIn("style.height", handler)
        self.assertNotIn("resize", handler.lower())

    def test_live_polling_refreshes_the_conversation_in_the_monitor_view(self) -> None:
        start = SCRIPT.index("const POLL_MIN_MS=")
        tick = SCRIPT.index("async function tick(")
        polling = SCRIPT[start : _block_end(SCRIPT, SCRIPT.index("{", tick))]
        program = r"""
let runId='r1', workspaceView='monitor', pollTimer=null;
const calls=[];
globalThis.setTimeout=()=>0; globalThis.clearTimeout=()=>{};
const document={hidden:false,addEventListener(){}};
globalThis.fetch=async()=>({ok:true,json:async()=>({run_id:'r1',status:'running',agents:[],totals:{}})});
const $=()=>({textContent:''});
function setPill(){} function updateChatStatus(){} function updateProblemStrip(){} function updatePromptStrip(){}
function renderConversation(run){calls.push('convo:'+run.run_id);} function renderAutoPipeline(){} function applySandboxPreview(){}
async function refreshProofPreview(){} function loadChat(){} function loadRuns(){}
""" + polling + r"""
(async()=>{ await tick(); workspaceView='sandbox'; await tick(); console.log(JSON.stringify(calls)); })();
"""
        self.assertEqual(_run(program), ["convo:r1"])

    def test_run_selection_refreshes_the_conversation_in_the_monitor_view(self) -> None:
        body = _function(SCRIPT, "selectRun")
        self.assertIn("if(workspaceView==='monitor')refreshConversation(true);", body)
        self.assertNotIn("'conversation'", body)


class MonitorFlowLayoutTests(unittest.TestCase):
    def test_flow_layout_only_inside_the_console_monitor_frame(self) -> None:
        head = MONITOR.split("<head>", 1)[1].split("</head>", 1)[0]
        script = re.findall(r"<script>(.*?)</script>", head, re.S)[0]
        program = r"""
const cases=[[true,'?layout=flow'],[false,'?layout=flow'],[true,''],[true,'?run_id=x&layout=flow']];
const out=[];
for(const [framed,search] of cases){
  const classes=new Set();
  const window={}; window.parent=framed?{}:window;
  const location={search};
  const document={documentElement:{classList:{add:c=>classes.add(c)}}};
  (function(window,location,document){ """ + script + r""" })(window,location,document);
  out.push(classes.has('flow-embed'));
}
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), [True, False, False, True])

    def test_flow_css_never_sizes_from_the_frame_viewport(self) -> None:
        flow = MONITOR.split('<style id="monitor-flow-style">', 1)[1].split("</style>", 1)[0]
        self.assertNotRegex(flow, r"\d(?:d|s|l)?vh\b")
        self.assertIn("var(--host-vh", flow)
        self.assertIn("html.flow-embed { height: auto; min-height: 0; overflow: hidden; overscroll-behavior: auto; }", flow)
        # The flow block comes last, so it overrides every earlier viewport rule.
        self.assertGreater(MONITOR.index('<style id="monitor-flow-style">'), MONITOR.index('<style id="clear-palette-style">'))


def _settings_harness(settings: dict, *, guest: bool = False, fetch_reply: str = "", get_reply: str = "") -> str:
    """Stub page for the runtime card functions.

    ``fetch_reply`` answers the POST; ``get_reply`` answers the read-back GET
    (by default the unchanged saved settings, i.e. what the server kept).
    """
    return (
        "let SETTINGS=" + json.dumps(settings) + ", CURRENT_USER=" + json.dumps({"guest": guest}) + ";\n"
        "const SERVER=JSON.parse(JSON.stringify(SETTINGS));\n"
        "let SETTINGS_RUNTIME_BASELINE=SETTINGS.account_runtime, GUEST_POLICY=null, ENGINES=[], engine='plain';\n"
        "let LAST_CODEX_STATUS={status:'idle'}, LAST_CLAUDE_STATUS={status:'idle'};\n"
        "const requests=[], rendered={codex:0,claude:0}; let scrolled='', focused='';\n"
        "const host={innerHTML:'',querySelectorAll:()=>[],contains:el=>!!el&&el.inHost,\n"
        "  querySelector:sel=>({focus(){focused=sel;}})};\n"
        "const document={activeElement:{inHost:true}};\n"
        "const nodes={};\n"
        "const $=id=>id==='runtime-selector'?host:(nodes[id]||(nodes[id]={id,scrollIntoView(){scrolled=id;}}));\n"
        "function syncRuntimeModelControls(){} function syncApiProviderGroup(){} function renderEngines(){}\n"
        "function renderCodexCard(){rendered.codex++;} function renderClaudeCard(){rendered.claude++;}\n"
        "globalThis.fetch=async(url,init)=>{ const method=(init&&init.method)||'GET';\n"
        "  if(method==='GET'){ requests.push([url,'GET']); " + (get_reply or "return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(SERVER))};") + " }\n"
        "  requests.push([url,JSON.parse(init.body)]); " + fetch_reply + " };\n"
        "const LIB_ESC=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\"/g,'&quot;');\n"
        + _functions(
            "runtimeDisplayName",
            "runtimeModelOptions",
            "runtimeUiSpec",
            "runtimeCardState",
            "selectedAccountRuntime",
            "applyRuntimeToSettings",
            "setAccountRuntime",
            "syncSettingsFooter",
            "typedApiProviderFields",
            "fetchSavedSettings",
            "chooseAccountRuntime",
        )
        + "\nlet RUNTIME_SWITCH_PENDING='', RUNTIME_SWITCH_STATUS=null, SETTINGS_SAVE_PENDING=false;\n"
        + _function(SCRIPT, "renderRuntimeSelector")
        + "\n"
    )


BASE_SETTINGS = {
    "account_runtime": "api",
    "codex_connected": True,
    "claude_connected": False,
    "claude_reauth_required": False,
    "sponsored_kimi_available": True,
    "configured_providers": [],
    "models_by_runtime": {"api": ["kimi-k3"], "codex": ["gpt-6-astra"], "claude": []},
}


class RuntimeCardTests(unittest.TestCase):
    def test_each_card_states_in_use_connected_or_not_connected(self) -> None:
        program = _settings_harness(BASE_SETTINGS) + r"""
const out={};
for(const r of ['claude','codex','api'])out[r]=runtimeCardState(r).label;
SETTINGS.account_runtime='claude';
out.inUseDisconnected=runtimeCardState('claude').label;
SETTINGS.account_runtime='codex';
out.codexInUse=runtimeCardState('codex').label;
renderRuntimeSelector();
out.html=host.innerHTML;
console.log(JSON.stringify(out));
"""
        out = _run(program)
        self.assertEqual(out["claude"], "Not connected")
        self.assertEqual(out["codex"], "Connected — not in use (select to use)")
        self.assertEqual(out["api"], "In use")
        self.assertEqual(out["inUseDisconnected"], "In use · not connected")
        self.assertEqual(out["codexInUse"], "In use")
        self.assertIn('data-account-runtime="codex" role="radio" aria-checked="true"', out["html"])
        self.assertIn("Connected — not in use (select to use)", out["html"])
        self.assertIn('id="runtime-choice-status" role="status" aria-live="polite"', out["html"])

    def test_selecting_a_connected_card_saves_it_immediately(self) -> None:
        reply = (
            "return {ok:true,status:200,json:async()=>({ok:true,settings:{...SETTINGS,account_runtime:'codex',"
            "codex_enabled:true,saved_default_model:'gpt-6-astra'}})};"
        )
        program = _settings_harness(BASE_SETTINGS, fetch_reply=reply) + r"""
(async()=>{
  const pending=chooseAccountRuntime('codex');
  const during={pending:RUNTIME_SWITCH_PENDING,busy:host.innerHTML.includes('aria-busy="true"'),label:host.innerHTML.includes('Switching…'),
    footer:[nodes['settings-save'].disabled,nodes['settings-cancel'].disabled]};
  await pending;
  console.log(JSON.stringify({requests,during,footer:[nodes['settings-save'].disabled,nodes['settings-cancel'].disabled],runtime:SETTINGS.account_runtime,baseline:SETTINGS_RUNTIME_BASELINE,
    savedModel:SETTINGS.saved_default_model,status:RUNTIME_SWITCH_STATUS,pending:RUNTIME_SWITCH_PENDING,rendered,
    card:runtimeCardState('codex').label,focused}));
})();
"""
        out = _run(program)
        self.assertEqual(out["requests"], [["/api/settings", {"updates": {}, "account_runtime": "codex"}]])
        self.assertEqual(out["during"], {"pending": "codex", "busy": True, "label": True, "footer": [True, True]})
        self.assertEqual(out["footer"], [False, False])
        self.assertEqual(out["runtime"], "codex")
        self.assertEqual(out["baseline"], "codex")  # closing Settings keeps it
        self.assertEqual(out["savedModel"], "gpt-6-astra")
        self.assertEqual(out["status"]["tone"], "ok")
        self.assertIn("New runs now use Codex subscription", out["status"]["text"])
        self.assertEqual(out["pending"], "")
        self.assertEqual(out["rendered"], {"codex": 1, "claude": 1})
        self.assertEqual(out["card"], "In use")
        self.assertEqual(out["focused"], '[data-account-runtime="codex"]')  # keyboard focus stays

    def test_a_failed_save_reverts_the_selection_and_shows_the_error(self) -> None:
        reply = "return {ok:false,status:400,json:async()=>({error:'account_runtime <b>refused</b>'})};"
        program = _settings_harness(BASE_SETTINGS, fetch_reply=reply) + r"""
(async()=>{
  await chooseAccountRuntime('codex');
  console.log(JSON.stringify({runtime:SETTINGS.account_runtime,baseline:SETTINGS_RUNTIME_BASELINE,status:RUNTIME_SWITCH_STATUS,html:host.innerHTML,requests}));
})();
"""
        out = _run(program)
        self.assertEqual(out["runtime"], "api")
        self.assertEqual(out["baseline"], "api")
        self.assertEqual(out["status"]["tone"], "err")
        self.assertEqual(out["requests"][-1], ["/api/settings", "GET"])
        self.assertIn("account_runtime <b>refused</b>", out["status"]["text"])
        self.assertIn("New runs still use API providers", out["status"]["text"])
        # Server text is escaped when rendered.
        self.assertIn("account_runtime &lt;b>refused&lt;/b>", out["html"])
        self.assertNotIn("<b>refused</b>", out["html"])

    def test_network_error_and_server_mismatch_also_revert(self) -> None:
        for reply, fragment in (
            ("throw new Error('offline');", "offline"),
            ("return {ok:true,status:200,json:async()=>({ok:true,settings:{...SETTINGS,account_runtime:'api'}})};", "kept"),
        ):
            with self.subTest(fragment=fragment):
                program = _settings_harness(BASE_SETTINGS, fetch_reply=reply) + r"""
(async()=>{ await chooseAccountRuntime('codex'); console.log(JSON.stringify({runtime:SETTINGS.account_runtime,status:RUNTIME_SWITCH_STATUS})); })();
"""
                out = _run(program)
                self.assertEqual(out["runtime"], "api")
                self.assertEqual(out["status"]["tone"], "err")
                self.assertIn(fragment, out["status"]["text"])

    def test_a_lost_response_adopts_the_runtime_the_server_saved(self) -> None:
        # The POST was applied but a proxy answered 502 (or the connection
        # dropped): the page must show the saved runtime, not the old one.
        for reply in (
            "return {ok:false,status:502,json:async()=>{throw new Error('not json');}};",
            "throw new TypeError('Failed to fetch');",
        ):
            with self.subTest(reply=reply):
                program = _settings_harness(
                    BASE_SETTINGS,
                    fetch_reply=reply,
                    get_reply="return {ok:true,status:200,json:async()=>({...SERVER,account_runtime:'codex'})};",
                ) + r"""
(async()=>{ await chooseAccountRuntime('codex');
  console.log(JSON.stringify({runtime:SETTINGS.account_runtime,baseline:SETTINGS_RUNTIME_BASELINE,status:RUNTIME_SWITCH_STATUS,
    card:runtimeCardState('codex').label,requests:requests.map(r=>r[1]==='GET'?'GET':'POST')})); })();
"""
                out = _run(program)
                self.assertEqual(out["runtime"], "codex")
                self.assertEqual(out["baseline"], "codex")  # Cancel keeps the saved runtime
                self.assertEqual(out["status"]["tone"], "ok")
                self.assertIn("New runs now use Codex subscription", out["status"]["text"])
                self.assertEqual(out["card"], "In use")
                self.assertEqual(out["requests"], ["POST", "GET"])

    def test_an_unconfirmed_failure_says_the_runtime_is_unknown(self) -> None:
        program = _settings_harness(
            BASE_SETTINGS,
            fetch_reply="throw new TypeError('Failed to fetch');",
            get_reply="throw new TypeError('Failed to fetch');",
        ) + r"""
(async()=>{ await chooseAccountRuntime('codex'); console.log(JSON.stringify({runtime:SETTINGS.account_runtime,status:RUNTIME_SWITCH_STATUS,pending:RUNTIME_SWITCH_PENDING,footer:nodes['settings-save'].disabled})); })();
"""
        out = _run(program)
        self.assertEqual(out["runtime"], "api")
        self.assertEqual(out["status"]["tone"], "err")
        self.assertIn("could not be confirmed", out["status"]["text"])
        self.assertNotIn("still use", out["status"]["text"])
        self.assertEqual(out["pending"], "")
        self.assertFalse(out["footer"])

    def test_save_cannot_race_a_pending_card_switch(self) -> None:
        program = _settings_harness(
            BASE_SETTINGS,
            fetch_reply="return {ok:true,status:200,json:async()=>({ok:true,settings:{...SERVER,account_runtime:'codex'}})};",
        ) + _function(SCRIPT, "saveSettings") + r"""
(async()=>{
  const switching=chooseAccountRuntime('codex');
  await saveSettings();                       // ignored while the card saves
  const duringSave=requests.length;
  SETTINGS_SAVE_PENDING=true; await chooseAccountRuntime('api'); const duringCard=requests.length; SETTINGS_SAVE_PENDING=false;
  await switching;
  console.log(JSON.stringify({duringSave,duringCard,requests,runtime:SETTINGS.account_runtime}));
})();
"""
        out = _run(program)
        self.assertEqual(out["duringSave"], 1)
        self.assertEqual(out["duringCard"], 1)
        self.assertEqual(out["requests"], [["/api/settings", {"updates": {}, "account_runtime": "codex"}]])
        self.assertEqual(out["runtime"], "codex")

    def test_the_api_card_opens_the_key_field_and_saves_a_typed_key_with_the_runtime(self) -> None:
        settings = {
            **BASE_SETTINGS,
            "account_runtime": "codex",
            "sponsored_kimi_available": False,
            "providers": [{"id": "openai", "key_var": "OPENAI_API_KEY"}],
        }
        reply = (
            "return {ok:true,status:200,json:async()=>({ok:true,settings:{...SERVER,account_runtime:'api',"
            "configured_providers:['openai']}})};"
        )
        program = _settings_harness(settings, fetch_reply=reply) + r"""
let keyFocused=false;
const key={dataset:{key:'OPENAI_API_KEY'},value:'',placeholder:'Paste your API key',focus(){keyFocused=true;}};
const base={dataset:{key:'OPENAI_BASE_URL'},value:'https://api.openai.com/v1',placeholder:''};
const otherBase={dataset:{key:'KIMI_API_BASE'},value:'https://kimi.example/v1',placeholder:''};
const openai={querySelectorAll:()=>[key,base]}, kimi={querySelectorAll:()=>[otherBase]};
nodes['api-provider-group']={open:false,scrollIntoView(){scrolled='api-provider-group';},
  querySelectorAll:sel=>sel==='.prov-card'?[openai,kimi]:[key,base,otherBase],querySelector:sel=>sel.includes('password')?key:null};
(async()=>{
  await chooseAccountRuntime('api');
  const refused={requests:requests.length,open:nodes['api-provider-group'].open,scrolled,keyFocused,status:RUNTIME_SWITCH_STATUS,runtime:SETTINGS.account_runtime};
  key.value='  sk-typed  ';
  await chooseAccountRuntime('api');
  console.log(JSON.stringify({refused,requests,runtime:SETTINGS.account_runtime,status:RUNTIME_SWITCH_STATUS,key:key.value,placeholder:key.placeholder,base:base.value,card:runtimeCardState('api').label}));
})();
"""
        out = _run(program)
        refused = out["refused"]
        self.assertEqual(refused["requests"], 0)
        self.assertTrue(refused["open"])  # the collapsed group is opened
        self.assertEqual(refused["scrolled"], "api-provider-group")
        self.assertTrue(refused["keyFocused"])
        self.assertIn("Paste a provider key", refused["status"]["text"])
        self.assertEqual(refused["runtime"], "codex")
        self.assertEqual(
            out["requests"],
            [["/api/settings", {"updates": {"OPENAI_API_KEY": "sk-typed", "OPENAI_BASE_URL": "https://api.openai.com/v1"}, "account_runtime": "api"}]],
        )
        self.assertEqual(out["runtime"], "api")
        self.assertEqual(out["status"]["tone"], "ok")
        self.assertEqual(out["key"], "")  # the saved key does not linger in the page
        self.assertEqual(out["placeholder"], "Key saved — paste to replace")
        self.assertEqual(out["base"], "https://api.openai.com/v1")
        self.assertNotIn("KIMI_API_BASE", out["requests"][0][1]["updates"])  # other cards wait for Save
        self.assertEqual(out["card"], "In use")

    def test_unconnected_cards_and_guests_never_save(self) -> None:
        program = _settings_harness(BASE_SETTINGS, fetch_reply="return {ok:true,json:async()=>({})};") + r"""
(async()=>{
  await chooseAccountRuntime('claude');
  const unconnected={requests:requests.length,runtime:SETTINGS.account_runtime,status:RUNTIME_SWITCH_STATUS,scrolled};
  await chooseAccountRuntime('api');
  const same=requests.length;
  CURRENT_USER.guest=true; await chooseAccountRuntime('codex');
  console.log(JSON.stringify({unconnected,same,guest:requests.length,runtime:SETTINGS.account_runtime}));
})();
"""
        out = _run(program)
        self.assertEqual(out["unconnected"]["requests"], 0)
        self.assertEqual(out["unconnected"]["runtime"], "api")
        self.assertIn("Connect Claude Code below first", out["unconnected"]["status"]["text"])
        self.assertEqual(out["unconnected"]["scrolled"], "claude-card")
        self.assertEqual(out["same"], 0)
        self.assertEqual(out["guest"], 0)
        self.assertEqual(out["runtime"], "api")

    def test_save_button_keeps_the_same_request_shape(self) -> None:
        self.assertIn("{updates,account_runtime:selectedAccountRuntime()}", SCRIPT)
        self.assertIn("body:JSON.stringify({updates:typed,account_runtime:runtime})", SCRIPT)
        self.assertIn("const typed=runtime==='api'&&!spec.ready?typedApiProviderFields():{};", SCRIPT)

    def test_connection_cards_say_whether_the_account_is_in_use(self) -> None:
        program = r"""
let SETTINGS={account_runtime:'api'};
const LIB_ESC=s=>String(s);
""" + _functions("selectedAccountRuntime", "codexCardBody", "claudeCardBody") + r"""
const idle={codex_installed:true,claude_installed:true,status:'idle',connected:true};
const out=[codexCardBody(idle).includes('Connected, not in use'),claudeCardBody(idle).includes('Connected, not in use')];
SETTINGS.account_runtime='codex'; out.push(codexCardBody(idle).includes('Connected and in use'));
SETTINGS.account_runtime='claude'; out.push(claudeCardBody(idle).includes('Connected and in use'));
console.log(JSON.stringify(out));
"""
        self.assertEqual(_run(program), [True, True, True, True])


class LauncherRuntimeTests(unittest.TestCase):
    def _summary(self, settings: dict, *, model: str = "", issue: str = "", guest: bool = False) -> dict:
        program = (
            "let SETTINGS=" + json.dumps(settings) + ", CURRENT_USER=" + json.dumps({"guest": guest}) + ", GUEST_POLICY=null;\n"
            "let ENGINES=[{id:'plain'}], engine='plain';\n"
            "const currentModel=()=>" + json.dumps(model) + ";\n"
            "const engineRuntimeIssue=()=>" + json.dumps(issue) + ";\n"
            "const modelVisual=m=>({provider:String(m).startsWith('gpt')?'OpenAI':'Provider'});\n"
            "const isCgmMax=()=>" + json.dumps(model == "max") + ";\n"
            "const cgmMaxSummary=()=>'Claude Code fable plans, verifies and curates; Codex gpt-6-astra writes and repairs, both at maximum effort.';\n"
            + _functions("selectedAccountRuntime", "runRuntimeSummary")
            + "\nconsole.log(JSON.stringify(runRuntimeSummary()));"
        )
        return _run(program)

    def test_launcher_describes_max(self) -> None:
        summary = self._summary({"account_runtime": "api", "codex_connected": True, "claude_connected": True}, model="max")
        self.assertEqual((summary["lead"], summary["strong"]), ("Runs on", "Max"))
        self.assertIn("Codex gpt-6-astra writes and repairs", summary["spare"])

    def test_launcher_names_the_runtime_and_unused_connected_accounts(self) -> None:
        api = self._summary(
            {"account_runtime": "api", "sponsored_kimi_available": True, "codex_connected": True, "claude_connected": True},
            model="kimi-k3",
        )
        self.assertEqual(api["strong"], "Kimi K3 (included)")
        self.assertFalse(api["warn"])
        self.assertEqual(api["spare"], " Codex and Claude Code are connected but not in use.")
        codex = self._summary({"account_runtime": "codex", "codex_connected": True}, model="gpt-6-astra")
        self.assertEqual(codex["strong"], "your Codex (ChatGPT) subscription")
        self.assertEqual(codex["spare"], "")
        own_key = self._summary(
            {"account_runtime": "api", "claude_connected": True, "configured_providers": ["openai"]},
            model="gpt-5.5",
        )
        self.assertEqual(own_key["strong"], "gpt-5.5 via your API key")
        self.assertEqual(own_key["spare"], " Claude Code is connected but not in use.")
        unconfigured = self._summary({"account_runtime": "api"}, model="kimi-k3")
        self.assertTrue(unconfigured["warn"])
        self.assertIn("no API provider is configured", unconfigured["text"])

    def test_launcher_warns_when_the_runtime_cannot_run_this_harness(self) -> None:
        blocked = self._summary(
            {"account_runtime": "claude", "claude_connected": False},
            issue="Claude Code is selected for new runs but is not connected. Connect it in Settings.",
        )
        self.assertTrue(blocked["warn"])
        self.assertIn("not connected", blocked["text"])
        self.assertIsNone(self._summary({"account_runtime": "api"}, guest=True))

    def test_launcher_text_is_built_without_html(self) -> None:
        body = _function(SCRIPT, "renderRunRuntime")
        self.assertNotIn("innerHTML", body)
        self.assertIn("renderRunRuntime();", _function(SCRIPT, "renderModels"))
        self.assertIn("$('run-runtime-change').onclick=openSettings;", SCRIPT)

    def test_a_stale_route_or_model_refusal_reloads_settings(self) -> None:
        body = _function(SCRIPT, "startRun")
        self.assertIn(
            "if(/Unsupported (?:Codex|Claude) account model|conflicts with the selected/i.test(String(d.error||'')))loadSettings({keepEngine:true});",
            body,
        )

    def test_settings_refreshes_keep_the_chosen_harness(self) -> None:
        # Only the first load applies the saved default engine; a refused
        # start, opening Settings or a new login must not swap the harness.
        self.assertIn("await loadSettings({keepEngine:true});", _function(SCRIPT, "openSettings"))
        self.assertIn("if(needsModels)await loadSettings({keepEngine:true});", _function(SCRIPT, "applyCodexStatus"))
        self.assertIn("Promise.all([loadEngines(),loadSettings()])", SCRIPT)
        program = r"""
let SETTINGS=null, GUEST_POLICY=null, CURRENT_USER={guest:false}, runId=null, engine='math_harness';
const ENGINES=[{id:'kimi'},{id:'math_harness'}];
globalThis.fetch=async()=>({json:async()=>({account_runtime:'codex',default_engine:'kimi'})});
const noop=()=>{}; const syncRuntimeModelControls=noop, renderRuntimeSelector=noop, syncApiProviderGroup=noop, renderEngines=noop, renderModels=noop, applyGuestUi=noop;
const $=()=>({textContent:''});
""" + _function(SCRIPT, "loadSettings") + r"""
(async()=>{
  await loadSettings({keepEngine:true}); const kept=engine;
  await loadSettings(); console.log(JSON.stringify([kept,engine,SETTINGS.account_runtime]));
})();
"""
        self.assertEqual(_run(program), ["math_harness", "kimi", "codex"])


if __name__ == "__main__":
    unittest.main()
