// The console's Readability mode: the fourth composer mode and its start
// request, the run view (result anchors, notation, questions and answers),
// the Paper map, and the shared and project views of a readable version.
// The real functions of console.html and readability.js run in a vm context
// with small stubs; Markdown goes through the vendored marked.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')
const css = read('../agent_monitor/web/readability.css')
const markedSource = read('../agent_monitor/web/vendor/shared/marked.min.js')

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
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
function run(names, consts, prelude, body, { marked = false, context = {} } = {}) {
  const sandbox = vm.createContext({ console, URL, Date, ...context })
  if (marked) vm.runInContext(markedSource, sandbox)
  const code = [prelude, ...consts.map(constant), ...names.map(fn), body].join('\n')
  const result = vm.runInContext(code, sandbox)
  return typeof result === 'function' ? (...args) => plain(result(...args)) : plain(result)
}
const storage = `const store={};const localStorage={getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}};`
// A small element tree: text, classes, data, attributes, children.
const miniDom = `
const make=tag=>({tag,className:'',textContent:'',innerHTML:'',type:'',title:'',hidden:false,dataset:{},attrs:{},kids:[],style:{setProperty(){}},
  setAttribute(k,v){this.attrs[k]=String(v);},append(...k){this.kids.push(...k.map(x=>typeof x==='string'?{tag:'#text',textContent:x,kids:[]}:x));},
  prepend(...k){this.kids.unshift(...k);},replaceChildren(...k){this.kids=[];this.append(...k);},querySelectorAll(){return [];},querySelector(){return null;}});
const document={createElement:make};
const flat=node=>({tag:node.tag,cls:node.className,text:node.textContent,html:node.innerHTML||undefined,data:Object.keys(node.dataset||{}).length?node.dataset:undefined,kids:(node.kids||[]).map(flat)});
const texts=node=>[node.textContent,...(node.kids||[]).flatMap(texts)].filter(Boolean);`

test('Readability is the fourth mode: its own composer, a locked engine chip and "Make readable"', () => {
  const out = run(
    ['formalizeChoice', 'renderFormalizeSwitch', 'setFormalizeChoice', 'engineServesProve', 'analyzeEngineEntry', 'analyzePolicy',
      'viewerIsAdmin', 'analyzeOffered', 'readabilityEngineEntry', 'readabilityPolicy', 'readabilityOffered', 'runKindOffered',
      'runKindChoice', 'chooseMode', 'manuscriptMode', 'syncEngineForRunKind', 'syncStartButton', 'applyRunKindComposer'],
    ['FORMALIZE_KEY', 'RUN_KIND_KEY', 'PROVE_COMPOSER', 'ANALYZE_COMPOSER', 'READABILITY_COMPOSER'],
    storage + `
const els={};
const $=id=>els[id]||(els[id]={id,hidden:false,textContent:'',placeholder:'',attrs:{},kids:[],setAttribute(k,v){this.attrs[k]=String(v);},
  replaceChildren(...k){this.kids=k;},querySelector(sel){ return $(id+' '+sel); }});
const bodyClasses=new Set();
const document={body:{classList:{toggle(c,on){ if(on)bodyClasses.add(c); else bodyClasses.delete(c); }}},querySelectorAll:()=>[],
  createElement:tag=>({tag,id:'',textContent:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;}})};
let ENGINES=[{id:'kimi',modes:['prove'],available:true},{id:'analyze',modes:['analyze'],available:true,analyze:{enabled:true}},
  {id:'readability',modes:['readability'],available:true,readability:{enabled:true}}];
let CURRENT_USER={id:4}, GUEST_POLICY=null, SETTINGS=null, engine='kimi', proveEngine='';
const calls=[], initialAttachments={refreshLimits(){ calls.push('limits'); }};
const renderAnalyzeCaps=()=>calls.push('analyze caps'), renderReadabilityCaps=()=>calls.push('readability caps'), refreshAnalyzeSource=()=>calls.push('source');
// renderEngines, reduced to what the mode changes.
const runKindChanged=()=>{ syncEngineForRunKind(); applyRunKindComposer(); };`,
    `(()=>{
  const state=()=>({engine,proveEngine,kind:runKindChoice(),verb:$('landing-verb').textContent,sub:$('landing-sub').textContent,
    placeholder:$('problem-text').placeholder,label:$('problem-text').attrs['aria-label'],start:$('start-btn').kids[0]?.textContent,
    composer:!$('analyze-composer').hidden,analyzeOptions:!$('analyze-composer .analyze-options').hidden,analyzeProvider:!$('analyze-provider').hidden,
    readabilityOptions:!$('readability-options').hidden,readabilityProvider:!$('readability-provider').hidden,body:[...bodyClasses].sort(),calls:calls.splice(0)});
  const out={};
  chooseMode('readability'); out.readability=state();
  chooseMode('analyze'); out.analyze=state();
  chooseMode('formal'); out.prove=state();
  return out;
})()`)
  assert.deepEqual(out.readability, {
    engine: 'readability', proveEngine: 'kimi', kind: 'readability', verb: 'read',
    sub: 'Paste or link a paper or proof — get a readable version that keeps every statement, a paper map, a notation table, and answers to your questions.',
    placeholder: 'Paste a proof or paper, or a link (arXiv, GitHub, openai/math, PDF/TeX)…', label: 'Paper or proof, link or text', start: 'Make readable',
    composer: true, analyzeOptions: false, analyzeProvider: false, readabilityOptions: true, readabilityProvider: true,
    body: ['readability-mode'], calls: ['limits', 'readability caps', 'source'],
  })
  // From Readability to Analyze the engine chosen for proofs is kept (never "readability").
  assert.equal(out.analyze.engine, 'analyze')
  assert.equal(out.analyze.proveEngine, 'kimi')
  assert.equal(out.analyze.start, 'Analyze')
  assert.deepEqual([out.analyze.composer, out.analyze.analyzeOptions, out.analyze.readabilityOptions], [true, true, false])
  assert.deepEqual(out.analyze.body, ['analyze-mode'])
  assert.deepEqual(out.analyze.calls, ['limits', 'analyze caps', 'source'])
  assert.deepEqual([out.prove.engine, out.prove.kind, out.prove.start, out.prove.composer, out.prove.verb, out.prove.body], ['kimi', 'prove', 'Prove', false, 'prove', []])
})

