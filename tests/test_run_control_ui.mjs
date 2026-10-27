// Stop, then Continue, in the console.
//
// Production, 2026-10-26: ■ Stop and ▶ Continue share one spot, and the next
// poll swapped them, so a second click meant for Stop continued the run 0.6 s
// later. A Stop now keeps its button there, inert and "Stopping…", until the
// run neither runs nor winds down on the server (run.stopping, job.stopping)
// and three seconds have passed; the run list's ■/▶ slot does the same.
//
// The real console functions run in vm contexts with small stubs, as in
// test_analyze_ask_ui.mjs.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]

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
  const at = consoleScript.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  let i = consoleScript.indexOf('(', at), depth = 0
  for (; i < consoleScript.length; i++) {
    if (consoleScript[i] === '(') depth++
    else if (consoleScript[i] === ')' && --depth === 0) break
  }
  return consoleScript.slice(at, scanEnd(consoleScript, i + 1))
}
function constant(name) {
  const at = consoleScript.indexOf(`const ${name}=`)
  assert.ok(at >= 0, 'missing const ' + name)
  return consoleScript.slice(at, scanEnd(consoleScript, at, { stopAtSemicolon: true }))
}
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
// A vm context with these constants and functions after `prelude`; `step` runs code in it.
function load(names, consts, prelude) {
  const sandbox = vm.createContext({ console })
  vm.runInContext([prelude, ...consts.map(constant), ...names.map(fn)].join('\n'), sandbox)
  return code => vm.runInContext(code, sandbox)
}

// The session bar's two buttons, and what a reader sees there.
const BAR = `
const button=text=>({textContent:text,disabled:false,names:new Set(),
  classList:{toggle(name,on){ on?this.owner.names.add(name):this.owner.names.delete(name); },contains(name){ return this.owner.names.has(name); }}});
const els={'sess-stop':button('■ Stop'),'sess-cont':button('▶ Continue')};
for(const el of Object.values(els))el.classList.owner=el;
const bar=()=>{ const stop=els['sess-stop'], cont=els['sess-cont'];
  return [stop.classList.contains('show')?(stop.disabled?'['+stop.textContent+']':stop.textContent):'',cont.classList.contains('show')?cont.textContent:''].filter(Boolean).join(' '); };`
// The session bar, a clock, and the server's answer to a Stop.
const SESSION = BAR + `
let clock=0, runId='r1', stopAnswer={ok:true,body:{ok:true,status:'stopped'}};
const Date={now:()=>clock};
const $=id=>els[id];
const calls=[];
const fetch=async(url,init)=>{ calls.push(init?.method+' '+url); if(stopAnswer instanceof Error)throw stopAnswer;
  return {ok:stopAnswer.ok,status:stopAnswer.ok?200:500,json:async()=>stopAnswer.body}; };
const setPill=(state,text)=>calls.push('pill '+text), startPolling=()=>calls.push('poll'), loadRuns=()=>calls.push('runs');`
const STOP_STATE = ['STOP_SETTLE_MS', 'STOPPING_RUNS']

test('after Stop, Continue takes the Stop button\'s spot only once the stop has settled', async () => {
  const step = load(['stopSettling', 'updateSessionButtons', 'stopRun'], STOP_STATE, SESSION)
  step("updateSessionButtons('running',false)")
  assert.equal(step('bar()'), '■ Stop')
  // The click turns ■ into an inert "Stopping…" at once, before the server answers.
  const stopping = step("stopRun('r1')")
  assert.equal(step('bar()'), '[Stopping…]')
  await stopping
  assert.deepEqual(plain(step('calls')), ['POST /api/runs/r1/stop', 'pill stopped', 'poll', 'runs'])
  // The next poll reads the run stopped while its worker still winds down: no Continue.
  step("clock=1600; updateSessionButtons('stopped',true)")
  assert.equal(step('bar()'), '[Stopping…]')
  // The worker is done, but three seconds have not passed: a second click still lands on nothing.
  step("clock=2500; updateSessionButtons('stopped',false)")
  assert.equal(step('bar()'), '[Stopping…]')
  step("clock=3100; updateSessionButtons('stopped',false)")
  assert.equal(step('bar()'), '▶ Continue')
  assert.equal(step("STOPPING_RUNS.size"), 0)
  // The Stop button is itself again for the next session.
  step("updateSessionButtons('running',false)")
  assert.equal(step('bar()'), '■ Stop')
  // Three seconds are not enough while the server still winds the run down.
  step("clock=10000; stopRun('r1')")
  step("clock=20000; updateSessionButtons('stopped',true)")
  assert.equal(step('bar()'), '[Stopping…]')
  step("updateSessionButtons('stopped',false)")
  assert.equal(step('bar()'), '▶ Continue')
})

