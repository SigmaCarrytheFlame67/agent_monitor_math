<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useRouter, withBase } from 'vitepress'
import { researchFetch } from './researchApi.js'
import { analyzeEvidence, analyzeLink, applyStatusOverlay, categoryInfo, categoryRules, categoryTabCounts, evidenceLabels, evidenceTimeline, filterProblems, formatEvidenceDate, indexCatalogue,
  maxOverlayChars, parseStatusOverlay, prepareProblemForSolve, presentProblem, problemBadgeList, problemCategories, problemQueryString, problemSorts, readProblemQuery,
  recentDays, signalLabels, solveAction, sourceLinks, statementReady, statusDate, statusFreshness, summaryRepeatsStatement, formatPrize, topicKey } from './problemCatalogue.mjs'
import ProblemMath from './ProblemMath.vue'
import ProblemCategoryMark from './ProblemCategoryMark.vue'

const catalog = shallowRef(null)
const indexed = shallowRef([])
const overlay = shallowRef(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const mode = ref('catalogue')
const search = ref('')
const topic = ref('')
const source = ref('')
const category = ref('')
const sort = ref('notable')
const prizeOnly = ref(false)
const aiOnly = ref(false)
const leanOnly = ref(false)
const changedOnly = ref(false)
const page = ref(1)
const perPage = 18
// "Recent" filters use the load time, so the list does not reorder while it is read; the freshness label ticks.
const loadedAt = ref(Date.now())
const now = ref(Date.now())
const selectedId = ref('')
const solveError = ref('')
let priorBodyOverflow = ''
let clock
const detailPanel = ref(null)
const viewButton = ref(null)
const resultsHeading = ref(null)
const tabButtons = []
const status = computed(() => applyStatusOverlay(indexed.value, overlay.value, catalog.value?.content_sha256))
const rows = computed(() => status.value.rows)
// Every filter but the category, so each tab counts what it would list.
const filters = computed(() => ({ search: search.value, source: source.value, topic: topic.value, prizeOnly: prizeOnly.value,
  ai: aiOnly.value, lean: leanOnly.value, changed: changedOnly.value, now: loadedAt.value }))
const counts = computed(() => categoryTabCounts(rows.value, filters.value))
const tabs = computed(() => [{ id: '', label: 'All', count: counts.value.all }, ...problemCategories.map(item => ({ ...item, count: counts.value[item.id] || 0 }))])
const activeCategory = computed(() => category.value ? categoryInfo(category.value) : null)
// One label per area: spellings that differ only in case or hyphens ("Ramsey theory", "ramsey theory") are one
// area, shown in their most common spelling.
const topicLabels = computed(() => {
  const spellings = new Map()
  for (const problem of indexed.value) for (const item of problem.topics) {
    const seen = spellings.get(topicKey(item)) || new Map()
    spellings.set(topicKey(item), seen.set(item, (seen.get(item) || 0) + 1))
  }
  return new Map([...spellings].map(([key, seen]) => [key, [...seen].sort((a, b) => b[1] - a[1])[0][0]]))
})
// Areas count the problems in the chosen collection, so "Number theory (756)" never leads to an empty Kourovka list;
// empty areas are hidden. Search, the tabs and the checkboxes do not recount them, so the long list stays put while
// typing. Labels are built here, once per change, rather than on every render.
const topics = computed(() => {
  const counts = new Map()
  // The overlay never changes a problem's areas, so the catalogue's rows will do.
  for (const problem of indexed.value) {
    if (source.value && problem.source !== source.value) continue
    for (const item of problem.topicKeys) counts.set(item, (counts.get(item) || 0) + 1)
  }
  if (topic.value && !counts.has(topic.value)) counts.set(topic.value, 0)
  return [...counts].sort((a, b) => b[1] - a[1]).map(([key, count]) => [key, `${niceTopic(topicLabels.value.get(key) || key)} (${formatCount(count)})`])
})
const collections = computed(() => (catalog.value?.sources || []).filter(item => catalog.value.counts?.[item.id]))
const abbreviations = computed(() => Object.fromEntries(collections.value.map(item => [item.id, item.abbreviation || ({ erdos: 'EP', kourovka: 'KN', wikipedia: 'WP', 'wikipedia-solved': 'WS' }[item.id]) || '↗'])))
const filtered = computed(() => filterProblems(rows.value, { ...filters.value, category: category.value, sort: sort.value }))
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / perPage)))
const visible = computed(() => filtered.value.slice((page.value - 1) * perPage, page.value * perPage).map(presentProblem))
const filtersActive = computed(() => Boolean(search.value || source.value || topic.value || prizeOnly.value || aiOnly.value || leanOnly.value || changedOnly.value || sort.value !== 'notable'))
const selected = computed(() => {
  const row = selectedId.value && rows.value.find(item => item.id === selectedId.value)
  return row ? presentProblem(row) : null
})
const freshness = computed(() => statusFreshness(overlay.value, now.value))
// Live status only when the overlay was computed against this catalogue; otherwise the snapshot speaks for itself.
const live = computed(() => status.value.applied && Boolean(freshness.value.checkedLabel))
watch([search, topic, source, category, sort, prizeOnly, aiOnly, leanOnly, changedOnly], () => { page.value = 1 })
watch([category, source, search, selectedId], writeQuery)
const numberFormat = new Intl.NumberFormat('en-US')
const formatCount = value => numberFormat.format(Number(value || 0))
const niceTopic = value => value.replace(/\b\w/g, character => character.toUpperCase())
const utcDate = (value, options = { month: 'short', day: 'numeric', year: 'numeric' }) => {
  const time = typeof value === 'number' ? value : Date.parse(value || '')
  return Number.isFinite(time) ? new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' }).format(time) : ''
}
const dateTitles = { changed: 'Date the status changed', evidence: 'Date of the evidence behind this status', checked: 'Date the status was last checked',
  source: 'Date of the source record behind this status (an edition or database update), not the date the status changed' }
