// Problem catalogue helpers shared by the Problems page, the home section and its build-time loader.
// Compact finite-family indices keep six-figure catalogues out of Vue's deep
// reactivity. Full descriptions are materialized only for the visible cards.
export const ratingLabels = ['', 'Long-term theory', 'Literature first', 'Specialist exploration', 'Concrete experiments', 'Focused experiments']
const dsTopics = ['design theory', 'difference sets', 'finite groups']

// Exclusive categories, in tab order. The copy is fixed by the status taxonomy; "AI proposed" is never solved.
export const problemCategories = [
  { id: 'solved', label: 'Solved', description: 'Answered, and the answer is accepted (source of record, refereed publication, or broad expert acceptance).' },
  { id: 'ai-proposed', label: 'AI proposed', description: 'An AI system produced a claimed full solution that mathematicians have not yet confirmed. Treated as unsolved until confirmed.' },
  { id: 'partial', label: 'Partial', description: 'An explicitly stated part of the problem is answered; the rest remains open.' },
  { id: 'open', label: 'Open', description: 'No accepted answer. Partial results, human claims under review, and AI partial results are shown as badges.' },
]
const categoryIds = new Set(problemCategories.map(category => category.id))
export const categoryRules = {
  S1: { category: 'solved', label: 'Source-of-record marker' },
  S2: { category: 'solved', label: 'Refereed or broadly accepted full answer' },
  A1: { category: 'ai-proposed', label: 'Source marker for an unconfirmed AI solution' },
  A2: { category: 'ai-proposed', label: 'Documented AI-produced full claim' },
  P1: { category: 'partial', label: 'Source marker on a stated part' },
  P2: { category: 'partial', label: 'Accepted answer to an explicitly stated part' },
  O: { category: 'open', label: 'No accepted answer recorded' },
}
// Non-exclusive badges, in display priority. The catalogue may supply longer descriptions.
export const problemBadges = {
  'withdrawn-claim': { label: 'Withdrawn claim', description: 'A claimed solution was withdrawn; it no longer counts.' },
  disputed: { label: 'Disputed', description: 'A claim or its formal statement is publicly disputed.' },
  'sources-conflict': { label: 'Sources disagree', description: 'Primary sources record different statuses.' },
  'reported-signal': { label: 'New report (unreviewed)', description: 'A new primary-source report was matched automatically and has not been reviewed.' },
  'ai-confirmed': { label: 'AI solution confirmed', description: 'An AI-produced solution was confirmed by a named mathematician or the source.' },
  'ai-assisted-solution': { label: 'AI-assisted solution', description: 'The accepted solution was found with substantial help from an AI system.' },
  'ai-partial': { label: 'AI partial result', description: 'An AI system claims a part, special case or related result.' },
  'human-claim': { label: 'Claim under review', description: 'A person has claimed a solution that is not yet accepted.' },
  'lean-pending': { label: 'Lean proof awaiting review', description: 'A Lean proof exists upstream but has not been reviewed by a person.' },
  'formal-statement': { label: 'Lean statement', description: 'A Lean formalization of the statement exists upstream.' },
  prize: { label: 'Prize', description: 'The source lists a prize; see the source for its terms.' },
  'ambiguous-statement': { label: 'Ambiguous statement', description: 'The statement admits more than one reading; the category follows the reading cited.' },
  'not-in-current-edition': { label: 'Not in current edition', description: 'The problem no longer appears in the current edition of its source.' },
  'upstream-substatus': { label: 'Upstream sub-status', description: 'The source records a finer status for this open problem.' },
}
const badgeOrder = Object.keys(problemBadges)
const aiBadges = new Set(['ai-confirmed', 'ai-assisted-solution', 'ai-partial'])
const leanBadges = new Set(['formal-statement', 'lean-pending'])
export const evidenceLabels = {
  kind: { 'accepted-result': 'Accepted result', claim: 'Claim', 'partial-result': 'Partial result', withdrawal: 'Withdrawal', refutation: 'Refutation', 'source-marker': 'Source marker', formalization: 'Formalization', survey: 'Survey', other: 'Other' },
  actor: { human: 'Human', ai: 'AI', 'ai-assisted': 'AI-assisted', maintainer: 'Maintainer', unknown: 'Unknown author' },
  scope: { full: 'Full problem', part: 'Part', 'special-case': 'Special case', related: 'Related', variant: 'Variant' },
  outcome: { proof: 'Proof', disproof: 'Disproof', counterexample: 'Counterexample', independence: 'Independence', bound: 'Bound', construction: 'Construction', computation: 'Computation', other: 'Other' },
  acceptance: { refereed: 'Refereed', 'accepted-by-maintainer': 'Accepted by maintainer', 'broadly-accepted': 'Broadly accepted', preprint: 'Preprint', announced: 'Announced', 'lean-checked-unreviewed': 'Lean-checked, unreviewed', withdrawn: 'Withdrawn', refuted: 'Refuted', disputed: 'Disputed', unknown: 'Acceptance unknown' },
  date_precision: { day: 'day', month: 'month', year: 'year' },
}
export const signalLabels = {
  relation: { 'claims-resolution': 'Claims a resolution', 'claims-partial': 'Claims partial progress', disputes: 'Disputes a claim', withdraws: 'Withdraws a claim', formalization: 'Formalization', mentions: 'Mentions the problem' },
  source: { arxiv: 'arXiv', palomar: 'Palomar', 'openai-math': 'openai/math', news: 'News', wikipedia: 'Wikipedia', 'formal-conjectures': 'formal-conjectures' },
  matched_by: { 'kimi-k3': 'Matched by Kimi K3', deterministic: 'Deterministic match' },
}
// Enum lookups use own keys only, so "constructor" or "__proto__" never pass as a category, badge or label.
const has = (map, key) => typeof key === 'string' && Object.hasOwn(map, key)
const problemId = /^[a-z0-9][a-z0-9._-]{0,159}$/
const evidenceDate = /^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$/
const legacyCategories = { 'source-open': 'open', 'resolution-announced': 'ai-proposed', proved: 'solved', solved: 'solved' }
const dayMs = 86_400_000
export const recentDays = 30

