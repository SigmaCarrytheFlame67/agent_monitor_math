// The console's and the shared page's stand-ins for commands KaTeX 0.16.11
// does not know, typeset with the vendored KaTeX, and every KaTeX call
// taking them under the author's own macros, which are counted as they
// expand.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const require = createRequire(import.meta.url)
const consoleHtml = read('../agent_monitor/web/console.html')
const script = consoleHtml.split('<script>').at(-1).split('</script>')[0]
const readability = read('../agent_monitor/web/readability.js')
const sharedText = read('../agent_monitor/web/run-viewer-text.js')
const viewerReadability = read('../agent_monitor/web/run-viewer-readability.js')
const katex = require('../agent_monitor/web/vendor/shared/katex.min.js')

// The shortest text from `start` that compiles and ends with `close`: one declaration.
function sourceFrom(source, start, close) {
  assert.ok(start >= 0, 'missing declaration')
  for (let end = source.indexOf(close, start); end >= 0; end = source.indexOf(close, end + 1)) {
    const text = source.slice(start, end + 1)
    try { new vm.Script(text); return text } catch { /* not complete yet */ }
  }
  throw new Error('unterminated source at ' + start)
}
const declaration = (source, name) => sourceFrom(source, source.search(new RegExp(`(?:async )?function ${name}\\(`)), '}')
const fn = name => declaration(script, name)
const constant = name => sourceFrom(script, script.indexOf(`const ${name}=`), ';')
// What one KaTeX call of the console uses: the reference keys, the stand-ins and the author's macros.
const KATEX_HELPERS = ['katexReferenceText', 'withKatexFallbacks', 'countedAuthorMacros']
// Functions of the console (or of readability.js) in a context of their own,
// with the vendored KaTeX as the page has it unless `prelude` says otherwise.
function consoleContext(names, consts = [], prelude = 'var window={katex};', globals = {}) {
  const context = vm.createContext({ console, katex, ...globals })
  const source = name => new RegExp(`function ${name}\\(`).test(script) ? fn(name) : declaration(readability, name)
  vm.runInContext([prelude, ...consts.map(constant), ...names.map(source)].join('\n'), context)
  return context
}
const plain = value => JSON.parse(JSON.stringify(value))
// The shared page's text renderer over a stub page; `parsed` is what marked was given.
function sharedRenderer({ renderMathInElement, document: page = {} } = {}) {
  const seen = {}
  const window = { marked: { parse: source => { seen.parsed = source; return source } }, renderMathInElement, katex }
  const document = {
    createElement: () => ({ set innerHTML(html) { seen.html = html }, content: { children: [] } }),
    createTreeWalker: () => ({ nextNode: () => null }),
    ...page,
  }
  vm.runInNewContext(sharedText, { window, document, NodeFilter: { SHOW_TEXT: 4 }, location: { href: 'https://ansatze.test/' } })
  return { text: window.AnsatzSharedText, seen }
}

// ── KaTeX stand-ins ──
// Every command the vendored KaTeX rejects that model and manuscript text uses.
const UNKNOWN = ['$\\qed$', 'x$\\qedhere$', '$\\mathbbm{1}_A$', '$\\mathds{1}$', '$\\symbb{R}$', '$a \\divides b$', '$\\textsc{Ramsey}(k)$',
  '$\\abs{x-y}$', '$\\norm{v}_2$', '$\\ceil{x/2}$', '$\\floor{\\log n}$', '$\\ref{sec:aec}$', '$\\eqref{eq:main}$', '$\\cref{lem:a}$',
  '$\\Cref{lem:a}$', '$\\autoref{thm:b}$', '$\\cite{Shelah1983}$', '$x = 1 \\label{eq:one}$',
  // Keys as LaTeX labels and BibTeX keys are written, and a citation's note.
  '$\\ref{lem_main}$', '$\\eqref{eq_1}$', '$\\cref{thm:a_b}$', '$\\Cref{a#b}$', '$\\autoref{a&b}$', '$\\ref{a^b}$', '$\\ref{a%b} + 1$',
  '$\\cite{Smith_2001}$', '$\\cite[Thm.~2]{Smith_2001}$', '$x \\label{eq_main}$']
