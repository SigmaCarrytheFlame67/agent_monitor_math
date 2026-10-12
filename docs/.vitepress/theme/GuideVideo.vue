<!-- Show the Quickstart guide: a short, silent screen recording of a real guest run over a same-size poster. -->
<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { withBase } from 'vitepress'
import { data as media } from './guideVideo.data.mjs'

const LABEL = 'Guide video, a silent screen recording of a real guest run, sped up: from the homepage the guest clicks Try as guest, asks for a proof that the sum of two odd integers is even, watches the proving pipeline run, reads the proof, opens the Explore and Facts graphs, and sees the Lean 4 check pass.'
// The picture under the video: frame 0 while the video loads and plays (so nothing jumps when it starts); a still of the
// run's Lean check where nothing plays on its own (no JavaScript, reduced motion, Save-Data, or no playable file).
const ALT = {
  any: 'A still from the guide video, a screen recording of a real guest run on Ansätze.',
  poster: 'The first frame of the guide video: the Ansätze homepage as it looked when the video was recorded, “Proofs that show their work.”, with the Try as guest button.',
  still: 'A still from the guide video: the guest run’s Lean 4 check, marked Verified, with the Lean proof file and the checker’s report.',
}
// Before any script runs, the still is chosen by media query alone.
const STILL_MEDIA = '(scripting: none), (prefers-reduced-motion: reduce)'
const stillMedia = media => `${media} and (scripting: none), ${media} and (prefers-reduced-motion: reduce)`

const desktop = media.desktop
const mobile = media.mobile
const chapters = media.chapters
const duration = media.duration
const hasVideo = Boolean(desktop && desktop.sources.length)

// poster: only the image (server render, no JavaScript, Save-Data, or nothing playable)
// loading: the video element is loading over the poster · ready: it can play · failed: back to the poster
const status = ref('poster')
const variantName = ref('desktop')
const playableBy = ref({ desktop: [], mobile: [] })
const reduced = ref(false)
const saveData = ref(false)
const blocked = ref(false)
const playing = ref(false)
const userPaused = ref(false)
const time = ref(0)
const video = ref(null)
const mounted = ref(false)

const variant = computed(() => (variantName.value === 'mobile' && mobile ? mobile : desktop))
const sources = computed(() => playableBy.value[variantName.value] || [])
const ready = computed(() => status.value === 'ready')
const active = computed(() => {
  let index = 0
  chapters.forEach((chapter, i) => { if (time.value + 0.05 >= chapter.t) index = i })
  return index
})
const end = i => (i + 1 < chapters.length ? chapters[i + 1].t : duration)
const fill = i => {
  if (i !== active.value) return i < active.value ? 1 : 0
  const span = end(i) - chapters[i].t
  return span > 0 ? Math.min(1, Math.max(0, (time.value - chapters[i].t) / span)) : 0
}
// On the client: the still whenever nothing will play on its own.
const useStill = computed(() => mounted.value && (reduced.value || saveData.value || status.value === 'failed'))
const posterUrl = computed(() => {
  const image = useStill.value && variant.value.still ? variant.value.still : variant.value.poster
  return withBase(image.webp || image.png)
})
const alt = computed(() => (!mounted.value ? ALT.any : useStill.value && desktop.still ? ALT.still : ALT.poster))
// A large play button appears only when nothing plays on its own: Save-Data, or autoplay was refused.
const showStart = computed(() => !reduced.value && (
  status.value === 'poster' ? saveData.value && sources.value.length > 0 : ready.value && blocked.value && !playing.value
))

let motion
let narrow
let observer
let frame = 0
let onScreen = true

function play() {
  const el = video.value
  if (!el || reduced.value || userPaused.value || !onScreen || !ready.value) return
  el.muted = true
  const attempt = el.play()
  // Only a refusal (NotAllowedError) means autoplay is blocked; a pause that interrupts play() (AbortError) does not.
  if (attempt && attempt.catch) {
    attempt.catch(error => {
      playing.value = false
      if (!error || error.name === 'NotAllowedError') blocked.value = true
    })
  }
}

function tick() {
  if (video.value) time.value = video.value.currentTime
  frame = playing.value ? requestAnimationFrame(tick) : 0
}

function onPlaying() {
  playing.value = true
  blocked.value = false
  if (!frame) frame = requestAnimationFrame(tick)
}

function onPause() {
  playing.value = false
  if (video.value) time.value = video.value.currentTime
}

function onTimeUpdate(event) {
  if (!playing.value) time.value = event.target.currentTime
}

function onReady() {
  if (status.value !== 'loading') return
  status.value = 'ready'
  play()
}