export function safeUrl(value, max = 500) {
  if (typeof value !== 'string' || value.length > max) return null
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null } catch { return null }
}
const text = (value, max) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : ''
const isoTime = /^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/
const timestamp = value => typeof value === 'string' && isoTime.test(value) && Number.isFinite(Date.parse(value)) ? value : null
const integer = value => Number.isInteger(value) && value >= 0 ? value : null

// A validated evidence record, or null. Optional enums that fail validation are dropped; kind, actor and url are required.
export function cleanEvidence(item) {
  if (!item || typeof item !== 'object') return null
  const url = safeUrl(item.url)
  const title = text(item.title, 300)
  if (!url || !title || !has(evidenceLabels.kind, item.kind) || !has(evidenceLabels.actor, item.actor)) return null
  const clean = { kind: item.kind, actor: item.actor, title, url }
  for (const field of ['scope', 'outcome', 'acceptance', 'date_precision']) if (has(evidenceLabels[field], item[field])) clean[field] = item[field]
  if (typeof item.date === 'string' && evidenceDate.test(item.date)) clean.date = item.date
  for (const [field, max] of [['claimant', 200], ['venue', 200], ['note', 240]]) if (text(item[field], max)) clean[field] = text(item[field], max)
  const systems = Array.isArray(item.ai_systems) ? item.ai_systems.map(name => text(name, 80)).filter(Boolean).slice(0, 8) : []
  if (systems.length) clean.ai_systems = systems
  // The builder's verdict on an answer's link: a manuscript about this problem, or a collection, index or landing page.
  if (typeof item.manuscript === 'boolean') clean.manuscript = item.manuscript
  return clean
}
const cleanEvidenceList = (items, max = 40) => (Array.isArray(items) ? items : []).slice(0, max).map(cleanEvidence).filter(Boolean)
// Evidence dates as sortable instants: a month or year counts from its first day.
export function evidenceTime(date) {
  if (typeof date !== 'string' || !evidenceDate.test(date)) return NaN
  const [year, month = '01', day = '01'] = date.split('-')
  return Date.UTC(Number(year), Number(month) - 1, Number(day))
}
// Never more precise than the record states; `coarsest` caps the detail (cards show months).
export function formatEvidenceDate(date, precision, coarsest = 'day') {
  const time = evidenceTime(date)
  if (!Number.isFinite(time)) return 'Date not recorded'
  const levels = ['year', 'month', 'day']
  const stated = precision === 'year' || date.split('-').length === 1 ? 0 : precision === 'month' || date.split('-').length === 2 ? 1 : 2
  const level = levels[Math.min(stated, levels.indexOf(coarsest))]
  const options = level === 'year' ? { year: 'numeric' } : level === 'month' ? { year: 'numeric', month: 'short' } : { year: 'numeric', month: 'short', day: 'numeric' }
  return new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' }).format(time)
}
// Newest first; undated records keep their order at the end.
export function evidenceTimeline(items) {
  return (items || []).map((item, index) => ({ item, index, time: evidenceTime(item.date) }))
    .sort((a, b) => (Number.isFinite(b.time) ? b.time : -Infinity) - (Number.isFinite(a.time) ? a.time : -Infinity) || a.index - b.index)
    .map(entry => entry.item)
}

export function categoryInfo(id) { return problemCategories.find(category => category.id === id) || problemCategories.at(-1) }
export function badgeInfo(id, problem, catalogue) {
  if (!has(problemBadges, id)) return null
  const known = problemBadges[id]
  const supplied = catalogue?.badges && has(catalogue.badges, id) ? catalogue.badges[id] : null
  const description = text(supplied?.description, 300) || known.description
  if (id === 'upstream-substatus') {
    // The upstream state itself (e.g. teorth's "falsifiable"), from upstream_substatus or a one-phrase source_status.
    const fallback = typeof problem?.source_status === 'string' && /^[a-z][a-z ()-]{2,39}$/.test(problem.source_status) ? problem.source_status : ''
    const state = text(problem?.upstream_substatus, 60) || fallback
    return { id, label: state ? state.charAt(0).toUpperCase() + state.slice(1) : known.label, description }
  }
  return { id, label: known.label, description }
}
export function problemBadgeList(problem, catalogue, limit = Infinity) {
  return (problem.badges || []).map(id => badgeInfo(id, problem, catalogue)).filter(Boolean).slice(0, limit)
}

