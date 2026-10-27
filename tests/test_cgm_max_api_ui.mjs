// Ansatz CGM "Max (API)" in the console (console.html): the model picker
// offers it next to Max when the settings' cgm_max_api plan is available,
// never as the automatic choice; choosing it asks for the API route with the
// fixed maximum-effort profile, whatever the account runtime; the picker, the
// launcher, the run-profile note and a run's chips say it is billed to API keys
// and name the plan's models. Subscription Max reads as before. The real
// console functions run in a vm context over a few stub elements.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const html = readFileSync(new URL('../agent_monitor/web/console.html', import.meta.url), 'utf8')
const script = html.split('<script>').at(-1).split('</script>')[0]

// Minimal JS scanner (as in test_analyze_ui.mjs): skips strings, template
// literals, comments and regex literals, so braces inside them never end a
// function early.
function scanEnd(source, start, { stopAtSemicolon = false } = {}) {
  const keywords = /(?:^|[^\w$])(?:return|typeof|case|do|else|in|of|new|delete|void|throw|yield|await)$/
  let depth = 0, i = start
  const modes = []
  while (i < source.length) {
    const c = source[i], n = source[i + 1]
    const mode = modes.at(-1)
    if (mode === 'template') {
      if (c === '\\') { i += 2; continue }
      if (c === '`') { modes.pop(); i++; continue }
      if (c === '$' && n === '{') { modes.push(depth); depth = 0; i += 2; continue }
      i++; continue
    }
    if (c === '/' && n === '/') { i = source.indexOf('\n', i); if (i < 0) return source.length; continue }
    if (c === '/' && n === '*') { i = source.indexOf('*/', i + 2) + 2; continue }
    if (c === '"' || c === "'") {
      i++
      while (i < source.length && source[i] !== c) i += source[i] === '\\' ? 2 : 1
      i++; continue
    }
    if (c === '`') { modes.push('template'); i++; continue }
    if (c === '/') {
      const before = source.slice(start, i).trimEnd()
      if (!before || /[(,=:[!&|?{};+\-*%<>~^]$/.test(before) || keywords.test(before)) {
        i++
        let inClass = false
        while (i < source.length) {
          const r = source[i]
          if (r === '\\') { i += 2; continue }
          if (r === '[') inClass = true
          else if (r === ']') inClass = false
          else if (r === '/' && !inClass) break
          i++
        }
        i++
        while (/[a-z]/i.test(source[i] || '')) i++
        continue
      }
    }
    if (c === '{') depth++
    else if (c === '}') {
      if (depth === 0 && typeof mode === 'number') { depth = modes.pop(); i++; continue }
      depth--
      if (depth === 0 && !stopAtSemicolon) return i + 1
    } else if (c === ';' && depth === 0 && stopAtSemicolon) return i + 1
    i++
  }
  throw new Error('unterminated source at ' + start)
}
function fn(name) {
  const at = script.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  // Skip the parameter list first: a default such as model=currentModel() has parentheses.
  let i = script.indexOf('(', at), depth = 0
  for (; i < script.length; i++) {
    if (script[i] === '(') depth++
    else if (script[i] === ')' && --depth === 0) break
  }
  return script.slice(at, scanEnd(script, i + 1))
}
function constant(name) {
  const at = script.indexOf(`const ${name}=`)
  assert.ok(at >= 0, 'missing const ' + name)
  return script.slice(at, scanEnd(script, at, { stopAtSemicolon: true }))
}
// Values cross the vm boundary as plain data (objects from another realm are never deep-equal).
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))

