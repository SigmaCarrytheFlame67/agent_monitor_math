/* Shared console prose: the same Markdown/math presentation as the console,
 * with an allowlist before any untrusted content reaches the document. */
'use strict';
(() => {
  const allowed = new Set([
    'A', 'BLOCKQUOTE', 'BR', 'CODE', 'DEL', 'EM', 'H1', 'H2', 'H3', 'H4',
    'H5', 'H6', 'HR', 'LI', 'OL', 'P', 'PRE', 'STRONG', 'TABLE', 'TBODY',
    'TD', 'TFOOT', 'TH', 'THEAD', 'TR', 'UL',
  ]);
  const escape = value => String(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));

  function safeHref(value) {
    try {
      if (!value) return null;
      const url = new URL(value, location.href);
      return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function sanitize(html) {
    const template = document.createElement('template');
    template.innerHTML = String(html);
    function scrub(element) {
      [...element.children].forEach(scrub);
      if (!allowed.has(element.tagName)) {
        element.replaceWith(document.createTextNode(element.textContent || ''));
        return;
      }
      const href = element.tagName === 'A' ? safeHref(element.getAttribute('href')) : null;
      const language = element.tagName === 'CODE' ? element.getAttribute('class') || '' : '';
      [...element.attributes].forEach(attribute => element.removeAttribute(attribute.name));
      if (href) {
        element.href = href;
        element.rel = 'noopener noreferrer';
        element.referrerPolicy = 'no-referrer';
      }
      if (/^language-[A-Za-z0-9_-]+$/.test(language)) element.className = language;
    }
    [...template.content.children].forEach(scrub);
    return template.content;
  }

  const environments = ['equation', 'equation*', 'align', 'align*', 'alignat', 'alignat*', 'gather', 'gather*'];
  const lineBreaks = '\n\r\u2028\u2029';

  /* Math is found by scanners that resume where they stopped, so a text costs
   * linear time. The regular expressions they stand for (quoted below) took
   * quadratic time on unclosed \[ or \( and exponential time on a "$" before
   * a long run of backslashes; on any text the scanners find the same math. */
  // The first `needle` at or after a position, for positions that only grow.
  function nextIndex(source, needle) {
    let found = -2;
    return from => {
      if (found !== -1 && found < from) found = source.indexOf(needle, from);
      return found;
    };
  }
  // `open`…`close` with at least `inside` characters between, closed as early
  // as possible: /\\\[[\s\S]+?\\\]/ has `inside` 1, /\\\[[\s\S]*?\\\]/ 0.
  function between(source, open, close, inside) {
    const opener = nextIndex(source, open), closer = nextIndex(source, close);
    return from => {
      const start = opener(from);
      if (start < 0) return null;
      const end = closer(start + open.length + inside);
      return end < 0 ? null : [start, end + close.length];
    };
  }
  // A display environment closed by its own \end:
  // /\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}[\s\S]*?\\end\{\1\}/
  function environment(source) {
    const opener = /\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}/g, closers = new Map();
    return from => {
      opener.lastIndex = from;
      for (let match; (match = opener.exec(source));) {
        const close = '\\end{' + match[1] + '}';
        if (!closers.has(close)) closers.set(close, nextIndex(source, close));
        const end = closers.get(close)(opener.lastIndex);
        if (end >= 0) return [match.index, end + close.length];
      }
      return null;
    };
  }
  // /\$(?:\\.|[^$\\])+\$/: a "$" closes it unless a backslash escapes it; a
  // backslash takes the next character with it (never a line break). There is
  // one reading only, so where each position leads is kept for every later
  // "$" (an escaped one inside a formula) that reaches it.
  function escapedDollar(source) {
    const closing = new Int32Array(source.length + 1).fill(-2);
    // The closing "$" reached from `from`, after the first character; -1 if none.
    const close = from => {
      const path = [];
      let at = from, end = -1;
      for (;;) {
        if (closing[at] !== -2) { end = closing[at]; break; }
        path.push(at);
        if (source[at] === '$') { end = at; break; }
        if (source[at] === '\\') {
          if (at + 1 < source.length && !lineBreaks.includes(source[at + 1])) { at += 2; continue; }
          break;
        }
        if (at >= source.length) break;
        at += 1;
      }
      for (const position of path) closing[position] = end;
      return end;
    };
    return from => {
      for (let start = source.indexOf('$', from); start >= 0; start = source.indexOf('$', start + 1)) {
        const first = source[start + 1];
        if (first === undefined || first === '$') continue;
        if (first === '\\' && (start + 2 >= source.length || lineBreaks.includes(source[start + 2]))) continue;
        const end = close(start + (first === '\\' ? 3 : 2));
        if (end >= 0) return [start, end + 1];
      }
      return null;
    };
  }
  // The math the `kinds` find, as one regular expression with those
  // alternatives finds it: from each position on, the match that starts
  // first (the earlier kind on a tie), then on from its end.
  function mathSpans(source, kinds) {
    const spans = [], found = kinds.map(() => null);
    for (let at = 0; ;) {
      let best = -1;
      kinds.forEach((kind, index) => {
        if (found[index] && found[index][0] < at) found[index] = null;
        if (found[index] === null) found[index] = kind(at) || false;
        if (found[index] && (best < 0 || found[index][0] < found[best][0])) best = index;
      });
      if (best < 0) return spans;
      spans.push(found[best]);
      at = found[best][1];
    }
  }
  function holdSpans(source, kind, hold) {
    let result = '', at = 0;
    for (const [start, end] of mathSpans(source, [kind])) {
      result += source.slice(at, start) + hold(source.slice(start, end));
      at = end;
    }
    return result + source.slice(at);
  }
  // /(^|[^\\$])(\$(?!\$)(?:\\.|[^$\n])+\$)/g: a "$" that starts the text or
  // follows a character other than \ and $ (that character stays text), and
  // is not followed by another "$".
  function holdInlineMath(source, hold) {
    let result = '', at = 0, dead = null;
    for (let open = source.indexOf('$'); open >= 0; open = source.indexOf('$', open + 1)) {
      if (open > 0 && (open - 1 < at || source[open - 1] === '\\' || source[open - 1] === '$')) continue;
      dead = dead || new Uint8Array(source.length + 1);
      const end = inlineMathEnd(source, open, dead);
      if (end < 0) continue;
      result += source.slice(at, open) + hold(source.slice(open, end + 1));
      at = end + 1;
      open = end;
    }
    return result + source.slice(at);
  }
  // The "$" that (?:\\.|[^$\n])+\$ closes the formula at `open` with: a
  // backslash either takes the next character with it (never a line break)
  // or stands alone, and the pattern backtracks through both readings,
  // escape first. This walks the same choices depth first, remembering each
  // position from which no "$" can be reached (`dead`, kept for the whole
  // text), so no position is explored twice. -1 when there is none.
  function inlineMathEnd(source, open, dead) {
    const first = open + 1;
    if (first >= source.length || source[first] === '$' || source[first] === '\n') return -1;
    const stack = [first], tried = [0];
    while (stack.length) {
      const top = stack.length - 1, at = stack[top];
      if (source[at] === '$') return at;
      let next = -1;
      if (tried[top] === 0) {
        tried[top] = 1;
        if (source[at] === '\\' && at + 1 < source.length && !lineBreaks.includes(source[at + 1])) next = at + 2;
      }
      if (next < 0 && tried[top] === 1) {
        tried[top] = 2;
        if (at < source.length && source[at] !== '\n') next = at + 1;
      }
      if (next < 0) { dead[at] = 1; stack.pop(); tried.pop(); }
      else if (!dead[next]) { stack.push(next); tried.push(0); }
    }
    return -1;
  }

  // Manuscript statements retain their notation. Remove only presentation
  // markup, and leave unresolved reference keys visible rather than inventing
  // equation numbers or author macros absent from the saved snapshot.
  function manuscriptProse(value) {
    const source = String(value || '');
    let end = 0, result = '';
    const prose = text => text
      .replace(/\[(?:\d*\.)?\d+(?:em|ex|pt)\]\s*\\(?:large|Large|LARGE|small|normalsize)\b/g, ' ')
      .replace(/\\(?:large|Large|LARGE|small|normalsize)\b/g, '')
      .replace(/\\(?:[cC]ref|autoref|nameref|ref)\*?\{([^{}]{1,200})\}/g, '$1')
      .replace(/\\eqref\*?\{([^{}]{1,200})\}/g, '($1)')
      .replace(/\\(?:emph|textbf|textit|textsc|textrm|textsf|texttt|text|mbox)\{([^{}]*)\}/g, '$1')
      .replace(/\\cite[pt]?\*?(?:\[[^\]]*\])*\{([^{}]{1,200})\}/g, '[$1]')
      .replace(/~/g, ' ').replace(/\\\\(?:\[[^\]]*\])?/g, ' ');
    // The math of /(?:<environment>|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?:\\.|[^$\\])+\$)/g.
    const kinds = [environment(source), between(source, '$$', '$$', 0), between(source, '\\[', '\\]', 0),
      between(source, '\\(', '\\)', 0), escapedDollar(source)];
    for (const [start, stop] of mathSpans(source, kinds)) {
      result += prose(source.slice(end, start));
      result += source.slice(start, stop).replace(/\\(begin|end)\{(equation|align|alignat|gather)\}/g, '\\$1{$2*}');
      end = stop;
    }
    return result + prose(source.slice(end));
  }

  // proof.md of a CGM run published before closure rendering v2 numbers its
  // lemmas in topological order, while each proof cites the worker's labels
  // ("Cited facts: L1 = fact …"). On screen only, name the lemma each label
  // stands for; the saved document keeps its attested bytes.
  function closureCitedLemmas(value) {
    const source = String(value || '');
    if (!source.startsWith('# Proof\n\nThis proof is the verified fact closure')) return source;
    const numbers = new Map([...source.matchAll(/^## Lemma (\d+) · fact ([0-9a-f]{16})$/gm)].map(match => [match[2], match[1]]));
    if (!numbers.size) return source;
    return source.replace(/^Cited facts: (.+)\.$/gm, (line, list) => 'Cited facts: ' + list.split('; ').map(item => {
      const cite = /^(\S+) = fact ([0-9a-f]{16})$/.exec(item);
      return cite && numbers.has(cite[2]) ? `${cite[1]} = Lemma ${numbers.get(cite[2])} (fact ${cite[2]})` : item;
    }).join('; ') + '.');
  }

  // Commands KaTeX 0.16.11 does not know, as the nearest it does (the
  // console's list), so a write-up's $\qed$ or a manuscript's \abs{x} is
  // typeset rather than shown as an error. Under the author's macros.
  const fallbackMacros = Object.freeze({
    '\\qed': '\\square', '\\qedhere': '', '\\mathbbm': '\\mathbb', '\\mathds': '\\mathbb', '\\symbb': '\\mathbb',
    '\\divides': '\\mid', '\\textsc': '\\text',
    '\\abs': '\\left\\lvert #1\\right\\rvert', '\\norm': '\\left\\lVert #1\\right\\rVert',
    '\\ceil': '\\left\\lceil #1\\right\\rceil', '\\floor': '\\left\\lfloor #1\\right\\rfloor',
    '\\ref': '\\text{#1}', '\\cref': '\\text{#1}', '\\Cref': '\\text{#1}', '\\autoref': '\\text{#1}',
    '\\eqref': '(\\text{#1})', '\\cite': '[\\text{#1}]',
    // A label is dropped: its argument is taken and nothing is written.
    '\\label': '\\@firstoftwo{}{#1}',
  });
  // The key of \ref, \eqref, \cref, \Cref, \autoref or \cite as those
  // stand-ins print it, in text: its _ # & % ^ $ escaped (a key written with a
  // backslash is left as it is), and \cite[note]{key} as \cite{key, note}.
  // Every KaTeX call passes its TeX through this first.
  function katexReferenceText(tex) {
    const escapeKey = key => key.includes('\\') ? key : key.replace(/[_#&%$]/g, '\\$&').replace(/\^/g, '\\textasciicircum{}');
    return String(tex).replace(/\\(ref|eqref|cref|Cref|autoref|cite)(?:\[([^[\]{}]*)\])?\{([^{}]*)\}/g, (whole, name, note, key) => {
      if (note === undefined) return '\\' + name + '{' + escapeKey(key) + '}';
      return name === 'cite' ? '\\cite{' + escapeKey(key) + ', ' + escapeKey(note) + '}' : whole;
    });
  }
  // The macros for typesetting `text`: the fallbacks, then the author's. KaTeX
  // refuses \newcommand for a name it knows, so a fallback is left out where
  // the text defines that name itself.
  function withKatexFallbacks(author, text) {
    const macros = { ...fallbackMacros };
    for (const match of String(text || '').matchAll(/\\newcommand\*?\s*(?:\{\s*)?(\\[A-Za-z]+)/g)) delete macros[match[1]];
    return Object.assign(macros, countedAuthorMacros(author, text));
  }
  // KaTeX's maxExpand counts expansions, not what they write: three preamble
  // macros of a few hundred characters can make one short formula typeset
  // hundreds of thousands of symbols and hold the page for minutes. So what an
  // author's macros write is counted as KaTeX expands them (the console's
  // rule): in one call at most 400 characters plus 2 for each character of
  // `text` (what is to be typeset), and 8,000 in one formula (each formula
  // has an expander of its own); past that KaTeX shows the formula as
  // written. A macro that writes one of its arguments more than once is left
  // out: KaTeX copies the argument after this count.
  function countedAuthorMacros(author, text) {
    const counted = {};
    if (!author || typeof author !== 'object') return counted;
    let room = 400 + 2 * String(text || '').length, formula = null, written = 0;
    for (const [name, body] of Object.entries(author)) {
      const uses = typeof body === 'string' ? body.replace(/##/g, '').match(/#[1-9]/g) || [] : null;
      if (!uses || new Set(uses).size < uses.length) continue;
      counted[name] = expander => {
        if (expander !== formula) { formula = expander; written = 0; }
        room -= body.length;
        written += body.length;
        if (room < 0 || written > 8000) throw new window.katex.ParseError('the author’s macros write too much here');
        return body;
      };
    }
    return counted;
  }

  // The text of `target` that KaTeX has yet to typeset: formulas it typeset
  // before (a readable version's notation symbols) are left out.
  function untypeset(target) {
    if (typeof target.querySelector !== 'function' || !target.querySelector('.katex')) return String(target.textContent || '');
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    let source = '';
    for (let item; (item = walker.nextNode());) if (!item.parentElement?.closest('.katex')) source += item.data;
    return source;
  }

  function math(target, options = {}) {
    if (typeof window.renderMathInElement !== 'function') return;
    if (options.manuscript) {
      const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
      let item;
      while ((item = walker.nextNode())) {
        if (!item.parentElement?.closest('pre,code,.katex')) item.textContent = manuscriptProse(item.textContent);
      }
    }
    window.renderMathInElement(target, {
      macros: withKatexFallbacks(options.macros, untypeset(target)),
      preProcess: katexReferenceText,
      delimiters: [
        ...environments.map(name => ({ left: '\\begin{' + name + '}', right: '\\end{' + name + '}', display: true })),
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false },
      ],
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'option'],
      throwOnError: false,
      errorColor: '#64748b',
      trust: false,
      strict: 'ignore',
      maxSize: 20,
      // The console's limit where an author's macros apply.
      maxExpand: options.macros && typeof options.macros === 'object' ? 200 : 1000,
      errorCallback: () => {},
    });
  }

  function render(target, value, options = {}) {
    const original = options.manuscript ? manuscriptProse(value) : closureCitedLemmas(value);
    const slots = [];
    const hold = expression => {
      slots.push(expression);
      return '@@ANSATZE_MATH_SLOT_' + (slots.length - 1) + '@@';
    };
    // Environments, then $$…$$, \[…\], \(…\) and $…$, each pass over the last.
    let source = holdSpans(original, environment(original), hold);
    source = holdSpans(source, between(source, '$$', '$$', 1), hold);
    source = holdSpans(source, between(source, '\\[', '\\]', 1), hold);
    source = holdSpans(source, between(source, '\\(', '\\)', 1), hold);
    source = holdInlineMath(source, hold);
    let html;
    try {
      html = window.marked?.parse(source, { gfm: true, breaks: false });
    } catch { /* Plain text remains readable when parsing fails. */ }
    if (typeof html !== 'string') html = '<pre>' + escape(original) + '</pre>';
    else html = html.replace(/@@ANSATZE_MATH_SLOT_(\d+)@@/g,
      (_, index) => escape(slots[Number(index)] || ''));
    target.replaceChildren(sanitize(html));
    target.classList.add('md-view');
    math(target, { macros: options.macros });
  }

  window.AnsatzSharedText = Object.freeze({ render, math, manuscriptProse, closureCitedLemmas, withKatexFallbacks, katexReferenceText });
})();
