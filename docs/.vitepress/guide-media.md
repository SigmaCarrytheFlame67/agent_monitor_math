# Quickstart guide media

`<GuideVideo/>` (theme/GuideVideo.vue) shows the files in `docs/public/media/`. It
sits at the top of `docs/quickstart.md`, under the intro line and above the four
first-run steps, at the full width of the text column.
`theme/guideVideo.data.mjs` checks at build time which of them exist, reads each
image's size from its PNG header and gives every URL a content hash (`?v=`), so
a replaced file is never served from cache.

| File | Used for |
| --- | --- |
| `guide.webm` (VP9), `guide.mp4` (H.264) | 16:10 video, 1280 × 800, 30 fps; the browser takes the first it can play |
| `guide-poster.webp`, `guide-poster.png` | its poster: frame 0 of the video, under the video while it loads and plays, so nothing jumps when playback starts |
| `guide-still.webp`, `guide-still.png` | a still of the result (the Lean check, Verified), shown where nothing plays on its own |
| `guide-mobile.webm`, `guide-mobile.mp4` | square cut, 720 × 720, for screens up to 859 px wide |
| `guide-poster-mobile.*`, `guide-still-mobile.*` | the square cut's frame 0 and still |
| `guide.json` | duration, chapters (start `t`, `title`, optional `seek`), and the codec strings for `<source type>` |

The player box is 16:10, and square (at most 640 px wide) up to 859 px, so the
page never shifts when the video loads. A missing video is simply left out;
with no desktop video the Quickstart shows only the poster. The square cut is
used only when its poster and at least one of its videos exist, and it must
share the desktop cut's timeline, because both use the chapters in `guide.json`.

