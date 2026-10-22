// The written proof of a CGM run in the console Sandbox and on the shared page,
// and the display-only lemma names for closures rendered before v2. The real
// console functions, the shared viewer scripts and the vendored marked run in
// vm contexts over a small DOM (enough HTML parsing for both sanitizers).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const consoleHtml = read('../agent_monitor/web/console.html')
const script = consoleHtml.split('<script>').at(-1).split('</script>')[0]
const viewerHtml = read('../agent_monitor/web/run-viewer.html')
const viewerJs = read('../agent_monitor/web/run-viewer.js')
const viewerTextJs = read('../agent_monitor/web/run-viewer-text.js')
const markedJs = read('../agent_monitor/web/vendor/shared/marked.min.js')
// A real closure published before rendering v2: Lemma 3 cites "L1" (Lemma 2) and "L2" (Lemma 1).
const CLOSURE = JSON.parse(read('./fixtures/cgm_phase1_artifacts.json')).cases['live-oddsum-kimi-k3'].files['proof.md']
const WRITEUP = [
  '# Written proof', '',
  '> Written from the verifier-accepted facts below (proof.md) and then checked as a whole by an independent, response-only model verifier that saw only the problem and this text. Model-verifier acceptance is not a Lean or other formal certificate.', '',
  '**Theorem.** For every natural number $n$, $1 + 3 + \\dots + (2n-1) = n^2$.', '',
  '**Proof.** The sum telescopes: $$\\sum_{k=1}^{n} (2k-1) = \\sum_{k=1}^{n} \\bigl(k^2 - (k-1)^2\\bigr) = n^2.$$ $\\blacksquare$', '',
].join('\n')
const BANNER = {
  accepted: '✓ Written from the verified facts and checked as a whole by an independent model verifier — not a formal certificate.',
  rejected: '⚠ Not accepted — the independent check found problems (listed below). The verified facts remain the proof.',
  unchecked: 'Not checked — the verified facts remain the proof.',
}
const STALE = 'This write-up was made for an earlier version of proof.md.'

// ── a small DOM ──
const VOID_TAGS = new Set(['AREA', 'BASE', 'BR', 'COL', 'EMBED', 'HR', 'IMG', 'INPUT', 'LINK', 'META', 'SOURCE', 'TRACK', 'WBR'])
const RAW_TAGS = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'TITLE'])
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
const decode = text => text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name) => name[0] === '#'
  ? String.fromCodePoint(/^#x/i.test(name) ? parseInt(name.slice(2), 16) : Number(name.slice(1)))
  : ENTITIES[name.toLowerCase()] ?? whole)
const escapeText = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const dataAttribute = key => 'data-' + String(key).replace(/[A-Z]/g, ch => '-' + ch.toLowerCase())