const datePrefixes = { checked: 'Checked ', source: 'As of ' }
function cardDate(row) {
  const date = statusDate(row)
  if (!date) return null
  return { label: (datePrefixes[date.kind] || '') + formatEvidenceDate(date.date, date.precision, 'month'), title: dateTitles[date.kind] }
}
function resetFilters() { search.value = ''; topic.value = ''; source.value = ''; sort.value = 'notable'; prizeOnly.value = false; aiOnly.value = false; leanOnly.value = false; changedOnly.value = false }
// The clear buttons disappear once they work, so focus moves to the results count, which announces the new total.
function clearFilters(everything = false) {
  resetFilters()
  if (everything) category.value = ''
  nextTick(() => resultsHeading.value?.focus())
}
const problemHref = row => withBase('/problems') + problemQueryString({ problem: row.id })
// A new page starts at the top of the list, not at the pager the reader just used.
function goToPage(next) {
  page.value = Math.min(Math.max(1, next), pageCount.value)
  nextTick(() => {
    resultsHeading.value?.closest('.results-heading')?.scrollIntoView({ block: 'start' })
    resultsHeading.value?.focus({ preventScroll: true })
  })
}
function writeQuery() {
  if (typeof window === 'undefined' || !catalog.value) return
  const next = window.location.pathname + problemQueryString({ category: category.value, source: source.value, q: search.value, problem: selectedId.value }) + window.location.hash
  if (next !== window.location.pathname + window.location.search + window.location.hash) window.history.replaceState(window.history.state, '', next)
}
function readQuery() {
  const state = readProblemQuery(window.location.search, { sources: collections.value.map(item => item.id) })
  notice.value = ''
  category.value = state.category
  source.value = state.source
  search.value = state.q
  if (state.problem && rows.value.some(row => row.id === state.problem)) openProblem(state.problem)
  else {
    if (selectedId.value) closeProblem(false)
    if (state.problem) notice.value = 'That problem is not in the current catalogue. It may have been renamed; search for it below.'
  }
}
function selectCategory(id, index) {
  category.value = id
  if (index !== undefined) nextTick(() => tabButtons[index]?.focus())
}
function onTabKey(event, index) {
  const steps = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }
  const last = tabs.value.length - 1
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? last : event.key in steps ? (index + steps[event.key] + last + 1) % (last + 1) : null
  if (next === null) return
  event.preventDefault()
  selectCategory(tabs.value[next].id, next)
}
// Cards are real links (target="_self" keeps VitePress's router out), so a modified click opens the deep link in a new tab.
// A problem opened from a card gets its own history entry, so Back (or a phone's back gesture) closes it instead
// of leaving the page. Both entries record the scroll position, which VitePress's router restores on Back and Forward.
let dialogEntry = false
function onCardClick(event, row) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  if (!selectedId.value) {
    window.history.replaceState({ ...window.history.state, scrollPosition: window.scrollY }, '')
    window.history.pushState({ problemDialog: true, scrollPosition: window.scrollY }, '', window.location.href)
    dialogEntry = true
  }
  openProblem(row.id, event)
}
function onPopState(event) {
  dialogEntry = Boolean(event.state?.problemDialog)
  readQuery()
}
async function openProblem(id, event) {
  if (!selectedId.value) priorBodyOverflow = document.body.style.overflow
  selectedId.value = id
  solveError.value = ''
  document.body.style.overflow = 'hidden'
  viewButton.value = event?.currentTarget || null
  loadProblemThreads(id)
  await nextTick()
  detailPanel.value?.focus()
}
function closeProblem(restoreFocus = true) {
  selectedId.value = ''
  document.body.style.overflow = priorBodyOverflow
  if (restoreFocus) (viewButton.value || resultsHeading.value)?.focus()
  // Closed with the button, Escape or a forum link: leave the dialog's history entry too.
  if (dialogEntry) { dialogEntry = false; window.history.back() }
}
onBeforeUnmount(() => { if (selectedId.value) document.body.style.overflow = priorBodyOverflow })
function handOff(kind) {
  solveError.value = ''
  try { window.location.assign(withBase(prepareProblemForSolve(selected.value, window.localStorage, Date.now(), { mode: kind }))) }
  catch (cause) { solveError.value = cause.message }
}
const evidenceStatus = value => ({ proved: 'Proved partial result', announced: 'Proof announced', claimed: 'Proof claimed' })[value] || value || 'See cited source'
function trapDialogFocus(event) {
  if (event.key !== 'Tab') return
  const focusable = [...detailPanel.value.querySelectorAll('a[href],button:not([disabled])')]
  const first = focusable[0], last = focusable.at(-1)
  if (event.shiftKey && (event.target === first || event.target === detailPanel.value)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && event.target === last) { event.preventDefault(); first?.focus() }
}
const action = computed(() => selected.value ? solveAction(selected.value) : null)
const claim = computed(() => action.value === 'analyze' ? analyzeEvidence(selected.value) : null)
// Guests analyze arXiv and GitHub links; a file from another host needs a member account.
const memberLink = computed(() => Boolean(claim.value) && analyzeLink(claim.value.url)?.connector === 'https')
const timeline = computed(() => selected.value ? evidenceTimeline(selected.value.status_evidence) : [])
// The prize is listed in the facts and new reports have their own section, so neither repeats as a badge here.
const detailBadges = computed(() => selected.value ? problemBadgeList(selected.value, catalog.value).filter(badge => !['prize', 'reported-signal'].includes(badge.id)) : [])
const cardBadges = row => problemBadgeList(row, catalog.value).filter(badge => badge.id !== 'prize')
const ruleLabel = computed(() => categoryRules[selected.value?.category_rule]?.label || '')
// Wikipedia edits are a secondary source, so a list that includes them says so.
const signalDisclosure = computed(() => {
  const signals = selected.value?.signals || []
  const matched = signals.some(signal => signal.matched_by === 'kimi-k3') ? 'Matched automatically by Kimi K3' : 'Matched automatically'
  const wiki = signals.filter(signal => signal.source === 'wikipedia').length
  const from = !wiki ? 'from primary sources' : wiki === signals.length ? 'from Wikipedia edits (a secondary source)' : 'from primary sources and Wikipedia edits (a secondary source)'
  return `${matched} ${from}; not reviewed; does not change the category.`
})
// A source-of-record entry that records no answer ("listed as open", "Active") is a listing: its maintainer's
// acceptance and full scope would read like an accepted answer, so only the listing itself is shown.
const isListing = item => item.kind === 'source-marker' && (!item.outcome || item.outcome === 'other')
const acceptanceTone = value => ['withdrawn', 'refuted', 'disputed'].includes(value) ? 'is-alert' : ['refereed', 'accepted-by-maintainer', 'broadly-accepted'].includes(value) ? 'is-firm' : ''
const leanStatementLabel = url => /^https:\/\/github\.com\/google-deepmind\/formal-conjectures\//.test(url) ? 'Lean statement in formal-conjectures' : 'Lean statement'
const problemThreads = ref(null)
let threadsVersion = 0
async function loadProblemThreads(id) {
  const version = ++threadsVersion
  problemThreads.value = null
  try {
    const data = await researchFetch(`/api/research/forum?${new URLSearchParams({ problem_id: id, limit: '1' })}`)
    if (version === threadsVersion) problemThreads.value = Number.isInteger(data.total) ? data.total : null
  } catch { if (version === threadsVersion) problemThreads.value = null }
}

