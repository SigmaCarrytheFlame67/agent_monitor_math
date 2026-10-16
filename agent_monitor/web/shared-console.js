/* A shared run, shown by the console's own interface (and its Monitor frame).
 *
 * Loaded before the console's scripts only on /shared/<token> pages. It
 * answers the run-view requests the console makes (run record, workspace
 * proof, graphs, Lean record, run list) from the owner's saved snapshot and
 * refuses every other request, so a shared page never reads or changes the
 * visitor's own account, never starts work and never reaches other runs.
 */
'use strict';
(() => {
  const meta = document.querySelector('meta[name="ansatze-shared"]');
  if (!meta) return;
  let config;
  try { config = JSON.parse(meta.getAttribute('content') || '{}'); } catch { return; }
  if (!config || typeof config.data !== 'string') return;

  const RUN = 'shared';
  const base = typeof config.base === 'string' ? config.base.replace(/\/+$/, '') : '';
  const nativeFetch = window.fetch.bind(window);
  document.documentElement.classList.add('shared-view');

  let snapshot = null;
  const loaded = nativeFetch(config.data, { credentials: 'same-origin', cache: 'no-store' })
    .then(async response => {
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || 'This shared link is unavailable or has been revoked.');
      snapshot = body;
      return body;
    });
  // An unhandled rejection is reported by the console's own boot instead.
  loaded.catch(() => {});

  const view = () => (snapshot && snapshot.console) || {};
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
  const refused = () => json({ error: 'This shared run is read-only.' }, 403);
  const missing = () => json({ error: 'Not included in this shared snapshot.' }, 404);

  function appPath(raw) {
    let url;
    try { url = new URL(raw, location.href); } catch { return null; }
    if (url.origin !== location.origin) return null;
    let path = url.pathname;
    if (base && (path === base || path.startsWith(base + '/'))) path = path.slice(base.length) || '/';
    return { path, query: url.searchParams };
  }

  function graphKey(query) {
    const kind = String(query.get('kind') || 'informal').toLowerCase();
    if (kind === 'engine') return 'engine:' + (String(query.get('view') || 'workflow').toLowerCase() === 'facts' ? 'facts' : 'workflow');
    return ['informal', 'formal', 'analysis'].includes(kind) ? kind : 'informal';
  }

  function answer(path, query) {
    const data = view();
    const run = data.run || {};
    const runPath = '/api/run/' + RUN;
    if (path === runPath) return json(run);
    if (path === runPath + '/proof_graph') {
      const key = graphKey(query);
      const payload = (data.graphs || {})[key];
      return json(payload || { status: 'none', kind: key.split(':')[0] });
    }
    if (path === runPath + '/lean_verify') return json(data.lean || { status: 'none' });
    if (path === '/api/workspace/' + RUN + '/files') return json({ files: data.files || [] });
    if (path === '/api/workspace/' + RUN + '/file') {
      // The saved document (proof.md, or an analysis report) is the only file.
      const preview = run.sandbox_preview || {};
      const name = String(query.get('path') || '');
      if (!preview.source || name !== preview.source || typeof preview.text !== 'string') return missing();
      return new Response(preview.text, { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }
    if (path === '/api/runs/' + RUN + '/chat') return json({ messages: Array.isArray(data.chat) ? data.chat : [] });
    if (path === '/api/runs') {
      return json({ runs: [{
        run_id: RUN, engine: run.engine || '', status: run.status || '', run_kind: run.run_kind || '',
        problem_id: RUN, problem_preview: data.title || run.problem_preview || '',
      }] });
    }
    if (path === '/api/jobs') return json({ jobs: [] });
    return missing();
  }

  async function sharedFetch(input, init) {
    const request = typeof Request !== 'undefined' && input instanceof Request ? input : null;
    const raw = request ? request.url : String(input);
    const method = String((init && init.method) || (request && request.method) || 'GET').toUpperCase();
    const target = appPath(raw);
    // The snapshot itself is the only request that reaches the server.
    if (target && target.path === appPath(config.data)?.path) return nativeFetch(input, init);
    if (!target || method !== 'GET' && method !== 'HEAD') return refused();
    if (!target.path.startsWith('/api/')) return refused();
    try { await loaded; } catch (error) { return json({ error: error.message }, 404); }
    return answer(target.path, target.query);
  }

  window.fetch = sharedFetch;
  window.__PC_SHARED__ = Object.freeze({
    runId: RUN,
    console: typeof config.console === 'string' ? config.console : (base || '') + '/',
    monitor: typeof config.monitor === 'string' ? config.monitor : '',
    view: typeof config.view === 'string' ? config.view : '',
    snapshot: () => loaded,
    data: () => snapshot,
  });
})();