const FUNCTIONS = [
  'selectedAccountRuntime', 'runtimeDisplayName', 'engineRuntimeIssue', 'engineModelMode', 'modelsForEngine',
  'currentModel', 'dagIsCgmEngine', 'cgmEngineUsable', 'cgmModels', 'cgmEngineFor', 'renderModels',
  'cgmMaxAvailable', 'isCgmMax', 'isCgmMaxApi', 'cgmMaxSummary', 'cgmMaxApiNames', 'cgmMaxApiSummary',
  'isCgmMaxApiRun', 'runAuthRoute', 'modelVisual', 'viewerIsAdmin', 'viewerHasFullAccess',
  'runTuningCapabilities', 'runTuningStorage', 'runTuningStorageKey', 'currentRunTuning', 'renderRunTuning',
  'runRuntimeSummary', 'withMaxApiHint', 'runtimeUiSpec', 'runtimeCardState', 'updateLiveChips', 'isAnalysisRun', 'isReadabilityRun', 'escapeHtml',
  'syncApiProviderGroup', 'startRun', 'convoKind', 'convoModeButtons', 'renderConversation', 'engineView',
]
// The page around them: elements made on first use (the model select keeps
// only a value it has an option for, as a browser's does), the choice menu,
// the run-profile store and the start request.
const PAGE = `
const els={}, menus={}, posts=[];
function selectElement(){
  let options=[], current='';
  return {id:'model-select',disabled:false,_html:'',
    set innerHTML(markup){ this._html=markup; options=[...markup.matchAll(/<option value="([^"]*)"/g)].map(m=>m[1]); current=options[0]??''; },
    get innerHTML(){ return this._html; },
    set value(next){ current=options.includes(String(next))?String(next):''; },
    get value(){ return current; }};
}
function element(id){
  return {id,value:'',textContent:'',innerHTML:'',title:'',className:'',disabled:false,open:false,attrs:{},
    style:{setProperty(){}},classList:{toggle(){},add(){},remove(){}},
    setAttribute(name,value){ this.attrs[name]=value; },querySelectorAll:()=>[]};
}
const $=id=>els[id]||(els[id]=id==='model-select'?selectElement():element(id));
const renderChoiceMenu=(id,title,options,footer='')=>{ menus[id]={options,footer}; };
const renderEngines=()=>renderModels(), renderRunRuntime=()=>{}, animateRunTuningFeedback=()=>{};
const RUN_TUNING_KEY='tuning', tuningStore={};
const localStorage={getItem:key=>tuningStore[key]??null,setItem:(key,value)=>{ tuningStore[key]=String(value); }};
const runKindChoice=()=>'prove', formalizeChoice=()=>true, showEngineSignIn=()=>{}, setPill=()=>{};
const selectRun=()=>{}, goSession=()=>{}, loadSettings=async()=>{}, applyGuestUi=()=>{}, pcUrl=path=>path, location={href:''};
const initialAttachments={busy:false,files:[],lock(){},clear(){},payload:async()=>[]};
const fetch=async(url,options)=>{ posts.push({url,body:JSON.parse(options.body)}); return {ok:true,status:200,json:async()=>({run_id:'r1',job_id:'j1'})}; };
let convoKey='', convoMode='agent'; const convoOpen=new Set();
const bindConversationControls=()=>{}, renderMathIn=()=>{}, formalConversationHtml=()=>'', mdToHtml=text=>String(text);
let CURRENT_USER={id:1,is_admin:true}, GUEST_POLICY=null, engine='kimi', ENGINES=[], SETTINGS=null;
`
function load() {
  const consts = ['LIB_ESC', 'CGM_MAX_MODEL', 'KIND_META', 'fmtTok', 'ENGINE_MARK', 'ENGINE_TONE', 'CGM_FAMILY', 'ENGINE_AUTH_LABELS', 'CGM_ENGINE_COPY']
  const code = [PAGE, ...consts.map(constant), ...FUNCTIONS.map(fn)].join('\n')
  const context = vm.createContext({ console })
  vm.runInContext(code, context)
  const js = source => plain(vm.runInContext(source, context))
  const set = (name, value) => vm.runInContext(`${name}=${JSON.stringify(value)}`, context)
  // A choice in the menu, made on the rendered picker as on the page.
  const pick = model => { vm.runInContext(`renderModels(); $('model-select').value=${JSON.stringify(model)}; renderModels();`, context) }
  return { context, js, set, pick }
}

