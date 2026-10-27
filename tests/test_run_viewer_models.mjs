// The shared run page counts the models of a run: a dated snapshot that a
// provider reports ("gpt-6-astra-2026-10-01") counts as the model it snapshots.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../agent_monitor/web/run-viewer.js', import.meta.url), 'utf8')
const line = source.split('\n').find(text => text.includes('const modelFamily = model =>'))
const modelFamily = new Function(`${line.trim()} return modelFamily;`)()

test('a dated snapshot counts as the model it snapshots', () => {
  const models = ['claude-fable-5-1', 'gpt-6-astra', 'gpt-6-astra', 'gpt-6-astra-2026-10-01', 'claude-sonnet-4-5-20250929', 'claude-sonnet-4-5']
  assert.deepEqual([...new Set(models.map(modelFamily))], ['claude-fable-5-1', 'gpt-6-astra', 'claude-sonnet-4-5'])
  // Other ids are left as they are.
  for (const id of ['gpt-5.6-sol', 'kimi-k3', 'claude-opus-5-5', 'gpt-4o-mini', 'model-2026']) {
    assert.equal(modelFamily(id), id)
  }
})

test('the snapshot stat counts models that way', () => {
  assert.match(source, /const models = new Set\(agents\.map\(agent => agent\.model\)\.filter\(Boolean\)\.map\(modelFamily\)\)/)
})
