// Shared runs: the console's read-only shim, and the console's share dialog.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const SHIM = readFileSync(new URL('../agent_monitor/web/shared-console.js', import.meta.url), 'utf8')
const SHARING = readFileSync(new URL('../agent_monitor/web/sharing.js', import.meta.url), 'utf8')

function loadShim(snapshot, status = 200) {
  const calls = []
  const config = { data: '/proof-x/api/shared/T', base: '/proof-x', console: '/proof-x/', monitor: '/proof-x/shared/T/monitor', view: 'dag' }
  const window = {
    fetch: async (input, init) => {
      calls.push([String(input), (init && init.method) || 'GET'])
      return new Response(JSON.stringify(snapshot), { status, headers: { 'Content-Type': 'application/json' } })
    },
  }
  const classes = new Set()
  const document = {
    querySelector: selector => selector === 'meta[name="ansatze-shared"]' ? { getAttribute: () => JSON.stringify(config) } : null,
    documentElement: { classList: { add: name => classes.add(name) } },
  }
  vm.runInNewContext(SHIM, { window, document, location: { href: 'https://ansatze.test/proof-x/shared/T', origin: 'https://ansatze.test' }, URL, Response, Request, JSON })
  return { window, calls, classes }
}

const VIEW = {
  run: { run_id: 'shared', status: 'finished', engine: 'math_harness', sandbox_preview: { source: 'proof.md', text: '# Proof' } },
  files: [{ path: 'proof.md', kind: 'text' }],
  graphs: { 'engine:facts': { kind: 'engine', graph_role: 'fact-dependency', graph: { nodes: [{ id: 'a' }], edges: [] } } },
  lean: { status: 'verified', lean: 'theorem t : True := trivial' },
  chat: [{ role: 'assistant', content: 'done' }],
  title: 'Odd sums',
}

test('the shared shim answers the console run view from the snapshot only', async () => {
  const { window, calls, classes } = loadShim({ console: VIEW })
  assert.ok(classes.has('shared-view'))
  assert.equal(window.__PC_SHARED__.runId, 'shared')
  assert.equal(window.__PC_SHARED__.monitor, '/proof-x/shared/T/monitor')
  const json = async (url, init) => (await window.fetch(url, init)).json()
  assert.equal((await json('/api/run/shared')).engine, 'math_harness')
  // Base-prefixed and unprefixed console URLs both resolve.
  assert.equal((await json('/proof-x/api/run/shared/proof_graph?kind=engine&view=facts')).graph_role, 'fact-dependency')
  assert.deepEqual(await json('/api/run/shared/proof_graph?kind=formal'), { status: 'none', kind: 'formal' })
  assert.equal((await json('/api/run/shared/lean_verify')).status, 'verified')
  assert.deepEqual(await json('/api/workspace/shared/files'), { files: VIEW.files })
  assert.equal(await (await window.fetch('/api/workspace/shared/file?path=proof.md&t=1')).text(), '# Proof')
  assert.equal((await window.fetch('/api/workspace/shared/file?path=attachments/a.txt')).status, 404)
  assert.deepEqual(await json('/api/runs/shared/chat'), { messages: VIEW.chat })
  assert.equal((await json('/api/runs')).runs[0].problem_preview, 'Odd sums')
  assert.deepEqual(await json('/api/jobs'), { jobs: [] })
  // Nothing else: no account, settings, other runs, writes, pages or other origins.
  for (const url of ['/api/auth/me', '/api/settings', '/api/run/other-run', '/api/run/shared/export'])
    assert.equal((await window.fetch(url)).status, 404, url)
  for (const [url, method] of [['/api/runs/shared/chat', 'POST'], ['/api/run/shared/lean_verify', 'POST'],
    ['/api/runs/shared/share', 'DELETE'], ['/login', 'GET'], ['https://evil.test/x', 'GET']])
    assert.equal((await window.fetch(url, { method })).status, 403, method + ' ' + url)
  assert.deepEqual(calls, [['/proof-x/api/shared/T', 'GET']])
})

