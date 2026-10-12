<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef } from 'vue'
import { withBase } from 'vitepress'
import { newsCategories, newsRoles, problemRelations, summarySources, parseNewsFeed, newsFreshness, groupNews, entryDate, newsHighlights, problemHref, problemLabel, newsDateLabel } from './newsFeed.mjs'
import { escapeHtml, plainMath, renderMath } from './mathText.js'
import './vendor/katex/katex.min.css'

const feed = shallowRef(null), loading = ref(true), error = ref(''), category = ref('all'), now = ref(Date.now())
const opened = reactive(new Set())
let polling, controller, timeout, disposed = false, pending = false
const categories = [{ id: 'all', label: 'All news' }, ...newsCategories]
const entries = computed(() => groupNews(feed.value, category.value).map(entry => ({ ...entry, when: entryDate(entry) })))
const highlights = computed(() => newsHighlights(feed.value?.items || [], now.value))
const freshness = computed(() => newsFreshness(feed.value, now.value))
const sourceFailures = computed(() => feed.value?.source_errors?.length || 0)
const categoryName = id => newsCategories.find(item => item.id === id)?.label || id
const categoryCount = id => groupNews(feed.value, id).length
const sourceRecord = item => feed.value?.sources.find(source => source.id === item.source || source.id === item.source_id || source.name === item.source)
const sourceName = item => sourceRecord(item)?.name || item.source || 'Original source'
const roleName = item => item.source_role === 'preprint' && item.status === 'Preprint' ? '' : newsRoles[item.source_role] || ''
const statusClass = item => ({ announced: /announc|claim/i.test(item.status || ''), preprint: /preprint/i.test(item.status || ''), update: item.status === 'Update', correction: item.status === 'Correction' })
const relationName = link => problemRelations[link.relation] || link.relation
const problemTitle = link => `${relationName(link)}: “${link.evidence}” (matched by Kimi K3; not a status change)`
const summaryLabel = item => summarySources[item.summary_source] || ''
// arXiv titles write formulas in TeX ("$\tilde{A}_2$"); elsewhere a "$" is money, so only preprint titles are rendered.
const titleHtml = item => item.source_role === 'preprint' ? renderMath(item.title, { texText: true }) : escapeHtml(item.title)
const titleText = item => item.source_role === 'preprint' ? plainMath(item.title) : item.title
function dateLabel(value, includeTime = false, precision) {
  const date = new Date(value)
  if (!value || Number.isNaN(date.getTime())) return 'Not recorded'
  if (!includeTime) return newsDateLabel(value, precision)
  return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }).format(date) + ' UTC'
}
// A highlight jumps to the place where its item is shown, opening a story's coverage when needed.
async function showItem(item, event) {
  event?.preventDefault()
  // A story is filed under its lead's category, so its members may sit outside the current filter.
  const find = view => groupNews(feed.value, view).find(row => row.lead.id === item.id || row.updates.includes(item) || row.related.includes(item))
  let entry = find(category.value)
  if (!entry && category.value !== 'all') { category.value = 'all'; entry = find('all') }
  if (entry?.related.includes(item)) opened.add(entry.id)
  await nextTick()
  const target = document.getElementById(item.id) || (entry && document.getElementById(entry.lead.id))
  target?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  target?.focus({ preventScroll: true })
}
function toggleCoverage(entry, event) { if (event.target.open) opened.add(entry.id); else opened.delete(entry.id) }
async function loadFeed() {
  if (pending || disposed) return
  pending = true; loading.value = true
  controller = new AbortController()
  timeout = setTimeout(() => controller?.abort(), 15_000)
  try {
    const response = await fetch(withBase('/research/news.json'), { cache: 'no-store', signal: controller.signal })
    if (!response.ok) throw new Error('The news feed is temporarily unavailable.')
    const next = parseNewsFeed(await response.json())
    if (disposed) return
    const first = !feed.value
    feed.value = next; error.value = ''; now.value = Date.now()
    // A shared #news-… link: the item exists only once the feed has loaded.
    const target = first && window.location.hash.slice(1)
    const linked = target && next.items.find(item => item.id === target)
    if (linked) { await nextTick(); showItem(linked) }
  } catch {
    if (!disposed) error.value = feed.value ? 'News could not be refreshed. The last loaded headlines are still shown.' : 'News is temporarily unavailable. Please try again shortly.'
  } finally {
    clearTimeout(timeout); pending = false
    if (!disposed) loading.value = false
  }
}
function refreshVisible() { if (document.visibilityState === 'visible') { now.value = Date.now(); loadFeed() } }
onMounted(() => { loadFeed(); polling = setInterval(refreshVisible, 180_000); document.addEventListener('visibilitychange', refreshVisible) })
onBeforeUnmount(() => { disposed = true; clearInterval(polling); clearTimeout(timeout); controller?.abort(); document.removeEventListener('visibilitychange', refreshVisible) })
</script>

