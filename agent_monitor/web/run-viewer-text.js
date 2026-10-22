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
  const mathPattern = /(?:\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}[\s\S]*?\\end\{\1\}|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$(?:\\.|[^$\\])+\$)/g;

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
    for (const match of source.matchAll(mathPattern)) {
      result += prose(source.slice(end, match.index));
      result += match[0].replace(/\\(begin|end)\{(equation|align|alignat|gather)\}/g, '\\$1{$2*}');
      end = match.index + match[0].length;
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
      maxExpand: 1000,
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
    let source = original.replace(/\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?)\}[\s\S]*?\\end\{\1\}/g, hold)
      .replace(/\$\$[\s\S]+?\$\$/g, hold)
      .replace(/\\\[[\s\S]+?\\\]/g, hold)
      .replace(/\\\([\s\S]+?\\\)/g, hold)
      .replace(/(^|[^\\$])(\$(?!\$)(?:\\.|[^$\n])+\$)/g,
        (_, prefix, expression) => prefix + hold(expression));
    let html;
    try {
      html = window.marked?.parse(source, { gfm: true, breaks: false });
    } catch { /* Plain text remains readable when parsing fails. */ }
    if (typeof html !== 'string') html = '<pre>' + escape(original) + '</pre>';
    else html = html.replace(/@@ANSATZE_MATH_SLOT_(\d+)@@/g,
      (_, index) => escape(slots[Number(index)] || ''));
    target.replaceChildren(sanitize(html));
    target.classList.add('md-view');
    math(target);
  }

  window.AnsatzSharedText = Object.freeze({ render, math, manuscriptProse, closureCitedLemmas });
})();
