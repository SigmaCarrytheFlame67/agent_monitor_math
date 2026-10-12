import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { indexCatalogue, filterProblems, presentProblem, problemStatus, statementReady, createSolvePayload, prepareProblemForSolve, solveStorageKey, sourceLinks,
  problemCategories, problemBadges, categoryCounts, categoryInfo, problemBadgeList, primaryEvidence, solveAction, parseStatusOverlay, applyStatusOverlay, overlayCategoryCounts,
  statusFreshness, readProblemQuery, problemQueryString, evidenceTimeline, formatEvidenceDate, cleanEvidence, maxOverlayChars, statusDate, categoryEvidence,
  analyzeLink, analyzeEvidence, categoryTabCounts, recentlyChanged, maxManuscriptProblems, summaryRepeatsStatement, formatPrize } from '../docs/.vitepress/theme/problemCatalogue.mjs'
import { renderMath } from '../docs/.vitepress/theme/mathText.js'

const json = path => JSON.parse(readFileSync(new URL(path, import.meta.url)))
const shipped = json('../docs/public/research/open-problems.json')
const rows = indexCatalogue(shipped)
const fixture = json('./fixtures/problems_catalogue_v3.json')
const v3 = indexCatalogue(fixture)
const row = id => v3.find(item => item.id === id)
const overlayFile = json('./fixtures/problems_status_overlay.json')
const now = Date.parse('2026-10-11T12:00:00Z')
const categories = problemCategories.map(category => category.id)