test('a new session after the Stop gets a Stop of its own; a re-check keeps Stopping… until it ends', async () => {
  const step = load(['stopSettling', 'updateSessionButtons', 'stopRun'], STOP_STATE, SESSION)
  // A message sent after the Stop continues the run once the stopped worker is done.
  await step("stopRun('r1')")
  step("clock=1000; updateSessionButtons('running',false)")
  assert.equal(step('bar()'), '[Stopping…]')
  step("clock=3100; updateSessionButtons('running',false)")
  assert.equal(step('bar()'), '■ Stop')
  assert.equal(step('STOPPING_RUNS.size'), 0)
  // A re-check (or a question) of the run is recorded stopped only when its worker ends.
  step("stopAnswer={ok:true,body:{ok:true,status:'stopped',recheck:true}}; clock=10000")
  await step("stopRun('r1')")
  step("clock=20000; updateSessionButtons('running',false)")
  assert.equal(step('bar()'), '[Stopping…]')
  step("updateSessionButtons('finished',false)")
  assert.equal(step('bar()'), '▶ Continue')
})

test('a Stop that stopped nothing, or failed, offers Stop again at once', async () => {
  const step = load(['stopSettling', 'updateSessionButtons', 'stopRun'], STOP_STATE, SESSION)
  for (const answer of ["{ok:true,body:{ok:true,status:'not_running'}}", "{ok:false,body:{error:'boom'}}", "new Error('offline')"]) {
    step(`stopAnswer=${answer}; clock+=100`)
    await step("stopRun('r1')")
    assert.equal(step('STOPPING_RUNS.size'), 0, answer)
    step("updateSessionButtons('running',false)")
    assert.equal(step('bar()'), '■ Stop', answer)
    step("updateSessionButtons('finished',false)")
    assert.equal(step('bar()'), '▶ Continue', answer)
  }
})

test('a run stopped in another tab shows Stopping… until its worker is done', () => {
  const step = load(['stopSettling', 'updateSessionButtons'], STOP_STATE, SESSION)
  step("updateSessionButtons('stopped',true)")
  assert.equal(step('bar()'), '[Stopping…]')
  step("updateSessionButtons('stopped',false)")
  assert.equal(step('bar()'), '▶ Continue')
})

test('the poll goes on while a Stop settles, then stops and lists the run as stopped', async () => {
  const step = load(['tick', 'nextPollDelay', 'stopSettling'], ['POLL_MIN_MS', ...STOP_STATE], `
let clock=0, record=null, runId='r1', pollSig='', pollQuiet=0, pollDelay=8000, pollListedEnd=null, workspaceView='sandbox';
const Date={now:()=>clock};
const calls=[]; const $=()=>({textContent:''}); const setTimeout=()=>0;
const fetch=async()=>({ok:true,json:async()=>JSON.parse(JSON.stringify(record))});
const consoleRunSignature=run=>JSON.stringify(run);
const setPill=()=>{}, updateChatStatus=()=>{}, updateProblemStrip=()=>{}, updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{};
const applySandboxPreview=()=>{}, refreshProofPreview=async()=>{}, loadChat=()=>{}, loadRuns=()=>calls.push('runs'), updateSessionButtons=()=>{};`)
  const tick = async (record, clock) => { step(`clock=${clock}; record=${JSON.stringify(record)}`); return step('tick()') }
  step("STOPPING_RUNS.set('r1',{since:0,recorded:true})")
  const stopped = { run_id: 'r1', status: 'stopped', agents: [], totals: {} }
  assert.equal(await tick({ ...stopped, stopping: true }, 1600), true)
  assert.equal(step('pollDelay'), 2000)  // the next look comes soon, whatever the backoff
  assert.equal(await tick(stopped, 2500), true)
  assert.deepEqual(plain(step('calls')), [])
  // Settled: polling ends as for any run that ended, and the run list learns it.
  assert.equal(await tick(stopped, 3100), false)
  assert.deepEqual(plain(step('calls')), ['runs'])
  // A run stopped elsewhere is polled until its worker is done.
  assert.equal(await tick({ ...stopped, stopping: true }, 9000), true)
  assert.equal(await tick(stopped, 9100), false)
})

