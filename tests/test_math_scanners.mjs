// The math scanners that set formulas aside before Markdown is read (the
// console, readability.js through it, and the shared page) and that split
// math from prose in the Analyze and Readability views: they must find what
// the regular expressions they replace found, in linear time.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const consoleHtml = read('../agent_monitor/web/console.html')
const script = consoleHtml.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')
const sharedText = read('../agent_monitor/web/run-viewer-text.js')
// A real closure: proof.md of a live CGM run.
const CLOSURE = JSON.parse(read('./fixtures/cgm_phase1_artifacts.json')).cases['live-oddsum-kimi-k3'].files['proof.md']

// The shortest text from `start` that compiles and ends with `close`: one declaration.
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
// The console's functions in a context of their own.
function consoleContext(names, consts = [], prelude = '') {
  const context = vm.createContext({ console })
  vm.runInContext([prelude, ...consts.map(constant), ...names.map(fn)].join('\n'), context)
  return context
}
const plain = value => JSON.parse(JSON.stringify(value))
// The shared page's text renderer over a stub page; `parsed` is what marked was given.
function sharedRenderer({ renderMathInElement } = {}) {
  const seen = {}
  const window = { marked: { parse: source => { seen.parsed = source; return source } }, renderMathInElement }
  const document = {
    createElement: () => ({ set innerHTML(html) { seen.html = html }, content: { children: [] } }),
    createTreeWalker: () => ({ nextNode: () => null }),
  }
  vm.runInNewContext(sharedText, { window, document, NodeFilter: {}, location: { href: 'https://ansatze.test/' } })
  return { text: window.AnsatzSharedText, seen }
}

// ── math scanners ──
// The regular expressions the scanners replace, as they were.
function legacyStashMath(src) {
  const slots = []
  const hold = m => { slots.push(m); return '@@MATH' + (slots.length - 1) + '@@' }
  let s = String(src || '')
  s = s.replace(/\$\$[\s\S]+?\$\$/g, hold)
  s = s.replace(/\\\[[\s\S]+?\\\]/g, hold)
  s = s.replace(/\\\([\s\S]+?\\\)/g, hold)
  s = s.replace(/(^|[^\\$])(\$(?!\$)(?:\\.|[^$\n])+\$)/g, (m, pre, eq) => pre + hold(eq))
  return { s, slots }
}
function legacySharedStash(original) {
  const slots = []
  const hold = expression => { slots.push(expression); return '@@ANSATZE_MATH_SLOT_' + (slots.length - 1) + '@@' }
  const source = original.replace(/\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}[\s\S]*?\\end\{\1\}/g, hold)
    .replace(/\$\$[\s\S]+?\$\$/g, hold)
    .replace(/\\\[[\s\S]+?\\\]/g, hold)
    .replace(/\\\([\s\S]+?\\\)/g, hold)
    .replace(/(^|[^\\$])(\$(?!\$)(?:\\.|[^$\n])+\$)/g, (_, prefix, expression) => prefix + hold(expression))
  return { source, slots }
}
const LEGACY_MANUSCRIPT_MATH = /(?:\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}[\s\S]*?\\end\{\1\}|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?:\\.|[^$\\])+\$)/g
const STASH = ['stashMath', 'stashInlineMath', 'inlineMathEnd', 'mathNextIndex', 'mathBetween', 'mathSpans', 'holdMath']
// What the shared renderer sets aside: the text marked reads, and the page it writes.
function sharedStash(shared, value) {
  const { text: renderer, seen } = shared
  renderer.render({ replaceChildren() {}, classList: { add() {} } }, value)
  return [seen.parsed, seen.html]
}
// The same through the legacy expressions (slots back in, as the renderer does).
const { closureCitedLemmas } = sharedRenderer().text
function legacySharedStashed(value) {
  const { source, slots } = legacySharedStash(closureCitedLemmas(value))
  const escape = item => String(item).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]))
  return [source, source.replace(/@@ANSATZE_MATH_SLOT_(\d+)@@/g, (_, index) => escape(slots[Number(index)] || ''))]
}