// A <source> that cannot load fires 'error' on itself; when the last one fails, nothing can play.
function onSourceError(event) {
  if (event.target === video.value?.querySelector('source:last-of-type')) fail()
}

function fail() {
  status.value = 'failed'
  playing.value = false
}

function start() {
  userPaused.value = false
  blocked.value = false
  if (status.value === 'poster') status.value = sources.value.length ? 'loading' : 'failed'
  else play()
}

function toggle() {
  const el = video.value
  if (!el) return
  if (el.paused) {
    userPaused.value = false
    play()
  } else {
    userPaused.value = true
    el.pause()
  }
}

// Land on the chapter's own screen: its seek time from guide.json (e.g. once a graph has finished drawing),
// else just after the chapter starts.
function seek(i) {
  const el = video.value
  if (!el || !ready.value) return
  const chapter = chapters[i]
  const t = Number.isFinite(chapter.seek) ? chapter.seek : Math.min(chapter.t + 0.4, (chapter.t + end(i)) / 2)
  el.currentTime = t
  time.value = t
  play()
}

function motionChanged() {
  reduced.value = motion.matches
  if (reduced.value) video.value?.pause()
  else play()
}

// Crossing the mobile breakpoint swaps in the other cut (the frame's shape changes with it).
function widthChanged() {
  const next = narrow.matches ? 'mobile' : 'desktop'
  if (next === variantName.value) return
  variantName.value = next
  playing.value = false
  if (status.value === 'ready') status.value = sources.value.length ? 'loading' : 'failed'
}

onMounted(() => {
  mounted.value = true
  if (!hasVideo) return
  motion = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduced.value = motion.matches
  motion.addEventListener('change', motionChanged)
  const probe = document.createElement('video')
  const canPlay = entry => (entry ? entry.sources.filter(source => probe.canPlayType(source.type) !== '') : [])
  playableBy.value = { desktop: canPlay(desktop), mobile: canPlay(mobile) }
  if (mobile) {
    narrow = window.matchMedia(mobile.media)
    narrow.addEventListener('change', widthChanged)
    variantName.value = narrow.matches ? 'mobile' : 'desktop'
  }
  saveData.value = Boolean(navigator.connection && navigator.connection.saveData)
  // With Save-Data on, nothing downloads until the visitor asks for the video.
  if (saveData.value && !reduced.value) return
  status.value = sources.value.length ? 'loading' : 'failed'
})

watch(video, el => {
  observer?.disconnect()
  if (!el || !('IntersectionObserver' in window)) return
  observer = new IntersectionObserver(([entry]) => {
    onScreen = entry.isIntersecting && entry.intersectionRatio >= 0.25
    if (!onScreen) el.pause()
    else play()
  }, { threshold: 0.25 })
  observer.observe(el)
})

onBeforeUnmount(() => {
  observer?.disconnect()
  cancelAnimationFrame(frame)
  motion?.removeEventListener('change', motionChanged)
  narrow?.removeEventListener('change', widthChanged)
})
</script>