test('a Stop that settles while the proof refreshes still leaves ▶ Continue when polling ends', async () => {
  // updateChatStatus draws the bar before the tick waits for the proof's
  // refresh; the Stop can settle meanwhile, and the check after that wait ends
  // the poll. Nothing drew the bar again, so it kept an inert "Stopping…" and
  // no ▶ Continue (review, 2026-10-27; reproduced in Chromium).
  const step = load(['tick', 'nextPollDelay', 'stopSettling', 'updateSessionButtons'], ['POLL_MIN_MS', ...STOP_STATE], BAR + `
let clock=0, record=null, refreshMs=0, runId='r1', pollSig='', pollQuiet=0, pollDelay=2000, pollListedEnd=null, workspaceView='sandbox';
const Date={now:()=>clock};
const calls=[]; const $=id=>els[id]||{textContent:''}; const setTimeout=()=>0;
const fetch=async()=>({ok:true,json:async()=>JSON.parse(JSON.stringify(record))});
const consoleRunSignature=run=>JSON.stringify(run);
const setPill=()=>{}, updateProblemStrip=()=>{}, updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{};
// The bar as updateChatStatus draws it, and what it showed then.
const updateChatStatus=run=>{ updateSessionButtons(run.analyze?.recheck?.status==='running'?'running':run.status,!!run.stopping); calls.push('drawn '+bar()); };
// Fetching and drawing the proof takes refreshMs.
const applySandboxPreview=()=>{}, refreshProofPreview=async()=>{ clock+=refreshMs; }, loadChat=()=>{}, loadRuns=()=>calls.push('runs');`)
  // Stopped from this tab at 0 ms; at 2.8 s the worker is done and the record reads stopped.
  step("STOPPING_RUNS.set('r1',{since:0,recorded:true})")
  step("clock=2800; refreshMs=300; record={run_id:'r1',status:'stopped',agents:[],totals:{}}")
  assert.equal(await step('tick()'), false)
  // "Stopping…" at 2.8 s, settled by 3.1 s: the poll ends with ▶ Continue in the bar.
  assert.deepEqual(plain(step('calls')), ['drawn [Stopping…]', 'runs'])
  assert.equal(step('bar()'), '▶ Continue')
  assert.equal(step('STOPPING_RUNS.size'), 0)
})

test('the run list keeps ■ inert in the spot ▶ takes until the stop has settled', async () => {
  const step = load(['escapeHtml', 'loadRuns', 'stopSettling'], STOP_STATE, `
let clock=0, jobs=[];
const Date={now:()=>clock};
const RUN_KIND_BY_ID=new Map(); let runId='r2', CURRENT_USER={id:7};
const box={innerHTML:'',querySelectorAll:()=>[]}; const $=()=>box;
const fetch=async url=>({json:async()=>url==='/api/jobs'?{jobs}:{runs:[]}});
const syncIdlePill=()=>{}, runningRunCount=()=>0;
// The action button of run r1: what it is and whether it can be clicked.
const action=async()=>{ await loadRuns();
  const m=box.innerHTML.match(/<button class="run-act (stop|cont)" data-run="r1"[^>]*>/); return m?m[1]+(/ disabled>/.test(m[0])?' (inert)':''):'none'; };`)
  const set = (job, clock) => step(`clock=${clock}; jobs=${JSON.stringify([job])}`)
  const job = { run_id: 'r1', job_id: 'j1', engine: 'math_harness', status: 'running' }
  set(job, 0)
  assert.equal(await step('action()'), 'stop')
  // Stopped from the run list: the server records the stop, the worker winds down.
  step("STOPPING_RUNS.set('r1',{since:0,recorded:true})")
  set({ ...job, status: 'stopped', stopping: true }, 500)
  assert.equal(await step('action()'), 'stop (inert)')
  set({ ...job, status: 'stopped' }, 2000)
  assert.equal(await step('action()'), 'stop (inert)')
  set({ ...job, status: 'stopped' }, 3100)
  assert.equal(await step('action()'), 'cont')
  // Stopped in another tab: inert while the server winds it down, then ▶.
  set({ ...job, status: 'stopped', stopping: true }, 9000)
  assert.equal(await step('action()'), 'stop (inert)')
  set({ ...job, status: 'stopped' }, 9100)
  assert.equal(await step('action()'), 'cont')
})

