<script setup>
import { computed, onMounted, ref, shallowRef } from 'vue'
import { withBase } from 'vitepress'
import { data } from './problems.data.mjs'
import { maxOverlayChars, overlayCategoryCounts, overlayMatches, parseStatusOverlay, problemCategories, problemQueryString } from './problemCatalogue.mjs'
import ProblemCategoryMark from './ProblemCategoryMark.vue'

// Counts are baked in at build time; the live status overlay replaces them when it belongs to the same catalogue.
// Collection names come from the catalogue. The notes only describe where the questions come from: what is
// solved is for each entry's category to say, not for a collection's blurb.
const collectionNotes = {
  erdos: { note: 'Number theory, combinatorics, and questions that connect them.', mark: 'EP' },
  kourovka: { note: 'A continuing collection of problems in group theory.', mark: 'KN' },
  clay: { note: 'Foundational questions, recorded progress, and recent announcements.', mark: 'MP' },
  ramanujan: { note: 'Questions surrounding factorials, tau coefficients, and modular forms.', mark: 'R' },
  hilbert: { note: 'Questions from Hilbert’s 1900 list and their later extensions.', mark: 'H' },
  smale: { note: 'Questions across dynamics, computation, and mathematical physics.', mark: 'S' },
  wikipedia: { note: 'Named questions from Wikipedia’s list of unsolved problems, linked to their articles.', mark: 'WP' },
  'wikipedia-solved': { note: 'Entries from Wikipedia’s list of problems solved since 2015; each shows its own status.', mark: 'WS' },
}
const tileNotes = {
  solved: 'Answered, and the answer is accepted.',
  'ai-proposed': 'An AI-claimed solution, not yet confirmed. Treated as unsolved.',
  partial: 'A stated part is answered; the rest is open.',
  open: 'No accepted answer yet.',
}
const order = Object.keys(collectionNotes)
const rank = id => order.includes(id) ? order.indexOf(id) : order.length
const collections = computed(() => [...data.collections].sort((a, b) => rank(a.id) - rank(b.id))
  // The catalogue's abbreviation first, so a collection carries the same mark here and on its Problems cards.
  .map(item => ({ ...item, note: collectionNotes[item.id]?.note || '', mark: item.abbreviation || collectionNotes[item.id]?.mark || '↗' })))
const overlay = shallowRef(null)
const live = computed(() => overlayCategoryCounts(overlay.value, data.content_sha256, data.total))
const tiles = computed(() => problemCategories.map(category => ({ ...category, note: tileNotes[category.id], count: (live.value || data.category_counts)[category.id] || 0 })))
const recent = computed(() => overlayMatches(overlay.value, data.content_sha256) ? overlay.value.recent_changes.slice(0, 5) : [])
const formatCount = value => Number(value || 0).toLocaleString('en-US')
const dateLabel = value => new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(Date.parse(value))
onMounted(async () => {
  if (!data.content_sha256) return
  try {
    const response = await fetch(withBase('/research/problem-status.json'), { cache: 'no-cache', signal: AbortSignal.timeout(15_000) })
    const body = response.ok ? await response.text() : ''
    if (body && body.length <= maxOverlayChars) overlay.value = parseStatusOverlay(JSON.parse(body))
  } catch {}
})
</script>

