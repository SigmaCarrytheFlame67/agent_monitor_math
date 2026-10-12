// Pin the public homepage, the merged Quickstart with its guide video, and its worked example.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'

const docs = new URL('../docs/', import.meta.url)
const read = path => readFileSync(new URL(path, docs), 'utf8')
const config = read('.vitepress/config.mjs')
const home = read('index.md')
const theme = read('.vitepress/theme/index.js')
const css = read('.vitepress/theme/custom.css')
const quickstart = read('quickstart.md')
const example = JSON.parse(read('.vitepress/theme/quickstartExample.json'))

test('navigation is News · Quickstart · DAG · Problems · Try as guest · Sign up / Sign in', () => {
  const nav = config.slice(config.indexOf('nav: ['), config.indexOf('search:'))
  const texts = [...nav.matchAll(/text: '([^']+)'/g)].map(match => match[1])
  // one name for the guest action: the nav, the Quickstart's first step and the guide video's first chapter
  assert.deepEqual(texts, ['News', 'Quickstart', 'DAG', 'Problems', 'Try as guest', 'Sign up / Sign in'])
  assert.match(nav, /\{ text: 'Problems', link: '\/problems' \}/)
  assert.match(config.slice(config.indexOf('sidebar:')), /\{ text: 'Problems & Forum', link: '\/problems' \}/)
  assert.match(quickstart, /Select <a href="\/guest">Try as guest<\/a>/)
  assert.doesNotMatch(config, /\/example-run'|\/engineering'|\/open-problems'|Engineering & reliability/)
})

test('no page links to the removed Engineering page or the old Example run page', () => {
  const pages = readdirSync(docs).filter(name => name.endsWith('.md') && !['engineering.md', 'example-run.md'].includes(name))
  const theme = readdirSync(new URL('.vitepress/theme/', docs)).filter(name => /\.(vue|js|mjs)$/.test(name))
  for (const file of [...pages, ...theme.map(name => '.vitepress/theme/' + name)]) {
    const text = read(file)
    assert.doesNotMatch(text, /["'(]\/engineering(?:[#"')]|$)/m, file)
    assert.doesNotMatch(text, /["'(]\/example-run(?:[#"')]|$)/m, file)
    assert.doesNotMatch(text, /Engineering (?:&|and) reliability/, file)
  }
})

test('old URLs are small redirects', () => {
  const exampleRun = read('example-run.md')
  assert.match(exampleRun, /location\.replace\(withBase\('\/quickstart'\)/)
  assert.match(exampleRun, /'#example'/)
  const engineering = read('engineering.md')
  assert.match(engineering, /location\.replace\(withBase\('\/'\)\)/)
  // Open Problems became Problems: deep links keep ?source=, ?problem= and the hash
  const openProblems = read('open-problems.md')
  assert.match(openProblems, /location\.replace\(withBase\('\/problems'\) \+ window\.location\.search \+ window\.location\.hash\)/)
  assert.match(openProblems, /rel: canonical\n\s+href: https:\/\/ansatze\.ai\/problems\n/)
  assert.doesNotMatch(openProblems, /<Problems|<OpenProblems/)
  for (const stub of [exampleRun, engineering, openProblems]) {
    assert.match(stub, /search: false/)
    assert.match(stub, /content: noindex,follow/)
    assert.ok(stub.split('\n').length < 40, 'redirect stubs stay tiny')
  }
  // redirect-only pages stay out of the sitemap
  assert.match(config, /overview\|example-run\|engineering\|open-problems\)\$/)
})

test('no page or console view links to /open-problems; the Problems page replaces it', () => {
  const pages = readdirSync(docs).filter(name => name.endsWith('.md') && name !== 'open-problems.md')
  const theme = readdirSync(new URL('.vitepress/theme/', docs)).filter(name => /\.(vue|js|mjs)$/.test(name))
  const web = new URL('../agent_monitor/web/', import.meta.url)
  const consoleFiles = readdirSync(web).filter(name => /\.(html|js)$/.test(name)).map(name => readFileSync(new URL(name, web), 'utf8'))
  for (const [name, text] of [...[...pages, ...theme.map(name => '.vitepress/theme/' + name)].map(file => [file, read(file)]), ...consoleFiles.map((text, i) => ['web ' + i, text])])
    assert.doesNotMatch(text, /["'(]\/open-problems(?:[?#"')]|$)/m, name)
  assert.match(readFileSync(new URL('../agent_monitor/web/console.html', import.meta.url), 'utf8'), /<a href="\/problems">Problems ↗<\/a>/)
  const page = read('problems.md')
  assert.match(page, /title: Problems & Forum/)
  assert.match(page, /import Problems from '\.\/\.vitepress\/theme\/Problems\.vue'/)
  assert.match(theme.join(' '), /Problems\.vue/)
  assert.ok(!theme.includes('OpenProblems.vue'))
})

test('home Problems section: four status tiles with build-time counts, no bundled catalogue', async () => {
  const section = read('.vitepress/theme/HomeProblemCollections.vue')
  assert.match(section, /<p class="home-problems-eyebrow">PROBLEMS<\/p>/)
  assert.match(section, /import \{ data \} from '\.\/problems\.data\.mjs'/)
  assert.doesNotMatch(section, /open-problems\.json/, 'the home page never loads the full catalogue')
  assert.match(section, /withBase\('\/problems' \+ problemQueryString\(\{ category: tile\.id \}\)\)/)
  assert.match(section, /withBase\('\/problems' \+ problemQueryString\(\{ source: collection\.id \}\)\)/)
  assert.match(section, /fetch\(withBase\('\/research\/problem-status\.json'\), \{ cache: 'no-cache'/)
  assert.match(section, /<div v-if="recent\.length" class="home-recent">/, 'Recently changed is hidden when empty')
  const { default: loader } = await import(new URL('.vitepress/theme/problems.data.mjs', docs))
  const summary = loader.load()
  const catalogue = JSON.parse(read('public/research/open-problems.json'))
  assert.equal(summary.total, catalogue.total)
  assert.deepEqual(Object.keys(summary.category_counts), ['solved', 'ai-proposed', 'partial', 'open'])
  assert.equal(Object.values(summary.category_counts).reduce((a, b) => a + b, 0), catalogue.total)
  assert.equal(summary.collections.reduce((sum, item) => sum + item.count, 0), catalogue.total)
  assert.ok(JSON.stringify(summary).length < 4000, 'only counts reach the client bundle')
})

test('Problems page: fresh catalogue and overlay, source notes, honest actions, no green', () => {
  const page = read('.vitepress/theme/Problems.vue')
  assert.match(page, /fetch\(withBase\('\/research\/open-problems\.json'\), \{ cache: 'no-cache' \}\)/)
  assert.match(page, /fetch\(withBase\('\/research\/problem-status\.json'\), \{ cache: 'no-cache'/)
  assert.match(page, /selected\.source_status_note/)
  // A category set by a decision says so, with who made it; an open listing never reads like an accepted answer.
  assert.match(page, /class="status-decision">Category set by a decision of \{\{ selected\.decision\.by \}\}/)
  assert.match(page, /const isListing = item => item\.kind === 'source-marker'/)
  // Summaries of researched entries carry TeX, rendered like the statement, and are left out when they repeat it.
  assert.match(page, /<ProblemMath v-if="!summaryRepeatsStatement\(selected\)" class="detail-summary" :text="selected\.summary" \/>/)
  // Titles, headlines and evidence text may hold $…$ math too: they are typeset, never shown as raw TeX.
  for (const field of ['problem\\.title', 'problem\\.status_headline \\|\\| problem\\.summary', 'selected\\.title', 'selected\\.status_headline', 'item\\.title', 'item\\.note', 'reference\\.label', 'signal\\.title'])
    assert.match(page, new RegExp(`<ProblemMath inline :text="${field}" />`), field)
  assert.doesNotMatch(page, /\{\{ (?:problem|selected)\.(?:title|status_headline)/)
  assert.doesNotMatch(page, /selected\.status_note/)
  assert.match(page, />Analyze the claimed solution</)
  assert.match(page, /handOff\('analyze'\)/)
  assert.match(page, />Click to solve</)
  assert.match(page, /'Matched automatically by Kimi K3' : 'Matched automatically'/)
  assert.match(page, /'from primary sources' : wiki === signals\.length \? 'from Wikipedia edits \(a secondary source\)'/)
  assert.match(page, /\$\{matched\} \$\{from\}; not reviewed; does not change the category\./)
  assert.match(page, /Upstream Lean · not re-checked by this server/)
  assert.match(page, /role="tablist"/)
  assert.match(page, /@keydown="onTabKey\(\$event, index\)"/)
  for (const file of ['.vitepress/theme/Problems.vue', '.vitepress/theme/HomeProblemCollections.vue', '.vitepress/theme/ProblemCategoryMark.vue'])
    assert.doesNotMatch(read(file), /(?:color|background|border[a-z-]*|fill|stroke)\s*:[^;}]*\bgreen\b|var\(--ok\)|#178a50|#22a060/i, file)
  // Category colours: ink, amber, violet and neutral in both themes. Green stays reserved for Lean's kernel.
  const hues = [...css.matchAll(/--problem-[a-z-]+:\s*#([0-9a-f]{6});/gi)].map(([, hex]) => {
    const [r, g, b] = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
    const hue = d === 0 ? 0 : max === r ? 60 * (((g - b) / d) % 6) : max === g ? 60 * ((b - r) / d + 2) : 60 * ((r - g) / d + 4)
    return { hex, hue: (hue + 360) % 360, saturation: d === 0 ? 0 : d / (1 - Math.abs(max + min - 1)) }
  })
  assert.ok(hues.length >= 16)
  for (const { hex, hue, saturation } of hues) assert.ok(saturation < 0.25 || hue < 70 || hue > 170, `#${hex} is not green`)
})

test('Problems page: tabs count the current view, keep focus and stay visible; amber means AI proposed only', () => {
  const page = read('.vitepress/theme/Problems.vue')
  const style = page.slice(page.indexOf('<style scoped>'))
  // every filter but the category feeds the tab counts, so a tab never promises rows the list will not show
  assert.match(page, /const counts = computed\(\(\) => categoryTabCounts\(rows\.value, filters\.value\)\)/)
  assert.match(page, /filterProblems\(rows\.value, \{ \.\.\.filters\.value, category: category\.value, sort: sort\.value \}\)/)
  // the clear buttons remove themselves, so focus moves to the results count instead of the page body
  assert.match(page, /function clearFilters\(everything = false\) \{[^}]*nextTick\(\(\) => resultsHeading\.value\?\.focus\(\)\)/)
  assert.doesNotMatch(page, /@click="resetFilters/)
  // forced colours keep the selected tab distinct; its count keeps full contrast; the strip has room for the focus ring
  // (the rule outranks the per-category selected colours, so every selected tab takes the system highlight)
  assert.match(style, /@media \(forced-colors: active\) \{\s*\.category-tabs \.category-tab\[aria-selected='true'\] \{[^}]*background: Highlight; color: HighlightText;/)
  assert.doesNotMatch(style, /\[aria-selected='true'\] \.tab-count \{[^}]*opacity/)
  const padding = Number(style.match(/\.category-tabs \{[^}]*padding: (\d+)px;/)[1])
  const offset = Number(style.match(/\.category-tab:focus-visible \{ outline-offset: (\d+)px; \}/)[1])
  assert.ok(padding >= offset + 2, 'the 2px focus ring and its offset fit inside the scrolling tab strip')
  // amber is the AI-proposed category's colour, never a warning or an actor
  for (const file of ['.vitepress/theme/Problems.vue', '.vitepress/theme/HomeProblemCollections.vue']) {
    const css = read(file).slice(read(file).indexOf('<style'))
    for (const [, selector] of css.matchAll(/([^{}]+)\{[^}]*var\(--problem-ai/g)) assert.match(selector, /ai-proposed/, `${file}: ${selector.trim()}`)
  }
  // each new report says who matched it
  assert.match(page, /<span class="chip">\{\{ signalLabels\.matched_by\[signal\.matched_by\] \}\}<\/span>/)
})

test('home Problems collections use the catalogue\'s names and claim nothing about status', () => {
  const section = read('.vitepress/theme/HomeProblemCollections.vue')
  const notes = section.slice(section.indexOf('const collectionNotes'), section.indexOf('const tileNotes'))
  assert.doesNotMatch(notes, /\bname:/, 'names come from the catalogue (e.g. "Recently solved (Wikipedia list)")')
  assert.doesNotMatch(notes, /settled|with the evidence for each answer/)
  assert.match(section, /<h3>\{\{ collection\.name \}\}<\/h3>/)
})

test('homepage is the original one: "Every proof begins with an idea." with the animated hero and its sections', () => {
  const front = home.slice(0, home.indexOf('---', 4))
  assert.match(front, /layout: home/)
  assert.match(front, /titleTemplate: An AI workspace for mathematical research/)
  assert.match(front, /\n  name: Ansätze\n  text: Every proof begins with an idea\.\n/)
  const actions = front.slice(front.indexOf('actions:'))
  assert.deepEqual([...actions.matchAll(/text: (.+)/g)].map(m => m[1]), ['Use as guest', 'Sign in', 'Quickstart'])
  assert.deepEqual([...actions.matchAll(/link: (.+)/g)].map(m => m[1]), ['/guest', '/sign-in', '/quickstart'])
  assert.match(actions, /link: \/guest\n\s+target: _self/)
  assert.match(actions, /link: \/sign-in\n\s+target: _self/)
  // the sections below the hero, in their original order
  const order = ['class="ansatz-definition"', '<AtlasPreview />', '<HomeProblemCollections />', 'class="ansatz-system"',
    'class="ansatz-workflow"', 'class="ansatz-attribution"', 'class="ansatz-next"']
  const at = order.map(marker => home.indexOf(marker))
  assert.ok(at.every(i => i > 0), 'every section is present')
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'sections keep their order')
  assert.equal((home.match(/<article><span>0\d<\/span>/g) || []).length, 6, 'six system capabilities')
  assert.equal((home.match(/class="ansatz-step"/g) || []).length, 4, 'four workflow steps')
  assert.match(home, /href="\/quickstart#how-a-cgm-run-works"/)
  const next = home.slice(home.indexOf('class="ansatz-next"'))
  assert.deepEqual([...next.matchAll(/<a href="([^"]+)"/g)].map(m => m[1]), ['/guest', '/sign-in', '/news', '/quickstart'])
  // the redesign is gone: no guide video, feature cards or short footer on the homepage
  assert.doesNotMatch(home, /GuideVideo|home-feature|home-footer|Proofs that show their work/)
})

test('homepage theme: HeroMotion in the hero, the UCLA rail and scroll reveal after it', () => {
  assert.match(theme, /'home-hero-image': \(\) => h\(HeroMotion\)/)
  assert.match(theme, /'home-hero-after': \(\) => h\(Fragment, \[h\(InstitutionRail\), h\(HomeReveal\)\]\)/)
  assert.match(theme, /'UCLA · AI \+ MATHEMATICS'/)
  for (const name of ['HeroMotion', 'HomeReveal', 'AtlasPreview', 'HomeProblemCollections', 'InstitutionRail'])
    assert.ok(existsSync(new URL(`.vitepress/theme/${name}.vue`, docs)), name)
  const motion = read('.vitepress/theme/HeroMotion.vue')
  assert.match(motion, /prefers-reduced-motion: reduce/)
  assert.match(motion, /Pause motion/)
  assert.match(read('.vitepress/theme/HomeReveal.vue'), /prefers-reduced-motion: reduce/)
  assert.match(read('.vitepress/theme/InstitutionRail.vue'), /class="ansatz-institution-rail"/)
  // the original hero and section styles, not the centred redesign
  for (const rule of ['.VPHome .VPHero.has-image .container {', '.ansatz-institution-rail {', '.ansatz-system {', '.ansatz-steps {',
    '.ansatz-next nav {', '.ansatz-definition {', '.ansatz-attribution {'])
    assert.ok(css.includes(rule), rule)
  assert.doesNotMatch(css, /\.home-features|\.home-footer/)
})

test('the guide video lives in Quickstart, right under the intro and above the four steps', () => {
  assert.match(quickstart, /import GuideVideo from '\.\/\.vitepress\/theme\/GuideVideo\.vue'/)
  const video = quickstart.indexOf('<GuideVideo />')
  assert.ok(video > quickstart.indexOf('\n# Quickstart\n'), 'under the page title')
  assert.ok(video < quickstart.indexOf('<ol class="qs-steps">'), 'before the four steps')
  assert.equal(quickstart.split('<GuideVideo />').length, 2, 'one player')
  // the intro names the video and its real length
  const { duration } = JSON.parse(read('public/media/guide.json'))
  const intro = quickstart.slice(quickstart.indexOf('\n# Quickstart\n'), video)
  assert.match(intro, new RegExp(`Watch a real run \\(${Math.round(duration)}&nbsp;s\\), then follow the four steps yourself\\.`))
  // no other page carries the player
  for (const name of readdirSync(docs).filter(name => name.endsWith('.md') && name !== 'quickstart.md'))
    assert.doesNotMatch(read(name), /GuideVideo/, name)
  // it spans the text column, and the doc's list spacing does not shift chapters 2+
  const component = read('.vitepress/theme/GuideVideo.vue')
  assert.match(component, /\.guide \{\s*width: 100%;\s*margin: 24px 0 0;\s*\}/)
  assert.match(component, /\.guide-chapters li \{\s*margin: 0;\s*\}/)
  assert.match(component, /<a :href="withBase\('\/quickstart#example'\)">The same problem, step by step →<\/a>/)
  // in that narrower column a long chapter title wraps instead of being cut off with '…'
  const label = component.match(/\n\.guide-label \{[^}]*\}/)[0]
  assert.doesNotMatch(label, /nowrap|ellipsis/)
  assert.match(label, /overflow-wrap: break-word;/)
  assert.match(component, /\.guide-controls \{\s*container-type: inline-size;/)
  assert.match(component, /@container \(max-width: 600px\) \{\s*\.guide-chapters button \{\s*font-size: 12px;/)
})

test('GuideVideo autoplays silently, respects reduced motion and keeps its box', () => {
  const component = read('.vitepress/theme/GuideVideo.vue')
  for (const attribute of [':autoplay="!reduced"', 'muted', 'loop', 'playsinline', 'preload="metadata"', ':poster=', ':controls="reduced"', ':aria-label="LABEL"'])
    assert.ok(component.includes(attribute), attribute)
  assert.match(component, /prefers-reduced-motion: reduce/)
  assert.match(component, /aspect-ratio: 16 \/ 10/, '16:10 box, the shape of the recording')
  assert.match(component, /aspect-ratio: 1 \/ 1/, 'square box for the mobile cut')
  assert.match(component, /v-if="status === 'loading' \|\| status === 'ready'"/, 'the video is added only after mount, over the poster')
  assert.match(component, /Pause the guide video/)
})

test('GuideVideo falls back to the poster when nothing can play', () => {
  const component = read('.vitepress/theme/GuideVideo.vue')
  assert.match(component, /canPlayType\(source\.type\)/, 'sources the browser cannot play are dropped')
  assert.match(component, /@loadedmetadata="onReady"/, 'controls appear only once the video can play')
  assert.match(component, /@error="onSourceError"/, 'the last failing <source> returns to the poster')
  assert.match(component, /@error="fail"/)
  assert.match(component, /v-if="ready && !reduced"/, 'the pause button appears only once the video can play')
  assert.match(component, /:disabled="!ready"/)
  assert.match(component, /\.guide-chapters \{\s*visibility: hidden;/, 'chapters keep their space but stay hidden until playable')
  assert.match(component, /<span :class="\{ 'is-off': !ready \}"><span class="guide-wide">Screen recording of a real guest run, sped up/, 'the duration is claimed only for a playing video')
  assert.match(component, /<span :class="\{ 'is-off': ready \}">From a screen recording of a real guest run\.<\/span>/)
  assert.match(component, /\.guide-note-text > \.is-off \{\s*visibility: hidden;/, 'both texts keep their space, so the link never moves')
  assert.match(component, /navigator\.connection && navigator\.connection\.saveData/, 'Save-Data waits for a click')
  assert.match(component, /:aria-hidden="ready \? 'true' : undefined"/, 'the covered poster leaves the accessibility tree')
})

test('GuideVideo: a still of the result where nothing autoplays, controls under the picture, chapters seek to their screen', () => {
  const component = read('.vitepress/theme/GuideVideo.vue')
  // frame 0 under a playing video (no jump at the start); the Lean "Verified" still for no JavaScript, reduced motion,
  // Save-Data and unplayable files: by media query before any script runs, then by state
  assert.match(component, /const STILL_MEDIA = '\(scripting: none\), \(prefers-reduced-motion: reduce\)'/)
  assert.match(component, /useStill = computed\(\(\) => mounted\.value && \(reduced\.value \|\| saveData\.value \|\| status\.value === 'failed'\)\)/)
  assert.match(component, /:media="useStill \? undefined : STILL_MEDIA"/)
  assert.match(component, /:media="useStill \? mobile\.media : stillMedia\(mobile\.media\)"/)
  // the pause/play button is in the controls row under the frame, never over the recording
  const frame = component.slice(component.indexOf('<div class="guide-frame"'), component.indexOf('<div class="guide-controls">'))
  assert.doesNotMatch(frame, /guide-toggle/)
  assert.match(component.slice(component.indexOf('<div class="guide-controls">')), /class="guide-toggle"/)
  assert.doesNotMatch(component, /\.guide-toggle \{[^}]*position: absolute/)
  // a chapter lands on its seek time when guide.json gives one
  assert.match(component, /Number\.isFinite\(chapter\.seek\) \? chapter\.seek :/)
  // the square cut up to 859 px, capped at 640 px wide (and centred) on tablets
  assert.match(component, /@media \(max-width: 859px\) \{\s*\.has-mobile \{\s*width: min\(100%, 640px\);\s*margin-inline: auto;\s*\}\s*\.has-mobile \.guide-frame \{\s*aspect-ratio: 1 \/ 1;/)
  // the note never leaves "audio." or "s" alone on a line, and narrow screens get a wording that fits on one
  assert.match(component, /\{\{ Math\.round\(duration\) \}\}&nbsp;s<\/template> · no&nbsp;audio\./)
  assert.match(component, /<span class="guide-narrow">Recorded guest run, sped up<\/span>/)
})

test('guide media loader lists only files that exist, with sizes from the files', async () => {
  const { default: loader } = await import(new URL('.vitepress/theme/guideVideo.data.mjs', docs))
  const media = loader.load()
  const png = name => {
    const bytes = readFileSync(new URL('public/media/' + name, docs))
    return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) }
  }
  assert.ok(media.desktop.poster.png && media.desktop.poster.webp)
  assert.deepEqual({ width: media.desktop.poster.width, height: media.desktop.poster.height }, png('guide-poster.png'))
  for (const [variant, suffix] of [[media.desktop, ''], [media.mobile, '-mobile']]) {
    if (!variant) continue
    for (const [ext, type] of [['webm', 'video/webm'], ['mp4', 'video/mp4']]) {
      const file = `guide${suffix}.${ext}`
      const listed = variant.sources.some(source => source.type.startsWith(type) && source.src.startsWith(`/media/${file}?v=`))
      assert.equal(listed, existsSync(new URL('public/media/' + file, docs)), file)
    }
  }
  if (media.mobile) {
    assert.equal(media.mobile.media, '(max-width: 859px)')
    assert.deepEqual({ width: media.mobile.poster.width, height: media.mobile.poster.height }, png('guide-poster-mobile.png'))
  }
  // stills are listed only when their PNG exists, with the same size as their cut's poster
  for (const [variant, suffix] of [[media.desktop, ''], [media.mobile, '-mobile']]) {
    if (!variant) continue
    const file = `guide-still${suffix}.png`
    if (!existsSync(new URL('public/media/' + file, docs))) assert.equal(variant.still, null)
    else assert.deepEqual({ width: variant.still.width, height: variant.still.height }, { width: variant.poster.width, height: variant.poster.height })
  }
  if (!media.desktop.sources.length) assert.deepEqual(media.chapters, [])
  else {
    assert.ok(media.chapters.length >= 3 && media.chapters[0].t === 0)
    assert.ok(media.chapters.every((chapter, i) => i === 0 || chapter.t > media.chapters[i - 1].t))
    assert.ok(media.duration > media.chapters.at(-1).t)
  }
})

test('guide video: the real recording, 16:10 with a square mobile cut, chapters and posters from the files', async () => {
  const { default: loader } = await import(new URL('.vitepress/theme/guideVideo.data.mjs', docs))
  const media = loader.load()
  const manifest = JSON.parse(read('public/media/guide.json'))
  assert.deepEqual(manifest.chapters.map(chapter => chapter.title),
    ['Try as guest', 'Ask for a proof', 'Watch the pipeline', 'Read the proof', 'Explore + Facts graphs', 'Lean 4 check'])
  assert.ok(manifest.duration >= 25 && manifest.duration <= 35, 'a short guide: 25–35 s')
  // a seek time lies inside its chapter (Explore + Facts: after the graph has finished drawing)
  manifest.chapters.forEach((chapter, i) => {
    if (chapter.seek === undefined) return
    const next = i + 1 < manifest.chapters.length ? manifest.chapters[i + 1].t : manifest.duration
    assert.ok(chapter.seek > chapter.t && chapter.seek < next, chapter.title)
  })
  assert.ok(Number.isFinite(manifest.chapters[4].seek), 'Explore + Facts lands on the drawn graph, not its loading state')
  assert.ok(media.desktop.still && media.mobile.still, 'stills for the states that never autoplay')
  assert.deepEqual(manifest.codecs, { webm: 'vp9', mp4: 'avc1.640028' })
  assert.deepEqual({ width: media.desktop.poster.width, height: media.desktop.poster.height }, { width: 1280, height: 800 })
  assert.ok(media.mobile, 'mobile cut present')
  assert.equal(media.mobile.poster.width, media.mobile.poster.height, 'square mobile poster')
  const bytes = name => readFileSync(new URL('public/media/' + name, docs)).length
  for (const name of ['guide.webm', 'guide.mp4', 'guide-mobile.webm', 'guide-mobile.mp4'])
    assert.ok(bytes(name) <= 2.5 * 1024 * 1024, `${name} is at most 2.5 MB`)
  // H.264 High with the moov atom before mdat (+faststart), so playback starts before the whole file arrives
  for (const name of ['guide.mp4', 'guide-mobile.mp4']) {
    const file = readFileSync(new URL('public/media/' + name, docs))
    assert.ok(file.indexOf('moov') > 0 && file.indexOf('moov') < file.indexOf('mdat'), name + ' +faststart')
  }
  // only the guide files live in media/: the old screenshot slideshow is gone
  assert.deepEqual(readdirSync(new URL('public/media/', docs)).sort(), ['guide-mobile.mp4', 'guide-mobile.webm', 'guide-poster-mobile.png',
    'guide-poster-mobile.webp', 'guide-poster.png', 'guide-poster.webp', 'guide-still-mobile.png', 'guide-still-mobile.webp',
    'guide-still.png', 'guide-still.webp', 'guide.json', 'guide.mp4', 'guide.webm'])
})

test('Quickstart merges the guide and the worked example', () => {
  for (const anchor of ['{#example}', '{#example-proof}', '{#example-facts}', '{#example-explore}', '{#example-lean}', '{#how-a-cgm-run-works}', '{#proving-engines}'])
    assert.ok(quickstart.includes(anchor), anchor)
  assert.equal((quickstart.match(/<li><strong>/g) || []).length, 4, 'four first-run steps')
  assert.match(quickstart, /<<< @\/public\/examples\/odd-sum\/Proof\.lean/)
  assert.match(quickstart, /not a Lean certificate/)
  assert.doesNotMatch(quickstart, /Erdős|Erdos/)
})

test('worked example is the real, sanitized odd + odd run', () => {
  assert.equal(example.problem, 'Prove that the sum of two odd integers is even.')
  assert.deepEqual(example.lemmas.map(lemma => lemma.label), ['Lemma 1', 'Lemma 2', 'Lemma 3', 'Target theorem'])
  assert.ok(example.lemmas.every(lemma => lemma.verifier.verdict === 'correct' && lemma.lean_decl))
  assert.equal(example.explore.nodes, 33)
  assert.equal(example.lean.status, 'verified')
  assert.equal(example.lean.sorry, 0)
  assert.deepEqual(example.lean.coverage.verified, example.lean.coverage.total)
  const lean = readFileSync(new URL('public/examples/odd-sum/Proof.lean', docs))
  assert.equal(createHash('sha256').update(lean).digest('hex'), example.lean.source_sha256)
  const text = JSON.stringify(example) + lean.toString('utf8')
  for (const pattern of [/\/var\//, /\/opt\//, /\/home\//, /@public\.local/, /guest\+/, /owner/i, /adhoc_/, /cookie/i])
    assert.doesNotMatch(text, pattern)
  for (const lemma of example.lemmas) {
    for (const field of ['statement_tex', 'proof_tex']) assert.equal((lemma[field].match(/\$/g) || []).length % 2, 0, lemma.label + ' ' + field)
    // Typesetting adds math markup only: the words are the run's own.
    const words = text => text.replace(/\$[^$]*\$/g, ' ').match(/[A-Za-z]{4,}/g)
    assert.deepEqual(words(lemma.statement_tex), words(lemma.statement.replace(/\b[ZR]\b/g, ' ')).filter(w => words(lemma.statement_tex).includes(w)))
  }
})

test('math helper renders KaTeX and escapes prose', async () => {
  const { renderMath } = await import(new URL('.vitepress/theme/mathText.js', docs))
  const html = renderMath('If <a> then $x \\in \\mathbb{Z}$.')
  assert.match(html, /class="katex"/)
  assert.match(html, /If &lt;a&gt; then /)
})

test('preprint titles: text-mode TeX outside math reads as typeset (arXiv dashes, accents, quotes)', async () => {
  const { renderMath, texText } = await import(new URL('.vitepress/theme/mathText.js', docs))
  assert.equal(texText("A proof of the Galt--Tyutyanov conjecture on Skiba's question"), "A proof of the Galt–Tyutyanov conjecture on Skiba's question")
  assert.equal(texText('A short proof of the Erd\\H os--S\\\'os Conjecture'), 'A short proof of the Erdős–Sós Conjecture')
  assert.equal(texText('G\\"odel, Pe\\~na, Ko\\c{c}, \\v{S}koda, \\L{}ukasz, Stra\\ss e'), 'Gödel, Peña, Koç, Škoda, Łukasz, Straße')
  assert.equal(texText("``Quoted'' ---and--- 50\\% \\& {L}ean"), '“Quoted” —and— 50% & Lean')
  const html = renderMath('Over $\\mathbb{F}_2$ -- after Erd\\H{o}s', { texText: true })
  assert.match(html, /class="katex"/)
  assert.match(html, /– after Erdős$/)
  assert.match(renderMath('x -- y'), /x -- y/, 'only titles opt in')
  assert.match(read('.vitepress/theme/News.vue'), /renderMath\(item\.title, \{ texText: true \}\)/)
  const { plainMath } = await import(new URL('.vitepress/theme/mathText.js', docs))
  assert.equal(plainMath('The rank of $3\\times 3$ matrix multiplication over $\\mathbb{F}_2$ is 23 -- after Erd\\H{o}s'),
    'The rank of 3 × 3 matrix multiplication over F 2 is 23 – after Erdős')
  assert.match(read('.vitepress/theme/News.vue'), /news-sr-only">: \{\{ titleText\(entry\.lead\) \}\}/, 'labels read the title as typeset')
})

test('inline math in the example stays together, and long chains break only at =', async () => {
  const { keepTogether } = await import(new URL('.vitepress/theme/mathText.js', docs))
  assert.equal(keepTogether('x = 2k + 1'), '{x = 2k + 1}')
  assert.equal(keepTogether('a+b=(2m+1)+(2n+1)=2m+2n+2=2(m+n+1)'), '{a+b}={(2m+1)+(2n+1)}={2m+2n+2}={2(m+n+1)}')
  assert.equal(keepTogether('f(x=1) + g(y) + h(z) + k(w)'), '{f(x=1) + g(y) + h(z) + k(w)}', 'only top-level = splits')
  const component = read('.vitepress/theme/ExampleRun.vue')
  assert.match(component, /renderMath\(text \|\| '', \{ keepTogether: true \}\)/)
  assert.doesNotMatch(component, /overflow-wrap: anywhere;\s*\}\s*\n\s*\.ex-math/)
})

test('Explore explains why Plan 1 closed with open subgoals, and colours follow the key', () => {
  const [closed, dead] = example.explore.plans
  assert.equal(closed.rule, 'accepted-target')
  assert.deepEqual(closed.subgoals.filter(goal => goal.status === 'open').map(goal => goal.label), ['S2', 'S4'])
  assert.equal(dead.rule, 'dead-member')
  const component = read('.vitepress/theme/ExampleRun.vue')
  assert.doesNotMatch(component, /all must close/)
  assert.match(component, /a plan closes that way even with subgoals still open/)
  // Amber marks model-verifier acceptance; green is reserved for Lean's kernel.
  assert.match(component, /\.ex-node \{[^}]*border-left: 3px solid var\(--ex-model-line\)/)
  assert.match(component, /\.ex-verdicts li b \{ color: var\(--ex-model\); \}/)
  assert.doesNotMatch(component, /#178a50|#22a060/)
  assert.doesNotMatch(component, /opacity: \.72/)
  assert.doesNotMatch(quickstart, /subgoals that all had to close/)
})
