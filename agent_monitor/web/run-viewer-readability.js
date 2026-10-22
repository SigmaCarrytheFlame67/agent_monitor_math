/* A shared readable version (snapshot kind "readability"): the readable text
 * with its result anchors, the notation table and the paper map as a list.
 * Read-only and static (the shared page's CSP allows only its own files);
 * model and manuscript text is set as text, or rendered by AnsatzSharedText. */
'use strict';
(() => {
  const text = value => value == null ? '' : String(value);
  const isObject = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = text(value);
    return element;
  };
  // The readable text is the snapshot's "proof"; its paper map and notation
  // are in the "readability" block.
  function parts(data) {
    const block = isObject(data?.readability) ? data.readability : {};
    const notation = isObject(block.notation) ? block.notation : {}, map = isObject(block.paper_map) ? block.paper_map : {};
    return {
      readable: text(data?.proof),
      rows: (Array.isArray(notation.rows) ? notation.rows : []).filter(isObject).slice(0, 300),
      terms: (Array.isArray(notation.terms) ? notation.terms : []).filter(isObject).slice(0, 200),
      nodes: (Array.isArray(map.nodes) ? map.nodes : []).filter(isObject).slice(0, 400),
      edges: Array.isArray(map.edges) ? map.edges.length : 0,
      macros: katexMacros(map.macros),
    };
  }
  // Author macros as the console filters them: plain names, bounded bodies,
  // nothing that defines, links or includes.
  function katexMacros(raw) {
    const out = {};
    if (!isObject(raw)) return out;
    Object.entries(raw).slice(0, 400).forEach(([name, value]) => {
      if (!/^\\[A-Za-z]{1,40}$/.test(name) || typeof value !== 'string' || value.length > 400) return;
      if (/\\(?:href|url|includegraphics|html[A-Za-z]*|def|gdef|edef|xdef|let|newcommand|renewcommand)\b/.test(value)) return;
      out[name] = value;
    });
    return out;
  }
  // A result marker ("[result:<id>]: #", which Markdown renders as nothing)
  // becomes a line of its own that the anchors are placed on after rendering.
  function withAnchorTokens(readable) {
    return text(readable).replace(/\r\n?/g, '\n')
      .replace(/^[ \t]{0,3}\[result:([a-z0-9][a-z0-9:_-]{0,79})\]:[ \t]*#[ \t]*$/gim, (_, id) => '\n⟦result:' + id.toLowerCase() + '⟧\n');
  }
  function importance(entry) {
    const value = Math.round(Number(entry?.importance));
    return value >= 1 && value <= 5 ? value : 1;
  }
  function sectionLabel(section) {
    if (Array.isArray(section)) {
      const top = section.find(isObject);
      return top ? [top.number, top.title].map(value => text(value).trim()).filter(Boolean).join(' ').slice(0, 160) : '';
    }
    return text(section).trim().slice(0, 160);
  }
  function title(entry) {
    const label = text(entry?.label || entry?.id).trim().slice(0, 160), name = text(entry?.title).trim().slice(0, 200);
    return name && name !== label ? label + ' · ' + name : label;
  }
  function tex(element, value, macros) {
    element.textContent = value;
    if (!value || typeof window.katex?.render !== 'function') return;
    const all = window.AnsatzSharedText?.withKatexFallbacks?.(macros, value) || { ...macros };
    try { window.katex.render(value, element, { throwOnError: false, displayMode: false, trust: false, macros: all, maxExpand: 200 }); }
    catch { element.textContent = value; }
  }
  function placeAnchors(target, labels) {
    for (const paragraph of [...target.querySelectorAll('p')]) {
      const match = /^⟦result:([a-z0-9][a-z0-9:_-]{0,79})⟧$/.exec(paragraph.textContent.trim());
      if (!match) continue;
      const badge = node('span', 'rv-anchor', labels.get(match[1]) || match[1]);
      badge.dataset.anchor = match[1]; badge.tabIndex = -1;
      const row = node('p', 'rv-anchor-row'); row.append(badge);
      paragraph.replaceWith(row);
    }
  }
  // The provenance note under the title, and "⚠ Check" notes, look like what they are.
  function markBlocks(target) {
    const first = target.firstElementChild, second = first?.nextElementSibling;
    if (first?.tagName === 'H1' && second?.tagName === 'BLOCKQUOTE') second.classList.add('rv-provenance');
    target.querySelectorAll('blockquote').forEach(quote => { if (/^⚠/.test(quote.textContent.trim())) quote.classList.add('rv-check'); });
  }
  function table(headings) {
    const element = node('table', 'rv-rd-table'), head = node('thead'), row = node('tr'), body = node('tbody');
    headings.forEach(heading => { const cell = node('th', '', heading); cell.scope = 'col'; row.append(cell); });
    head.append(row); element.append(head, body);
    return { element, body };
  }
  function notationSection(view) {
    const section = node('section', 'rv-rd-section rv-markdown'); section.id = 'rv-notation';
    section.append(node('h2', '', 'Notation'));
    if (!view.rows.length && !view.terms.length) {
      section.append(node('p', 'rv-muted', 'No notation table was included in this snapshot.'));
      return section;
    }
    if (view.rows.length) {
      const { element, body } = table(['Symbol', 'Meaning', 'Where introduced']);
      for (const row of view.rows) {
        const line = node('tr'), symbol = node('td', 'rv-rd-symbol');
        tex(symbol, text(row.symbol).slice(0, 200), view.macros);
        line.append(symbol, node('td', '', text(row.meaning).slice(0, 600)), node('td', '', text(row.where).slice(0, 200)));
        body.append(line);
      }
      section.append(element);
    }
    if (view.terms.length) {
      const { element, body } = table(['Term', 'Meaning', 'Where introduced']);
      for (const term of view.terms) {
        const line = node('tr');
        line.append(node('td', 'rv-rd-term', text(term.term).slice(0, 200)), node('td', '', text(term.meaning).slice(0, 600)), node('td', '', text(term.where).slice(0, 200)));
        body.append(line);
      }
      section.append(node('h3', '', 'Terms'), element);
    }
    section.append(node('p', 'rv-muted', 'Collected by a model from the manuscript — check a meaning against the original when it matters.'));
    return section;
  }
  function mapSection(view, readable) {
    const section = node('section', 'rv-rd-section rv-markdown'); section.id = 'rv-paper-map';
    section.append(node('h2', '', 'Paper map'));
    if (!view.nodes.length) {
      section.append(node('p', 'rv-muted', 'No paper map was included in this snapshot.'));
      return section;
    }
    section.append(node('p', 'rv-muted', `${view.nodes.length} statement${view.nodes.length === 1 ? '' : 's'} · ${view.edges} ${view.edges === 1 ? 'dependency' : 'dependencies'} · more dots, more central to the main argument.`));
    const groups = new Map();
    for (const entry of view.nodes) {
      const name = sectionLabel(entry.section) || 'Other results';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(entry);
    }
    groups.forEach((entries, name) => {
      const list = node('ul', 'rv-rd-map');
      for (const entry of entries) {
        const level = importance(entry), row = node('li', 'rv-rd-row imp-' + level);
        const dots = node('span', 'rv-rd-imp', '●'.repeat(level) + '○'.repeat(5 - level));
        dots.setAttribute('role', 'img'); dots.setAttribute('aria-label', `Importance ${level} of 5`);
        const body = node('div'), label = node('strong', '', title(entry));
        if (entry.main === true) label.append(' ', node('span', 'rv-rd-main', 'main result'));
        body.append(label);
        if (text(entry.role).trim()) body.append(node('p', '', text(entry.role).slice(0, 400)));
        const id = text(entry.id);
        const anchor = [...readable.querySelectorAll('.rv-anchor')].find(element => element.dataset.anchor === id);
        row.append(dots, body);
        if (anchor) {
          const read = node('button', 'rv-small-button', 'Read'); read.type = 'button';
          read.addEventListener('click', () => { anchor.scrollIntoView({ block: 'center', behavior: 'smooth' }); anchor.focus({ preventScroll: true }); });
          row.append(read);
        }
        list.append(row);
      }
      section.append(node('h3', '', name), list);
    });
    section.append(node('p', 'rv-muted', 'Importance and roles come from a reading guide written by a model — not a verification.'));
    return section;
  }
  // Renders the readable version into the proof document, with its notation
  // and paper map after it; returns the snapshot's parts.
  function render(data, { target, renderText }) {
    const view = parts(data);
    const labels = new Map(view.nodes.map(entry => [text(entry.id), text(entry.label || entry.id).slice(0, 160)]));
    if (view.readable.trim()) {
      renderText(target, withAnchorTokens(view.readable));
      placeAnchors(target, labels);
      markBlocks(target);
    }
    const sections = [notationSection(view), mapSection(view, target)];
    target.after(...sections);
    // Meanings, titles and roles may hold inline math (the symbols are typeset already).
    sections.forEach(section => window.AnsatzSharedText?.math?.(section, { macros: view.macros }));
    return view;
  }
  const api = { render, parts, katexMacros, withAnchorTokens, importance, sectionLabel };
  if (typeof window !== 'undefined') window.AnsatzSharedReadability = Object.freeze(api);
  if (typeof module === 'object' && module.exports) module.exports = api;
})();