test('categories and badges use the status taxonomy\'s exact wording', () => {
  assert.deepEqual(problemCategories.map(category => [category.id, category.label, category.description]), [
    ['solved', 'Solved', 'Answered, and the answer is accepted (source of record, refereed publication, or broad expert acceptance).'],
    ['ai-proposed', 'AI proposed', 'An AI system produced a claimed full solution that mathematicians have not yet confirmed. Treated as unsolved until confirmed.'],
    ['partial', 'Partial', 'An explicitly stated part of the problem is answered; the rest remains open.'],
    ['open', 'Open', 'No accepted answer. Partial results, human claims under review, and AI partial results are shown as badges.'],
  ])
  assert.deepEqual(Object.fromEntries(Object.entries(problemBadges).map(([id, badge]) => [id, badge.label])), {
    'withdrawn-claim': 'Withdrawn claim', disputed: 'Disputed', 'sources-conflict': 'Sources disagree', 'reported-signal': 'New report (unreviewed)',
    'ai-confirmed': 'AI solution confirmed', 'ai-assisted-solution': 'AI-assisted solution', 'ai-partial': 'AI partial result', 'human-claim': 'Claim under review',
    'lean-pending': 'Lean proof awaiting review', 'formal-statement': 'Lean statement', prize: 'Prize', 'ambiguous-statement': 'Ambiguous statement',
    'not-in-current-edition': 'Not in current edition', 'upstream-substatus': 'Upstream sub-status',
  })
})
test('the shipped catalogue indexes completely, one category per entry, with stable ids', () => {
  assert.ok([2, 3].includes(shipped.schema_version))
  assert.equal(rows.length, shipped.total)
  assert.equal(new Set(rows.map(item => item.id)).size, rows.length)
  assert(rows.every(item => /^[a-z0-9][a-z0-9._-]{0,159}$/.test(item.id)))
  assert(rows.every(item => categories.includes(item.category)))
  const sources = new Set(shipped.sources.map(source => source.id))
  assert(rows.every(item => sources.has(item.source)))
  for (const id of Object.keys(shipped.counts)) assert.equal(rows.filter(item => item.source === id).length, shipped.counts[id], id)
  // the category tabs partition the catalogue
  assert.equal(categories.reduce((sum, category) => sum + filterProblems(rows, { category }).length, 0), rows.length)
  if (shipped.schema_version === 3) {
    assert.match(shipped.content_sha256, /^[0-9a-f]{64}$/)
    assert.deepEqual(categoryCounts(rows), shipped.category_counts)
    // AI proposed is never counted as solved, and every non-open status shows primary evidence
    assert(rows.every(item => item.category === 'open' || item.status_evidence.length > 0), 'non-open entries cite evidence')
  }
})
test('every shipped entry can be handed to the workspace', () => {
  for (const item of rows) {
    assert.doesNotThrow(() => createSolvePayload(item, 1), item.id)
    if (solveAction(item) === 'analyze') assert.match(createSolvePayload(item, 1, { mode: 'analyze' }).analyze_url, /^https:\/\//, item.id)
  }
})
test('schema 1 and 2 derive the category from the old status; other schemas are refused', () => {
  const legacy = (schema, statuses) => indexCatalogue({ schema_version: schema, total: statuses.length,
    problems: statuses.map((status, index) => ({ id: `p-${index}`, number: String(index), title: `P ${index}`, summary: '', source: 'erdos', source_url: 'https://example.org', topics: [], status })) })
  assert.deepEqual(legacy(2, ['source-open', 'resolution-announced', 'proved', 'solved', 'mystery']).map(item => item.category), ['open', 'ai-proposed', 'solved', 'solved', 'open'])
  assert.deepEqual(legacy(1, ['source-open']).map(item => item.category), ['open'])
  const announced = legacy(2, ['resolution-announced'])[0]
  assert.equal(problemStatus(announced), 'Resolution announced')
  assert.equal(categoryInfo(announced.category).label, 'AI proposed')
  assert.notEqual(announced.category, 'solved')
  assert.throws(() => indexCatalogue({ ...fixture, schema_version: 4 }), /Unsupported/)
  assert.throws(() => indexCatalogue({ schema_version: 3, total: 0 }), /Unsupported/)
})
test('inherited object keys never pass as categories, rules, badges or evidence labels', () => {
  const tricky = indexCatalogue({ ...fixture, total: 1, problems: [{ ...fixture.problems[1], category: 'constructor', category_rule: 'toString',
    badges: ['constructor', '__proto__', 'toString'], status_evidence: [{ kind: 'constructor', actor: 'ai', title: 'x', url: 'https://example.org' },
      { kind: 'claim', actor: 'toString', title: 'x', url: 'https://example.org' }, { kind: 'claim', actor: 'ai', acceptance: 'hasOwnProperty', title: 'kept', url: 'https://example.org' }] }] })[0]
  assert.equal(tricky.category, 'open')
  assert.equal(tricky.category_rule, null)
  assert.deepEqual(tricky.badges, [])
  assert.deepEqual(tricky.status_evidence, [{ kind: 'claim', actor: 'ai', title: 'kept', url: 'https://example.org/' }])
  const legacy = indexCatalogue({ schema_version: 2, total: 1, problems: [{ id: 'p', number: '1', title: 'P', summary: '', source: 'erdos', source_url: 'https://example.org', topics: [], status: 'constructor' }] })[0]
  assert.equal(legacy.category, 'open')
  assert.equal(problemStatus({ status: 'toString', source_status: 'Listed' }), 'Listed')
  const polluted = JSON.parse('{"__proto__": {"category": "solved"}, "summary": "{number}"}')
  const expanded = indexCatalogue({ ...fixture, total: 1, sources: [{ id: 'erdos', defaults: polluted }], problems: [{ ...fixture.problems[1], summary: undefined }] })[0]
  assert.equal(Object.getPrototypeOf(expanded), Object.prototype, 'a "__proto__" default never becomes the prototype')
  assert.equal(expanded.category, 'open')
  const overlay = parseStatusOverlay({ ...overlayFile, entries: { 'erdos-0005': { ...overlayFile.entries['erdos-0005'], category: 'solved', category_rule: 'constructor', badges_add: ['constructor'] } },
    signals: { 'erdos-0005': [{ ...overlayFile.signals['clay-poincare-conjecture'][0], relation: 'constructor' }] } })
  assert.equal(overlay.entries.has('erdos-0005'), false, 'an inherited key is not a rule: the entry is void')
  assert.equal(overlay.signals.size, 0)
})
test('missing records cannot silently reduce the displayed catalogue', () => {
  assert.throws(() => indexCatalogue({ ...shipped, problems: shipped.problems.slice(1) }), /incomplete/)
  assert.throws(() => indexCatalogue({ ...fixture, problems: fixture.problems.slice(1) }), /incomplete/)
})
test('v3 expands collection defaults, and a field on the problem wins', () => {
  const pointer = row('erdos-0005')
  assert.equal(pointer.summary, 'Erdős problem #5. Read the statement at the source.')
  assert.equal(pointer.source_name, 'Erdős Problems')
  assert.equal(pointer.rating.score, 2)
  assert.equal(pointer.status_as_of, '2026-10-11')
  assert.equal(row('erdos-0020').summary, 'A combinatorics question answered upstream.')
  assert.equal(row('erdos-0020').rating, null, 'an explicit null overrides the default')
  assert.equal(row('kourovka-20-003').formal_status, 'Fixture formal status overrides the default')
  assert.equal(row('kourovka-03-020').formal_status, 'No proof asserted by this catalogue')
  assert.equal(row('clay-poincare-conjecture').source_name, 'Millennium Prize Problems')
  assert.equal(fixture.problems.find(item => item.id === 'erdos-0005').summary, undefined, 'the input is not modified')
  assert.equal(presentProblem(pointer), pointer)
})
test('v3 categories, rules, badges and evidence are validated, never upgraded', () => {
  assert.deepEqual(categoryCounts(v3), fixture.category_counts)
  assert.equal(row('erdos-0390').category, 'open', 'an unknown category is shown as open, not derived from status')
  assert.equal(row('wiki-casas-alvero-conjecture').category, 'open')
  assert.equal(row('wiki-casas-alvero-conjecture').category_rule, null)
  assert.equal(row('kourovka-07-038').category_rule, null, 'a rule from another category is dropped')
  assert.equal(row('erdos-0003').category_rule, 'A2')
  const erdos3 = row('erdos-0003')
  assert.deepEqual(erdos3.badges, ['lean-pending', 'formal-statement', 'prize'])
  assert.deepEqual(erdos3.status_evidence.map(item => item.kind), ['source-marker', 'claim', 'formalization'], 'non-https and unknown kinds are dropped')
  assert.deepEqual(problemBadgeList(erdos3, fixture, 2).map(badge => badge.label), ['Lean proof awaiting review', 'Lean statement'])
  assert.equal(problemBadgeList(erdos3, fixture)[0].description, 'Fixture description from the catalogue.')
  assert.deepEqual(problemBadgeList(row('erdos-0242'), fixture).map(badge => badge.label), ['Falsifiable'])
  assert.equal(cleanEvidence({ kind: 'claim', actor: 'ai', title: 'x', url: 'https://example.org/' + 'a'.repeat(500) }), null, 'urls are at most 500 characters')
  const clean = cleanEvidence({ kind: 'claim', actor: 'robot', title: 'x', url: 'https://example.org' })
  assert.equal(clean, null, 'unknown actors are dropped')
  const partial = cleanEvidence({ kind: 'claim', actor: 'ai', title: 'x', url: 'https://example.org', acceptance: 'certified', date: '2026-13-01', note: 'n'.repeat(400) })
  assert.equal(partial.acceptance, undefined)
  assert.equal(partial.date, undefined)
  assert.equal(partial.note.length, 240)
})
test('the evidence timeline runs newest first and keeps the stated date precision', () => {
  assert.deepEqual(evidenceTimeline(row('clay-poincare-conjecture').status_evidence).map(item => item.date), ['2010-03-18', '2003', '2002-11'])
  assert.equal(formatEvidenceDate('2002-11', 'month'), 'Nov 2002')
  assert.equal(formatEvidenceDate('2003', 'year'), '2003')
  assert.equal(formatEvidenceDate('2026-10-06', 'day'), 'Oct 6, 2026')
  assert.equal(formatEvidenceDate('2026-10-06', 'year'), '2026')
  assert.equal(formatEvidenceDate(undefined), 'Date not recorded')
  assert.equal(formatEvidenceDate('2026-10-06', 'day', 'month'), 'Oct 2026', 'cards show months')
  assert.equal(formatEvidenceDate('1963', undefined, 'month'), '1963')
})
test('a card dates the evidence behind its category, never a later note', () => {
  assert.deepEqual(statusDate(row('erdos-0003')), { kind: 'evidence', date: '2026-10-06', precision: 'day' })
  assert.deepEqual(statusDate(row('clay-poincare-conjecture')), { kind: 'evidence', date: '2010-03-18', precision: undefined }, 'the accepted result, not the 2002 preprint')
  assert.deepEqual(statusDate(row('erdos-0020')), { kind: 'source', date: '2025-08-31', precision: undefined }, 'a source-of-record marker dates a solved entry as of that record')
  assert.deepEqual(statusDate(row('kourovka-07-038')), { kind: 'source', date: '2026-10-06', precision: undefined }, 'the answered part dates a partial entry')
  assert.deepEqual(statusDate(row('kourovka-03-020')), { kind: 'changed', date: '2026-10-06' }, 'the builder\'s status_changed date wins over the marker')
  assert.deepEqual(statusDate(row('erdos-0005')), { kind: 'checked', date: '2026-10-11', precision: 'day' }, 'open entries show when they were checked')
  assert.equal(categoryEvidence(row('erdos-0005')), null)
  // a formalization or survey is not a status change
  const noted = indexCatalogue({ ...fixture, total: 1, problems: [{ ...fixture.problems[1], status_evidence: [
    { kind: 'formalization', actor: 'ai', title: 'Lean file', url: 'https://example.org/lean', date: '2026-10-09' },
    { kind: 'survey', actor: 'human', title: 'Survey', url: 'https://example.org/survey', date: '2026-10-08' }] }] })[0]
  assert.equal(noted.statusChanged, null)
  assert.equal(filterProblems([noted], { changed: true, now }).length, 0)
  const { rows: updated } = applyStatusOverlay(v3, parseStatusOverlay(overlayFile), fixture.content_sha256)
  assert.deepEqual(statusDate(updated.find(item => item.id === 'erdos-0005')), { kind: 'changed', date: '2026-10-10', precision: 'day' })
  assert.deepEqual(statusDate(updated.find(item => item.id === 'erdos-0003')), { kind: 'changed', date: '2026-10-08', precision: 'day' })
})
test('a source marker is dated by its source revision, never counted as a status change', () => {
  const kourovka = { ...fixture.problems.find(item => item.id === 'kourovka-03-020'), status_changed: undefined }
  const erdos = { ...fixture.problems.find(item => item.id === 'erdos-0020'), status_evidence: [{ kind: 'source-marker', actor: 'maintainer', scope: 'full', date: '2026-10-09',
    title: 'Erdős problems database: proved (entry updated)', url: 'https://github.com/teorth/erdosproblems/blob/48e0c21/data/problems.yaml#L20', acceptance: 'accepted-by-maintainer' }] }
  // As the builder writes it for a researched answer: status_changed is the date the answer first held.
  const answered = { ...kourovka, id: 'kourovka-09-066', status_changed: '2026-10-05', status_evidence: [...kourovka.status_evidence,
    { kind: 'accepted-result', actor: 'human', scope: 'full', date: '2026-10-05', title: 'Solution', url: 'https://arxiv.org/abs/2610.01234', acceptance: 'accepted-by-maintainer' }] }
  const marked = indexCatalogue({ ...fixture, total: 3, problems: [kourovka, erdos, answered] })
  assert.deepEqual(marked.map(item => item.statusChanged), [null, null, Date.parse('2026-10-05')])
  assert.deepEqual(marked.map(item => recentlyChanged(item, now)), [false, false, true], 'an edition dated last week is not a change last week')
  assert.deepEqual(filterProblems(marked, { changed: true, now }).map(item => item.id), ['kourovka-09-066'])
  assert.deepEqual(filterProblems(marked, { sort: 'changed', now }).map(item => item.id), ['kourovka-09-066', 'kourovka-03-020', 'erdos-0020'])
  assert.deepEqual(filterProblems(marked, { now }).map(item => item.id), ['kourovka-09-066', 'kourovka-03-020', 'erdos-0020'], 'no recency boost from a marker')
  assert.deepEqual(statusDate(marked[0]), { kind: 'source', date: '2026-10-06', precision: undefined }, 'the card shows the marker as the source record\'s date')
  // The overlay's change time counts when it changes the category, not for a badge on an unchanged category.
  const badgeOnly = parseStatusOverlay({ schema_version: 1, catalogue_sha256: fixture.content_sha256,
    entries: { 'kourovka-03-020': { changed_at: '2026-10-10T06:00:00Z', change: 'Kourovka 3.20: a part is now marked', badges_add: ['ai-partial'] } } })
  assert.equal(recentlyChanged(applyStatusOverlay(marked, badgeOnly, fixture.content_sha256).rows[0], now), false)
  const recategorized = parseStatusOverlay({ schema_version: 1, catalogue_sha256: fixture.content_sha256,
    entries: { 'kourovka-03-020': { category: 'solved', category_rule: 'S1', changed_at: '2026-10-10T06:00:00Z', change: 'Kourovka 3.20 is now marked ∗ (answered)',
      evidence_add: [{ kind: 'source-marker', actor: 'maintainer', scope: 'full', date: '2026-10-10', title: 'Kourovka Notebook: marked ∗',
        url: 'https://arxiv.org/abs/1401.0300v49', acceptance: 'accepted-by-maintainer' }] } } })
  assert.equal(recentlyChanged(applyStatusOverlay(marked, recategorized, fixture.content_sha256).rows[0], now), true)
})
test('tab counts follow every filter except the category', () => {
  assert.deepEqual(categoryTabCounts(v3), { all: 10, ...fixture.category_counts })
  assert.deepEqual(categoryTabCounts(v3, { source: 'kourovka' }), { all: 3, solved: 0, 'ai-proposed': 1, partial: 1, open: 1 })
  assert.deepEqual(categoryTabCounts(v3, { source: 'erdos', search: 'progressions' }), { all: 1, solved: 0, 'ai-proposed': 1, partial: 0, open: 0 })
  assert.deepEqual(categoryTabCounts(v3, { source: 'kourovka', category: 'solved' }), categoryTabCounts(v3, { source: 'kourovka' }), 'the selected tab does not change the counts')
  for (const category of categories) assert.equal(categoryTabCounts(v3, { source: 'erdos', ai: true })[category], filterProblems(v3, { source: 'erdos', ai: true, category }).length, category)
})
test('category tabs partition the catalogue and the filters compose', () => {
  assert.deepEqual(filterProblems(v3, { category: 'ai-proposed', sort: 'source' }).map(item => item.id), ['erdos-0003', 'kourovka-03-020'])
  assert.equal(categories.reduce((sum, category) => sum + filterProblems(v3, { category }).length, 0), v3.length)
  // AI involvement: any AI badge, AI-proposed category or AI evidence
  assert.deepEqual(filterProblems(v3, { ai: true, sort: 'source' }).map(item => item.id), ['erdos-0003', 'erdos-0020', 'kourovka-03-020', 'kourovka-07-038', 'wiki-casas-alvero-conjecture'])
  assert.deepEqual(filterProblems(v3, { lean: true, sort: 'source' }).map(item => item.id), ['erdos-0003', 'erdos-0390', 'clay-poincare-conjecture'])
  assert.deepEqual(filterProblems(v3, { changed: true, now, sort: 'source' }).map(item => item.id), ['kourovka-03-020'], 'the builder\'s change date (or an overlay change of category); never a marker\'s edition date or a dated claim by itself')
  assert.deepEqual(filterProblems(v3, { prizeOnly: true, sort: 'source' }).map(item => item.id), ['erdos-0003', 'clay-poincare-conjecture'])
  assert.deepEqual(filterProblems(v3, { search: 'Turán progressions', sort: 'source' }).map(item => item.id), ['erdos-0003'], 'aliases are searchable')
  assert.deepEqual(filterProblems(v3, { source: 'kourovka', topic: 'profinite groups' }).map(item => item.id), ['kourovka-07-038'])
  assert.equal(filterProblems(v3, { source: 'linear-codes' }).length, 0)
})
test('sorts: notable first, latest status change, source order; none mutates the input', () => {
  const first = v3[0].id
  const notable = filterProblems(v3, { now }).map(item => item.id)
  assert.deepEqual(notable.slice(0, 2), ['erdos-0003', 'clay-poincare-conjecture'], 'curated prize problems lead, alternating collections')
  assert.equal(notable.length, v3.length)
  const changed = filterProblems(v3, { sort: 'changed', now }).map(item => item.id)
  assert.deepEqual(changed.slice(0, 3), ['kourovka-03-020', 'erdos-0003', 'erdos-0005'], 'only a change of category dates the sort; the rest keep source order')
  assert.deepEqual(changed.slice(-5), ['erdos-0390', 'kourovka-07-038', 'kourovka-20-003', 'clay-poincare-conjecture', 'wiki-casas-alvero-conjecture'], 'entries without a change of category (dated only by a marker or by claims) keep source order')
  assert.deepEqual(filterProblems(v3, { sort: 'source' }).map(item => item.id), v3.map(item => item.id))
  assert.equal(v3[0].id, first)
})
test('the overlay parser keeps only allowed values and bounded sizes', () => {
  const overlay = parseStatusOverlay(overlayFile)
  assert.deepEqual([...overlay.entries.keys()], ['erdos-0005', 'erdos-0003', 'not-in-catalogue'], 'entries without a timestamp, with invalid ids or with a category change the overlay may not make are dropped')
  assert.deepEqual(overlay.entries.get('erdos-0005').badges_add, ['ai-assisted-solution'])
  assert.equal(overlay.entries.get('erdos-0005').evidence_add.length, 1, 'credentials in a link drop the record')
  assert.equal(overlay.entries.has('kourovka-20-003'), false, 'a rule from another category voids the whole entry')
  assert.equal(overlay.signals.get('clay-poincare-conjecture').length, 1, 'non-https, unknown relations and unknown sources are dropped')
  assert.equal(overlay.recent_changes.length, 3)
  assert.equal(overlay.sources[2].revision, '1379514120')
  assert.equal(overlay.source_errors[0].source_id, 'kourovka')
  for (const bad of [null, [], 'text', { ...overlayFile, schema_version: 2 }]) assert.equal(parseStatusOverlay(bad), null)
  assert.equal(parseStatusOverlay({ ...overlayFile, catalogue_sha256: 'not-a-sha' }).catalogue_sha256, null)
  const loose = parseStatusOverlay({ ...overlayFile, last_checked_at: 'Oct 10 2026', entries: { 'erdos-0005': { ...overlayFile.entries['erdos-0005'], changed_at: 'yesterday' } } })
  assert.equal(loose.last_checked_at, null, 'timestamps must be ISO 8601')
  assert.equal(loose.entries.size, 0)
  const flood = parseStatusOverlay({ ...overlayFile,
    signals: { 'erdos-0005': Array.from({ length: 30 }, (_, i) => ({ title: `S${i}`, url: `https://arxiv.org/abs/2610.${String(i).padStart(5, '0')}`, date: '2026-10-01', relation: 'mentions', source: 'arxiv', matched_by: 'kimi-k3', evidence_quote: 'quoted text' })) },
    recent_changes: Array.from({ length: 80 }, (_, i) => ({ id: 'erdos-0005', date: '2026-10-10', change: `change ${i}`, source: 'erdos' })),
    entries: { 'erdos-0005': { ...overlayFile.entries['erdos-0005'], status_headline: 'h'.repeat(900), evidence_add: Array.from({ length: 40 }, () => overlayFile.entries['erdos-0005'].evidence_add[0]) } } })
  assert.equal(flood.signals.get('erdos-0005').length, 10)
  assert.equal(flood.recent_changes.length, 50)
  assert.equal(flood.entries.get('erdos-0005').status_headline.length, 280)
  assert.equal(flood.entries.get('erdos-0005').evidence_add.length, 12)
  const longQuote = parseStatusOverlay({ ...overlayFile, signals: { 'erdos-0005': [{ ...overlayFile.signals['clay-poincare-conjecture'][0], evidence_quote: 'q'.repeat(241) }] } })
  assert.equal(longQuote.signals.size, 0, 'an evidence quote longer than 240 characters is not shortened into a different quote')
  assert.ok(maxOverlayChars >= 150_000)
})
test('the overlay changes a category only with a source marker it carries, or back to Open; anything else voids the entry', () => {
  const marker = { kind: 'source-marker', actor: 'maintainer', scope: 'full', date: '2026-10-10', title: 'Erdős problems database: proved',
    url: 'https://github.com/teorth/erdosproblems/blob/abc1234/data/problems.yaml#L80', acceptance: 'accepted-by-maintainer' }
  const entry = fields => ({ changed_at: '2026-10-10T00:00:00Z', change: 'A change', status_headline: 'A new headline', ...fields })
  const overlay = parseStatusOverlay({ schema_version: 1, catalogue_sha256: fixture.content_sha256, entries: {
    'erdos-0005': entry({ category: 'solved', category_rule: 'S2', evidence_add: [marker] }),
    'erdos-0242': entry({ category: 'ai-proposed', category_rule: 'A2', evidence_add: [marker] }),
    'erdos-0390': entry({ category: 'solved', category_rule: 'S1' }),
    'kourovka-20-003': entry({ category: 'solved', category_rule: 'S1', evidence_add: [{ ...marker, url: 'http://example.org/marker' }] }),
    'wiki-casas-alvero-conjecture': entry({ category: 'partial', category_rule: 'P1', evidence_add: [{ ...marker, kind: 'claim', actor: 'ai' }] }),
    'erdos-0003': entry({ category: 'solved', category_rule: 'S1', evidence_add: [{ ...marker, acceptance: 'withdrawn' }] }),
    'erdos-0020': entry({ category: 'open', category_rule: 'O' }),
    'kourovka-07-038': entry({ category: 'solved', category_rule: 'S1', evidence_add: [{ ...marker, title: 'Kourovka 7.38 marked ∗', url: 'https://arxiv.org/pdf/1401.0300v49#page=25' }] }),
  }, recent_changes: [{ id: 'erdos-0390', date: '2026-10-10', change: 'Erdős #390 marked proved' }, { id: 'kourovka-07-038', date: '2026-10-10', change: 'Kourovka 7.38 marked ∗' }] })
  assert.deepEqual([...overlay.entries.keys()], ['erdos-0020', 'kourovka-07-038'], 'research rules, and moves out of Open without a valid live marker, are void')
  assert.deepEqual(overlay.recent_changes.map(change => change.id), ['kourovka-07-038'], 'a void entry is not announced either')
  const { rows: updated } = applyStatusOverlay(v3, overlay, fixture.content_sha256)
  const find = id => updated.find(item => item.id === id)
  for (const id of ['erdos-0005', 'erdos-0242', 'erdos-0390', 'kourovka-20-003', 'wiki-casas-alvero-conjecture', 'erdos-0003']) {
    assert.equal(find(id).category, v3.find(item => item.id === id).category, id)
    assert.notEqual(find(id).status_headline, 'A new headline', id)
  }
  assert.equal(find('erdos-0020').category, 'open', 'a return to Open needs no evidence')
  assert.equal(find('kourovka-07-038').category, 'solved')
  assert(updated.every(item => item.category === 'open' || item.status_evidence.length > 0), 'every non-open status shows evidence')
})
test('overlay change text keeps up to 400 characters and marks a longer cut at a word', () => {
  const exact = 'a'.repeat(400)
  const words = Array.from({ length: 90 }, (_, index) => `word${index}`).join(' ')
  const overlay = parseStatusOverlay({ schema_version: 1, entries: { 'erdos-0005': { changed_at: '2026-10-11T00:00:00Z', change: exact }, 'erdos-0020': { changed_at: '2026-10-11T00:00:00Z', change: words } },
    recent_changes: [{ id: 'erdos-0020', date: '2026-10-11', change: words }] })
  assert.equal(overlay.entries.get('erdos-0005').change, exact, 'the updater\'s 400 characters are kept whole')
  const cut = overlay.entries.get('erdos-0020').change
  assert.ok(cut.length <= 400 && cut.endsWith('…'), cut)
  assert.ok(words.startsWith(cut.slice(0, -1)) && words[cut.length - 1] === ' ', 'cut at a word boundary')
  assert.equal(overlay.recent_changes[0].change, cut)
})
test('signal quotes: optional for deterministic matches, required from Kimi K3, never Wikipedia text', () => {
  const signal = { title: 'openai/math withdrew a manuscript of family 159', url: 'https://github.com/openai/math/blob/fd4aeeb/history.md', date: '2026-10-07', relation: 'withdraws',
    source: 'openai-math', matched_by: 'deterministic', first_seen: '2026-10-11T06:15:00Z' }
  const overlay = parseStatusOverlay({ schema_version: 1, signals: {
    'erdos-0003': [signal],
    'erdos-0005': [{ ...signal, matched_by: 'kimi-k3' }, { ...signal, url: signal.url + '#short', matched_by: 'kimi-k3', evidence_quote: ' short ' }],
    'erdos-0020': [{ ...signal, source: 'wikipedia', matched_by: 'kimi-k3', relation: 'claims-resolution', url: 'https://en.wikipedia.org/w/index.php?diff=2&oldid=1',
      evidence_quote: 'In 2026 the conjecture was announced as proved with the help of an AI system.' }],
    'erdos-0242': [{ ...signal, evidence_quote: 42 }],
    'erdos-0006': [{ ...signal, source: 'wikipedia', matched_by: 'kimi-k3', relation: 'claims-resolution', url: 'https://en.wikipedia.org/w/index.php?diff=4&oldid=3' }],
  } })
  assert.deepEqual(overlay.signals.get('erdos-0003').map(item => item.evidence_quote), [''], 'a deterministic match without a quote is kept, as the updater writes it')
  assert.equal(overlay.signals.has('erdos-0005'), false, 'a Kimi K3 match must quote 8 to 240 characters')
  assert.deepEqual(overlay.signals.get('erdos-0020').map(item => [item.evidence_quote, item.url]), [['', 'https://en.wikipedia.org/w/index.php?diff=2&oldid=1']], 'Wikipedia prose is not republished; the diff link stays')
  assert.equal(overlay.signals.has('erdos-0242'), false, 'a quote that is not text drops the signal')
  assert.deepEqual(overlay.signals.get('erdos-0006').map(item => [item.evidence_quote, item.source]), [['', 'wikipedia']], 'the updater publishes Wikipedia reports without their quote')
  assert.equal(parseStatusOverlay(overlayFile).signals.get('erdos-0242').length, 1, 'the fixture\'s quote-less deterministic signal is shown')
})
test('a withdrawal re-sent with its claim retargets Analyze; one that does not name its claim leaves nothing to analyze', () => {
  const sha = 'b'.repeat(64)
  const claims = [{ kind: 'claim', actor: 'ai', scope: 'full', acceptance: 'preprint', title: 'Manuscript A', url: 'https://github.com/openai/math/tree/fd4aeeb/preprints/a' },
    { kind: 'claim', actor: 'ai', scope: 'full', acceptance: 'preprint', title: 'Manuscript B', url: 'https://github.com/openai/math/tree/fd4aeeb/preprints/b' }]
  const [base] = indexCatalogue({ schema_version: 3, total: 1, content_sha256: sha, sources: [], problems: [{ id: 'wiki-x', number: 'x', title: 'X', source: 'wikipedia',
    source_url: 'https://en.wikipedia.org/wiki/X', category: 'ai-proposed', category_rule: 'A2', status_evidence: claims }] })
  const withdrawal = { kind: 'withdrawal', actor: 'ai', claimant: 'OpenAI', scope: 'full', outcome: 'other', date: '2026-10-07', title: 'openai/math withdrew “A”',
    url: 'https://github.com/openai/math/blob/fd4aeeb/history.md', acceptance: 'withdrawn' }
  const apply = evidence => applyStatusOverlay([base], parseStatusOverlay({ schema_version: 1, catalogue_sha256: sha, entries: { 'wiki-x': { changed_at: '2026-10-11T06:15:00Z',
    change: 'openai/math withdrew a manuscript this entry cites (A)', badges_add: ['withdrawn-claim'], evidence_add: evidence } } }), sha).rows[0]
  assert.equal(analyzeEvidence(base).title, 'Manuscript A')
  const unnamed = apply([withdrawal])
  assert.equal(unnamed.category, 'ai-proposed')
  assert.equal(analyzeEvidence(unnamed), null, 'which manuscript was withdrawn is unknown, so none is offered')
  assert.equal(solveAction(unnamed), 'solve')
  const named = apply([withdrawal, { ...claims[0], acceptance: 'withdrawn' }])
  assert.equal(analyzeEvidence(named).title, 'Manuscript B')
  assert.equal(createSolvePayload(named, 1, { mode: 'analyze' }).analyze_url, 'https://github.com/openai/math/tree/fd4aeeb/preprints/b')
  assert.deepEqual(named.status_evidence.map(item => `${item.kind}:${item.acceptance}`), ['claim:withdrawn', 'claim:preprint', 'withdrawal:withdrawn'], 'the re-sent claim replaces the catalogue copy in place')
  assert.equal(base.status_evidence[0].acceptance, 'preprint', 'the catalogue row is not mutated')
})
test('overlay entries apply only to their catalogue and never add or remove rows', () => {
  const overlay = parseStatusOverlay(overlayFile)
  const { rows: updated, applied, recent } = applyStatusOverlay(v3, overlay, fixture.content_sha256)
  assert.equal(applied, true)
  assert.deepEqual(updated.map(item => item.id), v3.map(item => item.id))
  assert.deepEqual(categoryCounts(updated), overlayFile.category_counts)
  const proved = updated.find(item => item.id === 'erdos-0005')
  assert.equal(proved.category, 'solved')
  assert.equal(proved.category_rule, 'S1')
  assert.equal(proved.status_headline, 'Marked proved in the Erdős Problems database on October 10.')
  assert.equal(proved.overlay.from, 'open')
  assert.deepEqual(proved.badges, ['ai-assisted-solution'])
  const withdrawn = updated.find(item => item.id === 'erdos-0003')
  assert.equal(withdrawn.category, 'open', 'a withdrawal overrides at once')
  assert.deepEqual(withdrawn.badges, ['withdrawn-claim', 'formal-statement', 'prize'])
  assert.equal(withdrawn.status_evidence.at(-1).kind, 'withdrawal')
  assert.equal(solveAction(withdrawn), 'solve')
  const restored = updated.find(item => item.id === 'kourovka-20-003')
  assert.equal(restored.category, 'open')
  assert.deepEqual(restored.badges, ['not-in-current-edition'], 'a void entry changes nothing, not even its badges')
  const signalled = updated.find(item => item.id === 'clay-poincare-conjecture')
  assert.equal(signalled.signals.length, 1)
  assert.equal(signalled.badges[0], 'reported-signal')
  assert.equal(signalled.category, 'solved', 'signals never change the category')
  assert.deepEqual(recent.map(change => change.id), ['erdos-0005', 'erdos-0003'])
  assert.equal(v3.find(item => item.id === 'erdos-0005').category, 'open', 'the catalogue rows are not mutated')
  // another catalogue: only the signals are shown
  const other = applyStatusOverlay(v3, overlay, '953ae5db2db47cb7d84a4be61d5a92a8f055b3bacff251c066edf3a8bf86ece2')
  assert.equal(other.applied, false)
  assert.deepEqual(categoryCounts(other.rows), fixture.category_counts)
  assert.deepEqual(other.recent, [])
  assert.equal(other.rows.find(item => item.id === 'clay-poincare-conjecture').signals.length, 1)
  assert.equal(applyStatusOverlay(v3, null, fixture.content_sha256).rows, v3)
  assert.equal(applyStatusOverlay(v3, overlay, undefined).applied, false, 'a catalogue without content_sha256 never takes entries')
})
test('home counts follow the overlay only when it matches and adds up', () => {
  const overlay = parseStatusOverlay(overlayFile)
  assert.deepEqual(overlayCategoryCounts(overlay, fixture.content_sha256, 10), overlayFile.category_counts)
  assert.equal(overlayCategoryCounts(overlay, fixture.content_sha256, 11), null)
  assert.equal(overlayCategoryCounts(overlay, 'f'.repeat(64), 10), null)
  assert.equal(overlayCategoryCounts(parseStatusOverlay({ ...overlayFile, category_counts: { solved: -1 } }), fixture.content_sha256, 10), null)
})
test('status freshness: checked time, stale after two missed refreshes, source errors', () => {
  const overlay = parseStatusOverlay(overlayFile)
  const fresh = statusFreshness(overlay, Date.parse('2026-10-11T08:15:00Z'))
  assert.equal(fresh.checkedLabel, '2 hours ago')
  assert.equal(fresh.interval, 360)
  assert.equal(fresh.stale, false)
  assert.equal(fresh.errors, 1)
  assert.equal(statusFreshness(overlay, Date.parse('2026-10-12T00:16:00Z')).stale, true)
  assert.equal(statusFreshness(overlay, Date.parse('2026-10-11T06:15:20Z')).checkedLabel, 'just now')
  assert.equal(statusFreshness(parseStatusOverlay({ ...overlayFile, refresh_interval_minutes: 1 }), now).interval, 30)
  const bundled = statusFreshness(parseStatusOverlay({ ...overlayFile, last_checked_at: null, last_successful_refresh_at: null }), now)
  assert.equal(bundled.checkedLabel, null)
  assert.equal(bundled.stale, true)
  assert.equal(statusFreshness(null, now).stale, true)
})
test('page state round-trips through ?category=&source=&problem=&q=', () => {
  const sources = ['erdos', 'kourovka']
  assert.deepEqual(readProblemQuery('?category=ai-proposed&source=erdos&problem=erdos-0003&q=progressions', { sources }),
    { category: 'ai-proposed', source: 'erdos', problem: 'erdos-0003', q: 'progressions' })
  assert.deepEqual(readProblemQuery('?category=proved&source=nowhere&problem=../x', { sources }), { category: '', source: '', problem: '', q: '' })
  assert.equal(problemQueryString({ category: 'open', source: 'kourovka', q: ' group rings ', problem: 'kourovka-01-003' }), '?category=open&source=kourovka&q=group+rings&problem=kourovka-01-003')
  assert.equal(problemQueryString({}), '')
  assert.equal(readProblemQuery('?q=' + 'x'.repeat(300)).q.length, 200)
})
test('open and partial problems keep "Click to solve"; claimed solutions are analyzed', () => {
  assert.equal(solveAction(row('erdos-0005')), 'solve')
  assert.equal(solveAction(row('kourovka-07-038')), 'solve')
  assert.equal(solveAction(row('erdos-0003')), 'analyze')
  assert.equal(solveAction(row('clay-poincare-conjecture')), 'analyze')
  assert.equal(solveAction(row('erdos-0020')), null, 'a source marker alone is not a solution to analyze')
  assert.equal(solveAction(row('kourovka-03-020')), 'solve', 'an AI-proposed entry without a linked claim can still be attempted')
  assert.equal(primaryEvidence(row('clay-poincare-conjecture')).url, 'https://www.claymath.org/millennium/poincare-conjecture/')
  const writes = []
  assert.equal(prepareProblemForSolve(row('erdos-0003'), { setItem: (...args) => writes.push(args) }, 12345, { mode: 'analyze' }), '/sign-in#solve')
  assert.equal(writes[0][0], solveStorageKey)
  const payload = JSON.parse(writes[0][1])
  assert.equal(payload.version, 1)
  assert.equal(payload.mode, 'analyze')
  assert.equal(payload.analyze_url, 'https://github.com/openai/math/tree/fd4aeeb/preprints/159')
  assert.equal(payload.source_url, 'https://www.erdosproblems.com/3', 'source_url still names the problem')
  assert.equal(payload.statement_ready, true)
  assert.match(payload.context, /Catalogue status: AI proposed \(rule A2\)/)
  assert.throws(() => createSolvePayload(row('erdos-0020'), 1, { mode: 'analyze' }), /claimed solution/)
  const solve = createSolvePayload(row('erdos-0005'), 1)
  assert.equal(solve.mode, undefined)
  assert.equal(solve.analyze_url, undefined)
})
test('Analyze is offered only for a manuscript about the problem: arXiv, GitHub or a direct file, never an index or landing page', () => {
  const links = {
    'https://arxiv.org/abs/2406.05203': 'arxiv', 'https://arxiv.org/pdf/2304.08459.pdf': 'arxiv', 'https://arxiv.org/abs/math/0211159': 'arxiv', 'https://arxiv.org/abs/2509.22346v2': 'arxiv',
    'https://github.com/openai/math/tree/fd4aeeb/preprints/159': 'github', 'https://github.com/openai/NavierStokesAndEuler': 'github', 'https://raw.githubusercontent.com/owner/repo/main/paper.tex': 'github',
    'https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/kourovka_10.46_solution.pdf': 'https',
    'https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems/c8ad4309d20120c67cb97faa86daa1443acee018': null, 'https://zenodo.org/records/22721349': null,
    'https://doi.org/10.1007/s00605-025-02116-8': null, 'https://www.claymath.org/millennium/poincare-conjecture/': null, 'https://arxiv.org/list/math.GR/new': null,
    'https://github.com/openai/math/commit/fd4aeeb': null, 'https://github.com/openai': null, 'http://arxiv.org/abs/2406.05203': null, 'javascript:alert(1)': null,
  }
  for (const [url, connector] of Object.entries(links)) assert.equal(analyzeLink(url)?.connector ?? null, connector, url)
  assert.equal(analyzeEvidence(row('clay-poincare-conjecture')).url, 'https://arxiv.org/abs/math/0211159', 'the Clay prize page is evidence, the preprint is the manuscript')
  // a document cited as the answer to many problems is a collection, not a manuscript about one of them
  const pointer = { ...fixture.problems.find(item => item.id === 'kourovka-03-020'), status_changed: undefined }
  const cited = (id, category, url, more = []) => ({ ...pointer, id, category, category_rule: category === 'solved' ? 'S1' : 'A1', status_evidence: [...pointer.status_evidence,
    { kind: category === 'solved' ? 'accepted-result' : 'claim', actor: 'ai', scope: 'full', title: `Solution cited for ${id}`, url, acceptance: category === 'solved' ? 'accepted-by-maintainer' : 'preprint' }, ...more] })
  const compilation = 'https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/kourovka-mathematics.pdf'
  const paper = 'https://arxiv.org/abs/2607.17477'
  const solution = 'https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/kourovka_10.46_solution.pdf'
  const problems = [
    ...Array.from({ length: maxManuscriptProblems + 1 }, (_, index) => cited(`kourovka-09-${String(index).padStart(3, '0')}`, index % 2 ? 'solved' : 'ai-proposed', compilation)),
    ...Array.from({ length: maxManuscriptProblems }, (_, index) => cited(`kourovka-03-${String(index).padStart(3, '0')}`, 'solved', paper)),
    cited('kourovka-10-046', 'solved', solution),
    cited('kourovka-12-001', 'ai-proposed', 'https://zenodo.org/records/22721372'),
    cited('kourovka-12-002', 'solved', 'https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems/c8ad4309d20120c67cb97faa86daa1443acee018',
      [{ kind: 'accepted-result', actor: 'human', scope: 'full', title: 'The paper', url: 'https://arxiv.org/abs/2406.05203', acceptance: 'refereed' }]),
  ]
  const catalogue = indexCatalogue({ ...fixture, total: problems.length, problems })
  const find = id => catalogue.find(item => item.id === id)
  assert.equal(solveAction(find('kourovka-09-000')), 'solve', 'an AI-proposed problem whose only claim sits in a compilation can still be attempted')
  assert.equal(solveAction(find('kourovka-09-001')), null, 'a solved problem whose only answer is a compilation has nothing to analyze')
  assert.throws(() => createSolvePayload(find('kourovka-09-001'), 1, { mode: 'analyze' }), /manuscript link/)
  assert.equal(solveAction(find('kourovka-03-000')), 'analyze', `a paper answering ${maxManuscriptProblems} problems is still a paper`)
  assert.equal(createSolvePayload(find('kourovka-10-046'), 1, { mode: 'analyze' }).analyze_url, solution)
  assert.equal(analyzeLink(analyzeEvidence(find('kourovka-10-046')).url).connector, 'https', 'a direct file outside arXiv and GitHub (a member link)')
  assert.equal(solveAction(find('kourovka-12-001')), 'solve', 'a landing page is not a manuscript')
  assert.equal(analyzeEvidence(find('kourovka-12-002')).url, 'https://arxiv.org/abs/2406.05203', 'the first manuscript among the answers, past the wiki table')
  assert.equal(primaryEvidence(find('kourovka-12-002')).url.includes('/wiki/'), true, 'the timeline and dates still use the primary record')
})
test('the builder\'s manuscript verdict decides what Analyze is offered; counting citations is only the fallback', () => {
  const pointer = { ...fixture.problems.find(item => item.id === 'kourovka-03-020'), status_changed: undefined }
  const answered = (id, url, manuscript) => ({ ...pointer, id, category: 'solved', category_rule: 'S1', status_evidence: [...pointer.status_evidence,
    { kind: 'accepted-result', actor: 'ai', scope: 'full', title: `Solution cited for ${id}`, url, acceptance: 'accepted-by-maintainer', ...(manuscript === undefined ? {} : { manuscript }) }] })
  const wiki = 'https://raw.githubusercontent.com/wiki/teorth/erdosproblems/AI-contributions-to-Erd%C5%91s-problems.md'
  const paper = 'https://arxiv.org/abs/2607.17477'
  const problems = [
    // A raw wiki page looks like a GitHub file and few problems cite it: only the builder knows it is an index.
    answered('kourovka-04-001', wiki, false), answered('kourovka-04-002', wiki),
    // A verdict of true stands even where the page's own count would call the link a collection.
    ...Array.from({ length: maxManuscriptProblems + 1 }, (_, index) => answered(`kourovka-05-${String(index).padStart(3, '0')}`, paper, true)),
  ]
  const catalogue = indexCatalogue({ ...fixture, total: problems.length, problems })
  const find = id => catalogue.find(item => item.id === id)
  assert.equal(analyzeEvidence(find('kourovka-04-001')), null, 'the builder says the wiki is not a manuscript')
  assert.equal(solveAction(find('kourovka-04-001')), null)
  assert.equal(analyzeEvidence(find('kourovka-04-002')).url, wiki, 'without a verdict the page falls back to its count (two citations)')
  assert.equal(createSolvePayload(find('kourovka-05-010'), 1, { mode: 'analyze' }).analyze_url, paper, 'eleven citations, but the builder read it as a paper')
  assert.equal(cleanEvidence({ kind: 'claim', actor: 'ai', title: 'T', url: paper, manuscript: 'yes' }).manuscript, undefined, 'only true or false is a verdict')
  assert.equal(cleanEvidence({ kind: 'claim', actor: 'ai', title: 'T', url: paper, manuscript: false }).manuscript, false)
  // A record the overlay re-sends keeps the catalogue's verdict on its link.
  const sha = fixture.content_sha256
  const resent = { kind: 'accepted-result', actor: 'ai', scope: 'full', title: 'Solution cited for kourovka-04-001', url: wiki, acceptance: 'disputed' }
  const overlay = parseStatusOverlay({ schema_version: 1, catalogue_sha256: sha, entries: { 'kourovka-04-001': { changed_at: '2026-10-11T06:15:00Z', change: 'Disputed', evidence_add: [resent] } } })
  const updated = applyStatusOverlay(catalogue, overlay, sha).rows.find(item => item.id === 'kourovka-04-001')
  assert.deepEqual(updated.status_evidence.filter(item => item.url === wiki).map(item => [item.acceptance, item.manuscript]), [['disputed', false]])
  assert.equal(analyzeEvidence(updated), null)
})
test('the shipped catalogue gives every answer and claim the builder\'s verdict, and Analyze follows it', () => {
  if (shipped.schema_version !== 3) return
  for (const item of rows) {
    for (const record of item.status_evidence) {
      if (['accepted-result', 'claim', 'partial-result'].includes(record.kind)) assert.equal(typeof record.manuscript, 'boolean', `${item.id} ${record.url}`)
      else assert.equal(record.manuscript, undefined, `${item.id} ${record.kind}`)
    }
    const target = analyzeEvidence(item)
    if (target) assert.equal(target.manuscript, true, item.id)
  }
  assert(rows.some(item => analyzeEvidence(item)), 'some shipped entries offer Analyze')
})
test('shipped change dates come only with a category that rests on dated answers or claims', () => {
  if (shipped.schema_version !== 3) return
  const dated = rows.filter(item => item.status_changed)
  for (const item of dated) {
    assert.ok(['S2', 'A2', 'P2'].includes(item.category_rule), `${item.id}: a source marker's category has no change date`)
    assert.deepEqual(statusDate(item), { kind: 'changed', date: item.status_changed }, item.id)
  }
  assert.equal(rows.find(item => item.id === 'clay-navier-stokes-equation').status_changed, '2026-09-08', 'the date OpenAI announced its claim')
})
test('solve payload preserves account route and distinguishes statement readiness', () => {
  const detailed = row('erdos-0003')
  const pointer = row('erdos-0005')
  const writes = []
  assert.equal(prepareProblemForSolve(detailed, { setItem: (...args) => writes.push(args) }, 12345), '/sign-in#solve')
  const payload = JSON.parse(writes[0][1])
  assert.equal(payload.created_at, 12345)
  assert.equal(payload.statement, detailed.statement.text)
  assert.match(payload.context, /https:\/\//)
  const sourceOnly = createSolvePayload(pointer)
  assert.equal(sourceOnly.statement_ready, false)
  assert.equal(sourceOnly.statement, '')
  assert.equal(sourceOnly.source_url, pointer.source_url)
  assert.equal(statementReady(pointer), false)
})
test('storage failure and unsafe inputs cannot silently navigate or prepare a run', () => {
  const detailed = row('erdos-0003')
  assert.throws(() => prepareProblemForSolve(detailed, { setItem() { throw new Error('blocked') } }), /site storage/)
  assert.throws(() => createSolvePayload({ ...detailed, source_url: 'javascript:alert(1)' }), /HTTPS/)
  assert.throws(() => createSolvePayload({ ...detailed, source_url: 'https://user:secret@example.com/proof' }), /credentials/)
  assert.throws(() => createSolvePayload({ ...detailed, id: '../../private' }), /identifier/)
  assert.throws(() => createSolvePayload({ ...detailed, status_evidence: [{ ...detailed.status_evidence[1], url: 'javascript:alert(1)' }] }, 1, { mode: 'analyze' }), /claimed solution/)
  assert.deepEqual(sourceLinks([{ label: 'unsafe', url: 'javascript:alert(1)' }]), [])
  assert.deepEqual(sourceLinks([{ label: 'unsafe', url: 'https://user:secret@example.com/proof' }]), [])
})
test('solve payload bounds statement and contextual prose', () => {
  const payload = createSolvePayload({ ...row('erdos-0003'), statement: { kind: 'editorial', text: 'x'.repeat(9000) }, detail_note: 'y'.repeat(7000) })
  assert.equal(payload.statement.length, 6000)
  assert.equal(payload.context.length, 4000)
  assert(JSON.stringify(payload).length < 40000)
})

// The built catalogue itself (schema 3), read without the page helpers: the
// builder's counts, categories and the bundled overlay must agree.
const built = shipped
const builtOverlay = json('../docs/public/research/problem-status.json')
const builtDefaults = Object.fromEntries(built.sources.map(source => [source.id, source.defaults || {}]))
const builtRows = built.problems.map(problem => ({ ...builtDefaults[problem.source], ...problem }))
const builtById = Object.fromEntries(builtRows.map(problem => [problem.id, problem]))

test('schema 3: per-collection counts, categories and the bundled overlay agree', () => {
  assert.equal(built.schema_version, 3)
  assert.equal(builtRows.length, built.total)
  assert.equal(new Set(builtRows.map(problem => problem.id)).size, builtRows.length)
  for (const [source, count] of Object.entries(built.counts)) assert.equal(builtRows.filter(problem => problem.source === source).length, count)
  assert.deepEqual(built.categories.map(category => category.id), ['solved', 'ai-proposed', 'partial', 'open'])
  for (const category of built.categories) assert.equal(builtRows.filter(problem => problem.category === category.id).length, built.category_counts[category.id])
  assert(builtRows.every(problem => problem.status === problem.category && problem.source_url.startsWith('https://')))
  assert(builtRows.every(problem => Array.isArray(problem.badges) && problem.badges.every(badge => built.badges[badge])))
  assert(builtRows.filter(problem => problem.category !== 'open').every(problem => problem.status_evidence.length > 0))
  assert.equal(builtOverlay.schema_version, 1)
  assert.equal(builtOverlay.catalogue_sha256, built.content_sha256)
  assert.deepEqual(builtOverlay.entries, {})
  assert.deepEqual(builtOverlay.category_counts, built.category_counts)
})
test('named problems keep honest categories', () => {
  assert.equal(builtById['clay-navier-stokes-equation'].category, 'ai-proposed')
  assert.equal(builtById['clay-poincare-conjecture'].category, 'solved')
  assert.notEqual(problemStatus(builtById['clay-navier-stokes-equation']), 'Solved')
  assert.notEqual(problemStatus(builtById['clay-navier-stokes-equation']), 'Listed open')
  for (const id of ['clay-riemann-hypothesis', 'clay-p-vs-np', 'clay-hodge-conjecture', 'clay-birch-and-swinnerton-dyer-conjecture', 'clay-yang-mills-the-maths-gap'])
    assert.equal(builtById[id].category, 'open', id)
})
test('collection defaults are stored once and expand on read', () => {
  const compact = built.problems.find(problem => problem.id === 'erdos-0005')
  assert(!('source_name' in compact) && !('rating' in compact))
  assert.equal(builtById['erdos-0005'].source_name, 'Erdős Problems')
  assert.equal(builtById['erdos-0005'].rating.score, 2)
  assert.equal(builtById['clay-riemann-hypothesis'].rating.score, 1)
  assert.equal(rows.find(row => row.id === 'erdos-0005').source_name, 'Erdős Problems')
})

test('a summary that repeats the statement, or opens it, is shown once', () => {
  const statement = { kind: 'editorial', text: 'Let $G$ be a group.\nIs $G$ abelian?' }
  assert.equal(summaryRepeatsStatement({ summary: 'Let $G$ be a group. Is $G$ abelian?', statement }), true)
  assert.equal(summaryRepeatsStatement({ summary: 'Let $G$  be a group.', statement }), true, 'its first sentence, whatever the spacing')
  assert.equal(summaryRepeatsStatement({ summary: 'Is every group abelian?', statement }), false)
  assert.equal(summaryRepeatsStatement({ summary: 'Let $G$ be a group.', statement: { kind: 'pointer', text: statement.text } }), false, 'no statement is shown, so the summary stays')
  assert.ok(rows.some(summaryRepeatsStatement))
})
test('a solved card is dated by the answer, not by a wiki row that lists it', () => {
  const evidence = (date, manuscript, title) => ({ kind: 'accepted-result', actor: 'ai', scope: 'full', acceptance: 'broadly-accepted', date, manuscript, title, url: `https://example.org/${title}` })
  const solved = { category: 'solved', status_changed: null, status_as_of: '2026-10-11',
    status_evidence: [evidence('2026-06-09', false, 'wiki'), evidence('2026-05', true, 'paper')] }
  assert.equal(statusDate(solved).date, '2026-05')
})
test('shipped titles, headlines and evidence text: $…$ math typesets cleanly, a lone $ is a price, no colour verdicts', () => {
  const inline = /(?<![\\$])\$(?!\$)((?:\\.|[^$\n])+?)\$/g
  for (const item of rows) {
    const texts = [item.title, item.status_headline, ...item.aliases, ...item.status_evidence.flatMap(record => [record.title, record.note])]
    for (const value of texts.filter(Boolean)) {
      assert.doesNotMatch(renderMath(value), /katex-error/, `${item.id}: ${value}`)
      assert.doesNotMatch(value.replace(inline, ''), /\$(?!\d)/, `${item.id}: a dollar sign outside math must start a price: ${value}`)
      assert.doesNotMatch(value, /[\u{1F534}\u{1F535}\u{1F7E0}-\u{1F7EB}\u26AA\u26AB]/u, `${item.id}: ${value}`)
    }
  }
})
test('prizes keep their currency and read with thousands separators', () => {
  assert.deepEqual(['$10000', '$5000', '$1,000,000', '$500', '£25', '₹2000'].map(formatPrize), ['$10,000', '$5,000', '$1,000,000', '$500', '£25', '₹2,000'])
})
