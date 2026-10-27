// The Ask panel of an Analyze run: questions about a finished analysis
// (POST /api/run/<id>/analysis/ask; run.analyze.asks and run.analyze.ask),
// the passages of its report and stored manuscript, "Ask about this result"
// in the Manuscript DAG, and answers whose result links open that DAG. The
// real functions of console.html and readability.js run in a vm context with
// small stubs (the rendering of answers through Markdown and the sanitizer is
// tested with the Readability ones, in test_readability_ui.mjs).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')

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
// A function or const of the console's script or of readability.js.
function fn(name) {
  const source = new RegExp(`(?:async )?function ${name}\\(`).test(consoleScript) ? consoleScript : readability
  const at = source.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  let i = source.indexOf('(', at), depth = 0
  for (; i < source.length; i++) {
    if (source[i] === '(') depth++
    else if (source[i] === ')' && --depth === 0) break
  }
  return source.slice(at, scanEnd(source, i + 1))
}
function constant(name) {
  const source = consoleScript.includes(`const ${name}=`) ? consoleScript : readability
  const at = source.indexOf(`const ${name}=`)
  assert.ok(at >= 0, 'missing const ' + name)
  return source.slice(at, scanEnd(source, at, { stopAtSemicolon: true }))
}
// Values cross the vm boundary as plain data.
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
// The console's math scanners: its prose helpers (analysisTextAccents) find math with them.
const MATH_SCANNERS = ['mathNextIndex', 'mathBetween', 'mathEnvironment', 'mathDollarLine', 'mathEscapedDollar', 'mathSpans', 'splitMath', 'holdMath']
const MATH_KINDS = ['ANALYZE_MATH_KINDS', 'ANALYZE_STATEMENT_MATH_KINDS']
const ACCENTS = ['ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS']
function source(names, consts, prelude) {
  return [prelude, ...consts.map(constant), ...MATH_KINDS.filter(name => !consts.includes(name)).map(constant),
    ...names.map(fn), ...MATH_SCANNERS.filter(name => !names.includes(name)).map(fn)].join('\n')
}
// A vm context with these functions and constants after `prelude`; `step` runs code in it.
function load(names, consts, prelude, context = {}) {
  const sandbox = vm.createContext({ console, URL, Date, ...context })
  vm.runInContext(source(names, consts, prelude), sandbox)
  return code => vm.runInContext(code, sandbox)
}

// The Ask panel's record helpers, for either kind of run.
const ASK_STATE = ['isAnalysisRun', 'isReadabilityRun', 'manuscriptAskKind', 'manuscriptAskPolicy', 'manuscriptAskWords', 'manuscriptAskOff', 'manuscriptAskRunOff',
  'manuscriptAskRunning', 'manuscriptAsks', 'manuscriptAskAllowance', 'manuscriptAskReason']
// A finished analysis with its report published.
const analysis = (extra = {}) => ({ run_id: 'a1', run_kind: 'analyze', engine: 'analyze', status: 'finished',
  sandbox_preview: { kind: 'analysis', has_report: true }, ...extra })
const DONE = { ask_id: 'a1q1', status: 'done', question: 'Is the gap real?', answer: 'No.', node_id: 'prop:density', view: 'report' }

