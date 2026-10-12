// Render text with $…$, $$…$$, \(…\) and \[…\] math to escaped HTML with KaTeX (build- and client-safe).
import katex from './vendor/katex/katex.mjs'

const MATH = /(?<!\\)\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?!\$)((?:\\.|[^$\n])+?)\$/g
// Titles, headlines and notes also quote prizes: there a "$" before an amount ("$500", "$1,000,000).") is money, not
// the start of inline math, so "a $500 prize for $n=2$" typesets only n=2.
const MATH_WITH_MONEY = /(?<!\\)\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<![\\$])\$(?!\$)(?!\d[\d,]*(?:\.\d+)?(?:\s|\)|[.,;:!?](?=\s|$)|$))((?:\\.|[^$\n])+?)\$/g
const SHORT = 24

export const escapeHtml = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')

// KaTeX lets inline math wrap after every top-level relation and operator ("x =" / "2k + 1").
// Bracing keeps a short formula on one line and lets a long one wrap only after a top-level '='.
export function keepTogether(tex) {
  const parts = []
  let depth = 0
  let last = 0
  for (let i = 0; i < tex.length; i++) {
    const ch = tex[i]
    if (ch === '\\') i++
    else if ('{(['.includes(ch)) depth++
    else if ('})]'.includes(ch)) depth--
    else if (ch === '=' && depth === 0) {
      parts.push(tex.slice(last, i))
      last = i + 1
    }
  }
  parts.push(tex.slice(last))
  if (tex.length <= SHORT || parts.length < 2 || parts.some(part => !part.trim())) return `{${tex}}`
  return parts.map(part => `{${part}}`).join('=')
}

const decode = text => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'").replace(/&amp;/g, '&')
// The words of a formula, from KaTeX's MathML: the name of a link or heading leaves MathML out, so a formula there needs them.
export function mathSpeech(html) {
  const math = html.match(/<math[\s\S]*?<\/math>/)?.[0] || ''
  return decode(math.replace(/<annotation[\s\S]*?<\/annotation>/g, '').replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim()
}

// Text-mode TeX that arXiv titles carry outside math: accents (Erd\H{o}s, G\"odel), dashes, quotes, escapes.
const TEXT_ACCENTS = { "'": '́', '`': '̀', '^': '̂', '"': '̈', '~': '̃', '=': '̄', '.': '̇',
  H: '̋', v: '̌', u: '̆', c: '̧', k: '̨', r: '̊' }
const TEXT_LETTERS = { o: 'ø', O: 'Ø', l: 'ł', L: 'Ł', ss: 'ß', aa: 'å', AA: 'Å', ae: 'æ', AE: 'Æ', oe: 'œ', OE: 'Œ', i: 'ı', j: 'ȷ' }
export function texText(text = '') {
  return String(text)
    .replace(/\\([`'^"~=.])\s*(?:\{\s*([A-Za-z]|\\i)\s*\}|([A-Za-z]))/g, (_, accent, braced, bare) =>
      ((braced || bare) === '\\i' ? 'i' : (braced || bare)) + TEXT_ACCENTS[accent])
    .replace(/\\([HvuckR])(?:\s*\{\s*([A-Za-z]|\\i)\s*\}|\s+([A-Za-z]))/g, (match, accent, braced, bare) =>
      TEXT_ACCENTS[accent] ? ((braced || bare) === '\\i' ? 'i' : (braced || bare)) + TEXT_ACCENTS[accent] : match)
    .replace(/\\(ss|aa|AA|ae|AE|oe|OE|o|O|l|L|i|j)(?![A-Za-z])(?:\{\}|[ \t])?/g, (_, name) => TEXT_LETTERS[name])
    .replace(/\\([&%#_$])/g, '$1')
    .replace(/---/g, '—').replace(/--/g, '–')
    .replace(/``/g, '“').replace(/''/g, '”')
    .replace(/~/g, ' ')
    .replace(/(?<!\\)[{}]/g, '')
    .normalize('NFC')
}

// Plain words for a label (aria, title attributes): text-mode TeX typeset, each formula as KaTeX's MathML words.
export function plainMath(input = '') {
  let result = ''
  let position = 0
  for (const match of input.matchAll(MATH)) {
    const tex = match[1] || match[2] || match[3] || match[4]
    result += texText(input.slice(position, match.index))
    result += mathSpeech(katex.renderToString(tex, { throwOnError: false, trust: false, strict: false }))
    position = match.index + match[0].length
  }
  return (result + texText(input.slice(position))).replace(/\s+/g, ' ').trim()
}

export function renderMath(input = '', options = {}) {
  let result = ''
  let position = 0
  const text = options.texText ? texText : value => value
  for (const match of input.matchAll(options.money ? MATH_WITH_MONEY : MATH)) {
    const display = Boolean(match[1] || match[2])
    const tex = match[1] || match[2] || match[3] || match[4]
    result += escapeHtml(text(input.slice(position, match.index)))
    const html = katex.renderToString(!display && options.keepTogether ? keepTogether(tex) : tex, {
      displayMode: display,
      throwOnError: false,
      trust: false,
      strict: false,
    })
    result += options.speech ? `<span aria-hidden="true">${html}</span><span class="math-speech">${escapeHtml(mathSpeech(html))}</span>` : html
    position = match.index + match[0].length
  }
  return result + escapeHtml(text(input.slice(position)))
}