// Text as the console and shared page meet it: proofs, write-ups, readable versions, manuscripts.
const NORMAL = [
  CLOSURE,
  'For every $n\\in\\mathbb N$, $n^2+n$ is even. $$n^2+n=n(n+1).$$ \\[ a^2 \\] and \\( b \\). $\\blacksquare$',
  'A price of \\$5, a ratio $a/b$, and $\\{x : x>0\\}$; then $x\\$y$ and $$\\begin{aligned}a&=b\\\\c&=d\\end{aligned}$$',
  '| $x$ | $y$ |\n|---|---|\n| $\\|v\\|$ | `$code$` |\n\nInline `\\(` code and $\\alpha$-stable sets.',
  '\\begin{equation}E=mc^2\\end{equation} then \\begin{align*}a&=1\\\\b&=2\\end{align*} and \\begin{alignat}{2}x\\end{alignat}',
  'Unclosed $x and a closed $y$ later; \\[ never closed, \\( neither; $$ alone.',
  '**Theorem 2.1.** If $f\\colon X\\to Y$ is continuous, then $f^{-1}(U)$ is open.\n\n> ⚠ **Check:** compare with the original.\n\n[result:thm:main]: #\n',
  'Lemma~\\ref{lem:a} and \\cite[Thm. 2]{Smith}, with ~ ties; $\\ref{x}~y$ stays.',
  '$$\n\\sum_{k=1}^{n} (2k-1) = n^2\n$$\n\nwhere $n\\ge 1$ and $$\n$$ empty.',
]
// A deterministic sprinkle of the characters that matter.
function* randomTexts(count, pieces, maxLength, seed = 20261022) {
  let state = seed
  const next = n => { state = (state * 1103515245 + 12345) & 0x7fffffff; return state % n }
  for (let i = 0; i < count; i++) {
    let value = ''
    for (let j = 1 + next(maxLength); j > 0; j--) value += pieces[next(pieces.length)]
    yield value
  }
}
const PIECES = ['$', '$', '$', '$$', '\\', '\\', '\\', '[', ']', '(', ')', '\n', '\r', 'a', ' ', '{', '}', '\\[', '\\]', '\\(', '\\)', '\\$',
  '\\begin{equation}', '\\end{equation}', '\\begin{align*}', '\\end{align*}', '\\end{align}', '\\begin{alignat}', '\\end{alignat}', '~', '\u2028']

test('the console finds the same math as the patterns it replaces, on real text and on a sprinkle of delimiters', () => {
  const context = consoleContext(STASH)
  for (const text of [...NORMAL, ...randomTexts(4000, PIECES, 18)])
    assert.deepEqual(plain(context.stashMath(text)), legacyStashMath(text), JSON.stringify(text))
  assert.deepEqual(plain(context.stashMath(null)), { s: '', slots: [] })
  // The real closure's math is all set aside.
  assert.ok(context.stashMath(CLOSURE).slots.length > 0)
})

test('the shared page finds the same math as the patterns it replaces', () => {
  const shared = sharedRenderer()
  for (const text of [...NORMAL, ...randomTexts(2500, PIECES, 14, 7)]) {
    assert.deepEqual(sharedStash(shared, text), legacySharedStashed(text), JSON.stringify(text))
    // A manuscript's statements: prose cleaned, math left as written (environments unnumbered).
    assert.equal(shared.text.manuscriptProse(text), legacyManuscriptProse(text), JSON.stringify(text))
  }
})
function legacyManuscriptProse(source) {
  let end = 0, result = ''
  const prose = text => text
    .replace(/\[(?:\d*\.)?\d+(?:em|ex|pt)\]\s*\\(?:large|Large|LARGE|small|normalsize)\b/g, ' ')
    .replace(/\\(?:large|Large|LARGE|small|normalsize)\b/g, '')
    .replace(/\\(?:[cC]ref|autoref|nameref|ref)\*?\{([^{}]{1,200})\}/g, '$1')
    .replace(/\\eqref\*?\{([^{}]{1,200})\}/g, '($1)')
    .replace(/\\(?:emph|textbf|textit|textsc|textrm|textsf|texttt|text|mbox)\{([^{}]*)\}/g, '$1')
    .replace(/\\cite[pt]?\*?(?:\[[^\]]*\])*\{([^{}]{1,200})\}/g, '[$1]')
    .replace(/~/g, ' ').replace(/\\\\(?:\[[^\]]*\])?/g, ' ')
  for (const match of source.matchAll(LEGACY_MANUSCRIPT_MATH)) {
    result += prose(source.slice(end, match.index))
    result += match[0].replace(/\\(begin|end)\{(equation|align|alignat|gather)\}/g, '\\$1{$2*}')
    end = match.index + match[0].length
  }
  return result + prose(source.slice(end))
}