<template>
  <section v-if="desktop" class="guide" :class="{ 'has-mobile': mobile }" aria-label="How a run works, in a short video">
    <div class="guide-frame" :class="{ 'is-ready': ready }">
      <picture class="guide-poster" :aria-hidden="ready ? 'true' : undefined">
        <template v-if="mobile && mobile.still">
          <source v-if="mobile.still.webp" :media="useStill ? mobile.media : stillMedia(mobile.media)" :srcset="withBase(mobile.still.webp)" type="image/webp">
          <source :media="useStill ? mobile.media : stillMedia(mobile.media)" :srcset="withBase(mobile.still.png)" type="image/png">
        </template>
        <template v-if="desktop.still">
          <source v-if="desktop.still.webp" :media="useStill ? undefined : STILL_MEDIA" :srcset="withBase(desktop.still.webp)" type="image/webp">
          <source :media="useStill ? undefined : STILL_MEDIA" :srcset="withBase(desktop.still.png)" type="image/png">
        </template>
        <template v-if="mobile">
          <source v-if="mobile.poster.webp" :media="mobile.media" :srcset="withBase(mobile.poster.webp)" type="image/webp">
          <source :media="mobile.media" :srcset="withBase(mobile.poster.png)" type="image/png">
        </template>
        <source v-if="desktop.poster.webp" :srcset="withBase(desktop.poster.webp)" type="image/webp">
        <img
          :src="withBase(desktop.poster.png)"
          :width="desktop.poster.width"
          :height="desktop.poster.height"
          :alt="alt"
          decoding="async"
          fetchpriority="high"
        >
      </picture>
      <video
        v-if="status === 'loading' || status === 'ready'"
        :key="variantName"
        ref="video"
        class="guide-media"
        :class="{ 'is-shown': ready }"
        :autoplay="!reduced"
        muted
        loop
        playsinline
        preload="metadata"
        :controls="reduced"
        :poster="posterUrl"
        :aria-label="LABEL"
        aria-describedby="guide-note"
        @loadedmetadata="onReady"
        @playing="onPlaying"
        @pause="onPause"
        @seeked="time = $event.target.currentTime"
        @timeupdate="onTimeUpdate"
        @error="fail"
      >
        <source v-for="source in sources" :key="source.src" :src="withBase(source.src)" :type="source.type" @error="onSourceError">
      </video>
      <button v-if="showStart" type="button" class="guide-start" aria-label="Play the guide video" @click="start">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6a.6.6 0 0 0 .9.5l7.6-4.8a.6.6 0 0 0 0-1L5.9 2.7a.6.6 0 0 0-.9.5Z" /></svg>
      </button>
    </div>

    <!-- Controls sit under the picture, never over the recording: pause/play, then the chapters. -->
    <div class="guide-controls">
      <button
        v-if="ready && !reduced"
        type="button"
        class="guide-toggle"
        :aria-label="playing ? 'Pause the guide video' : 'Play the guide video'"
        @click="toggle"
      >
        <svg v-if="playing" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="3" height="10" rx="1" /><rect x="9" y="3" width="3" height="10" rx="1" /></svg>
        <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6a.6.6 0 0 0 .9.5l7.6-4.8a.6.6 0 0 0 0-1L5.9 2.7a.6.6 0 0 0-.9.5Z" /></svg>
      </button>
      <nav v-if="chapters.length" class="guide-chapters" :class="{ 'is-ready': ready }" aria-label="Video chapters">
        <ol>
          <li v-for="(chapter, i) in chapters" :key="chapter.t">
            <button
              type="button"
              :aria-current="ready && active === i ? 'step' : undefined"
              :disabled="!ready"
              @click="seek(i)"
            >
              <span class="guide-bar" aria-hidden="true"><i :style="{ transform: `scaleX(${fill(i)})` }"></i></span>
              <span class="guide-label"><span class="guide-sr">Chapter {{ i + 1 }} of {{ chapters.length }}: </span>{{ chapter.title }}</span>
            </button>
          </li>
        </ol>
        <p class="guide-now" aria-hidden="true">{{ chapters[active].title }}<span>{{ active + 1 }} / {{ chapters.length }}</span></p>
      </nav>
    </div>

    <!-- Both texts share one cell (the other one hidden), so the link never moves when the video becomes playable;
         narrow screens get a shorter wording that fits on one line. -->
    <p id="guide-note" class="guide-note">
      <span class="guide-note-text">
        <span :class="{ 'is-off': !ready }"><span class="guide-wide">Screen recording of a real guest run, sped up</span><span class="guide-narrow">Recorded guest run, sped up</span><template v-if="duration"> · {{ Math.round(duration) }}&nbsp;s</template> · no&nbsp;audio.</span>
        <span :class="{ 'is-off': ready }">From a screen recording of a real guest run.</span>
      </span>
      <a :href="withBase('/quickstart#example')">The same problem, step by step →</a>
    </p>
  </section>
</template>

<style scoped>
/* The full width of the Quickstart's text column. */
.guide {
  width: 100%;
  margin: 24px 0 0;
}

.guide-frame {
  position: relative;
  aspect-ratio: 16 / 10;
  overflow: hidden;
  border: 1px solid var(--vp-c-divider);
  border-radius: 14px;
  /* the recording's own page colour, so the box never flashes while the poster loads */
  background: #101018;
  box-shadow: 0 1px 2px rgb(15 23 42 / 4%), 0 24px 60px -28px rgb(15 23 42 / 28%);
}

.guide-poster img,
.guide-media {
  position: absolute;
  inset: 0;
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* The video stays invisible until it can play, so a failed load never hides the poster. */
.guide-media {
  background: transparent;
  opacity: 0;
}

.guide-media.is-shown {
  opacity: 1;
}

.guide-toggle,
.guide-start {
  display: grid;
  place-items: center;
  border-radius: 50%;
  cursor: pointer;
  transition: color 150ms ease, background-color 150ms ease, border-color 150ms ease;
}

/* Under the picture, left of the chapters: a page control, never over the recording. */
.guide-toggle {
  flex: none;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--vp-c-divider);
  color: var(--vp-c-text-1);
  background: var(--vp-c-bg-soft);
}

.guide-toggle:hover {
  border-color: var(--vp-c-brand-1);
  color: var(--vp-c-brand-1);
}

