// Tell <GuideVideo/> at build time which guide media files exist in docs/public/media.
// A missing video leaves the poster on its own; a replaced file gets a new cache key.
import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const MEDIA = fileURLToPath(new URL('../../public/media/', import.meta.url))
const FORMATS = [
  { ext: 'webm', type: 'video/webm' },
  { ext: 'mp4', type: 'video/mp4' },
]
const VARIANTS = [
  { name: 'desktop', suffix: '' },
  // the square cut up to the width where the chapter bars lose their labels (phones and tablets in portrait)
  { name: 'mobile', suffix: '-mobile', media: '(max-width: 859px)' },
]

function versioned(file) {
  const path = MEDIA + file
  if (!existsSync(path)) return null
  const hash = createHash('sha256').update(readFileSync(path)).digest('hex').slice(0, 10)
  return `/media/${file}?v=${hash}`
}

// Width and height from a PNG's IHDR chunk, so a replaced poster keeps correct size hints.
function pngSize(file) {
  try {
    const bytes = readFileSync(MEDIA + file)
    if (bytes.toString('ascii', 12, 16) !== 'IHDR') return null
    return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) }
  } catch {
    return null
  }
}

function manifest() {
  try {
    const data = JSON.parse(readFileSync(MEDIA + 'guide.json', 'utf8'))
    // seek (optional): where a click on the chapter lands, e.g. once a graph has finished drawing
    const chapters = Array.isArray(data.chapters)
      ? data.chapters.filter(c => Number.isFinite(c.t) && typeof c.title === 'string')
        .map(c => (Number.isFinite(c.seek) && c.seek >= c.t ? { t: c.t, title: c.title, seek: c.seek } : { t: c.t, title: c.title }))
      : []
    return { duration: Number(data.duration) || 0, chapters, codecs: data.codecs || {} }
  } catch {
    return { duration: 0, chapters: [], codecs: {} }
  }
}

// An image as { webp, png, width, height }, or null without its PNG.
function image(stem) {
  const size = pngSize(stem + '.png')
  return size ? { webp: versioned(stem + '.webp'), png: versioned(stem + '.png'), ...size } : null
}

function variant({ name, suffix, media }, codecs) {
  const poster = image('guide-poster' + suffix)
  if (!poster) return null
  const sources = FORMATS
    .map(({ ext, type }) => ({ src: versioned(`guide${suffix}.${ext}`), type: codecs[ext] ? `${type}; codecs="${codecs[ext]}"` : type }))
    .filter(source => source.src)
  return {
    name,
    media: media || null,
    sources,
    // poster: frame 0, under the video while it loads and plays; still: shown where nothing plays on its own
    poster,
    still: image('guide-still' + suffix),
  }
}

export default {
  watch: ['../../public/media/guide*'],
  load() {
    const info = manifest()
    const desktop = variant(VARIANTS[0], info.codecs)
    const mobile = variant(VARIANTS[1], info.codecs)
    const hasVideo = Boolean(desktop && desktop.sources.length)
    return {
      desktop,
      // A mobile cut is used only alongside a desktop video and with its own poster and video.
      mobile: hasVideo && mobile && mobile.sources.length ? mobile : null,
      duration: hasVideo ? info.duration : 0,
      chapters: hasVideo ? info.chapters : [],
    }
  },
}
