// The console's Analyze mode: three-way mode switch, Problems hand-off parsing,
// Manuscript-DAG verdict colours (never green) and report escaping. The real
// console functions run in a vm context with small stubs.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const html = readFileSync(new URL('../agent_monitor/web/console.html', import.meta.url), 'utf8')
const script = html.split('<script>').at(-1).split('</script>')[0]

// Minimal JS scanner: skips strings, template literals, comments and regex
// literals, so braces inside them never end a function early.
function scanEnd(source, start, { stopAtSemicolon = false } = {}) {
  const keywords = /(?:^|[^\w$])(?:return|typeof|case|do|else|in|of|new|delete|void|throw|yield|await)$/
  let depth = 0, i = start, last = ''
  const modes = []
  while (i < source.length) {
    const c = source[i], n = source[i + 1]
    const mode = modes.at(-1)
    if (mode === 'template') {
      if (c === '\\') { i += 2; continue }
      if (c === '`') { modes.pop(); i++; last = '`'; continue }
      if (c === '$' && n === '{') { modes.push(depth); depth = 0; i += 2; last = '{'; continue }
      i++; continue
    }
    if (c === '/' && n === '/') { i = source.indexOf('\n', i); if (i < 0) return source.length; continue }
    if (c === '/' && n === '*') { i = source.indexOf('*/', i + 2) + 2; continue }
    if (c === '"' || c === "'") {
      i++
      while (i < source.length && source[i] !== c) i += source[i] === '\\' ? 2 : 1
      i++; last = c; continue
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
        last = '/'; continue
      }
    }
    if (c === '{') depth++
    else if (c === '}') {
      if (depth === 0 && typeof mode === 'number') { depth = modes.pop(); i++; last = '}'; continue }
      depth--
      if (depth === 0 && !stopAtSemicolon) return i + 1
    } else if (c === ';' && depth === 0 && stopAtSemicolon) return i + 1
    if (!/\s/.test(c)) last = c
    i++
  }
  throw new Error('unterminated source at ' + start)
}
function fn(name) {
  const at = script.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  // Skip the parameter list first: a destructured default ({a=1}={}) has braces.
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
function run(names, consts, prelude, body) {
  const code = [prelude, ...consts.map(constant), ...names.map(fn), body].join('\n')
  const context = vm.createContext({ console, URL, Date })
  const result = vm.runInContext(code, context)
  return typeof result === 'function' ? (...args) => plain(result(...args)) : plain(result)
}
const storage = `const store={};const localStorage={getItem:k=>k in store?store[k]:null,setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}};`

test('the mode switch is three radios; Analyze appears only when the server offers it', () => {
  const out = run(
    ['formalizeChoice', 'renderFormalizeSwitch', 'setFormalizeChoice', 'engineServesProve', 'analyzeEngineEntry', 'analyzePolicy',
      'viewerIsAdmin', 'analyzeOffered', 'runKindChoice', 'modeRadios', 'radioMode', 'chooseMode', 'nextModeRadio'],
    ['FORMALIZE_KEY', 'RUN_KIND_KEY'],
    storage + `
let ENGINES=[], CURRENT_USER={guest:false,is_admin:false}, changed=0;
const runKindChanged=()=>{changed++;};
const buttons=[{formalize:'0'},{formalize:'1'},{runKind:'analyze'}].map(dataset=>({dataset,hidden:dataset.runKind==='analyze',attrs:{},tabIndex:0,setAttribute(k,v){this.attrs[k]=v;}}));
const document={querySelectorAll:()=>buttons};`,
    `(()=>{
  const state=()=>({checked:buttons.filter(b=>b.attrs['aria-checked']==='true').map(b=>b.dataset.formalize||b.dataset.runKind),
    tab:buttons.map(b=>b.tabIndex),hidden:buttons[2].hidden,kind:runKindChoice()});
  const out={};
  store[RUN_KIND_KEY]='analyze';                       // remembered, but nothing is advertised yet
  renderFormalizeSwitch(); out.none=state();
  ENGINES=[{id:'kimi',modes:['prove'],available:true},{id:'analyze',modes:['analyze'],available:false,analyze:{enabled:true}}];
  renderFormalizeSwitch(); out.setup=state();          // a runner that needs setup: members do not see it
  CURRENT_USER={is_admin:true};
  renderFormalizeSwitch(); out.admin=state();          // ... the administrator does
  ENGINES[1].available=true; CURRENT_USER={guest:true};
  renderFormalizeSwitch(); out.guest=state();
  chooseMode('formal'); out.formal=[state(),changed];
  out.cycle=[]; let at=buttons[1];
  for(const key of ['ArrowRight','ArrowRight','ArrowRight','ArrowLeft','End','Home']){ at=nextModeRadio(at,key); out.cycle.push(radioMode(at)); }
  ENGINES[1].modes=['prove']; out.notAnalyzeMode=analyzeOffered();  // modes must say "analyze"
  return out;
})()`)
  assert.deepEqual(out.none, { checked: ['1'], tab: [-1, 0, -1], hidden: true, kind: 'prove' })
  assert.deepEqual(out.setup, { checked: ['1'], tab: [-1, 0, -1], hidden: true, kind: 'prove' })
  assert.deepEqual(out.admin, { checked: ['analyze'], tab: [-1, -1, 0], hidden: false, kind: 'analyze' })
  assert.deepEqual(out.guest, { checked: ['analyze'], tab: [-1, -1, 0], hidden: false, kind: 'analyze' })
  // Exactly one radio is checked and focusable; leaving Analyze keeps the proof output choice.
  assert.deepEqual(out.formal, [{ checked: ['1'], tab: [-1, 0, -1], hidden: false, kind: 'prove' }, 1])
  assert.deepEqual(out.cycle, ['analyze', 'informal', 'formal', 'informal', 'analyze', 'informal'])
  assert.equal(out.notAnalyzeMode, false)
})

test('the composer markup keeps the old selectors and adds the Analyze controls', () => {
  const switchMarkup = html.split('id="formalize-switch"')[1].split('</div>')[0]
  assert.match(switchMarkup, /role="radiogroup" aria-label="Mode"/)
  assert.match(switchMarkup, /data-formalize="0"[^>]*>Informal only</)
  assert.match(switchMarkup, /data-formalize="1"[^>]*>Informal \+ formalization</)
  assert.match(switchMarkup, /role="radio" data-run-kind="analyze" aria-checked="false" tabindex="-1" hidden/)
  assert.match(html, /What would you like to <span id="landing-verb">prove<\/span>\?/)
  assert.match(script, /verb:'analyze'/)
  assert.match(script, /'Paste a link \(arXiv, GitHub, openai\/math, PDF\/TeX URL\) or paste a manuscript…'/)
  assert.match(html, /<input type="checkbox" id="analyze-formalize"\/><span>Also formalize main statements in Lean \(statements only\)<\/span>/)
  assert.match(html, /id="dag-mode">\s*<button type="button" data-mode="analysis" hidden[^>]*>Manuscript<\/button>/)
  assert.match(html, /id="dag-filters" role="group" aria-label="Show results by verdict" hidden/)
})

test('Problems hand-off: an analyze payload opens Analyze with its link; anything odd falls back or is refused', async () => {
  const catalogue = await import(new URL('../docs/.vitepress/theme/problemCatalogue.mjs', import.meta.url))
  const now = 1_790_000_000_000
  const problem = {
    id: 'kourovka-02-048', title: 'Kourovka problem 2.48', category: 'ai-proposed', source_url: 'https://kourovka-notebook.org/',
    statement: { kind: 'exact', text: 'Is every group of this kind finite?' },
    status_evidence: [{ kind: 'claim', actor: 'ai', scope: 'full', acceptance: 'claimed', url: 'https://arxiv.org/abs/2406.05203', title: 'A claim', date: '2026-09-01' }],
  }
  const analyzePayload = JSON.stringify(catalogue.createSolvePayload(problem, now, { mode: 'analyze' }))
  const solvePayload = JSON.stringify(catalogue.createSolvePayload(problem, now, { mode: 'solve' }))
  const parse = run(['safeHandoffUrl', 'parseCatalogueProblem'], [], '', 'parseCatalogueProblem')
  const analyze = parse(analyzePayload, now + 1000)
  assert.equal(analyze.mode, 'analyze')
  assert.equal(analyze.analyzeUrl, 'https://arxiv.org/abs/2406.05203')
  assert.equal(analyze.title, 'Kourovka problem 2.48')
  assert.equal(parse(solvePayload, now + 1000).mode, 'solve')
  const tweak = change => JSON.stringify({ ...JSON.parse(analyzePayload), ...change })
  // Only an https link without credentials is handed to Analyze; otherwise the problem opens for proving.
  for (const url of ['javascript:alert(1)', 'http://arxiv.org/abs/2406.05203', 'https://user:pw@arxiv.org/abs/1', 'data:text/html,x', '', 42, 'https://' + 'a'.repeat(2100)]) {
    const parsed = parse(tweak({ analyze_url: url }), now + 1000)
    assert.equal(parsed.mode, 'solve', String(url))
    assert.equal(parsed.analyzeUrl, '')
    assert.equal(parsed.analyzeRequested, true)
  }
  // Stale, future, malformed, oversized or unsigned payloads are ignored.
  assert.equal(parse(analyzePayload, now + 31 * 60 * 1000), null)
  assert.equal(parse(analyzePayload, now - 1), null)
  assert.equal(parse('{not json', now), null)
  assert.equal(parse('x'.repeat(40001), now), null)
  assert.equal(parse(tweak({ version: 2 }), now + 1), null)
  assert.equal(parse(tweak({ source_url: 'http://example.org/' }), now + 1), null)
  assert.equal(parse(tweak({ problem_id: '../x' }), now + 1), null)
  // The console never starts an analysis on its own: the hand-off only fills the composer.
  const handoff = fn('loadAnalyzeHandoff')
  assert.doesNotMatch(handoff, /startRun|startAnalyzeRun|\.click\(/)
  assert.match(handoff, /nothing runs until you do/)
})

test('Manuscript DAG: verdicts map to blue, amber, red, grey and hollow — never green', () => {
  const visual = run(['analysisVerdict', 'analysisVerdictVisual', 'analysisNodeVisual', 'dagNodeVisual', 'dagKindColor', 'dagLookup'],
    ['ANALYZE_VERDICTS', 'ANALYZE_VERDICT_LABEL', 'ANALYZE_VERDICT_SHORT', 'ANALYZE_VERDICT_COLOR', 'DAG_KIND_COLOR'], '',
    `(meta,kind)=>dagNodeVisual({id:'n',kind:kind||'lemma',meta},{role:'analysis'})`)
  const of = verdict => visual({ verdict })
  assert.equal(of('checked').verdictColor, 'var(--verdict-checked)')
  assert.equal(of('gap').verdictColor, 'var(--verdict-gap)')
  assert.equal(of('error').verdictColor, 'var(--verdict-error)')
  assert.equal(of('unclear').verdictColor, 'var(--verdict-unclear)')
  assert.equal(of('not_checked').verdictColor, 'var(--verdict-none)')
  assert.deepEqual(of('checked').status, { label: 'checked', tone: 'vd' })
  assert.ok(of('not_checked').classes.includes('v-not_checked'))
  // Unknown or missing verdicts are "not checked"; an unusable reply says so.
  assert.ok(of('verified').classes.includes('v-not_checked'))
  assert.ok(visual({}).classes.includes('v-not_checked'))
  const unusable = visual({ verdict: 'unclear', interpreted: false, closure: 'unclear' })
  assert.equal(unusable.status.label, 'no usable output')
  assert.ok(unusable.classes.includes('uninterpreted'))
  // ... and it is "not checked" (hollow), never a grey "unclear" judgement.
  assert.ok(unusable.classes.includes('v-not_checked') && !unusable.classes.includes('v-unclear'))
  assert.equal(unusable.verdictColor, 'var(--verdict-none)')
  assert.deepEqual(unusable.chips, [])
  // The kind badge is neutral even for kinds the proof views colour green.
  for (const kind of ['fact', 'theorem', 'conclusion', 'lemma']) assert.equal(visual({ verdict: 'checked' }, kind).color, '#64748b')
  const main = visual({ verdict: 'checked', closure: 'gap', main_result: true, external: [{ citation: '[A]' }] })
  assert.deepEqual(main.chips.map(chip => chip.text), ['main result', 'with deps: gap', '1 external'])
  assert.ok(main.classes.includes('main-result'))
  // The verdict palette (both themes) has no green: hue outside 75°–165°.
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  const colours = [...css.matchAll(/--verdict-[a-z]+:(#[0-9a-f]{6})/gi)].map(match => match[1])
  assert.ok(colours.length >= 10, colours.join(' '))
  for (const hex of colours) {
    const [r, g, b] = [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16) / 255)
    const max = Math.max(r, g, b), min = Math.min(r, g, b)
    if (max - min < 0.12) continue  // greys
    const hue = max === r ? 60 * (((g - b) / (max - min)) % 6) : max === g ? 60 * ((b - r) / (max - min) + 2) : 60 * ((r - g) / (max - min) + 4)
    const h = (hue + 360) % 360
    assert.ok(h < 75 || h > 165, `${hex} is green (hue ${Math.round(h)})`)
  }
  const checked = css.match(/\[data-theme="light"\] \{ --verdict-checked:(#[0-9a-f]{6})/i)[1]
  assert.equal(checked.toLowerCase(), '#1d4ed8')
  assert.doesNotMatch(css, /var\(--ok\)/)
})

test('report and verdict text stay text: raw HTML escaped, math kept, links sanitized', () => {
  const render = run(['stashMath', 'escapeHtml', 'analysisMarkdownHtml'], [],
    `let analysisMarkdownParser=null; const sanitizeRenderedHtml=html=>html; const window={};`,
    `(text,marked)=>{ window.marked=marked; analysisMarkdownParser=null; return analysisMarkdownHtml(text); }`)
  // Without marked the report is preformatted, escaped text.
  assert.equal(render('<img src=x onerror=alert(1)> $a<b$'), '<pre>&lt;img src=x onerror=alert(1)&gt; $a&lt;b$</pre>')
  // With marked, raw HTML reaches the html renderer, which escapes it (marked 12 passes text, later versions a token).
  class Marked {
    constructor() { this.renderer = {} }
    use({ renderer }) { Object.assign(this.renderer, renderer) }
    parse(source) {
      return source.split(/(<[^>]+>)/).map((part, index) => index % 2
        ? (index === 1 ? this.renderer.html(part) : this.renderer.html({ type: 'html', raw: part, text: part }))
        : `<p>${part}</p>`).join('')
    }
  }
  const out = render('Gap: <img src=x onerror=alert(1)> and <b>bold</b> with $a<b$', { Marked })
  assert.doesNotMatch(out, /<img|<b>/)
  assert.match(out, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(out, /&lt;b&gt;/)
  // Math is restored after parsing as escaped TeX for KaTeX.
  assert.match(out, /\$a&lt;b\$/)
  // The console's report renderer is the sanitizing one, and links open safely.
  assert.match(fn('analysisMarkdownHtml'), /return sanitizeRenderedHtml\(html\);/)
  assert.match(fn('renderAnalysisReport'), /link\.target='_blank'; link\.rel='noopener noreferrer'/)
  // Titles, statements, findings and picker entries are set as text, never as HTML.
  for (const name of ['renderAnalyzeChip', 'renderAnalyzePicker', 'renderAnalysisDetail', 'analysisDetailActions', 'renderAnalyzeNote', 'renderAnalysisStages', 'renderAnalysisFilters', 'renderLeanUpstream'])
    assert.doesNotMatch(fn(name), /innerHTML|insertAdjacentHTML|outerHTML/, name)
})

test('statement prose, progress lines and author macros are cleaned for display', () => {
  const prose = run(['analysisRefLabels', 'analysisProse'], [], '', `(text,graph)=>analysisProse(text,analysisRefLabels(graph))`)
  const graph = { nodes: [{ label: 'Definition 2.1', meta: { number: '2.1', labels: ['def:parity'] } }, { label: 'Lemma 3.2', meta: { number: '3.2', labels: ['lem:sum'] } }] }
  assert.equal(prose('By Definition~\\ref{def:parity} and \\cref{lem:sum}, \\emph{every} $x\\in\\mathbb{Z}$ works \\cite[Thm. 2]{Smith}.', graph),
    'By Definition 2.1 and Lemma 3.2, every $x\\in\\mathbb{Z}$ works [Smith].')
  assert.equal(prose('Keep $\\ref{x}~y$ and \\(a~b\\) as written; see \\ref{missing}.', graph), 'Keep $\\ref{x}~y$ and \\(a~b\\) as written; see missing.')
  const progress = run(['analysisProgressText'], [], '', 'analysisProgressText')
  assert.equal(progress('$ /venv/bin/python -u /srv/x/analyze_runner.py\nAnalyze · 1 Ingest\n{"verdict":"checked"}\n\n---\n\nAnalyze · 3 Check · Lemma 3.2 · checked'),
    'Analyze · 1 Ingest\nAnalyze · 3 Check · Lemma 3.2 · checked')
  const macros = run(['analysisKatexMacros'], [], '', 'analysisKatexMacros')
  assert.deepEqual(macros({ macros: { '\\R': '\\mathbb{R}', '\\bad': '\\href{javascript:x}{y}', '\\loop': '\\def\\a{\\a}', 'R': 'x', '\\long': 'x'.repeat(401), '\\ok': '#1^2' } }),
    { '\\R': '\\mathbb{R}', '\\ok': '#1^2' })
  assert.deepEqual(macros({ macros: ['\\R'] }), {})
})

test('a link is one line; anything else is pasted text', () => {
  const link = run(['analyzeLinkFromText'], [], '', 'analyzeLinkFromText')
  assert.equal(link('  https://arxiv.org/abs/2510.01234v2 '), 'https://arxiv.org/abs/2510.01234v2')
  assert.equal(link('arxiv.org/abs/2510.01234'), 'https://arxiv.org/abs/2510.01234')
  assert.equal(link('github.com/openai/math'), 'https://github.com/openai/math')
  assert.equal(link('arXiv: 2510.01234'), 'arXiv:2510.01234')
  assert.equal(link('2510.01234v3'), '2510.01234v3')
  assert.equal(link('math/0601001v3'), 'math/0601001v3')
  assert.equal(link('\\begin{theorem} https://x.org \\end{theorem}'), '')
  assert.equal(link('Theorem 1. Every even number is a sum'), '')
  assert.equal(link('https://' + 'a'.repeat(4100)), '')
})

test('derived chips and the Lean tab speak of statements, not incomplete proofs', () => {
  const dom = `const els={};const element=id=>els[id]||(els[id]={id,textContent:'',innerHTML:'',hidden:false,className:'',style:{},classes:new Set(),
  classList:{add(...c){c.forEach(x=>els[id].classes.add(x));},remove(...c){c.forEach(x=>els[id].classes.delete(x));}},querySelectorAll:()=>[]});
const $=element; const tabs={lean:element('tab-lean'),dag:element('tab-dag')};
const document={querySelector:selector=>selector.includes('"lean"')?tabs.lean:tabs.dag};
const escapeHtml=value=>String(value); let workspaceView='sandbox', autoPipelineKey='', CURRENT_USER=null, lastSandboxRun=null, runId='';
const syncDagRebuild=()=>{}, refreshDagView=()=>{}, refreshLeanView=()=>{}, leanStatus=()=>{}, setLeanBadge=()=>{};`
  const out = run(['pipelineStageVisualStatus', 'renderAutoPipeline', 'syncLeanPipelineEmptyState', 'setStatementsLeanBadge'], [], dom, `(()=>{
  const done={status:'done',outcome:'success'};
  renderAutoPipeline({run_kind:'analyze',status:'done',stages:{informal_dag:done,lean:{status:'done',outcome:'attention',result:{status:'incomplete',scope:'statements'}},formal_dag:done,coverage:done}});
  const html=els['auto-pipeline-status'].innerHTML;
  const off={status:'off',outcome:'not_run'};
  const handled=syncLeanPipelineEmptyState({run_kind:'analyze',stages:{lean:off}});
  const badges=['incomplete','verified','unfaithful','failed','toolchain_missing'].map(status=>{ setStatementsLeanBadge({status,status_label:'L-'+status}); return [els['lean-badge'].className,els['lean-badge'].textContent]; });
  return {html,lean:[...tabs.lean.classes],dag:[...tabs.dag.classes],handled,title:els['lean-progress-title'].textContent,detail:els['lean-progress-detail'].textContent,badges};
})()`)
  assert.match(out.html, /<span>Manuscript DAG<\/span>/)
  assert.match(out.html, /<span class="pipeline-chip done"[^>]*><span>Lean statements<\/span><span class="chip-state">compiled<\/span>/)
  assert.match(out.html, /Analyze · derived views/)
  assert.doesNotMatch(out.html, /incomplete|Informal DAG/)
  assert.deepEqual(out.lean, ['pipeline-done'])
  assert.equal(out.handled, false)
  assert.equal(out.title, 'Formalization is off for this analysis')
  assert.match(out.detail, /proofs are not attempted/)
  assert.deepEqual(out.badges.map(([cls]) => cls), ['lean-badge statements', 'lean-badge statements', 'lean-badge incomplete', 'lean-badge failed', 'lean-badge none'])
  assert.equal(out.badges[0][1], 'L-incomplete')
})

test('unusable verifier output is counted and filtered with not checked, as in the report', () => {
  const dom = `const made=[];const document={createElement:tag=>{const el={tag,className:'',textContent:'',title:'',dataset:{},style:{setProperty(){}},attrs:{},
  setAttribute(k,v){this.attrs[k]=v;},replaceChildren(...kids){this.kids=kids;}};made.push(el);return el;}};
const box={hidden:true,replaceChildren(...kids){this.kids=kids;},append(...kids){this.kids.push(...kids);}};const $=id=>id==='dag-filters'?box:null;
let analysisHidden=new Set(), analysisFilterRunId='', runId='r1'; const applyAnalysisFilters=()=>{};`
  const counts = run(['analyzeEl', 'analysisVerdict', 'renderAnalysisFilters'],
    ['ANALYZE_VERDICTS', 'ANALYZE_VERDICT_LABEL', 'ANALYZE_VERDICT_SHORT', 'ANALYZE_VERDICT_COLOR'], dom,
    `(nodes=>{ renderAnalysisFilters({nodes},'Checked 5 of 5 results whose proofs were located · 1 with no usable verifier output'); return box.kids.filter(kid=>kid.tag==='button').map(kid=>[kid.dataset.verdict,kid.textContent,kid.title]); })`)
  // The reviewer's run: two checked, a gap, an error, one unreadable reply and one result never checked.
  const nodes = [{ meta: { verdict: 'checked' } }, { meta: { verdict: 'checked' } }, { meta: { verdict: 'gap' } },
    { meta: { verdict: 'error' } }, { meta: { verdict: 'unclear', interpreted: false } }, { meta: { verdict: 'not_checked' } }]
  const buttons = counts(nodes)
  assert.deepEqual(buttons.map(([verdict, text]) => [verdict, text]), [
    ['checked', 'checked · 2'], ['gap', 'gap · 1'], ['error', 'error · 1'], ['unclear', 'unclear · 0'], ['not_checked', 'not checked · 2']])
  // The report's table: unclear 0, not checked 1 and one "no usable verifier output" row; the filter says which.
  assert.match(buttons[4][2], /\(1 with no usable verifier output\)$/)
  assert.doesNotMatch(buttons[3][2], /usable/)
})

test('the Lean tab of an analysis never speaks of verifying a proof (waiting, running, failed, checked again)', () => {
  const dom = `const els={};const element=id=>els[id]||(els[id]={id,textContent:'',innerHTML:'',hidden:false,className:'',value:'',title:'',
  style:{},scrollTop:0,clientHeight:0,scrollHeight:0,classes:new Set(),classList:{add(...c){c.forEach(x=>els[id].classes.add(x));},remove(...c){c.forEach(x=>els[id].classes.delete(x));},toggle(){}},querySelectorAll:()=>[]});
const $=element; let bodyClasses=new Set();
const document={body:{classList:{contains:c=>bodyClasses.has(c)}},querySelector:()=>null};
let lastSandboxRun=null, runId='r1', CURRENT_USER={guest:false}, leanRunKey='', leanDirty=false, statusLine='';
const leanStatus=text=>{statusLine=String(text);}; const escapeHtml=value=>String(value);
const LEAN_STATUS_LABEL={verified:'Verified',compiled:'Compiled',failed:'Failed',incomplete:'Incomplete (sorry)',none:'—'};
const setLeanBadge=status=>{ element('lean-badge').className='lean-badge '+(status||'none'); element('lean-badge').textContent=LEAN_STATUS_LABEL[status]||status; };
const leanEditorText=()=>'', leanToolchainLabel=()=>'Lean 4.14', leanAttemptAxioms=()=>'', leanAttemptLabel=()=>'', renderLeanFidelity=()=>{}, renderLeanCitations=()=>{}, renderLeanChat=()=>{}, setLeanRunning=()=>{};`
  const out = run(['pipelineStageVisualStatus', 'leanForAnalysis', 'isAnalysisRun', 'renderLeanProgress', 'syncLeanPipelineEmptyState', 'setStatementsLeanBadge', 'renderLean'],
    ['LEAN_PROGRESS_STAGES', 'LEAN_ANALYSIS_STAGE_NAMES'], dom, `(()=>{
  const card=()=>[els['lean-progress-title'].textContent,els['lean-progress-detail'].textContent];
  const stages=(lean,dag)=>({informal_dag:dag||{status:'waiting'},lean,formal_dag:{status:'waiting'},coverage:{status:'waiting'}});
  const out={};
  // An analysis that is still running (formalize on).
  bodyClasses=new Set(['analysis-run']);
  renderLeanProgress({run_kind:'analyze',proof_engine:'analyze',stages:stages({status:'waiting'})}); out.waiting=card();
  renderLeanProgress({run_kind:'analyze',stages:stages({status:'running',detail:''},{status:'done',outcome:'success'})}); out.running=card();
  // The statement stage failed (no Proof.lean).
  const failed={run_kind:'analyze',stages:stages({status:'error',outcome:'error',detail:'model response contained neither JSON nor recognisable Lean',result:{status:'error',outcome:'error'}},{status:'done',outcome:'success'})};
  renderLeanProgress(failed); out.failedHandled=syncLeanPipelineEmptyState(failed); out.failed=[...card(),els['lean-badge'].className,els['lean-badge'].textContent];
  const uncompiled={run_kind:'analyze',stages:stages({status:'done',detail:'Lean failed',result:{status:'failed',proof_lean:'available'}},{status:'done',outcome:'success'})};
  syncLeanPipelineEmptyState(uncompiled); out.uncompiled=card()[0];
  // Check pressed on the statements file: a result without the statement fields still reads as statements.
  renderLean({status:'incomplete',sorry_count:1,lean:'theorem main : True := sorry',notes:'Typechecks, but 1 sorry/admit remain at line(s) 5.',action:'check'});
  out.checked=[els['lean-badge'].className,els['lean-badge'].textContent,statusLine];
  // A proof run keeps its wording.
  bodyClasses=new Set(); leanRunKey='';
  renderLeanProgress({proof_engine:'kimi',stages:stages({status:'waiting'})}); out.proofWaiting=card();
  const proofFailed={stages:stages({status:'error',detail:'x'},{status:'done',outcome:'success'})};
  syncLeanPipelineEmptyState(proofFailed); out.proofFailed=card();
  renderLean({status:'incomplete',sorry_count:1,lean:'x',action:'check'}); out.proofChecked=els['lean-badge'].className;
  return out;
})()`)
  const proofWords = /proof draft|Proof engine|verify this proof|Harness|Formal verification|Verify\b/
  assert.equal(out.waiting[0], 'Waiting for the analysis')
  assert.doesNotMatch(out.waiting.join(' '), proofWords)
  assert.equal(out.running[0], 'Formalizing statements')
  assert.doesNotMatch(out.running.join(' '), proofWords)
  assert.equal(out.failedHandled, true)
  assert.equal(out.failed[0], 'No Lean statements were produced · proofs not attempted')
  assert.match(out.failed[1], /Retry with ∎ Formalize statements/)
  assert.doesNotMatch(out.failed.join(' '), proofWords)
  assert.deepEqual(out.failed.slice(2), ['lean-badge failed', 'No Lean statements were produced · proofs not attempted'])
  assert.equal(out.uncompiled, 'Statements did not compile · proofs not attempted')
  assert.equal(out.checked[0], 'lean-badge statements')
  assert.equal(out.checked[1], 'Statements compiled · proofs not attempted')
  assert.match(out.checked[2], /proofs not attempted/)
  assert.doesNotMatch(out.checked[2], /1 sorry/)
  assert.equal(out.proofWaiting[0], 'Preparing formal verification')
  assert.equal(out.proofFailed[0], 'Formal translation failed')
  assert.match(out.proofFailed[1], /Harness/)
  assert.equal(out.proofChecked, 'lean-badge incomplete')
  // The action label and the editor placeholder follow the mode.
  assert.match(fn('leanAction'), /verify:extra\.prove===false\?'Formalizing statements…':'Verifying…'/)
  assert.match(fn('applyRunKindView'), /'Lean 4 statements will appear here after ∎ Formalize statements…'/)
  assert.match(fn('applyRunKindView'), /analysis\?'Lean statements':'Lean Verification'/)
})

test('light theme: verdict tags, the Analyze badge and model-proposed edges meet WCAG contrast', () => {
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  const light = Object.fromEntries([...css.match(/\[data-theme="light"\] \{([^}]*)\}/)[1].matchAll(/--([a-z-]+):(#[0-9a-f]{6})/gi)].map(m => [m[1], m[2]]))
  const rgb = hex => [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16) / 255)
  const lum = c => { const f = v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; const [r, g, b] = c.map(f); return 0.2126 * r + 0.7152 * g + 0.0722 * b }
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05) }
  const white = [1, 1, 1], tint = c => c.map(v => v * 0.11 + 0.89)
  // The 8.5px bold tag on each card: the verdict colour on its own 11% tint.
  for (const name of ['verdict-checked', 'verdict-gap', 'verdict-error', 'verdict-unclear', 'verdict-none']) {
    const c = rgb(light[name])
    assert.ok(ratio(c, tint(c)) >= 4.5, `${name} ${light[name]} ${ratio(c, tint(c)).toFixed(2)}`)
  }
  assert.ok(ratio(rgb(light['analyze-ink']), white) >= 4.5)
  assert.ok(ratio(rgb(light['edge-model']), white) >= 3)
  assert.match(css, /\.run-kind \{[^}]*color: var\(--analyze-ink\)/)
  assert.match(css, /\.dag-edge\.e-model \{ stroke: var\(--edge-model\)/)
})

test('Manuscript DAG coverage and caveats wrap under the filters instead of being cut off in the header', () => {
  const texts = run(['analysisDagStatusText', 'analysisCoverageText'], [], '', `(d=>[analysisDagStatusText(d),analysisCoverageText(d)])`)
  const [header, coverage] = texts({ graph: { source: { title: 'Parity' }, nodes: [{}, {}, {}], edges: [{}], dependency_basis: 'model',
    coverage: { checked: 2, total: 3, uninterpretable: 1 } } })
  assert.equal(header, 'Manuscript · Parity · 3 statements · 1 links')
  assert.equal(coverage, 'Checked 2 of 3 results whose proofs were located · 1 with no usable verifier output · dependencies proposed by the model, not established · checked by a model verifier — not a certificate')
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  assert.match(css, /\.dag-filters \.coverage \{ flex-basis: 100%;[^}]*white-space: normal;/)
  assert.match(fn('renderAnalysisFilters'), /box\.append\(analyzeEl\('p','coverage',coverageText\)\)/)
})

test('an analysis\'s Progress pane and process log show progress, never raw verifier JSON or usage lines', () => {
  const lines = run(['analysisLogLines'], [], '', 'analysisLogLines')
  const log = ['◦ session analyze…', 'assistant · Ansatz Analyze · kimi-k3 via sponsored_kimi · no tools', 'stage · 1 Ingest · pasted text',
    'thinking · Analyze · 1 Ingest · pasted text', 'assistant · {"summary": "The manuscript claims…", "main_results": ["thm:main"]}',
    '· usage in 3000 / out 500', '$ /venv/bin/python -u /srv/agent_monitor/runners/analyze_runner.py',
    'assistant · {"claim": "c", "role": "lemma", "verification_report": {"gaps": []}}', '[{"x":1}]',
    'thinking · Analyze · Check · Lemma 3.2 · error — sign of the trace term', '---', 'stage · 4 Roll-up'].join('\n')
  assert.deepEqual(lines(log), ['assistant · Ansatz Analyze · kimi-k3 via sponsored_kimi · no tools', 'stage · 1 Ingest · pasted text',
    'thinking · Analyze · 1 Ingest · pasted text', 'thinking · Analyze · Check · Lemma 3.2 · error — sign of the trace term', 'stage · 4 Roll-up'])
  assert.match(fn('updateProcLog'), /isAnalysisRun\(run\)\?analysisLogLines\(raw\)/)
  const chat = run(['analysisLogLines', 'selectedRunIsAnalysis', 'isAnalysisRun', 'renderChat'], [],
    `const RUN_KIND_BY_ID=new Map([['a1','analyze']]); let runId='a1', lastSandboxRun=null; const box={innerHTML:'',scrollTop:0,scrollHeight:0};
const $=()=>box; const escapeHtml=v=>String(v); const fmtTs=()=>''; const attachmentLinks=()=>'';`,
    `(msgs=>{ renderChat(msgs); return box.innerHTML; })`)
  const html = chat([{ role: 'system', content: 'Re-check · Lemma 3.2 · started (model verifier, one result)' },
    { role: 'assistant', content: 'assistant · {"claim": "c"}\n· usage in 3000 / out 500' },
    { role: 'assistant', content: 'stage · 5 Report\nassistant · {"verdict": "checked"}' }])
  assert.doesNotMatch(html, /\{"|usage in/)
  assert.match(html, /Re-check · Lemma 3\.2 · started/)
  assert.match(html, /stage · 5 Report/)
  assert.equal((html.match(/chat-msg assistant/g) || []).length, 1)
})

test('the stage strip says each stage\'s state in words, and shows it without colour', () => {
  const dom = `const els={};const document={createElement:tag=>({tag,className:'',textContent:'',title:'',kids:[],attrs:{},setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}})};
const list={hidden:true,replaceChildren(...kids){this.kids=kids;}};const $=id=>id==='analysis-stages'?list:null;
const analysisStageStates=()=>[{label:'Ingest',state:'done'},{label:'Segment',state:'done'},{label:'Check',state:'stopped'},{label:'Roll-up',state:'running'},{label:'Report',state:'waiting'}];`
  const items = run(['analyzeEl', 'renderAnalysisStages'], [], dom,
    `(()=>{ renderAnalysisStages({}); return list.kids.map(li=>[li.className,li.textContent+li.kids.map(k=>k.textContent).join(''),li.kids.map(k=>k.className).join(),li.attrs['aria-current']||'']); })()`)
  assert.deepEqual(items, [
    ['done', 'Ingest — done', 'sr-only', ''], ['done', 'Segment — done', 'sr-only', ''], ['stopped', 'Check — stopped', 'sr-only', ''],
    ['running', 'Roll-up — in progress', 'sr-only', 'step'], ['waiting', 'Report — waiting', 'sr-only', '']])
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  const forced = css.split('@media (forced-colors: active)')[1]
  assert.match(forced, /\.analysis-stages li\.waiting \{ border-style: dashed; \}/)
  assert.match(forced, /\.analysis-stages li\.running \{ border: 2px solid Highlight; \}/)
  assert.match(forced, /\.analysis-stages li\.stopped \{ border-style: double;[^}]*line-through/)
  assert.match(css, /\.sr-only \{ position: absolute;/)
})

test('the stored-copy toggle keeps its name; aria-pressed carries the state', () => {
  const dom = `const link={hidden:false,href:'',title:''}, toggle={hidden:true,textContent:'',title:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;}};
const $=id=>id==='analysis-source-link'?link:id==='analysis-source-toggle'?toggle:null;
let analysisSourceShown=false, analysisSourceRunId='r1'; const analysisSourceFile=()=>'analysis/source/paper.tex';
const isAnalysisRun=()=>true;`
  const states = run(['syncAnalysisSourceControls'], [], dom, `(()=>{ const run={run_id:'r1',sandbox_preview:{source_url:'https://arxiv.org/abs/2510.01234'}};
  const out=[]; for(const shown of [false,true]){ analysisSourceShown=shown; syncAnalysisSourceControls(run); out.push([toggle.textContent,toggle.attrs['aria-pressed']]); }
  syncAnalysisSourceControls({run_id:'r1',sandbox_preview:{}}); out.push([toggle.textContent,toggle.attrs['aria-pressed']]); return out; })()`)
  assert.deepEqual(states, [['Stored copy', 'false'], ['Stored copy', 'true'], ['Source', 'true']])
})

test('the Analyze radio is named "Analyze": its diamond is decorative', () => {
  const css = html.split('<style id="analyze-style">')[1].split('</style>')[0]
  const rule = css.match(/button\[data-run-kind="analyze"\]::before \{([^}]*)\}/)[1]
  assert.match(rule, /content: '◇' \/ '';/)
  assert.match(html, /role="radio" data-run-kind="analyze"[^>]*>Analyze<\/button>/)
})

test('a guest sees how many re-checks are left, and the button stops at the limit', () => {
  const dom = `const document={createElement:tag=>({tag,className:'',textContent:'',title:'',disabled:false,kids:[],attrs:{},type:'',
  setAttribute(k,v){this.attrs[k]=v;},append(...k){this.kids.push(...k);}})};
let guest=true, used=0; const viewerIsGuest=()=>guest, analyzePolicy=()=>({rechecks_per_analysis:2});
const analysisRunState=()=>({run:{run_id:'r1',status:'finished',analyze:{rechecks:{started:used}}},running:false,recheck:null});
const dagDetailText=v=>String(v||''), analysisSourceFile=()=>'', recheckAnalysisNode=()=>{}, formalizeAnalysisNode=()=>{}, showAnalysisSourceLines=()=>{};`
  const actions = run(['analyzeEl', 'analysisDetailActions'], ['ANALYZE_VERDICT_LABEL'], dom, `((u,g)=>{ used=u; guest=g;
  const box=analysisDetailActions({id:'lem:sum',label:'Lemma 2.2'},{checkable:true},{});
  const row=box.kids[1], status=box.kids[2]; const button=row.kids[0]; return [button.disabled,button.title,status.textContent]; })`)
  const [fresh, last, spent, member] = [actions(0, true), actions(1, true), actions(2, true), actions(5, false)]
  assert.equal(fresh[0], false); assert.match(fresh[1], /guests re-check 2 results per analysis, 2 left/)
  assert.equal(last[0], false); assert.match(last[1], /1 left/)
  assert.equal(spent[0], true); assert.match(spent[1], /^Guests can re-check 2 results of an analysis; sign in/)
  assert.match(spent[2], /sign in to re-check more/)
  assert.equal(member[0], false); assert.doesNotMatch(member[1], /Guests/)
})

test('the stage strip starts at Ingest: the placeholder session node before the runner\'s first line is not Segment', () => {
  const states = run(['analysisStageStates'], ['ANALYZE_STAGES'], '', 'analysisStageStates')
  const placeholder = { stage_name: 'analyze_session', pipeline_stage: 'work' }
  const at = run => states(run).map(stage => stage.state).join(' ')
  assert.equal(at({ status: 'running', agents: [placeholder] }), 'running waiting waiting waiting waiting')
  assert.equal(at({ status: 'failed', agents: [{ ...placeholder, pipeline_stage: 'finalize' }] }), 'error waiting waiting waiting waiting')
  assert.equal(at({ status: 'running', agents: [{ stage_name: 'turn-1 (message)', pipeline_stage: 'verify' }] }), 'done done running waiting waiting')
})

test('manuscript display environments keep their row breaks and render as math', () => {
  const prose = run(['analysisProse'], [], '', 'analysisProse')
  const text = 'For all $x$,\n\\begin{align}\n |F(x)|&\\le 2,\\\\\n |X_x|&\\le 3.\n\\end{align}\nSee~\\ref{a}.'
  const out = prose(text, null)
  assert.match(out, /\\begin\{align\*\}\n \|F\(x\)\|&\\le 2,\\\\\n \|X_x\|&\\le 3\.\n\\end\{align\*\}/, 'unnumbered, rows kept')
  assert.match(out, /See a\.$/)
  assert.match(script, /function mathEnvDelimiters\(\)\{ return \['equation','equation\*','align','align\*','alignat','alignat\*','gather','gather\*'\]/)
  assert.match(script, /renderMathInElement\(el,\{delimiters:\[\.\.\.mathEnvDelimiters\(\),/)
})