<template>
  <main class="news-page">
    <header class="news-hero">
      <div><p class="news-eyebrow">THE RESEARCH NOTEBOOK / NEWS</p><h1>What’s new.<br><em>What comes next.</em></h1></div>
      <div class="news-intro"><p>Mathematical developments, model releases, and research worth following. Every story leads back to its original source.</p><a :href="withBase('/problems')">Explore the questions behind the work <span aria-hidden="true">↗</span></a></div>
    </header>

    <section class="news-freshness" aria-label="News feed updates">
      <div><span class="news-check-dot" :class="{ stale: error || freshness.stale || sourceFailures }" aria-hidden="true"></span><strong>Feed refreshed every {{ feed ? freshness.interval : 30 }} minutes</strong><span v-if="feed">Last checked <time :datetime="feed.last_checked_at">{{ dateLabel(feed.last_checked_at, true) }}</time></span><span v-else>{{ loading ? 'Loading source updates…' : 'No feed available' }}</span></div>
      <button class="news-refresh" :aria-disabled="loading ? 'true' : undefined" @click="loadFeed">{{ loading ? 'Refreshing…' : 'Refresh feed' }} <span aria-hidden="true">↻</span></button>
      <p v-if="feed" class="news-snapshot">Headlines updated <time :datetime="feed.updated_at">{{ dateLabel(feed.updated_at, true) }}</time><template v-if="freshness.success"> · Last successful source check {{ dateLabel(freshness.success, true) }}</template></p>
    </section>
    <p v-if="error" class="news-notice" role="status">{{ error }}</p>
    <p v-else-if="feed && sourceFailures" class="news-notice" role="status">{{ sourceFailures }} {{ sourceFailures === 1 ? 'source could' : 'sources could' }} not be refreshed at the last check. The feed may be incomplete; available headlines are shown below.</p>
    <p v-if="feed && freshness.stale" class="news-notice" role="status">This feed has not had a successful source check recently. Publication dates below belong to the original stories.</p>

    <section v-if="highlights.length" class="news-highlights" aria-labelledby="news-highlights-title">
      <h2 id="news-highlights-title">Highlights <span>· last 14 days</span></h2>
      <ol>
        <li v-for="item in highlights" :key="item.id">
          <a :href="'#' + item.id" @click="showItem(item, $event)">
            <span class="news-highlight-meta"><time :datetime="item.published_at">{{ dateLabel(item.published_at, false, item.date_precision) }}</time><span class="news-status" :class="statusClass(item)">{{ item.status || 'Published' }}</span></span>
            <strong v-html="titleHtml(item)"></strong>
            <span class="news-highlight-source">{{ sourceName(item) }}</span>
          </a>
        </li>
      </ol>
    </section>

    <div class="news-toolbar">
      <nav class="news-filters" aria-label="Filter news by category"><button v-for="item in categories" :key="item.id" :aria-pressed="category === item.id" :data-category="item.id" @click="category = item.id">{{ item.label }}<span v-if="feed">{{ categoryCount(item.id) }}</span></button></nav>
      <p class="news-count" aria-live="polite">{{ entries.length }} {{ entries.length === 1 ? 'story' : 'stories' }}<span> · Newest first</span></p>
    </div>
    <div v-if="loading && !feed" class="news-empty" role="status">Loading the latest source snapshot…</div>
    <div v-else-if="!feed" class="news-empty"><h2>Updates will return here.</h2><p>The feed is unavailable, so there are no headlines to show right now.</p><button class="news-text-button" @click="loadFeed">Try again →</button></div>
    <div v-else-if="!entries.length" class="news-empty"><h2>No stories in this view yet.</h2><p>Choose another category or check back after the next source update.</p><button v-if="category !== 'all'" class="news-text-button" @click="category = 'all'">Show all news →</button></div>
    <ol v-else class="news-list" aria-label="News stories in reverse chronological order">
      <li v-for="entry in entries" :key="entry.id">
        <article :id="entry.lead.id" class="news-card" :class="{ 'news-story': entry.kind === 'story' }" :data-news-id="entry.lead.id" :data-story-id="entry.kind === 'story' ? entry.id : undefined" tabindex="-1">
          <div class="news-card-date"><time :datetime="entry.when.value">{{ entry.when.updated ? 'Updated ' : '' }}{{ dateLabel(entry.when.value, false, entry.when.precision) }}</time><span>{{ categoryName(entry.lead.category) }}</span></div>
          <div class="news-card-body">
            <p v-if="entry.kind === 'story'" class="news-story-label"><span>Story</span>{{ entry.story.label }}</p>
            <div class="news-card-meta"><time v-if="entry.when.updated" :datetime="entry.lead.published_at">{{ dateLabel(entry.lead.published_at, false, entry.lead.date_precision) }}</time><a :href="sourceRecord(entry.lead)?.url || entry.lead.url" target="_blank" rel="noopener noreferrer">{{ sourceName(entry.lead) }}</a><span v-if="roleName(entry.lead)" class="news-role">{{ roleName(entry.lead) }}</span><span class="news-status" :class="statusClass(entry.lead)">{{ entry.lead.status || 'Published' }}</span><span v-if="entry.lead.updated_at" class="news-updated">Updated {{ dateLabel(entry.lead.updated_at) }}</span></div>
            <h2><a :href="entry.lead.url" target="_blank" rel="noopener noreferrer"><span v-html="titleHtml(entry.lead)"></span> <span class="news-arrow" aria-hidden="true">↗</span></a></h2><p v-if="entry.lead.summary" class="news-summary"><span v-if="summaryLabel(entry.lead)" class="news-summary-source">{{ summaryLabel(entry.lead) }}</span><q v-if="entry.lead.summary_source === 'source'">{{ entry.lead.summary }}</q><template v-else>{{ entry.lead.summary }}</template></p>
            <div v-if="entry.lead.problem_links.length" class="news-problems"><p class="news-problems-note">Problem links · matched by Kimi K3 from the quoted words · not a status change</p><ul aria-label="Problems this source discusses"><li v-for="link in entry.lead.problem_links" :key="link.problem_id + link.relation"><a :href="withBase(problemHref(link.problem_id))" :title="problemTitle(link)"><span class="news-problem-relation">{{ relationName(link) }}:</span> {{ problemLabel(link) }}<span class="news-sr-only"> (matched by Kimi K3; not a status change)</span></a><q>{{ link.evidence }}</q></li></ul></div>
            <ul v-if="entry.lead.related_links.length" class="news-related" aria-label="Related source statements"><li v-for="link in entry.lead.related_links" :key="link.url"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{ link.label }} ↗</a></li></ul><a class="news-read" :href="entry.lead.url" target="_blank" rel="noopener noreferrer">Read the original <span class="news-sr-only">: {{ titleText(entry.lead) }}</span><span aria-hidden="true">→</span></a>
            <ol v-if="entry.updates.length" class="news-updates" aria-label="Updates and corrections in this story">
              <li v-for="item in entry.updates" :id="item.id" :key="item.id" tabindex="-1">
                <div class="news-card-meta"><time :datetime="item.published_at">{{ dateLabel(item.published_at, false, item.date_precision) }}</time><span class="news-status" :class="statusClass(item)">{{ item.status }}</span><a :href="sourceRecord(item)?.url || item.url" target="_blank" rel="noopener noreferrer">{{ sourceName(item) }}</a></div>
                <h3><a :href="item.url" target="_blank" rel="noopener noreferrer"><span v-html="titleHtml(item)"></span> <span class="news-arrow" aria-hidden="true">↗</span></a></h3><p v-if="item.summary" class="news-summary"><span v-if="summaryLabel(item)" class="news-summary-source">{{ summaryLabel(item) }}</span><q v-if="item.summary_source === 'source'">{{ item.summary }}</q><template v-else>{{ item.summary }}</template></p>
                <div v-if="item.problem_links.length" class="news-problems"><p class="news-problems-note">Problem links · matched by Kimi K3 from the quoted words · not a status change</p><ul aria-label="Problems this source discusses"><li v-for="link in item.problem_links" :key="link.problem_id + link.relation"><a :href="withBase(problemHref(link.problem_id))" :title="problemTitle(link)"><span class="news-problem-relation">{{ relationName(link) }}:</span> {{ problemLabel(link) }}<span class="news-sr-only"> (matched by Kimi K3; not a status change)</span></a><q>{{ link.evidence }}</q></li></ul></div>
                <ul v-if="item.related_links.length" class="news-related" aria-label="Related source statements"><li v-for="link in item.related_links" :key="link.url"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{ link.label }} ↗</a></li></ul>
              </li>
            </ol>
            <details v-if="entry.related.length" class="news-coverage" :open="opened.has(entry.id)" @toggle="toggleCoverage(entry, $event)">
              <summary>Related coverage <span>{{ entry.related.length }}</span></summary>
              <ol>
                <li v-for="item in entry.related" :id="item.id" :key="item.id" tabindex="-1">
                  <div class="news-card-meta"><time :datetime="item.published_at">{{ dateLabel(item.published_at, false, item.date_precision) }}</time><a :href="sourceRecord(item)?.url || item.url" target="_blank" rel="noopener noreferrer">{{ sourceName(item) }}</a><span v-if="roleName(item)" class="news-role">{{ roleName(item) }}</span><span v-if="item.status && item.status !== 'Published'" class="news-status" :class="statusClass(item)">{{ item.status }}</span></div>
                  <h3><a :href="item.url" target="_blank" rel="noopener noreferrer"><span v-html="titleHtml(item)"></span> <span class="news-arrow" aria-hidden="true">↗</span></a></h3><p v-if="item.summary" class="news-summary"><span v-if="summaryLabel(item)" class="news-summary-source">{{ summaryLabel(item) }}</span><q v-if="item.summary_source === 'source'">{{ item.summary }}</q><template v-else>{{ item.summary }}</template></p>
                  <div v-if="item.problem_links.length" class="news-problems"><p class="news-problems-note">Problem links · matched by Kimi K3 from the quoted words · not a status change</p><ul aria-label="Problems this source discusses"><li v-for="link in item.problem_links" :key="link.problem_id + link.relation"><a :href="withBase(problemHref(link.problem_id))" :title="problemTitle(link)"><span class="news-problem-relation">{{ relationName(link) }}:</span> {{ problemLabel(link) }}<span class="news-sr-only"> (matched by Kimi K3; not a status change)</span></a><q>{{ link.evidence }}</q></li></ul></div>
                </li>
              </ol>
            </details>
          </div>
        </article>
      </li>
    </ol>

    <footer class="news-footer"><p>Headlines, dates and links come from the sources; reviewed cards, marked “Editors’ summary”, are written by the Ansätze editors from the sources. Status labels are assigned by Ansätze, by fixed rules for each source or by the editors. Summaries marked “Kimi K3 summary” are written by Kimi K3 under fixed rules from the source text, and text marked “From the source” is quoted from it. Stories and highlights are set by the editors or produced with Kimi K3; problem links are matched by Kimi K3 and quote the source. Each source is checked on its own schedule, at most every 30 minutes, and dates are given in UTC. Read the original source for precise claims. A problem link records what a source says about a problem, not whether it is correct, and never changes the problem’s status. Preprints are shared manuscripts whose peer-review status is not established here. A proof announcement remains labelled as an announcement.</p><details v-if="feed?.sources.length"><summary>Sources <span>{{ feed.sources.length }}</span></summary><ul><li v-for="source in feed.sources" :key="source.id || source.url"><a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.name }} ↗</a><span class="news-source-kind">{{ source.automated === false ? 'Curated source' : source.evidence_only ? 'Monitored for evidence; not shown as stories' : 'Monitored feed' }}<template v-if="newsRoles[source.role]"> · {{ newsRoles[source.role] }}</template></span></li></ul></details><a :href="withBase('/quickstart')">Start your own research →</a></footer>
  </main>