class TextNode {
  constructor(data) { this.nodeType = 3; this.data = String(data); this.parentNode = null }
  get textContent() { return this.data }
  get outerHTML() { return escapeText(this.data) }
}
class Elem {
  constructor(tag, ownerDocument) {
    Object.assign(this, { nodeType: 1, tagName: String(tag).toUpperCase(), attrs: [], childNodes: [], parentNode: null, style: {}, listeners: {}, ownerDocument })
  }
  getAttribute(name) { const hit = this.attrs.find(([key]) => key === String(name).toLowerCase()); return hit ? hit[1] : null }
  setAttribute(name, value) {
    const key = String(name).toLowerCase(), hit = this.attrs.find(([k]) => k === key)
    if (hit) hit[1] = String(value); else this.attrs.push([key, String(value)])
  }
  removeAttribute(name) { this.attrs = this.attrs.filter(([key]) => key !== String(name).toLowerCase()) }
  hasAttribute(name) { return this.getAttribute(name) !== null }
  get attributes() { return this.attrs.map(([name, value]) => ({ name, value })) }
  get id() { return this.getAttribute('id') || '' }
  get className() { return this.getAttribute('class') || '' }
  set className(value) { this.setAttribute('class', value) }
  get title() { return this.getAttribute('title') || '' }
  set title(value) { this.setAttribute('title', value) }
  set href(value) { this.setAttribute('href', value) }
  set rel(value) { this.setAttribute('rel', value) }
  set referrerPolicy(value) { this.setAttribute('referrerpolicy', value) }
  set type(value) { this.setAttribute('type', value) }
  get hidden() { return this.hasAttribute('hidden') }
  set hidden(on) { if (on) this.setAttribute('hidden', ''); else this.removeAttribute('hidden') }
  get disabled() { return this.hasAttribute('disabled') }
  set disabled(on) { if (on) this.setAttribute('disabled', ''); else this.removeAttribute('disabled') }
  get tabIndex() { return Number(this.getAttribute('tabindex') ?? 0) }
  set tabIndex(value) { this.setAttribute('tabindex', value) }
  get classList() {
    const el = this, names = () => el.className.split(/\s+/).filter(Boolean)
    return {
      add(...more) { el.className = [...new Set([...names(), ...more])].join(' ') },
      remove(...less) { el.className = names().filter(name => !less.includes(name)).join(' ') },
      contains(name) { return names().includes(name) },
    }
  }
  get dataset() {
    const el = this
    return new Proxy({}, {
      get: (_, key) => typeof key === 'string' ? el.getAttribute(dataAttribute(key)) ?? undefined : undefined,
      set: (_, key, value) => { el.setAttribute(dataAttribute(key), value); return true },
      deleteProperty: (_, key) => { el.removeAttribute(dataAttribute(key)); return true },
    })
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1) }
  get parentElement() { return this.parentNode?.nodeType === 1 ? this.parentNode : null }
  appendChild(node) {
    if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter(child => child !== node)
    node.parentNode = this; this.childNodes.push(node); return node
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node?.isFragment) [...node.childNodes].forEach(child => this.appendChild(child))
      else this.appendChild(typeof node === 'string' ? new TextNode(node) : node)
    }
  }
  replaceChildren(...nodes) { this.childNodes.forEach(node => { node.parentNode = null }); this.childNodes = []; this.append(...nodes) }
  replaceWith(node) {
    const parent = this.parentNode
    parent.childNodes[parent.childNodes.indexOf(this)] = node
    node.parentNode = parent; this.parentNode = null
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join('') }
  set textContent(value) { this.replaceChildren(); if (String(value)) this.appendChild(new TextNode(value)) }
  get innerHTML() { return this.childNodes.map(node => node.outerHTML).join('') }
  set innerHTML(html) { this.replaceChildren(); parseInto(String(html), this) }
  get outerHTML() {
    const tag = this.tagName.toLowerCase()
    const attrs = this.attrs.map(([name, value]) => ` ${name}="${value.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`).join('')
    if (VOID_TAGS.has(this.tagName)) return `<${tag}${attrs}>`
    return `<${tag}${attrs}>${RAW_TAGS.has(this.tagName) ? this.textContent : this.innerHTML}</${tag}>`
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener) }
  dispatch(type, init = {}) {
    const event = { preventDefault() { this.defaultPrevented = true }, ...init, type, target: this }
    for (const listener of this.listeners[type] || []) listener(event)
    this['on' + type]?.(event)
    return event
  }
  focus() { this.ownerDocument.activeElement = this }
  setSelectionRange(start, end) { this.selection = [start, end] }
  dispatchEvent(event) { (this.events ||= []).push(event.type + (event.bubbles ? ' (bubbles)' : '')); return true }
  querySelectorAll(selector) { return descendants(this).filter(el => matches(el, selector)) }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
}
const descendants = el => el.children.flatMap(child => [child, ...descendants(child)])
function matchesCompound(el, compound) {
  for (const [, tag, id, cls, attr, value] of compound.matchAll(/^([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (tag && el.tagName !== tag.toUpperCase()) return false
    if (id && el.id !== id) return false
    if (cls && !el.className.split(/\s+/).includes(cls)) return false
    if (attr && (value === undefined ? !el.hasAttribute(attr) : el.getAttribute(attr) !== value)) return false
  }
  return true
}
// Compound selectors, descendant combinators and selector lists.
function matches(el, selector) {
  return selector.split(',').some(part => {
    const steps = part.trim().split(/\s+/)
    if (!matchesCompound(el, steps.at(-1))) return false
    let node = el.parentNode
    for (const step of steps.slice(0, -1).reverse()) {
      while (node?.nodeType === 1 && !matchesCompound(node, step)) node = node.parentNode
      if (node?.nodeType !== 1) return false
      node = node.parentNode
    }
    return true
  })
}
function parseInto(html, root) {
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>|([^<]+|<)/gi
  let current = root, match
  while ((match = token.exec(html))) {
    const [, close, open, attrs, selfClosing, text] = match
    if (close) {
      let node = current
      while (node !== root && node.tagName !== close.toUpperCase()) node = node.parentNode
      if (node !== root) current = node.parentNode
    } else if (open) {
      const el = current.appendChild(new Elem(open, root.ownerDocument))
      for (const [, name, double, single, bare] of attrs.matchAll(/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
        el.setAttribute(name, decode(double ?? single ?? bare ?? ''))
      }
      if (RAW_TAGS.has(el.tagName)) {
        const end = html.toLowerCase().indexOf('</' + open.toLowerCase(), token.lastIndex)
        const stop = end < 0 ? html.length : end
        if (stop > token.lastIndex) el.appendChild(new TextNode(html.slice(token.lastIndex, stop)))
        token.lastIndex = stop
      } else if (!selfClosing && !VOID_TAGS.has(el.tagName)) current = el
    } else if (text !== undefined) current.appendChild(new TextNode(decode(text)))
  }
}
function createDocument(markup) {
  const document = { activeElement: null, title: '' }
  document.documentElement = new Elem('html', document)
  parseInto(markup, document.documentElement)
  document.body = document.documentElement.querySelector('body') || document.documentElement.appendChild(new Elem('body', document))
  document.getElementById = id => document.documentElement.querySelector('#' + id)
  document.querySelectorAll = selector => document.documentElement.querySelectorAll(selector)
  document.querySelector = selector => document.documentElement.querySelector(selector)
  document.createTextNode = text => new TextNode(text)
  document.createElement = tag => {
    if (String(tag).toLowerCase() !== 'template') return new Elem(tag, document)
    const template = new Elem('template', document)
    template.content = Object.assign(new Elem('#fragment', document), { isFragment: true })
    Object.defineProperty(template, 'innerHTML', {
      get: () => template.content.innerHTML,
      set: html => { template.content.replaceChildren(); parseInto(String(html), template.content) },
    })
    return template
  }
  return document
}

// ── the console ──
// The shortest text from `start` that compiles and ends with `close`: one whole
// declaration or statement (vm parses the strings, regexes and templates).
function sourceFrom(start, close) {
  assert.ok(start >= 0, 'missing declaration')
  for (let end = script.indexOf(close, start); end >= 0; end = script.indexOf(close, end + 1)) {
    const source = script.slice(start, end + 1)
    try { new vm.Script(source); return source } catch { /* not complete yet */ }
  }
  throw new Error('unterminated source at ' + start)
}
const fn = name => sourceFrom(script.search(new RegExp(`(?:async )?function ${name}\\(`)), '}')
const constant = name => sourceFrom(script.indexOf(`const ${name}=`), ';')
const switchWiring = () => sourceFrom(script.indexOf(`document.querySelectorAll('#proof-doc-switch [role="radio"]')`), ';')

const CONSOLE_FUNCTIONS = [
  'escapeHtml', 'stashMath', 'stashInlineMath', 'inlineMathEnd', 'mathNextIndex', 'mathBetween', 'mathSpans', 'holdMath', 'safeMarkdownHref', 'sanitizeRenderedHtml', 'mdToHtml', 'unwrapProofMarkdownFence',
  'normalizeProofMarkdown', 'splitProofs', 'problemItems', 'problemCardHtml', 'liveSandboxText', 'isAnalysisRun',
  'isReadabilityRun', 'syncSandboxEditor', 'beginSandboxEdit', 'saveSandboxProof', 'finishSandboxEdit', 'applySandboxPreview', 'runWriteup',
  'writeupBody', 'proofDocShown', 'showProofDocFile', 'syncProofDocSwitch', 'chooseProofDoc', 'writeupBannerHtml', 'closureCitedLemmas',
  'renderMarkdownIntoView', 'showProofMd', 'refreshProofPreview',
  // Make readable, and the Readability policy it follows.
  'makeReadableSource', 'syncMakeReadable', 'makeReadable', 'dagIsCgmEngine', 'readabilityEngineEntry', 'readabilityPolicy',
  'readabilityOffered', 'viewerIsAdmin', 'viewerIsGuest', 'readabilitySponsoredOnly',
]
// The proof toolbar and the Sandbox body (with its proof.md editor) exactly as the page has them.
const TOOLBAR = consoleHtml.slice(consoleHtml.indexOf('<div class="proof-toolbar">'), consoleHtml.indexOf('<div class="prompt-strip'))
const SANDBOX_BODY = consoleHtml.slice(consoleHtml.indexOf('<div class="proof-body" id="proof-body">'), consoleHtml.indexOf('<div class="cite-strip'))

function consolePage() {
  const document = createDocument(TOOLBAR + SANDBOX_BODY + '<textarea id="problem-text"></textarea>')
  const calls = { math: 0, coverage: 0, downloads: [], analysis: 0, fetches: [], saves: [], status: [], left: [] }
  // proof.md on the server; its revision is "rev-<mtime>".
  const files = { proof: CLOSURE, mtime: 1 }
  const context = vm.createContext({ console, URL, document, calls, files, setTimeout, clearTimeout })
  context.window = context
  context.location = { href: 'https://ansatze.test/console' }
  vm.runInContext(markedJs, context)
  vm.runInContext([
    'const $=id=>document.getElementById(id);',
    "let runId='r1', lastSandboxRun=null, sandboxEditing=false, sandboxDirty=false, sandboxConflict=false, sandboxSaving=false, sandboxSaveAgain=false, sandboxSaveTimer=null;",
    "let sandboxText='', sandboxRevision='missing', sandboxServerRevision='', lastProofMtime=0;",
    'function renderMathIn(){ calls.math++; }',
    'function applyFormalCoverage(){ calls.coverage++; }',
    'function setDownloadUrl(url,name){ calls.downloads.push([url,name]); }',
    'function applyAnalysisPreview(){ calls.analysis++; return true; }',
    'function editorStatus(message){ calls.status.push(message); }',
    'function setProofEmpty(){} function renderCitations(){} function resizeSandboxEditor(){} function showProofPdf(){}',
    // No Readability on this page until a test lists it; leaving the run is recorded.
    "let ENGINES=[], CURRENT_USER={id:1}, GUEST_POLICY=null, jobId='j1';",
    "function stopPolling(){ calls.left.push('stop polling'); } function goLanding(){ calls.left.push('composer'); } function chooseMode(mode){ calls.left.push('mode '+mode); }",
    'class Event{ constructor(type,init={}){ this.type=type; this.bubbles=!!init.bubbles; } }',
    `async function fetch(url,init){
      calls.fetches.push(url);
      if(init?.method==='POST'){
        // As console_server._save_sandbox_proof: an edit of an older proof.md is a conflict unless forced.
        const body=JSON.parse(init.body), current='rev-'+files.mtime; calls.saves.push(body);
        if(body.expected_revision&&body.expected_revision!==current&&!body.force)return {ok:false,status:409,json:async()=>({error:'proof.md changed since editing began',conflict:true,revision:current,content:files.proof})};
        files.proof=body.content; files.mtime++;
        return {ok:true,status:200,json:async()=>({ok:true,path:'proof.md',revision:'rev-'+files.mtime,mtime:files.mtime,size:files.proof.length})};
      }
      if(/\\/files$/.test(url))return {ok:true,json:async()=>({files:[{path:'proof.md',mtime:files.mtime,size:files.proof.length}]})};
      if(url.includes('path=proof.md'))return {ok:true,text:async()=>JSON.stringify({path:'proof.md',content:files.proof,revision:'rev-'+files.mtime})};
      throw new Error('unexpected fetch '+url);
    }`,
    ...['SAFE_MARKDOWN_TAGS', 'WRITEUP_BANNER', 'WRITEUP_STALE_NOTE', 'proofDocChoice'].map(constant),
    ...CONSOLE_FUNCTIONS.map(fn),
    switchWiring(),
    sourceFrom(script.indexOf("$('make-readable-btn').onclick="), ';'),
  ].join('\n'), context)
  const $ = id => document.getElementById(id)
  const radio = doc => document.querySelector(`#proof-doc-switch [data-proof-doc="${doc}"]`)
  const state = () => ({
    switchHidden: $('proof-doc-switch').hidden,
    checked: ['writeup', 'facts'].filter(doc => radio(doc).getAttribute('aria-checked') === 'true'),
    tabbable: ['writeup', 'facts'].filter(doc => radio(doc).tabIndex === 0),
    radioText: radio('writeup').textContent,
    chip: $('writeup-chip').getAttribute('data-status'),
    label: $('proof-label').textContent,
    download: calls.downloads.at(-1)?.[1],
    editHidden: $('proof-edit-btn').hidden,
    banner: $('md-view').querySelector('.writeup-banner')?.textContent ?? null,
    bannerStatus: $('md-view').querySelector('.writeup-banner')?.getAttribute('data-status') ?? null,
    doc: $('md-view').querySelector('.md-doc')?.textContent ?? '',
  })
  return { context, document, calls, files, $, radio, state, run: code => vm.runInContext(code, context) }
}
// A run record as /api/run returns it; `proof` replaces fields of its proof.md preview.
const cgmRun = (writeup, extra = {}, proof = {}) => ({
  run_id: 'r1', status: 'finished', engine: 'kimi', problem_text: 'Prove that for every natural number n, 1 + 3 + ... + (2n-1) = n^2.',
  proof_coverage: { source_revision: 'lean-1', items: [] },
  sandbox_preview: { text: CLOSURE, source: 'proof.md', has_proof_md: true, has_proof_tex: false, revision: 'rev-1', mtime: 1, ...proof, ...(writeup === undefined ? {} : { writeup }) },
  ...extra,
})
const writeupBlock = (status, fresh = true, text = WRITEUP) => ({
  text, status, fresh, path: 'writeup.md', revision: 'w-1', mtime: 2,
  check: { verdict: status === 'accepted' ? 'correct' : 'wrong', summary: '', issues: [] }, model: 'kimi-k3', provider: 'kimi',
})

test('the written proof opens first only when accepted for the current proof.md', () => {
  const cases = {
    acceptedFresh: writeupBlock('accepted'),
    acceptedStale: writeupBlock('accepted', false),
    rejected: writeupBlock('rejected'),
    unchecked: writeupBlock('unchecked'),
    // Only the exact status counts: nothing else reads as accepted.
    shouted: writeupBlock('ACCEPTED'),
    prototype: writeupBlock('__proto__'),
    freshAsText: { ...writeupBlock('accepted'), fresh: 'true' },
    blank: writeupBlock('accepted', true, '  \n'),
    notText: { ...writeupBlock('accepted'), text: { toString: () => WRITEUP } },
    none: undefined,
  }
  const out = {}
  for (const [name, writeup] of Object.entries(cases)) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeup))
    out[name] = page.state()
  }
  assert.deepEqual(
    { ...out.acceptedFresh, doc: undefined },
    { switchHidden: false, checked: ['writeup'], tabbable: ['writeup'], radioText: 'Written proof ✓', chip: 'accepted', label: 'writeup.md',
      download: 'writeup.md', editHidden: true, banner: BANNER.accepted, bannerStatus: 'accepted', doc: undefined })
  assert.match(out.acceptedFresh.doc, /Theorem\. For every natural number/)
  for (const name of ['acceptedStale', 'rejected', 'unchecked', 'shouted', 'prototype', 'freshAsText']) {
    assert.equal(out[name].switchHidden, false, name)
    assert.deepEqual(out[name].checked, ['facts'], name)
    assert.equal(out[name].label, 'proof.md', name)
    assert.equal(out[name].download, 'proof.md', name)
    assert.equal(out[name].editHidden, false, name)
    assert.equal(out[name].banner, null, name)
    assert.match(out[name].doc, /Lemma 3 · fact a8021b90635eb4e2/, name)
  }
  assert.equal(out.rejected.radioText, 'Written proof ⚠')
  assert.equal(out.rejected.chip, 'rejected')
  assert.equal(out.unchecked.radioText, 'Written proof')
  assert.equal(out.shouted.chip, 'unchecked')
  assert.equal(out.prototype.radioText, 'Written proof')
  for (const name of ['blank', 'notText', 'none']) {
    assert.equal(out[name].switchHidden, true, name)
    assert.deepEqual(out[name].checked, ['facts'], name)
    assert.equal(out[name].label, 'proof.md', name)
    assert.equal(out[name].editHidden, false, name)
  }
})

test("a reader's choice survives polls and proof.md refreshes, per run", async () => {
  const page = consolePage(), { context, state, calls } = page
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  assert.equal(state().label, 'writeup.md')
  // The Sandbox keeps proof.md as the text Edit would change.
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())

  context.chooseProofDoc('facts')
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.editHidden, now.banner], [['facts'], 'proof.md', 'proof.md', false, null])
  assert.match(now.doc, /Lemma 3 · fact a8021b90635eb4e2/)
  // A later poll of the same run, then a forced refresh that reads proof.md again.
  context.applySandboxPreview({ ...cgmRun(writeupBlock('accepted')), updated_at: 'later' })
  assert.deepEqual(state().checked, ['facts'])
  assert.equal(await context.refreshProofPreview(true, run), true)
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])

  context.chooseProofDoc('writeup')
  await context.refreshProofPreview(true, run)
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.editHidden, now.banner], [['writeup'], 'writeup.md', 'writeup.md', true, BANNER.accepted])
  assert.ok(calls.fetches.some(url => url.includes('path=proof.md')), 'the refresh still reads proof.md')
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())
  assert.deepEqual([...calls.downloads.at(-1)], ['/api/workspace/r1/file?path=writeup.md&download=1', 'writeup.md'])

  // Another run starts from its own default; the first keeps its choice.
  page.run("runId='r2'")
  context.applySandboxPreview({ ...cgmRun(writeupBlock('accepted')), run_id: 'r2' })
  assert.deepEqual(state().checked, ['writeup'])
  context.chooseProofDoc('facts')
  page.run("runId='r1'")
  context.applySandboxPreview(run)
  assert.deepEqual(state().checked, ['writeup'])
})

