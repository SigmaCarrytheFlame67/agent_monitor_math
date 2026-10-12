export const newsCategories = [
  { id: 'mathematics', label: 'Mathematics' },
  { id: 'models', label: 'Models' },
  { id: 'research', label: 'Research' },
]
export const newsRoles = { primary: 'Primary source', journalism: 'Journalism', commentary: 'Commentary', preprint: 'Preprint', repository: 'Repository' }
export const problemRelations = {
  mentions: 'Mentions', 'claims-partial-progress': 'Claims partial progress', 'claims-resolution': 'Claims a resolution',
  'claims-counterexample': 'Claims a counterexample', 'disputes-claim': 'Disputes a claim', 'withdraws-claim': 'Withdraws a claim',
  formalization: 'Formalization',
}
// Who wrote a card's summary: the editors (reviewed record), Kimi K3, or the source itself (shown as a quotation).
export const summarySources = { editor: 'Editors’ summary', model: 'Kimi K3 summary', source: 'From the source' }
const TOPICS = new Set(['ai-math-result', 'formalization', 'model-release', 'community-debate', 'prize-or-award', 'problem-status', 'tooling', 'other'])
const PROBLEM_ID = /^[a-z0-9][a-z0-9._-]{0,159}$/
const STORY_ID = /^[a-z0-9][a-z0-9-]{0,63}$/
const MOVING_ROLES = new Set(['primary', 'journalism', 'repository'])
const DAY = 86_400_000
export function safeNewsUrl(value) {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null }
  catch { return null }
}
const text = (value, limit) => typeof value === 'string' && value.trim() && value.length <= limit ? value : null
// Optional fields from newer feeds are kept only when they are well formed.
function cleanItem(item) {
  const clean = { ...item, url: safeNewsUrl(item.url), related_links: (Array.isArray(item.related_links) ? item.related_links : []).filter(link => link && typeof link.label === 'string' && link.label.trim() && safeNewsUrl(link.url)).map(link => ({ label: link.label, url: safeNewsUrl(link.url) })) }
  if (!(Number.isInteger(item.importance) && item.importance >= 0 && item.importance <= 3)) delete clean.importance
  if (!TOPICS.has(item.topic)) delete clean.topic
  if (!Object.hasOwn(newsRoles, item.source_role ?? '')) delete clean.source_role
  if (!['day', 'month', 'year'].includes(item.date_precision)) delete clean.date_precision
  if (typeof item.story_id !== 'string' || !STORY_ID.test(item.story_id)) delete clean.story_id
  if (!Object.hasOwn(summarySources, item.summary_source ?? '') || !item.summary.trim()) delete clean.summary_source
  const updated = typeof item.updated_at === 'string' ? Date.parse(item.updated_at) : NaN
  if (!(Number.isFinite(updated) && updated > Date.parse(item.published_at))) delete clean.updated_at
  clean.problem_links = (Array.isArray(item.problem_links) ? item.problem_links : []).filter(link => link && typeof link.problem_id === 'string' && PROBLEM_ID.test(link.problem_id) && Object.hasOwn(problemRelations, link.relation ?? '') && text(link.evidence, 240)).slice(0, 3).map(link => ({ problem_id: link.problem_id, relation: link.relation, evidence: link.evidence, ...(text(link.title, 160) ? { title: link.title } : {}) }))
  return clean
}
export function parseNewsFeed(value) {
  if (!value || value.schema_version !== 1 || !Array.isArray(value.items)) throw new Error('The news feed could not be read.')
  const categories = new Set(newsCategories.map(item => item.id))
  const seen = new Set()
  const items = value.items.filter(item => {
    if (!item || typeof item.id !== 'string' || !item.id || seen.has(item.id) || typeof item.title !== 'string' || !item.title.trim() || typeof item.summary !== 'string' || !safeNewsUrl(item.url) || !categories.has(item.category) || typeof item.published_at !== 'string' || !Number.isFinite(Date.parse(item.published_at))) return false
    seen.add(item.id)
    return true
  }).map(cleanItem)
  if (value.items.length && !items.length) throw new Error('The news feed could not be read.')
  items.sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at) || String(a.id).localeCompare(String(b.id)))
  const sources = (Array.isArray(value.sources) ? value.sources : []).filter(source => source && typeof source.name === 'string' && source.name.trim() && safeNewsUrl(source.url)).map(source => ({ ...source, url: safeNewsUrl(source.url) }))
  // A story groups at least two published items; an item belongs to one story at most.
  const byId = new Map(items.map(item => [item.id, item])), claimed = new Set(), stories = []
  for (const story of Array.isArray(value.stories) ? value.stories : []) {
    if (!story || typeof story.id !== 'string' || !STORY_ID.test(story.id) || !text(story.label, 160) || !Array.isArray(story.item_ids) || stories.some(other => other.id === story.id)) continue
    const ids = [...new Set(story.item_ids)].filter(id => byId.has(id) && !claimed.has(id))
    if (ids.length < 2) continue
    ids.forEach(id => claimed.add(id))
    const lead = ids.includes(story.lead_id) ? story.lead_id : ids[0]
    stories.push({ id: story.id, label: story.label, item_ids: ids, lead_id: lead, updated_at: new Date(storyDate(ids.map(id => byId.get(id)), byId.get(lead))).toISOString() })
  }
  const membership = new Map(stories.flatMap(story => story.item_ids.map(id => [id, story.id])))
  for (const item of items) { if (membership.has(item.id)) item.story_id = membership.get(item.id); else delete item.story_id }
  return { ...value, items, sources, stories, source_errors: Array.isArray(value.source_errors) ? value.source_errors : [] }
}
export function newsFreshness(feed, now = Date.now()) {
  const interval = Math.max(5, Math.min(1440, Number(feed?.refresh_interval_minutes) || 30))
  const success = feed?.last_successful_refresh_at
  const lastSuccess = Date.parse(success || '')
  return { interval, stale: !Number.isFinite(lastSuccess) || now - lastSuccess > Math.max(90, interval * 2) * 60_000, success: Number.isFinite(lastSuccess) ? success : null }
}
export function filterNews(items, category = 'all') {
  return category === 'all' ? items : items.filter(item => item.category === category)
}
const corrective = item => item.status === 'Update' || item.status === 'Correction'
const newestFirst = (a, b) => Date.parse(b.published_at) - Date.parse(a.published_at) || a.id.localeCompare(b.id)
const itemDate = item => Date.parse(item.updated_at || item.published_at)
// A story moves up with its lead, primary sources, journalism, repository
// records and corrections; blog posts and preprints join it without moving it.
function storyDate(members, lead) {
  return Math.max(itemDate(lead), ...members.filter(item => !item.source_role || MOVING_ROLES.has(item.source_role) || corrective(item)).map(itemDate))
}
// Entries for the list: a story appears once, whole, with its updates and its
// related coverage, filed under its lead's category (so category counts add up
// to the total and a filtered view never leads a story with a blog post); every
// other item is its own entry.
export function groupNews(feed, category = 'all') {
  const all = feed?.items || []
  const byId = new Map(all.map(item => [item.id, item])), used = new Set(), entries = []
  for (const story of feed?.stories || []) {
    const members = story.item_ids.map(id => byId.get(id)).filter(Boolean)
    if (members.length < 2) continue
    members.forEach(item => used.add(item.id))
    const lead = members.find(item => item.id === story.lead_id) || members[0]
    if (category !== 'all' && lead.category !== category) continue
    const rest = members.filter(item => item !== lead).sort(newestFirst)
    entries.push({ kind: 'story', id: story.id, story, lead, updates: rest.filter(corrective), related: rest.filter(item => !corrective(item)), date: storyDate(members, lead) })
  }
  for (const item of filterNews(all, category)) if (!used.has(item.id)) entries.push({ kind: 'item', id: item.id, lead: item, updates: [], related: [], date: Date.parse(item.published_at) })
  return entries.sort((a, b) => b.date - a.date || a.id.localeCompare(b.id))
}
// The card's date column: a story updated after its lead's day shows that later date.
export function entryDate(entry) {
  const lead = Date.parse(entry.lead.published_at)
  const updated = entry.kind === 'story' && new Date(entry.date).toISOString().slice(0, 10) > new Date(lead).toISOString().slice(0, 10)
  return updated ? { updated: true, value: new Date(entry.date).toISOString(), precision: undefined } : { updated: false, value: entry.lead.published_at, precision: entry.lead.date_precision }
}
export function newsHighlights(items, now = Date.now(), { days = 14, limit = 4 } = {}) {
  return (items || []).filter(item => item.importance >= 2 && Date.parse(item.published_at) <= now + DAY && now - Date.parse(item.published_at) <= days * DAY)
    .sort((a, b) => b.importance - a.importance || newestFirst(a, b)).slice(0, limit)
}
export function problemHref(problemId) {
  return '/problems?problem=' + encodeURIComponent(problemId)
}
export function problemLabel(link) {
  if (link.title) return link.title
  const erdos = /^erdos-0*(\d+)$/.exec(link.problem_id), kourovka = /^kourovka-0*(\d+)-0*(\d+)$/.exec(link.problem_id)
  if (erdos) return `Erdős #${erdos[1]}`
  if (kourovka) return `Kourovka ${kourovka[1]}.${kourovka[2]}`
  return link.problem_id.replace(/^(clay|wiki|solved)-/, '').replace(/-/g, ' ')
}
export function newsDateLabel(value, precision) {
  const date = new Date(value)
  if (!value || Number.isNaN(date.getTime())) return 'Not recorded'
  if (precision === 'year') return String(date.getUTCFullYear())
  return new Intl.DateTimeFormat('en-US', precision === 'month' ? { month: 'long', year: 'numeric', timeZone: 'UTC' } : { dateStyle: 'medium', timeZone: 'UTC' }).format(date)
}