test('an unavailable shared link reports why to the console', async () => {
  const { window } = loadShim({ error: 'This shared link is unavailable or has been revoked.' }, 404)
  await assert.rejects(window.__PC_SHARED__.snapshot(), /revoked/)
  const response = await window.fetch('/api/run/shared')
  assert.equal(response.status, 404)
  assert.match((await response.json()).error, /revoked/)
})

function element(id = '') {
  return {
    id, style: {}, dataset: {}, disabled: false, textContent: '', value: '', checked: false, open: false,
    prepend() {}, append() {}, focus() {}, select() {},
    showModal() { this.open = true }, close() { this.open = false },
  }
}

function loadDialog(globals) {
  const nodes = new Map()
  const dialog = element('dialog')
  dialog.querySelector = selector => {
    const id = selector.slice(1)
    if (!nodes.has(id)) nodes.set(id, element(id))
    return nodes.get(id)
  }
  const made = []
  const document = {
    querySelector: selector => selector === '.session-side' ? element('side') : null,
    getElementById: id => id === 'run-meta' ? element('run-meta') : null,
    createElement: tag => { const el = tag === 'dialog' ? dialog : element(tag); made.push(el); return el },
    head: { append() {} }, body: { append() {} },
  }
  const pending = []
  const context = {
    document, URL, location: { origin: 'https://ansatze.test' }, navigator: {},
    MutationObserver: class { constructor(fn) { context.observe = fn } observe() {} },
    fetch: (url, init) => new Promise(resolve => pending.push({ url, method: init.method, resolve })),
    window: { open() {} }, ...globals,
  }
  vm.createContext(context)
  vm.runInContext(SHARING, context)
  const button = made.find(el => el.id === 'share-run-btn')
  return { context, button, nodes, pending }
}

const reply = body => ({ ok: true, status: 200, json: async () => body })

test('the share dialog drops a reply meant for the run it showed before', async () => {
  const { context, button, nodes, pending } = loadDialog({ runId: 'run-a', CURRENT_USER: { id: 1 }, lastSandboxRun: { run_id: 'run-a', owner_id: 1 } })
  const first = button.onclick()
  context.runId = 'run-b'
  context.lastSandboxRun = { run_id: 'run-b', owner_id: 1 }
  const second = button.onclick()
  assert.deepEqual(pending.map(p => p.url), ['/api/runs/run-a/share', '/api/runs/run-b/share'])
  pending[1].resolve(reply({ shared: true, path: '/shared/B' }))
  await second
  pending[0].resolve(reply({ shared: true, path: '/shared/A' }))
  await first
  assert.equal(nodes.get('share-url').value, 'https://ansatze.test/shared/B')
  nodes.get('share-revoke').onclick()
  assert.equal(pending[2].url, '/api/runs/run-b/share')
  assert.equal(pending[2].method, 'DELETE')
})

test('Share is offered only to the person who started the run, or the operator for ownerless runs', () => {
  const cases = [
    [{ id: 1 }, { run_id: 'r', owner_id: 1 }, ''],
    [{ id: 2 }, { run_id: 'r', owner_id: 1 }, 'none'],
    [{ id: 1, guest: true }, { run_id: 'r', owner_id: 1 }, 'none'],
    [{ id: 3, is_admin: true }, { run_id: 'r', owner_id: 1 }, 'none'],
    [{ id: 3, is_admin: true }, { run_id: 'r', owner_id: null }, ''],
    [{ id: 1 }, null, 'none'],
  ]
  for (const [user, run, display] of cases) {
    const { button } = loadDialog({ runId: 'r', CURRENT_USER: user, lastSandboxRun: run })
    assert.equal(button.style.display, display, JSON.stringify([user, run]))
  }
})

test('a non-JSON server error reads as a sentence in the dialog', async () => {
  const { button, nodes, pending } = loadDialog({ runId: 'r', CURRENT_USER: { id: 1 }, lastSandboxRun: { run_id: 'r', owner_id: 1 } })
  const opened = button.onclick()
  pending[0].resolve({ ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected token <') } })
  await opened
  assert.equal(nodes.get('share-status').textContent, 'Sharing failed (HTTP 502). Try again.')
})