test('the banner states each status above the write-up, and a stale write-up says so', () => {
  const out = {}
  for (const [name, status, fresh] of [['accepted', 'accepted', true], ['rejected', 'rejected', true], ['unchecked', 'unchecked', true], ['stale', 'accepted', false], ['staleRejected', 'rejected', false]]) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeupBlock(status, fresh)))
    page.context.chooseProofDoc('writeup')
    const view = page.$('md-view')
    const order = view.querySelectorAll('.problem-message, .writeup-banner, .md-doc').map(el => el.className)
    const paragraphs = view.querySelector('.writeup-banner').querySelectorAll('p').map(p => p.textContent)
    out[name] = { order, paragraphs, status: view.querySelector('.writeup-banner').getAttribute('data-status'), role: view.querySelector('.writeup-banner').getAttribute('role'), title: page.radio('writeup').title }
  }
  for (const name of ['accepted', 'rejected', 'unchecked', 'stale']) {
    // Problem first, then the banner, then the write-up: never inside the Markdown.
    assert.deepEqual(out[name].order, ['problem-message', 'writeup-banner', 'md-doc'], name)
    assert.equal(out[name].role, 'note')
  }
  assert.deepEqual(out.accepted.paragraphs, [BANNER.accepted])
  assert.deepEqual(out.rejected.paragraphs, [BANNER.rejected])
  assert.deepEqual(out.unchecked.paragraphs, [BANNER.unchecked])
  assert.deepEqual(out.stale.paragraphs, [BANNER.accepted, STALE])
  assert.deepEqual(out.staleRejected.paragraphs, [BANNER.rejected, STALE])
  assert.equal(out.rejected.status, 'rejected')
  assert.equal(out.accepted.title, BANNER.accepted)
  assert.equal(out.stale.title, BANNER.accepted + ' ' + STALE)
})