test('an analysis keeps its questions in run.analyze: the answered ones, the one in flight and the one just sent', () => {
  const step = load(ASK_STATE, ['MANUSCRIPT_ASK'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='';
const viewerIsGuest=()=>false, analyzePolicy=()=>null, readabilityPolicy=()=>null;`)
  const asks = (record, sent) => {
    step('manuscriptAskSent.clear()')
    if (sent) step(`manuscriptAskSent.set('a1',${JSON.stringify(sent)})`)
    return plain(step(`manuscriptAsks(${JSON.stringify(record)}).map(item=>item.ask_id+':'+item.status)`))
  }
  const record = analysis({ analyze: { asks: [DONE, { ask_id: 'a1q2', status: 'failed', question: 'q' }, 'junk', null], ask: { ask_id: 'a1q3', status: 'running', question: 'And?' } },
    // A readable version's block on the same record is not this panel's.
    readability: { asks: [{ ask_id: 'rd1', status: 'done' }] } })
  assert.deepEqual(asks(record), ['a1q1:done', 'a1q2:failed', 'a1q3:running'])
  // An analysis is known by its engine or its preview too.
  assert.deepEqual(asks({ run_id: 'a1', engine: 'analyze', analyze: { asks: [DONE] } }), ['a1q1:done'])
  assert.deepEqual(asks({ run_id: 'a1', sandbox_preview: { kind: 'analysis' }, analyze: { asks: [DONE] } }), ['a1q1:done'])
  assert.deepEqual(plain(step(`[manuscriptAskKind(${JSON.stringify(record)}),manuscriptAskKind({run_kind:'readability'}),manuscriptAskKind({engine:'kimi'}),manuscriptAskKind(null)]`)),
    ['analyze', 'readability', '', ''])
  // Just sent: shown in flight until the record shows it (once) or lists it, or after 20 s without.
  const sent = { ask_id: 'a1q9', status: 'running', question: 'Why?', sent_at: Date.now() }
  assert.deepEqual(asks(analysis({ analyze: { asks: [DONE] } }), sent), ['a1q1:done', 'a1q9:running'])
  assert.deepEqual(asks(analysis({ analyze: { ask: { ask_id: 'a1q9', status: 'running' } } }), sent), ['a1q9:running'])
  assert.deepEqual(asks(analysis({ analyze: { asks: [{ ...DONE, ask_id: 'a1q9' }] } }), sent), ['a1q9:done'])
  assert.deepEqual(asks(analysis(), { ...sent, sent_at: Date.now() - 60000 }), [])
  // The record keeps the last 50.
  assert.equal(asks(analysis({ analyze: { asks: Array.from({ length: 60 }, (_, i) => ({ ...DONE, ask_id: 'q' + i })) } })).length, 50)
})

test('compose: an analysis says why it cannot be asked about yet, and a guest how many questions are left', () => {
  const step = load([...ASK_STATE, 'syncManuscriptAskCompose'], ['MANUSCRIPT_ASK'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='', guest=false, policy={enabled:true,reason:null,asks:{enabled:true,per_hour:30,per_run:null}};
const els={}; const $=id=>els[id]||(els[id]={id,disabled:false,title:'',textContent:''});
const viewerIsGuest=()=>guest, analyzePolicy=()=>policy, readabilityPolicy=()=>({asks:{per_run:9}});`)
  const compose = (record, { isGuest = false, policy = null, sending = '' } = {}) => {
    step(`guest=${isGuest}; manuscriptAskSending=${JSON.stringify(sending)}; policy=${JSON.stringify(policy || { enabled: true, reason: null, asks: { enabled: true, per_hour: 30, per_run: isGuest ? 3 : null } })}`)
    return plain(step(`(()=>{ const run=${JSON.stringify(record)}; syncManuscriptAskCompose(run,manuscriptAsks(run)); return [$('ask-send').disabled,$('ask-send').title,$('ask-hint').textContent,$('ask-input').disabled]; })()`))
  }
  const ready = [false, '', 'Enter to ask · Shift+Enter newline', false]
  assert.deepEqual(compose(analysis({ analyze: { asks: [DONE] } })), ready)
  // Not published: still running, or ended without a report (a stopped analysis with its report can be asked about).
  assert.deepEqual(compose(analysis({ status: 'running', sandbox_preview: { kind: 'analysis', has_report: false } })).slice(0, 2), [true, 'You can ask once the analysis is published.'])
  assert.deepEqual(compose(analysis({ status: 'failed', sandbox_preview: { kind: 'analysis', has_report: false } })).slice(0, 2), [true, 'This analysis published no report to ask about.'])
  assert.deepEqual(compose(analysis({ status: 'stopped' })), ready)
  // One question at a time (the record's, or one being sent from this run); a re-check of the analysis waits too.
  const busy = 'One question at a time — the answer is on its way.'
  assert.deepEqual(compose(analysis({ analyze: { ask: { ask_id: 'q', status: 'running' } } })).slice(0, 3), [true, busy, busy])
  assert.deepEqual(compose(analysis(), { sending: 'a1' }).slice(0, 2), [true, busy])
  assert.deepEqual(compose(analysis(), { sending: 'a0' }), ready)
  assert.deepEqual(compose(analysis({ analyze: { recheck: { node_id: 'lem:x', status: 'running' } } })).slice(0, 2), [true, 'A result is being re-checked — you can ask when it finishes.'])
  assert.deepEqual(compose(analysis({ analyze: { recheck: { node_id: 'lem:x', status: 'done' } } })), ready)
  // A guest: three questions per analysis, counted on the record (or by the questions listed).
  assert.deepEqual(compose(analysis({ analyze: { asks: [DONE], asks_started: 1 } }), { isGuest: true }), [false, '', '2 of 3 guest questions left · Enter to ask', false])
  assert.deepEqual(compose(analysis({ analyze: { asks: [DONE, DONE] } }), { isGuest: true })[2], '1 of 3 guest questions left · Enter to ask')
  const spent = 'Guests can ask 3 questions about an analysis — sign in to ask more.'
  assert.deepEqual(compose(analysis({ analyze: { asks: [DONE], asks_started: 3 } }), { isGuest: true }), [true, spent, spent, false])
  // Asking turned off: the policy's reason (an Analyze refusal), else the default words; nothing can be written.
  const off = { enabled: true, reason: null, asks: { enabled: false } }
  assert.deepEqual(compose(analysis({ analyze: { asks: [DONE] } }), { policy: off }),
    [true, 'Questions about analyses are turned off on this site.', 'Questions about analyses are turned off on this site.', true])
  assert.deepEqual(compose(analysis(), { policy: { enabled: false, reason: 'Analyze needs a signed-in account on this site.', asks: { enabled: false } } }).slice(1, 4),
    ['Analyze needs a signed-in account on this site.', 'Analyze needs a signed-in account on this site.', true])
  // Without the asks object (an older listing) asking stays on.
  assert.deepEqual(compose(analysis(), { policy: { enabled: true } }), ready)
  // The DAG's button leaves out what passes by itself (now: false).
  const reason = (record, now) => plain(step(`(()=>{ const run=${JSON.stringify(record)}; return manuscriptAskReason(run,manuscriptAsks(run),{now:${now}}); })()`))
  step('guest=false')
  assert.equal(reason(analysis({ analyze: { ask: { ask_id: 'q', status: 'running' } } }), false), '')
  assert.equal(reason(analysis({ analyze: { recheck: { status: 'running' } } }), false), '')
  assert.equal(reason(analysis({ status: 'running' }), false), 'You can ask once the analysis is published.')
  // A readable version keeps its own words and block.
  assert.deepEqual(compose({ run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { has_readable: true }, readability: { asks_started: 9 } }, { isGuest: true }).slice(0, 2),
    [true, 'Guests can ask 9 questions about a readable version — sign in to ask more.'])
})

test('asking about an analysis: the request names the question, a passage of the report or of the manuscript, and the result', async () => {
  const step = load([...ASK_STATE, 'shownAskKind', 'readabilitySelectionView', 'manuscriptSelectionView', 'renderManuscriptAskContext', 'manuscriptAskCut',
    'askAboutSelection', 'askAboutNode', 'sendManuscriptAsk', 'dagDetailText', 'analysisTextAccents', 'analysisResultName'], ['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS', ...ACCENTS], `
const calls=[], posts=[], els={};
const $=id=>els[id]||(els[id]={id,value:'',textContent:'',disabled:false,hidden:false,kids:[],focus(){ calls.push('focus '+id); },replaceChildren(...kids){ this.kids=kids; }});
let manuscriptAskContext=null, manuscriptAskSending='', readabilityTab='readable', runId='a1', workspaceView='dag', analysisSourceShown=false, analysisSourceRunId='';
const manuscriptAskSent=new Map();
let lastSandboxRun={run_id:'a1',run_kind:'analyze',status:'finished',sandbox_preview:{kind:'analysis',has_report:true},analyze:{asks:[]}};
let reply={ok:true,status:202,body:{ok:true,status:'asking',run_id:'a1',ask_id:'feedfeed0001',job_id:'j1'}}, policy={asks:{enabled:true}};
const fetch=async(url,options)=>{ posts.push([url,options.method,JSON.parse(options.body)]); return {ok:reply.ok,status:reply.status,json:async()=>reply.body}; };
const window={getSelection:()=>({removeAllRanges(){ calls.push('unselect'); }})};
const startPolling=()=>calls.push('poll'), renderManuscriptAsks=()=>calls.push('drawn'), syncManuscriptAskCompose=()=>{}, limitMessage=message=>message+' (limit)';
const setWorkspaceView=view=>{ workspaceView=view; calls.push('view '+view); };
const analyzeEl=(tag,cls,text)=>({tag,cls,text,onclick:null,setAttribute(){}}), paperMapCardTitle=()=>'(a paper map title)';
const analyzePolicy=()=>policy, readabilityPolicy=()=>null, viewerIsGuest=()=>false;
const document={body:{classList:{contains:name=>name==='analysis-run'}}};
const state=()=>({calls:calls.splice(0),posts:posts.splice(0),context:manuscriptAskContext,shown:$('ask-context').hidden?'':$('ask-context').kids[0]?.text,input:$('ask-input').value,status:$('ask-status').textContent});`)
  const state = () => plain(step('state()'))
  // A passage of the published report: as written, with its view.
  assert.equal(step('manuscriptSelectionView()'), 'report')
  step("askAboutSelection('  the gap is in Step 2 of $x<y$  ')")
  assert.deepEqual(state(), { calls: ['unselect', 'focus ask-input'], posts: [], context: { selection: 'the gap is in Step 2 of $x<y$', view: 'report' },
    shown: 'About this passage of the report: “the gap is in Step 2 of $x<y$”', input: '', status: '' })
  step("$('ask-input').value='  Is it real?  '")
  await step('sendManuscriptAsk()')
  let after = state()
  assert.deepEqual(after.posts, [['/api/run/a1/analysis/ask', 'POST', { question: 'Is it real?', selection: 'the gap is in Step 2 of $x<y$', view: 'report' }]])
  // Sent: the box is cleared, the question shows in flight, and the run is polled for its answer.
  assert.deepEqual([after.context, after.shown, after.input, after.calls], [null, '', '', ['poll', 'drawn']])
  assert.deepEqual(plain(step("manuscriptAsks(lastSandboxRun).map(item=>[item.ask_id,item.status,item.view,item.selection])")),
    [['feedfeed0001', 'running', 'report', 'the gap is in Step 2 of $x<y$']])
  // A passage of the stored manuscript (its own run's stored copy shown): the source view.
  step("manuscriptAskSent.clear(); analysisSourceShown=true; analysisSourceRunId='a1'; askAboutSelection('\\\\[ K_d=\\\\frac{3D_X}{\\\\eta} \\\\]'); $('ask-input').value='What is K_d?'")
  assert.equal(state().shown, 'About this passage of the manuscript: “\\[ K_d=\\frac{3D_X}{\\eta} \\]”')
  await step('sendManuscriptAsk()')
  assert.deepEqual(state().posts[0][2], { question: 'What is K_d?', selection: '\\[ K_d=\\frac{3D_X}{\\eta} \\]', view: 'source' })
  // Another run's stored copy says nothing about this one.
  step("manuscriptAskSent.clear(); analysisSourceRunId='a0'")
  assert.equal(step('manuscriptSelectionView()'), 'report')
  // A result from the Manuscript DAG: its id and printed name; the Sandbox opens on the question.
  step("analysisSourceShown=false; askAboutNode({id:'lem:residues',kind:'lemma',label:'Lemma 4.1 (Residues)',meta:{number:'4.1',env:'lemma'}}); $('ask-input').value='Why is it needed?'")
  after = state()
  assert.deepEqual([after.calls, after.context, after.shown], [['view sandbox', 'focus ask-input'], { node_id: 'lem:residues', label: 'Lemma 4.1', view: 'report' }, 'About Lemma 4.1'])
  await step('sendManuscriptAsk()')
  assert.deepEqual(state().posts, [['/api/run/a1/analysis/ask', 'POST', { question: 'Why is it needed?', selection: '', view: 'report', node_id: 'lem:residues' }]])
  // A refusal is shown and keeps the question and its context; a limit says when to try again.
  step("manuscriptAskSent.clear(); askAboutNode({id:'thm:main',label:'Theorem 1.1',meta:{number:'1.1'}}); $('ask-input').value='q'; reply={ok:false,status:409,body:{error:'Wait for the analysis (or its re-check, or the question before) to finish'}}")
  await step('sendManuscriptAsk()')
  after = state()
  assert.deepEqual([after.status, after.input, after.context.node_id], ['✗ Wait for the analysis (or its re-check, or the question before) to finish', 'q', 'thm:main'])
  step("reply={ok:false,status:403,body:{error:'Guests can ask 3 questions about an analysis; sign in to ask more',login_required:true}}")
  await step('sendManuscriptAsk()')
  assert.equal(state().status, '✗ Guests can ask 3 questions about an analysis; sign in to ask more')
  step("reply={ok:false,status:429,body:{error:'You can ask 30 questions per hour; please try again later'}}")
  await step('sendManuscriptAsk()')
  assert.equal(state().status, '✗ You can ask 30 questions per hour; please try again later (limit)')
  // Nothing is sent while this run's question is on its way, while the button is off, or for a run that is no analysis.
  step("reply={ok:true,status:202,body:{ok:true,ask_id:'x'}}; manuscriptAskSending='a1'"); await step('sendManuscriptAsk()')
  step("manuscriptAskSending=''; $('ask-send').disabled=true"); await step('sendManuscriptAsk()')
  step("$('ask-send').disabled=false; lastSandboxRun={run_id:'a1',engine:'kimi'}"); await step('sendManuscriptAsk()')
  assert.deepEqual(state().posts, [])
  // A passage selected while asking is off, or before the report is published, is not offered.
  step("lastSandboxRun={run_id:'a1',run_kind:'analyze',status:'running',sandbox_preview:{kind:'analysis',has_report:false}}")
  assert.equal(step('manuscriptSelectionView()'), '')
  step("lastSandboxRun.status='finished'; lastSandboxRun.sandbox_preview.has_report=true; policy={asks:{enabled:false}}")
  assert.equal(step('manuscriptSelectionView()'), '')
})

test('the selection pop-up asks about a passage of an analysis instead of quoting it into the hidden chat', () => {
  // Its words follow the run kind (applyRunKindView), and its click goes to the Ask panel for both manuscript kinds.
  assert.match(fn('applyRunKindView'), /\$\('sel-pop'\)\.textContent=readability\|\|analysis\?'Ask about this':'💬 Add to chat';/)
  const asks = load(['selectionAsks'], [], `let classes=new Set(); const document={body:{classList:{contains:name=>classes.has(name)}}};`)
  assert.deepEqual(plain(asks("[['analysis-run'],['readability-run'],[]].map(list=>{ classes=new Set(list); return selectionAsks(); })")), [true, true, false])
  assert.match(consoleScript, /if\(selectionAsks\(\)\)\{ selPop\.style\.display='none'; askAboutSelection\(selText\); return; \}/)
  assert.match(consoleScript, /const askable=!asks\|\|readabilityUiReady\(\)&&!!manuscriptSelectionView\(\);/)
})

test('the Manuscript DAG offers "Ask about this result": a run-change button that opens the Sandbox with the result', () => {
  const actions = load(['analyzeEl', 'analysisDetailActions', ...ASK_STATE], ['ANALYZE_VERDICT_LABEL', 'MANUSCRIPT_ASK'], `
const calls=[];
const document={createElement:tag=>({tag,className:'',textContent:'',title:'',disabled:false,type:'',kids:[],attrs:{},setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}})};
let guest=false, record=null, ready=true, policy={asks:{enabled:true,per_run:3}};
const viewerIsGuest=()=>guest, analyzePolicy=()=>policy, readabilityPolicy=()=>null, readabilityUiReady=()=>ready;
const analysisRunState=()=>({run:record,running:record?.status==='running',recheck:record?.analyze?.recheck||null});
const manuscriptAskSent=new Map(); let manuscriptAskSending='';
const dagDetailText=v=>String(v||''), analysisSourceFile=()=>'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{};
const askAboutNode=node=>calls.push('ask '+node.id);`)
  const button = (run, { isGuest = false, loaded = true, asksOn = true } = {}) => {
    actions(`record=${JSON.stringify(run)}; guest=${isGuest}; ready=${loaded}; policy={asks:{enabled:${asksOn},per_run:3}}`)
    return plain(actions(`(()=>{ const node={id:'lem:residues',label:'Lemma 4.1'}; const row=analysisDetailActions(node,{checkable:true},{}).kids[1];
      const ask=row.kids.find(kid=>kid.textContent==='Ask about this result'); ask.onclick(); return {order:row.kids.map(kid=>kid.textContent),cls:ask.className,type:ask.type,disabled:ask.disabled,title:ask.title,calls:calls.splice(0)}; })()`))
  }
  const fresh = button(analysis({ analyze: { asks: [] } }))
  assert.deepEqual(fresh, { order: ['↻ Re-check', '∎ Formalize statement', 'Ask about this result'], cls: 'run-change', type: 'button', disabled: false,
    title: 'Ask a question about this result in the Sandbox (answered by a model — not a certificate)', calls: ['ask lem:residues'] })
  // A question already on its way does not stop a new one being written.
  assert.equal(button(analysis({ analyze: { ask: { ask_id: 'q', status: 'running' } } })).disabled, false)
  // Off, unpublished, a guest's questions spent, or the Ask panel not loaded: disabled, saying why.
  assert.deepEqual(pick(button(analysis(), { asksOn: false })), [true, 'Questions about analyses are turned off on this site.'])
  assert.deepEqual(pick(button(analysis({ status: 'running' }))), [true, 'You can ask once the analysis is published.'])
  assert.deepEqual(pick(button(analysis({ analyze: { asks_started: 3 } }), { isGuest: true })), [true, 'Guests can ask 3 questions about an analysis — sign in to ask more.'])
  assert.deepEqual(pick(button(analysis({ analyze: { asks_started: 2 } }), { isGuest: true })), [false, fresh.title])
  assert.deepEqual(pick(button(analysis(), { loaded: false })), [true, 'The Ask panel has not loaded — reload the page to ask.'])
  // Before the record has arrived it waits for nothing.
  assert.equal(button(null).disabled, false)
  function pick(out) { return [out.disabled, out.title] }
  // Hidden on a run this account may only read, with Re-check and Formalize.
  assert.match(html, /html\.read-only-run \.run-change \{ display: none !important; \}/)
})

test('results are named as the manuscript prints them ("Lemma 3.2"), by lower-case id', () => {
  const name = load(['dagDetailText', 'analysisTextAccents', 'analysisResultName', 'analysisResultNames'], ACCENTS, '')
  const of = node => name(`analysisResultName(${JSON.stringify(node)})`)
  assert.equal(of({ id: 'lem:distinct', kind: 'lemma', label: 'Lemma 2.2 (Removing repetitions)', meta: { number: '2.2', env: 'lemma' } }), 'Lemma 2.2')
  assert.equal(of({ id: 'thm:main', kind: 'theorem', label: 'Theorem 1.1', meta: { number: '1.1', env: 'theorem' } }), 'Theorem 1.1')
  assert.equal(of({ id: 'thm:a', label: 'Theorem A.1 (Main)', meta: { number: 'A.1' } }), 'Theorem A.1')
  // The number not where the label has it (or only as part of another number): the kind and number.
  assert.equal(of({ id: 'lem:x', kind: 'lemma', label: 'Lemma 312', meta: { number: '3.2', env: 'lemma' } }), 'Lemma 3.2')
  assert.equal(of({ id: 'thm:x', kind: 'theorem', label: 'Main theorem', meta: { number: '3' } }), 'Theorem 3')
  assert.equal(of({ id: 'prop:x', kind: 'proposition', label: 'Proposition 2.1.', meta: { number: '2.1', env: 'proposition' } }), 'Proposition 2.1')
  assert.equal(of({ id: 'cor:x', kind: 'main_result', meta: { number: '4' } }), 'Main result 4')
  // No number: the label (accents as letters), else the id.
  assert.equal(of({ id: 'def:valid', kind: 'definition', label: 'Definition (Valid diagrams)', meta: {} }), 'Definition (Valid diagrams)')
  assert.equal(of({ id: 'lem:e', label: 'Erd\\H{o}s\'s  lemma' }), 'Erdős\'s lemma')
  assert.equal(of({ id: 'n7' }), 'n7')
  assert.equal(of({ id: 'n8', label: { toString: () => 'x' }, meta: 'junk' }), 'n8')
  assert.deepEqual(plain(name(`[...analysisResultNames({nodes:[{id:'Lem:A',label:'Lemma 1',meta:{number:'1'}},{id:'lem:a',label:'Lemma 9',meta:{number:'9'}},null,{id:''},{id:'def:b',label:'Definition 2.1',meta:{number:'2.1'}}]})]`)),
    [['lem:a', 'Lemma 1'], ['def:b', 'Definition 2.1']])
  assert.deepEqual(plain(name('[...analysisResultNames(null),...analysisResultNames({nodes:"junk"})]')), [])
})

test('a result link opens the Manuscript DAG at that result: at once when its graph is drawn, else once it is', () => {
  const jump = load(['analysisJumpTo'], [], `
const calls=[], stage={};
const $=id=>id==='dag-stage'?stage:null;
let runId='a1', dagMode='paper-map', workspaceView='sandbox', dagView=null, dagDisplayedRunId='', dagDisplayedMode='';
const setDagMode=(mode,options)=>{ dagMode=mode; calls.push('mode '+mode+(options.refresh?' (refresh)':'')); };
const setWorkspaceView=view=>{ workspaceView=view; calls.push('view '+view); }, refreshDagView=()=>calls.push('refresh');
const showAnalysisResult=node=>calls.push('show '+node.id);
const out=()=>({calls:calls.splice(0),pending:stage._pendingSelect||null});`)
  const go = (setup, id) => { jump(setup); jump(`analysisJumpTo(${JSON.stringify(id)})`); return plain(jump('out()')) }
  // Not drawn (another mode, the Sandbox shown): the Manuscript DAG opens and the stage holds the result for renderDag.
  assert.deepEqual(go("dagMode='paper-map'; workspaceView='sandbox'", 'LEM:Residues'),
    { calls: ['mode analysis', 'view dag'], pending: { runId: 'a1', mode: 'analysis', id: 'lem:residues' } })
  // In the DAG view already, in another mode: reloaded in Manuscript mode.
  assert.deepEqual(go("dagMode='informal'; workspaceView='dag'", 'lem:residues').calls, ['mode analysis', 'refresh'])
  // Manuscript mode, but this run's graph not drawn (another run's, or none yet).
  assert.deepEqual(go("dagMode='analysis'; workspaceView='dag'; dagView={runId:'a0',mode:'analysis',graph:{nodes:[]}}", 'lem:residues').calls, ['refresh'])
  // Drawn: selected at once (the Sandbox gives way to the DAG first, so that its card can be shown).
  const drawn = "dagMode='analysis'; dagView={runId:'a1',mode:'analysis',graph:{nodes:[{id:'thm:main'},{id:'lem:residues'}]}}; dagDisplayedRunId='a1'; dagDisplayedMode='analysis';"
  assert.deepEqual(go(drawn + "workspaceView='sandbox'", 'lem:residues'), { calls: ['view dag', 'show lem:residues'], pending: null })
  assert.deepEqual(go(drawn + "workspaceView='dag'", 'Lem:Residues'), { calls: ['show lem:residues'], pending: null })
  assert.deepEqual(go(drawn, 'lem:gone').calls, [])
  // Drawn for this run, but the view shows another mode's graph since: not drawn.
  assert.deepEqual(go(drawn + "dagDisplayedMode='informal'; workspaceView='dag'", 'lem:residues').calls, ['refresh'])
  assert.deepEqual(go("runId=''", 'lem:residues').calls, [])
  jump("runId='a1'")
  assert.deepEqual(go('', '').calls, [])
})

test('showing a result opens its details without toggling them closed, and shows a card its filter hid', () => {
  const show = load(['analysisVerdict', 'showAnalysisResult'], ['ANALYZE_VERDICTS'], `
const calls=[]; let dagSelectedNodeId=null, analysisHidden=new Set(['gap']);
const card={scrollIntoView(options){ calls.push('scroll '+options.block); },focus(options){ calls.push('focus'+(options.preventScroll?' (no scroll)':'')); }};
const document={getElementById:id=>id==='dagn-1'?card:null};
const dagView={view:{role:'analysis'},graph:{nodes:[]},payload:{},domIdByNodeId:new Map([['lem:a','dagn-1'],['lem:b','dagn-2']])};
const selectDagNode=node=>{ calls.push('select '+node.id); dagSelectedNodeId=String(node.id); };
const renderAnalysisFilters=()=>calls.push('filters'), analysisCoverageText=()=>'';
const out=()=>({calls:calls.splice(0),hidden:[...analysisHidden]});`)
  show("showAnalysisResult({id:'lem:a',meta:{verdict:'checked'}})")
  assert.deepEqual(plain(show('out()')), { calls: ['select lem:a', 'scroll center', 'focus (no scroll)'], hidden: ['gap'] })
  // Selected already: kept open (selectDagNode would close it).
  show("showAnalysisResult({id:'lem:a',meta:{verdict:'checked'}})")
  assert.deepEqual(plain(show('out()')).calls, ['scroll center', 'focus (no scroll)'])
  // Its verdict filtered out: shown again first.
  show("showAnalysisResult({id:'lem:b',meta:{verdict:'gap'}})")
  assert.deepEqual(plain(show('out()')), { calls: ['filters', 'select lem:b'], hidden: [] })
})

test('renderDag selects the result a link waits for, once this run\'s graph is drawn in Manuscript mode', () => {
  // The real renderDag, with its layout, card and edge helpers reduced to stubs.
  const render = load(['renderDag'], [], `
const calls=[], elements={};
const el=id=>elements[id]||(elements[id]={id,style:{},classList:{toggle(){},add(){},remove(){}},offsetHeight:60,clientWidth:900,innerHTML:'',setAttribute(){},querySelectorAll:()=>[],insertAdjacentHTML(){}});
const $=el, document={getElementById:id=>el(id),querySelectorAll:()=>[]};
let runId='a1', dagMode='analysis', dagEngineView='workflow', dagRunKey='', dagDisplayedRunId='', dagDisplayedMode='', dagDisplayedEngineView='', lastSandboxRun=null, dagView=null, dagSelectedNodeId=null;
const setDagObservation=()=>{}, dagStatus=()=>{}, analysisDagStatusText=()=>'', analysisRefLabels=()=>new Map(), analysisKatexMacros=()=>({}), analysisCoverageText=()=>'';
const dagLayers=graph=>[graph.nodes], dagEdgeRoutes=()=>[], dagEdgeSvg=()=>({html:'',drawn:[]}), renderDagLegend=()=>{}, renderAnalysisFilters=()=>{}, syncDagRebuild=()=>{};
const dagNodeVisual=()=>({badge:'lemma',color:'#64748b',classes:[],chips:[],status:null,score:null}), escapeHtml=String, analysisProse=text=>text, analysisTextAccents=text=>text;
const dagCardTitle=node=>node.label, dagFormalCheckHtml=()=>'', renderMathIn=()=>{};
const selectDagNode=node=>{ calls.push('select '+node.id); dagSelectedNodeId=String(node.id); };
const closeDagDetail=()=>{ calls.push('close'); dagSelectedNodeId=null; };
const showAnalysisResult=node=>{ calls.push('show '+node.id); dagSelectedNodeId=String(node.id); };
let generation=0;
const draw=pending=>{ if(pending!==undefined)$('dag-stage')._pendingSelect=pending;
  renderDag({kind:'analysis',status:'ready',generated_at:'g'+(++generation),graph:{nodes:[{id:'thm:main',label:'Theorem 1.1'},{id:'lem:b',label:'Lemma 2'}],edges:[]}});
  return {calls:calls.splice(0),selected:dagSelectedNodeId,pending:$('dag-stage')._pendingSelect}; };`)
  const draw = pending => plain(render(`draw(${pending === undefined ? '' : JSON.stringify(pending)})`))
  // The first drawing of the graph shows the result the link asked for, and the stage lets go of it.
  assert.deepEqual(draw({ runId: 'a1', mode: 'analysis', id: 'lem:b' }), { calls: ['show lem:b'], selected: 'lem:b', pending: null })
  // Drawn again (a re-check changed verdicts): the selection is kept as before.
  assert.deepEqual(draw(), { calls: ['select lem:b'], selected: 'lem:b', pending: null })
  // A link for another run, another mode or a result this graph lacks is dropped; the kept selection stays.
  assert.deepEqual(draw({ runId: 'a0', mode: 'analysis', id: 'thm:main' }), { calls: ['select lem:b'], selected: 'lem:b', pending: null })
  assert.deepEqual(draw({ runId: 'a1', mode: 'paper-map', id: 'thm:main' }), { calls: ['select lem:b'], selected: 'lem:b', pending: null })
  assert.deepEqual(draw({ runId: 'a1', mode: 'analysis', id: 'lem:gone' }), { calls: ['select lem:b'], selected: 'lem:b', pending: null })
  // Case does not matter (answers' links are read in lower case).
  assert.deepEqual(draw({ runId: 'a1', mode: 'analysis', id: 'thm:main' }).calls, ['show thm:main'])
})

test('the console polls an analysis while a question is answered, says so, and lists it as answering', async () => {
  // The run's signature follows the question in flight and the answers.
  const signature = load(['consoleRunSignature'], [], '')
  const sig = record => signature(`consoleRunSignature(${JSON.stringify(record)})`)
  const finished = analysis({ updated_at: 1, analyze: { asks: [DONE] } })
  assert.notEqual(sig(finished), sig({ ...finished, analyze: { ...finished.analyze, ask: { ask_id: 'q', status: 'running' } } }))
  assert.notEqual(sig(finished), sig({ ...finished, analyze: { asks: [DONE, DONE] } }))
  // Polling goes on after the analysis while its question runs, and stops once it is answered.
  const poll = load(['tick', 'nextPollDelay', 'stopSettling'], ['POLL_MIN_MS', 'STOP_SETTLE_MS', 'STOPPING_RUNS'], `
let record=null, runId='a1', pollSig='', pollQuiet=0, pollDelay=2000, pollListedEnd=null, workspaceView='sandbox';
const calls=[]; const $=()=>({textContent:''}); const setTimeout=()=>0;
const fetch=async()=>({ok:true,json:async()=>JSON.parse(JSON.stringify(record))});
const consoleRunSignature=run=>JSON.stringify(run);
const setPill=()=>{}, updateChatStatus=()=>{}, updateProblemStrip=()=>{}, updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{};
const applySandboxPreview=run=>calls.push('preview'), refreshProofPreview=async()=>{}, loadChat=()=>{}, loadRuns=()=>calls.push('runs'), updateSessionButtons=()=>{};`)
  const tick = async record => { poll(`record=${JSON.stringify(record)}`); return poll('tick()') }
  assert.equal(await tick({ ...finished, analyze: { ask: { ask_id: 'q', status: 'running' } } }), true)
  assert.equal(await tick(finished), false)
  // The status line says a question is being answered, with the question.
  const status = load(['isAnalysisRun', 'isReadabilityRun', 'updateChatStatus'], [], `
const els={}; const $=id=>els[id]||(els[id]={id,textContent:'',title:'',className:''});
const STAGE_LABELS={}, ANALYZE_STAGES=[], READABILITY_STAGES=[];
const syncAnalyzeRunUi=()=>{}, updateLiveChips=()=>{}, updateSessionButtons=()=>{}, updateProcLog=()=>{}, renderAnalysisStages=()=>{}, analysisStageStates=()=>[];
const analysisProgressDetail=()=>'', analysisRunProgress=()=>[];
let lastProcText='';`)
  const shown = record => plain(status(`(()=>{ updateChatStatus(${JSON.stringify(record)}); return [$('chat-stage').textContent,$('chat-detail').textContent]; })()`))
  assert.deepEqual(shown({ ...finished, analyze: { ask: { ask_id: 'q', status: 'running', question: 'What role does r play here?' } } }), ['Answering…', 'What role does r play here?'])
  assert.deepEqual(shown(finished), ['Finished', ''])
  // The run list says the analysis is answering a question (its question is no run of its own).
  const replies = {
    '/api/jobs': { jobs: [{ run_id: 'a1', job_id: 'j1', engine: 'analyze', run_kind: 'analyze', status: 'finished', problem_preview: 'Analyze · arXiv:2401.00001' },
      { run_id: 'a1', job_id: 'j2', kind: 'ask', engine: 'analyze', run_kind: 'analyze', status: 'running', problem_preview: 'Ask · why?' }] },
    '/api/runs': { runs: [{ run_id: 'a1', run_kind: 'analyze', engine: 'analyze', status: 'finished', problem_preview: 'Analysis · Short proofs' }] },
  }
  const list = load(['escapeHtml', 'loadRuns', 'stopSettling'], ['STOP_SETTLE_MS', 'STOPPING_RUNS'], `
const RUN_KIND_BY_ID=new Map(); let runId='a1', CURRENT_USER={id:4};
const box={innerHTML:'',querySelectorAll:()=>[]}; const $=()=>box;
const fetch=async url=>({json:async()=>replies[url]});
const syncIdlePill=()=>{}, runningRunCount=()=>0;`, { replies })
  await list('loadRuns()')
  const listed = list('box.innerHTML')
  assert.match(listed, /<div class="st"><span class="run-kind">Analyze<\/span>answering a question<\/div>/)
  assert.doesNotMatch(listed, /Ask · why\?/)
})

test('another run starts with an empty Ask panel; the same run keeps what is being written', () => {
  const step = load(['isAnalysisRun', 'isReadabilityRun', 'manuscriptAskKind', 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks'], ['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], `
const els={}; const $=id=>els[id]||(els[id]={id,value:'',textContent:'',hidden:true,kids:[],replaceChildren(...kids){ this.kids=kids; },querySelectorAll:()=>[],scrollHeight:0,scrollTop:0,clientHeight:0});
let manuscriptAskContext=null, manuscriptAskRunId='', runId='a1';
const manuscriptAsks=()=>[], manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), manuscriptAskItem=()=>({}), syncManuscriptAskCompose=()=>{}, syncManuscriptAskButtons=()=>{}, renderMathIn=()=>{};
const analyzeEl=(tag,cls,text)=>({tag,cls,text,setAttribute(){}});
const write=()=>{ $('ask-input').value='A draft'; $('ask-status').textContent='✗ refused'; manuscriptAskContext={node_id:'lem:a',label:'Lemma 1',view:'report'}; renderManuscriptAskContext(); };
const state=()=>[$('ask-input').value,$('ask-status').textContent,manuscriptAskContext&&manuscriptAskContext.node_id,$('ask-context').hidden];`)
  step(`renderManuscriptAsks(${JSON.stringify(analysis())}); write()`)
  assert.deepEqual(plain(step('state()')), ['A draft', '✗ refused', 'lem:a', false])
  // The same run drawn again (a poll) keeps the draft.
  step(`renderManuscriptAsks(${JSON.stringify(analysis({ analyze: { asks: [DONE] } }))})`)
  assert.deepEqual(plain(step('state()')), ['A draft', '✗ refused', 'lem:a', false])
  // Another analysis shown, or a readable version: empty.
  step(`runId='a2'; renderManuscriptAsks(${JSON.stringify(analysis({ run_id: 'a2' }))})`)
  assert.deepEqual(plain(step('state()')), ['', '', null, true])
  step('write()'); step(`runId='r1'; renderManuscriptAsks({run_id:'r1',run_kind:'readability',sandbox_preview:{has_readable:true}})`)
  assert.deepEqual(plain(step('state()')), ['', '', null, true])
  // A proof run has no Ask panel: nothing is drawn, and nothing is reset by drawing it...
  step('write()'); step(`runId='p1'; renderManuscriptAsks({run_id:'p1',engine:'kimi'})`)
  assert.deepEqual(plain(step('state()')), ['A draft', '✗ refused', 'lem:a', false])
  // ...but choosing another run does (selectRun, while readability.js is loaded).
  step("resetManuscriptAsk('p1')")
  assert.deepEqual(plain(step('state()')), ['', '', null, true])
  assert.match(fn('selectRun'), /if\(runId!==rid\)\{[^}]*if\(typeof resetManuscriptAsk==='function'\)resetManuscriptAsk\(rid\);\n\s*\}/)
  // A question still being sent stays its own run's: the sending flag holds that run's id.
  assert.match(fn('manuscriptAskReason'), /manuscriptAskSending===String\(run\.run_id\|\|''\)/)
})