const tex = item => item.slice(item.indexOf('$') + 1, item.lastIndexOf('$'))
const typeset = (formula, macros) => katex.renderToString(formula, { throwOnError: true, macros })
const strip = html => html.replace(/<annotation[\s\S]*?<\/annotation>/g, '').replace(/<[^>]+>/g, '').replace(/\u200b/g, '')
const visible = (formula, macros) => strip(typeset(formula, macros))
// The HTML half only (KaTeX writes the formula twice: MathML, then HTML), its
// no-break spaces (text mode's spaces and ~) as spaces.
const shownAs = html => strip(html.replace(/<span class="katex-mathml">[\s\S]*?<\/math><\/span>/, '')).replace(/\u00a0/g, ' ')

test('the vendored KaTeX typesets each command it lacks through the console\'s stand-ins', () => {
  const context = consoleContext(KATEX_HELPERS, ['KATEX_FALLBACK_MACROS'])
  for (const item of UNKNOWN) {
    assert.throws(() => typeset(tex(item), {}), /Undefined control sequence/, item)
    // As every KaTeX call of the console takes a formula: its reference keys as text, then the stand-ins.
    assert.doesNotThrow(() => typeset(context.katexReferenceText(tex(item)), context.withKatexFallbacks({}, item)), item)
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
  assert.equal(visible('\\abs{x}', context.withKatexFallbacks({ '\\abs': '|#1|', '\\R': '\\mathbb{R}' }, '')), visible('|x|', {}))
  assert.equal(visible('\\R', context.withKatexFallbacks({ '\\R': '\\mathbb{R}' }, '')), visible('\\mathbb{R}', {}))
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

test('a reference key is printed as written, and a citation\'s note after its key', () => {
  const context = consoleContext(KATEX_HELPERS, ['KATEX_FALLBACK_MACROS'])
  const macros = context.withKatexFallbacks({}, '')
  const shown = formula => shownAs(typeset(context.katexReferenceText(formula), macros))
  // Labels and BibTeX keys with _ (and the rarer # & % ^) are text, not subscripts or errors.
  assert.deepEqual(['\\ref{lem_main}', '\\eqref{eq_1}', '\\cref{thm:a_b}', '\\Cref{a#b} + \\autoref{a&b}', '\\ref{a^b}', '\\ref{a%b} + 1', '\\cite{Smith_2001}'].map(shown),
    ['lem_main', '(eq_1)', 'thm:a_b', 'a#b+a&amp;b', 'a^b', 'a%b+1', '[Smith_2001]'])
  // \cite[note]{key} as [key, note]; keys are listed as written.
  assert.deepEqual(['\\cite[Thm.~2]{Smith_2001}', '\\cite{a,b}', '\\cite[p.~5]{Lemma}'].map(shown), ['[Smith_2001, Thm. 2]', '[a,b]', '[Lemma, p. 5]'])
  assert.equal(context.katexReferenceText('\\cite[Thm.~2]{Smith_2001}'), '\\cite{Smith\\_2001, Thm.~2}')
  // A key the author escaped, and everything else, is left exactly as written.
  assert.equal(shown('\\ref{lem\\_main}'), 'lem_main')
  for (const formula of ['\\ref{lem\\_main}', '\\ref{sec:aec} = \\eqref{eq:main}', 'x_1^2 + y \\label{eq_main}', '\\ref[x]{y_1}', '\\cites{a_b}', '\\cite[{p}]{a_b}', '\\crefrange{a_b}{c}'])
    assert.equal(context.katexReferenceText(formula), formula)
  // The shared page escapes keys the same way.
  const { text } = sharedRenderer()
  for (const formula of [...UNKNOWN.map(tex), '\\ref{lem\\_main}', '\\cite[{p}]{a_b}']) assert.equal(text.katexReferenceText(formula), context.katexReferenceText(formula))
  // Linear on long unclosed keys and notes.
  const started = performance.now()
  context.katexReferenceText('\\cite['.repeat(20000) + '\\ref{' + '_'.repeat(100000))
  text.katexReferenceText('\\cite['.repeat(20000) + '\\ref{' + '_'.repeat(100000))
  assert.ok(performance.now() - started < 1000)
})

test('every KaTeX call takes the stand-ins: the console, readability.js and the shared page', () => {
  // The console's auto-render, with the author's macros over them (and their expansion limit).
  const calls = []
  const context = consoleContext(['mathEnvDelimiters', 'renderMathIn', 'untypesetText', ...KATEX_HELPERS], ['KATEX_FALLBACK_MACROS'],
    'var window={renderMathInElement:true,katex}; const renderMathInElement=(el,options)=>calls.push(options);', { calls })
  const element = { textContent: '$\\qed$' }
  context.renderMathIn(element)
  context.renderMathIn(element, { '\\R': '\\mathbb{R}' })
  assert.equal(calls[0].macros['\\qed'], '\\square')
  assert.equal(calls[0].maxExpand, undefined)
  assert.equal(calls[0].preProcess('\\eqref{eq_1}'), '\\eqref{eq\\_1}')
  assert.equal(visible('\\R', calls[1].macros), visible('\\mathbb{R}', {}))
  assert.deepEqual([calls[1].macros['\\abs'], calls[1].maxExpand], ['\\left\\lvert #1\\right\\rvert', 200])
  assert.match(fn('renderMathIn'), /renderMathInElement\(el,\{delimiters:\[\.\.\.mathEnvDelimiters\(\),[^]*preProcess:katexReferenceText,macros:withKatexFallbacks\(macros,untypesetText\(el\)\)/)
  // readability.js typesets notation symbols with them (the console's helpers; no list of its own).
  assert.match(readability, /window\.katex\.render\(katexReferenceText\(tex\),el,\{[^}]*macros:withKatexFallbacks\(macros,tex\),maxExpand:200\}/)
  assert.doesNotMatch(readability, /\\\\qed|KATEX_FALLBACK_MACROS|function (?:withKatexFallbacks|katexReferenceText|countedAuthorMacros)\(/)
  // The shared page: the same list in its text renderer, used by the readable version's symbols too.
  const options = []
  const { text } = sharedRenderer({ renderMathInElement: (target, given) => options.push(given) })
  assert.deepEqual(plain(text.withKatexFallbacks({}, '')), plain(context.withKatexFallbacks({}, '')))
  text.render({ replaceChildren() {}, classList: { add() {} }, textContent: '' }, 'Done. $\\qed$', { macros: { '\\R': '\\mathbb{R}' } })
  text.math({ textContent: '' })
  assert.deepEqual([options[0].macros['\\qed'], typeof options[0].macros['\\R'], options[1].macros['\\cite'], options[1].macros['\\R']],
    ['\\square', 'function', '[\\text{#1}]', undefined])
  assert.equal(visible('\\R', options[0].macros), visible('\\mathbb{R}', {}))
  // The console's expansion limit where an author's macros apply, KaTeX's usual one elsewhere.
  assert.deepEqual([options[0].maxExpand, options[1].maxExpand], [200, 1000])
  assert.deepEqual([options[0].preProcess, options[1].preProcess], [text.katexReferenceText, text.katexReferenceText])
  assert.match(viewerReadability, /window\.katex\.render\(shared\.katexReferenceText\(value\), element, options\)/)
})

// ── An author's macros, counted ──
// A preamble a manuscript may have (each body within the 400 characters the
// extractor keeps, nothing that defines): a readable version's one formula
// $\a$ wrote 148,200 symbols and held the shared page for minutes.
const CRAFTED = { '\\a': '\\b'.repeat(2), '\\b': '\\c'.repeat(190), '\\c': 'x'.repeat(390) }
// Each formula of `text` typeset with the options of one auto-render call, as auto-render does it.
function autoRender(text, options) {
  return [...text.matchAll(/\$([^$]+)\$/g)].map(([, formula]) => katex.renderToString(options.preProcess ? options.preProcess(formula) : formula, options))
}
const refused = html => /class="katex-error" title="ParseError: KaTeX parse error: the author’s macros write too much here"/.test(html)
const symbols = html => (shownAs(html).match(/x/g) || []).length

test('an author\'s macros cannot make a short text typeset far more than it shows', () => {
  const calls = [], options = []
  const context = consoleContext(['mathEnvDelimiters', 'renderMathIn', 'untypesetText', 'analysisKatexMacros', ...KATEX_HELPERS], ['KATEX_FALLBACK_MACROS'],
    'var window={renderMathInElement:true,katex}; const renderMathInElement=(el,options)=>calls.push(options);', { calls })
  const shared = sharedRenderer({ renderMathInElement: (target, given) => options.push(given) })
  const viewer = require('../agent_monitor/web/run-viewer-readability.js')
  // Both pages' filters keep this preamble (short bodies, nothing that defines): the count holds it.
  const consoleMacros = context.analysisKatexMacros({ macros: CRAFTED }), sharedMacros = viewer.katexMacros(CRAFTED)
  assert.deepEqual([Object.keys(consoleMacros), Object.keys(sharedMacros)], [Object.keys(CRAFTED), Object.keys(CRAFTED)])
  // The options of one call on each page for an element with this text.
  const pages = text => {
    context.renderMathIn({ textContent: text }, consoleMacros)
    shared.text.math({ textContent: text }, { macros: sharedMacros })
    return { console: calls.at(-1), shared: options.at(-1) }
  }

  // The formula is shown as written, at once (expanded, it took 117 s at KaTeX's maxExpand 1000 and 37 s at 200).
  for (const [page, given] of Object.entries(pages('The main quantity is $\\a$.'))) {
    const started = performance.now(), [html] = autoRender('The main quantity is $\\a$.', given)
    assert.ok(performance.now() - started < 1000, page)
    assert.ok(refused(html), page)
    assert.equal(shownAs(html), '\\a', page)
    assert.equal(given.maxExpand, 200, page)
  }
  // In one formula the author's macros write at most 8,000 characters; each formula counts its own.
  const long = 'Long. $' + '\\c'.repeat(20) + '$ $' + '\\c'.repeat(20) + '$ $' + '\\c'.repeat(21) + '$ ' + '.'.repeat(20000)
  for (const [page, given] of Object.entries(pages(long))) {
    const html = autoRender(long, given)
    assert.deepEqual(html.map(refused), [false, false, true], page)
    assert.deepEqual(html.slice(0, 2).map(symbols), [7800, 7800], page)
  }
  // In one call at most 400 characters plus 2 for each character of its text, however many formulas there are.
  const many = Array.from({ length: 300 }, () => '$\\c$').join(' ')
  for (const [page, given] of Object.entries(pages(many))) {
    const html = autoRender(many, given), done = html.filter(item => !refused(item))
    assert.equal(done.length, Math.floor((400 + 2 * many.length) / 390), page)
    assert.ok(html.slice(done.length).every(refused), page)
  }
  // A macro that writes an argument twice is left out: KaTeX copies arguments after the count.
  const doubling = { '\\d': '#1#1', '\\pair': '(#1, #2)', '\\esc': '\\##1' }
  for (const macros of [context.withKatexFallbacks(doubling, ''), shared.text.withKatexFallbacks(doubling, '')]) {
    assert.deepEqual(['\\d' in macros, typeof macros['\\pair'], typeof macros['\\esc']], [false, 'function', 'function'])
    assert.throws(() => typeset('\\d{\\d{\\d{\\d{\\d{\\d{\\d{' + 'x'.repeat(2000) + '}}}}}}}', macros), /Undefined control sequence: \\d/)
  }
  // A manuscript's own macros typeset as before, nested ones too.
  const real = { '\\RR': '\\mathbb R', '\\Rn': '\\RR^n', '\\rank': '\\operatorname{rank}', '\\abs': '\\left|#1\\right|' }
  context.renderMathIn({ textContent: 'x' }, context.analysisKatexMacros({ macros: real }))
  shared.text.math({ textContent: 'x' }, { macros: viewer.katexMacros(real) })
  for (const given of [calls.at(-1), options.at(-1)]) {
    assert.deepEqual(autoRender('$\\Rn$, $\\rank A \\le n$, $\\abs{x}$', given).map(shownAs),
      ['\\mathbb R^n', '\\operatorname{rank} A \\le n', '\\left|x\\right|'].map(formula => shownAs(katex.renderToString(formula))))
  }
  // Formulas KaTeX typeset before (a readable version's notation symbols) give the rest no room.
  const nodes = [{ data: 'Meaning: $\\c$', parentElement: { closest: () => null } }, { data: 'x'.repeat(50000), parentElement: { closest: () => ({}) } }]
  const walker = () => { let at = 0; return { nextNode: () => nodes[at++] || null } }
  const typesetBefore = { textContent: nodes.map(item => item.data).join(''), querySelector: selector => selector === '.katex' ? {} : null }
  const before = []
  sharedRenderer({ renderMathInElement: (target, given) => before.push(given), document: { createTreeWalker: walker } }).text.math(typesetBefore, { macros: sharedMacros })
  Object.assign(context, { document: { createTreeWalker: walker }, NodeFilter: { SHOW_TEXT: 4 } })
  context.renderMathIn(typesetBefore, consoleMacros)
  for (const given of [before[0], calls.at(-1)]) assert.ok(refused(autoRender('$\\c\\c$', given)[0]))
})

test('notation symbols count the author\'s macros too: readability.js and the shared page', () => {
  const rendered = []
  const record = (value, element, given) => rendered.push([value, given])
  // readability.js, with the console's helpers.
  const consoleSide = consoleContext(['readabilityTex', 'analysisKatexMacros', ...KATEX_HELPERS], ['KATEX_FALLBACK_MACROS'],
    'var window={katex:{ParseError:katex.ParseError,render:record}};', { record })
  consoleSide.readabilityTex({ textContent: '' }, '\\a', consoleSide.analysisKatexMacros({ macros: CRAFTED }))
  consoleSide.readabilityTex({ textContent: '' }, '\\eqref{eq_1}', {})
  // The shared page's tex(), with the page's text renderer.
  const { text } = sharedRenderer()
  const viewer = require('../agent_monitor/web/run-viewer-readability.js')
  const sharedSide = vm.createContext({ window: { katex: { render: record }, AnsatzSharedText: text } })
  vm.runInContext(declaration(viewerReadability, 'tex'), sharedSide)
  sharedSide.tex({ textContent: '' }, '\\a', viewer.katexMacros(CRAFTED))
  sharedSide.tex({ textContent: '' }, '\\eqref{eq_1}', {})
  assert.deepEqual(rendered.map(([value, given]) => [value, given.maxExpand, given.trust]),
    [['\\a', 200, false], ['\\eqref{eq\\_1}', 200, false], ['\\a', 200, false], ['\\eqref{eq\\_1}', 200, false]])
  for (const [value, given] of rendered) {
    const started = performance.now(), html = katex.renderToString(value, given)
    assert.ok(performance.now() - started < 1000, value)
    assert.equal(refused(html), value === '\\a', value)
  }
  assert.equal(shownAs(katex.renderToString(rendered[1][0], rendered[1][1])), '(eq_1)')
  // Without the page's text renderer a symbol stays text.
  const bare = vm.createContext({ window: { katex: { render: record } } })
  vm.runInContext(declaration(viewerReadability, 'tex'), bare)
  const element = { textContent: '' }
  bare.tex(element, '\\a', viewer.katexMacros(CRAFTED))
  assert.deepEqual([element.textContent, rendered.length], ['\\a', 4])
})
