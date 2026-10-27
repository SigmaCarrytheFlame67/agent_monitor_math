// An analysis's live progress in the console, after a refusal fallback.
//
// Outside Max (API) too, a call that a refusal fallback passed on now books
// each model's share apart (math_harness_runner._emit_call_usage); before
// each declined share the runner writes "Analyze · check-3-low · declined
// first by <model>". Like the bare "Analyze · check-3-low", that only marks a
// model call: the progress the Sandbox and the status line show leaves it out.
//
// The real console functions run in a vm context with small stubs, as in
// test_run_control_ui.mjs.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]
const runner = read('../agent_monitor/runners/math_harness_runner.py')

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
// A vm context with these constants and functions; the returned function runs code in it.
function load(names, consts) {
  const sandbox = vm.createContext({})
  vm.runInContext([...consts.map(constant), ...names.map(fn)].join('\n'), sandbox)
  return code => vm.runInContext(code, sandbox)
}

const PROGRESS = ['analysisProgressLines', 'analysisRunProgress', 'analysisProgressDetail']
const PROGRESS_CONSTS = ['ANALYZE_CALL_MARKER', 'ANALYZE_STAGE_LINE', 'ANALYZE_RESULT_LINE', 'ANALYZE_NOW_LINE',
  'ANALYZE_SOURCE_WORDS', 'ANALYZE_VERDICT_SHORT']
// The runner's note before a declined share (an agent's output in the run record).
const declined = (stage, model) => `Analyze · ${stage} · declined first by ${model}`

test('the runner still writes a declined share\'s note as these tests do', () => {
  assert.match(runner, /"text": f"\{ctx\.label\} · \{stage\} · declined first by \{share_model\}"/)
})

test('a refusal fallback\'s note stays out of an analysis\'s progress, as the bare call marker does', () => {
  const step = load(PROGRESS, PROGRESS_CONSTS)
  const notes = [
    'Analyze · 3 Check · checking up to 12 of 19 results',
    'Analyze · check-3-low',
    declined('check-3-low', 'claude-fable-5-1'),
    declined('map', 'claude-fable-5-1'),
    declined('segment-2-retry', 'claude-fable-5-1'),
    declined('check-12-retry-low', 'claude-opus-5-5'),
    // A note of the runner's own may say the same words: it stays.
    'Analyze · Check · Lemma 3.7 · gap — the case declined first by the authors is open',
    'Analyze · Check · Theorem 1.1 · checked',
    declined('check-4', 'claude-fable-5-1'),
  ]
  const shown = ['3 Check · checking up to 12 of 19 results',
    'Check · Lemma 3.7 · gap — the case declined first by the authors is open', 'Check · Theorem 1.1 · checked']
  // The server's text (an older server's included), and the run's agents before it arrives.
  assert.deepEqual(plain(step(`analysisProgressLines(${JSON.stringify(notes.join('\n'))})`)), shown)
  const run = { status: 'running', agents: notes.map(output => ({ stage_name: 'check', output })) }
  assert.deepEqual(plain(step(`analysisRunProgress(${JSON.stringify(run)})`)), shown)
  // The status line reads the last note, never the call's.
  assert.equal(step(`analysisProgressDetail(analysisRunProgress(${JSON.stringify(run)}))`), 'Theorem 1.1 · checked')
})