function collectionDefaults(sources) {
  const defaults = new Map()
  for (const source of Array.isArray(sources) ? sources : []) {
    if (source && typeof source.id === 'string' && source.defaults && typeof source.defaults === 'object' && !Array.isArray(source.defaults)) defaults.set(source.id, source.defaults)
  }
  return defaults
}
// A field present on the problem overrides the collection default. String defaults may use {number}, {id}, {title} and {source_locator}.
function expandDefaults(problem, defaults) {
  const row = { ...problem }
  for (const [key, value] of Object.entries(defaults || {})) {
    if (Object.hasOwn(problem, key) || ['__proto__', 'constructor', 'prototype'].includes(key)) continue
    row[key] = typeof value === 'string' ? value.replace(/\{(number|id|title|source_locator)\}/g, (_, field) => String(problem[field] ?? '')) : value
  }
  return row
}
const curated = row => statementReady(row) || Boolean(row.partial_results?.length || row.proof_claims?.length || row.detail_note) || row.status_evidence.some(item => item.kind !== 'source-marker')
const named = row => Boolean(row.aliases?.length) || / · |: /.test(row.title || '') || !['erdos', 'kourovka', 'difference-sets'].includes(row.source)

// Recompute the fields derived from status, so the overlay and the catalogue share one path.
function withDerived(row) {
  const known = new Set(row.badges)
  const evidenceAi = row.status_evidence.some(item => item.actor === 'ai' || item.actor === 'ai-assisted')
  // statusChanged ("changed in the last 30 days", "latest status change") is a change of category only: the
  // builder's status_changed (when the evidence behind the category first held) or the overlay's change of
  // category. A new claim or partial result on a problem whose category stayed the same is not a status change.
  const overlayChange = row.overlay && row.overlay.from && row.overlay.from !== row.category ? Date.parse(row.overlay.changed_at || '') : NaN
  const times = [evidenceTime(row.status_changed), overlayChange].filter(Number.isFinite)
  row.statusChanged = times.length ? Math.max(...times) : null
  row.aiInvolved = row.category === 'ai-proposed' || evidenceAi || [...known].some(id => aiBadges.has(id))
  row.leanLinked = Boolean(row.formal_statement_url) || row.status_evidence.some(item => item.kind === 'formalization') || [...known].some(id => leanBadges.has(id))
  row.notable = (curated(row) ? 8 : 0) + (row.prize ? 4 : 0) + (named(row) ? 2 : 0)
  row.searchText = [row.id, row.title, row.number, row.summary, row.statement?.text, row.status_headline, row.source_name, ...(row.topics || []), ...(row.aliases || [])]
    .filter(Boolean).join(' ').toLowerCase()
  row.foldedText = undefined
  return row
}
// Search ignores case, accents and the style of dashes and apostrophes, so "Erdos", "Szemeredi" and
// "Hadwiger-Nelson" find "Erdős", "Szemerédi" and "Hadwiger–Nelson". Only accented Latin letters, dashes and
// apostrophes are rewritten, and a row's text is folded the first time a search needs it, not while the page loads.
const foldable = /[\u00c0-\u024f\u1e00-\u1eff\u2010-\u2015\u2212\u2018\u2019\u02bc]/g
const folded = new Map()
const fold = character => {
  if (!folded.has(character)) folded.set(character, /[\u2010-\u2015\u2212]/.test(character) ? '-' : /[\u2018\u2019\u02bc]/.test(character) ? "'" : character.normalize('NFD')[0])
  return folded.get(character)
}
export const searchKey = value => String(value).toLowerCase().replace(foldable, fold)
// Areas are compared without case or hyphens ("Ramsey theory" is "ramsey theory", "knot-theory" is "knot theory").
// A catalogue has a few hundred distinct area names, so each is normalized once.
const topicKeys = new Map()
export function topicKey(value) {
  let key = topicKeys.get(value)
  if (key === undefined) topicKeys.set(value, key = String(value).trim().toLowerCase().replace(/[\s-]+/g, ' '))
  return key
}
const foldedText = row => {
  if (row.foldedText === undefined) row.foldedText = searchKey(row.searchText)
  return row.foldedText
}
function indexProblem(problem, schema, defaults) {
  const row = expandDefaults(problem, schema >= 3 ? defaults.get(problem.source) : null)
  // v1/v2 had only a status; in v3 an unknown category is shown as open, never upgraded.
  row.category = schema >= 3 ? (categoryIds.has(row.category) ? row.category : 'open') : has(legacyCategories, row.status) ? legacyCategories[row.status] : 'open'
  row.category_rule = has(categoryRules, row.category_rule) && categoryRules[row.category_rule].category === row.category ? row.category_rule : null
  row.status_headline = text(row.status_headline, 400) || null
  row.status_changed = schema >= 3 && typeof row.status_changed === 'string' && evidenceDate.test(row.status_changed) ? row.status_changed : null
  row.status_evidence = schema >= 3 ? cleanEvidenceList(row.status_evidence) : []
  // A category set by a decision names who made it, when and why.
  const decision = schema >= 3 && row.decision && typeof row.decision === 'object' ? row.decision : null
  row.decision = decision && text(decision.by, 80) && typeof decision.at === 'string' && evidenceDate.test(decision.at) && text(decision.note, 400)
    ? { by: text(decision.by, 80), at: decision.at, note: text(decision.note, 400) } : null
  row.topics = Array.isArray(row.topics) ? row.topics.filter(topic => typeof topic === 'string') : []
  row.topicKeys = [...new Set(row.topics.map(topicKey))]
  row.aliases = Array.isArray(row.aliases) ? row.aliases.map(alias => text(alias, 200)).filter(Boolean) : []
  row.formal_statement_url = safeUrl(row.formal_statement_url) || null
  const badges = new Set((schema >= 3 && Array.isArray(row.badges) ? row.badges : []).filter(id => has(problemBadges, id) && id !== 'reported-signal'))
  if (row.prize) badges.add('prize')
  if (row.formal_statement_url) badges.add('formal-statement')
  row.badges = badgeOrder.filter(id => badges.has(id))
  return withDerived(row)
}

