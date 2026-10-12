// Give the home page the Problems counts at build time, so the full catalogue is never bundled.
// The overlay (/research/problem-status.json) may update these counts in the browser when it matches content_sha256.
// A catalogue that does not parse fails the build, which keeps the last published site in place.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { categoryCounts, indexCatalogue } from './problemCatalogue.mjs'

const CATALOGUE = fileURLToPath(new URL('../../public/research/open-problems.json', import.meta.url))

export function summarize(data) {
  const rows = indexCatalogue(data)
  const counts = data.counts || {}
  return {
    total: rows.length,
    schema_version: data.schema_version,
    snapshot_date: typeof data.snapshot_date === 'string' ? data.snapshot_date : null,
    content_sha256: typeof data.content_sha256 === 'string' ? data.content_sha256 : null,
    category_counts: categoryCounts(rows),
    collections: (data.sources || []).filter(source => counts[source.id] > 0)
      .map(source => ({ id: source.id, name: source.name, abbreviation: source.abbreviation || null, count: counts[source.id] })),
  }
}

export default {
  watch: ['../../public/research/open-problems.json'],
  load() {
    return summarize(JSON.parse(readFileSync(CATALOGUE, 'utf8')))
  },
}
