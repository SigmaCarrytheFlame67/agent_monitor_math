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
// `raw` hands back the result as it is (a DOM node, a function returning one).
// The console's math scanners: its Markdown and prose helpers find math with them.
const MATH_SCANNERS = ['mathNextIndex', 'mathBetween', 'mathEnvironment', 'mathDollarLine', 'mathEscapedDollar', 'mathSpans', 'splitMath', 'holdMath']
const MATH_KINDS = ['ANALYZE_MATH_KINDS', 'ANALYZE_STATEMENT_MATH_KINDS']
function run(names, consts, prelude, body, { marked = false, context = {}, raw = false } = {}) {
  const sandbox = vm.createContext({ console, URL, Date, ...context })
  if (marked) vm.runInContext(markedSource, sandbox)
  const code = [prelude, ...consts.map(constant), ...MATH_KINDS.filter(name => !consts.includes(name)).map(constant),
    ...names.map(fn), ...MATH_SCANNERS.filter(name => !names.includes(name)).map(fn), body].join('\n')
  const result = vm.runInContext(code, sandbox)
  if (raw) return result
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

// A small DOM that parses and serializes HTML (the way a browser does for
// well-formed markup), enough to run the console's own sanitizer (a
// <template>) on what marked writes, and the anchors, result links and
// selected passages built on top. Selectors: tag, .class, #id and [attr(="v")]
// compounds, comma lists; anything else throws.
function miniDocument() {
  const VOID = new Set(['AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG', 'INPUT', 'LINK', 'META', 'SOURCE', 'TRACK', 'WBR'])
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
  const decode = value => value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, name) => name[0] !== '#' ? named[name.toLowerCase()] ?? entity
    : String.fromCodePoint(name[1].toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : Number(name.slice(1))))
  const escapeText = value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/ /g, '&nbsp;')
  const escapeAttribute = value => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/ /g, '&nbsp;')
  function matcher(selector) {
    const tests = selector.split(',').map(part => {
      const match = /^([a-z][a-z0-9-]*|\*)?((?:\.[\w-]+|#[\w-]+|\[[^\]]+\])*)$/i.exec(part.trim())
      if (!match || !part.trim()) throw new Error('mini DOM: unsupported selector ' + part)
      const tag = match[1] && match[1] !== '*' ? match[1].toUpperCase() : null
      const pieces = [...match[2].matchAll(/\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/g)]
      return element => (!tag || element.tagName === tag) && pieces.every(([, cls, id, name, double, single, bare]) => {
        if (cls) return element.classList.contains(cls)
        if (id) return element.getAttribute('id') === id
        const value = double ?? single ?? bare
        return value === undefined ? element.hasAttribute(name) : element.getAttribute(name) === value
      })
    })
    return element => tests.some(test => test(element))
  }
  const assigned = []
  class Node {
    constructor(nodeType) { this.nodeType = nodeType; this.parentNode = null; this.childNodes = [] }
    get parentElement() { return this.parentNode?.nodeType === 1 ? this.parentNode : null }
    get firstChild() { return this.childNodes[0] || null }
    get lastChild() { return this.childNodes.at(-1) || null }
    get children() { return this.childNodes.filter(child => child.nodeType === 1) }
    get firstElementChild() { return this.children[0] || null }
    get childElementCount() { return this.children.length }
    get nextElementSibling() { const siblings = this.parentNode?.children || []; return siblings[siblings.indexOf(this) + 1] || null }
    get nodeValue() { return this.nodeType === 3 ? this.data : null }
    set nodeValue(value) { if (this.nodeType === 3) this.data = String(value) }
    get textContent() { return this.nodeType === 3 ? this.data : this.childNodes.map(child => child.textContent).join('') }
    set textContent(value) { if (this.nodeType === 3) this.data = String(value); else this.replaceChildren(...(String(value ?? '') ? [String(value)] : [])) }
    // Nodes to insert: strings become text, a fragment gives its children; each leaves its old place.
    adopt(items) {
      const nodes = items.flatMap(item => typeof item === 'string' ? [new Text(item)] : item.nodeType === 11 ? [...item.childNodes] : [item])
      nodes.forEach(node => node.remove())
      return nodes
    }
    append(...items) { for (const node of this.adopt(items)) { node.parentNode = this; this.childNodes.push(node) } }
    prepend(...items) { const nodes = this.adopt(items); nodes.forEach(node => { node.parentNode = this }); this.childNodes.unshift(...nodes) }
    appendChild(node) { this.append(node); return node }
    replaceChildren(...items) { const nodes = this.adopt(items); this.childNodes.forEach(node => { node.parentNode = null }); this.childNodes = []; this.append(...nodes) }
    replaceWith(...items) {
      const parent = this.parentNode
      if (!parent) return
      const nodes = this.adopt(items), index = parent.childNodes.indexOf(this)
      nodes.forEach(node => { node.parentNode = parent })
      parent.childNodes.splice(index, 1, ...nodes)
      this.parentNode = null
    }
    remove() { if (this.parentNode) { this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null } }
    after(...items) {
      const parent = this.parentNode
      if (!parent) return
      const nodes = this.adopt(items), index = parent.childNodes.indexOf(this)
      nodes.forEach(node => { node.parentNode = parent })
      parent.childNodes.splice(index + 1, 0, ...nodes)
    }
    addEventListener() {}
    descendants() { return this.childNodes.flatMap(child => child.nodeType === 1 ? [child, ...child.descendants()] : []) }
    querySelectorAll(selector) { return this.descendants().filter(matcher(selector)) }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
  }
  class Text extends Node { constructor(data) { super(3); this.data = String(data) } }
  class Fragment extends Node { constructor() { super(11) } }
  class Element extends Node {
    constructor(tag) { super(1); this.tagName = String(tag).toUpperCase(); this.attributeMap = new Map(); this.style = { setProperty() {} }; if (this.tagName === 'TEMPLATE') this.content = new Fragment() }
    get localName() { return this.tagName.toLowerCase() }
    get attributes() { return [...this.attributeMap].map(([name, value]) => ({ name, value })) }
    getAttribute(name) { return this.attributeMap.has(name) ? this.attributeMap.get(name) : null }
    setAttribute(name, value) { this.attributeMap.set(String(name).toLowerCase(), String(value)) }
    removeAttribute(name) { this.attributeMap.delete(name) }
    hasAttribute(name) { return this.attributeMap.has(name) }
    get className() { return this.getAttribute('class') || '' }
    set className(value) { this.setAttribute('class', value) }
    get classList() {
      const names = () => this.className.split(/\s+/).filter(Boolean)
      return {
        contains: name => names().includes(name),
        add: (...more) => { this.className = [...new Set([...names(), ...more])].join(' ') },
        remove: (...less) => { this.className = names().filter(name => !less.includes(name)).join(' ') },
      }
    }
    get dataset() {
      const attribute = key => 'data-' + String(key).replace(/[A-Z]/g, letter => '-' + letter.toLowerCase())
      return new Proxy({}, {
        get: (_, key) => typeof key === 'string' ? this.getAttribute(attribute(key)) ?? undefined : undefined,
        set: (_, key, value) => { this.setAttribute(attribute(key), value); return true },
      })
    }
    get tabIndex() { return Number(this.getAttribute('tabindex') ?? -1) }
    set tabIndex(value) { this.setAttribute('tabindex', value) }
    get innerHTML() { return (this.content || this).childNodes.map(serialize).join('') }
    set innerHTML(html) { assigned.push(String(html)); const target = this.content || this; target.replaceChildren(); parse(target, String(html)) }
    get outerHTML() { return serialize(this) }
    closest(selector) { const test = matcher(selector); for (let node = this; node?.nodeType === 1; node = node.parentNode) if (test(node)) return node; return null }
    focus() {}
  }
  for (const name of ['id', 'title', 'type', 'href', 'target', 'rel']) {
    Object.defineProperty(Element.prototype, name, { get() { return this.getAttribute(name) ?? '' }, set(value) { this.setAttribute(name, value) } })
  }
  function serialize(node) {
    if (node.nodeType === 3) return escapeText(node.data)
    const open = '<' + node.localName + node.attributes.map(({ name, value }) => ` ${name}="${escapeAttribute(value)}"`).join('') + '>'
    return VOID.has(node.tagName) ? open : open + node.innerHTML + '</' + node.localName + '>'
  }
  const TAG = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*(\/?)>/g
  function parse(root, html) {
    let open = root, at = 0
    const text = value => {
      if (!value) return
      if (open.lastChild?.nodeType === 3) open.lastChild.data += decode(value)
      else open.append(new Text(decode(value)))
    }
    for (const match of html.matchAll(TAG)) {
      text(html.slice(at, match.index)); at = match.index + match[0].length
      const [whole, closing, name, attributes, selfClosing] = match
      if (whole.startsWith('<!--')) continue
      if (closing) {
        for (let node = open; node && node !== root; node = node.parentNode) if (node.localName === name.toLowerCase()) { open = node.parentNode; break }
        continue
      }
      const element = new Element(name)
      for (const [, key, double, single, bare] of attributes.matchAll(/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) element.setAttribute(key, decode(double ?? single ?? bare ?? ''))
      open.append(element)
      if (!VOID.has(element.tagName) && !selfClosing) open = element
    }
    text(html.slice(at))
  }
  const document = { createElement: tag => new Element(tag), createTextNode: data => new Text(data), createDocumentFragment: () => new Fragment() }
  return { document, assigned }
}
// The console's own sanitizer and its helpers, and the readable-Markdown renderer, for the vm harness.
const SANITIZER = ['escapeHtml', 'stashMath', 'stashInlineMath', 'inlineMathEnd', 'safeMarkdownHref', 'sanitizeRenderedHtml', 'analyzeEl']
const READABLE = ['readableMarkdown', 'renderReadableMarkdown', 'readableTextNodes', 'placeReadableMarks', 'readableAnchorNode', 'readableRefNode']

test('Readability is the fourth mode: its own composer, a locked engine chip and "Make readable"', () => {
  const out = run(
    ['formalizeChoice', 'renderFormalizeSwitch', 'setFormalizeChoice', 'engineServesProve', 'analyzeEngineEntry', 'analyzePolicy',
      'viewerIsAdmin', 'analyzeOffered', 'readabilityEngineEntry', 'readabilityPolicy', 'readabilityOffered', 'runKindOffered',
      'runKindChoice', 'chooseMode', 'manuscriptMode', 'syncEngineForRunKind', 'syncStartButton', 'applyRunKindComposer', 'readabilityProviderText'],
    ['FORMALIZE_KEY', 'RUN_KIND_KEY', 'PROVE_COMPOSER', 'ANALYZE_COMPOSER', 'READABILITY_COMPOSER'],
    storage + `
const els={};
const $=id=>els[id]||(els[id]={id,hidden:false,textContent:'',placeholder:'',attrs:{},kids:[],setAttribute(k,v){this.attrs[k]=String(v);},
  replaceChildren(...k){this.kids=k;},querySelector(sel){ return $(id+' '+sel); }});
const bodyClasses=new Set();
const document={body:{classList:{toggle(c,on){ if(on)bodyClasses.add(c); else bodyClasses.delete(c); }}},querySelectorAll:()=>[],
  createElement:tag=>({tag,id:'',textContent:'',attrs:{},setAttribute(k,v){this.attrs[k]=v;}})};
let ENGINES=[{id:'kimi',modes:['prove'],available:true},{id:'analyze',modes:['analyze'],available:true,analyze:{enabled:true}},
  {id:'readability',modes:['readability'],available:true,readability:{enabled:true,routes:['sponsored_kimi','api_key']}}];
let CURRENT_USER={id:4}, GUEST_POLICY=null, SETTINGS=null, engine='kimi', proveEngine='';
const calls=[], initialAttachments={refreshLimits(){ calls.push('limits'); }};
const renderAnalyzeCaps=()=>calls.push('analyze caps'), renderReadabilityCaps=()=>calls.push('readability caps'), refreshAnalyzeSource=()=>calls.push('source');
// renderEngines, reduced to what the mode changes.
const runKindChanged=()=>{ syncEngineForRunKind(); applyRunKindComposer(); };`,
    `(()=>{
  const state=()=>({engine,proveEngine,kind:runKindChoice(),verb:$('landing-verb').textContent,sub:$('landing-sub').textContent,
    placeholder:$('problem-text').placeholder,label:$('problem-text').attrs['aria-label'],start:$('start-btn').kids[0]?.textContent,
    composer:!$('analyze-composer').hidden,analyzeOptions:!$('analyze-composer .analyze-options').hidden,analyzeProvider:!$('analyze-provider').hidden,
    readabilityOptions:!$('readability-options').hidden,readabilityProvider:!$('readability-provider').hidden,provider:$('readability-provider').textContent,
    body:[...bodyClasses].sort(),calls:calls.splice(0)});
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
    provider: "Readability sends the manuscript text to the selected model provider — sponsored Kimi K3 or your own Kimi key. Don't submit work you aren't allowed to share.",
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

test('the provider note names only the routes the server lists for this viewer', () => {
  const note = run(['readabilityEngineEntry', 'readabilityPolicy', 'readabilityProviderText'], [], 'let ENGINES=[];',
    `(routes=>{ ENGINES=[{id:'readability',modes:['readability'],readability:routes?{routes}:{}}]; return readabilityProviderText(); })`)
  const where = routes => note(routes).replace(/^Readability sends the manuscript text to the selected model provider/, '').replace(/\. Don't submit work you aren't allowed to share\.$/, '')
  // A guest (or a member on sponsored Kimi only), a member, the operator.
  assert.equal(where(['sponsored_kimi']), ' — sponsored Kimi K3')
  assert.equal(where(['sponsored_kimi', 'api_key']), ' — sponsored Kimi K3 or your own Kimi key')
  assert.equal(where(['sponsored_kimi', 'api_key', 'codex_subscription', 'claude_subscription']), ' — sponsored Kimi K3, your own Kimi key, or a connected Codex or Claude Code account')
  assert.equal(where(['api_key', 'claude_subscription']), ' — your own Kimi key or a connected Claude Code account')
  assert.equal(where(null), '')
  // The page's own copy (before the listing arrives) names no account.
  const copy = html.split('id="readability-provider" hidden>')[1].split('</p>')[0]
  assert.equal(copy, "Readability sends the manuscript text to the selected model provider. Don't submit work you aren't allowed to share.")
  assert.match(fn('applyRunKindComposer'), /if\(readability\)\$\('readability-provider'\)\.textContent=readabilityProviderText\(\);/)
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

// Readable Markdown as the console renders it: marked, the console's own
// sanitizer, then the anchor badges and result links, in the mini DOM.
// `sanitized` lists the sanitizer's outputs, `assigned` every HTML string the page was given.
function readableRenderer() {
  const { document, assigned } = miniDocument()
  const render = run([...SANITIZER, ...READABLE], ['SAFE_MARKDOWN_TAGS', 'READABLE_MARKER_LINE', 'READABLE_RESULT_LINK'],
    'const window=globalThis; let readableMarkdownParser=null;',
    `(()=>{
      const sanitize=sanitizeRenderedHtml, sanitized=[];
      sanitizeRenderedHtml=html=>{ const out=sanitize(html); sanitized.push(out); return out; };
      return (text,labels)=>{ const box=document.createElement('div'); renderReadableMarkdown(box,text,new Map(labels)); return {box,sanitized:sanitized.splice(0)}; };
    })()`,
    { marked: true, raw: true, context: { document, location: { href: 'https://ansatze.test/console' } } })
  return { render, assigned }
}

test('result markers become anchor badges: id-safe, labelled, never DOM ids; raw HTML stays text', () => {
  const { render, assigned } = readableRenderer()
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
  const { box, sanitized } = render(doc, [['thm:main', 'Theorem 1.1']])
  const out = box.innerHTML
  assert.match(out, /<p class="rd-anchor-row"><span class="rd-anchor" data-rd-anchor="thm:main" tabindex="-1" title="Theorem 1.1 · thm:main">Theorem 1\.1<\/span><\/p>/)
  // Ids are lower case (as the runner writes them) and escaped; no badge carries a DOM id.
  assert.match(out, /data-rd-anchor="lem:x"[^>]*>lem:x</)
  assert.match(out, /data-rd-anchor="a&quot;b" tabindex="-1" title="a&quot;b">a"b</)
  assert.doesNotMatch(out, /\sid="/)
  // A marker the runner never writes (a space in its id) and the part marks stay invisible; no token is left.
  assert.doesNotMatch(out, /lem bad|\[part:1\]|\[result:|@@/)
  // Raw HTML is shown as text; math is kept as TeX for KaTeX.
  assert.equal(box.querySelectorAll('img').length, 0)
  assert.match(box.textContent, /<img src=x onerror=alert\(1\)>/)
  assert.match(box.textContent, /\$x<y\$/)
  const [ref] = box.querySelectorAll('.rd-ref')
  assert.deepEqual([ref.tagName, ref.type, ref.dataset.rdRef, ref.title, ref.textContent], ['BUTTON', 'button', 'thm:main', 'Show Theorem 1.1 in the readable version', 'Theorem 1.1'])
  // The page is given the sanitizer's output as it is; the badges and links are built on it afterwards, as elements.
  assert.equal(sanitized.length, 1)
  assert.equal(assigned.at(-1), sanitized[0])
  assert.doesNotMatch(sanitized[0], /rd-anchor|rd-ref|<button|<span/)
  // That is the only HTML readability.js gives the page.
  assert.equal([...readability.matchAll(/innerHTML|insertAdjacentHTML|outerHTML/g)].length, 1)
  assert.match(fn('renderReadableMarkdown'), /container\.innerHTML=html;\n\s*placeReadableMarks\(container,marks,nonce,labels,where\);/)
  assert.match(fn('readableMarkdown'), /return \{html:sanitizeRenderedHtml\(/)
})

test('display environments in a readable version are math, as on the shared page', () => {
  // Review: Markdown read inside \\begin{equation} dropped \\{ and \\\\ and made a heading of "=".
  const { render } = readableRenderer()
  const doc = 'Put\n\\begin{equation}\n E=\\{b\\in B: b\\ne 0\\},\\qquad\n =\n\\end{equation}\nand\n\\begin{align*}\n a &= 1 \\\\\n b &= 2\n\\end{align*}\nDone.'
  const { box } = render(doc, [])
  assert.doesNotMatch(box.innerHTML, /<h1|@@/)
  assert.ok(box.textContent.includes('\\begin{equation}\n E=\\{b\\in B: b\\ne 0\\},\\qquad\n =\n\\end{equation}'))
  assert.ok(box.textContent.includes('\\begin{align*}\n a &= 1 \\\\\n b &= 2\n\\end{align*}'))
})

test('a result link right after the name it names is a compact jump', () => {
  // Review: answers read "Lemma 3.9 Lemma 3.9" (the name, then a link of that name).
  const { render } = readableRenderer()
  const { box } = render('By Lemma 3.9 [[result:lem:a]] and, again, [[result:lem:a]].', [['lem:a', 'Lemma 3.9']])
  const [jump, full] = box.querySelectorAll('.rd-ref')
  assert.deepEqual([jump.textContent, jump.getAttribute('aria-label'), jump.title, jump.dataset.rdRef],
    ['↗', 'Show Lemma 3.9 in the readable version', 'Show Lemma 3.9 in the readable version', 'lem:a'])
  assert.equal(full.textContent, 'Lemma 3.9')
})

test('sanitized last: a result marker or link never turns into markup in a link, math or code, and text cannot forge one', () => {
  const { render } = readableRenderer()
  const labels = [['thm:main', 'Theorem 1']]
  // In a link's title or address it reads as it was written: no class, button or data on the link.
  let { box } = render('See [the def](https://example.com/ "[[result:thm:main]]") and [x](https://example.com/[[result:thm:main]]).', labels)
  const [titled, addressed] = box.querySelectorAll('a')
  assert.deepEqual(titled.attributes.map(({ name }) => name).sort(), ['href', 'rel', 'title'])
  assert.equal(titled.getAttribute('title'), '[[result:thm:main]]')
  assert.equal(titled.textContent, 'the def')
  assert.equal(addressed.getAttribute('href'), 'https://example.com/[[result:thm:main]]')
  assert.equal(box.querySelectorAll('button,span').length, 0)
  assert.doesNotMatch(box.innerHTML, /@@/)
  // Text shaped like a token is text: the tokens carry a nonce of each rendering.
  ;({ box } = render('Text @@RDREF0@@, @@RDabcdefghx0@@ and [[result:thm:main]].', labels))
  assert.equal(box.querySelectorAll('.rd-ref').length, 1)
  assert.equal(box.textContent.trim(), 'Text @@RDREF0@@, @@RDabcdefghx0@@ and Theorem 1.')
  ;({ box } = render('<https://example.com/@@RDANCHOR0@@>\n\n[result:thm:main]: #\n', labels))
  assert.equal(box.querySelectorAll('.rd-anchor').length, 1)
  assert.equal(box.querySelector('a').getAttribute('href'), 'https://example.com/@@RDANCHOR0@@')
  // Inside math it stays TeX, in one piece of text, so KaTeX sees the whole formula.
  ;({ box } = render('Then $a [[result:thm:main]] b$ holds.', labels))
  assert.equal(box.querySelectorAll('.rd-ref').length, 0)
  assert.deepEqual(box.querySelector('p').childNodes.map(node => node.textContent), ['Then $a [[result:thm:main]] b$ holds.'])
  // In code it reads as it was written; in a link's text as the result's name (no button inside a link).
  ;({ box } = render('Write `[[result:thm:main]]` or see [the result [[result:thm:main]]](https://example.com/).', labels))
  assert.equal(box.querySelector('code').textContent, '[[result:thm:main]]')
  assert.equal(box.querySelector('a').textContent, 'the result Theorem 1')
  assert.equal(box.querySelectorAll('button').length, 0)
  // A marker inside a list ends it and is a row of its own.
  ;({ box } = render('- item\n[result:thm:main]: #\n- next', labels))
  assert.deepEqual(box.querySelectorAll('.rd-anchor').map(anchor => [anchor.parentElement.tagName, anchor.parentElement.className]), [['P', 'rd-anchor-row']])
  // What the sanitizer drops stays dropped: images, raw HTML, javascript: links.
  ;({ box } = render('![x](https://evil.example/p.png) <img src=x onerror=alert(1)> <details open ontoggle=alert(2)>x</details> [y](javascript:alert(3)) [[result:thm:main]]', labels))
  assert.equal(box.querySelectorAll('img,details').length, 0)
  assert.equal(box.querySelector('a').hasAttribute('href'), false)
  assert.equal(box.querySelectorAll('.rd-ref').length, 1)
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
  const view = run(['analyzeEl', 'dagDetailText', 'readabilityNotation', 'katexReferenceText', 'withKatexFallbacks', 'countedAuthorMacros', 'readabilityTex', 'readabilityTable', 'readabilityWhereCell', 'renderReadabilityNotation'], ['KATEX_FALLBACK_MACROS'],
    miniDom + `
const typeset=[]; const window={katex:{render(tex,el,options){ const R=options.macros['\\\\R']; typeset.push([tex,options,typeof R==='function'?R({}):'not counted']); el.textContent='KATEX:'+tex; }}};
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
  // The author's macros (counted as KaTeX expands them), over the console's stand-ins for commands KaTeX lacks.
  assert.equal(out.typeset[0][2], '\\mathbb{R}')
  assert.equal(out.typeset[0][1].macros['\\qed'], '\\square')
  assert.equal(out.typeset[0][1].maxExpand, 200)
  // Model text arrives as text nodes, never markup.
  assert.ok(out.texts.includes('<img src=x onerror=alert(1)> a $\\mathbb{R}$-valued sequence'))
  assert.ok(out.texts.includes('§2 <b>bold</b>'))
  assert.ok(out.texts.includes('the "<i>" property'))
  assert.deepEqual(out.texts.filter(text => ['Symbol', 'Term', 'Meaning', 'Where introduced', 'Notation', 'Terms'].includes(text)),
    ['Notation', 'Symbol', 'Meaning', 'Where introduced', 'Terms', 'Term', 'Meaning', 'Where introduced'])
  const json = JSON.stringify(out.doc)
  assert.match(json, /"cls":"rd-ref","text":"Read","data":\{"rdRef":"def:alpha"\}/)
  for (const name of ['renderReadabilityNotation', 'readabilityWhereCell', 'readabilityTable', 'readabilityTex', 'renderReadabilityMap', 'readabilityMapRow', 'renderPaperMapDetail', 'renderManuscriptAskContext', 'renderReadabilityOriginal', 'readabilityNumberedLines'])
    assert.doesNotMatch(fn(name), /innerHTML|insertAdjacentHTML|outerHTML/, name)
  // Without KaTeX the symbol is plain text.
  const fallback = run(['readabilityTex'], [], 'const window={};', `(()=>{ const el={textContent:''}; readabilityTex(el,'\\\\beta<b>',{}); return el.textContent; })()`)
  assert.equal(fallback, '\\beta<b>')
})

test('answers: sanitized Markdown with result links; the question and the passage stay text', () => {
  const { document, assigned } = miniDocument()
  const item = run([...SANITIZER, ...READABLE, 'dagDetailText', 'manuscriptAskRunning', 'manuscriptAskItem'],
    ['SAFE_MARKDOWN_TAGS', 'READABLE_MARKER_LINE', 'READABLE_RESULT_LINK'],
    `const window=globalThis; let readableMarkdownParser=null; const fmtTs=()=>'10:42';`,
    `((entry,labels)=>manuscriptAskItem(entry,new Map(labels)))`,
    { marked: true, raw: true, context: { document, location: { href: 'https://ansatze.test/console' } } })
  const labels = [['thm:main', 'Theorem 1.1']]
  const done = item({ ask_id: 'a1', status: 'done', model: 'kimi-k3', finished_at: '2026-10-22T10:42:00Z', node_id: 'thm:main', question: '<b>Why</b> does $x<y$ hold?', selection: '<script>alert(1)</script> the passage',
    answer: 'By [[result:thm:main]] and [[ result : NOPE ]]: <iframe src=x></iframe> $a<b$ and [x](javascript:alert(1)).' }, labels)
  const answer = done.querySelector('.ask-a')
  const refs = answer.querySelectorAll('.rd-ref')
  assert.deepEqual(refs.map(ref => [ref.tagName, ref.type, ref.dataset.rdRef, ref.title, ref.textContent]),
    [['BUTTON', 'button', 'thm:main', 'Show Theorem 1.1 in the readable version', 'Theorem 1.1']])
  // A result the paper map does not list is plain text, never a link.
  assert.match(answer.textContent, /and nope:/)
  // Raw HTML is text, math is TeX for KaTeX, and the console's sanitizer dropped the javascript: address.
  assert.equal(answer.querySelectorAll('iframe,script').length, 0)
  assert.match(answer.textContent, /<iframe src=x><\/iframe> \$a<b\$ and x\./)
  assert.equal(answer.querySelector('a').hasAttribute('href'), false)
  assert.doesNotMatch(assigned.at(-1), /<button|javascript:/)
  // The question, the passage and the result asked about are text.
  assert.equal(done.querySelector('.ask-q').textContent, 'You<b>Why</b> does $x<y$ hold?')
  assert.equal(done.querySelector('.ask-passage').textContent, '<script>alert(1)</script> the passage')
  assert.equal(done.querySelectorAll('b,script').length, 0)
  assert.equal(done.querySelector('.ask-about').textContent, 'About Theorem 1.1')
  assert.equal(done.querySelector('.ask-meta').textContent, 'kimi-k3 · 10:42')
  // In flight, and failed.
  const pending = item({ ask_id: 'a2', status: 'running', question: 'q' }, labels)
  assert.equal(pending.querySelector('.ask-pending').textContent, 'Answering…')
  assert.equal(pending.className, 'ask-item pending')
  const failed = item({ ask_id: 'a3', status: 'failed', question: 'q', error: 'The provider refused <b>it</b>' }, labels)
  assert.equal(failed.querySelector('.ask-error').textContent, '✗ The provider refused <b>it</b>')
  assert.equal(failed.className, 'ask-item failed')
})

test('an analysis\'s answers name its results as the manuscript prints them, and their links open the Manuscript DAG', () => {
  const { document, assigned } = miniDocument()
  const item = run([...SANITIZER, ...READABLE, 'dagDetailText', 'manuscriptAskRunning', 'manuscriptAskItem', 'analysisTextAccents', 'analysisResultName', 'analysisResultNames'],
    ['SAFE_MARKDOWN_TAGS', 'READABLE_MARKER_LINE', 'READABLE_RESULT_LINK', 'ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS', 'MANUSCRIPT_ASK'],
    `const window=globalThis; let readableMarkdownParser=null; const fmtTs=()=>'10:42';`,
    // As renderManuscriptAsks draws an analysis's question: its graph's printed names, links to the Manuscript DAG.
    `((entry,graph)=>manuscriptAskItem(entry,analysisResultNames(graph),MANUSCRIPT_ASK.analyze.where))`,
    { marked: true, raw: true, context: { document, location: { href: 'https://ansatze.test/console' } } })
  // Nodes of a live analysis graph (Short Egyptian fractions): a label may carry the result's title.
  const graph = { nodes: [{ id: 'lem:residues', kind: 'lemma', label: 'Lemma 4.1', meta: { number: '4.1', env: 'lemma' } },
    { id: 'prop:density', kind: 'proposition', label: 'Proposition 2.1 (A dense set of short expansions)', meta: { number: '2.1', env: 'proposition' } }] }
  const entry = { ask_id: 'a1', status: 'done', node_id: 'prop:density', question: 'Is the gap real?', model: 'claude-opus-5-5', finished_at: '2026-10-22T10:42:00Z',
    answer: 'It rests on Lemma 4.1 [[result:lem:residues]] and on [[result:PROP:density]]; [[result:lem:gone]] is no result of this analysis, and $x<y$ <b>stays</b> text.' }
  const done = item(entry, graph)
  const refs = done.querySelector('.ask-a').querySelectorAll('.rd-ref')
  // Right after its own name a link is the compact ↗, named for screen readers; elsewhere it reads as the printed name.
  assert.deepEqual(refs.map(ref => [ref.className, ref.textContent, ref.dataset.rdRef, ref.title, ref.getAttribute('aria-label')]), [
    ['rd-ref rd-ref-jump', '↗', 'lem:residues', 'Show Lemma 4.1 in the Manuscript DAG', 'Show Lemma 4.1 in the Manuscript DAG'],
    ['rd-ref', 'Proposition 2.1', 'prop:density', 'Show Proposition 2.1 in the Manuscript DAG', null]])
  // A result the graph does not list stays plain text; raw HTML is text; math stays TeX for KaTeX.
  assert.match(done.querySelector('.ask-a').textContent, /lem:gone is no result of this analysis, and \$x<y\$ <b>stays<\/b> text\./)
  assert.equal(done.querySelectorAll('b').length, 0)
  assert.doesNotMatch(assigned.at(-1), /<button/)
  assert.equal(done.querySelector('.ask-about').textContent, 'About Proposition 2.1')
  assert.equal(done.querySelector('.ask-meta').textContent, 'claude-opus-5-5 · 10:42')
  // Before the graph has loaded, every link is a button named by its id (the panel is drawn again once it has).
  assert.deepEqual(item(entry, null).querySelector('.ask-a').querySelectorAll('.rd-ref').map(ref => ref.textContent), ['lem:residues', 'prop:density', 'lem:gone'])
})

test('a selected passage is sent as written: no line numbers or badges, result links and formulas as their source', () => {
  const { document } = miniDocument()
  const text = run(['manuscriptSelectionText', 'manuscriptFormulaAt'], [], '', 'manuscriptSelectionText', { raw: true, context: { document } })
  const fragment = html => { const box = document.createElement('div'); box.innerHTML = html; const out = document.createDocumentFragment(); out.append(...box.childNodes); return out }
  // A selection of one range whose copy is `html` (what Range.cloneContents gives for it).
  const selection = (html, { start = null, end = null } = {}) => {
    const moved = []
    return { moved, rangeCount: 1, getRangeAt: () => ({ cloneRange: () => ({ startContainer: start, endContainer: end,
      setStartBefore: node => moved.push(['start', node.className]), setEndAfter: node => moved.push(['end', node.className]), cloneContents: () => fragment(html) }) }) }
  }
  // Two lines of the Original: the second line's number (its gutter) is no part of the passage,
  // nor are the notes above the text when a selection starts there.
  assert.equal(text(selection('<span class="ln-row">tion{Setting}\n</span><span class="ln-row"><span class="ln">2</span>line two &lt;scr</span>')), 'tion{Setting} line two <scr')
  assert.equal(text(selection('<p class="analysis-live-note">The original in reading order · read-only</p><p class="rd-pdf-note">This text was extracted from a PDF</p>'
    + '<pre class="analysis-lines"><span class="ln-row"><span class="ln">1</span>\\section{Setting}\n</span></pre>')), '\\section{Setting}')
  // The Readable version across a result's anchor: no badge, formulas as TeX, a result link as written.
  const katex = (tex, display) => (display ? '<span class="katex-display">' : '')
    + `<span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>${tex}</mi></mrow><annotation encoding="application/x-tex">${tex}</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">${tex}</span></span>`
    + (display ? '</span>' : '')
  const readable = '<p class="rd-banner">Rewritten by a model to be easier to follow.</p>'
    + `<p>An integer ${katex('n')} is <em>even</em> if ${katex('n=2k')}.</p>\n`
    + '<p class="rd-anchor-row"><span class="rd-anchor" data-rd-anchor="thm:main" tabindex="-1" title="Theorem 2.1 · thm:main">Theorem 2.1</span></p>\n'
    + `<p><strong>Theorem 2.1.</strong> If ${katex('n')} is even, so is ${katex('n^2', true)} by <button type="button" class="rd-ref" data-rd-ref="def:even">Definition 1.1</button>.</p>`
  assert.equal(text(selection(readable)), 'An integer $n$ is even if $n=2k$. Theorem 2.1. If $n$ is even, so is $$n^2$$ by [[result:def:even]].')
  // A formula selected in part is taken whole (a displayed one with its display).
  const live = document.createElement('div')
  live.innerHTML = `<p>so is ${katex('n^2', true)} and ${katex('m')}</p>`
  const [displayed, inline] = live.querySelectorAll('.katex-html')
  const partial = selection(`<p>${katex('n^2', true)} and ${katex('m')}</p>`, { start: displayed.firstChild, end: inline.firstChild })
  assert.equal(text(partial), '$$n^2$$ and $m$')
  assert.deepEqual(partial.moved, [['start', 'katex-display'], ['end', 'katex']])
  const prose = selection('<p>so is</p>', { start: live.querySelector('p').firstChild, end: live.querySelector('p') })
  assert.equal(text(prose), 'so is')
  assert.deepEqual(prose.moved, [])
  assert.equal(text({ rangeCount: 0 }), '')
  assert.equal(text(null), '')
  // The selection pop-up of a readable version (or an analysis) takes a rendered passage from here.
  assert.match(consoleScript, /selText=asks&&\$\('md-view'\)\.contains\(sel\.anchorNode\)\?manuscriptSelectionText\(sel\)\|\|text:text;/)
})

// The Ask panel's record helpers and compose box, for either kind of run.
const ASK_STATE = ['isAnalysisRun', 'isReadabilityRun', 'manuscriptAskKind', 'manuscriptAskPolicy', 'manuscriptAskWords', 'manuscriptAskOff', 'manuscriptAskRunOff',
  'manuscriptAskRunning', 'manuscriptAsks', 'manuscriptAskAllowance', 'manuscriptAskReason']
test('questions: one in flight at a time, the one just sent shown at once, and guests see how many are left', () => {
  const ask = run([...ASK_STATE, 'syncManuscriptAskCompose'], ['MANUSCRIPT_ASK'], `
const manuscriptAskSent=new Map(); let manuscriptAskSending='', guest=false;
const els={}; const $=id=>els[id]||(els[id]={id,disabled:false,title:'',textContent:''});
const viewerIsGuest=()=>guest, readabilityPolicy=()=>({asks:{per_run:3}}), analyzePolicy=()=>null;`,
  `((op,run,arg)=>{
    if(op==='asks'){ manuscriptAskSent.clear(); if(arg)manuscriptAskSent.set(run.run_id,arg); return manuscriptAsks(run).map(item=>item.ask_id+':'+item.status); }
    guest=arg; syncManuscriptAskCompose(run,manuscriptAsks(run)); return [$('ask-send').disabled,$('ask-send').title,$('ask-hint').textContent];
  })`)
  ask.asks = (record, sent) => ask('asks', record, sent)
  ask.compose = (record, isGuest) => ask('compose', record, isGuest)
  const published = { run_id: 'r1', run_kind: 'readability', status: 'finished', sandbox_preview: { has_readable: true } }
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
  assert.deepEqual(ask.compose({ run_id: 'r1', run_kind: 'readability', status: 'running', sandbox_preview: {} }, false).slice(0, 2), [true, 'You can ask once the readable version is published.'])
  // The run is polled while a question is in flight, and its signature follows the questions.
  assert.match(consoleScript, /run\.readability\?\.ask\?\.status==='running';/)
  assert.match(consoleScript, /run\.readability\?\.ask\?\.status,run\.readability\?\.ask\?\.ask_id,Array\.isArray\(run\.readability\?\.asks\)\?run\.readability\.asks\.length:0,/)
  // The request names the question, the passage, its view and the result asked about.
  const send = fn('sendManuscriptAsk')
  assert.match(send, /fetch\('\/api\/run\/'\+encodeURIComponent\(rid\)\+'\/'\+spec\.endpoint,\{method:'POST'/)
  assert.match(constant('MANUSCRIPT_ASK'), /readability:\{block:'readability',endpoint:'readability\/ask',published:'has_readable',views:\['readable','original'\],/)
  assert.match(send, /const request=\{question,selection:manuscriptAskCut\(context\.selection,4000\),view:spec\.views\.includes\(context\.view\)\?context\.view:spec\.views\[0\]\};/)
  assert.match(send, /if\(context\.node_id\)request\.node_id=String\(context\.node_id\);/)
})

test('asking about a passage or a result: what is sent, and where a result link leads', async () => {
  const context = vm.createContext({ console, Date })
  vm.runInContext(`
const calls=[], posts=[], els={};
const $=id=>els[id]||(els[id]={id,value:'',textContent:'',disabled:false,hidden:false,kids:[],focus(){ calls.push('focus '+id); },replaceChildren(...kids){ this.kids=kids; }});
let manuscriptAskContext=null, manuscriptAskSending='', readabilityTab='original', runId='r1', workspaceView='dag', analysisSourceShown=false, analysisSourceRunId='';
const manuscriptAskSent=new Map();
let lastSandboxRun={run_id:'r1',run_kind:'readability',status:'finished',sandbox_preview:{has_readable:true},readability:{asks:[]}};
let reply={ok:true,status:202,body:{ok:true,ask_id:'f00dfeed0001'}};
const fetch=async(url,options)=>{ posts.push([url,JSON.parse(options.body)]); return {ok:reply.ok,status:reply.status,json:async()=>reply.body}; };
const window={getSelection:()=>({removeAllRanges(){ calls.push('unselect'); }})};
const startPolling=()=>calls.push('poll'), renderManuscriptAsks=()=>{}, syncManuscriptAskCompose=()=>{}, limitMessage=message=>message+' (limit)';
const setWorkspaceView=view=>{ workspaceView=view; calls.push('view '+view); }, setReadabilityTab=view=>{ readabilityTab=view; calls.push('tab '+view); };
const analyzeEl=(tag,cls,text)=>({tag,cls,text,onclick:null,setAttribute(){}}), paperMapCardTitle=node=>node.label, readabilityLabels=()=>new Map([['lem:gone','Lemma 9']]);
const anchors=[{dataset:{rdAnchor:'Thm:Main'}},{dataset:{rdAnchor:'thm:main'},scrollIntoView(){ calls.push('scroll'); },focus(){ calls.push('focus anchor'); },classList:{remove(){},add(name){ calls.push(name); }}}];
const document={querySelectorAll:selector=>{ calls.push(selector); return anchors; },body:{classList:{contains:()=>false}}};
let policy={enabled:true,asks:{enabled:true,per_run:3}};
const readabilityPolicy=()=>policy, analyzePolicy=()=>null, viewerIsGuest=()=>false;
${['MANUSCRIPT_ASK', 'MANUSCRIPT_ASK_VIEW_WORDS'].map(constant).join('\n')}
${[...ASK_STATE, 'shownAskKind', 'readabilitySelectionView', 'manuscriptSelectionView', 'renderManuscriptAskContext', 'manuscriptAskCut', 'askAboutSelection', 'askAboutNode', 'sendManuscriptAsk', 'readabilityJumpTo'].map(fn).join('\n')}
const state=()=>({calls:calls.splice(0),posts:posts.splice(0),context:manuscriptAskContext,shown:$('ask-context').hidden?'':$('ask-context').kids[0]?.text,input:$('ask-input').value,status:$('ask-status').textContent});`, context)
  const step = code => vm.runInContext(code, context)
  const state = () => plain(step('state()'))
  // A passage of the Original: trimmed, with its tab; the request carries both.
  step("askAboutSelection('  tion{Setting} line two <scr  ')")
  assert.deepEqual(state(), { calls: ['unselect', 'focus ask-input'], posts: [], context: { selection: 'tion{Setting} line two <scr', view: 'original' },
    shown: 'About this passage of the original: “tion{Setting} line two <scr”', input: '', status: '' })
  step("$('ask-input').value='  What is set up here?  '")
  await step('sendManuscriptAsk()')
  let after = state()
  assert.deepEqual(after.posts, [['/api/run/r1/readability/ask', { question: 'What is set up here?', selection: 'tion{Setting} line two <scr', view: 'original' }]])
  // Sent: the box is cleared, the question shows as in flight, and the run is polled for its answer.
  assert.deepEqual([after.context, after.shown, after.input, after.calls], [null, '', '', ['poll']])
  assert.deepEqual(plain(step("manuscriptAsks(lastSandboxRun).map(item=>[item.ask_id,item.status,item.view,item.selection])")),
    [['f00dfeed0001', 'running', 'original', 'tion{Setting} line two <scr']])
  // A result from the map or the DAG: its id, and the Readable version as the view.
  step("manuscriptAskSent.clear(); askAboutNode({id:'thm:main',label:'Theorem 2.1'}); $('ask-input').value='Why is it true?'")
  assert.equal(state().shown, 'About Theorem 2.1')
  await step('sendManuscriptAsk()')
  assert.deepEqual(state().posts, [['/api/run/r1/readability/ask', { question: 'Why is it true?', selection: '', view: 'readable', node_id: 'thm:main' }]])
  // A long passage is cut to 4,000 characters; a refusal is shown and keeps the question.
  step("manuscriptAskSent.clear(); readabilityTab='readable'; askAboutSelection('x'.repeat(4500)); $('ask-input').value='q'; reply={ok:false,status:429,body:{error:'Guests can ask 3 questions about a readable version'}}")
  await step('sendManuscriptAsk()')
  after = state()
  assert.equal(after.posts[0][1].selection.length, 4000)
  assert.equal(after.posts[0][1].view, 'readable')
  assert.deepEqual([after.status, after.input, after.context.view], ['✗ Guests can ask 3 questions about a readable version (limit)', 'q', 'readable'])
  // Nothing is sent while a question is on its way, or for a run that is not a readable version.
  step("manuscriptAskSending='r1'"); await step('sendManuscriptAsk()')
  step("manuscriptAskSending=''; lastSandboxRun={run_id:'r1',engine:'kimi'}"); await step('sendManuscriptAsk()')
  assert.deepEqual(state().posts, [])
  // A result link opens the Readable version at the result's anchor, found by its data attribute (never a DOM id).
  step("readabilityTab='notation'; workspaceView='dag'; readabilityJumpTo('thm:main')")
  assert.deepEqual(state().calls, ['view sandbox', 'tab readable', '#md-view .rd-anchor', 'scroll', 'focus anchor', 'flash'])
  step("readabilityJumpTo('lem:gone')")
  assert.deepEqual(state().calls, ['#md-view .rd-anchor'])
  assert.equal(step("$('readability-tab-status').textContent"), 'Lemma 9 is not marked in the readable version.')
})

test('a readable version opens on its tabs and Ask panel, with no editing, Lean or proof graphs', () => {
  const view = run(['applyRunKindView', 'syncDagModeForRun'], ['LEAN_ANALYSIS_STAGE_NAMES'], `
const els={}; const $=id=>els[id]||(els[id]={id,hidden:false,textContent:'',placeholder:'',dataset:{},attrs:{},disabled:false,setAttribute(k,v){this.attrs[k]=String(v);},
  getAttribute(k){ return k in this.attrs?this.attrs[k]:null; },removeAttribute(k){ delete this.attrs[k]; }});
const bodyClasses=new Set(), modes={};
for(const mode of ['analysis','paper-map','engine','informal','formal','compiler'])modes[mode]={hidden:mode==='analysis'||mode==='paper-map'};
const document={body:{classList:{toggle(c,on){ if(on)bodyClasses.add(c); else bodyClasses.delete(c); }}},
  querySelector:sel=>{ const m=/data-mode="([a-z-]+)"/.exec(sel); return m?modes[m[1]]:sel.includes('data-view="lean"')?$('lean-tab'):null; },
  querySelectorAll:sel=>sel.includes('#dag-mode')?[...sel.matchAll(/data-mode="([a-z-]+)"/g)].map(m=>modes[m[1]]):[]};
let dagMode='engine', dagAutoRunId='', workspaceView='lean'; const switched=[];
const setDagMode=(mode)=>{ dagMode=mode; switched.push(mode); }, setWorkspaceView=v=>{ workspaceView=v; switched.push('view:'+v); };
const ANALYZE_ASK_NOTE='analyze note', READABILITY_ASK_NOTE='Questions are answered by a model from this paper — not a certificate.';`,
  `(kind=>{ applyRunKindView(kind,'r1'); return {body:[...bodyClasses],chat:$('chat-title').textContent,proof:$('proof-title').textContent,
    shown:['readability-tabs','ask-list','ask-compose'].filter(id=>!$(id).hidden),selPop:$('sel-pop').textContent,stages:$('analysis-stages').attrs['aria-label'],
    modes:Object.keys(modes).filter(mode=>!modes[mode].hidden),dagMode,switched:switched.splice(0),workspaceView,source:[$('analysis-source-link').hidden,$('analysis-source-toggle').hidden],
    panel:{...$('md-view').attrs},notes:{...$('chat-msgs').attrs},placeholder:$('ask-input').placeholder}; })`)
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
  // The view is the tabs' panel while a readable version is shown, and only then.
  assert.deepEqual(readable.panel, { role: 'tabpanel', 'aria-labelledby': 'rd-tab-readable' })
  const proof = view('prove')
  assert.deepEqual(proof.panel, {})
  assert.deepEqual(proof.body, [])
  assert.deepEqual(proof.shown, [])
  assert.equal(proof.selPop, '💬 Add to chat')
  assert.deepEqual(proof.modes, ['engine', 'informal', 'formal', 'compiler'])
  assert.equal(proof.dagMode, 'engine')
  assert.deepEqual(proof.source, [true, true])
  const analysis = view('analyze')
  assert.deepEqual(analysis.body, ['analysis-run'])
  assert.deepEqual(analysis.modes, ['analysis', 'informal', 'formal', 'compiler'])
  // An analysis has the Ask panel too (no tabs), with its Progress notes kept above it as a named region.
  assert.equal(analysis.chat, 'Ask about this analysis')
  assert.deepEqual(analysis.shown, ['ask-list', 'ask-compose'])
  assert.equal(analysis.selPop, 'Ask about this')
  assert.deepEqual(analysis.notes, { role: 'region', 'aria-label': 'Progress notes' })
  assert.equal(analysis.placeholder, 'Ask about a result, a verdict or a step…')
  assert.deepEqual(analysis.panel, {})
  // Another kind of run gives the notes back their plain role.
  assert.deepEqual([readable.notes, view('prove').notes], [{}, {}])
  assert.equal(readable.placeholder, 'Ask about a step, a symbol or a result…')
  // What a readable version hides, and its tab strip and Ask panel, are in readability.css.
  assert.match(css, /\.readability-run #proof-edit-btn,[^{]*\.readability-run \.chat-msgs, \.readability-run \.chat-compose, \.readability-run #sess-cont,[^{]*\.readability-run \.seg-control button\[data-view="lean"\] \{ display: none !important; \}/)
  // The tabs are a tablist of their own; the status beside them is no tab.
  assert.match(html, /<div class="readability-tabs" id="readability-tabs" hidden>\s*<div class="readability-tablist" role="tablist" aria-label="Readability views">/)
  assert.match(html, /<\/div>\s*<span class="readability-tab-status" id="readability-tab-status" role="status"/)
  // Each tab has an id the panel is labelled by.
  assert.deepEqual([...html.matchAll(/role="tab" id="rd-tab-([a-z]+)" data-rd-view="([a-z]+)"[^>]*>([^<]+)</g)].map(m => [m[2], m[3], m[1] === m[2]]),
    [['readable', 'Readable version', true], ['original', 'Original', true], ['notation', 'Notation', true], ['map', 'Map', true]])
  assert.match(html, /<div class="ask-compose run-change" id="ask-compose" hidden>/)
  assert.match(html, /<script id="readability-loader">\{const base=window\.__PC_BASE__\|\|""[^<]*"\/static\/readability\.css"[^<]*"\/static\/readability\.js"[^<]*<\/script>/)
})

test('the DAG goes straight from one run kind\'s graph to another\'s, never through a mode its buttons hide', () => {
  const sync = run(['syncDagModeForRun'], [], `
const modes={}; for(const mode of ['analysis','paper-map','engine','informal','formal','compiler'])modes[mode]={hidden:false};
const document={querySelector:sel=>modes[/data-mode="([a-z-]+)"/.exec(sel)[1]],querySelectorAll:sel=>[...sel.matchAll(/data-mode="([a-z-]+)"/g)].map(m=>modes[m[1]])};
let dagMode='engine', dagAutoRunId='', workspaceView='dag'; const switched=[];
const setDagMode=(mode,options)=>{ dagMode=mode; switched.push(mode+(options.refresh?' (reload)':'')); };`,
  `((kind,rid,from)=>{ if(from)dagMode=from; syncDagModeForRun(kind,rid);
    return {mode:dagMode,switched:switched.splice(0),shown:Object.keys(modes).filter(mode=>!modes[mode].hidden)}; })`)
  const readable = { mode: 'paper-map', switched: ['paper-map (reload)'], shown: ['paper-map'] }
  assert.deepEqual(sync('readability', 'r1'), readable)
  // From a readable version to an analysis and back: one switch each, to the run's own graph.
  assert.deepEqual(sync('analyze', 'a1'), { mode: 'analysis', switched: ['analysis (reload)'], shown: ['analysis', 'informal', 'formal', 'compiler'] })
  assert.deepEqual(sync('readability', 'r1'), readable)
  // Polling the same run changes nothing.
  assert.deepEqual(sync('readability', 'r1').switched, [])
  // An analysis keeps the Outline chosen for it; without its run id it still leaves the Paper map for its own graph.
  sync('analyze', 'a1')
  assert.deepEqual(sync('analyze', 'a1', 'informal'), { mode: 'informal', switched: [], shown: ['analysis', 'informal', 'formal', 'compiler'] })
  assert.deepEqual(sync('analyze', '', 'paper-map').switched, ['analysis (reload)'])
  // A proof run leaves either manuscript graph for the CGM graph, and keeps its own modes.
  assert.deepEqual(sync('prove', 'p1', 'paper-map'), { mode: 'engine', switched: ['engine (reload)'], shown: ['engine', 'informal', 'formal', 'compiler'] })
  assert.deepEqual(sync('prove', 'p2', 'analysis').mode, 'engine')
  assert.deepEqual(sync('prove', 'p3', 'formal').switched, [])
})

test('before readability.js has loaded, a readable version waits instead of failing; on load it is drawn and polled again', async () => {
  const els = {}, calls = []
  const context = vm.createContext({ console, calls, els })
  vm.runInContext(`
const $=id=>els[id]||(els[id]={id,textContent:'',title:'',className:'',innerHTML:'',style:{},lastElementChild:{textContent:''},classList:{add(){},remove(){}},addEventListener(){}});
let lastSandboxRun=null, runId='r1', workspaceView='sandbox', dagMode='paper-map', lastProcText='', readabilityUiMissing=false;
let dagRefreshSeq=0, dagDisplayedRunId='', dagDisplayedMode='', dagDisplayedEngineView='', dagEngineView='workflow';
const RUN_KIND_BY_ID=new Map(), STAGE_LABELS={};
const syncAnalyzeRunUi=()=>{}, analysisStageStates=()=>[], updateLiveChips=()=>{}, updateSessionButtons=()=>{}, analysisLogLines=raw=>raw.split('\\n');
const setDagObservation=()=>{}, dagObservationText=()=>'', dagStatus=text=>calls.push('status: '+text);
const fetch=async url=>{ calls.push('fetch '+url); return {ok:false}; };
${['READABILITY_STAGES', 'ANALYZE_STAGES'].map(constant).join('\n')}
${['isAnalysisRun', 'isReadabilityRun', 'readabilityUiReady', 'readabilityUiUnavailable', 'showReadabilityPending', 'applySandboxPreview', 'updateChatStatus', 'updateProcLog', 'refreshDagView'].map(fn).join('\n')}`, context)
  const record = { run_id: 'r1', run_kind: 'readability', status: 'running', agents: [{ stage_name: 'readability', pipeline_stage: 'verify', output: 'assistant · {"verdict": "faithful"}' }], sandbox_preview: { kind: 'readability', text: 'Readability · 3 Rewrite · part 2 of 4' } }
  context.record = record
  // Nothing the console asks of readability.js throws while it is not there: the poll goes on.
  vm.runInContext('updateChatStatus(record); updateProcLog(record); applySandboxPreview(record);', context)
  assert.deepEqual([els['chat-stage'].textContent, els['chat-detail'].textContent, els['proc-body'].textContent], ['Rewrite…', '', ''])
  assert.deepEqual([els['proof-label'].textContent, els['proof-empty'].lastElementChild.textContent], ['loading…', 'Loading the readable version…'])
  // The Paper map waits too, without asking the server for it.
  await vm.runInContext('refreshDagView()', context)
  assert.equal(calls.filter(call => call.startsWith('fetch')).length, 0)
  assert.match(els['dag-empty'].innerHTML, /Loading the selected proof graph…/)
  // A file that could not be loaded says so.
  vm.runInContext('workspaceView="dag"; refreshDagView=async()=>{ calls.push("dag refresh"); }; readabilityUiUnavailable();', context)
  assert.deepEqual([els['proof-label'].textContent, els['proof-empty'].lastElementChild.textContent], ['not loaded', 'The readable version could not be loaded. Reload the page to try again.'])
  assert.ok(calls.includes('dag refresh'))
  assert.match(fn('refreshDagView'), /if\(readabilityUiMissing\)\{ \$\('dag-empty'\)\.innerHTML='[^']*The Paper map could not be loaded\. Reload the page to try again\.[^']*'; dagStatus\('paper map unavailable'\); \}\n\s*return;/)
  assert.match(html, /s\.src=base\+"\/static\/readability\.js";s\.onerror=\(\)=>readabilityUiUnavailable\(\);document\.body\.append\(s\);/)
  // Once loaded, the same calls reach it.
  vm.runInContext(`function initReadabilityUi(){} function applyReadabilityPreview(run){ calls.push('drawn '+run.run_id); return true; }
    function readabilityRunProgress(run){ return ['3 Rewrite · part 2 of 4']; } function readabilityProgressDetail(lines){ return lines.at(-1); }`, context)
  vm.runInContext('updateChatStatus(record); applySandboxPreview(record);', context)
  assert.equal(els['chat-detail'].textContent, '3 Rewrite · part 2 of 4')
  assert.equal(calls.at(-1), 'drawn r1')

  // readability.js, when it arrives, draws the selected readable version and polls it again (and its Paper map).
  const init = vm.createContext({ console })
  vm.runInContext(`
const calls=[], handlers={};
const $=id=>handlers[id]||(handlers[id]={addEventListener(){}});
const document={querySelectorAll:()=>[]};
let runId='', lastSandboxRun=null, workspaceView='sandbox', dagMode='engine';
const RUN_KIND_BY_ID=new Map();
const applySandboxPreview=run=>calls.push('draw '+run.run_id), startPolling=()=>calls.push('poll'), refreshDagView=()=>calls.push('dag');
const sendManuscriptAsk=()=>{}, setReadabilityTab=()=>{}, manuscriptAskJump=()=>{}, shownAskKind=()=>'', renderManuscriptAsks=run=>calls.push('asks '+run.run_id);
${fn('isAnalysisRun')}
${fn('isReadabilityRun')}
${fn('initReadabilityUi')}
const load=(rid,record,kind,view,mode)=>{ runId=rid; lastSandboxRun=record; RUN_KIND_BY_ID.clear(); if(kind)RUN_KIND_BY_ID.set(rid,kind); workspaceView=view; dagMode=mode; initReadabilityUi(); return calls.splice(0); };`, init)
  const loaded = (...args) => plain(vm.runInContext('load', init)(...args))
  assert.deepEqual(loaded('r1', { run_id: 'r1', run_kind: 'readability' }, 'readability', 'dag', 'paper-map'), ['draw r1', 'poll', 'dag'])
  // Its record not yet in: polling brings it.
  assert.deepEqual(loaded('r1', null, 'readability', 'sandbox', 'paper-map'), ['poll'])
  // An analysis already drawn gets its Ask panel (its record is polled by the console as before).
  assert.deepEqual(loaded('a1', { run_id: 'a1', run_kind: 'analyze' }, 'analyze', 'dag', 'analysis'), ['asks a1'])
  assert.deepEqual(loaded('a1', null, 'analyze', 'sandbox', 'analysis'), [])
  // Another kind of run, or none, is left alone.
  assert.deepEqual(loaded('p1', { run_id: 'p1', engine: 'kimi' }, 'prove', 'dag', 'engine'), [])
  assert.deepEqual(loaded('', null, '', 'sandbox', 'engine'), [])
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
${constant('STOP_SETTLE_MS')}
${constant('STOPPING_RUNS')}
${fn('stopSettling')}
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
  const status = run(['dagDetailText', 'paperMapSizeText', 'paperMapStatusText'], [], '', `[paperMapStatusText({graph:{title:'Short proofs',nodes:[{},{}],edges:[{}]}}),paperMapStatusText({graph:{nodes:[{}],edges:[]}})]`)
  assert.deepEqual(status, ['Paper map · Short proofs · 2 statements · 1 dependency', 'Paper map · 1 statement · 0 dependencies'])
  const title = run(['dagDetailText', 'dagMathSafeClip', 'analysisTextAccents', 'paperMapCardTitle'], ['ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS'], '', 'paperMapCardTitle')
  assert.equal(title({ id: 'lem:a', label: 'Lemma 3.2', title: 'Erd\\H{o}s bound' }), 'Lemma 3.2 · Erdős bound')
  assert.equal(title({ id: 'lem:a', label: '', title: '' }), 'lem:a')
  // \ref{<id>} reads as the result's number.
  const refs = run(['dagDetailText', 'paperMapRefLabels'], [], '', `[...paperMapRefLabels({nodes:[{id:'lem:a',label:'Lemma 3.2'},{id:'def:b',label:'Definition'}]})]`)
  assert.deepEqual(refs, [['lem:a', { number: '3.2', name: 'Lemma 3.2' }], ['def:b', { number: 'Definition', name: 'Definition' }]])
  // An unnumbered result reads as its title, as in the readable version.
  const titled = run(['dagDetailText', 'paperMapRefLabels'], [], '', `[...paperMapRefLabels({nodes:[{id:'def:t',label:'Definition',title:'The full subsequence test'},{id:'lem:n',label:'Lemma 3.6',title:'Representatives'}]})]`)
  assert.deepEqual(titled, [['def:t', { number: '(The full subsequence test)', name: 'Definition (The full subsequence test)' }],
    ['lem:n', { number: '3.6', name: 'Lemma 3.6' }]])
})

test('paper map statements read text markup around math as text, in the console and on the shared page', () => {
  const prose = run(['analysisTextAccents', 'analysisResolveRefs', 'analysisUnresolvedLabel', 'analysisProse', 'dagDetailText', 'paperMapRefLabels'],
    ['ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS', 'ANALYZE_REF_TIE', 'ANALYZE_LABEL_KINDS'], '',
    `(text)=>analysisProse(text,paperMapRefLabels({nodes:[{id:'def:t',label:'Definition',title:'The full subsequence test'}]}))`)
  assert.equal(prose('An \\emph{arity-$k$ diagram} is a triple $\\mathcal D=(E,R,\\rho)$.'), 'An arity-$k$ diagram is a triple $\\mathcal D=(E,R,\\rho)$.')
  assert.equal(prose('Suppose the test in Definition~\\ref{def:t} passes for \\textbf{every $J\\ne\\emptyset$}.'),
    'Suppose the test in Definition (The full subsequence test) passes for every $J\\ne\\emptyset$.')
  // Lists read inline with the manuscript's own labels (live: a definition's conditions showed \\begin{enumerate}).
  assert.equal(prose('It is valid if: \\begin{enumerate}[label=\\textup{(\\roman*)}] \\item $E$ is an equivalence. \\item On $Q$, $R$ decreases tags. \\end{enumerate} Done.'),
    'It is valid if: (i) $E$ is an equivalence. (ii) On $Q$, $R$ decreases tags. Done.')
  assert.equal(prose('\\begin{itemize}\\item one \\item[(b)] two\\end{itemize}'), '• one (b) two')
  assert.equal(prose('\\begin{enumerate}\\item a \\item b \\begin{enumerate}[(a)]\\item c\\end{enumerate} \\item d\\end{enumerate}'), '1. a 2. b (a) c 3. d')
  assert.equal(prose('\\begin{enumerate}[I.]\\item a \\item b\\end{enumerate}'), 'I. a II. b')
  assert.equal(prose('\\begin{enumerate}[label={\\rm (\\alph*)}]\\item a \\item b\\end{enumerate}'), '(a) a (b) b')
  assert.equal(prose('\\begin{enumerate}[label=\\upshape(\\roman*)]\\item a\\end{enumerate}'), '(i) a')
  // Markup around no math, and math itself, read as before.
  assert.equal(prose('\\emph{valid} and $\\emph{x}$, \\emph{a $x$ {b}}'), 'valid and $\\emph{x}$, \\emph{a $x$ {b}}')
  const sandbox = { window: {}, document: {}, NodeFilter: {}, location: { href: 'https://example.test/' } }
  vm.runInNewContext(readFileSync(new URL('../agent_monitor/web/run-viewer-text.js', import.meta.url), 'utf8'), sandbox)
  const shared = sandbox.window.AnsatzSharedText.manuscriptProse
  assert.equal(shared('An \\emph{arity-$k$ diagram} is a triple.'), 'An arity-$k$ diagram is a triple.')
  assert.equal(shared('\\emph{valid} and $\\emph{x}$'), 'valid and $\\emph{x}$')
  // Hostile text stays linear.
  for (const text of ['\\emph{a$x$'.repeat(40000), '\\emph{' + '$x$'.repeat(60000), '\\begin{enumerate}[label=('.repeat(20000), '\\item[$x$'.repeat(30000),
    '\\begin{enumerate}'.repeat(20000) + '\\end{itemize}'.repeat(20000)]) {
    const started = performance.now()
    prose(text); shared(text)
    assert.ok(performance.now() - started < 3000)
  }
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
    'Readability · guide', '{"title": "x"}', '=== READABLE ===', '```json', 'Readability · rewrite-2-low', 'Readability · recheck-3',
    'Readability · rewrite-2-low · declined first by claude-fable-5-1', 'assistant · {"verdict": "faithful"}',
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
  assert.deepEqual(shared.parts({ kind: 'readability', readability: { notation: 'junk', paper_map: [] } }), { readable: '', rows: [], terms: [], nodes: [], edges: 0, macros: {} })
  assert.equal(shared.withAnchorTokens('a\n[result:Thm:Main]: #\nb\n[result:x y]: #'), 'a\n\n⟦result:thm:main⟧\n\nb\n[result:x y]: #')
  assert.deepEqual([3, '5', 9, 'x'].map(importance => shared.importance({ importance })), [3, 5, 1, 1])
  assert.equal(shared.sectionLabel([{ number: '2', title: 'Proofs' }]), '2 Proofs')
  const viewer = read('../agent_monitor/web/run-viewer.js'), page = read('../agent_monitor/web/run-viewer.html')
  assert.match(viewer, /readable = data\.kind === 'readability';/)
  assert.match(viewer, /window\.AnsatzSharedReadability\.render\(data, \{ target: \$\('rv-proof-text'\), renderText \}\)/)
  // Its source link is the manuscript's, and the arrow keys move between the tabs on show (no Lean, no DAG).
  assert.match(viewer, /\$\('rv-source'\)\.textContent = analysis \|\| readable \? 'Source manuscript ↗' : 'Source problem ↗';/)
  assert.match(viewer, /button\.addEventListener\('keydown', event => \{\n[^\n]*\n\s*const buttons = all\('\[data-tab\]'\)\.filter\(item => !item\.hidden\);/)
  assert.match(page, /<script defer src="__ASSET_BASE__\/run-viewer-readability\.js"><\/script>\s*<script defer src="__ASSET_BASE__\/run-viewer\.js"><\/script>/)
  const sharing = read('../agent_monitor/web/sharing.js')
  assert.match(sharing, /readability:'Anyone with the link can view a read-only snapshot of the readable version, its notation table and its paper map/)
  assert.match(sharing, /The original manuscript, the questions asked about it, account settings/)
  assert.match(sharing, /run\.run_kind==='readability'\|\|run\.engine==='readability'\?'readability'/)
  assert.match(read('../agent_monitor/web/projects.js'), /tag\.textContent=readable\?'Readable':'Analyze'/)
})

test('the shared viewer\'s arrow keys skip the tabs a readable version hides', async () => {
  // run-viewer.js with a stub page whose snapshot cannot be fetched: its tab keys are wired all the same.
  const element = extra => ({ hidden: false, disabled: false, textContent: '', dataset: {}, attrs: {}, listeners: {}, style: {}, classList: { add() {}, remove() {} },
    setAttribute(name, value) { this.attrs[name] = String(value) }, addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener) },
    append() {}, replaceChildren() {}, focus() {}, ...extra })
  const tabs = ['sandbox', 'lean', 'monitor', 'dag'].map(tab => element({ dataset: { tab } }))
  const els = {}
  const document = { getElementById: id => (els[id] ||= element()), querySelectorAll: selector => selector === '[data-tab]' ? tabs : [],
    createElement: () => element(), body: { dataset: { snapshotUrl: '/shared/x/data' } }, documentElement: { dataset: {} } }
  vm.runInNewContext(read('../agent_monitor/web/run-viewer.js'), { document, window: {}, console, setTimeout, clearTimeout, fetch: async () => { throw new Error('offline') } })
  await new Promise(resolve => setImmediate(resolve))
  // As a readable version shows it: Sandbox and Monitor only.
  tabs[1].hidden = tabs[3].hidden = true
  const press = (from, key) => {
    tabs.find(tab => tab.dataset.tab === from).listeners.keydown[0]({ key, preventDefault() {} })
    return tabs.find(tab => tab.attrs['aria-selected'] === 'true').dataset.tab
  }
  assert.equal(press('sandbox', 'ArrowRight'), 'monitor')
  assert.equal(press('monitor', 'ArrowRight'), 'sandbox')
  assert.equal(press('sandbox', 'ArrowLeft'), 'monitor')
  assert.equal(press('monitor', 'Home'), 'sandbox')
  assert.equal(press('sandbox', 'End'), 'monitor')
  assert.equal(els['rv-monitor'].hidden, false)
  // A proof run's four tabs are all reachable.
  tabs[1].hidden = tabs[3].hidden = false
  assert.equal(press('sandbox', 'ArrowRight'), 'lean')
  assert.equal(press('sandbox', 'End'), 'dag')
})

// ── Check notes: the minor points of a part's check, collapsed under it ──
// A live readable version (rd-chpaper, Claude) cut short: its title and
// provenance, then three parts; the third as a part with a major problem reads.
const CHECKED_READABLE = [
  '# A CH obstruction to a prescribed categoricity threshold — Readable version', '',
  '> **Readable version** for researchers in the field, written by opus via a Claude account. Every statement and argument is meant to be kept; an independent model check compared each part with the original: 2 of 3 parts faithful, 1 with issues (marked), 0 unchecked. This is not a certificate of correctness.', '',
  '[part:1]: #', '',
  '## Abstract', '',
  'Assuming the continuum hypothesis (CH), we construct an abstract elementary class (AEC) with Löwenheim–Skolem number $\\aleph_0$.', '',
  '[result:thm:main]: #', '',
  '**Theorem 1.1.** Assume CH. There is an abstract elementary class $K$ in a countable finitary relational language such that', '',
  '[part:2]: #', '',
  '## 3. The abstract elementary class', '',
  'This section turns the color tests of Section 2 into a class of structures $K=(\\mathcal C,\\le_K)$.', '',
  '[part:3]: #', '',
  '> ⚠ **Check:** the independent check found problems that change the meaning here; compare with the original.',
  '> - **Lemma 4.4:** the case $n=0$ is missing.', '',
  '## 4. Two models at the prescribed endpoint', '',
  'Both models have cardinality $\\beth_{\\omega_2}$.', '',
].join('\n')
// sandbox_preview.check as the console server sends it (check.json's parts, bounded).
const CHECKED_PARTS = [
  { index: 1, title: 'Introduction', kind: 'text', status: 'faithful', reason: '', lines: [1, 120], repaired: false, missing: [], issues: [
    { severity: 'minor', location: "Section 1.1, paragraph 'Why H(K)'", problem: "The original says a model 'at that size' (H(K)) gives arbitrarily large models. The rewrite says 'of size at least H(K)'.",
      fix: "Write 'If K has a model of size H(K)', or add a note that the two forms are equivalent by the Löwenheim–Skolem axiom." }] },
  { index: 2, title: 'The abstract elementary class', kind: 'text', status: 'faithful', reason: '', lines: [121, 300], repaired: false, missing: [], issues: [
    { severity: 'minor', location: 'Remarks before Lemma 3.4 and before Lemma 3.8', problem: 'The rewrite adds forward-looking claims not in the original.' },
    { severity: 'minor', location: 'Section 3.2 introductory sentence', problem: 'The rewrite writes $S=2^\\omega/{=^*}$ explicitly <img src=x onerror=alert(1)>.' }] },
  { index: 3, title: 'Two models', kind: 'text', status: 'issues', reason: '', lines: [301, 400], repaired: true, missing: [], issues: [
    { severity: 'major', location: 'Lemma 4.4', problem: 'the case $n=0$ is missing.' },
    { severity: 'minor', location: 'unspecified', problem: 'A sentence is long.' }] },
  { index: 4, title: 'Nothing to note', status: 'faithful', issues: [] }, 'junk', { index: '5', issues: [{ severity: 'minor', problem: 'x' }] },
]
const READABLE_VIEW = ['readableParts', 'readabilityCheckNotes', 'readabilityCheckNotesBox', 'markReadableBlocks', 'readabilityCounts', 'readabilityProvenanceText', 'dagDetailText', 'renderReadableVersion']

function readableVersionPage() {
  const { document } = miniDocument()
  return run([...SANITIZER, ...READABLE, ...READABLE_VIEW], ['SAFE_MARKDOWN_TAGS', 'READABLE_MARKER_LINE', 'READABLE_RESULT_LINK', 'READABLE_PART_LINE'],
    `const window=globalThis; let readableMarkdownParser=null; const typeset=[];
const body={scrollTop:0}, $=id=>id==='proof-body'?body:null;
const readabilityLabels=()=>new Map([['thm:main','Theorem 1.1']]), readabilityMacros=()=>({}), renderMathIn=(el,macros)=>typeset.push(el);`,
    `(run=>{ const view=document.createElement('div'); renderReadableVersion(run,view); return {view,typeset,again:()=>{ renderReadableVersion(run,view); return typeset.length; }}; })`,
    { marked: true, raw: true, context: { document, location: { href: 'https://ansatze.test/console' } } })
}

test('a part\'s minor check notes follow it, collapsed; its major problems stay the "⚠ Check" note in the text', () => {
  const render = readableVersionPage()
  const run_ = { run_id: 'r1', status: 'finished', sandbox_preview: { kind: 'readability', has_readable: true, text: CHECKED_READABLE, check: CHECKED_PARTS } }
  const { view, typeset, again } = render(run_)
  const doc = view.querySelector('.rd-doc')
  // The title and provenance, then each part in a section of its own.
  assert.deepEqual(doc.children.map(el => el.tagName + (el.className ? '.' + el.className : '') + (el.dataset.rdPart ? '#' + el.dataset.rdPart : '')),
    ['H1', 'BLOCKQUOTE.rd-provenance', 'SECTION.rd-part#1', 'SECTION.rd-part#2', 'SECTION.rd-part#3'])
  const [one, two, three] = doc.querySelectorAll('section')
  const notesOf = section => section.querySelectorAll('details').map(box => ({
    last: section.lastChild === box, cls: box.className, summary: box.querySelector('summary').textContent,
    items: box.querySelectorAll('li').map(item => item.textContent),
  }))
  assert.deepEqual(notesOf(one), [{ last: true, cls: 'rd-check-notes', summary: 'Check notes (1)',
    items: ["Section 1.1, paragraph 'Why H(K)': The original says a model 'at that size' (H(K)) gives arbitrarily large models. The rewrite says 'of size at least H(K)'."
      + " Suggested fix: Write 'If K has a model of size H(K)', or add a note that the two forms are equivalent by the Löwenheim–Skolem axiom."] }])
  assert.equal(one.querySelector('.rd-check-fix').textContent.trim().slice(0, 15), 'Suggested fix: ')
  assert.deepEqual(notesOf(two).map(box => [box.summary, box.items.length]), [['Check notes (2)', 2]])
  // The checker's words are text, never markup; math in them is typeset with the rest.
  assert.equal(doc.querySelectorAll('img').length, 0)
  assert.match(notesOf(two)[0].items[1], /\$S=2\^\\omega\/\{=\^\*\}\$ explicitly <img src=x onerror=alert\(1\)>\.$/)
  assert.equal(typeset.length, 1)
  assert.equal(typeset[0], doc)
  // A major problem is the part's "⚠ Check" note; only the minor point is a check note, without "unspecified".
  assert.equal(three.querySelector('blockquote').className, 'rd-check')
  assert.deepEqual(notesOf(three).map(box => box.items), [['A sentence is long.']])
  assert.match(three.querySelector('.rd-check-foot').textContent, /not a certificate/)
  // Anchors still mark the results; no part mark or token is left.
  assert.equal(one.querySelector('.rd-anchor').dataset.rdAnchor, 'thm:main')
  assert.doesNotMatch(doc.textContent, /\[part:|\[result:|@@/)
  // Drawn once; the same record is not drawn again.
  assert.equal(again(), 1)
})

test('readable.md splits at the runner\'s part marks only', () => {
  const parts = run(['readableParts'], ['READABLE_PART_LINE'], '', 'readableParts')
  assert.deepEqual(parts('# T\n\n[part:1]: #\n\nA\n\n[part:12]: #\n\nB\n').map(part => [part.index, part.text]),
    [[null, '# T\n\n'], [1, '\n\nA\n\n'], [12, '\n\nB\n']])
  // No part marks: one piece. The original text a part repeats has mark-like lines indented, so they mark nothing.
  assert.deepEqual(parts('# T\n\nBody'), [{ index: null, text: '# T\n\nBody' }])
  assert.deepEqual(parts('~~~~text\n [part:2]: #\n~~~~\n[part:x]: #\n[part:3]: # \n').length, 1)
  assert.deepEqual(parts('[part:1]: #\r\nA').map(part => [part.index, part.text]), [[null, ''], [1, '\nA']])
  // Notes are taken from well-formed parts only, minor ones, at most 12 a part.
  const notes = run(['dagDetailText', 'readabilityCheckNotes'], [], '', `(check=>[...readabilityCheckNotes({sandbox_preview:{check}})])`)
  assert.deepEqual(notes(CHECKED_PARTS).map(([index, list]) => [index, list.length]), [[1, 1], [2, 2], [3, 1]])
  assert.deepEqual(notes([{ index: 1, issues: Array.from({ length: 20 }, (_, i) => ({ severity: 'minor', problem: 'p' + i })) }])[0][1].length, 12)
  assert.deepEqual(notes([{ index: 1, issues: [{ severity: 'MINOR', problem: 'x' }, { severity: 'minor', problem: '  ' }, { severity: 'minor', problem: { toString: () => 'x' } }, null] }]), [])
  assert.deepEqual(notes('junk'), [])
  // A passage selected across a part's end leaves its check notes out.
  const { document } = miniDocument()
  const selected = run(['manuscriptSelectionText', 'manuscriptFormulaAt'], [], '', 'manuscriptSelectionText', { raw: true, context: { document } })
  const fragment = html => { const box = document.createElement('div'); box.innerHTML = html; const out = document.createDocumentFragment(); out.append(...box.childNodes); return out }
  const selection = html => ({ rangeCount: 1, getRangeAt: () => ({ cloneRange: () => ({ startContainer: null, endContainer: null, setStartBefore() {}, setEndAfter() {}, cloneContents: () => fragment(html) }) }) })
  assert.equal(selected(selection('<p>the end of part one.</p>\n<details class="rd-check-notes"><summary>Check notes (1)</summary><ul><li>x</li></ul></details>\n<h2>3. The class</h2>')),
    'the end of part one. 3. The class')
})

test('the shared page shows the same check notes under each part of a readable version', () => {
  const require = createRequire(import.meta.url)
  const shared = require('../agent_monitor/web/run-viewer-readability.js')
  const snapshot = { kind: 'readability', proof: CHECKED_READABLE, readability: { check: CHECKED_PARTS.map(part => typeof part === 'object' ? { index: part.index, title: part.title, status: part.status, issues: part.issues } : part) } }
  assert.deepEqual([...shared.checkNotes(snapshot)].map(([index, list]) => [index, list.map(note => note.location)]),
    [[1, ["Section 1.1, paragraph 'Why H(K)'"]], [2, ['Remarks before Lemma 3.4 and before Lemma 3.8', 'Section 3.2 introductory sentence']], [3, ['unspecified']]])
  assert.deepEqual(shared.readableParts('a\n[part:2]: #\nb').map(part => part.index), [null, 2])
  // Drawn into a page: each part rendered on its own (with the author's macros), its notes after it.
  const { document } = miniDocument()
  const context = vm.createContext({ console, document })
  vm.runInContext(markedSource, context)
  const renders = [], typeset = []
  context.window = { AnsatzSharedText: { math: (element, options) => typeset.push([element.className, options?.macros]) } }
  vm.runInContext(read('../agent_monitor/web/run-viewer-readability.js'), context)
  const page = document.createElement('div'), target = document.createElement('article')
  page.append(target)
  const renderText = (element, value, options) => { renders.push([value.split('\n').find(line => line.trim()) || '', options?.macros]); element.innerHTML = context.marked.parse(value) }
  context.window.AnsatzSharedReadability.render({ ...snapshot, readability: { ...snapshot.readability, paper_map: { nodes: [{ id: 'thm:main', label: 'Theorem 1.1' }], macros: { '\\R': '\\mathbb{R}' } } } }, { target, renderText })
  const macros = { '\\R': '\\mathbb{R}' }
  assert.deepEqual(plain(renders), [['# A CH obstruction to a prescribed categoricity threshold — Readable version', macros], ['## Abstract', macros], ['## 3. The abstract elementary class', macros],
    ['> ⚠ **Check:** the independent check found problems that change the meaning here; compare with the original.', macros]])
  assert.deepEqual(target.children.map(el => el.tagName + '.' + el.className), ['H1.', 'BLOCKQUOTE.rv-provenance', 'SECTION.rv-rd-part', 'SECTION.rv-rd-part', 'SECTION.rv-rd-part'])
  const [one, two, three] = target.querySelectorAll('section')
  for (const [section, summary] of [[one, 'Check notes (1)'], [two, 'Check notes (2)'], [three, 'Check notes (1)']]) {
    const box = section.lastChild
    assert.deepEqual([box.tagName, box.className, box.querySelector('summary').textContent], ['DETAILS', 'rv-check-notes', summary])
  }
  assert.equal(three.querySelector('blockquote').className, 'rv-check')
  assert.equal(three.lastChild.querySelector('li').textContent, 'A sentence is long.')
  assert.match(one.lastChild.querySelector('.rv-check-fix').textContent, /^ Suggested fix: Write 'If K has a model of size H\(K\)'/)
  assert.equal(one.querySelector('.rv-anchor').textContent, 'Theorem 1.1')
  assert.equal(target.querySelectorAll('img').length, 0)
  // The notes' math is typeset with the author's macros, as are the notation and map sections.
  assert.deepEqual(plain(typeset.filter(([cls]) => cls === 'rv-check-notes')), [['rv-check-notes', macros], ['rv-check-notes', macros], ['rv-check-notes', macros]])
  assert.match(read('../agent_monitor/web/run-viewer.css'), /\.rv-markdown details\.rv-check-notes\{/)
})