export function indexCatalogue(data) {
  if (!data || ![1, 2, 3].includes(data.schema_version) || !Array.isArray(data.problems)) throw new Error('Unsupported problem catalogue. Please refresh.')
  const defaults = collectionDefaults(data.sources)
  const rows = data.problems.map(problem => indexProblem(problem, data.schema_version, defaults))
  for (const family of data.compact_families || []) {
    if (family.encoding !== 'difference-sets-v1') throw new Error('Unknown problem family. Please refresh.')
    for (const tuple of family.cases) {
      const [v, k, lambda, group] = tuple
      const number = [v, k, lambda, ...group].join('-')
      rows.push(withDerived({ id: `difference-sets-${number}`, number, source: 'difference-sets', kind: 'parameter-case',
        topics: dsTopics, tuple, rating: { score: v <= 1000 ? 4 : 3 }, category: 'open', category_rule: null, badges: [], status_evidence: [], aliases: [],
        title: `Difference set (${v}, ${k}, ${lambda})`, summary: group.map(n => `Z/${n}Z`).join(' × ') }))
    }
  }
  if (rows.length !== data.total) throw new Error('The problem catalogue is incomplete. Please refresh.')
  markCollections(rows)
  return rows
}
// A link cited as the claim or answer for many problems is a collection (a wiki table, a compilation of
// solutions), not a manuscript about any one of them, so it is never offered to Analyze. The catalogue builder
// decides this per answer (`manuscript`: true or false), knowing indexes such as wikis and statement collections;
// for records without its verdict (older catalogues) the page counts the problems citing each link.
export const maxManuscriptProblems = 10
const answerKinds = new Set(['accepted-result', 'claim', 'partial-result'])
function markCollections(rows) {
  const citing = new Map()
  for (const row of rows) for (const item of row.status_evidence) {
    if (answerKinds.has(item.kind)) citing.set(item.url, (citing.get(item.url) || new Set()).add(row.id))
  }
  for (const row of rows) for (const item of row.status_evidence) {
    if (typeof item.manuscript !== 'boolean' && citing.get(item.url)?.size > maxManuscriptProblems) item.collection = true
  }
}
const aboutThisProblem = item => typeof item.manuscript === 'boolean' ? item.manuscript : !item.collection
export function categoryCounts(rows) {
  const counts = Object.fromEntries(problemCategories.map(category => [category.id, 0]))
  for (const row of rows) counts[row.category] = (counts[row.category] || 0) + 1
  return counts
}