const KIMI = { id: 'kimi', label: 'Ansatz CGM · Kimi K3', available: true, auth_modes: ['api_key'], supported_models: ['kimi-k3'] }
const HARNESS = { id: 'math_harness', label: 'Ansatz CGM harness', available: true, auth_modes: ['codex_subscription', 'claude_subscription', 'api_key'] }
const PLAIN = { id: 'plain', label: 'Plain (no harness)', available: true, auth_modes: ['codex_subscription', 'claude_subscription', 'api_key'] }
const ANALYZE = { id: 'analyze', label: 'Ansatz Analyze', available: true, auth_modes: ['codex_subscription', 'claude_subscription', 'api_key'] }
const ENGINES = [KIMI, HARNESS, PLAIN, ANALYZE]
// /api/settings' cgm_max_api (cgm_max.plan_for_api) and cgm_max (plan_for).
const API_PLAN = {
  available: true, reason: '', mode: 'api',
  advisor: { route: 'api_key', provider: 'anthropic', model: 'claude-fable-5-1', effort: 'max', roles: ['strategist', 'verifier', 'curator'] },
  worker: { route: 'api_key', provider: 'openai', model: 'gpt-6-astra', effort: 'max', roles: ['worker', 'repair'] },
}
const MAX_PLAN = {
  available: true, reason: '',
  advisor: { route: 'claude_subscription', model: 'fable', effort: 'max', roles: ['strategist', 'verifier', 'curator'] },
  worker: { route: 'codex_subscription', model: 'gpt-6-astra', effort: 'max', roles: ['worker', 'repair'] },
}
const NO_MAX = { available: false, reason: 'Max needs both Claude Code and Codex linked; connect Claude Code in Settings' }
const NO_API = { available: false, reason: 'Max (API) needs both an Anthropic and an OpenAI API key; add your OpenAI key in Settings' }
// The administrator on the API runtime, with both kinds of Max.
const ADMIN = {
  account_runtime: 'api', api_models: ['kimi-k3', 'gpt-6-astra', 'claude-opus-5-5'], configured_providers: ['anthropic', 'openai'],
  default_model: 'kimi-k3', sponsored_kimi_available: true, sponsored_kimi_engines: ['kimi', 'plain', 'math_harness'],
  kimi_user_key_set: false, codex_connected: true, claude_connected: true, codex_models: ['gpt-6-astra', 'gpt-6-sol'],
  claude_models: ['fable', 'opus'], cgm_max: MAX_PLAN, cgm_max_api: API_PLAN, run_tuning_by_runtime: { api: {}, codex: {}, claude: {} },
  max_iterations: 40,
}
// A Claude Code runtime whose account is gone: no runtime route can run, Max needs it too.
const BROKEN_RUNTIME = { ...ADMIN, account_runtime: 'claude', claude_connected: false, claude_models: [], cgm_max: NO_MAX }
const SUMMARY = 'Claude Fable 5.1 plans and checks · GPT-6 Astra writes · max effort · billed to API keys'

test('the Ansatz CGM picker offers Max (API) next to Max, from its own plan in the settings', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', ADMIN)
  assert.deepEqual(page.js('cgmModels()'), ['max', 'max-api', 'kimi-k3', 'gpt-6-astra', 'claude-opus-5-5'])
  assert.equal(page.js("cgmEngineFor('max-api')"), 'math_harness')
  // Without the linked subscriptions it still leads the list on its own.
  page.set('SETTINGS', { ...ADMIN, cgm_max: NO_MAX })
  assert.deepEqual(page.js('cgmModels()'), ['max-api', 'kimi-k3', 'gpt-6-astra', 'claude-opus-5-5'])
  // Its own plan decides: with a key missing only Max is offered.
  page.set('SETTINGS', { ...ADMIN, cgm_max_api: NO_API })
  assert.deepEqual(page.js('cgmModels()'), ['max', 'kimi-k3', 'gpt-6-astra', 'claude-opus-5-5'])
  // Neither for a harness that needs a sign-in (a guest's view) or has no API route.
  page.set('SETTINGS', { ...ADMIN, cgm_max: NO_MAX })
  page.set('ENGINES', [KIMI, { ...HARNESS, requires_login: true }, PLAIN])
  assert.ok(!page.js('cgmModels()').includes('max-api'))
  page.set('ENGINES', [KIMI, { ...HARNESS, auth_modes: ['codex_subscription', 'claude_subscription'] }, PLAIN])
  assert.ok(!page.js('cgmModels()').includes('max-api'))
  // The account runtime does not matter: with the Claude Code runtime disconnected
  // nothing else can run, but Max (API) can, on the harness.
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  assert.deepEqual(page.js('cgmModels()'), ['max-api'])
  assert.equal(page.js("cgmEngineFor('max-api')"), 'math_harness')
  page.set('SETTINGS', { ...BROKEN_RUNTIME, cgm_max_api: NO_API })
  assert.deepEqual(page.js('cgmModels()'), [])
  assert.equal(page.js("cgmEngineFor('max-api')"), 'kimi')
})