test('the write-up is rendered through the sanitizer, gets no Lean marks, and is redrawn only when it changes', () => {
  const hostile = [
    '**Theorem.** $a<b$ for all $a$.', '',
    '<script>window.pwned=1</script>', '',
    '<img src=x onerror="window.pwned=2">', '',
    '[a link](javascript:window.pwned=3) and <a href="javascript:window.pwned=4" onclick="window.pwned=5">another</a>', '',
    '<iframe src="https://example.invalid/"></iframe><b style="color:red" onmouseover="x()">bold</b>',
  ].join('\n')
  const page = consolePage(), { context, calls, $ } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', true, hostile)))
  const html = $('md-view').innerHTML
  for (const bad of ['<script', '<img', '<iframe', '<b ', 'onerror', 'onclick', 'onmouseover', 'style=', 'javascript:']) {
    assert.ok(!html.includes(bad), `${bad} reached the view: ${html}`)
  }
  assert.ok(html.includes('$a&lt;b$'), 'math stays text for KaTeX')
  assert.equal(context.pwned, undefined)
  assert.equal(calls.math, 1)
  assert.equal(calls.coverage, 0, 'no Lean coverage marks on the write-up')

  // The same write-up again is not redrawn; the facts and back are.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', true, hostile)))
  assert.equal(calls.math, 1)
  context.chooseProofDoc('facts')
  assert.deepEqual([calls.math, calls.coverage], [2, 1])
  context.chooseProofDoc('writeup')
  assert.deepEqual([calls.math, calls.coverage], [3, 1])
  // A new status or text redraws it.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', false, hostile)))
  assert.equal(calls.math, 4)
  assert.equal($('md-view').querySelectorAll('.writeup-banner p').length, 2)
})

test('the switch waits while proof.md is being edited, and Edit returns with the facts', async () => {
  const page = consolePage(), { context, state, radio } = page
  context.applySandboxPreview(cgmRun(writeupBlock('rejected')))
  assert.equal(state().editHidden, false)
  context.beginSandboxEdit()
  assert.deepEqual([radio('writeup').disabled, radio('facts').disabled], [true, true])
  context.chooseProofDoc('writeup')
  assert.deepEqual(state().checked, ['facts'])
  await context.finishSandboxEdit()
  assert.deepEqual([radio('writeup').disabled, radio('facts').disabled], [false, false])
  context.chooseProofDoc('writeup')
  assert.deepEqual([state().checked, state().editHidden], [['writeup'], true])
  context.chooseProofDoc('facts')
  assert.equal(state().editHidden, false)
})