.guide-start {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 64px;
  height: 64px;
  border: 0;
  color: #ffffff;
  transform: translate(-50%, -50%);
  background: var(--vp-c-brand-1);
  box-shadow: 0 8px 24px -8px rgb(15 23 42 / 45%);
}

.guide-toggle svg {
  width: 14px;
  height: 14px;
  fill: currentColor;
}

.guide-start svg {
  width: 26px;
  height: 26px;
  margin-left: 3px;
  fill: currentColor;
}

.guide-toggle:focus-visible,
.guide-start:focus-visible,
.guide-chapters button:focus-visible {
  outline: 3px solid var(--vp-c-brand-1);
  outline-offset: 2px;
}

/* The button lines up with the chapter bars, however many lines the chapter titles take. */
.guide-controls {
  container-type: inline-size;
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-top: 14px;
}

.guide-controls .guide-toggle {
  margin-top: 3px;
}

/* Chapters: a segmented progress bar that doubles as navigation. Hidden until the video can play. */
.guide-chapters {
  visibility: hidden;
  flex: 1;
  min-width: 0;
}

.guide-chapters.is-ready {
  visibility: visible;
}

.guide-chapters ol {
  display: grid;
  grid-auto-columns: minmax(0, 1fr);
  grid-auto-flow: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.guide-chapters li {
  margin: 0;
}

.guide-chapters button {
  display: flex;
  width: 100%;
  flex-direction: column;
  gap: 8px;
  padding: 6px 0 4px;
  border: 0;
  color: var(--vp-c-text-3);
  background: none;
  font-size: 13px;
  font-weight: 560;
  line-height: 1.3;
  text-align: left;
  cursor: pointer;
  transition: color 150ms ease;
}

.guide-chapters button:disabled {
  cursor: default;
}

.guide-chapters button:hover:not(:disabled),
.guide-chapters button[aria-current='step'] {
  color: var(--vp-c-text-1);
}

.guide-bar {
  position: relative;
  display: block;
  height: 3px;
  overflow: hidden;
  border-radius: 2px;
  background: var(--vp-c-divider);
}

.guide-bar i {
  position: absolute;
  inset: 0;
  background: var(--vp-c-brand-1);
  transform: scaleX(0);
  transform-origin: left center;
}

/* In the text column a chapter is about 80 to 100 px wide, so a longer title wraps onto a second line
   instead of being cut off; in the narrowest column (a 960 px window) the titles are a little smaller so they
   still take at most two lines. */
.guide-label {
  min-width: 0;
  overflow-wrap: break-word;
  text-wrap: balance;
}

@container (max-width: 600px) {
  .guide-chapters button {
    font-size: 12px;
  }
}

.guide-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.guide-now {
  display: none;
}

.guide-note {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 4px 14px;
  margin: 10px 0 0;
  color: var(--vp-c-text-3);
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
}

.guide-note-text {
  display: grid;
}

.guide-note-text > span {
  grid-area: 1 / 1;
}

.guide-note-text > .is-off {
  visibility: hidden;
}

.guide-narrow {
  display: none;
}

.guide-note a {
  color: var(--vp-c-brand-1);
  font-weight: 600;
  text-decoration: none;
}

.guide-note a:hover {
  text-decoration: underline;
}

/* The recording is of the dark theme: no dimming, and an edge that still separates it from the dark page. */
.dark .guide-frame {
  border-color: #3a4356;
  box-shadow: 0 24px 60px -30px rgb(0 0 0 / 70%);
}

/* Narrower screens: the square cut (its UI text stays legible), at most 640 px wide so a tablet's frame is not too tall;
   bars only, with the current chapter spelled out below. */
@media (max-width: 859px) {
  .has-mobile {
    width: min(100%, 640px);
    margin-inline: auto;
  }

  .has-mobile .guide-frame {
    aspect-ratio: 1 / 1;
  }

  .guide-controls {
    align-items: center;
  }

  .guide-controls .guide-toggle {
    margin-top: 0;
  }

  .guide-chapters ol {
    gap: 4px;
  }

  .guide-chapters button {
    padding: 8px 0;
  }

  .guide-label {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }

  .guide-now {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin: 2px 0 0;
    color: var(--vp-c-text-1);
    font-size: 13.5px;
    font-weight: 600;
    line-height: 1.4;
  }

  .guide-now span {
    color: var(--vp-c-text-3);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }
}

@media (max-width: 639px) {
  .guide-frame {
    border-radius: 12px;
  }
}

@media (max-width: 439px) {
  .guide-wide {
    display: none;
  }

  .guide-narrow {
    display: inline;
  }
}

@media (prefers-reduced-motion: reduce) {
  .guide-toggle,
  .guide-start,
  .guide-chapters button {
    transition: none;
  }
}
</style>