test('Max (API) is never the automatic choice, not even as the saved default', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', { ...ADMIN, cgm_max: NO_MAX, default_model: 'max-api' })
  page.js('renderModels()')
  assert.equal(page.js("$('model-select').value"), 'kimi-k3')
  assert.match(page.js("$('model-select').innerHTML"), /<option value="max-api">✦ Max \(API\)<\/option>/)
  // Where it is the only choice it is offered, but nothing is chosen for the visitor.
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.set('engine', 'kimi')
  page.js("$('model-select').innerHTML=''; renderModels()")
  assert.equal(page.js("$('model-select').value"), '')
  assert.equal(page.js("$('model-current-name').textContent"), 'Account connection required')
  assert.match(page.js("menus['model-menu'].options"), /<b>Max \(API\)<\/b>/)
  // Chosen, it runs on the harness and the trigger says how it is billed.
  page.pick('max-api')
  assert.equal(page.js('engine'), 'math_harness')
  assert.equal(page.js("$('model-current-name').textContent"), 'Max (API)')
  assert.equal(page.js("$('model-current-meta').textContent"), 'Anthropic + OpenAI API keys · max effort')
  assert.equal(page.js("$('model-current-mark').textContent"), 'MX')
})

test('choosing Max (API) asks for the API route with the fixed maximum-effort profile, whatever the runtime', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  for (const settings of [ADMIN, { ...ADMIN, account_runtime: 'codex' }, BROKEN_RUNTIME]) {
    page.set('SETTINGS', settings)
    page.set('engine', 'kimi')
    page.pick('max-api')
    assert.equal(page.js('engine'), 'math_harness')
    assert.equal(page.js('runAuthRoute()'), 'api_key', settings.account_runtime)
    const caps = page.js('runTuningCapabilities()')
    assert.deepEqual([caps.reasoning.map(item => item.id), caps.speed.map(item => item.id), caps.max_mode], [['max'], ['standard'], true])
    assert.deepEqual(page.js('currentRunTuning()'), { reasoning_effort: 'max', speed_mode: 'standard' })
    assert.equal(page.js('runTuningStorageKey()'), 'api_key:max-api')
    assert.equal(page.js("$('run-profile-summary').textContent"), 'Ultra')
    assert.equal(page.js("$('run-profile-note').textContent"),
      'Max (API) runs Claude Fable 5.1 and GPT-6 Astra at maximum effort, billed to API keys. Formal Lean and Lean DAG run on GPT-6 Astra at High.')
    assert.equal(page.js("$('run-thinking-slider').disabled"), true)
    assert.equal(page.js("$('run-speed-toggle').disabled"), true)
  }
  // Max keeps its subscriptions and its own note; another model follows the runtime.
  page.set('SETTINGS', { ...ADMIN, account_runtime: 'codex' })
  page.pick('max')
  assert.equal(page.js('runAuthRoute()'), 'dual_subscription')
  assert.equal(page.js("$('run-profile-note').textContent"),
    'Max runs Claude Code and Codex at maximum effort. Formal Lean and Lean DAG run on Codex at High.')
  page.pick('gpt-6-sol')
  assert.equal(page.js('runAuthRoute()'), 'codex_subscription')
  assert.equal(page.js('runTuningCapabilities().max_mode'), undefined)
  // Analyze (and Readability) never send it: only an Ansatz CGM run is Max (API).
  page.set('engine', 'analyze')
  page.js(`$('model-select').innerHTML='<option value="max-api"></option>'; $('model-select').value='max-api'`)
  assert.equal(page.js('isCgmMaxApi()'), false)
  assert.equal(page.js('runAuthRoute()'), 'codex_subscription')
})