test('an edit saved on a finished run survives the switch, and the write-up then reads as stale', async () => {
  const page = consolePage(), { context, state, files, calls, $, radio } = page
  // The last record of a finished run: it is no longer polled, so it keeps the proof.md before the edit.
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  await context.refreshProofPreview(true, run)
  context.chooseProofDoc('facts')
  context.beginSandboxEdit()
  const editor = $('notion-editor')
  editor.value = files.proof.replace('# Proof', '# Proof\n\nFirst edit.')
  page.run('sandboxDirty=true')
  await context.finishSandboxEdit()
  assert.deepEqual(calls.saves.map(save => save.expected_revision), ['rev-1'])
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])
  assert.match(now.doc, /First edit\./)
  // The record still calls the write-up fresh, but it was written for the proof.md this edit replaced.
  assert.equal(radio('writeup').title, BANNER.accepted + ' ' + STALE)

  context.chooseProofDoc('writeup')
  now = state()
  assert.deepEqual($('md-view').querySelectorAll('.writeup-banner p').map(p => p.textContent), [BANNER.accepted, STALE])
  assert.deepEqual([now.label, now.download], ['writeup.md', 'writeup.md'])
  // Back on the facts, the Sandbox still holds the saved edit and its revision.
  context.chooseProofDoc('facts')
  assert.match(state().doc, /First edit\./)
  assert.deepEqual([page.run('sandboxRevision'), page.run('sandboxText')], ['rev-2', files.proof])

  // So the next edit starts from it and saves without a conflict.
  context.beginSandboxEdit()
  assert.match(editor.value, /First edit\./)
  editor.value += '\nSecond edit.\n'
  page.run('sandboxDirty=true')
  assert.equal(await context.saveSandboxProof(false), true)
  assert.equal(page.run('sandboxConflict'), false)
  assert.deepEqual(calls.saves.map(save => [save.expected_revision, save.force]), [['rev-1', false], ['rev-2', false]])
  assert.match(files.proof, /First edit\.[\s\S]*Second edit\./)
  assert.ok(!calls.status.includes('New engine version — review or overwrite'), calls.status.join(' | '))
})

test('a write-up that lands while proof.md is in the editor waits behind the facts', async () => {
  const page = consolePage(), { context, state, $, radio } = page
  // The write-up round runs after the run published proof.md, so Edit is offered meanwhile.
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }))
  context.beginSandboxEdit()
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), { status: 'running' }))
  let now = state()
  assert.deepEqual([now.switchHidden, now.checked, now.chip, now.label, now.download], [false, ['facts'], 'accepted', 'proof.md · live', 'proof.md'])
  assert.deepEqual([radio('writeup').disabled, $('notion-editor-wrap').hidden], [true, false])
  // Done returns to the proof.md that was open in the editor.
  await context.finishSandboxEdit()
  now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner, radio('writeup').disabled], [['facts'], 'proof.md · live', 'proof.md', null, false])
  assert.match(now.doc, /Lemma 3 · fact a8021b90635eb4e2/)
  // Nothing was saved, so the write-up is still fresh for this proof.md.
  context.chooseProofDoc('writeup')
  assert.equal(state().banner, BANNER.accepted)
})

test('a proof.md newer than the run record leaves the write-up stale until a record judges that proof.md', async () => {
  const page = consolePage(), { context, state, files, radio } = page
  const run = cgmRun(writeupBlock('accepted'))
  context.applySandboxPreview(run)
  assert.equal(state().label, 'writeup.md')
  // Saved from another tab after this record was made: the refresh reads it.
  files.proof = CLOSURE.replace('# Proof', '# Proof\n\nEdited elsewhere.')
  files.mtime = 2
  await context.refreshProofPreview(true, run)
  let now = state()
  assert.deepEqual([now.checked, now.label, now.download, now.banner], [['facts'], 'proof.md', 'proof.md', null])
  assert.match(now.doc, /Edited elsewhere\./)
  assert.equal(radio('writeup').title, BANNER.accepted + ' ' + STALE)
  // The server judges each proof.md it sends: stale for this one...
  context.applySandboxPreview(cgmRun(writeupBlock('accepted', false), {}, { text: files.proof, revision: 'rev-2', mtime: 2 }))
  assert.deepEqual([state().checked, radio('writeup').title], [['facts'], BANNER.accepted + ' ' + STALE])
  // ...and fresh again for one the write-up was made for.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), {}, { revision: 'rev-3', mtime: 3 }))
  now = state()
  assert.deepEqual([now.checked, now.label, now.banner, radio('writeup').title], [['writeup'], 'writeup.md', BANNER.accepted, BANNER.accepted])
})

test('a document newly on screen opens at its top, and a redraw of the same one keeps the place', () => {
  const page = consolePage(), { context, state, $, radio } = page
  const body = $('proof-body')
  const longer = { text: CLOSURE + '\nOne more line.\n', revision: 'rev-2', mtime: 2 }
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }))
  body.scrollTop = 500
  context.applySandboxPreview(cgmRun(undefined, { status: 'running' }, longer))
  assert.equal(body.scrollTop, 500)
  // The accepted write-up lands and takes the facts' place: it opens under its banner.
  context.applySandboxPreview(cgmRun(writeupBlock('accepted'), { status: 'running' }, longer))
  assert.deepEqual([state().checked, state().banner, body.scrollTop], [['writeup'], BANNER.accepted, 0])
  body.scrollTop = 300
  radio('writeup').dispatch('click')
  assert.equal(body.scrollTop, 300, 'choosing the document already shown moves nothing')
  radio('facts').dispatch('click')
  assert.deepEqual([state().checked, body.scrollTop], [['facts'], 0])
})

test('the radios work with a click, the arrow keys, Home and End', () => {
  const page = consolePage(), { context, document, state, radio } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  const key = (doc, name) => radio(doc).dispatch('keydown', { key: name })
  assert.equal(key('writeup', 'ArrowRight').defaultPrevented, true)
  assert.deepEqual(state().checked, ['facts'])
  assert.equal(document.activeElement, radio('facts'))
  key('facts', 'ArrowLeft')
  assert.deepEqual(state().checked, ['writeup'])
  assert.equal(document.activeElement, radio('writeup'))
  key('writeup', 'End')
  assert.deepEqual(state().checked, ['facts'])
  key('facts', 'Home')
  assert.deepEqual(state().checked, ['writeup'])
  assert.equal(key('writeup', 'Enter').defaultPrevented, undefined)
  radio('facts').dispatch('click')
  assert.deepEqual(state().checked, ['facts'])
})