export const solveStorageKey = 'ansatze.solveProblem.v1'
const statusLabels = { 'source-open': 'Listed open', 'resolution-announced': 'Resolution announced', 'proved': 'Proved', 'solved': 'Solved', 'ai-proposed': 'AI proposed', partial: 'Partial', open: 'Open' }
export function problemStatus(problem) {
  return has(statusLabels, problem.status) ? statusLabels[problem.status] : problem.source_status || 'Source status recorded'
}
export function statementReady(problem) {
  return ['exact', 'editorial'].includes(problem.statement?.kind) && Boolean(problem.statement?.text?.trim())
}
// Many researched summaries are the statement itself, or its first sentence; the detail panel then shows the statement only.
export function summaryRepeatsStatement(problem) {
  const plain = value => String(value || '').replace(/\s+/g, ' ').trim().replace(/…$/, '')
  const summary = plain(problem.summary)
  return statementReady(problem) && Boolean(summary) && plain(problem.statement.text).startsWith(summary)
}
// A prize keeps the source's currency and amount; long amounts get thousands separators ("$10000" → "$10,000").
export const formatPrize = value => String(value || '').replace(/([$£€¥₹])(\d{4,})(?![\d,.])/g, (_, symbol, digits) => symbol + Number(digits).toLocaleString('en-US'))
export function sourceLinks(items) {
  return (items || []).filter(item => {
    try { const url = new URL(item.url); return url.protocol === 'https:' && !url.username && !url.password && Boolean(item.label) } catch { return false }
  })
}
const isVoid = item => ['withdrawn', 'refuted'].includes(item.acceptance) || ['withdrawal', 'refutation'].includes(item.kind)
// The records a claimed solution can be read from, best first: the full claims behind "AI proposed", the accepted results behind "Solved".
function answerRecords(problem) {
  const evidence = (problem.status_evidence || []).filter(item => !isVoid(item))
  const full = item => !item.scope || item.scope === 'full'
  if (problem.category === 'ai-proposed') return [...evidence.filter(item => item.kind === 'claim' && full(item) && ['ai', 'ai-assisted'].includes(item.actor)),
    ...evidence.filter(item => item.kind === 'claim' && full(item) && !['ai', 'ai-assisted'].includes(item.actor))]
  // A source-of-record marker (S1) says the problem is solved, not which claim was accepted: a claim the catalogue
  // listed while the problem was open (one under review, say) is not offered as the solution.
  if (problem.category === 'solved') return [...evidence.filter(item => item.kind === 'accepted-result' && full(item)),
    ...(problem.category_rule === 'S1' ? [] : evidence.filter(item => item.kind === 'claim' && full(item)))]
  return []
}
export function primaryEvidence(problem) { return answerRecords(problem)[0] || null }
const arxivPaper = /^\/(?:abs|pdf|e-print|src|format)\/(?:\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[a-z]{2})?\/\d{7})(?:v\d{1,3})?(?:\.pdf)?\/?$/i
// A link the workspace's Analyze connectors read as a manuscript: an arXiv paper, a GitHub repository, folder or
// file, or a direct PDF, TeX or archive file elsewhere (that last one needs a member account). Wiki pages,
// landing pages (a DOI, a Zenodo record) and other web pages are not manuscripts.
export function analyzeLink(value) {
  const href = safeUrl(value)
  if (!href) return null
  const { hostname, pathname } = new URL(href)
  const parts = pathname.split('/').filter(Boolean)
  if (['arxiv.org', 'www.arxiv.org', 'export.arxiv.org'].includes(hostname)) return arxivPaper.test(pathname) ? { href, connector: 'arxiv' } : null
  if (hostname === 'raw.githubusercontent.com') return parts.length >= 4 ? { href, connector: 'github' } : null
  if (['github.com', 'www.github.com'].includes(hostname)) return parts.length === 2 || (['blob', 'tree', 'raw'].includes(parts[2]) && parts.length >= 4) ? { href, connector: 'github' } : null
  return /\.(?:pdf|tex|zip|tar\.gz|tgz)$/i.test(pathname) ? { href, connector: 'https' } : null
}
// The claimed solution to hand to Analyze: the best answer record that is a manuscript about this problem.
// A live withdrawal that does not say which claim it covers leaves nothing to analyze, because the withdrawn
// manuscript may be any of them; the updater names it by re-sending that claim as withdrawn.
export function analyzeEvidence(problem) {
  if (problem.overlay?.unnamedWithdrawal) return null
  return answerRecords(problem).find(item => aboutThisProblem(item) && analyzeLink(item.url)) || null
}
// The record behind the current category, for its date: the answer or claim, the answered part, or the source marker.
export function categoryEvidence(problem) {
  const live = (problem.status_evidence || []).filter(item => !isVoid(item))
  const latest = items => items.reduce((best, item) => !best || evidenceTime(item.date) > evidenceTime(best.date) ? item : best, null)
  const marker = () => latest(live.filter(item => item.kind === 'source-marker' && item.date))
  if (problem.category === 'open') return null
  if (problem.category === 'partial') return latest(live.filter(item => ['part', 'special-case'].includes(item.scope) && item.date)) || marker()
  // The answer itself dates the category; a wiki row or an index that lists the answer is dated by its own edit.
  const answers = answerRecords(problem)
  const primary = answers.find(item => aboutThisProblem(item) && item.date) || answers[0]
  return primary?.date ? primary : marker()
}
// The date a card shows: when the status changed (overlay, then the builder's date), when the category's evidence
// is dated, else when it was checked. A source marker's date is the source record's ("as of"), not a change.
export function statusDate(problem) {
  if (problem.overlay && problem.overlay.from !== problem.category) return { kind: 'changed', date: problem.overlay.changed_at.slice(0, 10), precision: 'day' }
  if (problem.status_changed) return { kind: 'changed', date: problem.status_changed }
  const item = categoryEvidence(problem)
  if (item?.date) return { kind: item.kind === 'source-marker' ? 'source' : 'evidence', date: item.date, precision: item.date_precision }
  const checked = typeof problem.status_as_of === 'string' && evidenceDate.test(problem.status_as_of.slice(0, 10)) ? problem.status_as_of.slice(0, 10) : null
  return checked ? { kind: 'checked', date: checked, precision: 'day' } : null
}
export function solveAction(problem) {
  if (['solved', 'ai-proposed'].includes(problem.category) && analyzeEvidence(problem)) return 'analyze'
  return problem.category === 'solved' ? null : 'solve'
}
const evidenceLine = item => `${evidenceLabels.kind[item.kind]} (${[evidenceLabels.actor[item.actor], evidenceLabels.acceptance[item.acceptance], item.date].filter(Boolean).join(', ')}): ${item.title}\n${item.url}`
export function createSolvePayload(problem, now = Date.now(), { mode = 'solve' } = {}) {
  const id = String(problem.id || '')
  if (!problemId.test(id)) throw new Error('This problem has no valid catalogue identifier.')
  let source
  try { source = new URL(problem.source_url) } catch { throw new Error('The original problem source is unavailable.') }
  if (source.protocol !== 'https:' || source.username || source.password) throw new Error('The original problem must have an HTTPS source without embedded credentials.')
  let analyzeUrl = null
  if (mode === 'analyze') {
    analyzeUrl = ['solved', 'ai-proposed'].includes(problem.category) ? analyzeLink(analyzeEvidence(problem)?.url)?.href : null
    if (!analyzeUrl) throw new Error('This problem has no claimed solution with a manuscript link to analyze.')
  }
  const ready = statementReady(problem)
  const category = problem.status_evidence?.length || problem.category_rule ? categoryInfo(problem.category) : null
  const context = [
    category ? `Catalogue status: ${category.label}${problem.category_rule ? ` (rule ${problem.category_rule})` : ''}${problem.status_headline ? ` — ${problem.status_headline}` : ''}` : '',
    ...(problem.status_evidence || []).slice(0, 6).map(evidenceLine),
    problem.detail_note || '',
    ...(problem.partial_results || []).map(item => `Known partial result: ${item.title}\nStatus: ${item.status || 'See cited source'}\n${item.text}\n${sourceLinks(item.sources).map(link => `${link.label}: ${link.url}`).join('\n')}`),
    ...(problem.proof_claims || []).map(item => `Proof claim or announcement: ${item.title}\nStatus: ${item.status || 'Claim; see cited source'}\n${item.text}\n${sourceLinks(item.sources).map(link => `${link.label}: ${link.url}`).join('\n')}`),
  ].filter(Boolean).join('\n\n').slice(0, 4000)
  return { version: 1, ...(analyzeUrl ? { mode: 'analyze', analyze_url: analyzeUrl } : {}), problem_id: id, title: String(problem.title || id).slice(0, 240),
    statement: ready ? problem.statement.text.slice(0, 6000) : '',
    source_url: source.href, source_status: String(problem.source_status || problemStatus(problem)).slice(0, 500),
    context, statement_ready: ready, created_at: now }
}
export function prepareProblemForSolve(problem, storage, now = Date.now(), options = {}) {
  const payload = createSolvePayload(problem, now, options)
  try { storage.setItem(solveStorageKey, JSON.stringify(payload)) }
  catch { throw new Error('Your browser could not save this problem for the workspace. Allow site storage and try again.') }
  return '/sign-in#solve'
}

