#!/usr/bin/env node
// Build VitePress in a staging directory and atomically publish it for Caddy.
//
// Publication swaps the staging tree with docs/.vitepress/dist, preferring (1)
// GNU `mv --exchange` (coreutils >= 9.5), then (2) the Linux renameat2
// RENAME_EXCHANGE system call through python3, and only then (3) two plain
// renames, which leave the path missing for an instant but never expose a
// partially written site. DOCS_PUBLISH_METHOD=exchange|renameat2|rename forces
// one method (for testing or unusual hosts).
import { chmodSync, existsSync, mkdtempSync, realpathSync, renameSync, rmSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const RENAME_EXCHANGE = `
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
call = libc.renameat2
call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
AT_FDCWD, EXCHANGE = -100, 2
if call(AT_FDCWD, os.fsencode(sys.argv[1]), AT_FDCWD, os.fsencode(sys.argv[2]), EXCHANGE) != 0:
    raise SystemExit(os.strerror(ctypes.get_errno()))
`

export function mvSupportsExchange() {
  const help = spawnSync('mv', ['--help'], { encoding: 'utf8' })
  return help.status === 0 && /--exchange\b/.test(help.stdout || '')
}

function exchangeWithMv(staging, published) {
  const result = spawnSync(
    'mv',
    ['--exchange', '--no-copy', '--no-target-directory', staging, published],
    { stdio: 'inherit' },
  )
  return result.status === 0
}

function exchangeWithRenameat2(staging, published) {
  if (process.platform !== 'linux') return false
  const result = spawnSync('python3', ['-c', RENAME_EXCHANGE, staging, published], {
    encoding: 'utf8',
  })
  return result.status === 0
}

function replaceWithRenames(staging, published) {
  // Both paths share a parent directory, so each rename is itself atomic.
  const previous = mkdtempSync(join(dirname(published), '.dist-previous-'))
  rmSync(previous, { recursive: true, force: true })
  renameSync(published, previous)
  try {
    renameSync(staging, published)
  } catch (error) {
    renameSync(previous, published)
    throw error
  }
  // Leave the previous release where the caller's cleanup expects it.
  renameSync(previous, staging)
}

/**
 * Make `staging` visible at `published`. Afterwards `staging` holds the
 * previous release (or nothing) and the caller removes it. Returns the method.
 */
export function publish(staging, published, { method = process.env.DOCS_PUBLISH_METHOD || '' } = {}) {
  if (!existsSync(published)) {
    renameSync(staging, published)
    return 'rename-new'
  }
  const forced = method.trim().toLowerCase()
  if (forced && !['exchange', 'renameat2', 'rename'].includes(forced)) {
    throw new Error(`Unknown DOCS_PUBLISH_METHOD: ${forced}`)
  }
  if ((!forced || forced === 'exchange') && (forced === 'exchange' || mvSupportsExchange())) {
    if (exchangeWithMv(staging, published)) return 'exchange'
    if (forced) throw new Error('mv --exchange failed')
  }
  if (!forced || forced === 'renameat2') {
    if (exchangeWithRenameat2(staging, published)) return 'renameat2'
    if (forced) throw new Error('renameat2(RENAME_EXCHANGE) failed')
  }
  replaceWithRenames(staging, published)
  return 'rename'
}

function main() {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
  const vitepressRoot = join(repoRoot, 'docs', '.vitepress')
  const published = join(vitepressRoot, 'dist')
  const staging = mkdtempSync(join(vitepressRoot, '.dist-build-'))
  const vitepress = join(repoRoot, 'node_modules', '.bin', 'vitepress')

  function removeStaging() {
    if (staging.startsWith(vitepressRoot + '/') && existsSync(staging)) {
      rmSync(staging, { recursive: true, force: true })
    }
  }

  const build = spawnSync(
    vitepress,
    ['build', 'docs', '--outDir', staging],
    { cwd: repoRoot, stdio: 'inherit' },
  )
  if (build.status !== 0) {
    removeStaging()
    process.exit(build.status ?? 1)
  }

  // mkdtemp intentionally creates a private 0700 directory. The static server
  // runs as another account, so make the fully rendered release traversable
  // before it can become the published path.
  chmodSync(staging, 0o755)

  // Both directories live under .vitepress. The old release moves to
  // `staging` and is removed only after the new release is visible at the
  // path served by Caddy.
  try {
    const method = publish(staging, published)
    console.log(`Published docs/.vitepress/dist (${method}).`)
  } catch (error) {
    console.error(`Publishing failed: ${error.message}`)
    removeStaging()
    process.exit(1)
  }
  removeStaging()
}

// Run only as a script, so tests can import publish() without building.
function invokedDirectly() {
  if (!process.argv[1]) return false
  try {
    return realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
  } catch {
    return false
  }
}

if (invokedDirectly()) main()