test('a closure rendered before v2 names the lemma behind each cited label, on screen only', () => {
  const page = consolePage(), { context } = page
  const shared = { window: {}, document: {}, NodeFilter: {}, location: { href: 'https://ansatze.test/' } }
  vm.runInNewContext(viewerTextJs, shared)
  const views = { console: context.closureCitedLemmas, shared: shared.window.AnsatzSharedText.closureCitedLemmas }
  const v2 = CLOSURE.replace(/^## Lemma (\d) · /gm, '## Lemma L$1 · ')
  const notClosure = '# Proof\n\nBy L1.\n\n## Lemma 1 · fact c868b47bd56b2884\n\nCited facts: L1 = fact c868b47bd56b2884.\n'
  for (const [where, rewrite] of Object.entries(views)) {
    const shown = rewrite(CLOSURE)
    assert.ok(shown.includes('Cited facts: L1 = Lemma 2 (fact c868b47bd56b2884); L2 = Lemma 1 (fact 72f15db5ead2748a).'), where)
    assert.ok(shown.includes('Cited facts: L3 = Lemma 3 (fact a8021b90635eb4e2).'), where)
    // Everything else is unchanged.
    assert.equal(shown.replace(/ = Lemma \d+ \(fact ([0-9a-f]{16})\)/g, ' = fact $1'), CLOSURE, where)
    assert.equal(rewrite(v2), v2, where)
    assert.equal(rewrite(notClosure), notClosure, where)
    // An unknown id or an item of another shape is left as it is.
    const odd = CLOSURE.replace('L3 = fact a8021b90635eb4e2', 'L3 = fact 0000000000000000; L4 = see above')
    assert.ok(rewrite(odd).includes('Cited facts: L3 = fact 0000000000000000; L4 = see above.'), where)
  }
  // The Sandbox shows the names; proof.md itself is untouched.
  context.applySandboxPreview(cgmRun(undefined))
  assert.match(page.$('md-view').textContent, /Cited facts: L1 = Lemma 2 \(fact c868b47bd56b2884\); L2 = Lemma 1 \(fact 72f15db5ead2748a\)\./)
  assert.equal(page.run('sandboxText').trim(), CLOSURE.trim())
})

// ── the write-up's own header ──
// writeup.md of a live run (Codex, n^2+n even), byte for byte, and the other
// two headers as cgm/writeup.render_writeup writes them (a body may quote).
const LIVE_WRITEUP = [
  '# Written proof', '',
  '> Written from the verifier-accepted facts (proof.md) and then checked as a whole by an independent, response-only model verifier that saw only the problem and this text. Model-verifier acceptance is not a Lean or other formal certificate.', '',
  '**Theorem.** For every natural number $n$, the integer $n^2+n$ is even.', '',
  '**Conventions.** Let $\\mathbb N=\\{0,1,2,\\ldots\\}$. An integer $m$ is even if $m=2k$ for some integer $k$.', '',
  '**Proof.** We show that one of $n$ and $n+1$ is even, and then use the factorization $n^2+n=n(n+1)$.', '',
  'Let $n\\in\\mathbb N$ be arbitrary. The division-with-remainder theorem states that for every integer $x$ and every positive integer $d$, there exist unique integers $a$ and $r$ such that $x=da+r$ and $0\\le r<d$. Applying it with $x=n$ and $d=2$ gives',
  '$$', 'n=2a+r,\\qquad 0\\le r<2.', '$$',
  'Since $r$ is an integer, either $r=0$ or $r=1$. Also, $a\\ge0$: otherwise $a\\le-1$, and thus', '$$', 'n=2a+r\\le-2+1=-1,', '$$', 'contrary to $n\\ge0$.', '',
  'If $r=0$, set $q=a$. Then $n=2q$. The integer $k=q(n+1)$ satisfies', '$$', 'n^2+n=n(n+1)=(2q)(n+1)=2k.', '$$', '',
  'If $r=1$, set $q=a+1$. Then', '$$', 'n+1=2a+2=2q.', '$$', 'The integer $k=nq$ therefore satisfies', '$$', 'n^2+n=n(n+1)=n(2q)=2k.', '$$', '',
  'In either case, $n^2+n$ is even. Since $n$ was arbitrary, this holds for every $n\\in\\mathbb N$, and hence also under the convention that natural numbers begin at $1$. $\\blacksquare$', '',
].join('\n')
const QUOTING_BODY = '**Theorem.** For every natural number $n$, the integer $n^2+n$ is even.\n\n> A quoted remark inside the body stays.\n\n**Proof.** By cases. $\\blacksquare$'
const REJECTED_WRITEUP = '# Written proof\n\n> Written from the verifier-accepted facts (proof.md). An independent model check of this text found problems, listed at the end, so this write-up is NOT accepted; the verified facts remain the proof.\n\n'
  + QUOTING_BODY + '\n\n**Problems found by the check.**\n\n- Case 2: The step \\$n+1=2a+2\\$ uses \\$r=1\\$ but the case split never shows \\$r\\\\in\\\\{0,1\\\\}\\$.\n'
const UNCHECKED_WRITEUP = '# Written proof\n\n> Written from the verifier-accepted facts (proof.md). This text has not been checked; the verified facts remain the proof.\n\n' + QUOTING_BODY + '\n'

test('the write-up is shown without its own heading and status note: the banner says that already', () => {
  const { context } = consolePage()
  const body = context.writeupBody
  assert.ok(body(LIVE_WRITEUP).startsWith('**Theorem.** For every natural number $n$'))
  assert.equal(body(LIVE_WRITEUP), LIVE_WRITEUP.slice(LIVE_WRITEUP.indexOf('**Theorem.**')))
  for (const file of [REJECTED_WRITEUP, UNCHECKED_WRITEUP]) {
    const shown = body(file)
    // The body's own quotation and the check's findings stay.
    assert.ok(shown.startsWith(QUOTING_BODY), shown)
    assert.doesNotMatch(shown, /Written proof|Written from the verifier-accepted facts/)
  }
  assert.match(body(REJECTED_WRITEUP), /\*\*Problems found by the check\.\*\*\n\n- Case 2:/)
  // Only that header: another heading, no heading, or a heading without a note.
  for (const other of ['# Proof\n\n> Note.\n\nBody', '**Theorem.** x\n\n> Note.', '', ' # Written proof\n\n> Note.\n\nBody'])
    assert.equal(body(other), other)
  assert.equal(body('# Written proof\n\n**Theorem.** x\n\n> kept'), '**Theorem.** x\n\n> kept')
  assert.equal(body('# Written proof\r\n\r\n> Note.\r\n\r\n**Theorem.** x'), '**Theorem.** x')

  // In the Sandbox: the banner, then the proof itself (the download is the file as written).
  for (const [status, file, quotes] of [['accepted', LIVE_WRITEUP, 0], ['rejected', REJECTED_WRITEUP, 1], ['unchecked', UNCHECKED_WRITEUP, 1]]) {
    const page = consolePage()
    page.context.applySandboxPreview(cgmRun(writeupBlock(status, true, file)))
    page.context.chooseProofDoc('writeup')
    const doc = page.$('md-view').querySelector('.md-doc')
    assert.equal(doc.querySelectorAll('h1').length, 0, status)
    assert.equal(doc.querySelectorAll('blockquote').length, quotes, status)
    assert.match(doc.textContent, /^\s*Theorem\. For every natural number/, status)
    assert.deepEqual(page.$('md-view').querySelectorAll('.problem-message, .writeup-banner, .md-doc').map(el => el.className), ['problem-message', 'writeup-banner', 'md-doc'])
    assert.equal(page.state().download, 'writeup.md')
  }
})

test('the shared page shows the write-up without its own heading and status note', async () => {
  for (const [status, file, quotes] of [['accepted', LIVE_WRITEUP, 0], ['rejected', REJECTED_WRITEUP, 1]]) {
    const page = await sharedPage({ ...SNAPSHOT, writeup: file, writeup_status: status, writeup_fresh: true })
    page.radio('writeup').dispatch('click')
    const proof = page.$('rv-proof-text')
    assert.equal(proof.querySelectorAll('h1').length, 0, status)
    assert.equal(proof.querySelectorAll('blockquote').length, quotes, status)
    assert.match(proof.textContent, /^\s*Theorem\. For every natural number/, status)
    assert.doesNotMatch(proof.textContent, /Written from the verifier-accepted facts/)
    assert.equal(page.state().bannerHidden, false)
  }
  // The verified facts are shown as saved.
  const page = await sharedPage({ ...SNAPSHOT, writeup: LIVE_WRITEUP, writeup_status: 'accepted', writeup_fresh: true })
  page.radio('facts').dispatch('click')
  assert.equal(page.$('rv-proof-text').querySelectorAll('h1')[0].textContent, 'Proof')
})

// ── the shared page ──
async function sharedPage(snapshot) {
  const document = createDocument(viewerHtml.slice(viewerHtml.indexOf('<body')))
  const context = vm.createContext({ console, URL, document, setTimeout, clearTimeout, navigator: {}, NodeFilter: { SHOW_TEXT: 4 } })
  context.window = context
  context.location = { href: 'https://ansatze.test/shared/token' }
  context.fetch = async () => ({ ok: true, json: async () => JSON.parse(JSON.stringify(snapshot)) })
  vm.runInContext(markedJs, context)
  vm.runInContext(viewerTextJs, context)
  vm.runInContext(viewerJs, context)
  for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve))
  const $ = id => document.getElementById(id)
  const radio = doc => document.querySelector(`[data-proof-doc="${doc}"]`)
  const state = () => ({
    switchHidden: $('rv-proof-doc-switch').hidden,
    checked: ['writeup', 'facts'].filter(doc => radio(doc).getAttribute('aria-checked') === 'true'),
    radioText: radio('writeup').textContent,
    bannerHidden: $('rv-writeup-banner').hidden,
    banner: $('rv-writeup-banner').querySelectorAll('p').map(p => p.textContent),
    bannerStatus: $('rv-writeup-banner').getAttribute('data-status'),
    text: $('rv-proof-text').textContent,
    html: $('rv-proof-text').innerHTML,
  })
  return { document, context, $, radio, state }
}
const SNAPSHOT = { version: 2, title: 'Odd numbers', status: 'finished', problem: 'Prove that 1 + 3 + ... + (2n-1) = n^2.', proof: CLOSURE, agents: [], graphs: {} }