const forum = ref([])
const forumUser = ref(null)
const forumError = ref('')
const forumLoading = ref(false)
const forumPage = ref(1)
const forumTotal = ref(0)
const forumKind = ref('')
const forumQuery = ref('')
const forumProblem = ref(null)
const activeThread = ref(null)
const replies = ref([])
const replyText = ref('')
const posting = ref(false)
const posted = ref('')
const composing = ref(false)
const form = ref({ title: '', body: '', kind: 'discussion', source_url: '', problem_id: '' })
let requestVersion = 0
let threadVersion = 0
const forumVisible = computed(() => forum.value)
async function loadForum() {
  const version = ++requestVersion
  forumLoading.value = true
  forumError.value = ''
  try {
    const query = new URLSearchParams({ page: String(forumPage.value), limit: '12' })
    if (forumQuery.value.trim()) query.set('q', forumQuery.value.trim())
    if (forumKind.value) query.set('kind', forumKind.value)
    if (forumProblem.value) query.set('problem_id', forumProblem.value.id)
    const data = await researchFetch(`/api/research/forum?${query}`)
    if (version !== requestVersion) return
    forum.value = data.threads || []
    forumTotal.value = data.total || 0
    forumUser.value = data.user || null
  } catch (cause) { if (version === requestVersion) forumError.value = cause.message }
  finally { if (version === requestVersion) forumLoading.value = false }
}
watch(mode, next => { if (next === 'forum') loadForum() })
watch(forumPage, loadForum)
watch(forumKind, () => { forumPage.value = 1; loadForum() })
function searchForum() { forumPage.value = 1; loadForum() }
function showProblemForum(problem) {
  forumProblem.value = { id: problem.id, title: problem.title }
  closeProblem(false)
  activeThread.value = null
  composing.value = false
  forumPage.value = 1
  if (mode.value === 'forum') loadForum()
  else mode.value = 'forum'
}
function clearForumProblem() { forumProblem.value = null; forumPage.value = 1; loadForum() }
async function openThread(thread) {
  const version = ++threadVersion
  activeThread.value = thread
  replies.value = []
  replyText.value = ''
  posted.value = ''
  forumError.value = ''
  try {
    const data = await researchFetch(`/api/research/forum/${encodeURIComponent(thread.id)}`)
    if (version !== threadVersion) return
    activeThread.value = data.thread
    replies.value = data.replies || []
  } catch (cause) { if (version === threadVersion) forumError.value = cause.message }
}
function compose(kind = 'discussion', problem = null) {
  form.value = { kind, title: problem ? `Discuss ${problem.title}` : '', body: '', source_url: problem?.source_url || '', problem_id: problem?.id || '' }
  composing.value = true
  mode.value = 'forum'
  activeThread.value = null
  if (selectedId.value) closeProblem(false)
  posted.value = ''
  nextTick(() => document.getElementById('forum-composer')?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
}
async function submitThread() {
  posting.value = true
  forumError.value = ''
  try {
    await researchFetch('/api/research/forum', { method: 'POST', body: JSON.stringify(form.value) })
    composing.value = false
    posted.value = form.value.kind === 'problem' ? 'Your proposed problem is now in the community forum, marked as unreviewed.' : 'Your discussion has been published.'
    forumKind.value = form.value.kind
    forumQuery.value = ''
    forumPage.value = 1
    await loadForum()
  } catch (cause) { forumError.value = cause.message }
  finally { posting.value = false }
}
async function submitReply() {
  posting.value = true
  forumError.value = ''
  try {
    const thread = activeThread.value
    await researchFetch(`/api/research/forum/${encodeURIComponent(thread.id)}/replies`, { method: 'POST', body: JSON.stringify({ body: replyText.value }) })
    await openThread(thread)
    posted.value = 'Your reply has been published.'
    await loadForum()
  } catch (cause) { forumError.value = cause.message }
  finally { posting.value = false }
}
function dateLabel(value) {
  if (!value) return ''
  const date = new Date(typeof value === 'number' ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}
const authorName = author => typeof author === 'string' ? author : author?.name || 'Community member'
const safeLink = value => /^https?:\/\//i.test(value || '') ? value : null
// The live status overlay is optional: without it the catalogue snapshot stays authoritative.
async function loadOverlay() {
  try {
    const response = await fetch(withBase('/research/problem-status.json'), { cache: 'no-cache', signal: AbortSignal.timeout(15_000) })
    if (!response.ok) return
    const body = await response.text()
    if (body.length <= maxOverlayChars) overlay.value = parseStatusOverlay(JSON.parse(body))
  } catch {}
}
// A link to this page (the nav's "Problems") changes only the query: start from the URL again. Back and Forward
// between this page's entries restore what the address holds and keep the other filters. VitePress runs these
// hooks before this page's own popstate listener, so they read the entry themselves. The hooks are chained, and
// restored when the page goes away.
const router = useRouter()
let previousRouteHook, previousBeforeLoad, previousBeforeRoute
let followingLink = false
async function beforeRouteChange(href) {
  if ((await previousBeforeRoute?.(href)) === false) return false
  followingLink = true
}
async function afterRouteChange(href) {
  await previousRouteHook?.(href)
  const link = followingLink
  followingLink = false
  if (!catalog.value || new URL(href, window.location.href).pathname !== window.location.pathname) return
  if (link) { resetFilters(); mode.value = 'catalogue'; window.scrollTo(0, 0) }
  else dialogEntry = Boolean(window.history.state?.problemDialog)
  readQuery()
}
// VitePress loads a page again on every route change, and this page's first load used its "lean" module, so the
// reload would remount it: refetch and re-index the catalogue and drop the filters, the forum view or a dialog.
// A change that stays on this page needs only the address read (above), so the reload is skipped.
async function beforePageLoad(href) {
  if ((await previousBeforeLoad?.(href)) === false) return false
  if (new URL(href, window.location.href).pathname === router.route.path) return false
}
onMounted(async () => {
  loadOverlay()
  clock = setInterval(() => { now.value = Date.now() }, 60_000)
  window.addEventListener('popstate', onPopState)
  previousRouteHook = router.onAfterRouteChange ?? router.onAfterRouteChanged
  router.onAfterRouteChange = afterRouteChange
  previousBeforeLoad = router.onBeforePageLoad
  router.onBeforePageLoad = beforePageLoad
  previousBeforeRoute = router.onBeforeRouteChange
  router.onBeforeRouteChange = beforeRouteChange
  try {
    const response = await fetch(withBase('/research/open-problems.json'), { cache: 'no-cache' })
    if (!response.ok) throw new Error('The problem catalogue could not be loaded. Please refresh to try again.')
    catalog.value = await response.json()
    indexed.value = indexCatalogue(catalog.value)
    loadedAt.value = Date.now()
    readQuery()
  } catch (cause) { error.value = cause.message }
  finally { loading.value = false }
})
onBeforeUnmount(() => {
  clearInterval(clock)
  window.removeEventListener('popstate', onPopState)
  if (router.onAfterRouteChange === afterRouteChange) router.onAfterRouteChange = previousRouteHook
  if (router.onBeforePageLoad === beforePageLoad) router.onBeforePageLoad = previousBeforeLoad
  if (router.onBeforeRouteChange === beforeRouteChange) router.onBeforeRouteChange = previousBeforeRoute
})
</script>

<template>
  <main class="problem-observatory">
    <header class="observatory-hero">
      <div class="hero-copy">
        <p class="eyebrow"><span class="small-star">✳</span> THE RESEARCH COMMONS</p>
        <h1>Good questions.<br><em>New directions.</em></h1>
        <p class="hero-description">Explore landmark research problems—solved, <span class="keep">AI-proposed</span>, partly answered and open—with the evidence behind each status. Read the sources, then choose an approach.</p>
        <div class="hero-actions"><button class="primary-button" @click="compose('problem')">Contribute a problem <span aria-hidden="true">↗</span></button><a :href="withBase('/dag')">Explore the research DAG <span aria-hidden="true">→</span></a></div>
      </div>
      <div class="hero-art" aria-hidden="true">
        <svg viewBox="0 0 380 240" fill="none">
          <defs><pattern id="problem-dots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".6" fill="currentColor" opacity=".2" /></pattern></defs>
          <rect width="380" height="240" fill="url(#problem-dots)" />
          <ellipse cx="190" cy="119" rx="139" ry="86" transform="rotate(-22 190 119)" />
          <ellipse cx="190" cy="119" rx="139" ry="86" transform="rotate(22 190 119)" />
          <path d="M55 166 117 61 190 120 264 57 324 168 190 120 55 166M117 61 264 57M55 166 324 168" />
          <path class="art-dashed" d="M190 20V219M24 120H356" />
          <circle cx="55" cy="166" r="5"/><circle cx="117" cy="61" r="5"/><circle cx="190" cy="120" r="10"/><circle cx="264" cy="57" r="5"/><circle cx="324" cy="168" r="5"/>
        </svg>
        <div class="art-caption"><span>KNOWN STRUCTURES</span><span>UNKNOWN CONNECTIONS</span></div>
      </div>
    </header>

    <div class="catalogue-masthead">
      <nav class="view-switch" aria-label="Research commons views">
        <button :aria-pressed="mode === 'catalogue'" @click="mode = 'catalogue'">Problem catalogue <span>{{ catalog ? formatCount(catalog.total) : '…' }}</span></button>
        <button :aria-pressed="mode === 'forum'" @click="mode = 'forum'">Community forum <span aria-hidden="true">↗</span></button>
      </nav>
    </div>

    <section v-if="mode === 'catalogue'" class="catalogue-section" aria-label="Problem catalogue">
      <p class="status-line"><span class="status-dot" :class="{ stale: live && (freshness.stale || freshness.errors) }" aria-hidden="true"></span><span><template v-if="live">Status checked <time :datetime="freshness.checked" :title="utcDate(freshness.checked, { dateStyle: 'medium', timeStyle: 'short' }) + ' UTC'">{{ freshness.checkedLabel }}</time> · </template><template v-if="catalog?.snapshot_date">{{ live ? 'catalogue' : 'Status from the catalogue' }} {{ catalog.schema_version >= 3 ? 'researched' : 'snapshot of' }} {{ utcDate(catalog.snapshot_date) }}<template v-if="catalog.schema_version >= 3"> with AI assistance from primary sources</template> · </template><a :href="withBase('/problem-sources')">how categories work →</a></span></p>
      <p v-if="live && (freshness.stale || freshness.errors)" class="status-warning" role="status">{{ freshness.stale ? 'Status updates are delayed; the categories below may be out of date.' : `${freshness.errors} ${freshness.errors === 1 ? 'source' : 'sources'} could not be checked at the last update.` }}</p>
      <div class="category-tabs" role="tablist" aria-label="Problem status">
        <button v-for="(tab, index) in tabs" :key="tab.id || 'all'" :ref="element => { tabButtons[index] = element }" role="tab" type="button" class="category-tab" :class="tab.id ? 'is-' + tab.id : 'is-all'"
          :aria-selected="category === tab.id" :tabindex="category === tab.id ? 0 : -1" aria-controls="problem-results" :title="tab.description || 'Every problem in the catalogue'"
          @click="selectCategory(tab.id)" @keydown="onTabKey($event, index)"><ProblemCategoryMark v-if="tab.id" :category="tab.id" />{{ tab.label }}<span class="tab-count">{{ catalog ? formatCount(tab.count) : '…' }}</span></button>
      </div>
      <p class="category-description" aria-live="polite"><template v-if="activeCategory"><strong>{{ activeCategory.label }}.</strong> {{ activeCategory.description }}</template><template v-else>Every problem has exactly one category. Badges add detail: AI partial results, claims under review, Lean statements and prizes.</template></p>

      <div class="catalogue-view">
        <aside class="catalogue-filters">
          <p class="eyebrow">YOUR NEXT QUESTION</p>
          <h2>Follow your curiosity.</h2>
          <label class="search-field"><span class="sr-only">Search problems</span><span aria-hidden="true">⌕</span><input v-model="search" type="search" maxlength="200" placeholder="Name, number, keyword…" /></label>
          <label class="field-label" for="problem-source">COLLECTION</label>
          <select id="problem-source" v-model="source"><option value="">All collections</option><option v-for="collection in collections" :key="collection.id" :value="collection.id">{{ collection.name }} ({{ formatCount(catalog.counts[collection.id]) }})</option></select>
          <label class="field-label" for="problem-topic">AREA OF MATHEMATICS</label>
          <select id="problem-topic" v-model="topic"><option value="">All research areas</option><option v-for="[key, label] in topics" :key="key" v-memo="[label]" :value="key">{{ label }}</option></select>
          <fieldset class="filter-checks"><legend class="field-label">SHOW ONLY</legend>
            <label class="checkbox-field"><input v-model="aiOnly" type="checkbox" /> AI involvement</label>
            <label class="checkbox-field"><input v-model="leanOnly" type="checkbox" /> Lean statement or proof</label>
            <label class="checkbox-field"><input v-model="changedOnly" type="checkbox" /> Changed in the last {{ recentDays }} days</label>
            <label class="checkbox-field"><input v-model="prizeOnly" type="checkbox" /> A listed prize</label>
          </fieldset>
          <button v-if="filtersActive" class="text-button reset-button" @click="clearFilters()">Clear filters ↺</button>
          <div class="source-note"><span class="note-symbol" aria-hidden="true">↗</span><strong>A map to the original work.</strong><p>Every entry links to its original statement and author credits. Categories follow the source of record and reviewed evidence; they are not an independent verification by Ansätze.</p><a :href="withBase('/problem-sources')">Sources &amp; categories →</a></div>
        </aside>

        <div id="problem-results" class="catalogue-results" role="tabpanel" :aria-label="activeCategory ? activeCategory.label + ' problems' : 'All problems'">
          <div class="results-heading"><p ref="resultsHeading" tabindex="-1" aria-live="polite"><strong>{{ formatCount(filtered.length) }}</strong> {{ filtered.length === 1 ? 'problem' : 'problems' }}<template v-if="activeCategory"> · {{ activeCategory.label }}</template></p><label class="sort-control">Sort <select v-model="sort" aria-label="Sort problems"><option v-for="option in problemSorts" :key="option.id" :value="option.id">{{ option.label }}</option></select></label></div>
          <p v-if="notice" class="feedback" role="status">{{ notice }} <button class="text-button" @click="notice = ''">Dismiss</button></p>
          <p v-if="loading" class="empty-state" role="status">Loading the research catalogue…</p>
          <p v-else-if="error" class="feedback error" role="alert">{{ error }}</p>
          <div v-else-if="!visible.length" class="empty-state"><h3>No problems match yet.</h3><p>Try another keyword, another category or all research areas.</p><button class="text-button" @click="clearFilters(true)">Clear filters →</button></div>
          <div v-else class="problem-grid">
            <a v-for="problem in visible" :key="problem.id" class="problem-card" :href="problemHref(problem)" target="_self" @click="onCardClick($event, problem)">
              <span class="card-topline"><span class="collection-abbreviation">{{ abbreviations[problem.source] }}</span><span>{{ problem.source_name }}</span><span v-if="problem.prize" class="prize">{{ formatPrize(problem.prize) }}</span></span>
              <h3><ProblemMath inline :text="problem.title" /></h3>
              <p><ProblemMath inline :text="problem.status_headline || problem.summary" /></p>
              <span v-if="cardBadges(problem).length" class="card-badges"><span v-for="badge in cardBadges(problem).slice(0, 3)" :key="badge.id" class="badge" :title="badge.description">{{ badge.label }}</span><span v-if="cardBadges(problem).length > 3" class="badge more">+{{ cardBadges(problem).length - 3 }}</span></span>
              <span class="card-footer"><span class="category-chip" :class="'is-' + problem.category" :title="categoryInfo(problem.category).description"><ProblemCategoryMark :category="problem.category" />{{ categoryInfo(problem.category).label }}</span><span v-if="cardDate(problem)" class="card-date" :title="cardDate(problem).title">{{ cardDate(problem).label }}</span><span class="card-arrow" aria-hidden="true">↗</span></span>
            </a>
          </div>
          <nav v-if="filtered.length > perPage" class="pagination" aria-label="Catalogue pages"><button :disabled="page <= 1" @click="goToPage(page - 1)">← Previous</button><span>Page {{ page }} of {{ pageCount }}</span><button :disabled="page >= pageCount" @click="goToPage(page + 1)">Next →</button></nav>
        </div>
      </div>
    </section>

    <section v-else class="forum-view" aria-labelledby="forum-title">
      <div class="forum-intro"><div><p class="eyebrow">MATHEMATICS IS A CONVERSATION</p><h2 id="forum-title">Think together.</h2><p>Share a question, a useful reference, or the next step in a proof.</p></div><button class="primary-button" @click="compose('discussion')">Start a discussion <span aria-hidden="true">+</span></button></div>
      <p v-if="posted" class="feedback success" role="status">{{ posted }}</p>
      <p v-if="forumError" class="feedback error" role="alert">{{ forumError }} <button class="text-button" @click="loadForum">Try again</button></p>
      <div v-if="composing" id="forum-composer" class="forum-composer">
        <div class="composer-heading"><h3>{{ form.kind === 'problem' ? 'Propose an open problem' : 'Start a discussion' }}</h3><button class="text-button" @click="composing = false">Close ×</button></div>
        <template v-if="forumUser">
          <p v-if="form.kind === 'problem'" class="composer-note">Community proposals appear in the forum as unreviewed. Include a source and explain what remains open.</p>
          <form @submit.prevent="submitThread">
            <label>Title<input v-model="form.title" required minlength="3" maxlength="180" placeholder="Give your question a clear title" /></label>
            <label>{{ form.kind === 'problem' ? 'Problem statement and context' : 'Your message' }}<textarea v-model="form.body" required minlength="10" maxlength="20000" rows="6" placeholder="Describe the problem, what is known, and where to go next…" /></label>
            <label>Source link {{ form.kind === 'problem' ? '(recommended)' : '(optional)' }}<input v-model="form.source_url" type="url" maxlength="2000" placeholder="https://…" /></label>
            <p v-if="form.problem_id" class="composer-note">Linked to {{ form.problem_id }}</p>
            <button class="primary-button" :disabled="posting" type="submit">{{ posting ? 'Publishing…' : 'Publish to the forum' }} <span aria-hidden="true">→</span></button>
          </form>
        </template>
        <div v-else class="signin-note"><p>Sign in to add a problem or join the discussion.</p><a :href="withBase('/sign-in')" class="primary-button">Sign in to contribute →</a></div>
      </div>
      <div v-if="activeThread" class="thread-detail">
        <button class="text-button" @click="activeThread = null; threadVersion++">← All discussions</button>
        <span v-if="activeThread.kind === 'problem'" class="community-badge">Community proposal · unreviewed</span>
        <h3>{{ activeThread.title }}</h3><p class="thread-meta">{{ authorName(activeThread.author) }} · {{ dateLabel(activeThread.created) }}</p>
        <p class="thread-body">{{ activeThread.body }}</p>
        <a v-if="safeLink(activeThread.source_url)" :href="safeLink(activeThread.source_url)" target="_blank" rel="noopener noreferrer">Read the linked source ↗</a>
        <div class="thread-replies"><h4>{{ replies.length }} {{ replies.length === 1 ? 'reply' : 'replies' }}</h4><article v-for="reply in replies" :key="reply.id"><p class="thread-meta">{{ authorName(reply.author) }} · {{ dateLabel(reply.created) }}</p><p class="thread-body">{{ reply.body }}</p></article></div>
        <form v-if="forumUser" class="reply-form" @submit.prevent="submitReply"><label for="reply-body">Add to the conversation</label><textarea id="reply-body" v-model="replyText" required minlength="2" maxlength="10000" rows="4" placeholder="Share an idea or reference…" /><button class="primary-button" :disabled="posting">{{ posting ? 'Publishing…' : 'Post reply' }}</button></form>
        <a v-else :href="withBase('/sign-in')" class="text-button">Sign in to reply →</a>
      </div>
      <template v-else>
        <form class="forum-toolbar" @submit.prevent="searchForum"><label class="search-field"><span class="sr-only">Search discussions</span><span aria-hidden="true">⌕</span><input v-model="forumQuery" type="search" placeholder="Find a conversation…" /></label><button class="outline-button" type="submit">Search</button><label class="sr-only" for="forum-kind">Discussion type</label><select id="forum-kind" v-model="forumKind"><option value="">All conversations</option><option value="problem">Proposed problems</option><option value="discussion">Discussions</option></select></form>
        <p v-if="forumProblem" class="forum-filter">Discussions about <strong>{{ forumProblem.title }}</strong> <button class="text-button" @click="clearForumProblem">Show all discussions ×</button></p>
        <p v-if="forumLoading" class="empty-state" role="status">Loading conversations…</p>
        <div v-else-if="!forumVisible.length && !forumError" class="forum-empty"><span class="empty-symbol" aria-hidden="true">∴</span><h3>The next idea could start here.</h3><p>No discussions yet. Share the first question or a reference worth reading.</p><button class="text-button" @click="compose('discussion')">Start the conversation →</button></div>
        <div v-else class="thread-list"><button v-for="thread in forumVisible" :key="thread.id" class="thread-row" @click="openThread(thread)"><span class="thread-icon" aria-hidden="true">{{ thread.kind === 'problem' ? '?' : '↗' }}</span><span class="thread-main"><span v-if="thread.kind === 'problem'" class="community-badge">Proposed problem · unreviewed</span><strong>{{ thread.title }}</strong><span class="thread-preview">{{ thread.body }}</span><span class="thread-meta">{{ authorName(thread.author) }} · {{ dateLabel(thread.created) }}</span></span><span class="reply-count">{{ thread.replies || 0 }}<small>replies</small></span></button></div>
        <nav v-if="forumTotal > 12" class="pagination" aria-label="Forum pages"><button :disabled="forumPage <= 1" @click="forumPage--">← Previous</button><span>Page {{ forumPage }} of {{ Math.ceil(forumTotal / 12) }}</span><button :disabled="forumPage * 12 >= forumTotal" @click="forumPage++">Next →</button></nav>
      </template>
    </section>

    <footer class="observatory-footer"><p>Every question has a history. Every contribution should leave a trail.</p><a :href="withBase('/problem-sources')">Read our sources &amp; attribution ↗</a></footer>

    <div v-if="selected" class="detail-backdrop" @click.self="closeProblem" @keydown.esc="closeProblem">
      <section ref="detailPanel" class="problem-detail" tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="problem-detail-title" @keydown="trapDialogFocus">
        <div class="detail-top"><span class="eyebrow">{{ selected.source_name }}</span><button class="close-button" aria-label="Close problem details" @click="closeProblem">×</button></div>
        <span v-if="/^\d+(?:\.\d+)?$/.test(selected.number)" class="detail-number" aria-hidden="true">{{ selected.number }}</span>
        <h2 id="problem-detail-title"><ProblemMath inline :text="selected.title" /></h2>
        <p v-if="selected.aliases?.length" class="detail-aliases">Also known as <ProblemMath inline :text="selected.aliases.join(' · ')" /></p>
        <div class="detail-status"><span class="category-chip" :class="'is-' + selected.category"><ProblemCategoryMark :category="selected.category" />{{ categoryInfo(selected.category).label }}</span><span v-if="selected.status_as_of">Status checked {{ utcDate(selected.status_as_of) }}</span></div>
        <ProblemMath v-if="!summaryRepeatsStatement(selected)" class="detail-summary" :text="selected.summary" />
        <div class="topic-tags"><span v-for="tag in selected.topics" :key="tag">{{ niceTopic(tag) }}</span></div>

        <section class="detail-section" aria-labelledby="problem-claim-title">
          <div class="detail-section-heading"><h3 id="problem-claim-title">Statement</h3><span>{{ statementReady(selected) ? (selected.statement.kind === 'exact' ? 'Source wording' : 'Editorial formulation') : 'Original statement at source' }}</span></div>
          <ProblemMath v-if="statementReady(selected)" :text="selected.statement.text" />
          <div v-else class="detail-source-pointer"><p>The full mathematical statement is available at the original source. This entry currently provides a source reference.</p><p><strong>{{ selected.source_locator }}</strong></p><a :href="safeLink(selected.source_url)" target="_blank" rel="noopener noreferrer">Read the numbered statement ↗</a></div>
          <nav v-if="sourceLinks(selected.statement?.sources).length" class="detail-sources" aria-label="Sources for the statement"><a v-for="reference in sourceLinks(selected.statement.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="reference.label" /> ↗</a></nav>
        </section>
        <p v-if="selected.detail_note" class="detail-context">{{ selected.detail_note }}</p>

        <section class="detail-section" aria-labelledby="problem-status-title">
          <div class="detail-section-heading"><h3 id="problem-status-title">Status &amp; evidence</h3><span v-if="ruleLabel">Rule {{ selected.category_rule }} · {{ ruleLabel }}</span></div>
          <div class="status-card" :class="'is-' + selected.category">
            <p><strong>{{ categoryInfo(selected.category).label }}.</strong> {{ categoryInfo(selected.category).description }}</p>
            <p v-if="selected.status_headline" class="status-headline"><ProblemMath inline :text="selected.status_headline" /></p>
            <p v-if="selected.decision && !selected.overlay" class="status-decision">Category set by a decision of {{ selected.decision.by }}, {{ formatEvidenceDate(selected.decision.at) }}: {{ selected.decision.note }}</p>
            <p v-if="selected.overlay" class="status-update">Updated {{ utcDate(selected.overlay.changed_at) }} by the source monitor<template v-if="selected.overlay.change">: {{ selected.overlay.change }}</template><template v-if="selected.overlay.from !== selected.category"> (previously {{ categoryInfo(selected.overlay.from).label }})</template>.</p>
          </div>
          <ol v-if="timeline.length" class="evidence-timeline" aria-label="Status evidence, newest first">
            <li v-for="(item, index) in timeline" :key="index + item.url" :class="{ 'is-void': ['withdrawn', 'refuted'].includes(item.acceptance) && !['withdrawal', 'refutation'].includes(item.kind) }">
              <span class="timeline-date">{{ formatEvidenceDate(item.date, item.date_precision) }}</span>
              <div class="timeline-body">
                <span class="evidence-chips"><span v-if="item.kind !== 'other'" class="chip">{{ isListing(item) ? 'Source listing' : evidenceLabels.kind[item.kind] }}</span><span class="chip" :class="{ 'is-ai': ['ai', 'ai-assisted'].includes(item.actor) }">{{ evidenceLabels.actor[item.actor] }}</span><span v-if="item.acceptance && !isListing(item)" class="chip" :class="acceptanceTone(item.acceptance)">{{ evidenceLabels.acceptance[item.acceptance] }}</span><span v-if="item.scope && !isListing(item)" class="chip">{{ evidenceLabels.scope[item.scope] }}</span><span v-if="item.outcome && item.outcome !== 'other'" class="chip">{{ evidenceLabels.outcome[item.outcome] }}</span></span>
                <a :href="item.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="item.title" /> ↗</a>
                <span v-if="item.claimant || item.ai_systems || item.venue" class="timeline-meta">{{ [item.claimant, item.ai_systems && 'with ' + item.ai_systems.join(', '), item.venue].filter(Boolean).join(' · ') }}</span>
                <span v-if="item.kind === 'formalization'" class="timeline-meta">Upstream Lean · not re-checked by this server</span>
                <p v-if="item.note"><ProblemMath inline :text="item.note" /></p>
              </div>
            </li>
          </ol>
          <p v-else-if="!selected.partial_results?.length && !selected.proof_claims?.length" class="detail-empty">No status evidence is recorded beyond the source listing. This is not a claim that no such work exists.</p>
          <p v-if="selected.formal_statement_url" class="upstream-lean"><a :href="selected.formal_statement_url" target="_blank" rel="noopener noreferrer">{{ leanStatementLabel(selected.formal_statement_url) }} ↗</a><span>Upstream Lean · not re-checked by this server</span></p>
          <div v-if="detailBadges.length" class="detail-badges"><span v-for="badge in detailBadges" :key="badge.id" class="badge" :title="badge.description">{{ badge.label }}</span></div>
          <template v-if="selected.partial_results?.length">
            <h4 class="evidence-heading">Known partial results</h4>
            <article v-for="(result, index) in selected.partial_results" :key="index" class="evidence-entry">
              <span class="evidence-status">{{ evidenceStatus(result.status) }}</span><h5><ProblemMath inline :text="result.title" /></h5><ProblemMath :text="result.text" />
              <nav class="detail-sources" :aria-label="'Sources for ' + result.title"><a v-for="reference in sourceLinks(result.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="reference.label" /> ↗</a></nav>
            </article>
          </template>
          <template v-if="selected.proof_claims?.length">
            <h4 class="evidence-heading">Proof claims and announcements</h4>
            <article v-for="(item, index) in selected.proof_claims" :key="index" class="evidence-entry proof-claim">
              <span class="evidence-status">{{ evidenceStatus(item.status) }}</span><h5><ProblemMath inline :text="item.title" /></h5><ProblemMath :text="item.text" />
              <nav class="detail-sources" :aria-label="'Sources for ' + item.title"><a v-for="reference in sourceLinks(item.sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="reference.label" /> ↗</a></nav>
            </article>
          </template>
        </section>

        <section v-if="selected.signals?.length" class="detail-section" aria-labelledby="problem-signals-title">
          <div class="detail-section-heading"><h3 id="problem-signals-title">New reports (unreviewed)</h3></div>
          <p class="signal-disclosure">{{ signalDisclosure }}</p>
          <article v-for="signal in selected.signals" :key="signal.url" class="signal-entry">
            <span class="evidence-chips"><span class="chip">{{ signalLabels.relation[signal.relation] }}</span><span class="chip">{{ signalLabels.source[signal.source] }}</span><span class="chip">{{ signalLabels.matched_by[signal.matched_by] }}</span><span v-if="signal.date" class="chip">{{ formatEvidenceDate(signal.date.slice(0, 10)) }}</span></span>
            <a :href="signal.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="signal.title" /> ↗</a>
            <blockquote v-if="signal.evidence_quote">“<ProblemMath inline :text="signal.evidence_quote" />”</blockquote>
          </article>
        </section>

        <dl class="detail-facts"><div><dt>Source status</dt><dd>{{ selected.source_status }}</dd></div><div><dt>Original reference</dt><dd>{{ selected.source_locator }}</dd></div><div v-if="selected.prize"><dt>Listed prize</dt><dd>{{ formatPrize(selected.prize) }}<small>See source for terms</small></dd></div><div v-if="selected.formal_status"><dt>Formal evidence</dt><dd>{{ selected.formal_status }}</dd></div></dl>
        <nav v-if="sourceLinks(selected.additional_sources).length" class="detail-sources" aria-label="Additional references"><a v-for="reference in sourceLinks(selected.additional_sources)" :key="reference.url" :href="reference.url" target="_blank" rel="noopener noreferrer"><ProblemMath inline :text="reference.label" /> ↗</a></nav>
        <p v-if="selected.source_status_note" class="detail-note">{{ selected.source_status_note }}</p>
        <div class="detail-actions">
          <template v-if="action === 'analyze'"><button class="primary-button solve-button" @click="handOff('analyze')">Analyze the claimed solution</button><p>Open “{{ claim.title }}” in the workspace’s Analyze mode, which maps the argument and checks its steps with a model. A model check is not a certificate.<template v-if="memberLink"> Guests can analyze arXiv and GitHub links; this file needs a member account.</template></p></template>
          <template v-else-if="action === 'solve'"><button class="primary-button solve-button" @click="handOff('solve')">Click to solve</button><p>Open this problem in the proving workspace. {{ statementReady(selected) ? 'Review the statement and choose an approach before starting.' : 'Paste the full source statement before starting.' }}</p></template>
          <p v-if="solveError" class="feedback error" role="alert">{{ solveError }}</p>
          <a class="outline-button" :href="safeLink(selected.source_url)" target="_blank" rel="noopener noreferrer">Read the original problem ↗</a>
          <button class="text-button" @click="compose('discussion', selected)">Discuss this problem →</button>
          <button v-if="problemThreads" class="text-button" @click="showProblemForum(selected)">{{ problemThreads }} {{ problemThreads === 1 ? 'discussion' : 'discussions' }} in the forum →</button>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.detail-section{margin-top:28px;padding-top:22px;border-top:1px solid var(--vp-c-divider)}.detail-section-heading{display:flex;gap:10px 20px;align-items:baseline;justify-content:space-between;flex-wrap:wrap;margin-bottom:14px}.detail-section-heading h3{font-size:19px;letter-spacing:-.02em;margin:0;font-weight:600}.detail-section-heading>span{font-size:11px;color:var(--vp-c-text-2)}.detail-sources{display:flex;flex-direction:column;gap:7px;margin-top:13px;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.evidence-entry+.evidence-entry{margin-top:22px;padding-top:20px;border-top:1px solid var(--vp-c-divider)}.evidence-entry h5{font-size:15px;margin:7px 0 9px;font-weight:600}.evidence-status{font-size:11px;letter-spacing:.02em;color:var(--vp-c-brand-1);font-weight:600}.proof-claim .evidence-status{color:#966b17}.detail-empty,.detail-source-pointer,.detail-context{font-size:13px;color:var(--vp-c-text-2);line-height:1.8}.detail-source-pointer p{margin:0 0 10px}.detail-context{padding:13px 16px;border-left:3px solid var(--vp-c-brand-1);background:var(--vp-c-bg-soft);margin:20px 0}.detail-actions{margin-top:24px;padding-top:20px;border-top:1px solid var(--vp-c-divider);display:flex;flex-wrap:wrap;align-items:center;gap:12px}.detail-actions .solve-button{width:100%;font-size:15px;min-height:46px}.detail-actions>p{width:100%;margin:0;font-size:12px;color:var(--vp-c-text-2)}.detail-actions .outline-button,.detail-actions .text-button{font-size:12px}.problem-detail .detail-summary{margin-bottom:12px}.problem-detail .close-button{flex-shrink:0;min-width:40px;min-height:40px}

.sort-control{display:flex;gap:8px;align-items:center;min-width:0}.sort-control select{width:auto;max-width:200px;padding:6px 22px 6px 8px;border:1px solid var(--vp-c-divider);border-radius:4px;background:var(--vp-c-bg);color:var(--vp-c-text-1)}.problem-detail h2,.detail-facts dd{overflow-wrap:anywhere}.detail-top{margin-bottom:18px}.problem-card{min-width:0}.problem-card h3{overflow-wrap:anywhere}.card-topline{flex-wrap:wrap}.card-topline .prize{margin-left:auto}.results-heading{flex-wrap:wrap;gap:10px}.catalogue-filters select{max-width:100%;min-width:0}
.problem-observatory{max-width:1240px;margin:0 auto;padding:58px 40px 0;color:var(--vp-c-text-1);font-size:14px;line-height:1.6}.problem-observatory button,.problem-observatory input,.problem-observatory select,.problem-observatory textarea{font:inherit}.problem-observatory button{cursor:pointer}.problem-observatory a{color:var(--vp-c-brand-1)}.problem-observatory button:focus-visible,.problem-observatory a:focus-visible,.problem-observatory input:focus-visible,.problem-observatory select:focus-visible,.problem-observatory textarea:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}.problem-observatory button:disabled{cursor:default;opacity:.4}.observatory-hero{display:grid;grid-template-columns:1.3fr 1fr;align-items:center;gap:70px;padding-bottom:48px}.eyebrow{font-size:10px;letter-spacing:.16em;font-weight:600;color:var(--vp-c-text-2);margin:0 0 16px}.small-star{font-size:19px;color:var(--vp-c-brand-1);vertical-align:middle;margin-right:8px}.observatory-hero h1{font:500 clamp(42px,5vw,66px)/1.02 'Newsreader Variable',Georgia,serif;letter-spacing:-.045em;margin:0}.observatory-hero h1 em{font-weight:450;color:var(--vp-c-brand-1)}.hero-description{max-width:460px;font-size:15px;color:var(--vp-c-text-2);line-height:1.75;margin:20px 0 24px}.hero-actions{display:flex;align-items:center;flex-wrap:wrap;gap:22px;font-size:12px}.primary-button,.outline-button{display:inline-flex;justify-content:center;align-items:center;gap:20px;padding:11px 16px;border-radius:4px;font-weight:500;transition:background .15s,transform .15s}.problem-observatory .primary-button{background:var(--vp-c-brand-1);color:var(--vp-c-bg);border:1px solid var(--vp-c-brand-1)}.primary-button:hover{transform:translateY(-1px)}.outline-button{border:1px solid var(--vp-c-divider);background:var(--vp-c-bg);color:var(--vp-c-text-1)}.text-button{color:var(--vp-c-brand-1);background:none;border:0;font-size:12px;font-weight:500}.hero-art{color:var(--vp-c-brand-1);padding:12px 0}.hero-art svg{width:100%;stroke:currentColor;stroke-width:.8}.hero-art ellipse{opacity:.45}.hero-art circle{fill:var(--vp-c-bg);stroke-width:1.5}.hero-art circle[r="10"]{fill:var(--vp-c-brand-1)}.art-dashed{stroke-dasharray:3 7;opacity:.2}.art-caption{display:flex;justify-content:space-between;font:8px var(--vp-font-family-mono);letter-spacing:.1em;color:var(--vp-c-text-3);margin-top:6px}.catalogue-masthead{display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--vp-c-divider)}.view-switch{display:flex;gap:26px}.view-switch button{border:0;border-bottom:2px solid transparent;background:none;padding:17px 0;font-size:12px;font-weight:500;color:var(--vp-c-text-2)}.view-switch button[aria-pressed=true]{border-color:var(--vp-c-brand-1);color:var(--vp-c-text-1)}.view-switch button span{font-size:10px;margin-left:8px;padding:2px 6px;background:var(--vp-c-bg-alt);border-radius:3px}.catalogue-view{display:grid;grid-template-columns:226px minmax(0,1fr);gap:34px;padding:30px 0 42px}.catalogue-filters{border-right:1px solid var(--vp-c-divider);padding-right:25px}.catalogue-filters .eyebrow{font-size:8px;margin:0 0 8px}.catalogue-filters h2{font:500 24px/1.2 'Newsreader Variable',Georgia,serif;letter-spacing:-.03em;margin:0 0 22px}.search-field{display:flex;align-items:center;gap:7px;border:1px solid var(--vp-c-divider);background:var(--vp-c-bg-elv);border-radius:4px;padding:8px 10px;min-width:0}.search-field>span:not(.sr-only){font-size:20px;line-height:1}.search-field input{width:100%;min-width:0;border:0;background:transparent;color:var(--vp-c-text-1);font-size:11px}.search-field input:focus-visible{outline:none}.search-field:focus-within{outline:2px solid var(--vp-c-brand-1);outline-offset:2px}.field-label{display:block;font-size:8px;font-weight:600;letter-spacing:.12em;margin:23px 0 8px;color:var(--vp-c-text-3)}.problem-observatory select{width:100%;max-width:100%;border:1px solid var(--vp-c-divider);border-radius:4px;padding:8px 25px 8px 10px;background:var(--vp-c-bg);color:var(--vp-c-text-1);font-size:11px;appearance:auto}.checkbox-field{display:flex;align-items:center;gap:8px;font-size:10px;margin-top:20px;color:var(--vp-c-text-2)}.checkbox-field input{accent-color:var(--vp-c-brand-1)}.reset-button{margin-top:14px}.source-note{border-top:1px solid var(--vp-c-divider);padding-top:22px;margin-top:34px;font-size:10px;color:var(--vp-c-text-2)}.note-symbol{display:block;font-size:22px;color:var(--vp-c-brand-1);margin-bottom:8px}.source-note strong{font-size:11px;color:var(--vp-c-text-1);font-weight:500}.source-note p{margin:8px 0 14px;line-height:1.8}.results-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 18px;font-size:10px;color:var(--vp-c-text-3)}.results-heading p{margin:0}.results-heading strong{font-size:12px;color:var(--vp-c-text-1);font-weight:500}.problem-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.problem-card{display:flex;flex-direction:column;text-align:left;min-width:0;text-decoration:none;background:var(--vp-c-bg-elv);border:1px solid var(--vp-c-divider);border-radius:5px;padding:16px 15px 12px;color:var(--vp-c-text-1);transition:border-color .15s,transform .15s,box-shadow .15s}.problem-card:hover{border-color:var(--vp-c-brand-1);transform:translateY(-2px);box-shadow:0 5px 18px rgba(20,40,28,.05)}.card-topline{display:flex;align-items:center;gap:6px;font-size:10px;color:var(--vp-c-text-3);flex-wrap:wrap}.collection-abbreviation{padding:2px 4px;border:1px solid var(--vp-c-divider);border-radius:2px;font-size:7px;letter-spacing:.06em}.prize{margin-left:auto;color:var(--vp-c-brand-1)}.problem-card h3{font:500 21px/1.18 'Newsreader Variable',Georgia,serif;margin:19px 0 10px;letter-spacing:-.02em}.problem-card>p{font-size:12px;line-height:1.8;color:var(--vp-c-text-2);margin:0 0 15px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.topic-tags{display:flex;flex-wrap:wrap;gap:5px}.topic-tags span{border:1px solid var(--vp-c-divider);border-radius:3px;padding:2px 6px;font-size:10px;color:var(--vp-c-text-2)}.card-footer{display:flex;align-items:center;gap:10px;margin-top:16px;border-top:1px solid var(--vp-c-divider);padding-top:9px;font-size:10px;color:var(--vp-c-text-3)}.pagination{display:flex;align-items:center;justify-content:space-between;padding:25px 0 0;font-size:10px;color:var(--vp-c-text-2)}.pagination button{padding:7px 10px;border:1px solid var(--vp-c-divider);border-radius:4px;background:var(--vp-c-bg);color:var(--vp-c-text-1)}.empty-state{text-align:center;padding:55px 20px;color:var(--vp-c-text-2)}.empty-state h3{font-size:26px}.observatory-footer{border-top:1px solid var(--vp-c-divider);padding:22px 0 35px;display:flex;justify-content:space-between;gap:20px;font-size:10px;color:var(--vp-c-text-3)}.observatory-footer p{margin:0}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.forum-view{padding:32px 0 45px}.forum-intro{display:flex;justify-content:space-between;gap:24px;align-items:center;margin-bottom:28px}.forum-intro .eyebrow{margin-bottom:8px}.forum-intro h2{font:500 38px/1.1 'Newsreader Variable',Georgia,serif;margin:0}.forum-intro p:last-child{font-size:12px;color:var(--vp-c-text-2);margin:10px 0 0}.forum-intro>.primary-button{font-size:12px}.forum-toolbar{display:flex;gap:12px;align-items:center;margin:24px 0 12px}.forum-toolbar>.search-field{flex:1}.forum-toolbar>.outline-button{font-size:11px;padding:8px 14px}.forum-toolbar select{width:180px}.forum-empty{text-align:center;padding:65px 20px;border:1px dashed var(--vp-c-divider);border-radius:6px}.empty-symbol{font:400 46px 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1)}.forum-empty h3{font:500 30px/1.2 'Newsreader Variable',Georgia,serif;margin:8px 0}.forum-empty p{font-size:12px;color:var(--vp-c-text-2);margin:10px 0 18px}.forum-composer,.thread-detail{max-width:850px;margin:24px auto;padding:26px;border:1px solid var(--vp-c-divider);border-radius:6px;background:var(--vp-c-bg-elv)}.composer-heading{display:flex;justify-content:space-between;align-items:center;gap:16px}.composer-heading h3{font:500 28px/1.2 'Newsreader Variable',Georgia,serif;margin:0}.composer-note,.signin-note{font-size:12px;color:var(--vp-c-text-2)}.forum-composer form label,.reply-form label{display:block;font-size:11px;color:var(--vp-c-text-2);margin:18px 0}.forum-composer input,.forum-composer textarea,.reply-form textarea{display:block;width:100%;border:1px solid var(--vp-c-divider);background:var(--vp-c-bg);color:var(--vp-c-text-1);border-radius:4px;padding:10px;margin-top:6px;font-size:12px;resize:vertical}.forum-composer .primary-button,.reply-form .primary-button{font-size:12px}.feedback{border:1px solid var(--vp-c-divider);border-left:3px solid var(--vp-c-brand-1);background:var(--vp-c-bg-alt);padding:12px 16px;font-size:12px}.feedback.error{border-left-color:#a58154}.feedback .text-button{margin-left:12px}.thread-list{border-top:1px solid var(--vp-c-divider)}.thread-row{width:100%;display:flex;align-items:center;gap:18px;padding:22px 8px;text-align:left;border-bottom:1px solid var(--vp-c-divider);color:var(--vp-c-text-1)}.thread-row:hover{background:var(--vp-c-bg-alt)}.thread-icon{flex:none;width:38px;height:38px;display:grid;place-content:center;border:1px solid var(--vp-c-divider);border-radius:50%;font:24px 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1)}.thread-main{min-width:0;display:flex;flex-direction:column;gap:5px}.thread-main>strong{font-size:14px;font-weight:500}.thread-preview{font-size:11px;color:var(--vp-c-text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.thread-meta{font-size:10px;color:var(--vp-c-text-3)}.community-badge{display:block;font-size:8px;text-transform:uppercase;letter-spacing:.1em;color:var(--vp-c-brand-1)}.reply-count{margin-left:auto;text-align:center;font-size:16px;color:var(--vp-c-text-2);padding-left:10px}.reply-count small{display:block;font-size:9px}.thread-detail>h3{font:500 34px/1.2 'Newsreader Variable',Georgia,serif;margin:18px 0 8px}.thread-detail>.community-badge{margin-top:18px}.thread-body{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.85}.thread-detail>a{font-size:12px}.thread-replies{margin-top:25px;border-top:1px solid var(--vp-c-divider)}.thread-replies h4{font-size:13px;font-weight:500;margin:20px 0}.thread-replies article{padding:6px 0 15px;border-bottom:1px solid var(--vp-c-divider)}.reply-form .primary-button{margin-top:12px}.detail-backdrop{position:fixed;inset:0;background:rgba(10,22,15,.45);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;z-index:100;padding:24px}.problem-detail{width:760px;max-width:100%;max-height:calc(100dvh - 48px);overflow:auto;background:var(--vp-c-bg);border:1px solid var(--vp-c-divider);border-radius:8px;padding:28px;box-shadow:0 22px 100px rgba(0,0,0,.2)}.detail-top{display:flex;align-items:center;justify-content:space-between;gap:20px}.detail-top .eyebrow{margin:0}.close-button{display:grid;place-content:center;width:32px;height:32px;border:1px solid var(--vp-c-divider);border-radius:50%;font-size:22px!important;color:var(--vp-c-text-2)}.detail-number{display:block;font:400 65px/1 'Newsreader Variable',Georgia,serif;color:var(--vp-c-brand-1);opacity:.6;margin:16px 0}.problem-detail h2{font:500 35px/1.1 'Newsreader Variable',Georgia,serif;margin:0 0 14px}.detail-summary{color:var(--vp-c-text-2);font-size:13px;line-height:1.8;margin-bottom:16px}.detail-facts{margin:24px 0 16px;font-size:11px;border-top:1px solid var(--vp-c-divider)}.detail-facts>div{display:grid;grid-template-columns:110px 1fr;gap:18px;padding:11px 0;border-bottom:1px solid var(--vp-c-divider)}.detail-facts dt{color:var(--vp-c-text-3)}.detail-facts dd{margin:0}.detail-facts small{display:block;color:var(--vp-c-text-3)}.detail-note{font-size:10px;color:var(--vp-c-text-3);line-height:1.8;margin-bottom:20px}.problem-detail>.primary-button,.problem-detail>.outline-button{display:flex;width:100%;font-size:12px;margin-top:10px}.problem-detail:focus{outline:none}@media(min-width:1440px){.problem-observatory{max-width:1300px}}@media(max-width:1050px){.problem-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.observatory-hero{gap:30px}.catalogue-view{grid-template-columns:205px minmax(0,1fr);gap:24px}.catalogue-filters{padding-right:20px}}@media(max-width:760px){.problem-observatory{padding:36px 22px 0}.observatory-hero{grid-template-columns:1fr;gap:10px;padding-bottom:28px}.hero-art{display:none}.hero-copy{max-width:560px}.observatory-hero h1{font-size:47px}.hero-description{font-size:14px}.catalogue-masthead{gap:8px;flex-wrap:wrap}.view-switch{gap:24px}.catalogue-view{grid-template-columns:1fr;gap:24px}.catalogue-filters{border-right:0;padding-right:0;display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}.catalogue-filters>.eyebrow,.catalogue-filters>h2,.catalogue-filters>.source-note,.catalogue-filters>.field-label{display:none}.catalogue-filters>.search-field{grid-column:1/-1}.catalogue-filters .checkbox-field{margin:3px 0;font-size:11px}.catalogue-filters .reset-button{margin:0;text-align:right}.problem-card h3{font-size:23px}.problem-card>p{font-size:11px}.card-topline{font-size:9px}.topic-tags span{font-size:9px}.card-footer{font-size:9px}.results-heading{font-size:11px}.observatory-footer{flex-direction:column;gap:8px;font-size:11px}.forum-intro{align-items:flex-start;flex-direction:column;gap:18px}.forum-toolbar{flex-wrap:wrap}.forum-toolbar>.search-field{flex-basis:65%}.forum-toolbar select{width:100%}.forum-composer,.thread-detail{padding:20px}.thread-icon{display:none}.detail-backdrop{padding:14px}.problem-detail{padding:22px;max-height:calc(100dvh - 28px)}}@media(max-width:420px){.problem-observatory{padding:28px 16px 0}.observatory-hero h1{font-size:41px}.hero-actions{gap:16px}.view-switch{gap:18px}.view-switch button{font-size:11px}.view-switch button span{font-size:9px;margin-left:4px}.problem-grid{grid-template-columns:1fr}.problem-card{padding:18px}.problem-card h3{margin-top:15px;font-size:26px}.problem-card>p{font-size:12px}.problem-card .topic-tags span{font-size:10px}.problem-card .card-footer{font-size:10px}.catalogue-filters .checkbox-field{grid-column:1/-1}.detail-facts>div{grid-template-columns:94px 1fr;gap:12px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
/* Keep supporting text readable at normal zoom, including on touch screens. */
.problem-card > p { font-size: 13px; }
.card-topline, .card-footer, .topic-tags span,
.checkbox-field, .results-heading, .pagination, .observatory-footer,
.thread-meta, .detail-facts, .detail-note { font-size: 11px; }
.field-label { font-size: 10px; }
.source-note { font-size: 12px; }
.source-note strong { font-size: 12px; }
.problem-observatory select, .search-field input { font-size: 12px; }
.card-topline .collection-abbreviation { font-size: 8px; }
@media (max-width: 760px) {
  .problem-card > p { font-size: 14px; }
  .card-topline, .card-footer, .topic-tags span, .checkbox-field,
  .results-heading, .pagination, .observatory-footer { font-size: 12px; }
  .problem-observatory input:not([type='checkbox']),
  .problem-observatory select, .problem-observatory textarea { font-size: 16px; }
  .search-field { min-height: 44px; }
  .problem-observatory select { min-height: 44px; }
}
@media (max-width: 420px) {
  .catalogue-filters select { grid-column: 1 / -1; }
  .problem-card .card-topline, .problem-card .topic-tags span,
  .problem-card .card-footer { font-size: 12px; }
}
.dark .proof-claim .evidence-status { color: #d3b873; }
/* Status line, category tabs and chips. Colours come from the --problem-* tokens in custom.css; green stays reserved for Lean
   and amber means "AI proposed" only, so warnings use the alert colour and the AI actor chip stays neutral. */
.keep { white-space: nowrap; }
.status-line { display: flex; align-items: baseline; gap: 9px; margin: 20px 0 0; font-size: 12px; line-height: 1.7; color: var(--vp-c-text-2); }
.status-line a { white-space: nowrap; }
.status-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--vp-c-brand-1); transform: translateY(-1px); }
.status-dot.stale { background: var(--problem-alert); }
.status-warning { margin: 6px 0 0 15px; font-size: 12px; color: var(--problem-alert); }
/* 5px of padding holds the 2px focus ring and its 2px offset inside the scrolling strip; the negative margin keeps the tabs in place. */
.category-tabs { display: flex; gap: 8px; margin: 16px -3px 0; padding: 5px; overflow-x: auto; scrollbar-width: none; }
.category-tabs::-webkit-scrollbar { display: none; }
.category-tab { display: inline-flex; align-items: center; gap: 7px; flex: none; min-height: 38px; padding: 7px 14px; border: 1px solid var(--vp-c-divider); border-radius: 999px; background: var(--vp-c-bg); color: var(--vp-c-text-2); font-size: 13px; font-weight: 500; white-space: nowrap; transition: border-color .15s, background-color .15s, color .15s; }
.category-tab:hover { border-color: var(--vp-c-border); color: var(--vp-c-text-1); }
.problem-observatory .category-tab:focus-visible { outline-offset: 2px; }
.tab-count { font-size: 11px; font-variant-numeric: tabular-nums; color: var(--vp-c-text-3); }
.category-tab.is-solved .problem-category-mark { color: var(--problem-solved); }
.category-tab.is-ai-proposed .problem-category-mark { color: var(--problem-ai); }
.category-tab.is-partial .problem-category-mark { color: var(--problem-partial); }
.category-tab.is-open .problem-category-mark { color: var(--problem-open); }
.category-tab[aria-selected='true'] { border-color: var(--vp-c-text-1); background: var(--vp-c-bg-soft); color: var(--vp-c-text-1); }
.category-tab[aria-selected='true'] .tab-count { color: inherit; }
.category-tab.is-solved[aria-selected='true'] { border-color: var(--problem-solved); background: var(--problem-solved); color: var(--problem-solved-text); }
.category-tab.is-solved[aria-selected='true'] .problem-category-mark { color: inherit; }
.category-tab.is-ai-proposed[aria-selected='true'] { border-color: var(--problem-ai-line); background: var(--problem-ai-soft); color: var(--problem-ai); }
.category-tab.is-partial[aria-selected='true'] { border-color: var(--problem-partial-line); background: var(--problem-partial-soft); color: var(--problem-partial); }
.category-description { max-width: 780px; margin: 10px 0 0; font-size: 13px; line-height: 1.7; color: var(--vp-c-text-2); }
.category-description strong { font-weight: 600; color: var(--vp-c-text-1); }
.category-chip { display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px 2px 7px; border: 1px solid transparent; border-radius: 999px; font-size: 11px; font-weight: 600; line-height: 1.5; letter-spacing: .01em; white-space: nowrap; }
.category-chip .problem-category-mark { width: 12px; height: 12px; }
.category-chip.is-solved { border-color: var(--problem-solved); background: var(--problem-solved); color: var(--problem-solved-text); }
.category-chip.is-ai-proposed { border-color: var(--problem-ai-line); background: var(--problem-ai-soft); color: var(--problem-ai); }
.category-chip.is-partial { border-color: var(--problem-partial-line); background: var(--problem-partial-soft); color: var(--problem-partial); }
.category-chip.is-open { border-color: var(--problem-open-line); color: var(--problem-open); }

/* Cards: links, so a problem opens in a new tab and its URL can be shared. */
.problem-observatory .problem-card { color: var(--vp-c-text-1); }
.problem-card .card-badges { display: flex; flex-wrap: wrap; gap: 5px; margin-top: auto; }
.badge { padding: 1px 6px; border: 1px solid var(--vp-c-divider); border-radius: 3px; background: var(--vp-c-bg); font-size: 11px; line-height: 1.6; color: var(--vp-c-text-2); }
.badge.more { color: var(--vp-c-text-3); }
.problem-card .card-footer { margin-top: auto; padding-top: 11px; }
.problem-card .card-badges + .card-footer { margin-top: 14px; }
.card-date { margin-left: auto; font-variant-numeric: tabular-nums; }
.card-arrow { font-size: 15px; line-height: 1; color: var(--vp-c-brand-1); }
.filter-checks { min-width: 0; margin: 23px 0 0; padding: 0; border: 0; }
.filter-checks .field-label { margin: 0 0 2px; padding: 0; }
.filter-checks .checkbox-field { margin-top: 11px; }

/* The pager scrolls the list back to its heading, clear of the fixed nav bar. */
.results-heading { scroll-margin-top: calc(var(--vp-nav-height, 64px) + 16px); }

/* Detail panel: status and evidence. */
.detail-aliases { margin: -4px 0 14px; font-size: 13px; line-height: 1.6; color: var(--vp-c-text-2); }
.detail-status { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 2px 0 16px; font-size: 12px; color: var(--vp-c-text-3); }
.status-card { padding: 14px 16px; border: 1px solid var(--vp-c-divider); border-left: 3px solid var(--problem-open-line); border-radius: 4px; background: var(--vp-c-bg-soft); font-size: 13px; line-height: 1.75; color: var(--vp-c-text-2); }
.status-card p { margin: 0; }
.status-card p + p { margin-top: 8px; }
.status-card strong { font-weight: 600; color: var(--vp-c-text-1); }
.status-card.is-solved { border-left-color: var(--problem-solved); }
.status-card.is-ai-proposed { border-left-color: var(--problem-ai); }
.status-card.is-partial { border-left-color: var(--problem-partial); }
.status-card .status-headline { color: var(--vp-c-text-1); }
.status-card .status-update, .status-card .status-decision { font-size: 12px; }
.evidence-timeline { margin: 18px 0 0; padding: 0; list-style: none; }
.evidence-timeline li { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 4px 16px; padding: 14px 0; border-top: 1px solid var(--vp-c-divider); }
.timeline-date { padding-top: 2px; font-size: 12px; font-variant-numeric: tabular-nums; color: var(--vp-c-text-2); }
.timeline-body { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; min-width: 0; font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.timeline-body p { margin: 0; font-size: 12px; line-height: 1.7; color: var(--vp-c-text-2); }
.timeline-meta { font-size: 12px; color: var(--vp-c-text-3); }
.evidence-timeline li.is-void .timeline-body > a { text-decoration: line-through; text-decoration-thickness: 1px; }
.evidence-timeline li.is-void .timeline-body > a, .evidence-timeline li.is-void .timeline-date { opacity: .72; }
.evidence-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip { padding: 0 7px; border: 1px solid var(--vp-c-divider); border-radius: 999px; font-size: 11px; line-height: 1.7; color: var(--vp-c-text-2); }
.chip.is-ai { color: var(--vp-c-text-1); font-weight: 600; }
.chip.is-firm { border-color: var(--vp-c-text-3); color: var(--vp-c-text-1); }
.chip.is-alert { border-color: currentColor; color: var(--problem-alert); }
.upstream-lean { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin: 16px 0 0; font-size: 13px; }
.upstream-lean span { font-size: 12px; color: var(--vp-c-text-3); }
.detail-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 16px; }
.evidence-heading { margin: 26px 0 12px; font-size: 15px; font-weight: 600; }
.signal-disclosure { margin: 0 0 6px; padding: 10px 14px; border-left: 3px solid var(--problem-open-line); background: var(--vp-c-bg-soft); font-size: 12px; line-height: 1.7; color: var(--vp-c-text-1); }
.signal-entry { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; padding: 12px 0; border-bottom: 1px solid var(--vp-c-divider); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.signal-entry:last-child { border-bottom: 0; }
.signal-entry blockquote { margin: 0; padding-left: 12px; border-left: 2px solid var(--vp-c-divider); font-size: 12px; color: var(--vp-c-text-2); }
.forum-filter { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; margin: 0 0 12px; font-size: 13px; color: var(--vp-c-text-2); }
.forum-filter strong { color: var(--vp-c-text-1); font-weight: 500; }
@media (max-width: 760px) {
  .status-line { font-size: 13px; }
  .category-tabs { flex-wrap: wrap; overflow: visible; }
  .category-tab { min-height: 40px; gap: 6px; padding: 6px 11px; }
  .catalogue-filters .filter-checks { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; margin-top: 0; }
  .catalogue-filters .filter-checks .field-label { display: none; }
  .evidence-timeline li { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 420px) {
  .catalogue-filters .filter-checks { grid-template-columns: minmax(0, 1fr); }
  .card-footer .category-chip { font-size: 11px; }
}
/* Forced colours (Windows High Contrast) replace borders and backgrounds with system colours, so the selected
   tab takes the system highlight instead of looking like every other tab. */
@media (forced-colors: active) {
  .category-tabs .category-tab[aria-selected='true'] { forced-color-adjust: none; border: 2px solid Highlight; background: Highlight; color: HighlightText; }
  .category-tabs .category-tab[aria-selected='true'] .tab-count, .category-tabs .category-tab[aria-selected='true'] .problem-category-mark { color: HighlightText; }
  /* The view switch marks the current view with an underline; forced colours would draw both underlines. */
  .view-switch button { border-bottom-color: Canvas; }
  .view-switch button[aria-pressed='true'] { border-bottom-color: Highlight; }
}
/* A card needs about 260px to keep its category chip, date and arrow on one line: one column below 600px,
   and one column beside the filters up to 860px (a 768px tablet). */
@media (max-width: 600px) { .problem-grid { grid-template-columns: minmax(0, 1fr); } }
@media (min-width: 761px) and (max-width: 860px) { .problem-grid { grid-template-columns: minmax(0, 1fr); } }
.card-date { white-space: nowrap; }
.search-field input { text-overflow: ellipsis; }
/* Long entries scroll for thousands of pixels on a phone, so the source name and the close button stay in reach.
   Sticky offsets are measured inside the panel's padding: a negative top keeps the bar flush with the panel's edge. */
.problem-detail .detail-top { position: sticky; top: -28px; z-index: 2; margin: -28px -28px 18px; padding: 18px 28px 10px; background: var(--vp-c-bg); }
@media (max-width: 760px) { .problem-detail .detail-top { top: -22px; margin: -22px -22px 16px; padding: 12px 22px 8px; } }
</style>