</template>

<style scoped>
.news-page{max-width:1180px;margin:0 auto;padding:64px 40px 50px;color:var(--vp-c-text-1);font-size:14px;line-height:1.65}.news-page a{color:var(--vp-c-brand-1)}.news-page button{font:inherit;cursor:pointer}.news-page button:disabled,.news-page button[aria-disabled=true]{cursor:default;opacity:.55}.news-page a:focus-visible,.news-page button:focus-visible,.news-page summary:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:5px}.news-hero{display:grid;grid-template-columns:1.1fr 1fr;gap:72px;align-items:end;margin-bottom:48px}.news-eyebrow{font-size:10px;letter-spacing:.15em;font-weight:600;color:var(--vp-c-brand-1);margin:0 0 24px}.news-hero h1{font:450 clamp(44px,5.7vw,70px)/1.03 'Newsreader Variable',Georgia,serif;letter-spacing:-.045em;margin:0}.news-hero h1 em{font-weight:450;color:var(--vp-c-brand-1)}.news-intro>p{font-size:15px;color:var(--vp-c-text-2);line-height:1.8;max-width:440px;margin:0 0 20px}.news-intro>a{font-size:12px}.news-intro>a span{margin-left:8px}.news-freshness{display:flex;align-items:center;justify-content:space-between;gap:12px 24px;flex-wrap:wrap;border-top:1px solid var(--vp-c-divider);border-bottom:1px solid var(--vp-c-divider);padding:20px 0}.news-freshness>div{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;font-size:11px;color:var(--vp-c-text-2)}.news-freshness strong{font-weight:500;color:var(--vp-c-text-1)}.news-check-dot{width:6px;height:6px;border-radius:50%;background:var(--vp-c-brand-1);flex-shrink:0}.news-check-dot.stale{background:#b68435}.news-refresh,.news-text-button{border:0;background:none;color:var(--vp-c-brand-1);font-size:12px!important;padding:8px 0;min-height:40px}.news-refresh span{margin-left:8px}.news-snapshot{flex-basis:100%;font-size:11px;color:var(--vp-c-text-2);margin:0}.news-notice{padding:13px 16px;margin:16px 0 0;border:1px solid var(--vp-c-divider);border-left:3px solid #b68435;border-radius:3px;background:var(--vp-c-bg-soft);font-size:12px;color:var(--vp-c-text-2)}.news-toolbar{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;padding:26px 0 16px;border-bottom:1px solid var(--vp-c-divider)}.news-filters{display:flex;gap:7px;flex-wrap:wrap}.news-filters button{display:flex;align-items:center;gap:8px;padding:8px 12px;min-height:40px;border:1px solid transparent;border-radius:5px;color:var(--vp-c-text-2);background:transparent;font-size:12px}.news-filters button[aria-pressed=true]{border-color:var(--vp-c-divider);background:var(--vp-c-bg-soft);color:var(--vp-c-text-1)}.news-filters button span{font-size:10px}.news-count{font-size:11px;color:var(--vp-c-text-2);margin:0}.news-list{padding:0;margin:0;list-style:none}.news-card{display:grid;grid-template-columns:145px minmax(0,1fr);gap:38px;padding:31px 0 34px;border-bottom:1px solid var(--vp-c-divider)}.news-card-date{display:flex;flex-direction:column;gap:10px;padding-top:4px;font-size:12px;color:var(--vp-c-text-2)}.news-card-date>span{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--vp-c-brand-1)}.news-card-meta{display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;margin-bottom:12px;font-size:11px}.news-card-meta>a{color:var(--vp-c-text-2)}.news-status{padding:2px 7px;border:1px solid var(--vp-c-divider);border-radius:3px;color:var(--vp-c-brand-1);background:var(--vp-c-bg-soft);font-size:10px}.news-status.announced{color:#865d17}.news-status.preprint{color:#755184}.news-card h2{font:500 clamp(24px,2.5vw,31px)/1.2 'Newsreader Variable',Georgia,serif;letter-spacing:-.025em;margin:0 0 13px;overflow-wrap:anywhere}.news-card h2>a{color:var(--vp-c-text-1);text-decoration:none}.news-card h2>a:hover{color:var(--vp-c-brand-1)}.news-card h2 .news-arrow{font:14px var(--vp-font-family-base);color:var(--vp-c-brand-1);white-space:nowrap;margin-left:4px}.news-card-body{min-width:0}.news-card-body>p{overflow-wrap:anywhere;font-size:14px;color:var(--vp-c-text-2);line-height:1.8;max-width:760px;margin:0 0 18px}.news-related{list-style:none;padding:0;margin:0 0 14px;font-size:12px;display:flex;flex-wrap:wrap;gap:7px 18px}.news-related li{overflow-wrap:anywhere}.news-read{display:inline-flex;align-items:center;gap:12px;font-size:12px;min-height:32px}.news-empty{padding:62px 16px;text-align:center;color:var(--vp-c-text-2);font-size:14px;border-bottom:1px solid var(--vp-c-divider)}.news-empty h2{font:450 30px/1.2 'Newsreader Variable',Georgia,serif;color:var(--vp-c-text-1);margin:0 0 12px}.news-empty p{margin:0 auto 12px;max-width:430px}.news-footer{padding-top:24px;font-size:12px;color:var(--vp-c-text-2)}.news-footer>p{max-width:800px;margin:0 0 18px}.news-footer details{margin:16px 0 24px}.news-footer summary{cursor:pointer;color:var(--vp-c-text-1)}.news-footer summary span{margin-left:8px;font-size:10px}.news-footer ul{padding-left:19px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 25px;font-size:12px}.news-footer li{overflow-wrap:anywhere}.news-source-kind{display:block;font-size:10px;color:var(--vp-c-text-2)}.news-sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.dark .news-status.announced{color:#d3b873}.dark .news-status.preprint{color:#d2acdf}@media(max-width:760px){.news-page{padding:38px 24px}.news-hero{grid-template-columns:1fr;gap:24px;margin-bottom:30px}.news-hero h1{font-size:53px}.news-intro>p{max-width:560px;font-size:14px}.news-card{grid-template-columns:minmax(0,1fr);gap:12px;padding:25px 0}.news-card-date{flex-direction:row;justify-content:space-between;align-items:center;padding:0}.news-card h2{font-size:28px}.news-card-body{min-width:0}.news-card-body>p{overflow-wrap:anywhere;font-size:14px}.news-toolbar{gap:12px;padding-top:20px}.news-filters{gap:4px}.news-filters button{padding:8px 10px;min-height:44px}.news-count{width:100%}.news-footer ul{grid-template-columns:1fr}}@media(max-width:390px){.news-page{padding:30px 18px}.news-hero h1{font-size:47px}.news-filters button{font-size:11px;padding:8px}.news-filters button span{font-size:9px}.news-card h2{font-size:26px}.news-freshness>div{font-size:11px}.news-freshness>div>span:last-child{width:100%;margin-left:18px}}@media(prefers-reduced-motion:reduce){.news-page *{scroll-behavior:auto!important;transition:none!important}}@media(min-width:960px){.news-card,.news-updates>li,.news-coverage ol>li{scroll-margin-top:calc(var(--vp-nav-height,64px) + 24px)}}@media(forced-colors:active){.news-filters button[aria-pressed=true]{forced-color-adjust:none;border:2px solid Highlight;background:Highlight;color:HighlightText}}
.news-highlights{margin:26px 0 0}.news-highlights h2{font:600 10px/1.4 var(--vp-font-family-base);letter-spacing:.15em;text-transform:uppercase;color:var(--vp-c-brand-1);margin:0 0 12px;padding:0;border:0}.news-highlights h2 span{letter-spacing:.04em;text-transform:none;font-weight:400;color:var(--vp-c-text-2)}.news-highlights ol{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.news-highlights li{min-width:0}.news-highlights a{display:flex;flex-direction:column;gap:8px;height:100%;box-sizing:border-box;padding:14px 16px;border:1px solid var(--vp-c-divider);border-radius:6px;background:var(--vp-c-bg-soft);color:var(--vp-c-text-1);text-decoration:none}.news-highlights a:hover{border-color:var(--vp-c-brand-1)}.news-highlights strong{font:500 17px/1.3 'Newsreader Variable',Georgia,serif;letter-spacing:-.01em;overflow-wrap:anywhere}.news-highlight-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--vp-c-text-2)}.news-highlight-source{margin-top:auto;font-size:11px;color:var(--vp-c-text-2)}.news-story-label{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 8px;font-size:12px;color:var(--vp-c-text-1);margin:0 0 10px}.news-story-label>span{font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--vp-c-brand-1)}.news-role{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--vp-c-text-2)}.news-updated{font-size:10px;color:var(--vp-c-text-2)}.news-status.update{color:#2f5f98}.news-status.correction{color:#a3402d;border-color:currentColor}.news-summary-source{margin-right:8px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--vp-c-text-2);white-space:nowrap}.news-problems{margin:0 0 14px}.news-problems .news-problems-note{max-width:none;margin:0 0 6px;font-size:10px;line-height:1.5;letter-spacing:.06em;text-transform:uppercase;color:var(--vp-c-text-2)}.news-problems ul{list-style:none;padding:0;margin:0;display:grid;gap:6px}.news-problems li{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;min-width:0;max-width:100%}.news-problems a{position:relative;display:inline-block;max-width:100%;box-sizing:border-box;padding:3px 10px;border:1px solid var(--vp-c-divider);border-radius:999px;font-size:11px;line-height:1.6;color:var(--vp-c-text-1);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.news-problems a:hover{border-color:var(--vp-c-brand-1)}.news-problem-relation{margin-right:6px;font-size:10px;color:var(--vp-c-text-2)}.news-problems q{min-width:0;font-size:12px;line-height:1.6;color:var(--vp-c-text-2);overflow-wrap:anywhere}.news-updates,.news-coverage ol{list-style:none;padding:0;margin:20px 0 0;border-left:2px solid var(--vp-c-divider)}.news-coverage ol{margin-top:10px}.news-updates>li,.news-coverage ol>li{min-width:0;padding:4px 0 14px 16px}.news-updates .news-card-meta,.news-coverage .news-card-meta{margin-bottom:6px}.news-updates h3,.news-coverage h3{font:500 18px/1.3 'Newsreader Variable',Georgia,serif;letter-spacing:-.01em;margin:0 0 6px;overflow-wrap:anywhere}.news-updates h3 a,.news-coverage h3 a{color:var(--vp-c-text-1);text-decoration:none}.news-updates h3 a:hover,.news-coverage h3 a:hover{color:var(--vp-c-brand-1)}.news-updates h3 .news-arrow,.news-coverage h3 .news-arrow{font:12px var(--vp-font-family-base);color:var(--vp-c-brand-1)}.news-updates p,.news-coverage p{max-width:720px;margin:0 0 8px;font-size:13px;line-height:1.7;color:var(--vp-c-text-2);overflow-wrap:anywhere}.news-coverage{margin:18px 0 0;padding-top:10px;border-top:1px solid var(--vp-c-divider)}.news-coverage summary{cursor:pointer;padding:6px 0;font-size:12px;color:var(--vp-c-text-1)}.news-coverage summary span{margin-left:6px;font-size:10px;color:var(--vp-c-text-2)}.news-card:focus,.news-updates>li:focus,.news-coverage ol>li:focus{outline:none}.news-card:focus-visible,.news-updates>li:focus-visible,.news-coverage ol>li:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}.dark .news-status.update{color:#8fb3e3}.dark .news-status.correction{color:#e59a8a}@media(max-width:760px){.news-highlights ol{grid-template-columns:1fr}.news-updates>li,.news-coverage ol>li{padding-left:12px}.news-updates h3,.news-coverage h3{font-size:17px}}
</style>