test('the shared page offers the same two documents, opening the written proof only when accepted for its proof.md', async () => {
  const page = await sharedPage({ ...SNAPSHOT, writeup: WRITEUP, writeup_status: 'accepted', writeup_fresh: true })
  let now = page.state()
  assert.deepEqual([now.switchHidden, now.checked, now.radioText, now.bannerHidden, now.banner, now.bannerStatus],
    [false, ['writeup'], 'Written proof ✓', false, [BANNER.accepted], 'accepted'])
  assert.match(now.text, /Theorem\. For every natural number/)
  assert.equal(page.radio('writeup').title, BANNER.accepted)

  page.radio('facts').dispatch('click')
  now = page.state()
  assert.deepEqual([now.checked, now.bannerHidden], [['facts'], true])
  // The saved closure, with the lemma behind each cited label named on screen.
  assert.match(now.text, /Cited facts: L1 = Lemma 2 \(fact c868b47bd56b2884\); L2 = Lemma 1 \(fact 72f15db5ead2748a\)\./)

  assert.equal(page.radio('facts').dispatch('keydown', { key: 'ArrowLeft' }).defaultPrevented, true)
  assert.deepEqual(page.state().checked, ['writeup'])
  assert.equal(page.document.activeElement, page.radio('writeup'))
  page.radio('writeup').dispatch('keydown', { key: 'End' })
  assert.deepEqual(page.state().checked, ['facts'])
})

test('on the shared page a rejected, unchecked or stale write-up waits behind the verified facts', async () => {
  const cases = {
    rejected: ['rejected', true, 'Written proof ⚠', [BANNER.rejected]],
    unchecked: ['unchecked', true, 'Written proof', [BANNER.unchecked]],
    stale: ['accepted', false, 'Written proof ✓', [BANNER.accepted, STALE]],
    // Older or odd snapshots: no freshness, or a status this page does not know.
    noFreshness: ['accepted', undefined, 'Written proof ✓', [BANNER.accepted, STALE]],
    unknown: ['correct', true, 'Written proof', [BANNER.unchecked]],
  }
  for (const [name, [status, fresh, radioText, banner]] of Object.entries(cases)) {
    const page = await sharedPage({ ...SNAPSHOT, writeup: WRITEUP, writeup_status: status, writeup_fresh: fresh })
    let now = page.state()
    assert.deepEqual([now.switchHidden, now.checked, now.radioText, now.bannerHidden], [false, ['facts'], radioText, true], name)
    assert.match(now.text, /Lemma 3 · fact a8021b90635eb4e2/, name)
    page.radio('writeup').dispatch('click')
    now = page.state()
    assert.deepEqual([now.checked, now.bannerHidden, now.banner], [['writeup'], false, banner], name)
  }
})