export function presentProblem(row) {
  if (!row.tuple) return row
  const [v, k, lambda, group] = row.tuple
  const name = group.map(n => `Z/${n}Z`).join(' × ')
  return { ...row, title: `Difference set (${v}, ${k}, ${lambda}) in ${name}`, source_name: 'Difference Sets',
    summary: `Does this specific abelian group contain a ${k}-element subset in which every nonzero group element occurs exactly ${lambda} times as an ordered difference? The archived database labels this case Open.`,
    parameters: { v, k, lambda, group }, source_url: 'https://dmgordon.org/difference-sets/',
    source_locator: `DS(${v},${k},${lambda},[${group.join(',')}]) in ds.json`,
    status_as_of: '2026-09-10', source_status: 'Open in archived maintainer database; literature may contain later results',
    formal_status: 'No proof asserted by this catalogue',
    additional_sources: [{ label: 'Maintainer data · locate the DS identifier above', url: 'https://raw.githubusercontent.com/dmgordo/difference-sets/main/ds.json' }],
    rating: { score: row.rating.score, label: ratingLabels[row.rating.score], confidence: 'heuristic',
      reason: `Existence in this particular group is a finite construction target. Group order ${v.toLocaleString('en-US')} ${v <= 1000 ? 'meets' : 'exceeds'} the 1,000-element exploration threshold. This estimates an exploration starting point, not the cost or likelihood of finding a proof.` } }
}

// Round-robin across collections, keeping each collection's own order.
function interleave(rows) {
  const groups = new Map()
  for (const row of rows) {
    if (!groups.has(row.source)) groups.set(row.source, [])
    groups.get(row.source).push(row)
  }
  const balanced = []
  for (let index = 0; balanced.length < rows.length; index++) {
    for (const group of groups.values()) if (group[index]) balanced.push(group[index])
  }
  return balanced
}
export const recentlyChanged = (row, now = Date.now()) => row.statusChanged !== null && row.statusChanged <= now + dayMs && now - row.statusChanged <= recentDays * dayMs
export const problemSorts = [
  { id: 'notable', label: 'Notable first' },
  { id: 'changed', label: 'Latest status change' },
  { id: 'source', label: 'Source order' },
]
export function filterProblems(rows, { search = '', source = '', topic = '', category = '', prizeOnly = false, ai = false, lean = false, changed = false, sort = 'notable', now = Date.now() } = {}) {
  const words = searchKey(search).trim().split(/\s+/).filter(Boolean)
  const area = topic && topicKey(topic)
  const selected = rows.filter(row => (!category || row.category === category) && (!source || row.source === source) && (!area || (row.topicKeys || row.topics.map(topicKey)).includes(area)) &&
    (!prizeOnly || row.prize) && (!ai || row.aiInvolved) && (!lean || row.leanLinked) && (!changed || recentlyChanged(row, now)) &&
    (!words.length || words.every(word => foldedText(row).includes(word))))
  if (sort === 'changed') return selected.map((row, index) => ({ row, index })).sort((a, b) => (b.row.statusChanged ?? -Infinity) - (a.row.statusChanged ?? -Infinity) || a.index - b.index).map(entry => entry.row)
  if (sort !== 'notable') return selected
  // Curated, prize and named entries first; within a tier, alternate collections, then recent status changes lead.
  const tiers = new Map()
  for (const row of selected) {
    const score = row.notable * 2 + (recentlyChanged(row, now) ? 1 : 0)
    if (!tiers.has(score)) tiers.set(score, [])
    tiers.get(score).push(row)
  }
  return [...tiers.keys()].sort((a, b) => b - a).flatMap(score => interleave(tiers.get(score)))
}
// Tab counts for the current view: every filter applies except the category itself, so each tab shows
// how many problems it would list.
export function categoryTabCounts(rows, filters = {}) {
  const matching = filterProblems(rows, { ...filters, category: '', sort: 'source' })
  return { all: matching.length, ...categoryCounts(matching) }
}

// Page state that belongs in the address bar: ?category=&source=&problem=&q=
export function readProblemQuery(search, { sources = [] } = {}) {
  const params = new URLSearchParams(search)
  const category = params.get('category'), source = params.get('source'), problem = params.get('problem')
  return {
    category: categoryIds.has(category) ? category : '',
    source: sources.includes(source) ? source : '',
    problem: problemId.test(problem || '') ? problem : '',
    q: (params.get('q') || '').slice(0, 200),
  }
}
export function problemQueryString({ category = '', source = '', problem = '', q = '' } = {}) {
  const params = new URLSearchParams()
  if (categoryIds.has(category)) params.set('category', category)
  if (source) params.set('source', source)
  if (q.trim()) params.set('q', q.trim().slice(0, 200))
  if (problemId.test(problem)) params.set('problem', problem)
  const query = params.toString()
  return query ? `?${query}` : ''
}

