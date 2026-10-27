// The console's attachment composers follow the engine the files are for: a
// tool-free engine (Ansatz CGM on Kimi or any route, Plain) takes text and PDF
// files only and says how it reads them; tool-using engines keep images; the
// chat composer follows the open run's engine. The real console functions run
// in a vm context with a small DOM stub.
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
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))

const CONSTS = ['ATTACHMENT_EXTENSIONS', 'TOOL_FREE_ENGINES', 'ATTACHMENT_IMAGE_EXTENSIONS']
const FUNCTIONS = ['attachmentSize', 'attachmentExtension', 'attachmentLimits', 'attachmentRefusal', 'chatRunEngine', 'attachmentComposer']
// A DOM just large enough for attachmentComposer.
const DOM = `
function element(tag){
  const node={tag,children:[],className:'',textContent:'',title:'',accept:'',disabled:false,listeners:{},
    classList:{add(){},remove(){}},
    append(...c){ this.children.push(...c); }, replaceChildren(...c){ this.children=[...c]; },
    setAttribute(k,v){ this[k]=v; }, addEventListener(type,f){ (this.listeners[type]||(this.listeners[type]=[])).push(f); },
    contains(){ return false; }, click(){} };
  return node;
}
const document={createElement:element};
const URL={createObjectURL:()=>'blob:preview', revokeObjectURL(){ revoked++; }};
let revoked=0;
const nodes={};
function composerRoot(id){
  const root=element('div'), parts={input:element('input'),button:element('button'),'.attachment-list':element('div'),'.attachment-error':element('div'),'.attachment-help':element('span')};
  root.querySelector=selector=>parts[selector]; root.parentElement=element('form'); root.parts=parts;
  nodes[id]=root; return root;
}
const $=id=>nodes[id]||(id.endsWith('-attachments')?composerRoot(id):(nodes[id]=element('textarea')));
let CURRENT_USER={id:7,guest:false}, GUEST_POLICY=null, mode='prove', engine='codex', runId=null;
const manuscriptMode=()=>mode!=='prove';
const manuscriptPolicy=()=>({sources:{upload_extensions:['tex','md','txt','pdf','zip']}});
const file=(name,size=1000)=>({name,size});
`
function page(body, prelude = '') {
  const code = [...CONSTS.map(constant), ...FUNCTIONS.map(fn), DOM, prelude, body].join('\n')
  return plain(vm.runInContext(code, vm.createContext({ console, Date, Map, Set, Array, String, Number, Math, JSON })))
}

test('tool-free engines take text and PDF files only and say how they read them', () => {
  const out = page(`(()=>{
    const view=id=>{ const l=attachmentLimits(id); return {images:l.extensions.includes('png')||l.extensions.includes('jpg'),pdf:l.extensions.includes('pdf'),tex:l.extensions.includes('tex'),toolFree:!!l.toolFree,help:l.help,title:l.title}; };
    return {kimi:view('kimi'),harness:view('math_harness'),plain:view('plain'),codex:view('codex'),claude:view('claude'),none:view('')};
  })()`)
  for (const id of ['kimi', 'harness', 'plain']) {
    assert.deepEqual([out[id].images, out[id].pdf, out[id].tex, out[id].toolFree], [false, true, true, true], id)
    assert.equal(out[id].help, 'PDF and text files are read as extracted text (formulas may be garbled); long files are cut to fit, and the chat lists the pages read.')
    assert.match(out[id].title, /sees no images/)
    assert.match(out[id].title, /paste the formula or statement as text; a run on Claude Code or Codex, where your account has them, can read images\./)
    assert.doesNotMatch(out[id].title, /or use Claude Code or Codex/)
  }
  for (const id of ['codex', 'claude', 'none']) {
    assert.deepEqual([out[id].images, out[id].pdf, out[id].toolFree], [true, true, false], id)
  }
})

test('guests keep the server text allowance, with copy that no longer promises images', () => {
  const out = page(`(()=>{
    CURRENT_USER={guest:true}; GUEST_POLICY={attachments:{max_files:2,max_total_bytes:65536,extensions:['md','tex','txt']}};
    const l=attachmentLimits('kimi');
    return {extensions:l.extensions,toolFree:!!l.toolFree,title:l.title,image:attachmentRefusal(l,'png'),pdf:attachmentRefusal(l,'pdf')};
  })()`)
  assert.deepEqual(out.extensions, ['md', 'tex', 'txt'])
  assert.equal(out.toolFree, false)
  assert.doesNotMatch(out.title, /images/)
  assert.match(out.title, /Sign in to attach PDFs too\./)
  assert.equal(out.image, ': not available for guest runs (text files only).')
  assert.equal(out.pdf, ': not available for guest runs (text files only).')
})