test('the shared page shows no switch without a write-up, never for an analysis, and sanitizes the write-up', async () => {
  for (const snapshot of [SNAPSHOT, { ...SNAPSHOT, writeup: '   ' }, { ...SNAPSHOT, writeup: { text: WRITEUP }, writeup_status: 'accepted', writeup_fresh: true },
    { ...SNAPSHOT, kind: 'analysis', writeup: WRITEUP, writeup_status: 'accepted', writeup_fresh: true }]) {
    const page = await sharedPage(snapshot)
    const now = page.state()
    assert.deepEqual([now.switchHidden, now.bannerHidden, now.checked], [true, true, ['facts']])
    page.radio('writeup').dispatch('click')
    assert.deepEqual([page.state().checked, page.state().bannerHidden], [['facts'], true])
  }
  const hostile = '**Theorem.** $a<b$.\n\n<script>window.pwned=1</script>\n\n<img src=x onerror="window.pwned=2">\n\n[x](javascript:window.pwned=3)'
  const page = await sharedPage({ ...SNAPSHOT, writeup: hostile, writeup_status: 'accepted', writeup_fresh: true })
  const { html } = page.state()
  for (const bad of ['<script', '<img', 'onerror', 'javascript:']) assert.ok(!html.includes(bad), `${bad} reached the page: ${html}`)
  assert.ok(html.includes('$a&lt;b$'))
  assert.equal(page.context.pwned, undefined)
})

// ── Make readable ──
// The Readability entry of /api/engines for a viewer, as the server sends it.
const readabilityEngine = ({ entry = {}, policy = {} } = {}) => ({
  id: 'readability', modes: ['readability'], available: true, ...entry,
  readability: { enabled: true, routes: ['sponsored_kimi', 'api_key'], sources: { paste_chars: 400000 }, sponsored: { available: true, remaining_today: 12 }, ...policy },
})
function readablePage({ engine = readabilityEngine(), user = { id: 4 }, guestPolicy = null } = {}) {
  const page = consolePage()
  page.context.ENGINE_FOR_TEST = [engine]
  page.run(`ENGINES=ENGINE_FOR_TEST; CURRENT_USER=${JSON.stringify(user)}; GUEST_POLICY=${JSON.stringify(guestPolicy)};`)
  page.button = () => page.$('make-readable-btn')
  page.note = () => page.$('make-readable-note').textContent
  return page
}

test('Make readable pastes the document on screen into Readability, as written, and starts nothing', () => {
  // The button sits in the proof toolbar, hidden until a finished CGM run shows its document.
  assert.match(TOOLBAR, /<span class="make-readable-note" id="make-readable-note" role="status" aria-live="polite"><\/span>\s*<button type="button" id="make-readable-btn" hidden title="[^"]+">¶ Make readable<\/button>/)
  let page = readablePage(), { context, $, calls } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  assert.equal(page.button().hidden, false)
  page.button().dispatch('click')
  // As New does: the run is left (no more polling) for the composer, now in Readability.
  assert.deepEqual(calls.left, ['stop polling', 'composer', 'mode readability'])
  assert.equal(page.run('runId'), null)
  const input = $('problem-text')
  // The written proof as the file has it (its header says how it was checked).
  assert.equal(input.value, WRITEUP)
  assert.deepEqual(input.events, ['input (bubbles)'])
  assert.deepEqual(input.selection, [0, 0])
  assert.equal(page.document.activeElement, input)

  // With the verified facts on screen, proof.md.
  page = readablePage(); ({ context, $, calls } = page)
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  context.chooseProofDoc('facts')
  page.button().dispatch('click')
  assert.equal($('problem-text').value, CLOSURE.trim())
  // A run without a write-up offers its proof.md.
  page = readablePage(); ({ context, $ } = page)
  context.applySandboxPreview(cgmRun(undefined))
  assert.equal(page.button().hidden, false)
  page.button().dispatch('click')
  assert.equal($('problem-text').value, CLOSURE.trim())
})

test('Make readable says when the document is longer than the viewer may paste, and pastes nothing', () => {
  const page = readablePage({ engine: readabilityEngine({ policy: { sources: { paste_chars: 100 } } }), user: { id: 0, guest: true } })
  const { context, $, calls } = page
  context.applySandboxPreview(cgmRun(writeupBlock('accepted')))
  page.button().dispatch('click')
  assert.equal(page.note(), `The written proof has ${WRITEUP.length.toLocaleString('en-US')} characters: too long to paste into Readability (at most 100 for guests).`)
  assert.deepEqual([calls.left, page.run('runId'), $('problem-text').value ?? ''], [[], 'r1', ''])
  // The note is about that document: it goes when the other one is shown.
  context.chooseProofDoc('facts')
  assert.equal(page.note(), '')
  page.button().dispatch('click')
  assert.match(page.note(), /^The proof\.md has [\d,]+ characters: too long to paste into Readability \(at most 100 for guests\)\.$/)
})

test('Make readable is offered only for a finished CGM run, as the Readability policy allows the viewer', () => {
  const shown = (setup, record = cgmRun(writeupBlock('accepted'))) => {
    const page = readablePage(setup)
    page.context.applySandboxPreview(record)
    return !page.button().hidden
  }
  assert.equal(shown({}), true)
  // Not while it runs, not for another harness, not without proof.md.
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { status: 'running' })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { status: 'failed' })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { engine: 'plain' })), false)
  assert.equal(shown({}, cgmRun(undefined, {}, { has_proof_md: false })), false)
  assert.equal(shown({}, cgmRun(writeupBlock('accepted'), { engine: 'math_harness' })), true)
  // Only where Readability would start: offered, set up, open to this viewer.
  assert.equal(shown({ engine: readabilityEngine({ entry: { requires_login: true } }) }), false)
  assert.equal(shown({ engine: readabilityEngine({ policy: { enabled: false, reason: 'off' } }) }), false)
  assert.equal(shown({ engine: readabilityEngine({ entry: { available: false } }), user: { id: 1, is_admin: true } }), false)
  assert.equal(shown({ engine: { ...readabilityEngine(), readability: null } }), false)
  // A guest with runs left; a viewer on sponsored Kimi K3 only while today's sponsored runs allow one.
  assert.equal(shown({ user: { id: 0, guest: true }, guestPolicy: { runs_remaining: 2 } }), true)
  assert.equal(shown({ user: { id: 0, guest: true }, guestPolicy: { runs_remaining: 0 } }), false)
  assert.equal(shown({ user: { id: 0, guest: true }, engine: readabilityEngine({ policy: { sponsored: { available: true, remaining_today: 0 } } }) }), false)
  assert.equal(shown({ engine: readabilityEngine({ policy: { routes: ['sponsored_kimi'], sponsored: { available: false } } }) }), false)
  // A member with their own Kimi key is not held to the sponsored count.
  assert.equal(shown({ engine: readabilityEngine({ policy: { sponsored: { available: true, remaining_today: 0 } } }) }), true)
  // Analyses and readable versions have no proof document to paste.
  const page = readablePage()
  for (const engine of ['analyze', 'readability']) assert.equal(page.context.makeReadableSource({ ...cgmRun(writeupBlock('accepted')), engine }), null)
  // Not while proof.md is open in the editor.
  page.context.applySandboxPreview(cgmRun(writeupBlock('rejected')))
  assert.equal(page.button().hidden, false)
  page.context.beginSandboxEdit()
  assert.equal(page.button().hidden, true)
})
