// The Projects page: a project that accounts other than site administrators
// can write in offers only the engines the server lists for it. The real
// projects.js runs in a vm context with a small fake DOM and fetch.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source = readFileSync(new URL('../agent_monitor/web/projects.js', import.meta.url), 'utf8')

const ENGINES = [
  { id: 'claude', label: 'Claude Code', available: true },
  { id: 'codex', label: 'Codex', available: true },
  { id: 'kimi', label: 'Ansatz CGM · Kimi K3', available: true },
  { id: 'plain', label: 'Plain', available: true },
  { id: 'math_harness', label: 'Ansatz CGM', available: true },
  { id: 'openclaw', label: 'OpenClaw', available: false },
  { id: 'analyze', label: 'Ansatz Analyze', available: true, modes: ['analyze'] },
]

function element(tag) {
  return {
    tagName: tag.toUpperCase(), hidden: false, textContent: '', value: '', className: '', checked: false,
    selected: false, required: false, placeholder: '', children: [], selectionStart: 0,
    append(...nodes) {
      for (const node of nodes) {
        this.children.push(node)
        // A select shows its selected option, or else its first.
        if (this.tagName === 'SELECT' && (node.selected || this.children.length === 1)) this.value = node.value
      }
    },
    replaceChildren(...nodes) { this.children = []; this.value = ''; this.append(...nodes) },
    before(node) { this.previous = node },
    addEventListener() {},
    setSelectionRange() {},
  }
}

async function page(project) {
  const elements = new Map()
  const byId = id => {
    if (!elements.has(id)) elements.set(id, element(id === 'project-engine' ? 'select' : 'div'))
    return elements.get(id)
  }
  const prove = { ...element('input'), value: 'prove', checked: true }
  const analyze = { ...element('input'), value: 'analyze' }
  const intervals = []
  const state = { project }
  const responses = path => ({
    '/api/auth/me': { user: { id: 1, is_admin: true } },
    '/api/projects/p1': state.project,
    '/api/engines': { engines: ENGINES },
  })[path]
  const context = vm.createContext({
    document: {
      body: { dataset: { base: '' } },
      activeElement: null,
      getElementById: byId,
      createElement: element,
      querySelector: selector => (selector.includes(':checked') ? [prove, analyze].find(input => input.checked) : prove),
      querySelectorAll: () => [prove, analyze],
    },
    window: { addEventListener() {} },
    location: { pathname: '/projects/p1', origin: 'http://localhost' },
    crypto: globalThis.crypto,
    fetch: async path => ({ ok: true, status: 200, json: async () => structuredClone(responses(path)) }),
    setInterval: callback => intervals.push(callback),
    console,
  })
  vm.runInContext(source, context)
  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)) }
  await settle()
  return {
    options: () => byId('project-engine').children.map(option => option.value),
    runs: () => byId('project-runs').children,
    selected: () => byId('project-engine').value,
    note: () => byId('project-analyze-note').previous,
    choose: value => { byId('project-engine').value = value },
    async poll(next) { state.project = next; await intervals[1](); await settle() },
    analyze: async () => { prove.checked = false; analyze.checked = true; analyze.onchange(); await settle() },
  }
}

const PROJECT = { id: 'p1', title: 'Joint work', owner_id: 1, is_owner: true, problem: 'Prove it.', character_ids: [], revision: 1, members: [], runs: [] }

test('a project only administrators write in offers every available proving engine', async () => {
  const view = await page({ ...PROJECT, tool_free_engines: null })
  assert.deepEqual(view.options(), ['claude', 'codex', 'kimi', 'plain', 'math_harness'])
  assert.equal(view.note().hidden, true)
})

test('an untrusted project offers only its tool-free engines and says why', async () => {
  const view = await page({ ...PROJECT, tool_free_engines: ['kimi', 'math_harness', 'plain'] })
  assert.deepEqual(view.options(), ['kimi', 'plain', 'math_harness'])
  assert.equal(view.note().hidden, false)
  assert.match(view.note().textContent, /tool-free engines/)
  // The note is about proofs: an analysis never receives the document.
  await view.analyze()
  assert.equal(view.note().hidden, true)
})

test('the engine list follows the project when a member joins, keeping a choice that stays allowed', async () => {
  const view = await page({ ...PROJECT, tool_free_engines: null })
  view.choose('plain')
  await view.poll({ ...PROJECT, tool_free_engines: ['kimi', 'math_harness', 'plain'] })
  assert.deepEqual(view.options(), ['kimi', 'plain', 'math_harness'])
  assert.equal(view.selected(), 'plain')
  assert.equal(view.note().hidden, false)
})

test('a project lists a readable version with its own tag and caveat', async () => {
  const runs = [{ run_id: 'r1', run_kind: 'readability', engine: 'readability', status: 'finished', user: 1, task: '' },
    { run_id: 'r2', run_kind: 'analyze', engine: 'analyze', status: 'finished', user: 1, task: 'Lemma 3.7' }]
  const view = await page({ ...PROJECT, tool_free_engines: null, runs })
  const rows = view.runs()
  const heading = row => row.children[0].children[0].children
  assert.equal(heading(rows[0])[0].textContent, 'Readable')
  assert.equal(heading(rows[0])[1], 'Readable version')
  assert.match(rows[0].children[0].children[1].textContent, /^Ansatz Readability · checked against the original by a model, not a certificate · finished/)
  assert.equal(heading(rows[1])[0].textContent, 'Analyze')
})