test('the picker and the launcher say Max (API) is billed to API keys and name its models', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', ADMIN)
  page.pick('max-api')
  const options = page.js("menus['model-menu'].options")
  const row = model => options.split('</button>').find(item => item.includes(`data-model="${model}"`))
  assert.match(row('max-api'), /<b>Max \(API\)<\/b>/)
  assert.match(row('max-api'), /Claude leads · GPT works · billed to API keys/)
  assert.ok(row('max-api').includes(`title="${SUMMARY}"`))
  // Max's row reads as before.
  assert.match(row('max'), /<b>max<\/b>/)
  assert.match(row('max'), /Claude Code leads · Codex works · max effort/)
  assert.ok(row('max').includes('title="Claude Code fable plans, verifies and curates; Codex gpt-6-astra writes and repairs, both at maximum effort."'))
  assert.deepEqual(page.js('runRuntimeSummary()'), { warn: false, lead: 'Runs on', strong: 'Max (API)', spare: ` ${SUMMARY}.` })
  // It does not use the account runtime, so a disconnected one is no warning here.
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.pick('max-api')
  assert.deepEqual(page.js('runRuntimeSummary()'), { warn: false, lead: 'Runs on', strong: 'Max (API)', spare: ` ${SUMMARY}.` })
  page.set('SETTINGS', ADMIN)
  page.pick('max')
  assert.equal(page.js('runRuntimeSummary().strong'), 'Max')
  page.pick('gpt-6-astra')
  assert.equal(page.js('runRuntimeSummary().strong'), 'gpt-6-astra via your API key')
  assert.deepEqual(page.js("[modelVisual('max-api').mark,modelVisual('max-api').provider,modelVisual('max').provider,modelVisual('gpt-5').provider]"),
    ['MX', 'Anthropic + OpenAI API keys', 'Claude Code + Codex', 'OpenAI · Codex-ready'])
})

test('the plan\'s model ids read as model names; any other id is shown as it is, and escaped', () => {
  const page = load()
  page.set('SETTINGS', ADMIN)
  const names = (advisor, worker) => page.js(`cgmMaxApiNames(${JSON.stringify({ advisor: { model: advisor }, worker: { model: worker } })})`)
  assert.deepEqual(names('claude-fable-5-1', 'gpt-6-astra'), ['Claude Fable 5.1', 'GPT-6 Astra'])
  assert.deepEqual(names('claude-opus-5-5', 'gpt-6.1-sol'), ['Claude Opus 5.5', 'GPT-6.1 Sol'])
  assert.deepEqual(names('claude-sonnet-5-5', 'gpt-5.6-sol'), ['Claude Sonnet 5.5', 'GPT-5.6 Sol'])
  assert.deepEqual(names('claude-opus-4-8-20260101', 'o9-pro'), ['claude-opus-4-8-20260101', 'o9-pro'])
  // The settings' plan by default, and its defaults when a side is missing.
  assert.equal(page.js('cgmMaxApiSummary()'), SUMMARY)
  assert.deepEqual(page.js('cgmMaxApiNames({})'), ['Claude Fable 5.1', 'GPT-6 Astra'])
  const hostile = '<img src=x onerror="alert(1)">'
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', { ...ADMIN, cgm_max_api: { ...API_PLAN, worker: { ...API_PLAN.worker, model: hostile } } })
  page.js('renderModels()')
  const options = page.js("menus['model-menu'].options")
  assert.ok(!options.includes(hostile))
  assert.ok(options.includes('writes · max effort'))
  assert.ok(options.includes('&lt;img src=x onerror=&quot;alert(1)&quot;>'))
})