test('models: Readability offers Kimi K3 on an API route and the subscription\'s own models otherwise; no custom model', () => {
  const models = run(['modelsForEngine', 'engineModelMode', 'selectedAccountRuntime'], [],
    `let ENGINES=[], engine='readability', SETTINGS=null, CURRENT_USER={id:4}, GUEST_POLICY=null;`,
    `(runtime,settings,guest)=>{ SETTINGS={account_runtime:runtime,codex_connected:true,claude_connected:true,api_models:['kimi-k3','gpt-5','deepseek-v4'],
      codex_models:['gpt-5.6-sol','chat-latest'],claude_models:['sonnet','opus'],...settings};
      CURRENT_USER=guest?{guest:true}:{id:4}; GUEST_POLICY=guest?{default_model:'kimi-k3'}:null;
      return modelsForEngine({id:'readability',modes:['readability'],auth_modes:['codex_subscription','claude_subscription','api_key']}); }`)
  assert.deepEqual(models('api', {}), ['kimi-k3'])
  assert.deepEqual(models('api', {}, true), ['kimi-k3'])
  assert.deepEqual(models('codex', {}), ['gpt-5.6-sol'])
  assert.deepEqual(models('claude', {}), ['sonnet', 'opus'])
  // A member's listing (api_key only) never offers a subscription.
  assert.deepEqual(run(['modelsForEngine', 'engineModelMode', 'selectedAccountRuntime'], [],
    `let ENGINES=[], SETTINGS={account_runtime:'codex',codex_connected:true,codex_models:['gpt-5.6-sol']}, CURRENT_USER={id:4}, GUEST_POLICY=null;`,
    `modelsForEngine({id:'readability',auth_modes:['api_key']})`), [])
  assert.match(fn('renderModels'), /selected\?\.id!=='kimi'&&selected\?\.id!=='readability'&&mode==='api_key'/)
  assert.match(fn('runTuningCapabilities'), /engine==='analyze'\|\|engine==='readability'\?'math_harness':engine/)
})

test('the start request: run_kind, engine, audience, focus and exactly one source', async () => {
  const posts = [], selected = []
  const els = {}
  let source = { kind: 'paste', text: '\\begin{theorem}x\\end{theorem}', format: 'tex' }, sponsoredOnly = true, reply = { ok: true, status: 200, body: { run_id: 'r1', job_id: 'j1' } }
  const context = vm.createContext({
    console, setTimeout,
    $: id => (els[id] ||= { id, style: {}, disabled: false, textContent: '', className: '', value: '', checked: false }),
    initialAttachments: { busy: false, files: [], lock(busy) { this.busy = busy }, clear() {}, payload: async () => [{ name: 'paper.tex', data: 'eA==' }] },
    readabilityEngineEntry: () => ({ available: true, label: 'Ansatz Readability' }), readabilityPolicy: () => ({ enabled: true }),
    analyzeSourceFromComposer: () => source, analyzeResolveCache: new Map(), analyzeResolveTimer: 0, clearTimeout() {},
    readabilitySponsoredOnly: () => sponsoredOnly, engineRuntimeIssue: () => '', currentModel: () => 'kimi-k3',
    selectedAccountRuntime: () => 'api', runtimeDisplayName: () => '', setPill() {}, showEngineSignIn() {},
    currentRunTuning: () => ({ reasoning_effort: 'high', speed_mode: 'standard' }), runAuthRoute: () => 'api_key',
    RUN_KIND_BY_ID: new Map(), selectRun: rid => selected.push(rid), goSession() {}, loadEngines: async () => {}, limitMessage: m => m + ' (limit)',
    GUEST_POLICY: null, applyGuestUi() {},
  })
  context.resolveAnalyzeLink = link => new Promise(resolve => setTimeout(() => { context.analyzeResolveCache.set(link, { ok: true }); resolve() }, 20))
  context.fetch = async (url, options) => { posts.push([url, JSON.parse(options.body)]); return { ok: reply.ok, status: reply.status, headers: { get: () => null }, json: async () => reply.body } }
  vm.runInContext('let analyzeLinkLookupPending=false;\n' + fn('readabilityAudience') + '\n' + fn('startReadabilityRun'), context)
  context.$('readability-audience').value = 'graduate'
  context.$('readability-focus').value = '  the proof of Theorem 2 '
  await context.startReadabilityRun()
  assert.deepEqual(posts.at(-1), ['/api/runs', { run_kind: 'readability', engine: 'readability', model: null, auth_route: 'api_key', reasoning_effort: 'high', speed_mode: 'standard',
    readability: { audience: 'graduate', focus: 'the proof of Theorem 2' }, source: { text: '\\begin{theorem}x\\end{theorem}', format: 'tex' } }])
  assert.equal(context.RUN_KIND_BY_ID.get('r1'), 'readability')
  assert.deepEqual(selected, ['r1'])
  // A member's own model; a link is looked up once before it starts (Enter pressed twice starts one run).
  sponsoredOnly = false; source = { kind: 'url', url: 'https://arxiv.org/abs/2401.00001' }
  context.$('readability-audience').value = 'everyone'; context.$('readability-focus').value = ''
  await Promise.all([context.startReadabilityRun(), context.startReadabilityRun()])
  assert.equal(posts.length, 2)
  assert.deepEqual(posts.at(-1)[1], { run_kind: 'readability', engine: 'readability', model: 'kimi-k3', auth_route: 'api_key', reasoning_effort: 'high', speed_mode: 'standard',
    readability: { audience: 'graduate' }, source: { url: 'https://arxiv.org/abs/2401.00001' } })
  // Uploaded files are the manuscript.
  source = { kind: 'upload' }
  await context.startReadabilityRun()
  assert.deepEqual(posts.at(-1)[1].attachments, [{ name: 'paper.tex', data: 'eA==' }])
  assert.equal('source' in posts.at(-1)[1], false)
  // A refusal starts nothing and says why.
  reply = { ok: false, status: 429, body: { error: 'Guests can make 1 readable version per hour' } }
  await context.startReadabilityRun()
  assert.equal(context.$('form-error').textContent, 'Guests can make 1 readable version per hour (limit)')
  assert.deepEqual(selected, ['r1', 'r1', 'r1'])
  // Refused before any request: an engine that needs setup.
  context.readabilityEngineEntry = () => ({ available: false, label: 'Ansatz Readability', health_detail: 'pypdf is missing' })
  await context.startReadabilityRun()
  assert.equal(context.$('form-error').textContent, 'Ansatz Readability needs setup — pypdf is missing')
  assert.equal(posts.length, 4)
})

