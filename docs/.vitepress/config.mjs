// Configure public Ansatz documentation routes, navigation, and metadata.
import { defineConfig } from 'vitepress'
import { copyFileSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

export default defineConfig({
  async buildEnd(config) {
    // Publish the Quickstart's worked example for the read-only run viewer.
    const repo = resolve(config.root, '..')
    const web = join(repo, 'agent_monitor/web')
    const base = config.site.base.replace(/\/$/, '')
    for (const name of ['run-viewer.css', 'run-viewer.js']) copyFileSync(join(web, name), join(config.outDir, name))
    mkdirSync(join(config.outDir, 'examples'), { recursive: true })
    copyFileSync(join(config.root, '.vitepress/theme/quickstartExample.json'), join(config.outDir, 'examples/odd-sum.json'))
    const html = readFileSync(join(web, 'run-viewer.html'), 'utf8')
      .replaceAll('__DATA_URL__', base + '/examples/odd-sum.json')
      .replaceAll('__ASSET_BASE__', base)
      .replaceAll('__CONSOLE_URL__', base + '/guest')
    writeFileSync(join(config.outDir, 'example-run-viewer.html'), html)
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
    outline: [2, 3],
    footer: {
      message: 'By Richard Cai and Eric Jiang'
    }
  }
})