test('hostile text is scanned in linear time: unclosed \\[ and \\(, and a "$" before a long run of backslashes', () => {
  const context = consoleContext(STASH)
  const shared = sharedRenderer()
  // The patterns these replace took seconds on the first two (quadratic) and never ended on the
  // backslash runs (exponential: each backslash escapes the next or stands alone).
  const hostile = {
    'unclosed \\[': '\\['.repeat(100000), 'unclosed \\(': '\\('.repeat(100000), 'unclosed $$': '$$a'.repeat(60000),
    'backslashes after $': '$' + '\\'.repeat(100000), 'many short runs': ('a$' + '\\'.repeat(61)).repeat(2000),
    'escaped $ then backslashes': '$a\\$' + '\\'.repeat(100000), 'environments': '\\begin{equation}'.repeat(20000),
    'escaped dollars': '$a' + '\\$'.repeat(60000), 'many formulas': '$x$ '.repeat(50000),
  }
  for (const [name, text] of Object.entries(hostile)) {
    for (const [where, scan] of [['console', () => context.stashMath(text)], ['shared page', () => sharedStash(shared, text)], ['manuscript', () => shared.text.manuscriptProse(text)]]) {
      const started = performance.now()
      scan()
      const took = performance.now() - started
      assert.ok(took < 1500, `${where}, ${name}: ${Math.round(took)} ms`)
    }
  }
  // Short versions still give the patterns' answers.
  for (const text of ['$' + '\\'.repeat(21), '$a\\$' + '\\'.repeat(20) + '$', 'x$' + '\\'.repeat(15) + '$y$'])
    assert.deepEqual(plain(context.stashMath(text)), legacyStashMath(text), JSON.stringify(text))
  // readability.js has no math scanner of its own: it uses the console's
  // (display environments through mathSpans/mathEnvironment, then stashMath).
  assert.match(readability, /mathSpans\(source,\['equation','align','alignat','gather'\]\.map\(name=>mathEnvironment\(source,name\)\)\)/)
  assert.match(readability, /const stashed=stashMath\(held\+source\.slice\(at\)\);/)
  assert.doesNotMatch(readability, /\[\\s\\S\]\+\?|\(\?:\\\\\.\|/)
})

// ── the Analyze and Readability prose helpers ──
// The alternations analysisModelProse/analysisTextAccents and analysisProse split on, as they were.
const LEGACY_PROSE_SPLIT = /(\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])/
const LEGACY_STATEMENT_SPLIT = /(\\begin\{equation\*?\}[\s\S]*?\\end\{equation\*?\}|\\begin\{align\*?\}[\s\S]*?\\end\{align\*?\}|\\begin\{alignat\*?\}[\s\S]*?\\end\{alignat\*?\}|\\begin\{gather\*?\}[\s\S]*?\\end\{gather\*?\}|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?:\\.|[^$\\])+\$)/
const SPLIT = ['mathNextIndex', 'mathBetween', 'mathEnvironment', 'mathDollarLine', 'mathEscapedDollar', 'mathSpans', 'splitMath']
const PROSE = ['analysisTextAccents', 'analysisResolveRefs', 'analysisUnresolvedLabel', 'analysisProse', 'analysisModelProse']
const PROSE_CONSTS = ['ANALYZE_ACCENT_MARKS', 'ANALYZE_BARE_ACCENTED', 'ANALYZE_TEXT_LETTERS', 'ANALYZE_REF_TIE', 'ANALYZE_LABEL_KINDS', 'ANALYZE_MATH_KINDS', 'ANALYZE_STATEMENT_MATH_KINDS']

test('the Analyze prose helpers split math from prose as their patterns did, in linear time', () => {
  const context = consoleContext([...SPLIT, ...PROSE], PROSE_CONSTS)
  const split = (text, kinds) => plain(vm.runInContext(`(text=>splitMath(text,${kinds}))`, context)(text))
  const pieces = [...PIECES, "\\'", '\\H', '\\o', '\\ref{x}', '\\end{equation*}', '\\begin{gather}', '\\end{gather}', '\\emph{x}']
  for (const text of [...NORMAL, ...randomTexts(4000, pieces, 18, 11)]) {
    assert.deepEqual(split(text, 'ANALYZE_MATH_KINDS'), text.split(LEGACY_PROSE_SPLIT), JSON.stringify(text))
    assert.deepEqual(split(text, 'ANALYZE_STATEMENT_MATH_KINDS'), text.split(LEGACY_STATEMENT_SPLIT), JSON.stringify(text))
  }
  // Every helper splits with them.
  assert.match(fn('analysisModelProse'), /splitMath\(value,ANALYZE_MATH_KINDS\)/)
  assert.match(fn('analysisTextAccents'), /splitMath\(value,ANALYZE_MATH_KINDS\)/)
  assert.match(fn('analysisProse'), /splitMath\(String\(text\|\|''\),ANALYZE_STATEMENT_MATH_KINDS\)/)
  // A report or statement of hostile text is read quickly (the patterns took quadratic time).
  for (const text of ['\\['.repeat(100000) + '\\o', '\\('.repeat(100000) + '~', '$a' + '\\$'.repeat(60000), ('\\[ \\begin{align} ').repeat(20000) + '\\end{align}']) {
    for (const helper of ['analysisTextAccents', 'analysisModelProse', 'analysisProse']) {
      const started = performance.now()
      context[helper](text, null)
      const took = performance.now() - started
      assert.ok(took < 1500, `${helper}: ${Math.round(took)} ms`)
    }
  }
})