<template>
  <section class="home-problems" aria-labelledby="home-problems-title">
    <header><div><p class="home-problems-eyebrow">PROBLEMS</p><h2 id="home-problems-title">Choose a question<br><em>worth staying with.</em></h2></div><p>See which problems are solved, claimed by an AI system, partly answered or still open, and follow the evidence behind each status before starting your own approach.</p></header>
    <nav v-if="data.total" class="home-category-grid" aria-label="Problems by status">
      <a v-for="tile in tiles" :key="tile.id" :class="'is-' + tile.id" :href="withBase('/problems' + problemQueryString({ category: tile.id }))" :title="tile.description"><span class="home-category-label"><ProblemCategoryMark :category="tile.id" />{{ tile.label }}</span><strong>{{ formatCount(tile.count) }}</strong><span class="home-category-note">{{ tile.note }}</span></a>
    </nav>
    <div v-if="recent.length" class="home-recent">
      <h3>Recently changed</h3>
      <ol><li v-for="change in recent" :key="change.id + change.date"><time :datetime="change.date">{{ dateLabel(change.date) }}</time><a :href="withBase('/problems' + problemQueryString({ problem: change.id }))">{{ change.change }}</a></li></ol>
    </div>
    <nav class="home-collection-grid" :class="{ 'is-wide': collections.length > 6 }" aria-label="Problem collections">
      <a v-for="collection in collections" :key="collection.id" :href="withBase('/problems' + problemQueryString({ source: collection.id }))"><span class="collection-mark">{{ collection.mark }}</span><h3>{{ collection.name }}</h3><p>{{ collection.note }}</p><span class="collection-explore"><span><span class="explore-verb">Explore </span>{{ formatCount(collection.count) }} {{ collection.count === 1 ? 'problem' : 'problems' }}</span> <span aria-hidden="true">↗</span></span></a>
    </nav>
    <a class="home-problems-all" :href="withBase('/problems')">Browse all {{ data.total ? formatCount(data.total) + ' ' : '' }}problems →</a>
  </section>
</template>