test('result markers become anchor badges: id-safe, labelled, never DOM ids; raw HTML stays text', () => {
  const render = run(['stashMath', 'escapeHtml', 'readableMarkdownHtml', 'readableAnchorHtml', 'readableRefHtml'], ['READABLE_MARKER_LINE', 'READABLE_RESULT_LINK'],
    `const window=globalThis; let readableMarkdownParser=null; const sanitizeRenderedHtml=html=>{ sanitized.push(html); return html; }; const sanitized=[];`,
    `(text,labels)=>({html:readableMarkdownHtml(text,new Map(labels)),sanitized:sanitized.splice(0)})`, { marked: true })
  const doc = [
    '# Short proofs — Readable version', '',
    '> **Readable version** for graduate students, written by kimi-k3 via sponsored Kimi K3.', '',
    '[part:1]: #', '',
    '## 1 Introduction', '',
    '[result:thm:main]: #', '',
    '**Theorem 1.1.** For all $x<y$ we have <img src=x onerror=alert(1)>.', '',
    '[result:Lem:X]: #', '',
    '[result:a"b]: #', '',
    '[result:lem bad]: #', '',
    'See [[result:thm:main]].',
  ].join('\n')
  const { html: out, sanitized } = render(doc, [['thm:main', 'Theorem 1.1']])
  assert.match(out, /<p class="rd-anchor-row"><span class="rd-anchor" data-rd-anchor="thm:main" tabindex="-1" title="Theorem 1.1 · thm:main">Theorem 1\.1<\/span><\/p>/)
  // Ids are lower case (as the runner writes them) and escaped; no badge carries a DOM id.
  assert.match(out, /data-rd-anchor="lem:x"[^>]*>lem:x</)
  assert.match(out, /data-rd-anchor="a&quot;b" tabindex="-1" title="a&quot;b">a&quot;b</)
  assert.doesNotMatch(out, /\sid="/)
  // A marker the runner never writes (a space in its id) and the part marks stay invisible.
  assert.doesNotMatch(out, /lem bad|\[part:1\]|\[result:/)
  // Raw HTML is shown as text; math is kept as escaped TeX for KaTeX.
  assert.doesNotMatch(out, /<img/)
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(out, /\$x&lt;y\$/)
  // The badges are added after the sanitizer ran on the rendered Markdown.
  assert.equal(sanitized.length, 1)
  assert.doesNotMatch(sanitized[0], /rd-anchor|<button/)
  assert.match(sanitized[0], /@@RDANCHOR0@@/)
})

test('the readable version marks its provenance note and "⚠ Check" notes', () => {
  const marks = run(['markReadableBlocks'], [], `
const block=(tagName,text)=>({tagName,textContent:text,classes:new Set(),attrs:{},classList:{add(c){this.owner.classes.add(c);}},setAttribute(k,v){this.attrs[k]=v;}});
const elements=[block('H1','Title'),block('BLOCKQUOTE','Readable version for graduate students'),block('P','x'),block('BLOCKQUOTE','⚠ Check: the independent check found problems'),
  block('BLOCKQUOTE','Not rewritten (budget).'),block('BLOCKQUOTE','Reader’s note: a step is missing'),block('BLOCKQUOTE','A quotation')];
elements.forEach((el,i)=>{ el.classList.owner=el; el.nextElementSibling=elements[i+1]; });
const doc={firstElementChild:elements[0],querySelectorAll:()=>elements.filter(el=>el.tagName==='BLOCKQUOTE')};`,
  `(()=>{ markReadableBlocks(doc); return elements.map(el=>[...el.classes].join(' ')+(el.attrs.role?'|'+el.attrs.role:'')); })()`)
  assert.deepEqual(marks, ['', 'rd-provenance', '', 'rd-check|note', 'rd-note', 'rd-note', ''])
  // The console's own banner says what the check means when the document has none.
  const banner = run(['readabilityCounts', 'readabilityProvenanceText'], [], '',
    `[readabilityProvenanceText({readability:{summary:{parts:6,faithful:5,issues:1,unchecked:0}}}),readabilityProvenanceText({sandbox_preview:{parts:{parts:2,faithful:2,issues:0,unchecked:0}}})]`)
  assert.match(banner[0], /5 of 6 parts faithful, 1 with issues \(marked ⚠\), 0 unchecked\. Not a certificate of correctness\.$/)
  assert.match(banner[1], /2 of 2 parts faithful, 0 with issues/)
})

test('notation: symbols are typeset by KaTeX without trust; meanings and places are set as text', () => {
  const view = run(['analyzeEl', 'dagDetailText', 'readabilityNotation', 'readabilityTex', 'readabilityTable', 'readabilityWhereCell', 'renderReadabilityNotation'], [],
    miniDom + `
const typeset=[]; const window={katex:{render(tex,el,options){ typeset.push([tex,options]); el.textContent='KATEX:'+tex; }}};
const readabilityMacros=()=>({'\\\\R':'\\\\mathbb{R}'}), renderMathIn=()=>{};`,
    `(run=>{ const view=make('div'); renderReadabilityNotation(run,view); return {doc:flat(view.kids[0]),typeset,texts:texts(view)}; })`)
  const out = view({ run_id: 'r1', status: 'finished', sandbox_preview: { notation: {
    rows: [{ symbol: '\\alpha_n<script>', meaning: '<img src=x onerror=alert(1)> a $\\mathbb{R}$-valued sequence', where: '§2 <b>bold</b>', id: 'def:alpha' }, 'junk', null],
    terms: [{ term: 'Glow', meaning: 'the "<i>" property', where: 'Definition 2.1' }],
  } } })
  assert.equal(out.typeset.length, 1)
  assert.equal(out.typeset[0][0], '\\alpha_n<script>')
  assert.equal(out.typeset[0][1].trust, false)
  assert.equal(out.typeset[0][1].throwOnError, false)
  assert.deepEqual(out.typeset[0][1].macros, { '\\R': '\\mathbb{R}' })
  // Model text arrives as text nodes, never markup.
  assert.ok(out.texts.includes('<img src=x onerror=alert(1)> a $\\mathbb{R}$-valued sequence'))
  assert.ok(out.texts.includes('§2 <b>bold</b>'))
  assert.ok(out.texts.includes('the "<i>" property'))
  assert.deepEqual(out.texts.filter(text => ['Symbol', 'Term', 'Meaning', 'Where introduced', 'Notation', 'Terms'].includes(text)),
    ['Notation', 'Symbol', 'Meaning', 'Where introduced', 'Terms', 'Term', 'Meaning', 'Where introduced'])
  const json = JSON.stringify(out.doc)
  assert.match(json, /"cls":"rd-ref","text":"Read","data":\{"rdRef":"def:alpha"\}/)
  for (const name of ['renderReadabilityNotation', 'readabilityWhereCell', 'readabilityTable', 'readabilityTex', 'renderReadabilityMap', 'readabilityMapRow', 'renderPaperMapDetail', 'renderReadabilityAskContext', 'renderReadabilityOriginal', 'readabilityNumberedLines'])
    assert.doesNotMatch(fn(name), /innerHTML|insertAdjacentHTML|outerHTML/, name)
  // Without KaTeX the symbol is plain text.
  const fallback = run(['readabilityTex'], [], 'const window={};', `(()=>{ const el={textContent:''}; readabilityTex(el,'\\\\beta<b>',{}); return el.textContent; })()`)
  assert.equal(fallback, '\\beta<b>')
})

test('answers: sanitized Markdown with result links; the question and the passage stay text', () => {
  const item = run(['analyzeEl', 'dagDetailText', 'stashMath', 'escapeHtml', 'readableMarkdownHtml', 'readableAnchorHtml', 'readableRefHtml', 'readabilityAskRunning', 'readabilityAskItem'],
    ['READABLE_MARKER_LINE', 'READABLE_RESULT_LINK'],
    miniDom + `
const window=globalThis; let readableMarkdownParser=null; const sanitized=[];
const sanitizeRenderedHtml=html=>{ sanitized.push(html); return html; }; const fmtTs=()=>'10:42';`,
    `((entry,labels)=>{ const li=readabilityAskItem(entry,new Map(labels)); return {li:flat(li),texts:texts(li),sanitized:sanitized.splice(0)}; })`, { marked: true })
  const labels = [['thm:main', 'Theorem 1.1']]
  const done = item({ ask_id: 'a1', status: 'done', model: 'kimi-k3', finished_at: '2026-10-22T10:42:00Z', node_id: 'thm:main', question: '<b>Why</b> does $x<y$ hold?', selection: '<script>alert(1)</script> the passage',
    answer: 'By [[result:thm:main]] and [[ result : NOPE ]]: <iframe src=x></iframe> $a<b$ and [x](javascript:alert(1)).' }, labels)
  const answer = done.li.kids.find(kid => kid.cls === 'ask-a md-doc').html
  assert.match(answer, /<button type="button" class="rd-ref" data-rd-ref="thm:main" title="Show Theorem 1\.1 in the readable version">Theorem 1\.1<\/button>/)
  // A result the paper map does not list is plain text, never a link.
  assert.match(answer, /and nope:/)
  assert.doesNotMatch(answer, /data-rd-ref="nope"|<iframe/)
  assert.match(answer, /&lt;iframe src=x&gt;&lt;\/iframe&gt;/)
  assert.match(answer, /\$a&lt;b\$/)
  // The answer went through the console's sanitizer (which drops javascript: links) before the buttons were added.
  assert.equal(done.sanitized.length, 1)
  assert.match(done.sanitized[0], /javascript:alert/)
  assert.doesNotMatch(done.sanitized[0], /<button/)
  assert.ok(done.texts.includes('<b>Why</b> does $x<y$ hold?'))
  assert.ok(done.texts.includes('<script>alert(1)</script> the passage'))
  assert.ok(done.texts.includes('About Theorem 1.1'))
  assert.ok(done.texts.includes('kimi-k3 · 10:42'))
  // In flight, and failed.
  const pending = item({ ask_id: 'a2', status: 'running', question: 'q' }, labels)
  assert.ok(pending.texts.includes('Answering…'))
  assert.match(pending.li.cls, /pending/)
  const failed = item({ ask_id: 'a3', status: 'failed', question: 'q', error: 'The provider refused <b>it</b>' }, labels)
  assert.ok(failed.texts.includes('✗ The provider refused <b>it</b>'))
  assert.match(failed.li.cls, /failed/)
})

test('questions: one in flight at a time, the one just sent shown at once, and guests see how many are left', () => {
  const ask = run(['readabilityAskRunning', 'readabilityAsks', 'readabilityAskAllowance', 'syncReadabilityAskCompose'], [], `
const readabilityAskSent=new Map(); let readabilityAskSending=false, guest=false;
const els={}; const $=id=>els[id]||(els[id]={id,disabled:false,title:'',textContent:''});
const viewerIsGuest=()=>guest, readabilityPolicy=()=>({asks:{per_run:3}});`,
  `((op,run,arg)=>{
    if(op==='asks'){ readabilityAskSent.clear(); if(arg)readabilityAskSent.set(run.run_id,arg); return readabilityAsks(run).map(item=>item.ask_id+':'+item.status); }
    guest=arg; syncReadabilityAskCompose(run,readabilityAsks(run)); return [$('ask-send').disabled,$('ask-send').title,$('ask-hint').textContent];
  })`)
  ask.asks = (record, sent) => ask('asks', record, sent)
  ask.compose = (record, isGuest) => ask('compose', record, isGuest)
  const published = { run_id: 'r1', status: 'finished', sandbox_preview: { has_readable: true } }
  const record = { ...published, readability: { asks: [{ ask_id: 'a1', status: 'done', answer: 'x' }, { ask_id: 'a2', status: 'running' }], ask: { ask_id: 'a2', status: 'running' } } }
  assert.deepEqual(ask.asks(record), ['a1:done', 'a2:running'])
  // Sent, but the record does not show it yet: shown as in flight; once the record shows one in flight, never twice.
  const sent = { ask_id: 'a9', status: 'running', question: 'q', sent_at: Date.now() }
  assert.deepEqual(ask.asks(published, sent), ['a9:running'])
  assert.deepEqual(ask.asks({ ...published, readability: { ask: { ask_id: 'a9', status: 'running' } } }, sent), ['a9:running'])
  assert.deepEqual(ask.asks(published, { ...sent, sent_at: Date.now() - 60000 }), [])
  assert.deepEqual(ask.compose(record, false), [true, 'One question at a time — the answer is on its way.', 'One question at a time — the answer is on its way.'])
  const answered = { ...published, readability: { asks: [{ ask_id: 'a1', status: 'done', answer: 'x' }] } }
  assert.deepEqual(ask.compose(answered, false), [false, '', 'Enter to ask · Shift+Enter newline'])
  assert.deepEqual(ask.compose(answered, true), [false, '', '2 of 3 guest questions left · Enter to ask'])
  const spent = { ...published, readability: { asks_started: 3, asks: ['a1', 'a2'].map(ask_id => ({ ask_id, status: 'done', answer: 'x' })) } }
  assert.match(ask.compose(spent, true)[1], /^Guests can ask 3 questions about a readable version — sign in to ask more\.$/)
  assert.equal(ask.compose(spent, true)[0], true)
  assert.deepEqual(ask.compose({ run_id: 'r1', status: 'running', sandbox_preview: {} }, false).slice(0, 2), [true, 'You can ask once the readable version is published.'])
  // The run is polled while a question is in flight, and its signature follows the questions.
  assert.match(consoleScript, /run\.readability\?\.ask\?\.status==='running';/)
  assert.match(consoleScript, /run\.readability\?\.ask\?\.status,run\.readability\?\.ask\?\.ask_id,Array\.isArray\(run\.readability\?\.asks\)\?run\.readability\.asks\.length:0,/)
  // The request names the question, the passage, its view and the result asked about.
  const send = fn('sendReadabilityAsk')
  assert.match(send, /fetch\('\/api\/run\/'\+encodeURIComponent\(rid\)\+'\/readability\/ask',\{method:'POST'/)
  assert.match(send, /const request=\{question,selection:String\(context\.selection\|\|''\)\.slice\(0,4000\),view:context\.view==='original'\?'original':'readable'\};/)
  assert.match(send, /if\(context\.node_id\)request\.node_id=String\(context\.node_id\);/)
})

test('a readable version opens on its tabs and Ask panel, with no editing, Lean or proof graphs', () => {
  const view = run(['applyRunKindView', 'syncDagModeForRun'], ['LEAN_ANALYSIS_STAGE_NAMES'], `
const els={}; const $=id=>els[id]||(els[id]={id,hidden:false,textContent:'',placeholder:'',dataset:{},attrs:{},disabled:false,setAttribute(k,v){this.attrs[k]=String(v);}});
const bodyClasses=new Set(), modes={};
for(const mode of ['analysis','paper-map','engine','informal','formal','compiler'])modes[mode]={hidden:mode==='analysis'||mode==='paper-map'};
const document={body:{classList:{toggle(c,on){ if(on)bodyClasses.add(c); else bodyClasses.delete(c); }}},
  querySelector:sel=>{ const m=/data-mode="([a-z-]+)"/.exec(sel); return m?modes[m[1]]:sel.includes('data-view="lean"')?$('lean-tab'):null; },
  querySelectorAll:sel=>sel.includes('#dag-mode')?[...sel.matchAll(/data-mode="([a-z-]+)"/g)].map(m=>modes[m[1]]):[]};
let dagMode='engine', dagAutoRunId='', workspaceView='lean'; const switched=[];
const setDagMode=(mode)=>{ dagMode=mode; switched.push(mode); }, setWorkspaceView=v=>{ workspaceView=v; switched.push('view:'+v); };
const ANALYZE_CONTINUE_NOTE='analyze note', READABILITY_ASK_NOTE='Questions are answered by a model from this paper — not a certificate.';`,
  `(kind=>{ applyRunKindView(kind,'r1'); return {body:[...bodyClasses],chat:$('chat-title').textContent,proof:$('proof-title').textContent,
    shown:['readability-tabs','ask-list','ask-compose'].filter(id=>!$(id).hidden),selPop:$('sel-pop').textContent,stages:$('analysis-stages').attrs['aria-label'],
    modes:Object.keys(modes).filter(mode=>!modes[mode].hidden),dagMode,switched:switched.splice(0),workspaceView,source:[$('analysis-source-link').hidden,$('analysis-source-toggle').hidden]}; })`)
  const readable = view('readability')
  assert.deepEqual(readable.body, ['readability-run'])
  assert.equal(readable.chat, 'Ask about this paper')
  assert.equal(readable.proof, 'Readability')
  assert.deepEqual(readable.shown, ['readability-tabs', 'ask-list', 'ask-compose'])
  assert.equal(readable.selPop, 'Ask about this')
  assert.equal(readable.stages, 'Readability stages')
  assert.deepEqual(readable.modes, ['paper-map'])
  assert.equal(readable.dagMode, 'paper-map')
  // The Lean view a proof run had open gives way to the Sandbox.
  assert.deepEqual(readable.switched, ['view:sandbox', 'paper-map'])
  const proof = view('prove')
  assert.deepEqual(proof.body, [])
  assert.deepEqual(proof.shown, [])
  assert.equal(proof.selPop, '💬 Add to chat')
  assert.deepEqual(proof.modes, ['engine', 'informal', 'formal', 'compiler'])
  assert.equal(proof.dagMode, 'engine')
  assert.deepEqual(proof.source, [true, true])
  const analysis = view('analyze')
  assert.deepEqual(analysis.body, ['analysis-run'])
  assert.deepEqual(analysis.modes, ['analysis', 'informal', 'formal', 'compiler'])
  // What a readable version hides, and its tab strip and Ask panel, are in readability.css.
  assert.match(css, /\.readability-run #proof-edit-btn,[^{]*\.readability-run \.chat-msgs, \.readability-run \.chat-compose, \.readability-run #sess-cont,[^{]*\.readability-run \.seg-control button\[data-view="lean"\] \{ display: none !important; \}/)
  assert.match(html, /<div class="readability-tabs" id="readability-tabs" role="tablist" aria-label="Readability views" hidden>/)
  assert.deepEqual([...html.matchAll(/role="tab" data-rd-view="([a-z]+)"[^>]*>([^<]+)</g)].map(m => [m[1], m[2]]),
    [['readable', 'Readable version'], ['original', 'Original'], ['notation', 'Notation'], ['map', 'Map']])
  assert.match(html, /<div class="ask-compose run-change" id="ask-compose" hidden>/)
  assert.match(html, /<script id="readability-loader">\{const base=window\.__PC_BASE__\|\|""[^<]*"\/static\/readability\.css"[^<]*"\/static\/readability\.js"[^<]*<\/script>/)
})

test('the run list tags a readable version, keeps its title and never offers Continue; its questions are not runs', async () => {
  const replies = {
    '/api/jobs': { jobs: [{ run_id: 'r2', job_id: 'j2', engine: 'kimi', status: 'finished', problem_preview: 'Prove it' },
      { run_id: 'r1', job_id: 'j1', engine: 'readability', run_kind: 'readability', status: 'finished', problem_preview: 'Readability · arXiv:2401.00001' },
      { run_id: 'r1', job_id: 'j3', kind: 'ask', engine: 'readability', status: 'running', problem_preview: 'Ask · why?' }] },
    '/api/runs': { runs: [{ run_id: 'r1', run_kind: 'readability', engine: 'readability', status: 'finished', problem_preview: 'Readability · Short proofs' }] },
  }
  const context = vm.createContext({ console })
  vm.runInContext(`
const RUN_KIND_BY_ID=new Map(); let runId='r1', CURRENT_USER={id:4};
const box={innerHTML:'',querySelectorAll:()=>[]}; const $=()=>box;
const fetch=async url=>({json:async()=>replies[url]});
const syncIdlePill=()=>{}, runningRunCount=()=>0;
${fn('escapeHtml')}
${fn('loadRuns')}`, Object.assign(context, { replies }))
  await vm.runInContext('loadRuns()', context)
  const listed = vm.runInContext('box.innerHTML', context)
  assert.equal((listed.match(/data-run="r1"/g) || []).length, 2)  // the item and its delete button: no Continue, no Stop
  assert.match(listed, /title="Readability · Short proofs"/)
  assert.match(listed, /<div class="st"><span class="run-kind">Readable<\/span>answering a question<\/div>/)
  assert.doesNotMatch(listed.split('data-run="r1"')[1], /run-act cont/)
  assert.match(listed, /run-act cont" data-run="r2"/)
  assert.doesNotMatch(listed, /Ask · why\?/)
  assert.equal(vm.runInContext("RUN_KIND_BY_ID.get('r1')", context), 'readability')
  assert.equal(vm.runInContext("RUN_KIND_BY_ID.get('r2')", context), 'prove')
})

test('Paper map: shaded by importance, definitions as boxes, results rounded; proof uses solid, statement uses dashed', () => {
  const visual = run(['dagLookup', 'dagKindColor', 'paperMapImportance', 'paperMapNodeVisual', 'dagNodeVisual'], ['DAG_KIND_COLOR', 'PAPER_MAP_BOXED_KINDS'], '',
    `(n=>dagNodeVisual(n,{role:'paper-map'}))`)
  const definition = visual({ id: 'def:x', kind: 'definition', importance: 5, main: true })
  assert.deepEqual(definition.classes, ['paper-map', 'imp-5', 'pm-definition', 'pm-main'])
  assert.equal(definition.badge, 'definition')
  assert.equal(definition.color, '#64748b')
  assert.deepEqual(definition.status, { label: 'importance 5', tone: 'pm' })
  assert.deepEqual(definition.chips.map(chip => chip.text), ['main result'])
  assert.deepEqual(visual({ id: 'lem', kind: 'lemma', importance: '2' }).classes, ['paper-map', 'imp-2', 'pm-result'])
  // Importance outside 1..5 (or not a number) is the lightest; a model-chosen kind is only text.
  for (const importance of [0, 6, 'x', null, '__proto__']) assert.ok(visual({ kind: 'lemma', importance }).classes.includes('imp-1'))
  assert.equal(visual({ kind: 'constructor' }).badge, 'constructor')
  const edge = run(['dagLookup', 'paperMapEdge'], ['PAPER_MAP_EDGE_BASIS'], '', 'paperMapEdge')
  assert.deepEqual(edge({ kind: 'proof', provenance: 'ref' }), { cls: 'e-proof', title: 'Used in the proof · a reference in the manuscript' })
  assert.deepEqual(edge({ kind: 'statement', provenance: 'model' }), { cls: 'e-statement e-model', title: 'Used in the statement · proposed by the reading guide' })
  assert.deepEqual(edge({ provenance: 'toString' }), { cls: 'e-proof', title: 'Used in the proof · a reference in the manuscript' })
  // The console draws them with those classes and titles.
  const svg = run(['escapeHtml', 'dagLookup', 'dagEdgeGeometry', 'paperMapEdge', 'dagEdgeSvg'], ['CGM_EDGE_CLASS', 'PAPER_MAP_EDGE_BASIS'], '',
    `(()=>{ const box=(x,y)=>({x,y,w:200,h:60}); const pos=new Map([['a',box(0,0)],['b',box(0,140)],['c',box(240,140)]]);
      return dagEdgeSvg([{edge:{from:'a',to:'b',kind:'proof',provenance:'ref'},curveOffset:0,labelOffset:0},{edge:{from:'a',to:'c',kind:'statement',provenance:'inferred'},curveOffset:0,labelOffset:0}],pos,{paperMap:true,labels:false}).html; })()`)
  assert.match(svg, /<path class="dag-edge e-proof" d="[^"]+" marker-end="url\(#dag-arrow\)"><title>Used in the proof · a reference in the manuscript<\/title><\/path>/)
  assert.match(svg, /<path class="dag-edge e-statement" [^>]*><title>Used in the statement · inferred from the text<\/title>/)
  assert.match(css, /\.dag-stage\.paper-map-graph \.dag-edge\.e-statement \{ stroke-dasharray: 6 4; \}/)
  assert.match(css, /\.dag-node\.paper-map\.pm-definition \{ border-radius: 3px; \}/)
  assert.match(css, /\.dag-node\.paper-map\.pm-result \{ border-radius: 16px; \}/)
  // The legend and the status line name what the shading means; text is escaped.
  const legend = run(['escapeHtml', 'paperMapLegendHtml'], [], '', 'paperMapLegendHtml()')
  assert.match(legend, /sw pm-sw imp-1"><\/span>routine.*sw pm-sw imp-5"><\/span>central/)
  assert.match(legend, /ln pm-ln dash"><\/span>used in a statement/)
  const status = run(['dagDetailText', 'paperMapStatusText'], [], '', `paperMapStatusText({graph:{title:'Short proofs',nodes:[{},{}],edges:[{}]}})`)
  assert.equal(status, 'Paper map · Short proofs · 2 definitions and results · 1 dependencies')
  const title = run(['dagDetailText', 'dagMathSafeClip', 'analysisTextAccents', 'paperMapCardTitle'], ['ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS'], '', 'paperMapCardTitle')
  assert.equal(title({ id: 'lem:a', label: 'Lemma 3.2', title: 'Erd\\H{o}s bound' }), 'Lemma 3.2 · Erdős bound')
  assert.equal(title({ id: 'lem:a', label: '', title: '' }), 'lem:a')
  // \ref{<id>} reads as the result's number.
  const refs = run(['dagDetailText', 'paperMapRefLabels'], [], '', `[...paperMapRefLabels({nodes:[{id:'lem:a',label:'Lemma 3.2'},{id:'def:b',label:'Definition'}]})]`)
  assert.deepEqual(refs, [['lem:a', { number: '3.2', name: 'Lemma 3.2' }], ['def:b', { number: 'Definition', name: 'Definition' }]])
})

test('Paper map shading has a token per level in both themes, and its text stays readable on the darkest card', () => {
  const tokens = selector => Object.fromEntries([...css.split(selector)[1].split('}')[0].matchAll(/--([a-z0-9-]+):(#[0-9a-f]{6})/gi)].map(m => [m[1], m[2]]))
  const dark = tokens(':root, [data-theme="dark"] {'), light = tokens('[data-theme="light"] {')
  for (const theme of [dark, light]) for (let level = 1; level <= 5; level++) assert.ok(theme['pm-fill-' + level] && theme['pm-edge-' + level], 'level ' + level)
  const rgb = hex => [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16) / 255)
  const lum = c => { const f = v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; const [r, g, b] = c.map(f); return 0.2126 * r + 0.7152 * g + 0.0722 * b }
  const ratio = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05) }
  // Statement text and the console's text colours (clear palette) on the darkest fill.
  assert.ok(ratio(light['pm-text'], light['pm-fill-5']) >= 4.5)
  assert.ok(ratio('#17212f', light['pm-fill-5']) >= 4.5)
  assert.ok(ratio(dark['pm-text'], dark['pm-fill-5']) >= 4.5)
  assert.ok(ratio('#f6f8fc', dark['pm-fill-5']) >= 4.5)
  // Darker means more important: each light fill is darker than the one before.
  for (let level = 2; level <= 5; level++) assert.ok(lum(rgb(light['pm-fill-' + level])) < lum(rgb(light['pm-fill-' + (level - 1)])))
  // The DAG mode follows the analysis mode, and only reloads.
  assert.match(fn('setDagMode'), /\['engine','informal','formal','compiler','analysis','paper-map'\]\.includes\(mode\)/)
  assert.match(fn('dagRebuildReloadsOnly'), /dagMode==='paper-map'\)return true;/)
  assert.match(fn('refreshDagView'), /requestedMode==='paper-map'\?'paper-map'/)
})

test('progress: the runner\'s notes as stages, never model replies, call markers or the banner', () => {
  const lines = run(['readabilityProgressLines'], ['READABILITY_CALL_MARKER'], '', 'readabilityProgressLines')
  const raw = ['Ansatz Readability · kimi-k3 via sponsored_kimi · up to 6 parts', 'Readability · 1 Ingest · link', 'Readability · Ingest · reading order: 4 parts',
    'Readability · guide', '{"title": "x"}', '=== READABLE ===', '```json', 'Readability · rewrite-2-low', 'Readability · recheck-3', 'assistant · {"verdict": "faithful"}',
    'Readability · 3 Rewrite · rewriting 4 of 4 parts', 'Readability · Rewrite · now working on Part 1, Part 2', 'Ansatz Readability completed · 4 parts rewritten'].join('\n')
  assert.deepEqual(lines(raw), ['1 Ingest · link', 'Ingest · reading order: 4 parts', '3 Rewrite · rewriting 4 of 4 parts', 'Rewrite · now working on Part 1, Part 2'])
  const detail = run(['readabilityProgressDetail'], ['READABILITY_STAGE_LINE', 'ANALYZE_SOURCE_WORDS'], '', 'readabilityProgressDetail')
  assert.equal(detail(['1 Ingest · link']), 'from a link')
  assert.equal(detail(['Rewrite · now working on Part 1']), 'now working on Part 1')
  assert.equal(detail(['Ansatz Readability could not start: no Kimi key']), 'Ansatz Readability could not start: no Kimi key')
  const view = run(['analyzeEl', 'readabilityProgressView'], ['READABILITY_STAGE_LINE', 'ANALYZE_SOURCE_WORDS'], miniDom, `((lines,status)=>flat(readabilityProgressView(lines,{status})))`)
  const live = view(['1 Ingest · pasted text', 'Ingest · reading order: 4 parts', '3 Rewrite', 'Rewrite · now working on Part 1'], 'running')
  assert.deepEqual(live.kids.map(row => [row.cls, row.kids.map(kid => kid.text).join(' | ') || row.text]),
    [['ap-stage', 'Ingest | from pasted text'], ['ap-line', 'Reading order: 4 parts'], ['ap-stage', 'Rewrite'], ['ap-now', 'Now working on Part 1']])
  assert.equal(view(['3 Rewrite', 'The source could not be used: x'], 'failed').kids.at(-1).cls, 'ap-error')
})

test('the shared viewer shows a readable version with its notation and paper map; sharing and projects name it', () => {
  const require = createRequire(import.meta.url)
  const shared = require('../agent_monitor/web/run-viewer-readability.js')
  const parts = shared.parts({ kind: 'readability', proof: '# T', readability: { notation: { rows: [{ symbol: 'x' }, 'junk'], terms: [{ term: 't' }] },
    paper_map: { nodes: [{ id: 'a' }, null], edges: [{}, {}], macros: { '\\R': '\\mathbb{R}', '\\bad': '\\href{javascript:x}{y}' } } } })
  assert.deepEqual(parts, { readable: '# T', rows: [{ symbol: 'x' }], terms: [{ term: 't' }], nodes: [{ id: 'a' }], edges: 2, macros: { '\\R': '\\mathbb{R}' } })
  assert.equal(shared.parts({ readable: 'R', notation: [{ symbol: 'y' }], paper_map: { nodes: [{ id: 'b' }] } }).rows[0].symbol, 'y')
  assert.equal(shared.withAnchorTokens('a\n[result:Thm:Main]: #\nb\n[result:x y]: #'), 'a\n\n⟦result:thm:main⟧\n\nb\n[result:x y]: #')
  assert.deepEqual([3, '5', 9, 'x'].map(importance => shared.importance({ importance })), [3, 5, 1, 1])
  assert.equal(shared.sectionLabel([{ number: '2', title: 'Proofs' }]), '2 Proofs')
  const viewer = read('../agent_monitor/web/run-viewer.js'), page = read('../agent_monitor/web/run-viewer.html')
  assert.match(viewer, /readable = data\.kind === 'readability';/)
  assert.match(viewer, /window\.AnsatzSharedReadability\.render\(data, \{ target: \$\('rv-proof-text'\), renderText \}\)/)
  assert.match(page, /<script defer src="__ASSET_BASE__\/run-viewer-readability\.js"><\/script>\s*<script defer src="__ASSET_BASE__\/run-viewer\.js"><\/script>/)
  const sharing = read('../agent_monitor/web/sharing.js')
  assert.match(sharing, /readability:'Anyone with the link can view a read-only snapshot of the readable version, its notation table and its paper map/)
  assert.match(sharing, /The original manuscript, the questions asked about it, account settings/)
  assert.match(sharing, /run\.run_kind==='readability'\|\|run\.engine==='readability'\?'readability'/)
  assert.match(read('../agent_monitor/web/projects.js'), /tag\.textContent=readable\?'Readable':'Analyze'/)
})