// The live status overlay (/research/problem-status.json): parsed defensively, applied without adding or removing rows.
export const maxOverlayChars = 2_000_000
const overlayLimits = { entries: 20_000, signalProblems: 5_000, signalsPerProblem: 10, evidencePerEntry: 12, badgesPerEntry: 16, recentChanges: 50, sources: 40 }
function cleanBadgeList(value) { return [...new Set((Array.isArray(value) ? value : []).slice(0, overlayLimits.badgesPerEntry).filter(id => has(problemBadges, id) && id !== 'reported-signal'))] }
// Long prose is shortened at a word and the cut is marked, so a sentence never stops mid-word unannounced.
function clip(value, max) {
  const full = text(value, 20_000)
  if (full.length <= max) return full
  const cut = full.slice(0, max - 1)
  const space = cut.lastIndexOf(' ')
  return (space > max / 2 ? cut.slice(0, space) : cut).trimEnd() + '…'
}
// The overlay changes categories only from source-of-record markers (S1, A1, P1) and back to Open (O); research
// rules (S2, A2, P2) come only from the reviewed catalogue.
const overlayRules = new Set(['S1', 'A1', 'P1', 'O'])
// An entry is applied whole or not at all: a category change the overlay may not make, or a move out of Open
// that carries no valid source-marker evidence, voids the entry (fail closed) rather than half-applying it.
function cleanEntry(entry) {
  if (!entry || typeof entry !== 'object' || !timestamp(entry.changed_at)) return null
  const clean = { changed_at: entry.changed_at, change: clip(entry.change, 400) || null, badges_add: cleanBadgeList(entry.badges_add), badges_remove: cleanBadgeList(entry.badges_remove),
    evidence_add: cleanEvidenceList(entry.evidence_add, overlayLimits.evidencePerEntry), status_headline: text(entry.status_headline, 280) || null }
  if (entry.category === undefined && entry.category_rule === undefined) return clean
  if (!categoryIds.has(entry.category) || !overlayRules.has(entry.category_rule) || categoryRules[entry.category_rule].category !== entry.category) return null
  if (entry.category !== 'open' && !clean.evidence_add.some(item => item.kind === 'source-marker' && !isVoid(item))) return null
  return Object.assign(clean, { category: entry.category, category_rule: entry.category_rule })
}
// Quotes: optional for deterministic matches (the updater omits an empty one); a Kimi K3 match must quote 8–240
// characters of the fetched text. Wikipedia prose is CC BY-SA and is never republished here: its diff link stays.
function cleanSignal(signal) {
  if (!signal || typeof signal !== 'object') return null
  const url = safeUrl(signal.url), title = text(signal.title, 300)
  const raw = signal.evidence_quote ?? ''
  if (!url || !title || typeof raw !== 'string' || raw.length > 240 || !has(signalLabels.relation, signal.relation) || !has(signalLabels.source, signal.source) || !has(signalLabels.matched_by, signal.matched_by)) return null
  const quote = raw.replace(/\s+/g, ' ').trim()
  // Kimi K3 matches must quote their source (8-240 characters), except Wikipedia edits:
  // their quote stays in the updater's private state and only the diff link is published.
  if (signal.matched_by === 'kimi-k3' && signal.source !== 'wikipedia' && quote.length < 8) return null
  const date = typeof signal.date === 'string' && (evidenceDate.test(signal.date) || timestamp(signal.date)) ? signal.date : null
  return { title, url, date, relation: signal.relation, source: signal.source, matched_by: signal.matched_by, evidence_quote: signal.source === 'wikipedia' ? '' : quote, first_seen: timestamp(signal.first_seen) }
}
const ownEntries = value => value && typeof value === 'object' && !Array.isArray(value) ? Object.entries(value) : []
export function parseStatusOverlay(value) {
  if (!value || typeof value !== 'object' || value.schema_version !== 1) return null
  const entries = new Map(), voided = new Set()
  for (const [id, entry] of ownEntries(value.entries).slice(0, overlayLimits.entries)) {
    const clean = problemId.test(id) ? cleanEntry(entry) : null
    if (clean) entries.set(id, clean)
    else voided.add(id)
  }
  const signals = new Map()
  for (const [id, list] of ownEntries(value.signals).slice(0, overlayLimits.signalProblems)) {
    const clean = problemId.test(id) && Array.isArray(list) ? list.slice(0, overlayLimits.signalsPerProblem).map(cleanSignal).filter(Boolean) : []
    if (clean.length) signals.set(id, clean)
  }
  const counts = value.category_counts && problemCategories.every(category => integer(value.category_counts[category.id]) !== null)
    ? Object.fromEntries(problemCategories.map(category => [category.id, value.category_counts[category.id]])) : null
  return {
    schema_version: 1,
    catalogue_sha256: typeof value.catalogue_sha256 === 'string' && /^[0-9a-f]{64}$/.test(value.catalogue_sha256) ? value.catalogue_sha256 : null,
    generated_at: timestamp(value.generated_at), last_checked_at: timestamp(value.last_checked_at), last_successful_refresh_at: timestamp(value.last_successful_refresh_at),
    refresh_interval_minutes: Math.max(30, Math.min(10_080, Number(value.refresh_interval_minutes) || 360)),
    sources: (Array.isArray(value.sources) ? value.sources : []).slice(0, overlayLimits.sources).filter(source => source && text(source.id, 40))
      .map(source => ({ id: text(source.id, 40), revision: text(String(source.revision ?? ''), 80) || null, checked_at: timestamp(source.checked_at), ok: source.ok === true })),
    entries, signals,
    // A change whose entry was voided is not announced either.
    recent_changes: (Array.isArray(value.recent_changes) ? value.recent_changes : []).slice(0, overlayLimits.recentChanges)
      .filter(change => change && problemId.test(change.id || '') && !voided.has(change.id) && timestamp(change.date) && text(change.change, 400))
      .map(change => ({ id: change.id, date: change.date, change: clip(change.change, 400), source: text(change.source, 40) || null })),
    category_counts: counts,
    model: value.model && typeof value.model === 'object' ? { name: text(value.model.name, 40) || null, calls_today: integer(value.model.calls_today), daily_cap: integer(value.model.daily_cap) } : null,
    source_errors: (Array.isArray(value.source_errors) ? value.source_errors : []).slice(0, overlayLimits.sources).filter(error => error && text(error.source_id, 40))
      .map(error => ({ source_id: text(error.source_id, 40), error: text(error.error, 300) || 'Source check failed' })),
  }
}
export function overlayMatches(overlay, catalogueSha) {
  return Boolean(overlay?.catalogue_sha256 && catalogueSha && overlay.catalogue_sha256 === catalogueSha)
}
// Entries apply only to the catalogue they were computed against; signals are attached to known rows either way.
export function applyStatusOverlay(rows, overlay, catalogueSha) {
  if (!overlay) return { rows, applied: false, recent: [] }
  const applied = overlayMatches(overlay, catalogueSha)
  const known = new Set()
  let changed = false
  const next = rows.map(row => {
    known.add(row.id)
    const entry = applied ? overlay.entries.get(row.id) : null
    const signals = overlay.signals.get(row.id)
    if (!entry && !signals) return row
    changed = true
    const updated = { ...row, badges: [...row.badges], status_evidence: [...row.status_evidence] }
    if (entry) {
      // A withdrawal that does not re-send the claim it covers (marked withdrawn) leaves unclear which claim is void.
      const unnamedWithdrawal = entry.evidence_add.some(item => item.kind === 'withdrawal')
        && !entry.evidence_add.some(item => ['claim', 'accepted-result'].includes(item.kind) && ['withdrawn', 'refuted'].includes(item.acceptance))
      updated.overlay = { changed_at: entry.changed_at, change: entry.change, from: row.category, unnamedWithdrawal }
      if (entry.category) Object.assign(updated, { category: entry.category, category_rule: entry.category_rule, status: entry.category })
      if (entry.status_headline) updated.status_headline = entry.status_headline
      // The overlay is newer: a record with the same kind and link replaces the catalogue's copy (a claim re-sent as withdrawn).
      // The link is the same, so the catalogue's verdict on it (manuscript or collection) carries over.
      const at = new Map(updated.status_evidence.map((item, index) => [`${item.kind} ${item.url}`, index]))
      for (const item of entry.evidence_add) {
        const key = `${item.kind} ${item.url}`, index = at.get(key)
        if (index === undefined) { at.set(key, updated.status_evidence.length); updated.status_evidence.push(item); continue }
        const { manuscript, collection } = updated.status_evidence[index]
        updated.status_evidence[index] = { ...item, ...(typeof manuscript === 'boolean' ? { manuscript } : {}), ...(collection ? { collection } : {}) }
      }
      const badges = new Set([...updated.badges, ...entry.badges_add].filter(id => !entry.badges_remove.includes(id)))
      updated.badges = badgeOrder.filter(id => badges.has(id))
    }
    if (signals) {
      updated.signals = signals
      if (!updated.badges.includes('reported-signal')) updated.badges = badgeOrder.filter(id => id === 'reported-signal' || updated.badges.includes(id))
    }
    return withDerived(updated)
  })
  const recent = applied ? overlay.recent_changes.filter(change => known.has(change.id)) : []
  // An overlay that changes no row (the bundled one is empty) keeps the same list, so nothing downstream recomputes.
  return { rows: changed ? next : rows, applied, recent }
}
// Home counts: the overlay's counts only when they belong to this catalogue and add up to its total.
export function overlayCategoryCounts(overlay, catalogueSha, total) {
  if (!overlayMatches(overlay, catalogueSha) || !overlay.category_counts) return null
  const sum = problemCategories.reduce((value, category) => value + overlay.category_counts[category.id], 0)
  return sum === total ? overlay.category_counts : null
}

export function relativeTime(then, now = Date.now()) {
  const seconds = Math.round((then - now) / 1000)
  const format = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  for (const [unit, size] of [['day', 86_400], ['hour', 3_600], ['minute', 60]]) if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit)
  return 'just now'
}
export function statusFreshness(overlay, now = Date.now()) {
  const interval = overlay?.refresh_interval_minutes || 360
  const checked = Date.parse(overlay?.last_checked_at || '')
  const success = Date.parse(overlay?.last_successful_refresh_at || '')
  return {
    interval,
    checked: Number.isFinite(checked) ? overlay.last_checked_at : null,
    checkedLabel: Number.isFinite(checked) ? relativeTime(Math.min(checked, now), now) : null,
    success: Number.isFinite(success) ? overlay.last_successful_refresh_at : null,
    stale: !Number.isFinite(success) || now - success > Math.max(90, interval * 2) * 60_000,
    errors: overlay?.source_errors?.length || 0,
  }
}
