/* Read-only snapshot client. Navigation, graph inspection and copying stay local. */
'use strict';
(async () => {
  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const text = value => value == null ? '' : String(value);
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = text(value);
    return element;
  };
  const renderText = (target, value) => {
    if (window.AnsatzSharedText?.render) window.AnsatzSharedText.render(target, text(value), { manuscript: analysis });
    else { target.textContent = text(value); target.classList.add('rv-plain-text'); }
  };
  const statusLabel = value => text(value).replace(/_/g, ' ').replace(/^./, ch => ch.toUpperCase());
  const empty = (target, title, message, symbol = '◇') => {
    const box = node('div', 'rv-empty');
    const icon = node('span', 'rv-empty-icon', symbol); icon.setAttribute('aria-hidden', 'true');
    box.append(icon, node('h3', '', title), node('p', '', message));
    target.replaceChildren(box);
  };
  let data, analysis = false, readable = false, activeTab = 'sandbox', graphMode = 'engine', engineMode = 'explore', graphInstance;
  const graphLabels = { outline: 'Outline', explore: 'Explore', facts: 'Facts', formal: 'Formal · Lean', compiler: 'Compiler facts', manuscript: 'Manuscript' };
  const graphDescriptions = {
    outline: 'An informal outline of the saved proof and its dependencies.',
    explore: 'Plans, attempts, verifier feedback, and curation in the saved exploration record. These edges describe the search process.',
    facts: 'Saved facts and their dependencies. Each node shows its recorded model-verifier acceptance; this is not a Lean certificate.',
    formal: 'The structural dependency graph of the saved Lean declarations.',
    compiler: 'Compiler-observed proof-body uses and type mentions. These observations are not a logical proof certificate.',
    manuscript: 'The manuscript’s results and references, with model-verifier verdicts. Model checks are not proof certificates.',
  };
  const legacyReasons = {
    explore: 'This older snapshot does not contain the exploration record. The owner can update the shared snapshot from the console to include it.',
    facts: 'This older snapshot does not contain the CGM Facts graph. The owner can update the shared snapshot from the console to include it.',
    compiler: 'Compiler facts were not included in this saved snapshot.',
    outline: 'No informal outline was included in this saved snapshot.',
    formal: 'No structural Lean graph was included in this saved snapshot.',
    manuscript: 'No manuscript graph was included in this saved snapshot.',
  };
  function graphFor(mode) {
    if (data?.graphs && Object.prototype.hasOwnProperty.call(data.graphs, mode)) return data.graphs[mode];
    const old = mode === 'formal' ? data?.formal_graph : ((analysis && mode === 'manuscript') || (!analysis && mode === 'outline')) ? data?.informal_graph : null;
    if (old?.nodes?.length) return { kind: mode, status: 'ready', graph_role: mode === 'outline' ? 'outline' : mode, graph: old };
    return { kind: mode, status: 'unavailable', graph_role: mode, graph: { nodes: [], edges: [] }, reason: legacyReasons[mode] };
  }
  function graphNodes(mode) {
    const envelope = graphFor(mode);
    return Array.isArray(envelope?.graph?.nodes) ? envelope.graph.nodes : [];
  }
  function selectedMode() { return graphMode === 'engine' ? engineMode : graphMode; }
  function showGraph() {
    if (!data) return;
    const mode = selectedMode();
    $('rv-engine-modes').hidden = graphMode !== 'engine';
    const execution = graphFor(mode)?.graph_role === 'execution';
    $('rv-graph-title').textContent = graphMode === 'engine' ? (execution ? 'Engine trace' : 'Informal · CGM') : graphLabels[mode];
    $('rv-graph-description').textContent = execution ? 'Recorded engine activity and its execution sequence. These events are not accepted CGM facts or proof dependencies.' : graphDescriptions[mode];
    all('[data-graph-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.graphMode === graphMode)));
    all('[data-engine-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.engineMode === engineMode)));
    graphInstance?.destroy?.();
    graphInstance = null;
    const container = $('rv-graph-workspace');
    container.replaceChildren();
    if (window.AnsatzSharedGraphs?.mount) {
      graphInstance = window.AnsatzSharedGraphs.mount(container, { graph: graphFor(mode), mode, renderText });
    } else {
      const graph = graphFor(mode);
      empty(container, `${graphLabels[mode]} unavailable`, graph.reason || 'The graph viewer could not be loaded. Reload the page to try again.');
    }
  }
  function tab(name, focus = false) {
    if (!['sandbox', 'lean', 'monitor', 'dag'].includes(name)) return;
    // A readable version has no Lean and no graph tabs: its paper map is in the Sandbox.
    if (readable && (name === 'lean' || name === 'dag')) name = 'sandbox';
    activeTab = name;
    all('[data-tab]').forEach(button => {
      const selected = button.dataset.tab === name;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
    });
    for (const view of ['sandbox', 'lean', 'monitor', 'dag']) $('rv-' + view).hidden = view !== name;
    if (name === 'dag') showGraph();
  }
  function openGraph(mode) {
    if (mode === 'explore' || mode === 'facts') { graphMode = 'engine'; engineMode = mode; }
    else graphMode = mode;
    tab('dag');
  }
  all('[data-tab]').forEach(button => {
    button.addEventListener('click', () => tab(button.dataset.tab));
    button.addEventListener('keydown', event => {
      const buttons = all('[data-tab]');
      const index = buttons.indexOf(button);
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
      if (event.key === 'ArrowLeft') next = (index + buttons.length - 1) % buttons.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = buttons.length - 1;
      if (next != null) { event.preventDefault(); tab(buttons[next].dataset.tab, true); }
    });
  });
  all('[data-graph-mode]').forEach(button => button.addEventListener('click', () => { graphMode = button.dataset.graphMode; showGraph(); }));
  all('[data-engine-mode]').forEach(button => button.addEventListener('click', () => { engineMode = button.dataset.engineMode; showGraph(); }));
  all('[data-open-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.openTab)));
  all('[data-open-graph]').forEach(button => button.addEventListener('click', () => openGraph(button.dataset.openGraph)));
  $('rv-theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const label = 'Switch to ' + (dark ? 'light' : 'dark') + ' theme';
    $('rv-theme').setAttribute('aria-label', label);
    $('rv-theme').title = label;
  });
  let toastTimer;
  function toast(message) {
    clearTimeout(toastTimer); $('rv-toast').textContent = message; $('rv-toast').hidden = false;
    toastTimer = setTimeout(() => { $('rv-toast').hidden = true; }, 2600);
  }
  $('rv-copy-lean').addEventListener('click', async () => {
    if (!data?.lean_source) return;
    try { await navigator.clipboard.writeText(data.lean_source); toast('Lean source copied'); }
    catch { toast('Copy is unavailable in this browser. Select the source to copy it.'); }
  });
  try {
    const snapshotUrl = document.body.dataset.snapshotUrl;
    // Do not retain the capability-bearing endpoint in the rendered document.
    delete document.body.dataset.snapshotUrl;
    const response = await fetch(snapshotUrl, { credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer' });
    if (!response.ok) throw new Error('unavailable');
    data = await response.json();
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('unavailable');
  } catch {
    $('rv-notice').textContent = 'This shared run is unavailable. The owner may have revoked or replaced the link.';
    $('rv-notice').dataset.error = 'true';
    $('rv-status').textContent = 'Unavailable';
    $('rv-workspace').setAttribute('aria-busy', 'false');
    $('rv-problem-disclosure').hidden = true;
    empty($('rv-proof-text'), 'Shared run unavailable', 'Check the link or ask the owner for an updated shared snapshot.');
    $('rv-run-facts').hidden = true;
    all('.rv-sidebar-section').forEach(section => { section.hidden = true; });
    all('[data-tab]').forEach(button => { button.disabled = true; });
    return;
  }
  analysis = data.kind === 'analysis';
  readable = data.kind === 'readability';
  const originalTitle = data.title || (analysis ? 'Manuscript analysis' : readable ? 'Readable version' : 'Shared research run');
  const title = analysis && window.AnsatzSharedText?.manuscriptProse ? window.AnsatzSharedText.manuscriptProse(originalTitle) : originalTitle;
  document.title = title + ' · Ansätze';
  $('rv-title').textContent = title; $('rv-title').title = title;
  $('rv-status').textContent = statusLabel(data.status) || 'Saved run';
  $('rv-notice').textContent = data.notice || 'Read-only snapshot · browse the saved proof, activity, and graph views.';
  if (analysis) {
    $('rv-proof-title').textContent = 'Analysis report';
    $('rv-problem-label').textContent = 'Manuscript';
    $('rv-lean-title').textContent = 'Lean statements';
    $('rv-tab-lean').textContent = 'Lean statements';
    all('[data-graph-mode="manuscript"]').forEach(button => { button.hidden = false; });
    graphMode = 'manuscript';
  }
  if (readable) {
    $('rv-proof-title').textContent = 'Readable version';
    $('rv-problem-label').textContent = 'Manuscript';
    $('rv-tab-lean').hidden = true; $('rv-tab-dag').hidden = true;
  }
  renderText($('rv-problem'), data.problem || 'The original task was not included in this snapshot.');
  // A readable version brings its notation and paper map after the text.
  const readability = readable && window.AnsatzSharedReadability ? window.AnsatzSharedReadability.render(data, { target: $('rv-proof-text'), renderText }) : null;
  if (readability) {
    if (!readability.readable.trim()) empty($('rv-proof-text'), 'No saved readable version', 'The readable version was not included in this snapshot.', '∎');
  } else if (data.proof) renderText($('rv-proof-text'), data.proof);
  else empty($('rv-proof-text'), analysis ? 'No saved analysis report' : 'No saved proof', analysis ? 'The report was not included in this snapshot.' : 'The proof document was not included in this snapshot.', '∎');
  try {
    const source = new URL(data.source_url);
    if (source.protocol === 'https:' && !/\/shared\//i.test(source.pathname)) {
      $('rv-source').href = source.href; $('rv-source').hidden = false;
      $('rv-source').textContent = analysis ? 'Source manuscript ↗' : 'Source problem ↗';
    }
  } catch { /* An absent or invalid source link is simply omitted. */ }
  const agents = Array.isArray(data.agents) ? data.agents.filter(agent => agent && typeof agent === 'object') : [];
  const fact = (label, value) => { $('rv-run-facts').append(node('dt', '', label), node('dd', '', value)); };
  fact('Run type', analysis ? 'Analyze' : readable ? 'Readability' : 'Prove');
  fact('Status', statusLabel(data.status) || 'Saved');
  fact('Research steps', String(agents.length));
  if (readability) fact('Paper map', `${readability.nodes.length} definitions and results`);
  else fact('Graph views', `${Object.keys(graphLabels).filter(mode => graphNodes(mode).length).length} included`);
  // A readable version's views are sections of its Sandbox.
  for (const [id, label] of readability ? [['rv-notation', 'Notation'], ['rv-paper-map', 'Paper map']] : []) {
    const button = node('button', 'rv-view-shortcut'); button.type = 'button';
    button.append(node('strong', '', label), node('small', '', 'Below the readable version'));
    button.addEventListener('click', () => { tab('sandbox'); $(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' }); });
    $('rv-view-shortcuts').append(button);
  }
  const shortcutModes = readable ? [] : analysis ? ['manuscript', 'outline', 'formal', 'compiler'] : ['explore', 'facts', 'outline', 'formal', 'compiler'];
  for (const mode of shortcutModes) {
    const button = node('button', 'rv-view-shortcut'); button.type = 'button';
    const count = graphNodes(mode).length;
    button.append(node('strong', '', graphLabels[mode]), node('small', '', count ? `${count} ${count === 1 ? 'node' : 'nodes'}` : 'Not included'));
    button.addEventListener('click', () => openGraph(mode));
    $('rv-view-shortcuts').append(button);
  }
  const leanShortcut = node('button', 'rv-view-shortcut'); leanShortcut.type = 'button';
  leanShortcut.append(node('strong', '', analysis ? 'Lean statements' : 'Lean source'), node('small', '', data.lean_source ? 'Saved source' : 'Not included'));
  leanShortcut.addEventListener('click', () => tab('lean')); if (!readable) $('rv-view-shortcuts').append(leanShortcut);
  for (const [index, agent] of agents.slice(0, 4).entries()) {
    const item = node('li'); const body = node('div');
    body.append(node('strong', '', agent.label || `Research step ${index + 1}`), node('small', '', statusLabel(agent.status) || 'Recorded'));
    item.append(node('span', 'rv-activity-number', index + 1), body); $('rv-activity-preview').append(item);
  }
  if (!agents.length) $('rv-activity-preview').append(node('li', 'rv-muted rv-no-activity', 'No activity summaries included.'));
  function selectStep(index) {
    const agent = agents[index];
    all('.rv-step').forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
    const detail = $('rv-step-detail'); detail.replaceChildren();
    detail.append(node('p', 'rv-step-kicker', `RESEARCH STEP ${String(index + 1).padStart(2, '0')}`), node('h2', '', agent.label || 'Research step'));
    const meta = node('div', 'rv-detail-meta');
    if (agent.status) meta.append(node('span', 'rv-badge', statusLabel(agent.status)));
    if (agent.model) meta.append(node('span', 'rv-badge', agent.model));
    detail.append(meta);
    const summary = node('div', 'rv-markdown'); renderText(summary, agent.summary || 'No summary was included for this step.'); detail.append(summary);
  }
  for (const [index, agent] of agents.entries()) {
    const button = node('button', 'rv-step'); button.type = 'button'; button.setAttribute('aria-pressed', 'false');
    const body = node('span'); body.append(node('strong', '', agent.label || `Research step ${index + 1}`), node('small', '', agent.model || 'Recorded activity'));
    button.append(node('span', 'rv-step-index', String(index + 1).padStart(2, '0')), body, node('span', 'rv-step-status', statusLabel(agent.status)));
    button.addEventListener('click', () => selectStep(index)); $('rv-agents').append(button);
  }
  if (agents.length) selectStep(0);
  else {
    empty($('rv-agents'), 'No recorded steps', 'Activity summaries were not included in this shared snapshot.', '⌁');
    empty($('rv-step-detail'), 'No activity details', 'The proof and graph views may still contain saved artifacts.', '⌁');
  }
  $('rv-step-count').textContent = `${agents.length} ${agents.length === 1 ? 'step' : 'steps'}`;
  const models = new Set(agents.map(agent => agent.model).filter(Boolean));
  for (const [value, label] of [[String(agents.length), 'Recorded research steps'], [models.size ? String(models.size) : '—', 'Models included in snapshot'], [statusLabel(data.status) || 'Saved', 'Run status']]) {
    const card = node('div', 'rv-monitor-stat'); card.append(node('strong', '', value), node('span', '', label)); $('rv-monitor-stats').append(card);
  }
  const leanSource = text(data.lean_source);
  $('rv-lean-text').textContent = leanSource || (analysis ? 'No Lean statements were included in this snapshot.' : 'No Lean source was included in this snapshot.');
  $('rv-lean-lines').textContent = leanSource ? `${leanSource.split('\n').length} lines · read only` : 'No source included';
  $('rv-copy-lean').disabled = !leanSource;
  const verification = data.verification && typeof data.verification === 'object' ? data.verification : null;
  const recordedResult = verification ? text(verification.result || verification.status) : '';
  $('rv-lean-badge').textContent = recordedResult ? 'Recorded: ' + statusLabel(recordedResult) : analysis ? 'Statements only' : leanSource ? 'Saved source' : 'No saved source';
  if (/error|fail/i.test(recordedResult)) $('rv-lean-badge').dataset.state = 'error';
  $('rv-lean-version').textContent = verification?.version || '';
  if (verification && recordedResult) {
    $('rv-verification').append(node('p', '', statusLabel(recordedResult)));
    if (verification.reason) $('rv-verification').append(node('p', '', verification.reason));
    $('rv-verification').append(node('p', 'rv-muted', 'Saved checker metadata. This page has not independently rechecked the source.'));
  } else $('rv-verification').append(node('p', '', analysis ? 'Analyze formalizes statements; it does not attempt their Lean proofs.' : 'No checker result was included in this snapshot. Saved source alone does not establish verification.'));
  $('rv-fidelity').append(node('p', '', 'No fidelity assessment was included in this snapshot.'), node('p', 'rv-muted', 'Compilation and agreement with the original mathematical claim are separate checks.'));
  $('rv-workspace').setAttribute('aria-busy', 'false');
  const requested = new URL(location.href).searchParams.get('view');
  if (['outline', 'explore', 'facts', 'formal', 'compiler', 'manuscript'].includes(requested)) openGraph(requested);
  else if (requested === 'informal') openGraph(analysis ? 'manuscript' : 'outline');
  else if (requested === 'proof') tab('sandbox');
  else if (requested === 'agents') tab('monitor');
  else if (['sandbox', 'lean', 'monitor', 'dag'].includes(requested)) tab(requested);
  else tab(activeTab);
})();
