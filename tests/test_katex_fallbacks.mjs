// The console's and the shared page's stand-ins for commands KaTeX 0.16.11
// does not know, typeset with the vendored KaTeX, and every KaTeX call
// taking them under the author's own macros.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const consoleHtml = read('../agent_monitor/web/console.html')
const script = consoleHtml.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')
const sharedText = read('../agent_monitor/web/run-viewer-text.js')
const katex = createRequire(import.meta.url)('../agent_monitor/web/vendor/shared/katex.min.js')

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

// ── KaTeX stand-ins ──
// Every command the vendored KaTeX rejects that model and manuscript text uses.
const UNKNOWN = ['$\\qed$', 'x$\\qedhere$', '$\\mathbbm{1}_A$', '$\\mathds{1}$', '$\\symbb{R}$', '$a \\divides b$', '$\\textsc{Ramsey}(k)$',
  '$\\abs{x-y}$', '$\\norm{v}_2$', '$\\ceil{x/2}$', '$\\floor{\\log n}$', '$\\ref{sec:aec}$', '$\\eqref{eq:main}$', '$\\cref{lem:a}$',
  '$\\Cref{lem:a}$', '$\\autoref{thm:b}$', '$\\cite{Shelah1983}$', '$x = 1 \\label{eq:one}$']
const tex = item => item.slice(item.indexOf('$') + 1, item.lastIndexOf('$'))
const typeset = (formula, macros) => katex.renderToString(formula, { throwOnError: true, macros })
const visible = (formula, macros) => typeset(formula, macros).replace(/<annotation[\s\S]*?<\/annotation>/g, '').replace(/<[^>]+>/g, '').replace(/\u200b/g, '')

test('the vendored KaTeX typesets each command it lacks through the console\'s stand-ins', () => {
  const context = consoleContext(['withKatexFallbacks'], ['KATEX_FALLBACK_MACROS'])
  for (const item of UNKNOWN) {
    assert.throws(() => typeset(tex(item), {}), /Undefined control sequence/, item)
    assert.doesNotThrow(() => typeset(tex(item), context.withKatexFallbacks({}, item)), item)
  }
  const macros = context.withKatexFallbacks({}, '')
  // What they stand for.
  assert.match(visible('\\qed', macros), /□/)
  assert.equal(visible('x \\label{eq:one} y', macros), visible('x y', {}))
  assert.equal(visible('x\\qedhere', macros), visible('x', {}))
  assert.match(visible('\\eqref{eq:main}', macros), /\(eq:main\)/)
  assert.match(visible('\\cite{Shelah1983}', macros), /\[Shelah1983\]/)
  assert.match(visible('\\ref{sec:aec}', macros), /sec:aec/)
  assert.equal(visible('\\abs{x}', macros), visible('\\left\\lvert x\\right\\rvert', {}))
  assert.equal(visible('\\mathbbm{1}', macros), visible('\\mathbb{1}', {}))
  // The author's own macros win, and every call has a fresh set (KaTeX's \gdef writes into it).
  assert.equal(context.withKatexFallbacks({ '\\abs': '|#1|', '\\R': '\\mathbb{R}' }, '')['\\abs'], '|#1|')
  assert.equal(context.withKatexFallbacks({ '\\R': '\\mathbb{R}' }, '')['\\R'], '\\mathbb{R}')
  assert.notEqual(context.withKatexFallbacks({}, ''), context.withKatexFallbacks({}, ''))
  const fallbacks = vm.runInContext('KATEX_FALLBACK_MACROS', context)
  assert.equal(Object.isFrozen(fallbacks), true)
  assert.deepEqual(plain(context.withKatexFallbacks('junk', '')), plain(fallbacks))
  // A text that defines one of these names itself keeps its own: KaTeX refuses \newcommand for a known name.
  const own = '$\\newcommand{\\abs}[1]{|#1|}\\abs{x}$ and $\\newcommand\\norm[1]{\\|#1\\|}\\norm{v}$'
  assert.throws(() => typeset('\\newcommand{\\abs}[1]{|#1|}\\abs{x}', context.withKatexFallbacks({}, '')), /redefine/)
  assert.doesNotThrow(() => typeset('\\newcommand{\\abs}[1]{|#1|}\\abs{x}', context.withKatexFallbacks({}, own)))
  assert.deepEqual(['\\abs', '\\norm', '\\qed'].map(name => name in context.withKatexFallbacks({}, own)), [false, false, true])
  // That look-up stays linear on a long run of spaces.
  const started = performance.now()
  context.withKatexFallbacks({}, ('\\newcommand' + ' '.repeat(50000) + 'x').repeat(4))
  assert.ok(performance.now() - started < 1000)
})

test('every KaTeX call takes the stand-ins: the console, readability.js and the shared page', () => {
  // The console's auto-render, with the author's macros over them (and their expansion limit).
  const calls = []
  const context = consoleContext(['mathEnvDelimiters', 'renderMathIn', 'withKatexFallbacks'], ['KATEX_FALLBACK_MACROS'],
    'const window={renderMathInElement:true}; const renderMathInElement=(el,options)=>calls.push(options);')
  context.calls = calls
  const element = { textContent: '$\\qed$' }
  context.renderMathIn(element)
  context.renderMathIn(element, { '\\R': '\\mathbb{R}' })
  assert.equal(calls[0].macros['\\qed'], '\\square')
  assert.equal(calls[0].maxExpand, undefined)
  assert.deepEqual([calls[1].macros['\\R'], calls[1].macros['\\abs'], calls[1].maxExpand], ['\\mathbb{R}', '\\left\\lvert #1\\right\\rvert', 200])
  assert.match(fn('renderMathIn'), /renderMathInElement\(el,\{delimiters:\[\.\.\.mathEnvDelimiters\(\),[^]*macros:withKatexFallbacks\(macros,el\.textContent\)/)
  // readability.js typesets notation symbols with them (the console's helper; no list of its own).
  assert.match(readability, /window\.katex\.render\(tex,el,\{[^}]*macros:withKatexFallbacks\(macros,tex\)/)
  assert.doesNotMatch(readability, /\\\\qed|KATEX_FALLBACK_MACROS/)
  // The shared page: the same list, in its text renderer, used by the readable version's symbols too.
  const options = []
  const { text } = sharedRenderer({ renderMathInElement: (target, given) => options.push(given) })
  assert.deepEqual(plain(text.withKatexFallbacks({}, '')), plain(context.withKatexFallbacks({}, '')))
  text.render({ replaceChildren() {}, classList: { add() {} }, textContent: '' }, 'Done. $\\qed$', { macros: { '\\R': '\\mathbb{R}' } })
  text.math({ textContent: '' })
  assert.deepEqual([options[0].macros['\\qed'], options[0].macros['\\R'], options[1].macros['\\cite'], options[1].macros['\\R']],
    ['\\square', '\\mathbb{R}', '[\\text{#1}]', undefined])
  const viewerReadability = read('../agent_monitor/web/run-viewer-readability.js')
  assert.match(viewerReadability, /window\.AnsatzSharedText\?\.withKatexFallbacks\?\.\(macros, value\)/)
})