test('a Max (API) run shows Max (API), its route_plan models and that it was billed to API keys', () => {
  const page = load()
  const chips = run => { page.js(`updateLiveChips(${JSON.stringify(run)})`); return page.js("$('chat-live').innerHTML") }
  const apiRun = {
    run_id: 'r1', status: 'finished', engine: 'math_harness', model: 'max-api', requested_model: 'max-api', auth_route: 'api_key',
    route_plan: { mode: 'api', advisor: { ...API_PLAN.advisor, model: 'claude-opus-5-5' }, worker: { ...API_PLAN.worker, model: 'gpt-6.1-sol' } },
    live: { auth_mode: 'api_key', model: 'max-api', model_requested: false },
  }
  const shown = chips(apiRun)
  assert.match(shown, /◆ Max \(API\)<\/span>/)
  assert.ok(shown.includes('title="Max (API): Claude Opus 5.5 plans and checks · GPT-6.1 Sol writes · max effort · billed to API keys"'))
  assert.match(shown, /title="Max \(API\): billed to the Anthropic and OpenAI API keys, not a subscription">◉ Anthropic \+ OpenAI API<\/span>/)
  assert.ok(!shown.includes('Claude Code + Codex'))
  // The route plan alone marks one (as cgm_max.is_max_api_record does).
  assert.match(chips({ ...apiRun, model: null, requested_model: null, live: { auth_mode: 'api_key' }, agents: [{ model: 'claude-opus-5-5' }] }), /◆ Max \(API\)/)
  // A Max run reads as before, even with a stray mode in its plan.
  const maxRun = {
    run_id: 'r2', status: 'finished', engine: 'math_harness', model: 'max', auth_route: 'dual_subscription',
    route_plan: { mode: 'api', advisor: MAX_PLAN.advisor, worker: MAX_PLAN.worker }, live: { auth_mode: 'dual_subscription', model: 'max', model_requested: false },
  }
  const max = chips(maxRun)
  assert.match(max, /◆ max<\/span>/)
  assert.ok(max.includes('title="Max: Claude Code fable plans, verifies and curates; Codex gpt-6-astra writes and repairs"'))
  assert.match(max, /◉ Claude Code \+ Codex/)
  assert.ok(!max.includes('Max (API)'))
  // An ordinary API run is neither.
  const single = chips({ run_id: 'r3', status: 'finished', engine: 'math_harness', model: 'gpt-6-astra', auth_route: 'api_key', live: { auth_mode: 'api_key', model: 'gpt-6-astra' } })
  assert.match(single, /◆ gpt-6-astra/)
  assert.ok(!single.includes('Max'))
  assert.ok(!single.includes('Anthropic + OpenAI'))
})

test('the Monitor names a Max (API) run\'s two models, and each turn the model that answered it', () => {
  const page = load()
  const convo = run => { page.js(`renderConversation(${JSON.stringify(run)},true)`); return page.js("$('convo-col').innerHTML") }
  const agents = [
    { call_seq: 1, role: 'strategist', stage_name: 'strategist', model: 'claude-fable-5-1', output: 'A plan.' },
    { call_seq: 2, role: 'worker', stage_name: 'worker-1', model: 'gpt-6-astra', output: 'An attempt.' },
  ]
  const shown = convo({
    run_id: 'r1', status: 'finished', engine: 'math_harness', model: 'max-api', auth_route: 'api_key',
    route_plan: { mode: 'api', advisor: API_PLAN.advisor, worker: API_PLAN.worker }, agents,
  })
  assert.ok(shown.includes('<span class="convo-chip">Max (API) · Claude Fable 5.1 + GPT-6 Astra · API keys</span>'))
  assert.ok(shown.includes('strategist · claude-fable-5-1'))
  assert.ok(shown.includes('worker · gpt-6-astra'))
  // A Max run's header is unchanged.
  const max = convo({
    run_id: 'r2', status: 'finished', engine: 'math_harness', model: 'max', auth_route: 'dual_subscription',
    route_plan: { advisor: MAX_PLAN.advisor, worker: MAX_PLAN.worker }, agents,
  })
  assert.ok(!max.includes('Max (API)'))
  assert.ok(max.includes('<span class="convo-chip">math_harness · finished</span>'))
})

test('starting Max (API) sends the API route and the fixed profile, even when the saved runtime cannot run', async () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.set('engine', 'kimi')
  page.js("$('problem-text').value='Prove that every finite integral domain is a field.'")
  page.pick('max-api')
  await vm.runInContext('startRun()', page.context)
  assert.equal(page.js("$('form-error').style.display"), 'none')
  assert.deepEqual(page.js('posts.map(post=>post.url)'), ['/api/runs'])
  const body = page.js('posts[0].body')
  assert.deepEqual(
    [body.engine, body.model, body.auth_route, body.reasoning_effort, body.speed_mode, body.use_subagents, body.subagent_model],
    ['math_harness', 'max-api', 'api_key', 'max', 'standard', false, null])
  // Anything else still needs the runtime: nothing is sent.
  page.js("$('model-select').innerHTML=''")
  await vm.runInContext('startRun()', page.context)
  assert.equal(
    page.js("$('form-error').textContent"),
    'Claude Code is selected for new runs but is not connected. Connect it in Settings. ' +
      'Max (API) can still run on your API keys: choose it in the model menu.',
  )
  assert.equal(page.js('posts.length'), 1)
})