<style scoped>
.home-problems{max-width:1280px;margin:0 auto;padding:62px 0;border-bottom:1px solid var(--vp-c-divider)}.home-problems header{display:grid;grid-template-columns:1.1fr 1fr;gap:60px;align-items:end;margin-bottom:32px}.home-problems-eyebrow{font-size:10px;letter-spacing:.13em;font-weight:650;color:var(--vp-c-brand-1);margin:0 0 20px}.home-problems h2{font:400 clamp(30px,3.2vw,45px)/1.1 'Newsreader Variable',Georgia,serif;letter-spacing:-.03em;margin:0;border:0;padding:0}.home-problems h2 em{color:var(--vp-c-brand-1);font-weight:400}.home-problems header>p{font-size:14px;line-height:1.85;color:var(--vp-c-text-2);margin:0;max-width:480px}.home-collection-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.home-collection-grid>a{display:flex;flex-direction:column;align-items:flex-start;min-width:0;padding:23px;border:1px solid var(--vp-c-divider);border-radius:6px;text-decoration:none;background:var(--vp-c-bg);transition:border-color .15s}.home-collection-grid>a:hover{border-color:var(--vp-c-brand-1)}.home-collection-grid>a:focus-visible,.home-problems-all:focus-visible{outline:2px solid var(--vp-c-brand-1);outline-offset:4px}.collection-mark{display:grid;place-items:center;min-width:32px;height:30px;border:1px solid var(--vp-c-divider);border-radius:4px;font:600 10px var(--vp-font-family-mono);color:var(--vp-c-brand-1)}.home-collection-grid h3{font:500 24px/1.2 'Newsreader Variable',Georgia,serif;color:var(--vp-c-text-1);margin:17px 0 10px}.home-collection-grid p{font-size:13px;line-height:1.75;color:var(--vp-c-text-2);margin:0 0 22px}.collection-explore{display:flex;justify-content:space-between;width:100%;gap:10px;margin-top:auto;font-size:12px;color:var(--vp-c-brand-1)}.home-problems-all{display:inline-block;margin-top:24px;font-size:13px;color:var(--vp-c-brand-1);text-decoration:none}@media(max-width:1350px){.home-problems{margin:0 48px}}@media(max-width:800px){.home-problems header{grid-template-columns:1fr;gap:20px}.home-collection-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:639px){.home-problems{margin:0 24px;padding:44px 0}.home-collection-grid{grid-template-columns:1fr}.home-collection-grid>a{padding:21px}.home-problems h2{font-size:33px}}@media(prefers-reduced-motion:reduce){.home-collection-grid>a{transition:none}}
/* Four status tiles, in the Problems page's colours (no green: it is reserved for Lean). */
.home-category-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
.home-category-grid > a { display: flex; flex-direction: column; gap: 8px; min-width: 0; padding: 20px 22px 18px; border: 1px solid var(--vp-c-divider); border-top: 3px solid var(--problem-open-line); border-radius: 6px; background: var(--vp-c-bg); text-decoration: none; transition: border-color .15s; }
.home-category-grid > a:hover { border-color: var(--vp-c-brand-1); }
.home-category-grid > a:focus-visible, .home-recent a:focus-visible { outline: 2px solid var(--vp-c-brand-1); outline-offset: 4px; }
.home-category-grid > a.is-solved { border-top-color: var(--problem-solved); }
.home-category-grid > a.is-ai-proposed { border-top-color: var(--problem-ai); }
.home-category-grid > a.is-partial { border-top-color: var(--problem-partial); }
.home-category-label { display: flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; letter-spacing: .02em; color: var(--vp-c-text-1); }
.is-solved .home-category-label .problem-category-mark { color: var(--problem-solved); }
.is-ai-proposed .home-category-label { color: var(--problem-ai); }
.is-partial .home-category-label { color: var(--problem-partial); }
.is-open .home-category-label .problem-category-mark { color: var(--problem-open); }
.home-category-grid strong { font: 400 40px/1 'Newsreader Variable', Georgia, serif; letter-spacing: -.03em; font-variant-numeric: lining-nums tabular-nums; color: var(--vp-c-text-1); }
.home-category-note { font-size: 13px; line-height: 1.6; color: var(--vp-c-text-2); }
.home-recent { margin: 0 0 14px; padding: 18px 22px; border: 1px solid var(--vp-c-divider); border-radius: 6px; background: var(--vp-c-bg-alt); }
.home-recent h3 { margin: 0 0 10px; font: 600 11px/1.4 var(--vp-font-family-base); letter-spacing: .12em; text-transform: uppercase; color: var(--vp-c-text-2); }
.home-recent ol { margin: 0; padding: 0; list-style: none; }
.home-recent li { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 14px; padding: 6px 0; font-size: 13px; line-height: 1.6; }
.home-recent time { color: var(--vp-c-text-3); font-variant-numeric: tabular-nums; }
.home-recent a { color: var(--vp-c-text-1); text-decoration: none; overflow-wrap: anywhere; }
.home-recent a:hover { color: var(--vp-c-brand-1); }
.home-collection-grid.is-wide { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.home-collection-grid.is-wide > a { padding: 21px; }
.home-collection-grid.is-wide h3 { font-size: 22px; }
@media (max-width: 1050px) {
  .home-category-grid, .home-collection-grid.is-wide { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 639px) {
  .home-category-grid { gap: 10px; }
  .home-category-grid > a { padding: 16px 16px 14px; }
  .home-category-grid strong { font-size: 32px; }
  .home-category-note { font-size: 12px; }
  /* Eight collections: two compact columns instead of eight full-width cards. */
  .home-collection-grid.is-wide { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .home-collection-grid.is-wide > a { padding: 16px; }
  .home-collection-grid.is-wide h3 { margin: 12px 0 14px; font-size: 19px; }
  .home-collection-grid.is-wide p, .home-collection-grid.is-wide .explore-verb { display: none; }
  .home-recent { padding: 16px; }
  .home-recent li { grid-template-columns: 1fr; gap: 0; }
}
@media (prefers-reduced-motion: reduce) { .home-category-grid > a { transition: none; } }
/* On phones the page already has a 24px gutter; a second one left two-column tiles too narrow for
   "AI proposed", "Millennium" or "mathematics" (they wrapped or overflowed at 320-360px). */
@media (max-width: 639px) { .home-problems { margin: 0; } }
@media (max-width: 380px) { .home-collection-grid.is-wide { grid-template-columns: minmax(0, 1fr); } }
</style>
