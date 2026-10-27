// The locale of the console's numbers.
//
// Production, 2026-10-26: counts were formatted in the browser's locale on an
// English page: a Chinese one wrote 13,723 tokens as "1.4万" (10,117 as
// "1万"), a German one as "13.723", an Egyptian one in Arabic-Indic digits.
// Every number is now written in en-US (UI_LOCALE); dates and times keep the
// viewer's locale.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import vm from 'node:vm'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const html = read('../agent_monitor/web/console.html')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]

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
// A vm context with these constants and functions after `prelude`; `step` runs code in it.
function load(names, consts, prelude) {
  const sandbox = vm.createContext({ console })
  vm.runInContext([prelude, ...consts.map(constant), ...names.map(fn)].join('\n'), sandbox)
  return code => vm.runInContext(code, sandbox)
}

test('numbers are written in en-US whatever the browser\'s locale', () => {
  for (const locale of ['zh-CN', 'de-DE', 'hi-IN', 'ar-EG']) {
    // A browser in `locale`: formatting without a locale follows it.
    const step = load(['escapeHtml', 'problemCardHtml'], ['UI_LOCALE', 'fmtInt', 'fmtCompact'], `
const RealFormat=Intl.NumberFormat, realLocaleString=Number.prototype.toLocaleString;
Intl.NumberFormat=function(locales,options){ return new RealFormat(locales===undefined?${JSON.stringify(locale)}:locales,options); };
Number.prototype.toLocaleString=function(locales,options){ return realLocaleString.call(this,locales===undefined?${JSON.stringify(locale)}:locales,options); };
const browser=n=>[new Intl.NumberFormat().format(n),new Intl.NumberFormat(undefined,{notation:'compact',maximumFractionDigits:1}).format(n)].join(' ');`)
    // The stand-in browser is not en-US ("1.4万", "1.433.358", "14,33,358", Arabic-Indic digits).
    assert.notEqual(step('browser(13723)+" "+browser(1433358)'), '13,723 13.7K 1,433,358 1.4M', locale)
    assert.equal(step('fmtInt(1433358)'), '1,433,358', locale)
    assert.equal(step('fmtCompact(13723)'), '13.7K', locale)
    assert.equal(step('fmtCompact(1433358)'), '1.4M', locale)
    assert.match(step("problemCardHtml('x'.repeat(12345),1,1)"), /<span class="n">12,345 chars<\/span>/, locale)
  }
})

test('no number on any console page is formatted in the browser\'s locale', () => {
  // Dates and times may follow the viewer (toLocaleString([], …), toLocaleTimeString([], …)).
  // Any other toLocaleString / NumberFormat call names UI_LOCALE (or 'en-US'):
  // no locale, undefined, [] or navigator.language all mean the browser's.
  const pinned = /^\s*(?:UI_LOCALE|'en-US'|"en-US")\s*[,)]/
  const dateOrTime = /\b(?:dateStyle|timeStyle|hour|minute|second|weekday|month|year)\b|Date\b/
  const browserLocale = line => [...line.matchAll(/(?:\.toLocaleString|Intl\.NumberFormat)\(/g)]
    .some(call => !pinned.test(line.slice(call.index + call[0].length)) && !dateOrTime.test(line))
  for (const sample of ['n.toLocaleString()', 'n.toLocaleString(undefined,{maximumFractionDigits:1})',
    'n.toLocaleString([])', 'n.toLocaleString(navigator.language)', 'new Intl.NumberFormat().format(n)',
    "new Intl.NumberFormat(undefined,{notation:'compact'})", 'new Intl.NumberFormat([],{style:"percent"})']) {
    assert.ok(browserLocale(sample), sample)
  }
  for (const sample of ['n.toLocaleString(UI_LOCALE)', "new Intl.NumberFormat('en-US',{notation:'compact'})",
    "d.toLocaleString([],{dateStyle:'medium',timeStyle:'short'})"]) {
    assert.ok(!browserLocale(sample), sample)
  }
  const web = new URL('../agent_monitor/web/', import.meta.url)
  const sources = readdirSync(web).filter(name => /\.(?:js|html)$/.test(name))
  assert.ok(sources.includes('console.html') && sources.includes('public_kimi.js'))
  for (const name of sources) {
    const lines = readFileSync(new URL(name, web), 'utf8').split('\n')
    const hits = lines.map((line, i) => [i + 1, line]).filter(([, line]) => browserLocale(line))
    assert.deepEqual(hits.map(([n, line]) => `${name}:${n}: ${line.trim().slice(0, 120)}`), [], name)
  }
  const publicKimi = read('../agent_monitor/web/public_kimi.js')
  assert.match(publicKimi, /const UI_LOCALE='en-US';/)
  assert.match(publicKimi, /\$\{size\.toLocaleString\(UI_LOCALE\)\} \/ \$\{limit\.toLocaleString\(UI_LOCALE\)\}/)
  assert.match(publicKimi, /\$\{problemLimit\(\)\.toLocaleString\(UI_LOCALE\)\} character public limit/)
})