test('an analysis\'s right panel is the Ask panel, with its Progress notes kept above the conversation', () => {
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  assert.match(css, /\.analysis-run \.chat-msgs \{ flex: 0 1 auto; max-height: min\(22%, 150px\);[^}]*border-bottom: 1px solid var\(--border\); \}/)
  assert.match(css, /\.analysis-run \.chat-msgs:empty \{ display: none; \}/)
  // The proof chat's compose box stays hidden; the Ask panel's is shown.
  assert.match(css, /\.analysis-run #proof-edit-btn, \.analysis-run \.chat-compose,/)
  assert.match(constant('ANALYZE_ASK_NOTE'), /not a certificate\. An analysis is never continued; re-check a result in the DAG\./)
  // The answer, once loaded, is drawn again with the graph's names (and whichever view the Sandbox shows).
  const preview = fn('applyAnalysisPreview')
  assert.match(preview, /if\(readabilityUiReady\(\)\)renderManuscriptAsks\(run\);[\s\S]*if\(analysisSourceShown&&analysisSourceRunId===rid\)return true;/)
  assert.match(preview, /if\(readabilityUiReady\(\)&&lastSandboxRun\?\.run_id===rid\)renderManuscriptAsks\(lastSandboxRun\);/)
})

test('choosing another run empties the conversation and holds the compose box until that run\'s record is drawn', () => {
  // The real selectRun and Ask panel; the rest of the console reduced to stubs.
  const step = load(['selectRun', ...ASK_STATE, 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks', 'syncManuscriptAskCompose'],
    ['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], `
const els={};
const $=id=>els[id]||(els[id]={id,value:'',textContent:'',title:'',disabled:false,hidden:false,kids:[],scrollTop:0,scrollHeight:0,clientHeight:0,
  replaceChildren(...kids){ this.kids=kids; },querySelectorAll:()=>[]});
let runId=null, jobId=null, lastProofMtime=0, lastChatKey='', convoKey='', dagRunKey='', leanRunKey='', leanDirty=false, autoPipelineKey='';
let leanRefreshSeq=0, leanPollTimer=null, workspaceView='sandbox', lastSandboxRun=null;
let manuscriptAskContext=null, manuscriptAskSending='', manuscriptAskRunId='';
const manuscriptAskSent=new Map(), convoOpen=new Set(), RUN_KIND_BY_ID=new Map(), chatAttachments={clear(){}};
const syncLeanControls=()=>{}, syncRunAccess=()=>{}, resetProofView=()=>{}, loadRuns=()=>{}, syncRunToMonitor=()=>{}, loadChat=()=>{}, updateChatStatus=()=>{};
const applyRunKindView=()=>{}, refreshConversation=()=>{}, syncDagRebuild=()=>{}, refreshDagView=()=>{}, refreshLeanView=()=>{}, startPolling=()=>{};
let policy={enabled:true,reason:null,asks:{enabled:true,sponsored:true}};
const viewerIsGuest=()=>false, analyzePolicy=()=>policy, readabilityPolicy=()=>policy;
const manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), renderMathIn=()=>{}, syncManuscriptAskButtons=()=>{};
const analyzeEl=(tag,cls,text)=>({tag,cls,text,setAttribute(){}}), manuscriptAskItem=item=>({tag:'li',cls:'ask-item',text:item.question});
// As a poll draws the record of the run shown (applySandboxPreview).
const draw=run=>{ lastSandboxRun=run; renderManuscriptAsks(run); };
const panel=()=>({items:$('ask-list').kids.map(kid=>kid.cls),send:[$('ask-send').disabled,$('ask-send').title],hint:$('ask-hint').textContent,
  input:[$('ask-input').value,$('ask-input').disabled],status:$('ask-status').textContent,context:!$('ask-context').hidden});`)
  const panel = () => plain(step('panel()'))
  const first = analysis({ analyze: { asks: [DONE] } })
  step(`selectRun('a1',null); draw(${JSON.stringify(first)})`)
  const ready = { items: ['ask-item'], send: [false, ''], hint: 'Enter to ask · Shift+Enter newline', input: ['', false], status: '', context: false }
  assert.deepEqual(panel(), ready)
  // Another run, whose record has not arrived (or never does: a run that cannot be read): nothing of the first is left.
  step(`$('ask-input').value='A draft'; $('ask-status').textContent='✗ refused'; manuscriptAskContext={node_id:'lem:a',label:'Lemma 1',view:'report'}; renderManuscriptAskContext()`)
  step(`selectRun('a2',null)`)
  const waiting = { items: [], send: [true, ''], hint: 'Loading…', input: ['', false], status: '', context: false }
  assert.deepEqual(panel(), waiting)
  // Back to the first: its questions are drawn again once its record arrives, though they are the ones drawn before.
  step(`selectRun('a1',null)`)
  assert.deepEqual(panel(), waiting)
  step(`draw(${JSON.stringify(first)})`)
  assert.deepEqual(panel(), ready)
  // Choosing the run shown again keeps what is being written.
  step(`$('ask-input').value='Another draft'; selectRun('a1',null)`)
  assert.deepEqual(panel().input, ['Another draft', false])
  // A run nothing can be asked about (a sponsored one while sponsored questions are off) leaves nothing behind either.
  step(`policy={enabled:true,reason:null,asks:{enabled:true,sponsored:false,sponsored_reason:'Sponsored questions are off today.'}}`)
  step(`selectRun('a3',null); draw(${JSON.stringify(analysis({ run_id: 'a3', credential_source: 'sponsored_kimi' }))})`)
  assert.deepEqual(panel(), { items: ['ask-empty'], send: [true, 'Sponsored questions are off today.'], hint: 'Sponsored questions are off today.', input: ['', true], status: '', context: false })
  step(`selectRun('r1',null)`)
  assert.deepEqual(panel(), waiting)
})

test('a passage is offered for a question only once its run is published, whichever view it is selected in', () => {
  const step = load([...ASK_STATE, 'readabilitySelectionView', 'manuscriptSelectionView'], ['MANUSCRIPT_ASK'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='';
let runId='a1', lastSandboxRun=null, analysisSourceShown=false, analysisSourceRunId='', readabilityTab='readable';
const viewerIsGuest=()=>false, analyzePolicy=()=>({enabled:true,asks:{enabled:true}}), readabilityPolicy=()=>({enabled:true,asks:{enabled:true}});`)
  // The run shown and its view: the stored manuscript of this run, or a readable version's tab.
  const view = (record, { source = false, tab = 'readable' } = {}) => step(`lastSandboxRun=${JSON.stringify(record)}; runId=${JSON.stringify(record?.run_id || 'a1')};
    analysisSourceShown=${source}; analysisSourceRunId=runId; readabilityTab=${JSON.stringify(tab)}; manuscriptSelectionView()`)
  // The stored manuscript of an analysis still running, or of one that published no report: nothing to ask about.
  assert.equal(view(analysis({ status: 'running', sandbox_preview: { kind: 'analysis', has_report: false } }), { source: true }), '')
  assert.equal(view(analysis({ status: 'failed', sandbox_preview: { kind: 'analysis', has_report: false } }), { source: true }), '')
  assert.equal(view(analysis({ status: 'running' }), { source: true }), '')
  // Published (a stopped analysis publishes what had settled): the stored manuscript when shown, else the report.
  assert.equal(view(analysis({ status: 'stopped' }), { source: true }), 'source')
  assert.equal(view(analysis()), 'report')
  // Before the record arrives nothing is offered, even with this run's stored copy shown.
  assert.equal(view(null, { source: true }), '')
  // A readable version: its Readable or Original tab, once published.
  const readable = (extra = {}) => ({ run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { has_readable: true }, ...extra })
  assert.equal(view(readable({ status: 'running', sandbox_preview: {} }), { tab: 'original' }), '')
  assert.equal(view(readable({ status: 'failed', sandbox_preview: {} }), { tab: 'original' }), '')
  assert.equal(view(readable(), { tab: 'original' }), 'original')
  assert.equal(view(readable(), { tab: 'notation' }), '')
})

test('Re-check waits while a question about the analysis is answered (the server takes one at a time)', () => {
  const actions = load(['analyzeEl', 'analysisDetailActions', ...ASK_STATE], ['ANALYZE_VERDICT_LABEL', 'MANUSCRIPT_ASK'], `
const document={createElement:tag=>({tag,className:'',textContent:'',title:'',disabled:false,type:'',kids:[],attrs:{},setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}})};
let record=null;
const viewerIsGuest=()=>false, analyzePolicy=()=>({enabled:true,asks:{enabled:true}}), readabilityPolicy=()=>null, readabilityUiReady=()=>true;
const analysisRunState=()=>({run:record,running:record?.status==='running',recheck:record?.analyze?.recheck||null});
const manuscriptAskSent=new Map(); let manuscriptAskSending='';
const dagDetailText=v=>String(v||''), analysisSourceFile=()=>'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{}, askAboutNode=()=>{};`)
  // Each action of the result's details: [disabled, title].
  const buttons = (run, checkable = true) => plain(actions(`(()=>{ record=${JSON.stringify(run)};
    const row=analysisDetailActions({id:'lem:residues',label:'Lemma 4.1'},{checkable:${checkable}},{}).kids[1];
    return Object.fromEntries(row.kids.map(kid=>[kid.textContent,[kid.disabled,kid.title]])); })()`))
  const answering = analysis({ analyze: { ask: { ask_id: 'q', status: 'running' } } })
  const busy = buttons(answering)
  assert.deepEqual(busy['↻ Re-check'], [true, 'A question about this analysis is being answered — re-check when it is answered'])
  // Formalize, and a question about the result, are not held up by it.
  assert.equal(busy['∎ Formalize statement'][0], false)
  assert.equal(busy['Ask about this result'][0], false)
  // Answered: Re-check is offered again.
  assert.equal(buttons(analysis({ analyze: { ask: { ask_id: 'q', status: 'done' }, asks: [DONE] } }))['↻ Re-check'][0], false)
  // A result the parser did not locate, a running analysis and another re-check keep their own words.
  assert.equal(buttons(answering, false)['↻ Re-check'][1], 'Only results whose proof the parser located can be re-checked')
  assert.equal(buttons({ ...answering, status: 'running' })['↻ Re-check'][1], 'Available when the analysis finishes')
  assert.equal(buttons({ ...answering, analyze: { ...answering.analyze, recheck: { node_id: 'lem:x', status: 'running' } } })['↻ Re-check'][1], 'Another re-check is running')
})

test('a passage is cut to 4,000 characters by code point: never inside a surrogate pair', async () => {
  const step = load([...ASK_STATE, 'readabilitySelectionView', 'manuscriptSelectionView', 'shownAskKind', 'renderManuscriptAskContext', 'manuscriptAskCut',
    'askAboutSelection', 'sendManuscriptAsk'], ['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], `
const posts=[], els={};
const $=id=>els[id]||(els[id]={id,value:'',textContent:'',disabled:false,hidden:false,kids:[],focus(){},replaceChildren(...kids){ this.kids=kids; }});
let manuscriptAskContext=null, manuscriptAskSending='', readabilityTab='readable', runId='a1', analysisSourceShown=false, analysisSourceRunId='';
const manuscriptAskSent=new Map();
let lastSandboxRun={run_id:'a1',run_kind:'analyze',status:'finished',sandbox_preview:{kind:'analysis',has_report:true},analyze:{asks:[]}};
const fetch=async(url,options)=>{ posts.push(JSON.parse(options.body)); return {ok:true,status:202,json:async()=>({ok:true,ask_id:'feedfeed0002'})}; };
const window={getSelection:()=>({removeAllRanges(){}})}, document={body:{classList:{contains:name=>name==='analysis-run'}}};
const startPolling=()=>{}, renderManuscriptAsks=()=>{}, syncManuscriptAskCompose=()=>{}, limitMessage=message=>message;
const analyzeEl=(tag,cls,text)=>({tag,cls,text,setAttribute(){}});
const analyzePolicy=()=>({enabled:true,asks:{enabled:true}}), readabilityPolicy=()=>null, viewerIsGuest=()=>false;`)
  const wellFormed = text => !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(text)
  // 3,999 letters, then 𝔸 and 𝔹 (two UTF-16 units each): a cut by unit would keep half of 𝔸.
  const passage = 'x'.repeat(3999) + '\u{1D538}\u{1D539}'
  assert.equal(wellFormed(passage.slice(0, 4000)), false)
  step(`askAboutSelection(${JSON.stringify('  ' + passage + '  ')})`)
  const kept = step('manuscriptAskContext.selection')
  assert.equal(kept, 'x'.repeat(3999) + '\u{1D538}')
  assert.equal([...kept].length, 4000)
  assert.ok(wellFormed(kept))
  // The request is cut the same way, whatever the context holds.
  step(`manuscriptAskContext={selection:${JSON.stringify('y'.repeat(3998) + '\u{1D4AA}\u{1D4AB}\u{1D4AC}')},view:'source'}; $('ask-input').value='What is \u{1D4AA} here?'`)
  await step('sendManuscriptAsk()')
  const [sent] = plain(step('posts'))
  assert.deepEqual([sent.selection, sent.view], ['y'.repeat(3998) + '\u{1D4AA}\u{1D4AB}', 'source'])
  assert.ok(wellFormed(sent.selection))
  // The question is sent whole: the box holds at most 2,000 characters, and a longer one is refused, not cut.
  assert.equal(sent.question, 'What is \u{1D4AA} here?')
  assert.match(html, /<textarea id="ask-input" maxlength="2000"/)
  assert.match(fn('sendManuscriptAsk'), /if\(question\.length>2000\)\{ status\.textContent='Questions are limited to 2,000 characters\.'; return; \}/)
})

test('asking is off with the mode or its questions, without the mode in a listing read, and on a sponsored run while sponsored questions are; the DAG and the pop-up say the same', () => {
  const step = load([...ASK_STATE, 'syncManuscriptAskCompose', 'readabilitySelectionView', 'manuscriptSelectionView', 'analyzeEl', 'analysisDetailActions'],
    ['MANUSCRIPT_ASK', 'ANALYZE_VERDICT_LABEL'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='';
const els={}; const $=id=>els[id]||(els[id]={id,disabled:false,title:'',textContent:''});
const document={createElement:tag=>({tag,className:'',textContent:'',title:'',disabled:false,type:'',kids:[],attrs:{},setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}})};
let enginesListed=false, policies={analyze:null,readability:null}, runId='a1', lastSandboxRun=null, readabilityTab='readable', analysisSourceShown=false, analysisSourceRunId='';
const viewerIsGuest=()=>false, analyzePolicy=()=>policies.analyze, readabilityPolicy=()=>policies.readability, readabilityUiReady=()=>true;
const analysisRunState=()=>({run:lastSandboxRun,running:lastSandboxRun?.status==='running',recheck:null});
const dagDetailText=v=>String(v||''), analysisSourceFile=()=>'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{}, askAboutNode=()=>{};
// Where a question about the run shown stands: the compose box, the DAG's "Ask about this result" and the selection pop-up.
const where=run=>{ lastSandboxRun=run; runId=run?run.run_id:'a1';
  if(run)syncManuscriptAskCompose(run,manuscriptAsks(run));
  const ask=analysisDetailActions({id:'lem:a',label:'Lemma 1'},{checkable:true},{}).kids[1].kids.find(kid=>kid.textContent==='Ask about this result');
  return {compose:run?[$('ask-send').disabled,$('ask-send').title,$('ask-hint').textContent,$('ask-input').disabled]:null,dag:[ask.disabled,ask.title],popup:manuscriptSelectionView()}; };`)
  const state = (run, { analyze = null, readability = null, listed = true } = {}) =>
    plain(step(`enginesListed=${listed}; policies=${JSON.stringify({ analyze, readability })}; where(${JSON.stringify(run)})`))
  const on = { enabled: true, reason: null, asks: { enabled: true, reason: null, sponsored: true, sponsored_reason: null, per_run: null } }
  const ready = { compose: [false, '', 'Enter to ask · Shift+Enter newline', false],
    dag: [false, 'Ask a question about this result in the Sandbox (answered by a model — not a certificate)'], popup: 'report' }
  const off = reason => ({ compose: [true, reason, reason, true], dag: [true, reason], popup: '' })
  const DEFAULT = 'Questions about analyses are turned off on this site.'
  assert.deepEqual(state(analysis(), { analyze: on }), ready)
  // The mode off for this viewer (policy.enabled false): its reason, else the default words.
  assert.deepEqual(state(analysis(), { analyze: { ...on, enabled: false, reason: 'Analyze needs a signed-in account on this site.' } }), off('Analyze needs a signed-in account on this site.'))
  assert.deepEqual(state(analysis(), { analyze: { enabled: false, reason: null } }), off(DEFAULT))
  // Its questions off (asks.enabled false): their own reason, then the policy's, then the default words.
  assert.deepEqual(state(analysis(), { analyze: { ...on, asks: { enabled: false, reason: 'Sponsored questions are turned off for guests.' } } }), off('Sponsored questions are turned off for guests.'))
  assert.deepEqual(state(analysis(), { analyze: { enabled: false, reason: 'Analyze is turned off for members.', asks: { enabled: false, reason: null } } }), off('Analyze is turned off for members.'))
  assert.deepEqual(state(analysis(), { analyze: { ...on, asks: { enabled: false, reason: '  ' } } }), off(DEFAULT))
  // No Analyze policy at all (the mode switched off, so not listed): off once the listing has been read, not before.
  assert.deepEqual(state(analysis(), { listed: true }), off(DEFAULT))
  assert.deepEqual(state(analysis(), { listed: false }), ready)
  // The DAG's button before the record arrives: only what holds for every analysis stops it.
  assert.deepEqual(state(null, { listed: true }).dag, [true, DEFAULT])
  assert.deepEqual(state(null, { listed: false }).dag, ready.dag)
  assert.deepEqual(state(null, { analyze: on }).dag, ready.dag)
  // A sponsored run while sponsored questions are off: the policy's reason, else the default words.
  const sponsored = analysis({ credential_source: 'sponsored_kimi' })
  const noSponsored = { ...on, asks: { ...on.asks, sponsored: false, sponsored_reason: 'Sponsored Analyze is turned off on this site, so questions about this analysis can\'t be answered now' } }
  assert.deepEqual(state(sponsored, { analyze: noSponsored }), off('Sponsored Analyze is turned off on this site, so questions about this analysis can\'t be answered now'))
  assert.deepEqual(state(sponsored, { analyze: { ...on, asks: { ...on.asks, sponsored: false, sponsored_reason: null } } }), off('Sponsored questions are turned off on this site.'))
  // A run on the viewer's own key, or sponsored questions on: asked as before.
  assert.deepEqual(state(analysis({ credential_source: 'user' }), { analyze: noSponsored }), ready)
  assert.deepEqual(state(analysis(), { analyze: noSponsored }), ready)
  assert.deepEqual(state(sponsored, { analyze: on }), ready)
  // A readable version: the same states, in its own words.
  const readable = (extra = {}) => ({ run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { has_readable: true }, ...extra })
  const readableState = (run, policies) => { const { compose, popup } = state(run, policies); return { compose, popup } }
  const readableOff = reason => ({ compose: [true, reason, reason, true], popup: '' })
  assert.deepEqual(readableState(readable(), { readability: on }), { compose: ready.compose, popup: 'readable' })
  assert.deepEqual(readableState(readable(), { readability: { ...on, enabled: false, reason: 'Readability is turned off on this site.' } }), readableOff('Readability is turned off on this site.'))
  assert.deepEqual(readableState(readable(), { readability: { ...on, asks: { enabled: false } } }), readableOff('Questions about readable versions are turned off on this site.'))
  assert.deepEqual(readableState(readable(), { listed: true }), readableOff('Questions about readable versions are turned off on this site.'))
  assert.deepEqual(readableState(readable(), { listed: false }), { compose: ready.compose, popup: 'readable' })
  assert.deepEqual(readableState(readable({ credential_source: 'sponsored_kimi' }), { readability: { ...on, asks: { ...on.asks, sponsored: false, sponsored_reason: 'Sponsored Readability is off.' } } }), readableOff('Sponsored Readability is off.'))
})

test('the console knows when the engine listing has been read, and the Ask panel of the run shown follows it', async () => {
  const step = load(['loadEngines'], [], `
let ENGINES=[], enginesListed=false, runId='a1', lastSandboxRun=null, reply=null; const calls=[];
const fetch=async()=>{ if(reply==='offline')throw new Error('offline'); return {json:async()=>reply}; };
const renderEngines=()=>calls.push('engines'), readabilityUiReady=()=>true, renderManuscriptAsks=run=>calls.push('asks '+run.run_id);
const out=()=>({listed:enginesListed,ids:ENGINES.map(engine=>engine.id),calls:calls.splice(0)});`)
  const listing = async (body, record = null) => {
    step(`reply=${JSON.stringify(body)}; lastSandboxRun=${JSON.stringify(record)}`)
    await step('loadEngines()')
    return plain(step('out()'))
  }
  // Until the first answer: empty, and not read.
  assert.deepEqual(plain(step('out()')), { listed: false, ids: [], calls: [] })
  // Read: a mode it leaves out is off, and the run shown is drawn again with it.
  assert.deepEqual(await listing({ engines: [{ id: 'kimi' }] }, analysis()), { listed: true, ids: ['kimi'], calls: ['engines', 'asks a1'] })
  assert.deepEqual(await listing({ engines: [] }), { listed: true, ids: [], calls: ['engines'] })
  // Not read (offline, or an answer without a listing): the placeholder says nothing about the modes.
  assert.deepEqual(await listing('offline', analysis()), { listed: false, ids: ['hermes'], calls: ['engines', 'asks a1'] })
  assert.deepEqual(await listing({ error: 'Not signed in' }), { listed: false, ids: [], calls: ['engines'] })
  // Another run's record is not drawn for the run shown.
  assert.deepEqual((await listing({ engines: [] }, analysis({ run_id: 'a0' }))).calls, ['engines'])
})

test('the Map tab and the Paper map start no question while the run shown takes none', () => {
  const step = load([...ASK_STATE, 'shownAskBlocked', 'paperMapActions', 'readabilityMapRow'], ['MANUSCRIPT_ASK'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='', enginesListed=true, runId='r1', lastSandboxRun=null;
const viewerIsGuest=()=>false, analyzePolicy=()=>null;
let policy={enabled:true,asks:{enabled:true}}; const readabilityPolicy=()=>policy;
function analyzeEl(tag,cls,text){ return {tag,className:cls||'',textContent:text==null?'':String(text),title:'',disabled:false,attrs:{},kids:[],
  setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}}; }
const paperMapImportance=()=>3, paperMapCardTitle=node=>node.label, dagDetailText=value=>String(value||''), analysisModelProse=text=>text;
function readabilityJumpTo(){} function askAboutNode(){}
const buttons=()=>{ const node={id:'lem:a',label:'Lemma 1'};
  const fromMap=paperMapActions(node).kids.find(el=>el.textContent==='Ask about this');
  const fromTab=readabilityMapRow(node).kids.at(-1).kids.find(el=>el.textContent==='Ask');
  return [fromMap.disabled,fromMap.title,fromTab.disabled,fromTab.title]; };`)
  const published = { run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { kind: 'readability', has_readable: true } }
  step(`lastSandboxRun=${JSON.stringify(published)}`)
  assert.deepEqual(plain(step('buttons()')), [false, '', false, 'Ask about Lemma 1'])
  // Questions about readable versions turned off: the reason, on both buttons.
  step(`policy={enabled:true,asks:{enabled:false,reason:'Questions are off here.'}}`)
  assert.deepEqual(plain(step('buttons()')), [true, 'Questions are off here.', true, 'Questions are off here.'])
  // Not published yet.
  step(`policy={enabled:true,asks:{enabled:true}}; lastSandboxRun={...lastSandboxRun,status:'running',sandbox_preview:{kind:'readability'}}`)
  assert.equal(plain(step('buttons()'))[0], true)
})

// A small element tree for the views these buttons live in (the DAG's details,
// the Map tab, the Ask panel): classes, text, buttons, focus and scrolling.
// `$` finds an element by id, making it the first time.
const PANEL_DOM = `
let focused=null;
class El {
  constructor(tag){ this.tagName=String(tag).toUpperCase(); this.className=''; this.kids=[]; this.parent=null; this.attrs={}; this.dataset={}; this.style={setProperty(){}};
    this.classList={add(){},remove(){},toggle(){}}; this.offsetWidth=0;
    this.disabled=false; this.hidden=false; this.open=false; this.title=''; this.type=''; this.value=''; this.scrollTop=0; this.text=''; this.onclick=null; }
  get textContent(){ return this.text+this.kids.map(kid=>typeof kid==='object'?kid.textContent:String(kid)).join(''); }
  set textContent(value){ this.text=String(value); this.kids=[]; }
  get childElementCount(){ return this.kids.filter(kid=>typeof kid==='object').length; }
  setAttribute(k,v){ this.attrs[k]=String(v); }
  append(...items){ items.forEach(item=>{ if(typeof item==='object'){ item.remove(); item.parent=this; } this.kids.push(item); }); }
  replaceChildren(...items){ this.kids.forEach(kid=>{ if(typeof kid==='object')kid.parent=null; }); this.kids=[]; this.append(...items); }
  replaceWith(...items){
    const parent=this.parent; if(!parent)return;
    items.forEach(item=>{ if(typeof item==='object'&&item!==this)item.remove(); });
    items.forEach(item=>{ if(typeof item==='object')item.parent=parent; });
    parent.kids.splice(parent.kids.indexOf(this),1,...items); this.parent=null;
  }
  remove(){ if(this.parent){ this.parent.kids=this.parent.kids.filter(kid=>kid!==this); this.parent=null; } }
  addEventListener(){}
  elements(){ return this.kids.filter(kid=>typeof kid==='object').flatMap(kid=>[kid,...kid.elements()]); }
  querySelectorAll(selector){
    const test=selector.startsWith('.')?el=>(' '+el.className+' ').includes(' '+selector.slice(1)+' '):el=>el.tagName===selector.toUpperCase();
    return this.elements().filter(test);
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0]||null; }
  contains(el){ for(let node=el;node;node=node.parent)if(node===this)return true; return false; }
  focus(){ focused=this; }
}
let bodyClass='';
const document={createElement:tag=>new El(tag),get activeElement(){ return focused; },querySelectorAll:()=>[],getElementById:()=>null,
  body:{classList:{contains:name=>name===bodyClass}}};
const els={}; const $=id=>els[id]||(els[id]=Object.assign(new El('div'),{id}));
const syncDagRebuild=()=>{}, refreshDagView=()=>{};
// A result's details as renderAnalysisDetail lays them out: a head, then a
// body (the part that scrolls) with its statement, a proof excerpt to open,
// and the actions last.
function renderAnalysisDetail(node,context){
  const panel=$('dag-detail'), head=analyzeEl('div','dag-detail-head'), body=analyzeEl('div','dag-detail-body');
  const close=analyzeEl('button','','×'); close.onclick=closeDagDetail;
  head.append(analyzeEl('h4','',node.label),close);
  const excerpt=analyzeEl('details'); excerpt.append(analyzeEl('summary','','Proof excerpt from the manuscript'),analyzeEl('div','txt','Proof.'));
  const meta=node.meta||{};
  body.append(analyzeEl('section','txt',node.statement),excerpt,analysisDetailActions(node,meta,meta.source_ref||{}));
  panel.replaceChildren(head,body); panel.hidden=false;
}
// The actions of the details shown: {text: [disabled, title]}.
const detailButtons=()=>{ const row=$('dag-detail').hidden?null:$('dag-detail').querySelector('.analysis-action-row');
  return row&&Object.fromEntries(row.querySelectorAll('button').map(button=>[button.textContent,[button.disabled,button.title]])); };
const detailBody=()=>$('dag-detail').querySelector('.dag-detail-body');
const detailActions=()=>$('dag-detail').querySelector('.analysis-actions');`

test('an open result\'s details follow a question about the analysis: Re-check waits while it is answered, and "Ask about this result" with it', async () => {
  // The real details actions, views, Ask panel and send; the details' layout reduced to the actions (PANEL_DOM).
  const step = load([...ASK_STATE, 'analyzeEl', 'dagDetailText', 'analysisRunState', 'analysisDetailActions', 'closeDagDetail', 'selectDagNode', 'renderDagDetail',
    'dagDetailActionsKey', 'redrawDagDetailActions', 'setWorkspaceView', 'shownAskBlocked', 'shownAskKind', 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks',
    'syncManuscriptAskCompose', 'syncManuscriptAskButtons', 'manuscriptAskCut', 'askAboutNode', 'sendManuscriptAsk'],
  ['ANALYZE_VERDICT_LABEL', 'MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], PANEL_DOM + `
let runId='a1', lastSandboxRun=null, workspaceView='dag', readabilityTab='readable', dagView=null, dagSelectedNodeId=null;
let manuscriptAskContext=null, manuscriptAskSending='', manuscriptAskRunId='';
const manuscriptAskSent=new Map(), posts=[];
bodyClass='analysis-run';
// A guest: three questions about an analysis.
const viewerIsGuest=()=>true, analyzePolicy=()=>({enabled:true,asks:{enabled:true,per_run:3}}), readabilityPolicy=()=>null, readabilityUiReady=()=>true;
const fetch=async(url,options)=>{ posts.push([url,JSON.parse(options.body)]); return {ok:true,status:202,json:async()=>({ok:true,ask_id:'a1q3'})}; };
const startPolling=()=>{}, limitMessage=message=>message;
const manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), manuscriptAskItem=item=>analyzeEl('li','ask-item',item.question), renderMathIn=()=>{};
const analysisResultName=node=>node.label, paperMapCardTitle=node=>node.label, renderPaperMapDetail=()=>{}, dagHighlight=()=>{};
const analysisSourceFile=run=>run?'analysis/source/residues.tex':'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{};
const node={id:'lem:residues',label:'Lemma 4.1',statement:'The residues are distinct.',meta:{checkable:true,source_ref:{file:'residues.tex',lines:[67,93]}}};
// As a poll applies a record of the run shown (applySandboxPreview, then applyAnalysisPreview).
const apply=run=>{ lastSandboxRun=run; renderManuscriptAsks(run); };
const press=text=>$('dag-detail').querySelectorAll('button').find(button=>button.textContent===text).onclick();`)
  const record = (extra = {}) => analysis({ analyze: { asks: [DONE, { ...DONE, ask_id: 'a1q2' }], asks_started: 2, ...extra } })
  const answering = record({ asks_started: 3, ask: { ask_id: 'a1q3', status: 'running', question: 'Is the gap real?' } })
  const buttons = () => plain(step('detailButtons()'))
  const RECHECK = 'Check this result again with the model verifier (one result, a few calls)'
  const ASK = 'Ask a question about this result in the Sandbox (answered by a model — not a certificate)'
  const ANSWERING = 'A question about this analysis is being answered — re-check when it is answered'
  const SPENT = 'Guests can ask 3 questions about an analysis — sign in to ask more.'
  const SOURCE = 'Show these lines of the stored manuscript'
  step(`apply(${JSON.stringify(record())}); dagView={runId:'a1',view:{role:'analysis'},graph:{nodes:[node]},domIdByNodeId:new Map()}; selectDagNode(node)`)
  assert.deepEqual(buttons(), { '↻ Re-check': [false, RECHECK], '∎ Formalize statement': [false, 'Formalize this statement in Lean on this server (statements only; proofs are not attempted)'],
    'Ask about this result': [false, ASK], 'Open in source': [false, SOURCE] })
  // The reader opens the proof excerpt and the manuscript's lines, scrolls to the actions, asks about the
  // result and sends the guest's last question from the Sandbox.
  step(`var body=detailBody(), excerpt=$('dag-detail').querySelector('details'); excerpt.open=true; body.scrollTop=240;
    detailActions().append(analyzeEl('div','analysis-source-lines','residues.tex · statement · read-only'));
    press('Ask about this result'); $('ask-input').value='Is the gap real?'`)
  assert.equal(step('workspaceView'), 'sandbox')
  await step('sendManuscriptAsk()')
  assert.deepEqual(plain(step('posts.map(([url,body])=>[url,body.node_id])')), [['/api/run/a1/analysis/ask', 'lem:residues']])
  // The next record shows the question being answered (and counts it). Back in the DAG: Re-check waits for the
  // answer and the result offers no further question; the rest of the details is as the reader left it.
  step(`apply(${JSON.stringify(answering)}); setWorkspaceView('dag')`)
  assert.deepEqual(buttons(), { '↻ Re-check': [true, ANSWERING], '∎ Formalize statement': [false, 'Formalize this statement in Lean on this server (statements only; proofs are not attempted)'],
    'Ask about this result': [true, SPENT], 'Open in source': [false, SOURCE] })
  assert.deepEqual(plain(step("[detailBody()===body,body.scrollTop,excerpt.open,$('dag-detail').contains(excerpt),detailActions().querySelectorAll('.analysis-source-lines').length]")),
    [true, 240, true, true, 1])
  // A record that changes none of that leaves the actions alone.
  step(`var actions=detailActions(); apply(${JSON.stringify(answering)})`)
  assert.equal(step('detailActions()===actions'), true)
  // The answer arrives while the DAG is shown: Re-check is offered again, with focus kept on the action that had it.
  step("$('dag-detail').querySelectorAll('button').find(button=>button.textContent==='∎ Formalize statement').focus()")
  step(`apply(${JSON.stringify(record({ asks: [DONE, { ...DONE, ask_id: 'a1q2' }, { ...DONE, ask_id: 'a1q3' }], asks_started: 3, ask: { ask_id: 'a1q3', status: 'done' } }))})`)
  assert.deepEqual(buttons()['↻ Re-check'], [false, RECHECK])
  assert.deepEqual(buttons()['Ask about this result'], [true, SPENT])
  assert.equal(step('detailActions()===actions'), false)
  assert.deepEqual(plain(step("[document.activeElement.textContent,detailActions().contains(document.activeElement)]")), ['∎ Formalize statement', true])
  // Details drawn before the record arrived (the run chosen again) get their actions once it does.
  step("lastSandboxRun=null; dagSelectedNodeId=null; selectDagNode(node)")
  assert.deepEqual(Object.keys(buttons()), ['↻ Re-check', '∎ Formalize statement', 'Ask about this result'])
  assert.equal(buttons()['↻ Re-check'][0], true)
  step(`apply(${JSON.stringify(record({ asks_started: 3, ask: { ask_id: 'a1q3', status: 'done' } }))})`)
  assert.deepEqual(buttons()['↻ Re-check'], [false, RECHECK])
  assert.deepEqual(buttons()['Open in source'], [false, SOURCE])
  // Details of another run's graph (the DAG not drawn again yet) are left for that drawing.
  step(`dagView.runId='a0'; actions=detailActions(); apply(${JSON.stringify(record({ asks_started: 3, ask: { ask_id: 'a1q4', status: 'running' } }))})`)
  assert.equal(step('detailActions()===actions'), true)
  assert.match(fn('renderManuscriptAsks'), /syncManuscriptAskCompose\(run,asks\);\n\s*syncManuscriptAskButtons\(run\);\n\}/)
})

test('the Map tab, an open Paper-map card and an analysis\'s "Ask about this result" follow why no question can be started: a guest\'s last question, and the engine listing', async () => {
  const step = load([...ASK_STATE, 'analyzeEl', 'dagDetailText', 'closeDagDetail', 'selectDagNode', 'renderDagDetail', 'dagDetailActionsKey', 'redrawDagDetailActions', 'setWorkspaceView',
    'shownAskBlocked', 'shownAskKind', 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks', 'syncManuscriptAskCompose', 'syncManuscriptAskButtons',
    'renderReadabilityTab', 'renderReadabilityMap', 'readabilityMapRow', 'paperMapActions', 'renderPaperMapDetail', 'paperMapImportance', 'paperMapCardTitle',
    'paperMapSectionLabel', 'paperMapSizeText', 'analysisRunState', 'analysisDetailActions',
    'loadEngines', 'analyzeEngineEntry', 'analyzePolicy', 'readabilityEngineEntry', 'readabilityPolicy', 'viewerIsGuest'],
  ['ANALYZE_VERDICT_LABEL', 'MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], PANEL_DOM + `
let runId='r1', lastSandboxRun=null, workspaceView='sandbox', readabilityTab='map', dagView=null, dagSelectedNodeId=null, dagMode='paper-map';
let manuscriptAskContext=null, manuscriptAskSending='', manuscriptAskRunId='';
const manuscriptAskSent=new Map(), readabilityMapCache=new Map();
bodyClass='readability-run';
// A guest; the listing offers both kinds of question, three per run.
const policy={enabled:true,asks:{enabled:true,per_run:3}};
let CURRENT_USER={id:3,guest:true}, enginesListed=true, listing=null, ENGINES=[{id:'readability',modes:['readability'],available:true,readability:policy},{id:'analyze',modes:['analyze'],available:true,analyze:policy}];
const fetch=async()=>({json:async()=>JSON.parse(JSON.stringify(listing))});
const renderEngines=()=>{}, readabilityUiReady=()=>true, setProofEmpty=()=>{}, renderMathIn=()=>{}, readabilityMacros=()=>({}), ensurePaperMap=async()=>null;
const manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), manuscriptAskItem=item=>analyzeEl('li','ask-item',item.question);
const analysisModelProse=text=>text, analysisProse=text=>text, analysisTextAccents=text=>text, dagMathSafeClip=text=>text, analysisResultName=node=>node.label;
const dagHighlight=()=>{}, readabilityJumpTo=()=>{}, askAboutNode=()=>{}, setDagMode=()=>{};
const analysisSourceFile=()=>'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{};
// As applySandboxPreview applies a record of the run shown: a readable version's tab, then the Ask panel.
const apply=run=>{ lastSandboxRun=run; if(isReadabilityRun(run))renderReadabilityTab(run); renderManuscriptAsks(run); };
// The Map tab's Ask buttons, each [disabled, title].
const mapButtons=()=>$('md-view').querySelectorAll('.rd-map-actions').map(row=>row.querySelectorAll('button').find(button=>button.textContent==='Ask')).map(button=>[button.disabled,button.title]);`)
  const graph = { nodes: [
    { id: 'thm:main', kind: 'theorem', label: 'Theorem 1.1', importance: 5, main: true, section: '1 Introduction', role: 'The main result.' },
    { id: 'lem:key', kind: 'lemma', label: 'Lemma 2.1', importance: 3, section: '2 Estimates', role: 'Bounds the error term.' }],
  edges: [{ from: 'lem:key', to: 'thm:main', kind: 'proof' }] }
  const asked = { ask_id: 'r1q1', status: 'done', question: 'What is r?', answer: 'A radius.' }
  const readable = (extra = {}) => ({ run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { kind: 'readability', has_readable: true },
    readability: { asks: [asked, { ...asked, ask_id: 'r1q2' }], asks_started: 2, ...extra } })
  const map = () => plain(step('mapButtons()'))
  const card = () => plain(step('detailButtons()'))
  const ENABLED = [[false, 'Ask about Theorem 1.1'], [false, 'Ask about Lemma 2.1']]
  // The Map tab in the Sandbox, and a result's card left open in the Paper map.
  step(`readabilityMapCache.set('r1',{kind:'paper-map',status:'ready',generated_at:'g1',graph:${JSON.stringify(graph)}})`)
  step(`apply(${JSON.stringify(readable())}); setWorkspaceView('dag');
    dagView={runId:'r1',view:{role:'paper-map',refLabels:new Map(),macros:{}},graph:${JSON.stringify(graph)},domIdByNodeId:new Map()}; selectDagNode(dagView.graph.nodes[1]); setWorkspaceView('sandbox')`)
  assert.deepEqual(map(), ENABLED)
  assert.deepEqual(card(), { 'Read in readable version': [false, ''], 'Ask about this': [false, ''] })
  // The guest's last question: the record counts it, and every button that would start another says why it cannot.
  const SPENT = 'Guests can ask 3 questions about a readable version — sign in to ask more.'
  step(`apply(${JSON.stringify(readable({ asks_started: 3, ask: { ask_id: 'r1q3', status: 'running', question: 'Why?' } }))})`)
  assert.deepEqual(map(), [[true, SPENT], [true, SPENT]])
  assert.deepEqual(card(), { 'Read in readable version': [false, ''], 'Ask about this': [true, SPENT] })
  assert.equal(step("$('ask-hint').textContent"), 'One question at a time — the answer is on its way.')
  // A Map tab's disabled Ask looks it.
  assert.match(read('../agent_monitor/web/readability.css'), /\.rd-map-actions button:disabled \{ opacity: \.45; cursor: not-allowed; \}/)
  // The engine listing read again turns questions off: the Map tab and the open card follow it as the compose box does.
  step(`setWorkspaceView('dag'); apply(${JSON.stringify(readable({ asks_started: 1 }))})`)
  assert.deepEqual([map(), card()['Ask about this']], [ENABLED, [false, '']])
  const OFF = 'Questions about readable versions are paused for maintenance.'
  step(`listing={engines:[{id:'readability',modes:['readability'],available:true,readability:{enabled:true,asks:{enabled:false,reason:${JSON.stringify(OFF)}}}},{id:'analyze',modes:['analyze'],available:true,analyze:policy}]}`)
  await step('loadEngines()')
  assert.deepEqual(card(), { 'Read in readable version': [false, ''], 'Ask about this': [true, OFF] })
  assert.deepEqual(map(), [[true, OFF], [true, OFF]])
  assert.equal(step("$('ask-hint').textContent"), OFF)
  // An analysis shown with a result's details open: its "Ask about this result" follows the listing too.
  step(`runId='a1'; bodyClass='analysis-run'; readabilityTab='readable'; apply(${JSON.stringify(analysis({ analyze: { asks: [DONE], asks_started: 1 } }))});
    dagView={runId:'a1',view:{role:'analysis'},graph:{nodes:[{id:'lem:residues',label:'Lemma 4.1',statement:'x',meta:{checkable:true}}]},domIdByNodeId:new Map()}; selectDagNode(dagView.graph.nodes[0])`)
  assert.equal(card()['Ask about this result'][0], false)
  step(`listing={engines:[{id:'readability',modes:['readability'],available:true,readability:policy},{id:'analyze',modes:['analyze'],available:true,analyze:{enabled:true,asks:{enabled:false}}}]}`)
  await step('loadEngines()')
  assert.deepEqual(card()['Ask about this result'], [true, 'Questions about analyses are turned off on this site.'])
  assert.equal(card()['↻ Re-check'][0], false)
})

test('a late poll of the run left behind is not applied: the Ask panel, its draft and the record shown stay the run shown\'s', async () => {
  // The real tick, selectRun and Ask panel; the rest of the console reduced to stubs.
  const step = load(['tick', 'nextPollDelay', 'stopSettling', 'selectRun', ...ASK_STATE, 'resetManuscriptAsk', 'renderManuscriptAskContext', 'renderManuscriptAsks', 'syncManuscriptAskCompose'],
    ['POLL_MIN_MS', 'STOP_SETTLE_MS', 'STOPPING_RUNS', 'MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'], `
const els={};
const $=id=>els[id]||(els[id]={id,value:'',textContent:'',title:'',disabled:false,hidden:false,kids:[],scrollTop:0,scrollHeight:0,clientHeight:0,
  replaceChildren(...kids){ this.kids=kids; },querySelectorAll:()=>[]});
let runId=null, jobId=null, lastProofMtime=0, lastChatKey='', convoKey='', dagRunKey='', leanRunKey='', leanDirty=false, autoPipelineKey='';
let leanRefreshSeq=0, leanPollTimer=null, workspaceView='sandbox', lastSandboxRun=null, pollSig='', pollQuiet=0, pollDelay=2000, pollListedEnd=null;
let manuscriptAskContext=null, manuscriptAskSending='', manuscriptAskRunId='';
const manuscriptAskSent=new Map(), convoOpen=new Set(), RUN_KIND_BY_ID=new Map(), chatAttachments={clear(){}}, calls=[], requests=[];
const syncLeanControls=()=>{}, syncRunAccess=()=>{}, resetProofView=()=>{}, syncRunToMonitor=()=>{}, refreshConversation=()=>{}, applyRunKindView=()=>{};
const syncDagRebuild=()=>{}, refreshDagView=()=>{}, refreshLeanView=()=>{}, startPolling=()=>{}, setTimeout=()=>0;
const loadRuns=()=>calls.push('runs'), loadChat=()=>calls.push('chat'), updateChatStatus=run=>{ if(run)calls.push('status '+run.run_id); }, updateSessionButtons=()=>{};
const viewerIsGuest=()=>false, analyzePolicy=()=>({enabled:true,asks:{enabled:true}}), readabilityPolicy=()=>null;
const manuscriptAskLabels=()=>new Map(), manuscriptAskMacros=()=>({}), renderMathIn=()=>{}, syncManuscriptAskButtons=()=>{};
const analyzeEl=(tag,cls,text)=>({tag,cls,text,setAttribute(){}}), manuscriptAskItem=item=>({tag:'li',cls:'ask-item',text:item.question});
// A GET of a run record waits for the test to answer it.
const fetch=url=>new Promise(resolve=>requests.push({url,resolve}));
const answer=record=>{ const request=requests.shift(); calls.push('answer '+request.url); request.resolve({ok:true,json:async()=>JSON.parse(JSON.stringify(record))}); };
const consoleRunSignature=run=>JSON.stringify(run), setPill=(status,text)=>calls.push('pill '+text), updatePromptStrip=()=>{}, renderConversation=()=>{}, renderAutoPipeline=()=>{};
const updateProblemStrip=run=>{ lastSandboxRun=run; };
// As applySandboxPreview draws a record of an analysis (applyAnalysisPreview, then the Ask panel).
const applySandboxPreview=run=>{ calls.push('preview '+run.run_id); lastSandboxRun=run; renderManuscriptAsks(run); };
let proofWait=null; const refreshProofPreview=async()=>{ calls.push('proof'); if(proofWait)await proofWait; };
const panel=()=>({items:$('ask-list').kids.map(kid=>kid.text),input:$('ask-input').value,context:$('ask-context').hidden?'':$('ask-context').kids[0]?.text,
  hint:$('ask-hint').textContent,shown:lastSandboxRun&&lastSandboxRun.run_id,calls:calls.splice(0)});`)
  const panel = () => plain(step('panel()'))
  const first = analysis({ analyze: { asks: [DONE] } })
  const second = analysis({ run_id: 'a2', analyze: { asks: [{ ...DONE, ask_id: 'a2q1', question: 'Which bound is used?' }] } })
  step("selectRun('a1',null); calls.splice(0)")
  let ticking = step('tick()')
  step(`answer(${JSON.stringify(first)})`)
  await ticking
  assert.deepEqual(panel(), { items: ['Is the gap real?'], input: '', context: '', hint: 'Enter to ask · Shift+Enter newline', shown: 'a1',
    calls: ['answer /api/run/a1', 'pill finished · analyze', 'status a1', 'preview a1', 'proof', 'chat', 'runs'] })
  // The next poll of a1 is on its way when the reader chooses a2 and starts writing about it.
  ticking = step('tick()')
  step(`selectRun('a2',null); $('ask-input').value='A draft about a2'; manuscriptAskContext={node_id:'lem:b',label:'Lemma 2',view:'report'}; renderManuscriptAskContext()`)
  const waiting = { items: [], input: 'A draft about a2', context: 'About Lemma 2', hint: 'Loading…', shown: null }
  assert.deepEqual(panel(), { ...waiting, calls: ['runs', 'chat'] })
  // a1's record arrives late: nothing of it is applied (no record shown, no status, no conversation), and polling goes on.
  step(`answer(${JSON.stringify(first)})`)
  const polling = await ticking
  assert.deepEqual(panel(), { ...waiting, calls: ['answer /api/run/a1'] })
  assert.equal(polling, true)
  // Nor does the panel take another run's record handed to it.
  step(`renderManuscriptAsks(${JSON.stringify(first)})`)
  assert.deepEqual(panel(), { ...waiting, calls: [] })
  // a2's own record: its conversation, under the draft written for it.
  ticking = step('tick()')
  step(`answer(${JSON.stringify(second)})`)
  await ticking
  const drawn = panel()
  assert.deepEqual([drawn.items, drawn.input, drawn.context, drawn.shown, drawn.calls.slice(0, 4)],
    [['Which bound is used?'], 'A draft about a2', 'About Lemma 2', 'a2', ['answer /api/run/a2', 'pill finished · analyze', 'status a2', 'preview a2']])
  // A run chosen while a tick waits for the side files: the rest of that tick (the conversation, the run list) is left too.
  step('var release; proofWait=new Promise(resolve=>{ release=resolve; })')
  ticking = step('tick()')
  step(`answer(${JSON.stringify({ ...second, updated_at: 2 })})`)
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(panel().calls, ['answer /api/run/a2', 'pill finished · analyze', 'status a2', 'preview a2', 'proof'])
  step("selectRun('a1',null); calls.splice(0); release()")
  const going = await ticking
  assert.deepEqual(panel().calls, [])
  assert.equal(going, true)
})