test('Settings say the API keys still serve Max (API) while another runtime is selected', () => {
  const page = load()
  page.set('SETTINGS', { ...ADMIN, account_runtime: 'codex' })
  page.js('syncApiProviderGroup()')
  assert.equal(page.js("$('api-provider-summary-meta').textContent"), 'Dormant for new runs except Max (API) · 2 own keys')
  page.set('SETTINGS', { ...ADMIN, account_runtime: 'codex', cgm_max_api: NO_API })
  page.js('syncApiProviderGroup()')
  assert.equal(page.js("$('api-provider-summary-meta').textContent"), 'Dormant for new runs · 2 own keys')
  page.set('SETTINGS', ADMIN)
  page.js('syncApiProviderGroup()')
  assert.equal(page.js("$('api-provider-summary-meta').textContent"), 'Kimi K3 included · 2 own keys')
})

test('with the saved runtime unusable, the composer says only Max (API) can run, and offers it', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.set('engine', 'kimi')
  page.pick('max-api')
  assert.equal(page.js("runRuntimeSummary().strong"), 'Max (API)')
  assert.equal(
    page.js("menus['model-menu'].footer").replace(/<[^>]+>/g, ''),
    'Only Max (API) runs while the selected runtime is unavailable: it uses the Anthropic and OpenAI API keys.',
  )
  const view = page.js("engineView(ENGINES.find(e => e.id === 'math_harness'))")
  assert.equal(view.usable, true)
  assert.equal(view.issue, '')
  assert.match(view.copy, /^Only Max \(API\) can run until the selected runtime is connected/)
})

test('from another engine, Ansatz CGM stays reachable for Max (API) while the saved runtime is unusable', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.set('engine', 'plain')
  page.js('renderModels()')
  for (const id of ['kimi', 'math_harness']) {
    assert.equal(page.js(`engineView(ENGINES.find(e => e.id === '${id}')).usable`), true, id)
  }
  // Without the Max (API) plan, nothing on Ansatz CGM can run: it stays unavailable, as before.
  page.set('SETTINGS', { ...BROKEN_RUNTIME, cgm_max_api: NO_API })
  for (const id of ['kimi', 'math_harness']) {
    const view = page.js(`engineView(ENGINES.find(e => e.id === '${id}'))`)
    assert.equal(view.usable, false, id)
    assert.notEqual(view.issue, '', id)  // and it says why
  }
})

test('while the runtime blocks Ansatz CGM, the launcher and Prove say Max (API) can still run', async () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  page.set('engine', 'plain')
  page.js('renderModels()')
  page.set('engine', 'kimi')
  page.js('renderModels()')
  const summary = page.js('runRuntimeSummary()')
  assert.equal(summary.warn, true)
  assert.match(summary.text, /Max \(API\) can still run on your API keys: choose it in the model menu\.$/)
  // Without the Max (API) plan, the runtime issue reads as before.
  page.set('SETTINGS', { ...BROKEN_RUNTIME, cgm_max_api: NO_API })
  assert.doesNotMatch(page.js('runRuntimeSummary().text'), /Max \(API\)/)
})

test('the Settings runtime card says Max (API) can still start while the runtime is disconnected', () => {
  const page = load()
  page.set('ENGINES', ENGINES)
  page.set('SETTINGS', BROKEN_RUNTIME)
  assert.deepEqual(page.js("runtimeCardState('claude')"), {
    label: 'In use · not connected',
    detail: 'New runs other than Max (API) are blocked until you connect it below or select another runtime',
  })
  page.set('SETTINGS', { ...BROKEN_RUNTIME, cgm_max_api: NO_API })
  assert.equal(
    page.js("runtimeCardState('claude').detail"),
    'New runs are blocked until you connect it below or select another runtime',
  )
})