test('the new-run composer follows the chosen engine and drops images a tool-free engine cannot see', () => {
  const out = page(`(()=>{
    const composer=attachmentComposer('problem-attachments','problem-text',()=>engine);
    const root=nodes['problem-attachments'];
    const out={codexAccept:root.parts.input.accept};
    composer.add([file('figure.png'),file('paper.pdf')]);
    out.codexFiles=composer.files.map(x=>x.file.name);
    engine='kimi'; composer.refreshLimits();
    out.kimiFiles=composer.files.map(x=>x.file.name);
    out.kimiAccept=root.parts.input.accept;
    out.dropped=root.parts['.attachment-error'].textContent;
    out.help=root.parts['.attachment-help'].textContent;
    out.revoked=revoked;
    composer.add([file('plot.jpg'),file('notes.tex')]);
    out.refused=root.parts['.attachment-error'].textContent;
    out.after=composer.files.map(x=>x.file.name);
    engine='claude'; composer.refreshLimits();
    out.claudeKeeps=composer.files.map(x=>x.file.name);
    composer.add([file('figure.png')]);
    out.claudeFiles=composer.files.map(x=>x.file.name);
    return out;
  })()`)
  assert.match(out.codexAccept, /\.png/)
  assert.deepEqual(out.codexFiles, ['figure.png', 'paper.pdf'])
  assert.deepEqual(out.kimiFiles, ['paper.pdf'])
  assert.doesNotMatch(out.kimiAccept, /\.(png|jpe?g|gif|webp)\b/)
  assert.match(out.kimiAccept, /\.pdf/)
  // Every account reads it: Claude Code and Codex are named only as engines
  // the account may have, never as advice to switch to.
  const refusal = ': this engine reads text and PDF files but cannot see images. Paste the formula or statement as text instead; a run on Claude Code or Codex, where your account has them, can read images.'
  assert.equal(out.dropped, 'figure.png' + refusal)
  assert.match(out.help, /^PDF and text files are read as extracted text/)
  assert.equal(out.revoked, 1)
  assert.equal(out.refused, 'plot.jpg' + refusal)
  assert.doesNotMatch(out.refused + out.dropped, /or use Claude Code or Codex/)
  assert.deepEqual(out.after, ['paper.pdf', 'notes.tex'])
  assert.deepEqual(out.claudeKeeps, ['paper.pdf', 'notes.tex'])
  assert.deepEqual(out.claudeFiles, ['paper.pdf', 'notes.tex', 'figure.png'])
})

test("the chat composer follows the open run's engine", () => {
  // Built while the console's run record variable does not exist yet.
  const early = page(`(()=>{
    attachmentComposer('chat-attachments','chat-input',chatRunEngine);
    return nodes['chat-attachments'].parts.input.accept;
  })()`)
  assert.match(early, /\.png/)
  const out = page(`(()=>{
    const chat=attachmentComposer('chat-attachments','chat-input',chatRunEngine);
    const accept=()=>nodes['chat-attachments'].parts.input.accept;
    const out={};
    // From the run id until the record loads.
    for(const id of ['math_harness_adhoc_1a2b_3c','kimi_adhoc_1a2b_3c','plain_7_8','codex_adhoc_1_2','deepseek_harness_a_b']){ runId=id; chat.refreshLimits(); out[id]=accept(); }
    // The loaded record names the engine; another run's record never does.
    runId='legacy-run'; lastSandboxRun={run_id:'legacy-run',engine:'plain'}; chat.refreshLimits(); out.record=accept();
    lastSandboxRun={run_id:'other',engine:'plain'}; chat.refreshLimits(); out.stale=accept();
    return out;
  })()`, 'let lastSandboxRun=null;')
  for (const id of ['math_harness_adhoc_1a2b_3c', 'kimi_adhoc_1a2b_3c', 'plain_7_8']) assert.doesNotMatch(out[id], /\.png/, id)
  for (const id of ['codex_adhoc_1_2', 'deepseek_harness_a_b']) assert.match(out[id], /\.png/, id)
  assert.doesNotMatch(out.record, /\.png/)
  assert.match(out.stale, /\.png/)
})

test('the composers are built with their engines, and the chat one is refreshed when a run opens', () => {
  assert.match(script, /const initialAttachments=attachmentComposer\('problem-attachments','problem-text',\(\)=>engine\);/)
  assert.match(script, /const chatAttachments=attachmentComposer\('chat-attachments','chat-input',chatRunEngine\);/)
  assert.match(fn('selectRun'), /runId=rid;[^\n]*\n[^\n]*\n\s*chatAttachments\.refreshLimits\?\.\(\);/)
  // An engine change re-renders the composer (renderEngines → applyRunKindComposer).
  assert.match(fn('applyRunKindComposer'), /initialAttachments\.refreshLimits\(\);/)
})
