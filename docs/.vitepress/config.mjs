// Configure public Ansatz documentation routes, navigation, and metadata.
import { defineConfig } from 'vitepress'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'

export default defineConfig({
  async buildEnd(config) {
    // Shared runs now open in the console's own read-only view, which the
    // static site cannot host; the old standalone viewer page points to the
    // Quickstart's worked example instead.
    const base = config.site.base.replace(/\/$/, '')
    const target = base + '/quickstart#example'
    writeFileSync(join(config.outDir, 'example-run-viewer.html'),
      `<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex"><meta http-equiv="refresh" content="0; url=${target}"><link rel="canonical" href="${target}"><title>Worked example · Ansätze</title><a href="${target}">The worked example is on the Quickstart page.</a>\n`)
  },
  // Preload the two body and heading fonts so they do not swap in late and reflow the page.
  transformHead({ assets }) {
    return assets
      .filter(file => /\/(?:space-grotesk|newsreader)-latin-wght-normal\.[\w-]+\.woff2$/.test(file))
      .map(href => ['link', { rel: 'preload', href, as: 'font', type: 'font/woff2', crossorigin: '' }])
  },
  title: "Ansätze",
  description: "An AI workspace for mathematical research.",
  base: '/',
  cleanUrls: true,
  // Canonical public origin; www.ansatze.ai redirects here (deploy/ansatze).
  sitemap: {
    hostname: 'https://ansatze.ai',
    // Pages that only redirect elsewhere stay out of the sitemap.
    transformItems: items => items.filter(item => !/^(?:overview|example-run|engineering|open-problems)$/.test(item.url.replace(/^\/|\.html$/g, '')))
  },
  appearance: true,
  head: [
    ['link', { rel: 'icon', href: '/favicon.svg', type: 'image/svg+xml' }],
    ['link', { rel: 'alternate icon', href: '/favicon.ico', sizes: '32x32' }],
    ['meta', { name: 'theme-color', content: '#ffffff' }],
    ['meta', { name: 'robots', content: 'index,follow' }],
    ['meta', { name: 'author', content: 'Richard Cai, Eric Jiang, Junyi Zhang' }]
  ],
  themeConfig: {
    siteTitle: 'Ansätze',
    nav: [
      { text: 'News', link: '/news' },
      { text: 'Quickstart', link: '/quickstart' },
      { text: 'DAG', link: '/dag' },
      { text: 'Problems', link: '/problems' },
      {
        // the same label as the Quickstart's first step and the guide video's first chapter
        text: 'Try as guest',
        link: '/guest',
        target: '_self'
      },
      {
        text: 'Sign up / Sign in',
        link: '/sign-in',
        target: '_self'
      }
    ],
    search: {
      provider: 'local'
    },
    sidebar: [
      { text: 'Getting Started', items: [
        { text: 'Home', link: '/' },
        { text: 'News', link: '/news' },
        { text: 'Quickstart', link: '/quickstart' },
        { text: 'DAG · Research atlas', link: '/dag' },
        { text: 'Problems & Forum', link: '/problems' }
      ]}
    ],
    outline: [2, 3]
  }
})