test('a second click on the run list\'s ■ before it turns inert sends no second Stop', async () => {
  // The list draws ■ inert only once the server has answered the Stop. A
  // second click before that sent a second Stop, which took the first one's
  // place in STOPPING_RUNS and, answered "not_running" (the stopped worker had
  // ended), removed it: ▶ came within a second and a third click continued the
  // run (review, 2026-10-27; reproduced in Chromium). Checked with another run
  // shown in the session, then with this one.
  for (const shown of ['r2', 'r1']) {
    const step = load(['escapeHtml', 'loadRuns', 'stopSettling', 'updateSessionButtons', 'stopRun'], STOP_STATE, BAR + `
let clock=0, jobs=[], answers=[], runId=${JSON.stringify(shown)}, CURRENT_USER={id:7};
const Date={now:()=>clock};
const RUN_KIND_BY_ID=new Map();
const box={innerHTML:'',querySelectorAll:()=>[]}; const $=id=>id==='runs'?box:els[id];
// The server lists the run's job and answers each Stop in turn.
const posts=[];
const fetch=async(url,init)=>{ if(init?.method==='POST'){ posts.push(url); const answer=answers.shift(); return {ok:true,status:200,json:async()=>answer}; }
  return {ok:true,json:async()=>url==='/api/jobs'?{jobs}:{runs:[]}}; };
const setPill=()=>{}, startPolling=()=>{}, syncIdlePill=()=>{}, runningRunCount=()=>0;
// The action button of run r1: what it is and whether it can be clicked.
const action=async()=>{ await loadRuns();
  const m=box.innerHTML.match(/<button class="run-act (stop|cont)" data-run="r1"[^>]*>/); return m?m[1]+(/ disabled>/.test(m[0])?' (inert)':''):'none'; };`)
    const set = (job, clock) => step(`clock=${clock}; jobs=${JSON.stringify([job])}`)
    const job = { run_id: 'r1', job_id: 'j1', engine: 'claude', status: 'running' }
    set(job, 0)
    assert.equal(await step('action()'), 'stop', shown)
    // Two clicks on the live ■, 150 ms apart, before the first Stop's answer.
    step("answers=[{ok:true,status:'stopped',run_id:'r1',job_id:'j1'},{ok:true,status:'not_running',run_id:'r1'}]")
    const first = step("stopRun('r1')")
    const second = step("clock=150; stopRun('r1')")
    await Promise.all([first, second])
    await new Promise(resolve => setImmediate(resolve))  // the run list's redraw after each Stop
    // At 0.7 s the worker is done: ■ keeps the spot, inert, until three seconds have passed.
    set({ ...job, status: 'stopped' }, 700)
    assert.equal(await step('action()'), 'stop (inert)', shown)
    if (shown === 'r1') {
      step("updateSessionButtons('stopped',false)")
      assert.equal(step('bar()'), '[Stopping…]')
    }
    assert.deepEqual(plain(step('posts')), ['/api/runs/r1/stop'], shown)
    set({ ...job, status: 'stopped' }, 3100)
    assert.equal(await step('action()'), 'cont', shown)
    if (shown === 'r1') {
      step("updateSessionButtons('stopped',false)")
      assert.equal(step('bar()'), '▶ Continue')
    }
  }
})

test('the inert ■ in the run list swallows a second click instead of passing it to its run item', () => {
  // With pointer-events off, a click on the disabled ■ lands on what lies
  // under it, the run item, whose click opens that run (checked in Chromium);
  // a disabled button takes the click itself, and nothing happens.
  const styles = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(match => match[1]).join('\n')
  const rules = [...styles.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^}]*)\}/g)]
    .filter(([, selector]) => /\.run-act:disabled/.test(selector))
  assert.ok(rules.length > 0, 'a rule for the inert ■')
  for (const [, selector, body] of rules) {
    assert.doesNotMatch(body, /pointer-events\s*:\s*none/, selector.trim())
    assert.match(body, /cursor\s*:\s*default/, selector.trim())
  }
  // Hovered, it looks as inert as it is (no Stop highlight).
  assert.ok(rules.some(([, selector, body]) => /:disabled:hover/.test(selector) && /background\s*:\s*transparent/.test(body)))
})