The player is muted and loops. It starts on its own unless the visitor prefers
reduced motion (then it shows the browser's own controls and never autoplays) or
has Save-Data on (then no video downloads until they press play).
It pauses when scrolled out of view. The pause/play button and the chapters sit
under the picture and appear once the video can play; a chapter with a `seek`
time lands there (Explore + Facts lands once the graph has finished drawing).

**Which picture shows.** While the video loads and plays, the poster is frame 0.
Where nothing plays on its own, it is the still: chosen by media query before
any script runs (`(scripting: none), (prefers-reduced-motion: reduce)`), then by
state (reduced motion, Save-Data, or no playable file). The frame-0 poster is
the homepage hero as it looked when the video was recorded (the "Proofs that
show their work." design, since replaced by the earlier "Every proof begins with
an idea." homepage).

A visitor downloads one poster and one video (measured in Chromium over one
full loop): 1,554,401 bytes on a desktop (WebM poster 24,948 + `guide.webm`
1,529,453) and 1,545,356 on a phone (17,640 + `guide-mobile.webm` 1,527,716), or
2.06 / 1.76 MB where only the MP4 plays. With reduced motion or no JavaScript,
only the still (98 kB / 46 kB; with reduced motion also ~22 kB of video
metadata). With Save-Data on, the still plus the frame-0 poster the page asked
for before its script ran (123 kB / 63 kB), and nothing more until they press play.

## The current video (cut v4, 33.8 s)

It is a silent screen recording of **one real guest session on the live site**,
recorded on 27 September 2026 (22:30–22:37 UTC) with Playwright/Chromium at
1440 × 900: the guest asks "Prove that the sum of two odd integers is even.",
the Ansatz CGM pipeline runs on Kimi K3 (36 agents, 22.0k in / 11.3k out tokens,
210 s, $0.17), and the automatic Lean stage returns **Verified**. The Quickstart's
worked example is a different run of the same problem, so its numbers differ.

- **Opening shot and closing title card** were captured on 28 September 2026
  from the staging build of the homepage of that day (the "Proofs that show
  their work." design, since replaced), served on 127.0.0.1, frame by frame
  at 30 fps and at the cut's own resolution (headless Chrome's screencast only
  gives CSS-pixel frames): the hero at 1.4× (1440 × 900 at 56/45 device pixels,
  1280 × 800 cropped at x = 256), and for the square cut the phone layout (432 px
  at 2.25×) in a 320 CSS px square on the kicker, headline and buttons, about
  1.12× the page's own hero on a 390 px phone (the full-width tagline is left
  out so the square can be that tight). The nav, the guide player itself, the
  feature cards and the footer are hidden (visibility only, nothing moves), so
  the shot is the hero alone and not a copy of the page around it. The cursor
  and click ring are the recording overlay's own, placed per frame; the button's
  hover and pressed states are the page's own CSS transitions, started by real
  mouse events and stepped per frame. The "Try as guest" click was cancelled in
  the page, so no session was created; the edit hard-cuts from it into the
  recorded console.
- **Title card:** the hero's own headline and tagline, swapped in the page for
  "Try it at ansatze.ai" / "Start as a guest — no account needed." (same type,
  same place; on the square cut two lines, "Try it at / ansatze.ai", over "No
  account needed."). Its text fades out and the hero's own text arrives again with
  the homepage's entrance animation, ending on a frame identical to frame 0, so
  the loop has no seam and no dissolve.
- **Captions** in the console part are a DOM overlay injected during recording
  (Instrument Sans pill, bottom left), not burned in afterwards. In the opening,
  the 16:10 cut draws the same pill at the recorded pill's exact geometry, so
  it stays put across the cut. Chapters: Try as guest (0 s), Ask for a proof
  (3.4 s, when its caption appears), Watch the pipeline (7.9 s), Read the proof
  (15.7 s), Explore + Facts graphs (19.1 s, seek 20.0 s), Lean 4 check (25.3 s).
- **Sped up.** Waits are compressed and not marked on screen: the run (about
  220 s) plays at 55× and 100×, the Lean build (about 88 s) at 110×. Say so in
  any copy that goes with the video (the note under the player does).
- **Hidden while recording** by injected CSS: the run id in the header, run
  label and trace ids in the monitor, and the Lean checker log (it prints a
  server toolchain path).
- **Changed in the edit**, and nothing else: the problem id in the Sandbox's
  "run started" SYSTEM card is blurred for every frame the card is on screen;
  for 1.6 s after the cut into the console, the cursor left parked where the
  old homepage's button was is painted out with the unchanged sidebar pixels
  from a later frame; and in the square cut's wide console shot, the tail of
  the recorded "Start as a guest" pill is painted out with the same (static)
  background from a later frame, because that cut draws its own pill.
- **Left out:** the redirect pages between homepage and console, and the
  seconds after Prove where the Sandbox streams the engine launch preview
  (server paths).
- **Still visible, not session or account ids:** fact content hashes, the Lean
  toolchain version and the run's date and time. The console shows only "Guest".

**Square cut.** Same timeline and chapters, used up to 859 px wide. The opening
and title card are the phone layout, framed tight. The console parts are a square crop of the
1440 × 900 recording that follows the action and changes zoom only at a cut: a
wide shot of the whole composer, a left-aligned shot while typing, wide again
for the Prove click, then the Problem card, the monitor (panning to the agent
conversation once the table has scrolled away), the proof with its header rows,
the graphs, the Lean build, the Verified proof file and the checker's report.
Crops never include the blurred card or the recorded caption pill, and cut only
long paragraph and code lines at their edges. The captions are drawn in the same
pill style at 26 px (about 13 CSS px on a 390 px phone), alpha 0.93, over a soft
bottom scrim, at the times the recorded captions change. On a phone, console
text comes out at about 7–9 CSS px, the typed problem at about 9.

**Encoding.** VP9: libvpx-vp9, two-pass constant quality (`-crf 34 -b:v 0`),
yuv420p. H.264: High profile, level 4.0, yuv420p, `-preset veryslow -tune
stillimage -crf 24 -movflags +faststart`. Posters: frame 0 as PNG and WebP
(quality 86); stills: the frame at 28.2 s as PNG and WebP (quality 84). If you
change the codec or level, update `codecs` in `guide.json` (`avc1.640028` =
High, level 4.0). Sizes: `guide.webm` 1,529,453 bytes, `guide.mp4` 2,039,284,
`guide-mobile.webm` 1,527,716, `guide-mobile.mp4` 1,746,568; WebP posters
18–25 kB, WebP stills 46–98 kB (the PNGs are fallbacks).

**Raw material is not publishable.** The raw take (screencast frames, the 30 fps
master, Playwright's own video, the step log) still shows the unblurred problem
id and the launch preview with server paths. It and the recording/edit scripts
(`record.js`, `capture_home_v4.js`, `overlay.src.js`, `pills_v4.js`,
`edit_v4.py`, `edit_spec_v4.py`, `encode_v4.sh`) stay outside the repository and
out of `docs/`. Only the twelve media files and `guide.json` above are published.

## Replacing the video

Export a 16:10 cut (1280 × 800) and, optionally, a square cut on the same
timeline; save them under the names above with frame 0 as each poster and a
representative frame as each still; update `guide.json` (duration, chapters,
codecs); run `node --test tests/*.mjs` (`tests/test_docs_home.mjs` pins the
chapter titles, 25–35 s, at most 2.5 MB per video, +faststart, and that
`media/` holds only these files); and rebuild.

The Quickstart's worked example comes from its own run; rebuild its data with
`scripts/build_quickstart_example.py` (see the script's docstring).
