// The Accounts section of the admin view (console.html): the operator's list of
// accounts with a switch for each person's access, groups, changes for several
// rows at once, access set for a group from a pasted list, accounts added with
// generated passwords (the server checks the list first; five people per request
// when they are emailed), and a new password for one person. The real functions
// run in a vm context over a small element tree whose innerHTML throws (server
// text must be set as text) and which, like a browser, loses the focus of an
// element taken out of the page; every request waits for the test to answer it,
// and the clock is fixed.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

process.env.TZ = 'UTC'
const html = readFileSync(new URL('../agent_monitor/web/console.html', import.meta.url), 'utf8')
const consoleScript = html.split('<script>').at(-1).split('</script>')[0]

// Minimal JS scanner (as in test_analyze_ui.mjs): skips strings, template
// literals, comments and regex literals, so braces inside them never end a
// function early.
function scanEnd(source, start, { stopAtSemicolon = false } = {}) {
  const keywords = /(?:^|[^\w$])(?:return|typeof|case|do|else|in|of|new|delete|void|throw|yield|await)$/
  let depth = 0, i = start
  const modes = []
  while (i < source.length) {
    const c = source[i], n = source[i + 1]
    const mode = modes.at(-1)
    if (mode === 'template') {
      if (c === '\\') { i += 2; continue }
      if (c === '`') { modes.pop(); i++; continue }
      if (c === '$' && n === '{') { modes.push(depth); depth = 0; i += 2; continue }
      i++; continue
    }
    if (c === '/' && n === '/') { i = source.indexOf('\n', i); if (i < 0) return source.length; continue }
    if (c === '/' && n === '*') { i = source.indexOf('*/', i + 2) + 2; continue }
    if (c === '"' || c === "'") {
      i++
      while (i < source.length && source[i] !== c) i += source[i] === '\\' ? 2 : 1
      i++; continue
    }
    if (c === '`') { modes.push('template'); i++; continue }
    if (c === '/') {
      const before = source.slice(start, i).trimEnd()
      if (!before || /[(,=:[!&|?{};+\-*%<>~^]$/.test(before) || keywords.test(before)) {
        i++
        let inClass = false
        while (i < source.length) {
          const r = source[i]
          if (r === '\\') { i += 2; continue }
          if (r === '[') inClass = true
          else if (r === ']') inClass = false
          else if (r === '/' && !inClass) break
          i++
        }
        i++
        while (/[a-z]/i.test(source[i] || '')) i++
        continue
      }
    }
    if (c === '{') depth++
    else if (c === '}') {
      if (depth === 0 && typeof mode === 'number') { depth = modes.pop(); i++; continue }
      depth--
      if (depth === 0 && !stopAtSemicolon) return i + 1
    } else if (c === ';' && depth === 0 && stopAtSemicolon) return i + 1
    i++
  }
  throw new Error('unterminated source at ' + start)
}
function fn(name) {
  const at = consoleScript.search(new RegExp(`(?:async )?function ${name}\\(`))
  assert.ok(at >= 0, 'missing function ' + name)
  let i = consoleScript.indexOf('(', at), depth = 0
  for (; i < consoleScript.length; i++) {
    if (consoleScript[i] === '(') depth++
    else if (consoleScript[i] === ')' && --depth === 0) break
  }
  return consoleScript.slice(at, scanEnd(consoleScript, i + 1))
}
function constant(name) {
  const at = consoleScript.indexOf(`const ${name}=`)
  assert.ok(at >= 0, 'missing const ' + name)
  return consoleScript.slice(at, scanEnd(consoleScript, at, { stopAtSemicolon: true }))
}
// The section's script: every function of it is top-level and named, so each loads by name.
const BLOCK_START = consoleScript.indexOf('/* ── admin: accounts.')
const BLOCK_END = consoleScript.indexOf('\nwireAdminAccounts();', BLOCK_START)
assert.ok(BLOCK_START > 0 && BLOCK_END > BLOCK_START, 'the accounts block')
const block = consoleScript.slice(BLOCK_START, BLOCK_END)
const NAMES = [...block.matchAll(/^(?:async )?function (\w+)\(/gm)].map(match => match[1])
const CONSTS = [...block.matchAll(/^const (\w+)=/gm)].map(match => match[1])
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value))
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)) }

// A small element tree: text, classes, attributes, children, focus (lost, as in a
// browser, when the focused element leaves the page).
let focused = null
class El {
  constructor(tag) {
    Object.assign(this, { tagName: String(tag).toUpperCase(), kids: [], parent: null, attrs: {}, dataset: {}, style: {}, className: '', id: '',
      title: '', type: '', value: '', disabled: false, checked: false, hidden: false, readOnly: false, open: false, colSpan: 1, tabIndex: 0, text: '',
      onclick: null, onchange: null, oninput: null })
  }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean)
    return {
      contains: name => names().includes(name),
      add: (...more) => { this.className = [...new Set([...names(), ...more])].join(' ') },
      remove: (...less) => { this.className = names().filter(name => !less.includes(name)).join(' ') },
      toggle: (name, on) => {
        const want = on === undefined ? !names().includes(name) : !!on
        this.className = (want ? [...new Set([...names(), name])] : names().filter(other => other !== name)).join(' ')
        return want
      },
    }
  }
  get textContent() { return this.text + this.kids.map(kid => typeof kid === 'string' ? kid : kid.textContent).join('') }
  set textContent(value) { this.detachAll(); this.text = String(value) }
  get innerHTML() { throw new Error('innerHTML read on ' + this.tagName) }
  set innerHTML(value) { throw new Error(`innerHTML set on ${this.tagName}: ${value}`) }
  get children() { return this.kids.filter(kid => typeof kid === 'object') }
  detachAll() {
    this.kids.forEach(kid => { if (typeof kid === 'object') { if (focused && kid.contains(focused)) focused = null; kid.parent = null } })
    this.kids = []; this.text = ''
  }
  setAttribute(name, value) { this.attrs[name] = String(value) }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null }
  removeAttribute(name) { delete this.attrs[name] }
  hasAttribute(name) { return name in this.attrs }
  append(...items) {
    for (const item of items) {
      if (typeof item === 'object') { item.remove(); item.parent = this; this.kids.push(item) } else this.kids.push(String(item))
    }
  }
  replaceChildren(...items) { this.detachAll(); this.append(...items) }
  after(...items) {
    const parent = this.parent; if (!parent) return
    items.forEach(item => { if (typeof item === 'object') item.remove() })
    items.forEach(item => { if (typeof item === 'object') item.parent = parent })
    parent.kids.splice(parent.kids.indexOf(this) + 1, 0, ...items)
  }
  remove() {
    if (!this.parent) return
    if (focused && this.contains(focused)) focused = null
    this.parent.kids = this.parent.kids.filter(kid => kid !== this); this.parent = null
  }
  elements() { return this.children.flatMap(kid => [kid, ...kid.elements()]) }
  // tag, .class, [attr] and [attr=value], combined (no descendant selectors).
  matches(selector) {
    const parts = /^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i.exec(selector)
    assert.ok(parts, 'selector ' + selector)
    if (parts[1] && this.tagName !== parts[1].toUpperCase()) return false
    if (!parts[2].split('.').filter(Boolean).every(name => this.classList.contains(name))) return false
    return (parts[3].match(/\[[^\]]+\]/g) || []).every(test => {
      const [, name, value] = /^\[([\w-]+)(?:="?([^"\]]*)"?)?\]$/.exec(test)
      return value === undefined ? this.hasAttribute(name) : this.getAttribute(name) === value
    })
  }
  querySelectorAll(selector) { return this.elements().filter(el => el.matches(selector)) }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
  contains(el) { for (let node = el; node; node = node.parent) if (node === this) return true; return false }
  focus() { focused = this }
  select() {}
  scrollIntoView() {}
}

// The clock: Friday 2026-10-23, 12:00 UTC.
const NOW = Date.parse('2026-10-23T12:00:00Z')
class FixedDate extends Date {
  constructor(...args) { if (args.length) super(...args); else super(NOW) }
  static now() { return NOW }
}

// Requests wait for the test: answer(status, data) or fail(message) settles the oldest.
function server() {
  const pending = [], log = []
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    const request = { url, method: options.method || 'GET', headers: options.headers || {}, cache: options.cache,
      body: options.body === undefined ? undefined : JSON.parse(options.body), resolve, reject }
    log.push(request); pending.push(request)
  })
  const answer = (status, data) => {
    const request = pending.shift(); assert.ok(request, 'no request is waiting')
    request.resolve({ ok: status >= 200 && status < 300, status, json: async () => structuredClone(data) })
    return request
  }
  const fail = message => { const request = pending.shift(); assert.ok(request, 'no request is waiting'); request.reject(new TypeError(message)); return request }
  return { fetch, pending, log, answer, fail }
}

const OPERATOR = { id: 1, email: 'admin@example.org', name: 'Workshop Operator', is_admin: true, guest: false }
const GROUP = 'Field test'
const PEOPLE = [
  ['Ada Lovelace', 'ada.lovelace'], ['Emmy Noether', 'emmy.noether'], ['Srinivasa Ramanujan', 'ramanujan'], ['Sophie Germain', 'sophie.germain'],
  ['Carl Friedrich Gauss', 'gauss'], ['Bernhard Riemann', 'riemann'], ['Henri Poincaré', 'poincare'], ['David Hilbert', 'hilbert'],
  ['Kurt Gödel', 'goedel'], ['Alan Turing', 'turing'], ['Évariste Galois', 'galois'], ['Niels Henrik Abel', 'abel'],
  ['Sofia Kovalevskaya', 'kovalevskaya'], ['Leonhard Euler', 'euler'], ['Georg Cantor', 'cantor'], ['Hermann Weyl', 'weyl'], ['John von Neumann', 'von.neumann'],
]
const OFF = ['gauss', 'poincare', 'turing', 'cantor', 'weyl']
const RESET = { subject: 'Your new Ansätze password', message: 'Dear {name},\n\nHere is a new password.\n\nSign in at: {url}\nEmail: {email}\nPassword: {password}\n\n{sender}\n' }
const DEFAULTS = { subject: 'Your Ansätze account', message: 'Dear {name},\n\nSign in at: {url}\nEmail: {email}\nPassword: {password}\n\nBest regards,\n{sender}\n',
  reset_subject: RESET.subject, reset_message: RESET.message, password_length: 12 }
const email = local => local + '@example.org'
// Members of the public site: in no group, never touched by a workshop list.
const PUBLIC = [
  { id: 50, email: 'grace.hopper@example.org', name: 'Grace Hopper', access: 'on', group: '' },
  { id: 51, email: 'paul.erdos@example.org', name: 'Paul Erdős', access: 'off', group: '' },
]
function listPayload({ emailAvailable = false, url = 'https://ansatze.example/login', extra = [], sender = { name: 'Workshop Operator', email: 'admin@example.org' },
  people = PEOPLE, group = GROUP, groups } = {}) {
  const accounts = [
    { id: 1, email: 'admin@example.org', name: 'Workshop Operator', is_admin: true, group: '', created_at: '2026-09-23T09:00:00+00:00', access: 'on', access_paused_at: null, last_sign_in_at: '2026-10-23T08:02:00.123456+00:00' },
    ...people.map(([name, local], i) => ({ id: i + 2, email: email(local), name, is_admin: false, group, created_at: '2026-10-20T10:15:00.000001+00:00',
      access: OFF.includes(local) ? 'off' : 'on', access_paused_at: OFF.includes(local) ? '2026-10-23T07:30:00+00:00' : null,
      last_sign_in_at: i === 0 ? '2026-10-23T09:14:00+00:00' : i === 1 ? '2026-10-22T18:02:00+00:00' : i === 2 ? '2026-10-20T14:05:00+00:00' : null })),
    ...extra,
  ]
  return { accounts, groups: groups ?? [...new Set(accounts.map(account => account.group).filter(Boolean))].sort(),
    email_available: emailAvailable, sign_in_url: url, sender, defaults: DEFAULTS }
}

// The section in a vm context, with the page's static parts of it as the markup has them.
function load({ user = OPERATOR } = {}) {
  focused = null
  const els = {}
  const $ = id => els[id] || (els[id] = Object.assign(new El('div'), { id }))
  for (const id of ['admin-acct-list', 'admin-acct-plan', 'admin-acct-results', 'admin-acct-preview', 'admin-acct-add-nomail', 'admin-acct-add-nourl', 'admin-acct-add-url', 'admin-acct-list-nogroup',
    'admin-acct-bulk-ask', 'admin-acct-doors', 'admin-acct-list-doors', 'admin-acct-add-full-warn'])
    $(id).hidden = true
  for (const id of ['admin-acct-on', 'admin-acct-off', 'admin-acct-group-set', 'admin-acct-group-remove', 'admin-acct-full-on', 'admin-acct-full-off', 'admin-acct-add-send', 'admin-acct-add-create', 'admin-acct-list-open'])
    $(id).disabled = true
  // The bar's controls are in the bar, as in the markup.
  $('admin-acct-bulk').append(...['admin-acct-selected', 'admin-acct-all', 'admin-acct-clear', 'admin-acct-on', 'admin-acct-off', 'admin-acct-group-name',
    'admin-acct-group-set', 'admin-acct-group-remove', 'admin-acct-full-on', 'admin-acct-full-off'].map($))
  $('admin-acct-add-length').value = '12'
  $('admin-acct-filter').value = 'all'
  $('admin-acct-list-review').className = 'acct-btn primary'
  const srv = server(), copied = [], confirms = [], logged = [], answers = { confirm: true }, listeners = []
  const document = { createElement: tag => new El(tag), get activeElement() { return focused }, body: new El('body'), execCommand: () => false }
  const quiet = (...args) => logged.push(args)
  const window = { addEventListener: (type, handler) => listeners.push([type, handler]) }
  const context = vm.createContext({ $, document, window, fetch: srv.fetch, Date: FixedDate, structuredClone,
    navigator: { clipboard: { writeText: async text => { copied.push(text) } } },
    confirm: message => { confirms.push(message); return answers.confirm },
    console: { log: quiet, info: quiet, warn: quiet, error: quiet, debug: quiet } })
  vm.runInContext([`let CURRENT_USER=${JSON.stringify(user)};`, ...CONSTS.map(constant), ...['analyzeEl', 'viewerIsAdmin', ...NAMES].map(fn), 'wireAdminAccounts();'].join('\n'), context)
  const step = code => vm.runInContext(code, context)
  return { $, els, step, srv, copied, confirms, logged, answers, listeners }
}
async function loaded(view, payload = listPayload()) {
  const done = view.step('loadAdminAccounts()')
  assert.deepEqual([view.srv.pending.length, view.srv.pending[0].url, view.srv.pending[0].method, view.srv.pending[0].body], [1, '/api/admin/accounts', 'GET', undefined])
  view.srv.answer(200, payload)
  assert.equal(await done, true)
}
const rowsOf = $ => $('admin-acct-body').querySelectorAll('tr').filter(tr => tr.classList.contains('acct-row'))
const rowOf = ($, address) => rowsOf($).find(tr => tr.dataset.email === address)
const switchOf = ($, address) => rowOf($, address).querySelector('button.acct-switch')
const access = ($, address) => { const toggle = switchOf($, address); return [toggle.getAttribute('aria-checked'), toggle.textContent, toggle.getAttribute('aria-busy')] }
const button = (root, text) => root.querySelectorAll('button').find(el => el.textContent === text)
const status = ($, id = 'admin-acct-status') => [$(id).textContent, $(id).className]
const offNow = $ => rowsOf($).filter(tr => tr.querySelector('button.acct-switch').getAttribute('aria-checked') === 'false').map(tr => tr.dataset.email.split('@')[0])
const options = select => select.children.map(option => [option.value, option.textContent])
// What the server's preview says of each line of the box (status by address).
function previewOf(view, statuses = {}, group = '') {
  const request = view.srv.pending[0]
  assert.equal(request.body.preview, true)
  return request.body.text.split('\n').map(line => {
    const found = /(\S+@\S+?)>?$/.exec(line), address = found ? found[1].replace(/^</, '').toLowerCase() : ''
    const name = found ? line.slice(0, found.index).replace(/[\s<,-]+$/, '') : ''
    const state = statuses[address] || (address ? 'new' : 'invalid')
    const row = { email: address, name, line, status: state }
    if (state === 'invalid') row.error = 'No email address on this line: ' + line
    if (state === 'exists') { row.error = 'An account with this email already exists'; if (group) Object.assign(row, { group, group_joined: true }) }
    if (state === 'new' && group) row.group = group
    if (state === 'duplicate') row.error = 'Listed more than once'
    return row
  })
}
// "Create accounts": the preview asked and answered, then its Create button.
async function previewed(view, statuses = {}, extra = {}) {
  const done = view.$('admin-acct-add-create').onclick()
  const results = previewOf(view, statuses, extra.group)
  view.srv.answer(200, { results, email_available: !!extra.emailAvailable })
  await done
  return results
}
const confirmButton = $ => $('admin-acct-preview').querySelectorAll('button')[0]

test('the list: read defensively, counted (administrators apart), with its groups; nothing can be added or applied before it is read', async () => {
  const view = load(), { $, step } = view
  // Before the first read: Create and Set access wait for it; email cannot be asked for.
  assert.deepEqual([$('admin-acct-add-create').disabled, $('admin-acct-list-open').disabled, $('admin-acct-add-send').disabled], [true, true, true])
  const read = plain(step(`adminAccountsFromResponse(${JSON.stringify({
    accounts: [{ id: 1, email: ' Admin@Example.org ', name: 'Op', is_admin: true, access: 'off', access_paused_at: 'x' }, { id: 2, email: 'g@x.org', guest: true },
      { id: 3, email: 'p@x.org', name: null, access: 'paused', group: '  Field   test ' }, { id: 4, email: 'P@X.org', name: 'Copy' }, { id: 5, email: '' }, null, 'junk'],
    groups: ['Pilot', 'Field test', ''], email_available: 'yes', sign_in_url: '', sender: { name: null, email: 'admin@example.org' },
    defaults: { subject: 'S', message: 'M', reset_subject: 'RS', reset_message: 'RM', password_length: 40 } })})`))
  // Groups exactly as the server gives them (its order and spelling; the page never cleans or cuts one, finding #5).
  assert.deepEqual(read.accounts.map(a => [a.key, a.name, a.is_admin, a.access, a.access_paused_at, a.group]),
    [['admin@example.org', 'Op', true, 'on', '', ''], ['p@x.org', '', false, 'on', '', '  Field   test ']])
  assert.deepEqual([read.groups, read.emailAvailable, read.signInUrl, read.sender, read.defaults],
    [['Pilot', 'Field test', '  Field   test '], false, null, { name: '', email: 'admin@example.org' }, { subject: 'S', message: 'M', reset_subject: 'RS', reset_message: 'RM', password_length: 12 }])
  const astral = plain(step(`adminAccountsFromResponse({accounts:[{email:'a@x.org',group:'𝔸¹-homotopy theory workshop 2026: field test (intervention arm)'}],groups:['𝔸¹-homotopy theory workshop 2026: field test (intervention arm)']}).groups`))
  assert.deepEqual(astral, ['𝔸¹-homotopy theory workshop 2026: field test (intervention arm)'])

  await loaded(view, listPayload({ extra: [...PUBLIC, { id: 60, email: 'co@example.org', name: 'Co Organizer', is_admin: true, group: '', access: 'on' }] }))
  assert.deepEqual([$('admin-acct-add-create').disabled, $('admin-acct-list-open').disabled], [false, false])
  assert.equal($('admin-acct-counts').textContent, '19 accounts · 13 access on · 6 off · 2 admins')
  assert.equal(rowsOf($).length, 21)
  assert.deepEqual($('admin-acct-body').querySelector('table').querySelectorAll('th').map(th => [th.textContent, th.getAttribute('scope')]),
    [['Select', 'col'], ['Name', 'col'], ['Email', 'col'], ['Group', 'col'], ['Created', 'col'], ['Last sign-in', 'col'], ['Access', 'col'], ['Full access', 'col'], ['Action', 'col']])
  // A person: name, email, group, when created and last signed in (local time), the switch, the row action.
  assert.deepEqual(rowOf($, 'ada.lovelace@example.org').children.map(td => td.textContent),
    ['', 'Ada Lovelace', 'ada.lovelace@example.org', 'Field test', 'Oct 20', 'Today 09:14', 'On', 'No', 'New password'])
  assert.deepEqual([rowOf($, 'emmy.noether@example.org').children[5].textContent, rowOf($, 'ramanujan@example.org').children[5].textContent,
    rowOf($, 'sophie.germain@example.org').children[5].textContent, rowOf($, 'grace.hopper@example.org').children[3].textContent], ['Yesterday 18:02', 'Oct 20 14:05', '—', '—'])
  const toggle = switchOf($, 'ada.lovelace@example.org')
  assert.deepEqual([toggle.type, toggle.getAttribute('role'), toggle.getAttribute('aria-checked'), toggle.getAttribute('aria-label'), toggle.disabled],
    ['button', 'switch', 'true', 'Access for Ada Lovelace', false])
  assert.deepEqual([access($, 'gauss@example.org'), switchOf($, 'gauss@example.org').title], [['false', 'Off', null], 'Access off since today 07:30: click to turn it on'])
  // Administrators: always on, never selected, and no new password from here (the server refuses one).
  const own = rowOf($, 'admin@example.org'), co = rowOf($, 'co@example.org')
  assert.deepEqual(own.children[1].querySelectorAll('span').map(span => span.textContent), ['Workshop Operator', 'admin', 'you'])
  assert.deepEqual([switchOf($, 'admin@example.org').disabled, own.children[6].title, own.querySelector('input').disabled],
    [true, 'Administrators always have access; it cannot be turned off.', true])
  assert.deepEqual([button(own, 'New password').disabled, button(own, 'New password').title], [true, 'Your own account: change its password from the sign-in page (Forgot password?).'])
  assert.deepEqual([button(co, 'New password').disabled, button(co, 'New password').title],
    [true, 'An administrator’s password is not changed here: they can use “Forgot password?” on the sign-in page.'])
  step("openAdminPasswordReset('co@example.org')")
  assert.equal($('admin-acct-body').querySelector('tr.acct-reset-row'), null)
  // The groups: a filter (the counts follow it), names to pick from, the list's group.
  assert.deepEqual(options($('admin-acct-filter')), [['all', 'All groups'], ['=Field test', 'Field test'], ['none', 'No group'], ['full', 'Full access']])
  assert.deepEqual(options($('admin-acct-groups')), [['Field test', 'Field test']])
  $('admin-acct-filter').value = '=Field test'; $('admin-acct-filter').onchange()
  assert.deepEqual([rowsOf($).length, $('admin-acct-counts').textContent], [17, 'Field test: 17 accounts · 12 access on · 5 off'])
  $('admin-acct-filter').value = 'none'; $('admin-acct-filter').onchange()
  assert.deepEqual([rowsOf($).map(tr => tr.dataset.email), $('admin-acct-counts').textContent],
    [['admin@example.org', 'grace.hopper@example.org', 'paul.erdos@example.org', 'co@example.org'], 'No group: 2 accounts · 1 access on · 1 off · 2 admins'])
  $('admin-acct-filter').value = 'all'; $('admin-acct-filter').onchange()
  // Search: words of the name or the address, accents ignored.
  $('admin-acct-q').value = 'godel'; $('admin-acct-q').oninput()
  assert.deepEqual([rowsOf($).map(tr => tr.dataset.email), $('admin-acct-counts').textContent], [['goedel@example.org'], '19 accounts · 13 access on · 6 off · 2 admins · 1 shown'])
  $('admin-acct-q').value = 'nobody'; $('admin-acct-q').oninput()
  assert.equal($('admin-acct-body').textContent, 'No account matches this search.')
  // The form starts from the defaults; this site cannot send email, and says so; the sign-in address is shown.
  assert.deepEqual([$('admin-acct-add-subject').value, $('admin-acct-add-message').value, $('admin-acct-add-length').value, $('admin-acct-add-sender').value],
    [DEFAULTS.subject, DEFAULTS.message, '12', 'Workshop Operator'])
  assert.deepEqual([$('admin-acct-add-send').disabled, $('admin-acct-add-nomail').hidden, $('admin-acct-add-nourl').hidden, $('admin-acct-add-url').hidden,
    $('admin-acct-add-url-value').textContent], [true, false, true, false, 'https://ansatze.example/login'])
  assert.equal($('admin-acct-add').open, false)
  assert.deepEqual(view.logged, [])

  // Email possible, but the site's address is not set: no email, and the reason.
  const nourl = load()
  await loaded(nourl, listPayload({ emailAvailable: true, url: '' }))
  assert.deepEqual([nourl.$('admin-acct-add-send').disabled, nourl.$('admin-acct-add-nomail').hidden, nourl.$('admin-acct-add-nourl').hidden, nourl.$('admin-acct-add-url').hidden],
    [true, true, false, true])
  // Both: email can be asked for. Nobody invited yet: the form is open; an operator without a name signs with the address.
  const ready = load()
  await loaded(ready, listPayload({ emailAvailable: true, sender: { name: '', email: 'op@example.org' }, people: [] }))
  assert.deepEqual([ready.$('admin-acct-add-send').disabled, ready.$('admin-acct-add-nomail').hidden, ready.$('admin-acct-add-nourl').hidden,
    ready.$('admin-acct-add').open, ready.$('admin-acct-add-sender').value], [false, true, true, true, 'op@example.org'])
  ready.$('admin-acct-add-subject').value = 'Edited'
  const again = ready.step('loadAdminAccounts()'); ready.srv.answer(200, listPayload({ emailAvailable: false })); await again
  assert.deepEqual([ready.$('admin-acct-add-subject').value, ready.$('admin-acct-add-send').disabled], ['Edited', true])

  // The first read fails: the reason, a way to try again, and nothing to add or apply meanwhile.
  const refused = load(), first = refused.step('loadAdminAccounts()')
  refused.srv.answer(502, { error: 'Bad gateway' })
  assert.equal(await first, false)
  assert.equal(refused.$('admin-acct-body').textContent, 'Accounts unavailable: Bad gateway. Try again')
  assert.deepEqual([refused.$('admin-acct-add-create').disabled, refused.$('admin-acct-add-create').title, refused.$('admin-acct-list-open').disabled,
    refused.$('admin-acct-add-send').disabled], [true, 'The accounts have not loaded yet', true, true])
  assert.equal(refused.step("adminAccountsFormProblem(adminAccountsForm())"), 'The accounts have not loaded yet: try again in a moment.')
  const retry = button(refused.$('admin-acct-body'), 'Try again')
  retry.focus()
  const second = retry.onclick()
  assert.equal(focused, null)
  refused.srv.answer(502, { error: 'Still restarting' }); await second; await flush()
  const retryAgain = button(refused.$('admin-acct-body'), 'Try again')
  assert.equal(focused, retryAgain)
  retryAgain.onclick(); refused.srv.answer(200, listPayload()); await flush()
  assert.deepEqual([rowsOf(refused.$).length, refused.$('admin-acct-add-create').disabled, focused], [18, false, refused.$('admin-acct-q')])
  // Not an operator: nothing is asked.
  const member = load({ user: { id: 7, is_admin: false } })
  assert.equal(await member.step('loadAdminAccounts()'), false)
  assert.equal(member.srv.log.length, 0)
  assert.deepEqual([nourl.logged, ready.logged, refused.logged], [[], [], []])
})

test('a switch posts its one address and changes its row in place; an administrator\'s does nothing', async () => {
  const view = load(), { $, step, srv } = view
  await loaded(view)
  const tbody = $('admin-acct-body').querySelector('tbody'), row = rowOf($, 'ada.lovelace@example.org'), toggle = switchOf($, 'ada.lovelace@example.org')
  toggle.focus()
  let done = toggle.onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.method, r.headers['Content-Type'], r.body])),
    [['/api/admin/accounts/access', 'POST', 'application/json', { emails: ['ada.lovelace@example.org'], access: 'off' }]])
  assert.deepEqual(access($, 'ada.lovelace@example.org'), ['false', 'Off', 'true'])
  assert.equal($('admin-acct-counts').textContent, '17 accounts · 11 access on · 6 off · 1 admin')
  toggle.onclick()
  assert.equal(srv.pending.length, 1)
  srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [] }); await done
  assert.deepEqual(access($, 'ada.lovelace@example.org'), ['false', 'Off', null])
  // The same row, in the same table: nothing was drawn again; the switch keeps the focus.
  assert.equal($('admin-acct-body').querySelector('tbody'), tbody)
  assert.equal(rowOf($, 'ada.lovelace@example.org'), row)
  assert.equal(focused, toggle)
  assert.equal(toggle.title, 'Access off since today 12:00: click to turn it on')
  assert.deepEqual(status($), ['Access off for Ada Lovelace: signed out until it is on again.', 'acct-status ok'])
  // The server skips it, or the request fails: the row goes back, and says why.
  done = toggle.onclick()
  srv.answer(200, { updated: [], skipped: [{ email: 'ada.lovelace@example.org', reason: 'No account uses this email' }] }); await done
  assert.deepEqual([access($, 'ada.lovelace@example.org'), status($)], [['false', 'Off', null], ['Ada Lovelace was not changed: No account uses this email.', 'acct-status err']])
  done = toggle.onclick(); srv.answer(500, { error: 'Database is locked' }); await done
  assert.deepEqual([access($, 'ada.lovelace@example.org'), status($)], [['false', 'Off', null], ['Could not turn access on for Ada Lovelace: Database is locked.', 'acct-status err']])
  done = toggle.onclick(); srv.fail('Failed to fetch'); await done
  assert.equal(status($)[0], 'Could not turn access on for Ada Lovelace: The server could not be reached (Failed to fetch).')
  done = toggle.onclick(); srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [] }); await done
  assert.deepEqual([access($, 'ada.lovelace@example.org'), toggle.title], [['true', 'On', null], 'Access on: click to turn it off (signs Ada Lovelace out at once)'])
  // An administrator's switch is disabled, and nothing would send it.
  assert.equal(switchOf($, 'admin@example.org').disabled, true)
  await step("toggleAdminAccountAccess('admin@example.org')")
  assert.deepEqual(plain(await step("setAdminAccountsAccess(['admin@example.org','nobody@example.org'],'off')")), { updated: [], skipped: [] })
  assert.equal(srv.pending.length, 0)
  assert.deepEqual(view.logged, [])
})

test('selected rows are turned on or off, or put in a group, together; once a change is made they are no longer selected', async () => {
  const view = load(), { $, srv } = view
  await loaded(view, listPayload({ extra: PUBLIC }))
  const pick = (...addresses) => { for (const address of addresses) { const box = rowOf($, address).querySelector('input'); box.checked = true; box.onchange() } }
  const bulk = () => ['admin-acct-on', 'admin-acct-off', 'admin-acct-group-set', 'admin-acct-group-remove'].map(id => $(id).disabled)
  const three = ['ada.lovelace@example.org', 'emmy.noether@example.org', 'gauss@example.org']
  assert.deepEqual([$('admin-acct-selected').textContent, bulk()], ['None selected', [true, true, true, true]])
  pick(...three)
  assert.deepEqual([$('admin-acct-selected').textContent, bulk(), rowOf($, 'emmy.noether@example.org').classList.contains('acct-selected')], ['3 selected', [false, false, false, false], true])
  const off = $('admin-acct-off')
  off.focus()
  let done = off.onclick()
  assert.deepEqual(srv.pending[0].body, { emails: three, access: 'off' })
  off.onclick()
  assert.equal(srv.pending.length, 1)
  srv.answer(200, { updated: three, skipped: [] }); await done
  assert.deepEqual(offNow($), ['ada.lovelace', 'emmy.noether', 'gauss', 'poincare', 'turing', 'cantor', 'weyl', 'paul.erdos'])
  assert.deepEqual(status($), ['Access turned off for 3 accounts. They are signed out until it is on again.', 'acct-status ok'])
  // Made: the rows are no longer selected (finding #4), and the focus, on a button now disabled, goes to the search.
  assert.deepEqual([$('admin-acct-selected').textContent, bulk(), rowOf($, 'ada.lovelace@example.org').querySelector('input').checked, focused],
    ['None selected', [true, true, true, true], false, $('admin-acct-q')])
  // A request that fails keeps the selection, to try again; one the server answers (skipping some) does not.
  pick(...three)
  done = $('admin-acct-on').onclick(); srv.answer(500, { error: 'database is locked' }); await done
  assert.deepEqual([status($)[0], $('admin-acct-selected').textContent], ['Could not turn access on: database is locked.', '3 selected'])
  done = $('admin-acct-on').onclick()
  srv.answer(200, { updated: three.slice(0, 2), skipped: [{ email: 'gauss@example.org', reason: 'No account uses this email.' }] }); await done
  assert.deepEqual([status($), $('admin-acct-selected').textContent],
    [['Access turned on for 2 accounts. Skipped 1: gauss@example.org (No account uses this email).', 'acct-status err'], 'None selected'])
  // Rows ticked while a change is on its way stay selected after it.
  pick(three[0])
  done = $('admin-acct-off').onclick()
  pick('riemann@example.org')
  srv.answer(200, { updated: [three[0]], skipped: [] }); await done
  assert.deepEqual([$('admin-acct-selected').textContent, rowOf($, 'riemann@example.org').querySelector('input').checked], ['1 selected', true])
  $('admin-acct-clear').onclick()
  assert.equal($('admin-acct-selected').textContent, 'None selected')

  // A group for the selection: a name is needed; the list is read again after.
  pick(...three)
  $('admin-acct-group-set').onclick()
  assert.deepEqual([srv.pending.length, status($)[0], focused], [0, 'Type the group’s name first (or use “Remove from group”).', $('admin-acct-group-name')])
  // The name goes as typed: the server cleans it and says which group it is (finding #10).
  $('admin-acct-group-name').value = '  Day   two  '
  done = $('admin-acct-group-set').onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.body])), [['/api/admin/accounts/group', { emails: three, group: '  Day   two  ' }]])
  srv.answer(200, { updated: three, skipped: [], group: 'Day two', group_is_new: true }); await flush()
  assert.deepEqual(srv.pending.map(r => [r.url, r.method]), [['/api/admin/accounts', 'GET']])
  const regrouped = listPayload({ extra: PUBLIC })
  for (const account of regrouped.accounts) if (three.includes(account.email)) account.group = 'Day two'
  regrouped.groups = ['Day two', 'Field test']
  srv.answer(200, regrouped); await done
  assert.deepEqual([status($), $('admin-acct-group-name').value, rowOf($, 'ada.lovelace@example.org').children[3].textContent, options($('admin-acct-filter')).map(o => o[1]),
    $('admin-acct-selected').textContent], [['3 accounts now in Day two, a new group.', 'acct-status ok'], '', 'Day two', ['All groups', 'Day two', 'Field test', 'No group', 'Full access'], 'None selected'])
  // Typed in another spelling of a group there is: the server's spelling is the one said.
  pick(three[0])
  $('admin-acct-group-name').value = 'field TEST'
  done = $('admin-acct-group-set').onclick()
  srv.answer(200, { updated: [three[0]], skipped: [], group: 'Field test', group_is_new: false }); await flush(); srv.answer(200, regrouped); await done
  assert.deepEqual(status($), ['1 account joined the existing group Field test.', 'acct-status ok'])
  pick(...three)
  done = $('admin-acct-group-remove').onclick()
  assert.deepEqual(srv.pending[0].body, { emails: three, group: '' })
  srv.answer(200, { updated: [three[0]], skipped: [] }); await flush(); srv.answer(200, regrouped); await done
  assert.deepEqual([status($), $('admin-acct-selected').textContent], [['1 account taken out of its group.', 'acct-status ok'], 'None selected'])
  assert.deepEqual(view.logged, [])
})

test('access from a list, for a group: strict reading (nothing guessed), what each line became, and nobody outside the group turned off', async () => {
  const view = load(), { $, step } = view
  await loaded(view, listPayload({ extra: [...PUBLIC,
    // The reviewer's accounts: a surname-only address, a first-name-only address, and Rob Smith (robert.smith@).
    { id: 40, email: 'robert.smith@uni.example', name: 'Rob Smith', access: 'on', group: GROUP }, { id: 41, email: 'smith@uni2.example', name: 'John Smith', access: 'off', group: GROUP },
    { id: 42, email: 'robert@mail.example', name: 'Robert Brown', access: 'off', group: '' },
    { id: 43, email: "siobhan.o'brien@example.org", name: "Siobhan O'Brien", access: 'on', group: GROUP }, { id: 44, email: 'brien@example.org', name: 'B. Rien', access: 'off', group: GROUP },
    { id: 45, email: 'john.smith@a.example', name: 'John Smith', access: 'off', group: GROUP }] }))
  const plan = lines => plain(step(`(()=>{ const p=adminAccessPlan(${JSON.stringify(lines.join('\n'))},ACCOUNTS.list,'Field test');
    const keys=list=>list.map(a=>a.key); return {on:keys(p.on),turnOn:keys(p.turnOn),turnOff:keys(p.turnOff),outside:keys(p.outside),off:keys(p.off),admins:keys(p.admins),
      text:adminAccessPlanText(p),several:p.several.map(l=>l.entry),
      lines:p.lines.map(l=>({entry:l.entry,accounts:keys(l.accounts),problem:l.problem,notes:l.notes,matches:keys(l.matches),suggest:keys(l.suggest),extra:l.extra}))}; })()`))
  const line = lines => plan(lines).lines[0]
  // The reviewer's cases (finding #0): the address decides; a name beside it never adds anyone. Beside an
  // address only that account's name may be written (round 3): this account is Rob Smith, so "Robert" is
  // not his name, and nobody is turned on for the line (a warning says so).
  let read = plan(['Smith, Robert <robert.smith@uni.example>'])
  assert.deepEqual([read.on, read.lines[0].problem, read.lines[0].extra, read.lines[0].matches, read.lines[0].notes],
    [[], 'extra', ['Robert'], ['robert.smith@uni.example'], []])
  assert.ok(!read.on.includes('smith@uni2.example') && !read.on.includes('robert@mail.example'))
  // ...and every other account of the group goes off: John Smith (smith@) stays off, Ada (a control participant) goes off.
  assert.ok(read.off.includes('smith@uni2.example') && read.turnOff.includes('ada.lovelace@example.org'))
  assert.deepEqual(plain([line(['Robert <robert.smith@uni.example>']).accounts, line(['Robert <robert.smith@uni.example>']).extra]), [[], ['Robert']])
  assert.deepEqual([line(['Smith, Rob <robert.smith@uni.example>']).accounts, line(['Smith, Rob <robert.smith@uni.example>']).problem], [['robert.smith@uni.example'], ''])
  // A word is never an address before its @: "Smith" and "Smith, Robert" name nobody (a suggestion is shown, never applied).
  assert.deepEqual(line(['Smith']), { entry: 'Smith', accounts: [], problem: 'unmatched', notes: [], matches: [],
    suggest: ['robert.smith@uni.example', 'smith@uni2.example', 'john.smith@a.example'] })
  assert.deepEqual([line(['Smith, Robert']).problem, line(['Smith, Robert']).accounts, plan(['Smith, Robert']).on], ['unmatched', [], []])
  assert.deepEqual([line(['ramanujan']).problem, line(['ramanujan']).suggest], ['unmatched', ['ramanujan@example.org']])
  // An address no account has: nobody is turned on, and the name beside it is not used (finding #11).
  assert.deepEqual(line(['Ada Lovelace <ada.lovelace+workshop@example.org>']), { entry: 'Ada Lovelace <ada.lovelace+workshop@example.org>', accounts: [], problem: 'address',
    notes: ['no account has this address, and the name beside it is not used to find one'], matches: [], suggest: [] })
  // A name two accounts have: nobody, with both addresses; the address picks one, without a warning (finding #11).
  assert.deepEqual(plain([line(['John Smith']).problem, line(['John Smith']).matches]), ['ambiguous', ['smith@uni2.example', 'john.smith@a.example']])
  assert.deepEqual(plain([line(['John Smith <john.smith@a.example>']).accounts, line(['John Smith <john.smith@a.example>']).problem, line(['John Smith <john.smith@a.example>']).notes]),
    [['john.smith@a.example'], '', []])
  // Exact full names only: case, accents, word order and list markers aside (no title is dropped).
  read = plan(['NOETHER, emmy', 'Kurt Godel', '1. Évariste Galois', '- Sophie Germain', 'Abel, Niels Henrik', 'Prof. David Hilbert'])
  assert.deepEqual(read.on, ['emmy.noether@example.org', 'sophie.germain@example.org', 'goedel@example.org', 'galois@example.org', 'abel@example.org'])
  assert.deepEqual([read.lines[5].problem, read.lines[5].suggest], ['unmatched', ['hilbert@example.org']])
  // Several people on one line: by addresses, or at commas when each part is an exact full name; shown to be checked.
  read = plan(['ada.lovelace@example.org, emmy.noether@example.org', 'Sofia Kovalevskaya, Leonhard Euler', 'Sofia Kovalevskaya, Leonhard Eulr'])
  assert.deepEqual([read.on, read.several, read.lines[2].problem], [['ada.lovelace@example.org', 'emmy.noether@example.org', 'kovalevskaya@example.org', 'euler@example.org'],
    ['ada.lovelace@example.org, emmy.noether@example.org', 'Sofia Kovalevskaya, Leonhard Euler'], 'unmatched'])
  // A name on an address line that is none of its accounts' stops the line (finding #6, then round 3); the
  // accounts' own names, an initial, nothing at all, are taken.
  read = plan(['ada.lovelace@example.org, emmy.noether@example.org, Srinivasa Ramanujan', 'Bernhard Riemann <riemann@example.org>', 'Ada L <ada.lovelace@example.org>'])
  assert.deepEqual([read.on, read.lines[0].problem, read.lines[0].extra, read.lines[1].notes, read.lines[2].notes],
    [['ada.lovelace@example.org', 'riemann@example.org'], 'extra', ['Srinivasa', 'Ramanujan'], [], []])
  // An apostrophe belongs to an address; a shorter address that ends the same is someone else (finding #18).
  read = plan(["siobhan.o'brien@example.org", "Siobhan O'Brien <siobhan.o'brien@example.org>"])
  assert.deepEqual([read.on, read.off.includes('brien@example.org')], [["siobhan.o'brien@example.org"], true])
  // Listed outside the group: on, and flagged; nobody outside the group is turned off (Paul Erdős stays off, Grace Hopper on).
  read = plan(['Grace Hopper', 'Alan Turing', 'Workshop Operator'])
  assert.deepEqual([read.on, read.outside, read.turnOn, read.admins, read.off.includes('paul.erdos@example.org'), read.text],
    [['turing@example.org', 'grace.hopper@example.org'], ['grace.hopper@example.org'], ['turing@example.org'], ['admin@example.org'], false, '2 on, 21 off'])

  // Drawn: what each warning says Apply does, and "How each line was read" open because of them.
  view.step(`ACCOUNTS.plan=adminAccessPlan(${JSON.stringify(['Smith, Robert <robert.smith@uni.example>', 'Smith', 'John Smith', 'Ada Lovelace <ada.lovelace+workshop@example.org>',
    'ada.lovelace@example.org, emmy.noether@example.org', 'ramanujan@example.org, ramanujan+ws@example.org'].join('\n'))},ACCOUNTS.list,'Field test'); renderAdminAccessPlan();`)
  const drawn = $('admin-acct-plan')
  const boxes = drawn.children.filter(el => el.classList.contains('acct-warn') || el.classList.contains('acct-note-box')).map(el => el.textContent)
  assert.deepEqual(boxes, [
    "Nobody is turned on for these 4 lines:“Smith, Robert <robert.smith@uni.example>” The line also has “Robert”, which is not part of this account's name (Rob Smith). Remove it, or leave the person off the list."
      + '“Smith” No account has this name. Did you mean Rob Smith (robert.smith@uni.example), John Smith (smith@uni2.example) or John Smith (john.smith@a.example)? Use the full name as the table shows it, or the address.'
      + '“John Smith” 2 accounts have this name (smith@uni2.example, john.smith@a.example): use the address of the one meant.'
      + "“Ada Lovelace <ada.lovelace+workshop@example.org>” No account has this address, and the name beside it is not used to find one. Use the account's address as the table shows it.",
    'One line, several people — check (1):“ada.lovelace@example.org, emmy.noether@example.org” → Ada Lovelace (ada.lovelace@example.org), Emmy Noether (emmy.noether@example.org).',
    // What is still a note: a second address no account has, on a line whose other address is someone's.
    'Notes (1):“ramanujan@example.org, ramanujan+ws@example.org” → Srinivasa Ramanujan (ramanujan@example.org): no account has the address ramanujan+ws@example.org: nobody is turned on for it.'])
  const details = drawn.querySelector('details')
  assert.equal(details.open, true)
  assert.deepEqual(details.querySelectorAll('li').map(li => li.textContent), [
    "“Smith, Robert <robert.smith@uni.example>” → nobody: “Robert” is not part of Rob Smith's name",
    '“Smith” → nobody: no account has this name', '“John Smith” → nobody: more than one account has this name',
    '“Ada Lovelace <ada.lovelace+workshop@example.org>” → nobody: no account has this address',
    '“ada.lovelace@example.org, emmy.noether@example.org” → Ada Lovelace (ada.lovelace@example.org), Emmy Noether (emmy.noether@example.org)',
    '“ramanujan@example.org, ramanujan+ws@example.org” → Srinivasa Ramanujan (ramanujan@example.org) (no account has the address ramanujan+ws@example.org: nobody is turned on for it)'])
  // Everything read exactly: no warning, and the details stay closed.
  view.step(`ACCOUNTS.plan=adminAccessPlan('Ada Lovelace\\nrobert.smith@uni.example',ACCOUNTS.list,'Field test'); renderAdminAccessPlan();`)
  assert.deepEqual([$('admin-acct-plan').querySelector('details').open, $('admin-acct-plan').children.filter(el => el.classList.contains('acct-warn')).length], [false, 0])
  view.step('ACCOUNTS.plan=null; renderAdminAccessPlan();')

  // In the page: the group (the only one, so chosen), Review reads the accounts again, the plan, then Apply.
  const ui = load(), $u = ui.$
  await loaded(ui, listPayload({ extra: PUBLIC }))
  $u('admin-acct-list-open').onclick()
  assert.deepEqual([$u('admin-acct-list').hidden, $u('admin-acct-list-open').getAttribute('aria-expanded'), $u('admin-acct-list-group').value, focused],
    [false, 'true', GROUP, $u('admin-acct-list-text')])
  assert.deepEqual([options($u('admin-acct-list-group')), $u('admin-acct-list-scope').textContent, $u('admin-acct-list-nogroup').hidden],
    [[['', 'Choose a group…'], [GROUP, GROUP]], 'Everyone else in Field test is turned off; accounts outside the group are not touched.', true])
  $u('admin-acct-list-review').onclick()
  assert.deepEqual([status($u)[0], ui.srv.pending.length], ['Paste the email addresses or names of the people who should have access, one per line.', 0])
  const list = ['ada.lovelace@example.org', 'Emmy Noether', 'Ramanujan, Srinivasa', 'Sophie Germain', 'Bernhard Riemann <riemann@example.org>', 'David Hilbert',
    'Kurt Gödel', 'Évariste Galois', 'abel@example.org', 'Sofia Kovalevskaya', 'Leonhard Euler', 'Alan Turing', 'Emmy N.']
  $u('admin-acct-list-text').value = list.join('\n')
  const review = $u('admin-acct-list-review')
  review.focus()
  let done = review.onclick()
  // The accounts are read again first, so the plan is today's list (finding #6).
  assert.deepEqual([ui.srv.pending.map(r => [r.url, r.method]), review.textContent, review.getAttribute('aria-busy'), focused],
    [[['/api/admin/accounts', 'GET']], 'Reading the accounts…', 'true', review])
  ui.srv.answer(200, listPayload({ extra: PUBLIC })); await done
  const box = $u('admin-acct-plan')
  assert.deepEqual([box.hidden, focused, review.textContent, review.classList.contains('primary')], [false, box, 'Review', false])
  assert.deepEqual(box.children.map(el => el.tagName === 'DETAILS' ? el.children[0].textContent : el.textContent), [
    '12 on, 5 off', 'In Field test: everyone the list does not name is turned off. Accounts outside the group are not touched.',
    'Turn on (1): Alan Turing', 'Turn off (1): John von Neumann — signed out at once.', '15 other accounts already as listed.',
    'Nobody is turned on for this line:“Emmy N.” No account has this name. Use the full name as the table shows it, or the address.',
    'How each line was read (13)', 'Apply: 12 on, 5 offEdit the list'])
  assert.deepEqual([box.querySelector('details').open, box.querySelector('details').querySelectorAll('li')[2].textContent],
    [true, '“Ramanujan, Srinivasa” → Srinivasa Ramanujan (ramanujan@example.org)'])
  // Apply: one request, in place, keeping the focus (finding #21); the group's other accounts off on the server.
  const apply = button(box, 'Apply: 12 on, 5 off')
  apply.focus()
  done = apply.onclick()
  assert.deepEqual([apply.textContent, apply.getAttribute('aria-busy'), focused], ['Applying…', 'true', apply])
  assert.deepEqual(plain(ui.srv.pending.map(r => [r.url, r.body.access, r.body.others, r.body.group, r.body.emails.map(a => a.split('@')[0])])), [['/api/admin/accounts/access', 'on', 'off', GROUP,
    ['ada.lovelace', 'emmy.noether', 'ramanujan', 'sophie.germain', 'riemann', 'hilbert', 'goedel', 'turing', 'galois', 'abel', 'kovalevskaya', 'euler']]])
  ui.srv.answer(200, { updated: ui.srv.pending[0].body.emails, turned_off: ['von.neumann@example.org', 'late@example.org'], skipped: [] }); await flush()
  // The panel closes, the focus goes back to its button, and the table is read again.
  assert.deepEqual([$u('admin-acct-list').hidden, $u('admin-acct-plan').hidden, focused, ui.srv.pending.map(r => [r.url, r.method])],
    [true, true, $u('admin-acct-list-open'), [['/api/admin/accounts', 'GET']]])
  const after = listPayload({ extra: [...PUBLIC, { id: 70, email: 'late@example.org', name: 'Late Comer', access: 'off', group: GROUP }] })
  for (const account of after.accounts) {
    if (account.email === 'turing@example.org') account.access = 'on'
    if (account.email === 'von.neumann@example.org') account.access = 'off'
  }
  ui.srv.answer(200, after); await done
  assert.deepEqual(status($u), ['Access set from the list for Field test: 12 on, 6 off (2 turned off just now). One was not in the reviewed list (added to the group since): late@example.org.', 'acct-status ok'])
  assert.deepEqual(offNow($u), ['gauss', 'poincare', 'cantor', 'weyl', 'von.neumann', 'paul.erdos', 'late'])

  // A plan is dropped when the list is read again, or the list or the group changes.
  $u('admin-acct-list-open').onclick(); done = $u('admin-acct-list-review').onclick(); ui.srv.answer(200, after); await done
  assert.equal($u('admin-acct-plan').hidden, false)
  const reread = ui.step('loadAdminAccounts()'); ui.srv.answer(200, after); await reread
  assert.deepEqual([$u('admin-acct-plan').hidden, focused, $u('admin-acct-list-review').classList.contains('primary')], [true, $u('admin-acct-list-review'), true])
  done = $u('admin-acct-list-review').onclick(); ui.srv.answer(200, after); await done
  $u('admin-acct-list-text').value += '\nGeorg Cantor'; $u('admin-acct-list-text').oninput()
  assert.equal($u('admin-acct-plan').hidden, true)
  // A failed Apply: the button as it was, still focused, and the reason.
  done = $u('admin-acct-list-review').onclick(); ui.srv.answer(200, after); await done
  const again = button($u('admin-acct-plan'), 'Apply: 13 on, 5 off')
  again.focus()
  const failing = again.onclick()
  ui.srv.answer(500, { error: 'database is locked' }); await failing
  assert.deepEqual([again.textContent, again.getAttribute('aria-busy'), focused, status($u), ui.srv.pending.length],
    ['Apply: 13 on, 5 off', null, again, ['Could not set access from the list: database is locked.', 'acct-status err'], 0])

  // Several groups: the one shown in the table is chosen; without it, one must be.
  const two = load()
  await loaded(two, listPayload({ extra: [{ id: 80, email: 'x@example.org', name: 'Xena', access: 'on', group: 'Pilot' }] }))
  two.$('admin-acct-list-open').onclick()
  assert.deepEqual([two.$('admin-acct-list-group').value, focused], ['', two.$('admin-acct-list-group')])
  two.$('admin-acct-list-text').value = 'Xena'
  two.$('admin-acct-list-review').onclick()
  assert.deepEqual([status(two.$)[0], two.srv.pending.length], ['Choose the group this list is for.', 0])
  two.$('admin-acct-list-cancel').onclick()
  two.$('admin-acct-filter').value = '=Pilot'; two.$('admin-acct-filter').onchange()
  two.$('admin-acct-list-open').onclick()
  assert.deepEqual([two.$('admin-acct-list-group').value, two.$('admin-acct-list-scope').textContent], ['Pilot', 'Everyone else in Pilot is turned off; accounts outside the group are not touched.'])
  // A listed account outside the group is turned on, and flagged.
  two.$('admin-acct-list-text').value = 'Xena\nAda Lovelace'
  done = two.$('admin-acct-list-review').onclick(); two.srv.answer(200, listPayload({ extra: [{ id: 80, email: 'x@example.org', name: 'Xena', access: 'on', group: 'Pilot' }] })); await done
  assert.ok(two.$('admin-acct-plan').children.some(el => el.textContent === 'Not in Pilot (1): Ada Lovelace — listed, so it is turned on; its group does not change.'))
  // No group at all: the reason, and where to make one.
  const none = load()
  await loaded(none, listPayload({ group: '' }))
  none.$('admin-acct-list-open').onclick()
  assert.deepEqual([none.$('admin-acct-list-nogroup').hidden, none.$('admin-acct-list-review').disabled, none.$('admin-acct-list-group').disabled], [false, true, true])
  assert.deepEqual([view.logged, ui.logged, two.logged, none.logged], [[], [], [], []])
})

test('adding accounts: the server checks the list first, and shows line by line what it would do', async () => {
  const view = load(), { $, srv } = view
  await loaded(view, listPayload({ emailAvailable: true }))
  $('admin-acct-add-text').value = 'Maryam Mirzakhani - maryam@example.org\nemmy.noether@example.org\nno address here\nGrace Hopper <grace@example.org>\nmaryam@example.org'
  $('admin-acct-add-group').value = ' Field   test '
  $('admin-acct-add-send').checked = true; $('admin-acct-add-send').onchange()
  const create = $('admin-acct-add-create')
  create.focus()
  let done = create.onclick()
  // The same text and options as the real run, with preview: nothing is made or sent yet. The group goes as typed.
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.method, r.body])), [['/api/admin/accounts', 'POST', {
    text: 'Maryam Mirzakhani - maryam@example.org\nemmy.noether@example.org\nno address here\nGrace Hopper <grace@example.org>\nmaryam@example.org',
    password_length: 12, send_email: true, subject: DEFAULTS.subject, message: DEFAULTS.message, sender_name: 'Workshop Operator', group: ' Field   test ', preview: true }]])
  assert.deepEqual([create.textContent, create.getAttribute('aria-busy'), status($, 'admin-acct-add-status')[0]], ['Checking the list…', 'true', 'Checking the list…'])
  // The server says which group that is: the existing one, in its own spelling (finding #10).
  srv.answer(200, { email_available: true, group: GROUP, group_is_new: false, results: previewOf(view, { 'emmy.noether@example.org': 'exists', 'maryam@example.org': 'new' }, GROUP)
    .map((row, index) => index === 4 ? { ...row, status: 'duplicate', error: 'Listed more than once', group: undefined } : row) })
  await done
  const preview = $('admin-acct-preview')
  assert.deepEqual([preview.hidden, create.textContent, status($, 'admin-acct-add-status')[0], srv.pending.length], [false, 'Create accounts', '', 0])
  // The summary first (and the focus), what the emails will be, the group.
  assert.deepEqual(preview.children.slice(0, 3).map(el => el.textContent), [
    '2 new accounts · 1 already exists · 1 invalid · 1 listed twice',
    'Each new person will be emailed from "Workshop Operator via Ansätze", replies to admin@example.org, sign-in address https://ansatze.example/login.',
    'New accounts join the existing group Field test (you typed “Field   test”); 1 existing account will be added to it.'])
  assert.equal(focused, preview.querySelector('h4'))
  assert.equal(preview.querySelector('h4').tabIndex, -1)
  assert.deepEqual(preview.querySelector('tbody').children.map(tr => tr.children.map(td => td.textContent)), [
    ['Maryam Mirzakhani', 'maryam@example.org', 'New account · joins Field test', 'Maryam Mirzakhani - maryam@example.org'],
    ['—', 'emmy.noether@example.org', 'Already exists · joins Field test', 'emmy.noether@example.org'],
    ['—', '—', 'Invalid: No email address on this line', 'no address here'],
    ['Grace Hopper', 'grace@example.org', 'New account · joins Field test', 'Grace Hopper <grace@example.org>'],
    ['—', 'maryam@example.org', 'Listed twice', 'maryam@example.org']])
  assert.deepEqual(preview.querySelector('tbody').children[0].children.map(td => td.className), ['acct-name', 'acct-email', 'acct-res-new', 'acct-line'])
  assert.deepEqual(preview.querySelectorAll('button').map(b => [b.textContent, b.type, b.disabled]), [['Create 2 accounts and email them', 'button', false], ['Edit list', 'button', false]])
  // One primary action at a time: the preview's while it is shown.
  assert.deepEqual([preview.querySelectorAll('button')[0].classList.contains('primary'), create.classList.contains('primary')], [true, false])
  // Edit list: back to the form, the focus in the box.
  button(preview, 'Edit list').onclick()
  assert.deepEqual([preview.hidden, focused, create.classList.contains('primary')], [true, $('admin-acct-add-text'), true])

  // Any change to the form makes a preview stale, and an answer still on its way is not shown.
  for (const [id, change] of [['admin-acct-add-text', 'oninput'], ['admin-acct-add-group', 'oninput'], ['admin-acct-add-subject', 'oninput'], ['admin-acct-add-message', 'oninput'],
    ['admin-acct-add-sender', 'oninput'], ['admin-acct-add-length', 'oninput'], ['admin-acct-add-send', 'onchange']]) {
    done = create.onclick(); srv.answer(200, { results: previewOf(view) }); await done
    assert.equal(preview.hidden, false, id)
    $(id)[change]()
    assert.equal(preview.hidden, true, id)
  }
  done = create.onclick()
  $('admin-acct-add-text').oninput()
  srv.answer(200, { results: previewOf(view) }); await done
  // ...and the status says why no preview came (finding #13).
  assert.deepEqual([preview.hidden, create.textContent, status($, 'admin-acct-add-status')[0]],
    [true, 'Create accounts', 'The list changed while it was being checked: press “Create accounts” to check it again.'])
  // A group no account is in yet: said to be new.
  $('admin-acct-add-group').value = 'Field Test'
  done = create.onclick(); srv.answer(200, { group: 'Field Test', group_is_new: true, results: previewOf(view, {}, 'Field Test') }); await done
  assert.equal(preview.children[2].textContent, 'Field Test is a new group: new accounts join it; 0 existing accounts will be added to it.')
  $('admin-acct-add-group').value = ' Field   test '
  // Nothing new: nothing to create.
  done = create.onclick(); srv.answer(200, { results: previewOf(view, { 'maryam@example.org': 'exists', 'grace@example.org': 'exists', 'emmy.noether@example.org': 'exists' }) }); await done
  assert.deepEqual([confirmButton($).textContent, confirmButton($).disabled, confirmButton($).title], ['Create 0 accounts and email them', true, 'The list has no new account to create'])
  // Refused before anything is made (the site cannot send email now): the reason, and no table.
  done = create.onclick()
  srv.answer(400, { error: 'This site cannot send email (SMTP is not set up); send the details yourself' }); await done
  assert.deepEqual([preview.hidden, status($, 'admin-acct-add-status')], [true, ['Could not check the list: This site cannot send email (SMTP is not set up); send the details yourself.', 'acct-status err']])
  // Without email, a single sentence says so.
  $('admin-acct-add-send').checked = false; $('admin-acct-add-send').onchange(); $('admin-acct-add-group').value = ''
  done = create.onclick(); srv.answer(200, { results: previewOf(view) }); await done
  assert.deepEqual([preview.children[1].textContent, confirmButton($).textContent, srv.log.at(-1).body.group],
    ['Nobody will be emailed: the results will show each new password, to copy and send yourself.', 'Create 4 accounts', undefined])
  assert.deepEqual(view.logged, [])
})

test('creating accounts without email: one request, the passwords shown once and kept across runs, as CSV and as messages to send by hand', async () => {
  const view = load(), { $, srv, copied, confirms, answers } = view
  await loaded(view)
  // Checks before anything is asked: a person, a password length, a {password} in the message.
  $('admin-acct-add-create').onclick()
  assert.deepEqual([status($, 'admin-acct-add-status'), srv.pending.length], [['Add at least one person, one per line.', 'acct-status err'], 0])
  $('admin-acct-add-text').value = '\nMaryam Mirzakhani - maryam@example.org\n  Lovelace, Ada <ada2@example.org>  \n\nemmy.noether@example.org\n=HYPERLINK("x") <calc@example.org>\nPaul Erdős\n'
  $('admin-acct-add-text').oninput()
  assert.equal($('admin-acct-add-count').textContent, '5 people')
  $('admin-acct-add-length').value = '40'
  $('admin-acct-add-create').onclick()
  assert.deepEqual([status($, 'admin-acct-add-status')[0], srv.pending.length], ['Password length must be a whole number from 8 to 32.', 0])
  $('admin-acct-add-length').value = '14'
  $('admin-acct-add-message').value = 'Hello {name}, welcome.'
  $('admin-acct-add-create').onclick()
  assert.deepEqual([status($, 'admin-acct-add-status')[0], srv.pending.length], ['The message has no {password}: add it, or nobody would get a password.', 0])
  // Email ticked where it cannot go: refused, not dropped without a word (finding #20).
  $('admin-acct-add-send').checked = true
  $('admin-acct-add-create').onclick()
  assert.deepEqual([status($, 'admin-acct-add-status')[0], srv.pending.length],
    ['This site cannot send email (SMTP is not set up): untick “Email each person their sign-in details” to create the accounts without email.', 0])
  $('admin-acct-add-send').checked = false
  $('admin-acct-add-sender').value = '  Dr.   Operator '
  $('admin-acct-add-subject').value = 'Welcome to   the workshop'
  $('admin-acct-add-message').value = 'Hello {name} ({email}): {password} at {url}. {other} — {sender}'
  await previewed(view)
  assert.equal(srv.log.at(-1).body.password_length, 14)
  const done = confirmButton($).onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.method, r.body])), [['/api/admin/accounts', 'POST', {
    text: 'Maryam Mirzakhani - maryam@example.org\nLovelace, Ada <ada2@example.org>\nemmy.noether@example.org\n=HYPERLINK("x") <calc@example.org>\nPaul Erdős',
    password_length: 14, send_email: false, subject: 'Welcome to   the workshop', message: 'Hello {name} ({email}): {password} at {url}. {other} — {sender}', sender_name: 'Dr.   Operator' }]])
  // While it is made: said, the box read-only, the form's button busy and focused (the preview's is gone).
  assert.deepEqual([status($, 'admin-acct-add-status')[0], $('admin-acct-add-create').textContent, $('admin-acct-add-create').getAttribute('aria-busy'), $('admin-acct-add-text').readOnly,
    $('admin-acct-preview').hidden, focused], ['Creating 5 accounts…', 'Creating…', 'true', true, true, $('admin-acct-add-create')])
  srv.answer(200, { email_available: false, results: [
    { email: 'maryam@example.org', name: 'Maryam Mirzakhani', line: 'Maryam Mirzakhani - maryam@example.org', status: 'created', password: 'Mm7kQ2xPz9Rt4w' },
    { email: 'ada2@example.org', name: 'Lovelace, Ada', line: 'Lovelace, Ada <ada2@example.org>', status: 'created', password: 'Ad8"yT3,pL5nB2' },
    { email: 'emmy.noether@example.org', name: '', line: 'emmy.noether@example.org', status: 'exists', error: 'An account with this email already exists' },
    { email: 'calc@example.org', name: '=HYPERLINK("x")', line: '=HYPERLINK("x") <calc@example.org>', status: 'created', password: 'Cc4vN8sW2qE6rT' },
    { email: '', name: '', line: 'Paul Erdős', status: 'invalid', error: 'No email address on this line: Paul Erdős' }] })
  await done
  assert.deepEqual(srv.pending.map(r => [r.url, r.method]), [['/api/admin/accounts', 'GET']])
  srv.answer(200, listPayload())
  assert.deepEqual(status($, 'admin-acct-add-status'), ['3 created, 1 already existed, 1 invalid.', 'acct-status ok'])
  assert.deepEqual([$('admin-acct-add-text').value, $('admin-acct-add-text').readOnly, $('admin-acct-add-create').textContent], ['', false, 'Create accounts'])
  const results = $('admin-acct-results')
  assert.equal(results.querySelector('p.acct-warn').textContent,
    'Passwords are shown only now. Copy them before you leave this view: they cannot be shown again (you can still give anyone a new password from the table above).')
  assert.deepEqual(results.querySelector('tbody').children.map(tr => tr.children.map(td => td.textContent)), [
    ['Maryam Mirzakhani', 'maryam@example.org', 'Created', 'Mm7kQ2xPz9Rt4w', 'Not asked Copy message'],
    ['Lovelace, Ada', 'ada2@example.org', 'Created', 'Ad8"yT3,pL5nB2', 'Not asked Copy message'],
    ['—', 'emmy.noether@example.org', 'Already existsAn account with this email already exists', '—', '—'],
    ['=HYPERLINK("x")', 'calc@example.org', 'Created', 'Cc4vN8sW2qE6rT', 'Not asked Copy message'],
    ['—', '—', 'InvalidNo email address on this line: Paul Erdős', '—', '—']])
  // Leaving now would lose passwords neither emailed nor copied: asked first.
  answers.confirm = false
  assert.equal(view.step('adminAccountsMayLeave()'), false)
  assert.equal(confirms.at(-1), '3 new passwords shown here were neither emailed nor copied, and cannot be shown again. Leave anyway?')
  // name,email,password: quoted where needed, a would-be formula defused.
  await button(results, 'Copy as CSV').onclick()
  assert.equal(copied.at(-1), 'name,email,password\nMaryam Mirzakhani,maryam@example.org,Mm7kQ2xPz9Rt4w\n"Lovelace, Ada",ada2@example.org,"Ad8""yT3,pL5nB2"\n'
    + `"'=HYPERLINK(""x"")",calc@example.org,Cc4vN8sW2qE6rT\n`)
  assert.deepEqual([status($, 'admin-acct-add-status')[0], view.step('adminAccountsMayLeave()')], ['Copied 3 accounts as CSV (name,email,password).', true])
  // The messages, filled in as the server fills them; a name with a comma or a quote is quoted on its To line (finding #22).
  await button(results, 'Copy emails').onclick()
  const message = (to, name, address, password) => `To: ${to}\nSubject: Welcome to the workshop\n\nHello ${name} (${address}): ${password} at https://ansatze.example/login. {other} — Dr. Operator\n`
  assert.equal(copied.at(-1), [message('Maryam Mirzakhani <maryam@example.org>', 'Maryam Mirzakhani', 'maryam@example.org', 'Mm7kQ2xPz9Rt4w'),
    message('"Lovelace, Ada" <ada2@example.org>', 'Lovelace, Ada', 'ada2@example.org', 'Ad8"yT3,pL5nB2'),
    message('"=HYPERLINK(\\"x\\")" <calc@example.org>', '=HYPERLINK("x")', 'calc@example.org', 'Cc4vN8sW2qE6rT')].join('\n' + '-'.repeat(40) + '\n\n'))
  assert.deepEqual(plain(view.step(`[adminAccountAddress('Ada Lovelace','a@x.org'),adminAccountAddress('O\\\\Brien','o@x.org'),adminAccountAddress('','b@x.org')]`)),
    ['Ada Lovelace <a@x.org>', '"O\\\\Brien" <o@x.org>', 'b@x.org'])
  // A name left out is the address's first part, as on the server; the site's address stands in for a missing URL.
  assert.equal(view.step("adminAccountMessage('{name}|{url}|{sender}|{password}',{email:'x.y@z.org',password:'p'})"), "x.y|(the site's address)||p")

  // Another run keeps the earlier results (finding #14), and a refusal leaves them too.
  $('admin-acct-add-message').value = DEFAULTS.message
  $('admin-acct-add-text').value = 'Hypatia, hypatia@example.org'
  await previewed(view)
  let second = confirmButton($).onclick()
  srv.answer(200, { results: [{ email: 'hypatia@example.org', name: 'Hypatia', line: 'Hypatia, hypatia@example.org', status: 'created', password: 'Hy4' }] }); await second
  srv.answer(200, listPayload())
  assert.deepEqual(results.querySelector('tbody').children.map(tr => tr.children[1].textContent),
    ['maryam@example.org', 'ada2@example.org', 'emmy.noether@example.org', 'calc@example.org', '—', 'hypatia@example.org'])
  assert.deepEqual([status($, 'admin-acct-add-status')[0], results.querySelector('span.acct-note').textContent], ['1 created.', '4 created · 6 lines answered'])
  $('admin-acct-add-text').value = 'x@example.org'
  await previewed(view)
  second = confirmButton($).onclick()
  srv.answer(400, { error: "Create the administrator's own account first" }); await second
  assert.deepEqual([status($, 'admin-acct-add-status')[0], results.querySelector('tbody').children.length, $('admin-acct-add-text').value],
    ["Could not create accounts: Create the administrator's own account first.", 6, 'x@example.org'])
  // Each run's message stays its own: Hypatia's is the default text (under the subject still in the form).
  await results.querySelector('tbody').children[5].querySelector('button').onclick()
  assert.equal(copied.at(-1), 'To: Hypatia <hypatia@example.org>\nSubject: Welcome to the workshop\n\nDear Hypatia,\n\nSign in at: https://ansatze.example/login\nEmail: hypatia@example.org\nPassword: Hy4\n\nBest regards,\nDr. Operator\n')
  // Clear results asks while a password there was neither emailed nor copied; then the passwords go.
  view.step("ACCOUNTS.results.rows.forEach(row=>{ row.saved=false; })")
  answers.confirm = false
  button(results, 'Clear results').onclick()
  assert.deepEqual([confirms.at(-1), results.hidden], ['4 new passwords shown here were neither emailed nor copied, and cannot be shown again. Clear the results anyway?', false])
  answers.confirm = true
  button(results, 'Clear results').onclick()
  assert.deepEqual([results.hidden, results.children.length, view.step('ACCOUNTS.results'), status($, 'admin-acct-add-status')[0], focused],
    [true, 0, null, 'Results cleared.', $('admin-acct-add-create')])
  assert.deepEqual(view.logged, [])
})

test('creating accounts with email: five people per request with progress; a request that fails, or a server that stops, keeps what was done', async () => {
  const view = load(), { $, srv } = view
  await loaded(view, listPayload({ emailAvailable: true }))
  $('admin-acct-add-send').checked = true
  const lines = Array.from({ length: 12 }, (_, i) => `Speaker ${i + 1} - s${i + 1}@example.org`)
  lines[6] = 'Speaker Two again - s2@example.org'
  $('admin-acct-add-text').value = lines.join('\n') + '\n\n'
  $('admin-acct-add-send').onchange()
  assert.equal($('admin-acct-add-count').textContent, '12 people · created and emailed 5 at a time')
  const answerChunk = (view, options = {}) => {
    const request = view.srv.pending[0]
    view.srv.answer(200, { email_available: true, ...options.top, results: request.body.text.split('\n').map(line => {
      const [, name, address] = /^(.*) - (\S+)$/.exec(line)
      if (options.stopAt && address === options.stopAt) options.stopped = true
      if (options.stopped) return { email: address, name, line, status: 'not_processed', error: 'Not processed: an earlier line stopped the run' }
      const again = address === 's2@example.org' && name.includes('again')
      return again ? { email: address, name, line, status: 'exists', error: 'An account with this email already exists' }
        : { email: address, name, line, status: 'created', password: 'Pw-' + address.split('@')[0], email_sent: address !== 's4@example.org' }
    }) })
    return request
  }
  const progress = () => status($, 'admin-acct-add-status')[0]
  await previewed(view, {}, { emailAvailable: true })
  assert.equal(confirmButton($).textContent, 'Create 12 accounts and email them')
  const done = confirmButton($).onclick()
  assert.deepEqual([srv.pending.length, srv.pending[0].body.text, srv.pending[0].body.send_email, srv.pending[0].body.preview, progress()],
    [1, lines.slice(0, 5).join('\n'), true, undefined, 'Creating 1–5 of 12…'])
  answerChunk(view); await flush()
  assert.deepEqual([srv.pending.length, srv.pending[0].body.text, progress()], [1, lines.slice(5, 10).join('\n'), 'Creating 6–10 of 12…'])
  assert.equal($('admin-acct-results').querySelector('tbody').children.length, 5)
  answerChunk(view); await flush()
  assert.deepEqual([srv.pending.length, srv.pending[0].body.text, progress()], [1, lines.slice(10).join('\n'), 'Creating 11–12 of 12…'])
  answerChunk(view); await done
  const rows = $('admin-acct-results').querySelector('tbody').children.map(tr => tr.children.map(td => td.textContent))
  assert.deepEqual(rows.map(row => row[1]), ['s1', 's2', 's3', 's4', 's5', 's6', 's2', 's8', 's9', 's10', 's11', 's12'].map(local => local + '@example.org'))
  // Named again in a later request: "Listed twice", as a single request would say.
  assert.deepEqual(rows[6].slice(2, 4), ['Listed twiceListed more than once', '—'])
  assert.deepEqual([rows[0][4], rows[3][4]], ['Sent', 'Not sent Copy message'])
  assert.deepEqual(status($, 'admin-acct-add-status'), ['11 created, 1 listed twice, 10 emails sent (1 not: copy their details below).', 'acct-status ok'])
  await button($('admin-acct-results'), 'Copy emails').onclick()
  assert.equal(view.copied.at(-1).split('\n')[0], 'To: Speaker 4 <s4@example.org>')

  // A request that fails: what came before stays shown, and the lines not processed go back in the box.
  const second = load(), page = second.$
  await loaded(second, listPayload({ emailAvailable: true }))
  page('admin-acct-add-send').checked = true
  page('admin-acct-add-text').value = lines.join('\n')
  await previewed(second, {}, { emailAvailable: true })
  let failing = confirmButton(page).onclick()
  answerChunk(second); await flush()
  second.srv.answer(504, { error: 'Gateway timeout' }); await failing
  assert.equal(second.srv.pending.filter(r => r.method === 'POST').length, 0)
  assert.equal(page('admin-acct-add-text').value, lines.slice(5).join('\n'))
  assert.equal(page('admin-acct-results').querySelector('tbody').children.length, 5)
  assert.deepEqual(status(page, 'admin-acct-add-status'), ['Stopped at lines 6–10 of 12: Gateway timeout. Lines 6–12 are back in the box: press “Create accounts” to retry. '
    + 'Anyone the stopped request did create then shows as “Already exists”: give them a new password from the table. Before the stop: 5 created, 4 emails sent (1 not: copy their details below).', 'acct-status err'])
  let left = page('admin-acct-results').querySelector('div.acct-warn')
  assert.deepEqual([left.querySelector('b').textContent, left.querySelector('ul').children.map(li => li.textContent)], ['Not processed (7):', lines.slice(5)])
  // The retry, as the message says: the earlier results (and the password not emailed) stay (finding #14).
  second.srv.answer(200, listPayload({ emailAvailable: true })); await flush()
  await previewed(second, { 's2@example.org': 'exists' }, { emailAvailable: true })
  failing = confirmButton(page).onclick()
  // The server stops part way (the database locked): it says which lines it did not reach.
  answerChunk(second, { stopAt: 's8@example.org', top: { stopped: 'The server could not create this account (OperationalError: database is locked)' } }); await failing
  assert.deepEqual(status(page, 'admin-acct-add-status'), ['The server stopped at line 3 of 7: The server could not create this account (OperationalError: database is locked). '
    + 'The 5 lines it did not process are back in the box: press “Create accounts” to retry. Before the stop: 1 created, 1 already existed, 1 email sent.', 'acct-status err'])
  assert.equal(page('admin-acct-add-text').value, lines.slice(7).join('\n'))
  assert.deepEqual(page('admin-acct-results').querySelector('tbody').children.map(tr => tr.children[1].textContent),
    ['s1@example.org', 's2@example.org', 's3@example.org', 's4@example.org', 's5@example.org', 's6@example.org', 's2@example.org'])
  assert.equal(page('admin-acct-results').querySelector('tbody').children[3].children[3].textContent, 'Pw-s4')
  left = page('admin-acct-results').querySelector('div.acct-warn')
  assert.deepEqual(left.querySelector('ul').children.map(li => li.textContent), lines.slice(7))
  assert.deepEqual([view.logged, second.logged], [[], []])
})

test('a new password: asked first, then shown once under its row, with the new-password message; several rows can show theirs', async () => {
  const view = load(), { $, srv, copied, confirms, answers } = view
  await loaded(view)
  const row = rowOf($, 'riemann@example.org')
  button(row, 'New password').onclick()
  const resetRow = address => $('admin-acct-body').querySelectorAll('tr').find(tr => tr.classList.contains('acct-reset-row') && $('admin-acct-body').querySelector('tbody').kids[$('admin-acct-body').querySelector('tbody').kids.indexOf(tr) - 1]?.dataset.email === address)
  const panel = address => resetRow(address).querySelector('div.acct-reset')
  assert.deepEqual([panel('riemann@example.org').getAttribute('role'), panel('riemann@example.org').getAttribute('aria-label'), resetRow('riemann@example.org').children[0].colSpan],
    ['group', 'New password for Bernhard Riemann', 9])
  assert.equal(panel('riemann@example.org').querySelector('p').textContent, 'Give Bernhard Riemann (riemann@example.org) a new random password? The current one stops working, and they are signed out at once.')
  // No email here: the option is off, and says why; its label names the message it would send.
  assert.deepEqual([panel('riemann@example.org').querySelector('label').textContent, panel('riemann@example.org').querySelector('input').disabled, panel('riemann@example.org').querySelector('p.acct-note').textContent],
    [' Email them the new-password message', true, 'This site cannot send email (SMTP is not set up): copy the new password and send it yourself.'])
  assert.equal(focused, button(panel('riemann@example.org'), 'Set new password'))
  let done = button(panel('riemann@example.org'), 'Set new password').onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.body])), [['/api/admin/accounts/password', { email: 'riemann@example.org', password_length: 12, send_email: false }]])
  assert.deepEqual([button(panel('riemann@example.org'), 'Setting…').getAttribute('aria-busy'), button(panel('riemann@example.org'), 'Cancel').disabled, focused],
    [ 'true', true, button(panel('riemann@example.org'), 'Setting…')])
  view.step("closeAdminPasswordReset('riemann@example.org')")
  assert.ok(resetRow('riemann@example.org'), 'not cancelled while the password is being set')
  srv.answer(200, { email: 'riemann@example.org', password: 'Rm4kT8vB2nQ6' }); await done
  assert.deepEqual([panel('riemann@example.org').querySelector('code.acct-pw').textContent, focused], ['Rm4kT8vB2nQ6', button(panel('riemann@example.org'), 'Copy')])
  assert.equal(panel('riemann@example.org').querySelector('p.acct-warn').textContent,
    'Shown only now: copy it before you leave this view. It was not emailed. Bernhard Riemann was signed out, and the old password no longer works.')
  // Another row's new password: the first one stays shown (finding #14).
  button(rowOf($, 'hilbert@example.org'), 'New password').onclick()
  done = button(panel('hilbert@example.org'), 'Set new password').onclick()
  srv.answer(200, { email: 'hilbert@example.org', password: 'Hb2' }); await done
  assert.deepEqual([panel('riemann@example.org').querySelector('code.acct-pw').textContent, panel('hilbert@example.org').querySelector('code.acct-pw').textContent], ['Rm4kT8vB2nQ6', 'Hb2'])
  // "Copy email text": the new-password message (finding #11), not the add form's invitation.
  await button(panel('riemann@example.org'), 'Copy email text').onclick()
  assert.equal(copied.at(-1), 'To: Bernhard Riemann <riemann@example.org>\nSubject: Your new Ansätze password\n\nDear Bernhard Riemann,\n\nHere is a new password.\n\n'
    + 'Sign in at: https://ansatze.example/login\nEmail: riemann@example.org\nPassword: Rm4kT8vB2nQ6\n\nWorkshop Operator\n')
  // Done asks while the password was neither emailed nor copied.
  answers.confirm = false
  button(panel('hilbert@example.org'), 'Done').onclick()
  assert.deepEqual([confirms.at(-1), !!resetRow('hilbert@example.org')], ['The new password for David Hilbert was neither emailed nor copied, and cannot be shown again. Close it anyway?', true])
  await button(panel('hilbert@example.org'), 'Copy').onclick()
  assert.equal(copied.at(-1), 'Hb2')
  button(panel('hilbert@example.org'), 'Done').onclick()
  assert.deepEqual([resetRow('hilbert@example.org'), focused, confirms.length], [undefined, button(rowOf($, 'hilbert@example.org'), 'New password'), 1])
  // A search that hides a row keeps it while its password is shown.
  $('admin-acct-q').value = 'ada'; $('admin-acct-q').oninput()
  assert.deepEqual(rowsOf($).map(tr => tr.dataset.email), ['ada.lovelace@example.org', 'riemann@example.org'])
  button(panel('riemann@example.org'), 'Done').onclick()
  assert.deepEqual(rowsOf($).map(tr => tr.dataset.email), ['ada.lovelace@example.org'])

  // Email possible and asked for: the server sends its new-password message, signed by the form's sender.
  const mail = load()
  await loaded(mail, listPayload({ emailAvailable: true }))
  button(rowOf(mail.$, 'hilbert@example.org'), 'New password').onclick()
  const box = mail.$('admin-acct-body').querySelector('tr.acct-reset-row').querySelector('input')
  assert.equal(box.disabled, false)
  box.checked = true; box.onchange()
  done = button(mail.$('admin-acct-body').querySelector('tr.acct-reset-row'), 'Set new password').onclick()
  assert.deepEqual(mail.srv.pending[0].body, { email: 'hilbert@example.org', password_length: 12, send_email: true, sender_name: 'Workshop Operator' })
  mail.srv.answer(200, { email: 'hilbert@example.org', password: 'Hb2', email_sent: true }); await done
  const shown = mail.$('admin-acct-body').querySelector('tr.acct-reset-row')
  assert.match(shown.querySelector('p.acct-warn').textContent, /It was emailed to hilbert@example\.org \(the new-password message\)\./)
  assert.deepEqual([button(shown, 'Copy email text'), status(mail.$), mail.step('adminAccountsMayLeave()')], [undefined, ['New password set for David Hilbert and emailed.', 'acct-status ok'], true])
  // Refused: said in the panel and the status line; the question stays.
  button(rowOf(mail.$, 'euler@example.org'), 'New password').onclick()
  done = button(mail.$('admin-acct-body').querySelectorAll('tr.acct-reset-row').at(-1), 'Set new password').onclick()
  mail.srv.answer(400, { error: 'No account uses this email' }); await done
  assert.deepEqual([mail.$('admin-acct-body').querySelectorAll('tr.acct-reset-row').at(-1).querySelector('p.acct-status').textContent, status(mail.$)[1]],
    ['Could not set a new password: No account uses this email.', 'acct-status err'])
  // No sign-in address: no email, and why; without the defaults, the built-in new-password text.
  const bare = load()
  await loaded(bare, { ...listPayload({ emailAvailable: true, url: '' }), defaults: { password_length: 12 } })
  button(rowOf(bare.$, 'abel@example.org'), 'New password').onclick()
  const ask = bare.$('admin-acct-body').querySelector('tr.acct-reset-row')
  assert.deepEqual([ask.querySelector('input').disabled, ask.querySelector('p.acct-note').textContent],
    [true, "Emails need the site's address (AGENT_MONITOR_APP_URL), which is not set: copy the new password and send it yourself."])
  done = button(ask, 'Set new password').onclick(); bare.srv.answer(200, { email: 'abel@example.org', password: 'Ab1' }); await done
  await button(bare.$('admin-acct-body').querySelector('tr.acct-reset-row'), 'Copy email text').onclick()
  assert.equal(bare.copied.at(-1).split('\n').slice(0, 5).join('|'), 'To: Niels Henrik Abel <abel@example.org>|Subject: Your new Ansätze password||Dear Niels Henrik Abel,|')
  assert.match(bare.copied.at(-1), /Sign in at: \(the site's address\)\nEmail: abel@example\.org\nPassword: Ab1\n/)
  assert.deepEqual([view.logged, mail.logged, bare.logged], [[], [], []])
})

test('without the defaults, a message sent by hand still has a template with the password (finding #20)', async () => {
  const view = load(), { $, srv, copied } = view
  await loaded(view, { ...listPayload(), defaults: {} })
  assert.deepEqual([$('admin-acct-add-subject').value, $('admin-acct-add-message').value.includes('{password}')], ['Your Ansätze account', true])
  $('admin-acct-add-subject').value = ''; $('admin-acct-add-message').value = ''
  $('admin-acct-add-text').value = 'Grace Hopper <grace@example.org>'
  await previewed(view)
  const done = confirmButton($).onclick()
  srv.answer(200, { results: [{ email: 'grace@example.org', name: 'Grace Hopper', line: 'Grace Hopper <grace@example.org>', status: 'created', password: 'Gh7' }] }); await done
  await button($('admin-acct-results'), 'Copy emails').onclick()
  assert.match(copied.at(-1), /^To: Grace Hopper <grace@example\.org>\nSubject: Your Ansätze account\n\nDear Grace Hopper,\n\nAn account has been created for you on Ansätze\.\n/)
  assert.match(copied.at(-1), /\nPassword: Gh7\n/)
})

test('round 2: a blank message is the default, the page guards passwords on reload and on a 401, focus stays put, and group names are the server\'s', async () => {
  const view = load(), { $, step, srv, copied, listeners } = view
  await loaded(view, listPayload({ emailAvailable: true }))
  // A message (or subject) of spaces and line breaks is an empty one: '' is sent, and the default text is used by hand (finding #7).
  $('admin-acct-add-text').value = 'Sophie Germain - sophie@uni.example'
  $('admin-acct-add-subject').value = '   '
  $('admin-acct-add-message').value = '\n  \n'
  let done = $('admin-acct-add-create').onclick()
  assert.deepEqual([srv.pending[0].body.subject, srv.pending[0].body.message], ['', ''])
  srv.answer(200, { results: previewOf(view) }); await done
  done = confirmButton($).onclick()
  assert.deepEqual([srv.pending[0].body.subject, srv.pending[0].body.message], ['', ''])
  srv.answer(200, { results: [{ email: 'sophie@uni.example', name: 'Sophie Germain', line: 'Sophie Germain - sophie@uni.example', status: 'created', password: 'Sg5' }] }); await done
  srv.answer(200, listPayload({ emailAvailable: true })); await flush()
  await button($('admin-acct-results'), 'Copy emails').onclick()
  assert.equal(copied.at(-1), 'To: Sophie Germain <sophie@uni.example>\nSubject: Your Ansätze account\n\nDear Sophie Germain,\n\nSign in at: https://ansatze.example/login\nEmail: sophie@uni.example\nPassword: Sg5\n\nBest regards,\nWorkshop Operator\n')

  // Reloading or closing the page asks while a password is at risk (finding #9): not now (copied), but once one is shown unsaved.
  const unload = listeners.find(([type]) => type === 'beforeunload')[1]
  const event = () => { const e = { prevented: false, returnValue: undefined, preventDefault() { this.prevented = true } }; unload(e); return [e.prevented, e.returnValue] }
  assert.deepEqual(event(), [false, undefined])
  button(rowOf($, 'riemann@example.org'), 'New password').onclick()
  done = button($('admin-acct-body').querySelector('tr.acct-reset-row'), 'Set new password').onclick()
  assert.deepEqual(event(), [true, ''])
  srv.answer(200, { email: 'riemann@example.org', password: 'Rr2' }); await done
  assert.deepEqual(event(), [true, ''])
  // A 401 then does not take the page away: the view says to copy the passwords first.
  assert.match(html, /if\(r\.status===401&&!String\(a\[0\]\)\.includes\('\/api\/auth\/'\)&&!window\.__PC_PUBLIC__\)\{\n\s*if\(typeof adminAccountsAtRisk==='function'&&adminAccountsAtRisk\(\)\)adminAccountsSessionEnded\(\);\n\s*else location\.href=pcUrl\('\/login'\);/)
  $('admin-acct-session').hidden = true
  assert.equal(step('adminAccountsAtRisk()'), true)
  step('adminAccountsSessionEnded()')
  assert.equal($('admin-acct-session').hidden, false)

  // Closing a panel whose row the search hides: the focus goes to the search box, not nowhere (finding #12).
  $('admin-acct-q').value = 'ada'; $('admin-acct-q').oninput()
  const panel = $('admin-acct-body').querySelector('tr.acct-reset-row')
  await button(panel, 'Copy').onclick()
  button(panel, 'Done').focus()
  button(panel, 'Done').onclick()
  assert.deepEqual([rowsOf($).map(tr => tr.dataset.email), focused], [['ada.lovelace@example.org'], $('admin-acct-q')])
  assert.deepEqual(event(), [false, undefined])
  $('admin-acct-q').value = ''; $('admin-acct-q').oninput()

  // A results button keeps the focus while a run with email draws the results again after each request (finding #12).
  $('admin-acct-add-send').checked = true
  const people = Array.from({ length: 7 }, (_, i) => `Speaker ${i} - s${i}@uni.example`)
  $('admin-acct-add-text').value = people.join('\n')
  await previewed(view, {}, { emailAvailable: true })
  done = confirmButton($).onclick()
  srv.answer(200, { results: people.slice(0, 5).map((line, i) => ({ email: `s${i}@uni.example`, name: `Speaker ${i}`, line, status: 'created', password: 'P' + i, email_sent: i !== 2 })) }); await flush()
  const messageOf = address => $('admin-acct-results').querySelector('tbody').children.find(tr => tr.children[1].textContent === address).querySelector('button')
  const message = messageOf('s2@uni.example')
  message.focus()
  srv.answer(200, { results: people.slice(5).map((line, i) => ({ email: `s${i + 5}@uni.example`, name: `Speaker ${i + 5}`, line, status: 'created', password: 'P', email_sent: true })) }); await done
  assert.ok(focused && focused !== message && focused === messageOf('s2@uni.example'))

  // Group names are the server's, verbatim, in the filter, the list's group and the request (finding #5).
  const odd = load()
  const strange = '𝔸¹-homotopy  theory (field test)'
  await loaded(odd, listPayload({ group: strange }))
  assert.deepEqual(options(odd.$('admin-acct-filter'))[1], ['=' + strange, strange])
  odd.$('admin-acct-list-open').onclick()
  assert.equal(odd.$('admin-acct-list-group').value, strange)
  odd.$('admin-acct-list-text').value = 'Ada Lovelace'
  done = odd.$('admin-acct-list-review').onclick(); odd.srv.answer(200, listPayload({ group: strange })); await done
  done = button(odd.$('admin-acct-plan'), 'Apply: 1 on, 16 off').onclick()
  assert.equal(odd.srv.pending[0].body.group, strange)
  // A group no account is in (renamed meanwhile): the server refuses, and the reason is shown.
  odd.srv.answer(400, { error: `No account is in the group “${strange}”` }); await done
  assert.deepEqual(status(odd.$), [`Could not set access from the list: No account is in the group “${strange}”.`, 'acct-status err'])
  // The real run uses the group as the preview's answer spelled it.
  odd.$('admin-acct-add-text').value = 'x@uni.example'
  odd.$('admin-acct-add-group').value = 'FIELD test'
  done = odd.$('admin-acct-add-create').onclick(); odd.srv.answer(200, { group: GROUP, group_is_new: false, results: previewOf(odd, {}, GROUP) }); await done
  done = confirmButton(odd.$).onclick()
  assert.equal(odd.srv.pending[0].body.group, GROUP)
  odd.srv.answer(200, { group: GROUP, group_is_new: false, results: [{ email: 'x@uni.example', name: '', line: 'x@uni.example', status: 'created', password: 'Xx1', group: GROUP }] }); await done
  assert.equal(status(odd.$, 'admin-acct-add-status')[0], '1 created.')
  assert.deepEqual([view.logged, odd.logged], [[], []])
})

test('round 3: a change for the selection acts on what was meant: search, tick, change; rows not shown are named first; "Select all shown" replaces the selection', async () => {
  const view = load(), { $, srv, step } = view
  const payload = () => listPayload({ extra: [...PUBLIC, { id: 61, email: 'ana.silva@example.org', name: 'Ana Silva', access: 'on', group: GROUP }] })
  await loaded(view, payload())
  const search = text => { $('admin-acct-q').value = text; $('admin-acct-q').oninput() }
  const tick = address => { const box = rowOf($, address).querySelector('input'); box.checked = true; box.onchange() }
  const ask = $('admin-acct-bulk-ask'), on = $('admin-acct-on'), off = $('admin-acct-off')
  // The reviewer's day (finding #4): search, tick, off; search, tick, off; search, tick, on. Each change is for its own person.
  search('Lovelace'); tick('ada.lovelace@example.org')
  let done = off.onclick()
  assert.deepEqual(srv.pending[0].body, { emails: ['ada.lovelace@example.org'], access: 'off' })
  srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [] }); await done
  search('Silva'); tick('ana.silva@example.org')
  assert.equal($('admin-acct-selected').textContent, '1 selected')
  done = off.onclick()
  assert.deepEqual(srv.pending[0].body, { emails: ['ana.silva@example.org'], access: 'off' })
  srv.answer(200, { updated: ['ana.silva@example.org'], skipped: [] }); await done
  search('Turing'); tick('turing@example.org')
  done = on.onclick()
  assert.deepEqual([srv.pending[0].body, ask.hidden], [{ emails: ['turing@example.org'], access: 'on' }, true])
  srv.answer(200, { updated: ['turing@example.org'], skipped: [] }); await done
  search('')
  // The first two stay off.
  assert.deepEqual(['ada.lovelace', 'ana.silva', 'turing'].map(local => access($, email(local))[0]), ['false', 'false', 'true'])

  // A selection built across searches is kept; before a change, the rows of it not shown are named, in the page.
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Silva'); tick('ana.silva@example.org'); search('Turing'); tick('turing@example.org')
  assert.equal($('admin-acct-selected').textContent, '3 selected (2 not shown)')
  on.focus()
  on.onclick()
  assert.equal(srv.pending.length, 0)
  assert.deepEqual([ask.hidden, ask.children[0].textContent, ask.querySelectorAll('button').map(b => [b.textContent, b.type, b.className])], [false,
    'Turn access on for 3 accounts, 2 not shown here: Ada Lovelace, Ana Silva?', [['Turn access on for all 3', 'button', 'acct-btn primary'], ['Only the 1 shown', 'button', 'acct-btn'], ['Cancel', 'button', 'acct-btn']]])
  assert.deepEqual([focused, focused.getAttribute('aria-describedby'), ask.children[0].id], [button(ask, 'Turn access on for all 3'), 'admin-acct-bulk-question', 'admin-acct-bulk-question'])
  // Cancel: nothing is sent, the selection stays, the focus goes back to the button.
  button(ask, 'Cancel').onclick()
  assert.deepEqual([ask.hidden, ask.children.length, srv.pending.length, $('admin-acct-selected').textContent, focused], [true, 0, 0, '3 selected (2 not shown)', on])
  // Only the rows shown: the others are not sent, and are no longer selected either.
  on.onclick()
  done = button(ask, 'Only the 1 shown').onclick()
  assert.deepEqual([plain(srv.pending[0].body), ask.hidden, focused], [{ emails: ['turing@example.org'], access: 'on' }, true, on])
  srv.answer(200, { updated: ['turing@example.org'], skipped: [] }); await done
  assert.deepEqual([$('admin-acct-selected').textContent, focused, status($)[0]], ['None selected', $('admin-acct-q'), 'Access turned on for 1 account.'])
  // All of them, once named.
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Turing'); tick('turing@example.org')
  off.onclick()
  assert.equal(ask.children[0].textContent, 'Turn access off for 2 accounts, 1 not shown here: Ada Lovelace? They are signed out at once.')
  done = button(ask, 'Turn access off for all 2').onclick()
  assert.deepEqual(srv.pending[0].body, { emails: ['ada.lovelace@example.org', 'turing@example.org'], access: 'off' })
  srv.answer(200, { updated: ['ada.lovelace@example.org', 'turing@example.org'], skipped: [] }); await done
  assert.equal($('admin-acct-selected').textContent, 'None selected')
  // Nothing hidden: no question.
  tick('turing@example.org')
  done = on.onclick()
  assert.deepEqual([ask.hidden, srv.pending.length], [true, 1])
  srv.answer(200, { updated: ['turing@example.org'], skipped: [] }); await done
  // The question goes when what it asks about changes: the selection, the search, the group shown, the group's name.
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Turing'); tick('turing@example.org')
  for (const change of [() => tick('turing@example.org'), () => search('Turing '), () => { $('admin-acct-filter').onchange() },
    () => { $('admin-acct-group-name').oninput() }, () => $('admin-acct-clear').onclick()]) {
    on.onclick()
    assert.equal(ask.hidden, false)
    change()
    assert.deepEqual([ask.hidden, srv.pending.length], [true, 0])
  }
  // A group for a selection with rows not shown: named first too; then the group as typed.
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Turing'); tick('turing@example.org')
  $('admin-acct-group-name').value = 'Day two'
  $('admin-acct-group-set').onclick()
  assert.deepEqual([ask.children[0].textContent, button(ask, 'Set the group for all 2') !== undefined, srv.pending.length],
    ['Put 2 accounts in the group “Day two”, 1 not shown here: Ada Lovelace?', true, 0])
  done = button(ask, 'Set the group for all 2').onclick()
  assert.deepEqual(plain(srv.pending[0].body), { emails: ['ada.lovelace@example.org', 'turing@example.org'], group: 'Day two' })
  srv.answer(200, { updated: ['ada.lovelace@example.org', 'turing@example.org'], skipped: [], group: 'Day two', group_is_new: true }); await flush(); srv.answer(200, payload()); await done
  assert.deepEqual([status($)[0], $('admin-acct-selected').textContent], ['2 accounts now in Day two, a new group.', 'None selected'])
  tick('turing@example.org'); search('Lovelace'); tick('ada.lovelace@example.org')
  $('admin-acct-group-remove').onclick()
  assert.deepEqual([ask.children[0].textContent, button(ask, 'Take all 2 out') !== undefined], ['Take 2 accounts out of their group, 1 not shown here: Alan Turing?', true])
  button(ask, 'Cancel').onclick()
  assert.equal(focused, $('admin-acct-group-remove'))

  // "Select all shown" picks the rows shown, in place of the selection before (Ada is no longer selected).
  search(''); $('admin-acct-filter').value = 'none'; $('admin-acct-filter').onchange()
  $('admin-acct-all').onclick()
  assert.deepEqual([$('admin-acct-selected').textContent, step("[...ACCOUNTS.selected].join(' ')")], ['2 selected', 'grace.hopper@example.org paul.erdos@example.org'])
  // Leaving the view clears the selection.
  step('clearAdminAccountSecrets()')
  assert.deepEqual([$('admin-acct-selected').textContent, rowOf($, 'grace.hopper@example.org').querySelector('input').checked, on.disabled], ['None selected', false, true])
  assert.deepEqual(view.logged, [])
})

test('round 3: a line is taken only when it can mean one person: a name that also fits another account, or a mark on the line, turns nobody on', async () => {
  const view = load(), { $, step } = view
  await loaded(view, listPayload({ extra: [...PUBLIC,
    { id: 70, email: 'smith@uni.example', name: 'Smith', access: 'off', group: GROUP }, { id: 71, email: 'john.smith@uni.example', name: 'John Smith', access: 'on', group: GROUP },
    { id: 72, email: 'ana@uni.example', name: 'Ana', access: 'off', group: GROUP }, { id: 73, email: 'ana.silva@uni.example', name: 'Ana Silva', access: 'on', group: GROUP },
    { id: 74, email: 'wang.wei@uni.example', name: 'Wang Wei', access: 'off', group: GROUP }, { id: 75, email: 'wei.wang@uni.example', name: 'Wei J. Wang', access: 'on', group: GROUP }] }))
  const read = entry => plain(step(`(()=>{ const p=adminAccessPlan(${JSON.stringify(entry)},ACCOUNTS.list,'Field test'), l=p.lines[0], keys=list=>(list||[]).map(a=>a.key);
    return {on:keys(p.on),problem:l.problem,exact:l.exact?.key,also:keys(l.also),matches:keys(l.matches),marks:l.marks,text:l.problem?adminAccessProblemText(l):'',reading:adminAccessReadingText(l)}; })()`))
  // An exact name that also fits others (finding #5): nobody, both named.
  assert.deepEqual(read('Smith'), { on: [], problem: 'ambiguous', exact: 'smith@uni.example', also: ['john.smith@uni.example'], matches: ['smith@uni.example', 'john.smith@uni.example'],
    text: 'is the full name of Smith (smith@uni.example) and also fits John Smith (john.smith@uni.example): use the address of the one meant.',
    reading: 'nobody: the name also fits another account' })
  assert.deepEqual([read('John Smith').problem, read('John Smith').exact, read('John Smith').also], ['ambiguous', 'john.smith@uni.example', ['smith@uni.example']])
  assert.deepEqual([read('Ana').exact, read('Ana').also, read('Ana Silva').exact, read('Ana Silva').also],
    ['ana@uni.example', ['ana.silva@uni.example'], 'ana.silva@uni.example', ['ana@uni.example']])
  // The words in another order: said so.
  assert.deepEqual([read('Wei Wang').on, read('Wei Wang').text], [[],
    'is, in another order, the full name of Wang Wei (wang.wei@uni.example) and also fits Wei J. Wang (wei.wang@uni.example): use the address of the one meant.'])
  assert.deepEqual([read('Wang, Wei').exact, read('Wang, Wei').also, read('Wang Wei').also], ['wang.wei@uni.example', ['wei.wang@uni.example'], ['wei.wang@uni.example']])
  // At commas, each name must be one person's.
  assert.deepEqual([read('Smith, Emmy Noether').on, read('Smith, Emmy Noether').text], [[],
    'The names on this line fit 3 accounts (smith@uni.example, john.smith@uni.example, emmy.noether@example.org): use the address of each person meant.'])
  assert.equal(read('Smith, Emmy Noether').reading, 'nobody: a name on it fits more than one account')
  // The address picks the one meant; a name that fits nobody else is still taken.
  assert.deepEqual([read('smith@uni.example').on, read('Ana <ana@uni.example>').on, read('Emmy Noether').on, read('Noether, Emmy').on],
    [['smith@uni.example'], ['ana@uni.example'], ['emmy.noether@example.org'], ['emmy.noether@example.org']])

  // A mark on the line (finding #6): nobody, on a name line and on an address line alike.
  for (const [entry, marks] of [['Emmy Noether ✗', ['✗']], ['emmy.noether@example.org ✗', ['✗']], ['~~Emmy Noether~~', ['~~']], ['Emmy Noether -', ['-']], ['Emmy Noether —', ['—']],
    ['Ada Lovelace\t✓', ['✓']], ['Ada Lovelace ❌', ['❌']], ['*Ada Lovelace*', ['*']], ['Ada Lovelace × ', ['×']], ['Ada Lovelace!', ['!']], ['Ada Lovelace?', ['?']],
    ['Ada Lovelace & Emmy Noether', ['&']], ['ada.lovelace@example.org -', ['-']], ['Emmy Noether: ', [':']], ['✓ Ada Lovelace', ['✓']], ['Emmy Noether ✗ ~~', ['✗', '~~']]]) {
    const got = read(entry)
    assert.deepEqual([got.on, got.problem, got.marks], [[], 'marks', marks], entry)
  }
  assert.deepEqual([read('Emmy Noether ✗').text, read('Emmy Noether ✗').reading], ['The line also has “✗”, which is not understood. Remove the mark, or leave the person off the list.',
    'nobody: “✗” on the line is not understood'])
  assert.deepEqual([read('Emmy Noether ✗ ~~').text, read('✗').text], ['The line also has “✗” and “~~”, which are not understood. Remove the marks, or leave the person off the list.',
    'The line has “✗”, which is not understood. Remove the mark, or leave the person off the list.'])
  // Names and addresses as people write them are not marks.
  for (const entry of ['Ada L. <ada.lovelace@example.org>', '"Ada Lovelace" <ada.lovelace@example.org>', "'Ada Lovelace'", 'Ada Lovelace - ada.lovelace@example.org', 'Ada Lovelace — ada.lovelace@example.org',
    'Ada Lovelace: ada.lovelace@example.org', 'Ada Lovelace | ada.lovelace@example.org', 'Ada Lovelace\tada.lovelace@example.org', 'ada.lovelace@example.org, Ada Lovelace,',
    '<mailto:ada.lovelace@example.org>', 'Ada Lovelace (ada.lovelace@example.org)', '– Ada Lovelace', '• Ada Lovelace', '◦ Ada Lovelace', '▪ Ada Lovelace', '1) Ada Lovelace', 'Lovelace, Ada'])
    assert.deepEqual([read(entry).on, read(entry).problem], [['ada.lovelace@example.org'], ''], entry)
  assert.deepEqual(plain(step(`["Siobhan O'Brien",'Siobhan O’Brien','Jean-Pierre Serre','J.R.R. Tolkien','Prof. David Hilbert','Ada Lovelace.','St. John Smith','Gil·li','Kurt Gödel','Ｊｏｈｎ'].map(adminAccessMarks)`)),
    [[], [], [], [], [], [], [], [], [], []])

  // Drawn: these lines in the warning box, and in "How each line was read".
  view.step(`ACCOUNTS.plan=adminAccessPlan(${JSON.stringify(['Smith', 'Wei Wang', 'Emmy Noether ✗', 'emmy.noether@example.org ✗', 'Ada Lovelace'].join('\n'))},ACCOUNTS.list,'Field test'); renderAdminAccessPlan();`)
  const drawn = $('admin-acct-plan')
  assert.deepEqual(drawn.children.filter(el => el.classList.contains('acct-warn')).map(el => el.textContent), [
    'Nobody is turned on for these 4 lines:“Smith” is the full name of Smith (smith@uni.example) and also fits John Smith (john.smith@uni.example): use the address of the one meant.'
      + '“Wei Wang” is, in another order, the full name of Wang Wei (wang.wei@uni.example) and also fits Wei J. Wang (wei.wang@uni.example): use the address of the one meant.'
      + '“Emmy Noether ✗” The line also has “✗”, which is not understood. Remove the mark, or leave the person off the list.'
      + '“emmy.noether@example.org ✗” The line also has “✗”, which is not understood. Remove the mark, or leave the person off the list.'])
  assert.deepEqual([drawn.children[0].textContent, drawn.querySelector('details').open, drawn.querySelector('details').querySelectorAll('li').map(li => li.textContent)], ['1 on, 22 off', true, [
    '“Smith” → nobody: the name also fits another account', '“Wei Wang” → nobody: the name also fits another account', '“Emmy Noether ✗” → nobody: “✗” on the line is not understood',
    '“emmy.noether@example.org ✗” → nobody: “✗” on the line is not understood', '“Ada Lovelace” → Ada Lovelace (ada.lovelace@example.org)']])
  assert.deepEqual(view.logged, [])
})

test('round 3: beside an address only its account\'s name may be written; a sheet\'s labels ("control", "- no", "| intervention", "B") turn nobody on', async () => {
  const view = load(), { $, step } = view
  await loaded(view, listPayload({ extra: [...PUBLIC,
    { id: 80, email: 'wei.wang@uni.example', name: 'Wei J. Wang', access: 'off', group: GROUP }, { id: 81, email: 'robert.smith@uni.example', name: 'Robert Smith', access: 'off', group: GROUP },
    { id: 82, email: 'bob@x.org', name: 'Bob Brown', access: 'off', group: GROUP }] }))
  const read = entry => plain(step(`(()=>{ const p=adminAccessPlan(${JSON.stringify(entry)},ACCOUNTS.list,'Field test'), l=p.lines[0];
    return {on:p.on.map(a=>a.key),problem:l.problem,extra:l.extra,notes:l.notes,text:l.problem?adminAccessProblemText(l):'',reading:adminAccessReadingText(l)}; })()`))
  // The account's own name, in any order, with titles and initials: taken.
  for (const [entry, address] of [['Wang, Wei <wei.wang@uni.example>', 'wei.wang@uni.example'], ['Robert J. Smith <robert.smith@uni.example>', 'robert.smith@uni.example'],
    ['Smith, Robert <robert.smith@uni.example>', 'robert.smith@uni.example'], ['Smith, Robert J. <robert.smith@uni.example>', 'robert.smith@uni.example'],
    ['Robert Smith, robert.smith@uni.example', 'robert.smith@uni.example'], ['Rob Smith <robert.smith@uni.example>', 'robert.smith@uni.example'],
    ['Ada L <ada.lovelace@example.org>', 'ada.lovelace@example.org'], ['Lovelace, A. <ada.lovelace@example.org>', 'ada.lovelace@example.org'],
    ['A. Lovelace - ada.lovelace@example.org', 'ada.lovelace@example.org'], ['Prof. Ada Lovelace <ada.lovelace@example.org>', 'ada.lovelace@example.org'],
    ['ada.lovelace@example.org', 'ada.lovelace@example.org'], ['Bob Brown | bob@x.org', 'bob@x.org']])
    assert.deepEqual([read(entry).on, read(entry).problem], [[address], ''], entry)
  assert.deepEqual(read('Ada Lovelace <ada.lovelace@example.org>, Emmy Noether <emmy.noether@example.org>').on, ['ada.lovelace@example.org', 'emmy.noether@example.org'])
  // A sheet's labels beside the address: nobody, with a warning (not a note) that names the word and the account.
  assert.deepEqual(read('ada.lovelace@example.org control'), { on: [], problem: 'extra', extra: ['control'], notes: [],
    text: "The line also has “control”, which is not part of this account's name (Ada Lovelace). Remove it, or leave the person off the list.",
    reading: "nobody: “control” is not part of Ada Lovelace's name" })
  for (const [entry, extra] of [['ada.lovelace@example.org - no', ['no']], ['Ada Lovelace <ada.lovelace@example.org> | intervention', ['intervention']],
    ['Emmy Noether <emmy.noether@example.org> no', ['no']], ['bob@x.org | B', ['B']], ['ada.lovelace@example.org x', ['x']], ['Ada Lovelace <ada.lovelace@example.org> A', ['A']],
    ['Bob Brown <bob@x.org>\tB', ['B']], ['Ada Lovelace, ada.lovelace@example.org, 1', ['1']], ['ada.lovelace@example.org (control arm)', ['control', 'arm']],
    ['Robert J Smith <robert.smith@uni.example>', ['J']],
    // A letter is an initial only within the name, for a word of it not written out: not "A" after Ada Lovelace, nor in a field of its own.
    ['Ada Lovelace A <ada.lovelace@example.org>', ['A']], ['Lovelace <ada.lovelace@example.org> | A', ['A']]])
    assert.deepEqual([read(entry).on, read(entry).problem, read(entry).extra], [[], 'extra', extra], entry)
  assert.equal(read('ada.lovelace@example.org (control arm)').text,
    "The line also has “control” and “arm”, which are not part of this account's name (Ada Lovelace). Remove them, or leave the person off the list.")
  // Several addresses: a word that is none of their names stops the whole line.
  assert.deepEqual(read('ada.lovelace@example.org, emmy.noether@example.org, control'), { on: [], problem: 'extra', extra: ['control'], notes: [],
    text: "The line also has “control”, which is not part of these accounts' names (Ada Lovelace, Emmy Noether). Remove it, or leave these people off the list.",
    reading: "nobody: “control” is not part of these accounts' names" })
  assert.deepEqual(read('Ada Lovelace <ada.lovelace@example.org>, Emmy Noether <emmy.noether@example.org> | B').extra, ['B'])
  // What stays a note: a second address no account has, beside one that is someone's.
  assert.deepEqual([read('ada.lovelace@example.org, ada.lovelace+ws@example.org').on, read('ada.lovelace@example.org, ada.lovelace+ws@example.org').notes],
    [['ada.lovelace@example.org'], ['no account has the address ada.lovelace+ws@example.org: nobody is turned on for it']])

  // Drawn: in the warning box, not in a note; the details open.
  view.step(`ACCOUNTS.plan=adminAccessPlan(${JSON.stringify(['ada.lovelace@example.org control', 'Emmy Noether <emmy.noether@example.org> - no',
    'Bernhard Riemann <riemann@example.org> | intervention', 'Wang, Wei <wei.wang@uni.example>'].join('\n'))},ACCOUNTS.list,'Field test'); renderAdminAccessPlan();`)
  const drawn = $('admin-acct-plan')
  assert.deepEqual(drawn.children.filter(el => el.classList.contains('acct-warn') || el.classList.contains('acct-note-box')).map(el => el.textContent), [
    "Nobody is turned on for these 3 lines:“ada.lovelace@example.org control” The line also has “control”, which is not part of this account's name (Ada Lovelace). Remove it, or leave the person off the list."
      + "“Emmy Noether <emmy.noether@example.org> - no” The line also has “no”, which is not part of this account's name (Emmy Noether). Remove it, or leave the person off the list."
      + "“Bernhard Riemann <riemann@example.org> | intervention” The line also has “intervention”, which is not part of this account's name (Bernhard Riemann). Remove it, or leave the person off the list."])
  assert.deepEqual([drawn.children[0].textContent, drawn.querySelector('details').open], ['1 on, 19 off', true])
  assert.deepEqual(view.logged, [])
})

test('round 3: a change after Review sets the reviewed list aside; Review waits for a change on its way; nothing changes while a list is applied', async () => {
  const view = load(), { $, srv } = view
  const payload = () => listPayload({ extra: PUBLIC })
  await loaded(view, payload())
  const tick = address => { const box = rowOf($, address).querySelector('input'); box.checked = true; box.onchange() }
  const review = async () => { const done = $('admin-acct-list-review').onclick(); srv.answer(200, payload()); await done; assert.equal($('admin-acct-plan').hidden, false) }
  const ASIDE = ' The list you reviewed was set aside: press Review again.'
  $('admin-acct-list-open').onclick()
  $('admin-acct-list-text').value = 'Ada Lovelace\nEmmy Noether'
  await review()
  // A switch after Review (refuted #0): the plan goes when the change starts, and the status says to review again.
  const toggle = switchOf($, 'cantor@example.org')
  toggle.focus()
  let done = toggle.onclick()
  assert.deepEqual([$('admin-acct-plan').hidden, $('admin-acct-list-review').classList.contains('primary'), focused], [true, true, toggle])
  srv.answer(200, { updated: ['cantor@example.org'], skipped: [] }); await done
  assert.deepEqual(status($), ['Access on for Georg Cantor.' + ASIDE, 'acct-status ok'])
  // A change for the selection, and a group: the same.
  await review()
  tick('riemann@example.org')
  done = $('admin-acct-off').onclick()
  assert.equal($('admin-acct-plan').hidden, true)
  srv.answer(200, { updated: ['riemann@example.org'], skipped: [] }); await done
  assert.equal(status($)[0], 'Access turned off for 1 account. They are signed out until it is on again.' + ASIDE)
  await review()
  tick('hilbert@example.org'); $('admin-acct-group-name').value = 'Day two'
  done = $('admin-acct-group-set').onclick()
  assert.equal($('admin-acct-plan').hidden, true)
  srv.answer(200, { updated: ['hilbert@example.org'], skipped: [], group: 'Day two', group_is_new: true }); await flush(); srv.answer(200, payload()); await done
  assert.equal(status($)[0], '1 account now in Day two, a new group.' + ASIDE)
  // Without a plan, nothing is said about one.
  done = switchOf($, 'cantor@example.org').onclick(); srv.answer(200, { updated: ['cantor@example.org'], skipped: [] }); await done
  assert.equal(status($)[0], 'Access on for Georg Cantor.')
  // Review waits for a change still on its way...
  done = switchOf($, 'gauss@example.org').onclick()
  $('admin-acct-list-review').onclick()
  assert.deepEqual([srv.pending.length, status($)[0], $('admin-acct-plan').hidden], [1, 'An access change is still on its way: review the list once it is done.', true])
  srv.answer(200, { updated: ['gauss@example.org'], skipped: [] }); await done
  // ...and a change started while it reads the accounts leaves no plan.
  const reviewing = $('admin-acct-list-review').onclick()
  done = switchOf($, 'weyl@example.org').onclick()
  srv.answer(200, payload()); await reviewing
  assert.deepEqual([$('admin-acct-plan').hidden, status($)[0]], [true, 'Access was changed while the list was being reviewed: press Review again once the change is done.'])
  srv.answer(200, { updated: ['weyl@example.org'], skipped: [] }); await done
  // While the list is applied, no switch, change for the selection or group starts.
  await review()
  const applying = $('admin-acct-plan').querySelector('button.primary').onclick()
  assert.equal(srv.pending.length, 1)
  switchOf($, 'turing@example.org').onclick()
  tick('turing@example.org'); $('admin-acct-on').onclick(); $('admin-acct-group-name').value = 'Pilot'; $('admin-acct-group-set').onclick(); $('admin-acct-group-remove').onclick()
  assert.deepEqual([srv.pending.length, status($), access($, 'turing@example.org')], [1, ['The list is being applied: change access once it is done.', 'acct-status err'], ['false', 'Off', null]])
  srv.answer(200, { updated: ['ada.lovelace@example.org', 'emmy.noether@example.org'], turned_off: [], skipped: [] }); await flush(); srv.answer(200, payload()); await applying
  assert.match(status($)[0], /^Access set from the list for Field test: /)
  assert.deepEqual(view.logged, [])
})

test('round 3: ways in that a pause leaves open (sign-ups, guest access) are said by the switches and in the list\'s panel, only those open', async () => {
  const SIGN_UPS = 'sign-ups are open, so they could create a second account with another address or Google account (it would be in no group)'
  const GUESTS = 'guest access is on, so they could use the site as a guest'
  const LEAD = 'A paused participant can still get in another way: '
  const view = load(), { $, step } = view
  await loaded(view, { ...listPayload(), open_doors: { sign_ups: true, guests: true } })
  const doors = $('admin-acct-doors'), inList = $('admin-acct-list-doors')
  const both = LEAD + SIGN_UPS + '; and ' + GUESTS + '. To keep a control arm fully out, close sign-ups and guest access for the study.'
  assert.deepEqual([doors.hidden, doors.textContent, doors.children[0].tagName, doors.children[0].textContent, inList.textContent], [false, both, 'B', LEAD.trim(), both])
  // Said once: in the list's panel while it is open.
  $('admin-acct-list-open').onclick()
  assert.deepEqual([doors.hidden, inList.hidden], [true, false])
  $('admin-acct-list-cancel').onclick()
  assert.deepEqual([doors.hidden, inList.hidden], [false, false])
  // Only what is open; nothing when nothing is (or the server does not say).
  for (const [open, text] of [[{ sign_ups: true, guests: false }, LEAD + SIGN_UPS + '. To keep a control arm fully out, close sign-ups for the study.'],
    [{ sign_ups: false, guests: true }, LEAD + GUESTS + '. To keep a control arm fully out, close guest access for the study.'], [{ sign_ups: false, guests: false }, ''],
    [{ sign_ups: 'yes', guests: 1 }, ''], [undefined, '']]) {
    const again = view.step('loadAdminAccounts()'); view.srv.answer(200, { ...listPayload(), open_doors: open }); await again
    assert.deepEqual([doors.hidden, doors.textContent, inList.hidden, inList.textContent], [!text, text, !text, text], JSON.stringify(open))
  }
  assert.deepEqual(plain(step(`adminAccountsFromResponse({accounts:[],open_doors:{sign_ups:true}}).doors`)), { signUps: true, guests: false })
  assert.deepEqual(view.logged, [])
})

const FULL_WARNING = "Full access lets the account link its own Claude Code and Codex and run every engine, including harnesses that run commands on this server with no sandbox: such a run could read the server's files and other accounts' data. Give it only to people you trust."
function fullPayload() {
  const payload = listPayload({ extra: PUBLIC })
  for (const account of payload.accounts) account.full_access = account.is_admin || account.email === 'emmy.noether@example.org'
  return payload
}
const fullOf = ($, address) => rowOf($, address).querySelector('button.acct-full-switch')
const fullState = ($, address) => { const toggle = fullOf($, address); return [toggle.getAttribute('aria-checked'), toggle.textContent, toggle.disabled, toggle.getAttribute('aria-busy')] }

test('full access: a column with a switch per row (giving it is asked first, under the row; taking it away is at once), "Always" for administrators', async () => {
  const view = load(), { $, srv, step } = view
  await loaded(view, fullPayload())
  // The column: Yes or No, and Always (disabled) for an administrator; the counts say how many have it.
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), fullState($, 'emmy.noether@example.org'), fullState($, 'admin@example.org')],
    [['false', 'No', false, null], ['true', 'Yes', false, null], ['true', 'Always', true, null]])
  const ada = fullOf($, 'ada.lovelace@example.org')
  assert.deepEqual([ada.type, ada.getAttribute('role'), ada.getAttribute('aria-label'), ada.title, rowOf($, 'admin@example.org').children[7].title, fullOf($, 'emmy.noether@example.org').title],
    ['button', 'switch', 'Full access for Ada Lovelace', 'No full access: click to give it to Ada Lovelace (asked first)', 'An administrator always has full access.',
      'Full access: click to remove it from Emmy Noether'])
  assert.equal($('admin-acct-counts').textContent, '19 accounts · 13 access on · 6 off · 1 full access · 1 admin')
  // Read strictly: true only when the server says so, and always for an administrator.
  assert.deepEqual(plain(step("adminAccountsFromResponse({accounts:[{email:'a@x.org',is_admin:true,full_access:false},{email:'b@x.org',full_access:'yes'},{email:'c@x.org',full_access:true},{email:'d@x.org'}]}).accounts.map(a=>a.full_access)")),
    [true, false, true, false])
  // Giving it: asked first, in a row under Ada's, with what it opens; nothing is sent yet.
  ada.focus()
  ada.onclick()
  let ask = $('admin-acct-body').querySelector('tr.acct-ask-row')
  const tbody = $('admin-acct-body').querySelector('tbody')
  assert.deepEqual([srv.pending.length, tbody.children[tbody.children.indexOf(rowOf($, 'ada.lovelace@example.org')) + 1] === ask, ask.querySelector('td').colSpan,
    ask.querySelector('div').getAttribute('aria-label'), ask.querySelectorAll('p').map(p => p.textContent), ask.querySelectorAll('button').map(b => [b.textContent, b.type, b.className]),
    focused === button(ask, 'Give full access'), ada.getAttribute('aria-checked')],
    [0, true, 9, 'Full access for Ada Lovelace', ['Give Ada Lovelace (ada.lovelace@example.org) full access?', FULL_WARNING],
      [['Give full access', 'button', 'acct-btn primary'], ['Cancel', 'button', 'acct-btn']], true, 'false'])
  // Cancel: the question goes, nothing is sent, the focus is back on the switch.
  button(ask, 'Cancel').onclick()
  assert.deepEqual([$('admin-acct-body').querySelector('tr.acct-ask-row'), srv.pending.length, focused], [null, 0, ada])
  // Asked again and given: one request; the row changes in place; the status says what it opens.
  ada.onclick()
  let done = button($('admin-acct-body').querySelector('tr.acct-ask-row'), 'Give full access').onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.method, r.body])), [['/api/admin/accounts/full-access', 'POST', { emails: ['ada.lovelace@example.org'], full_access: 'on' }]])
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), focused, $('admin-acct-body').querySelector('tr.acct-ask-row')], [['true', 'Yes', false, 'true'], ada, null])
  srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [] }); await done
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), status($), $('admin-acct-counts').textContent],
    [['true', 'Yes', false, null], ['Full access given to Ada Lovelace: they can link their own Claude Code and Codex in Settings.', 'acct-status ok'],
      '19 accounts · 13 access on · 6 off · 2 full access · 1 admin'])
  // Taking it away: at once, no question.
  done = ada.onclick()
  assert.deepEqual([plain(srv.pending[0].body), $('admin-acct-body').querySelector('tr.acct-ask-row')], [{ emails: ['ada.lovelace@example.org'], full_access: 'off' }, null])
  srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [] }); await done
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), status($)[0]], [['false', 'No', false, null], 'Full access removed from Ada Lovelace.'])
  // Skipped by the server, or failed: the row goes back, and says why.
  ada.onclick(); done = button($('admin-acct-body').querySelector('tr.acct-ask-row'), 'Give full access').onclick()
  srv.answer(200, { updated: [], skipped: [{ email: 'ada.lovelace@example.org', reason: 'No account uses this email' }] }); await done
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), status($)], [['false', 'No', false, null], ['Ada Lovelace was not changed: No account uses this email.', 'acct-status err']])
  ada.onclick(); done = button($('admin-acct-body').querySelector('tr.acct-ask-row'), 'Give full access').onclick()
  srv.answer(500, { error: 'database is locked' }); await done
  assert.deepEqual([fullState($, 'ada.lovelace@example.org'), status($)[0]], [['false', 'No', false, null], 'Could not give full access to Ada Lovelace: database is locked.'])
  // An administrator's switch does nothing, and an administrator is never sent.
  step("toggleAdminAccountFull('admin@example.org')")
  assert.deepEqual([srv.pending.length, plain(await step("setAdminAccountsFull(['admin@example.org'],'on')"))], [0, { updated: [], skipped: [] }])
  // One question at a time; a search, a new list or leaving the view takes it away.
  ada.onclick(); fullOf($, 'ramanujan@example.org').onclick()
  ask = $('admin-acct-body').querySelectorAll('tr.acct-ask-row')
  assert.deepEqual([ask.length, ask[0].querySelector('div').getAttribute('aria-label')], [1, 'Full access for Srinivasa Ramanujan'])
  $('admin-acct-q').value = 'ramanujan'; $('admin-acct-q').oninput()
  assert.deepEqual([$('admin-acct-body').querySelector('tr.acct-ask-row'), step('ACCOUNTS.fullAsk')], [null, null])
  $('admin-acct-q').value = ''; $('admin-acct-q').oninput()
  ada.onclick()
  done = step('loadAdminAccounts()'); srv.answer(200, fullPayload()); await done
  assert.equal($('admin-acct-body').querySelector('tr.acct-ask-row'), null)
  fullOf($, 'ada.lovelace@example.org').onclick(); step('clearAdminAccountSecrets()')
  assert.deepEqual([$('admin-acct-body').querySelector('tr.acct-ask-row'), srv.pending.length], [null, 0])
  // The filter: the accounts with full access (an administrator always has it).
  $('admin-acct-filter').value = 'full'; $('admin-acct-filter').onchange()
  assert.deepEqual([rowsOf($).map(tr => tr.dataset.email), $('admin-acct-counts').textContent],
    [['admin@example.org', 'emmy.noether@example.org'], 'Full access: 1 account · 1 access on · 0 off · 1 full access · 1 admin'])
  $('admin-acct-q').value = 'ada'; $('admin-acct-q').oninput()
  assert.equal($('admin-acct-body').textContent, 'No account with full access matches.')
  assert.deepEqual(view.logged, [])
})

test('full access for the selected rows: giving it is always asked in the page (the count, what it opens, rows not shown); the selection goes after', async () => {
  const view = load(), { $, srv } = view
  await loaded(view, fullPayload())
  const tick = address => { const box = rowOf($, address).querySelector('input'); box.checked = true; box.onchange() }
  const search = text => { $('admin-acct-q').value = text; $('admin-acct-q').oninput() }
  const ask = $('admin-acct-bulk-ask'), give = $('admin-acct-full-on'), take = $('admin-acct-full-off')
  assert.deepEqual([give.disabled, take.disabled], [true, true])
  tick('ada.lovelace@example.org'); tick('ramanujan@example.org')
  assert.deepEqual([give.disabled, take.disabled], [false, false])
  give.focus()
  give.onclick()
  assert.equal(srv.pending.length, 0)
  assert.deepEqual([ask.hidden, ask.children.slice(0, 2).map(p => [p.id, p.textContent]), ask.querySelectorAll('button').map(b => b.textContent),
    focused === button(ask, 'Give full access to 2'), button(ask, 'Give full access to 2').getAttribute('aria-describedby')],
    [false, [['admin-acct-bulk-question', 'Give full access to 2 accounts?'], ['admin-acct-bulk-warning', FULL_WARNING]], ['Give full access to 2', 'Cancel'], true,
      'admin-acct-bulk-question admin-acct-bulk-warning'])
  button(ask, 'Cancel').onclick()
  assert.deepEqual([ask.hidden, srv.pending.length, focused, $('admin-acct-selected').textContent], [true, 0, give, '2 selected'])
  give.onclick()
  let done = button(ask, 'Give full access to 2').onclick()
  assert.deepEqual(plain(srv.pending.map(r => [r.url, r.body])), [['/api/admin/accounts/full-access', { emails: ['ada.lovelace@example.org', 'ramanujan@example.org'], full_access: 'on' }]])
  srv.answer(200, { updated: ['ada.lovelace@example.org', 'ramanujan@example.org'], skipped: [] }); await done
  assert.deepEqual([status($), fullState($, 'ramanujan@example.org')[1], $('admin-acct-selected').textContent, focused],
    [['Full access given to 2 accounts.', 'acct-status ok'], 'Yes', 'None selected', $('admin-acct-q')])
  // Taking it away from rows all shown: no question.
  tick('ada.lovelace@example.org'); tick('ramanujan@example.org')
  done = take.onclick()
  assert.deepEqual([ask.hidden, plain(srv.pending[0].body)], [true, { emails: ['ada.lovelace@example.org', 'ramanujan@example.org'], full_access: 'off' }])
  srv.answer(200, { updated: ['ada.lovelace@example.org'], skipped: [{ email: 'ramanujan@example.org', reason: 'No account uses this email' }] }); await done
  assert.deepEqual(status($), ['Full access removed from 1 account. Skipped 1: ramanujan@example.org (No account uses this email).', 'acct-status err'])
  // Rows not shown are named, with the count, for both.
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Turing'); tick('turing@example.org')
  give.onclick()
  assert.deepEqual([ask.children[0].textContent, ask.children[1].textContent, ask.querySelectorAll('button').map(b => b.textContent)],
    ['Give full access to 2 accounts, 1 not shown here: Ada Lovelace?', FULL_WARNING, ['Give full access to all 2', 'Only the 1 shown', 'Cancel']])
  done = button(ask, 'Only the 1 shown').onclick()
  assert.deepEqual(plain(srv.pending[0].body), { emails: ['turing@example.org'], full_access: 'on' })
  srv.answer(200, { updated: ['turing@example.org'], skipped: [] }); await done
  assert.equal($('admin-acct-selected').textContent, 'None selected')
  search('Lovelace'); tick('ada.lovelace@example.org'); search('Turing'); tick('turing@example.org')
  take.onclick()
  assert.deepEqual([ask.children[0].textContent, ask.children.length, ask.querySelectorAll('button').map(b => b.textContent)],
    ['Remove full access from 2 accounts, 1 not shown here: Ada Lovelace?', 2, ['Remove full access from all 2', 'Only the 1 shown', 'Cancel']])
  done = button(ask, 'Remove full access from all 2').onclick()
  srv.answer(500, { error: 'database is locked' }); await done
  assert.deepEqual([status($)[0], $('admin-acct-selected').textContent], ['Could not remove full access: database is locked.', '2 selected (1 not shown)'])
  assert.deepEqual(view.logged, [])
})

test('full access for added accounts: a checkbox with what it opens; new accounts get it, existing ones on the list never do (said in the preview and the run)', async () => {
  const view = load(), { $, srv, step } = view
  await loaded(view, fullPayload())
  const full = $('admin-acct-add-full'), warn = $('admin-acct-add-full-warn')
  assert.deepEqual([full.checked, warn.hidden, warn.textContent], [false, true, FULL_WARNING])
  $('admin-acct-add-text').value = 'Maryam Mirzakhani - maryam@example.org\nemmy.noether@example.org\nada.lovelace@example.org'
  $('admin-acct-add-group').value = GROUP
  // Unticked: nothing about full access is sent.
  let done = $('admin-acct-add-create').onclick()
  assert.equal('full_access' in srv.pending[0].body, false)
  srv.answer(200, { results: previewOf(view, { 'emmy.noether@example.org': 'exists', 'ada.lovelace@example.org': 'exists' }, GROUP) }); await done
  // Ticked: what it opens shows under it, and the preview is asked again.
  full.checked = true; full.onchange()
  assert.deepEqual([warn.hidden, $('admin-acct-preview').hidden], [false, true])
  done = $('admin-acct-add-create').onclick()
  assert.equal(srv.pending[0].body.full_access, true)
  const results = previewOf(view, { 'emmy.noether@example.org': 'exists', 'ada.lovelace@example.org': 'exists' }, GROUP)
  // Ada exists without full access: the list does not give it to her (who registered her address may not be her).
  results[0].full_access = true; results[2].full_access_skipped = true
  srv.answer(200, { results }); await done
  const preview = $('admin-acct-preview')
  assert.deepEqual([preview.children[3].textContent, preview.querySelector('tbody').children.map(tr => tr.children[2].textContent)],
    ['New accounts get full access. 1 existing account is not given full access by a list: check that it is the right person, then give it from the table.',
      ['New account · joins Field test · full access', 'Already exists · joins Field test', 'Already exists · joins Field test · full access not given (check, then use the table)']])
  // The run: full access in each request; the status counts it.
  done = confirmButton($).onclick()
  assert.equal(srv.pending[0].body.full_access, true)
  srv.answer(200, { results: [
    { email: 'maryam@example.org', name: 'Maryam Mirzakhani', line: 'Maryam Mirzakhani - maryam@example.org', status: 'created', password: 'Mm1', group: GROUP, full_access: true },
    { email: 'emmy.noether@example.org', name: '', line: 'emmy.noether@example.org', status: 'exists', error: 'An account with this email already exists', group: GROUP, group_joined: true },
    { email: 'ada.lovelace@example.org', name: '', line: 'ada.lovelace@example.org', status: 'exists', error: 'An account with this email already exists', group: GROUP, group_joined: true, full_access_skipped: true }] })
  await flush()
  assert.deepEqual(srv.pending.map(r => [r.url, r.method]), [['/api/admin/accounts', 'GET']])
  srv.answer(200, fullPayload()); await done
  assert.equal(status($, 'admin-acct-add-status')[0], '1 created with full access, 2 already existed, 2 existing added to Field test, 1 existing not given full access (check it, then use the table).')
  assert.deepEqual($('admin-acct-results').querySelector('tbody').children.map(tr => tr.children[2].textContent), ['CreatedIn Field testFull access',
    'Already existsAn account with this email already existsAdded to Field test', 'Already existsAn account with this email already existsAdded to Field testFull access not given (check, then use the table)'])
  assert.equal(plain(step("adminAccountsCreatedText({rows:[{status:'exists',full_access_skipped:true},{status:'exists',full_access_skipped:true}],total:2,fullAccess:true,group:''})")),
    '0 created, 2 already existed, 2 existing not given full access (check them, then use the table).')
  // Full access the server could not give: said in the status and on the row, with where to give it.
  $('admin-acct-add-text').value = 'Hilda Geiringer - hilda@example.org'
  done = $('admin-acct-add-create').onclick(); srv.answer(200, { results: previewOf(view, {}, GROUP) }); await done
  done = confirmButton($).onclick()
  srv.answer(200, { results: [{ email: 'hilda@example.org', name: 'Hilda Geiringer', line: 'Hilda Geiringer - hilda@example.org', status: 'created', password: 'Hg1', group: GROUP,
    full_access_error: 'Could not be given full access; try again from the table' }] })
  await flush(); srv.answer(200, fullPayload()); await done
  assert.equal(status($, 'admin-acct-add-status')[0], '1 created, 1 not given full access (give it from the table).')
  assert.equal($('admin-acct-results').querySelector('tbody').children.at(-1).children[2].textContent, 'CreatedIn Field testCould not be given full access; try again from the table')
  // Unticked, what it opens is not shown.
  full.checked = false; full.onchange()
  assert.equal(warn.hidden, true)
  assert.deepEqual(view.logged, [])
})

test('full access outside Accounts: the engine and harness choices a full-access account may use; the Admin tab and pages stay the administrator\'s', () => {
  const context = vm.createContext({})
  vm.runInContext(['let CURRENT_USER=null;', constant('CGM_MAX_MODEL'), fn('viewerIsAdmin'), fn('viewerHasFullAccess'), fn('modelVisual')].join('\n'), context)
  const as = user => { vm.runInContext(`CURRENT_USER=${JSON.stringify(user)}`, context); return plain(vm.runInContext('[viewerIsAdmin(),viewerHasFullAccess(),modelVisual("gpt-5.4").provider]', context)) }
  assert.deepEqual([as({ id: 1, is_admin: true }), as({ id: 2, is_admin: false, full_access: true }), as({ id: 3, guest: true, full_access: true }), as({ id: 4, is_admin: false }), as(null)], [
    [true, false, 'OpenAI · Codex-ready'], [false, true, 'OpenAI · Codex-ready'], [false, false, 'OpenAI'], [false, false, 'OpenAI'], [false, false, 'OpenAI']])
  // The gates the server opens to full access: the Base URL of an OpenAI-compatible endpoint, the formal harness and its engines.
  assert.match(fn('renderSettingsModal'), /const baseField=\(viewerIsAdmin\(\)\|\|viewerHasFullAccess\(\)\)&&p\.base_var\?/)
  assert.match(fn('syncLeanPipelineEmptyState'), /\(viewerIsAdmin\(\)\|\|viewerHasFullAccess\(\)\?' Retry Verify, or choose an available Harness\.':' Retry Verify\.'\)/)
  assert.match(fn('syncLeanControls'), /const harness=viewerIsAdmin\(\)\|\|viewerHasFullAccess\(\);/)
  assert.match(fn('fillLeanEngines'), /leanEngines=viewerIsAdmin\(\)\|\|viewerHasFullAccess\(\)\?\(list\|\|\[\]\):\[\];/)
  assert.equal((consoleScript.match(/viewerHasFullAccess\(\)/g) || []).length, 6)
  // What stays the administrator's: the curator, the accounts, legacy runs, the Admin tab and its pages; Analyze and
  // Readability follow the server's "available" (a runner that needs setup is shown to the administrator only).
  assert.match(fn('analyzeOffered'), /return !!e\.available\|\|viewerIsAdmin\(\);/)
  assert.match(fn('readabilityOffered'), /return !!e\.available\|\|viewerIsAdmin\(\);/)
  assert.match(consoleScript, /const curator=viewerIsAdmin\(\);/)
  assert.match(fn('readAdminAccounts'), /if\(!viewerIsAdmin\(\)\)return false;/)
  assert.match(consoleScript, /return run\.owner_id==null\?viewerIsAdmin\(\):run\.owner_id===CURRENT_USER\.id\|\|viewerIsAdmin\(\);/)
  assert.match(fn('loadAdmin'), /if\(!CURRENT_USER\?\.is_admin\)return;/)
  assert.match(consoleScript, /\$\('hub-admin-tab'\)\.style\.display=u\.is_admin&&!u\.guest\?'':'none';/)
})

test('hostile names, addresses and server messages are set as text', async () => {
  const view = load(), { $, srv } = view
  const evil = '<img src=x onerror="alert(1)">'
  await loaded(view, listPayload({ extra: [{ id: 66, email: 'mallory@example.org', name: evil + 'Mallory', access: 'off', group: '<b>g</b>', created_at: '<b>x</b>', last_sign_in_at: '"><script>alert(2)</script>' }] }))
  const row = rowOf($, 'mallory@example.org')
  // (Dates that cannot be read show their first 16 characters, as text.)
  assert.deepEqual(row.children.slice(1, 6).map(td => td.textContent), [evil + 'Mallory', 'mallory@example.org', '<b>g</b>', '<b>x</b>', '"><script>alert('])
  assert.equal(switchOf($, 'mallory@example.org').getAttribute('aria-label'), `Access for ${evil}Mallory`)
  assert.deepEqual(options($('admin-acct-filter'))[1], ['=<b>g</b>', '<b>g</b>'])
  assert.deepEqual($('admin-acct-body').elements().filter(el => ['IMG', 'SCRIPT', 'B'].includes(el.tagName)), [])
  let done = switchOf($, 'mallory@example.org').onclick()
  srv.answer(200, { updated: [], skipped: [{ email: 'mallory@example.org', reason: '<i>nope</i>' }] }); await done
  assert.equal($('admin-acct-status').textContent, `${evil}Mallory was not changed: <i>nope</i>.`)
  $('admin-acct-add-text').value = 'x@example.org'
  done = $('admin-acct-add-create').onclick()
  srv.answer(200, { results: [{ email: '<svg onload=alert(3)>@x.org', name: evil, line: '<u>line</u>', status: 'new' }] }); await done
  assert.deepEqual($('admin-acct-preview').querySelector('tbody').children[0].children.map(td => td.textContent), [evil, '<svg onload=alert(3)>@x.org', 'New account', '<u>line</u>'])
  done = confirmButton($).onclick()
  srv.answer(200, { results: [{ email: '<svg onload=alert(3)>@x.org', name: evil, status: 'created', password: '<script>' }] }); await done
  srv.answer(200, listPayload()); await flush()
  assert.deepEqual($('admin-acct-results').querySelector('tbody').children[0].children.slice(0, 4).map(td => td.textContent), [evil, '<svg onload=alert(3)>@x.org', 'Created', '<script>'])
  $('admin-acct-list-open').onclick()
  $('admin-acct-list-text').value = '<svg onload=alert(4)>'
  done = $('admin-acct-list-review').onclick(); srv.answer(200, listPayload()); await done
  assert.match($('admin-acct-plan').textContent, /Nobody is turned on for this line:“<svg onload=alert\(4\)>” The line also has “=”, which is not understood\./)
  assert.deepEqual([$('admin-acct-results'), $('admin-acct-plan'), $('admin-acct-preview')].flatMap(root => root.elements()).filter(el => ['IMG', 'SCRIPT', 'SVG', 'I', 'U'].includes(el.tagName)), [])
  assert.deepEqual(view.logged, [])
})

test('leaving the view, or another run, clears the one-time passwords; creating stops there', async () => {
  const view = load(), { $, step, srv, confirms, answers } = view
  await loaded(view, listPayload({ emailAvailable: true }))
  $('admin-acct-add-text').value = 'a@example.org'
  await previewed(view)
  let done = confirmButton($).onclick()
  srv.answer(200, { results: [{ email: 'a@example.org', name: '', line: 'a@example.org', status: 'created', password: 'Aa1' }] }); await done
  srv.answer(200, listPayload({ emailAvailable: true }))
  button(rowOf($, 'riemann@example.org'), 'New password').onclick()
  done = button($('admin-acct-body').querySelector('tr.acct-reset-row'), 'Set new password').onclick()
  srv.answer(200, { email: 'riemann@example.org', password: 'Rr2' }); await done
  answers.confirm = true
  assert.equal(step('adminAccountsMayLeave()'), true)
  assert.equal(confirms.at(-1), '2 new passwords shown here were neither emailed nor copied, and cannot be shown again. Leave anyway?')
  step('clearAdminAccountSecrets()')
  assert.deepEqual([$('admin-acct-results').hidden, $('admin-acct-results').children.length, step('ACCOUNTS.results'), step('ACCOUNTS.resets.size'),
    $('admin-acct-body').querySelector('tr.acct-reset-row'), $('admin-acct-body').textContent.includes('Rr2'), step('adminAccountsMayLeave()')], [true, 0, null, 0, null, false, true])
  // Who was left with a password nobody knows is said (finding #8).
  assert.deepEqual(status($), ['The new passwords of a@example.org, riemann@example.org were cleared without being emailed or copied: give these accounts a new password from the table.', 'acct-status err'])

  // Left while accounts are being made: asked first (counting the passwords already shown and not saved); if left,
  // the lines not answered go back in the box, and the answer arriving afterwards draws nothing and sends no further request.
  $('admin-acct-add-send').checked = true
  const lines = Array.from({ length: 7 }, (_, i) => `p${i}@example.org`)
  lines[1] = 'bounce1@example.org'
  $('admin-acct-add-text').value = lines.join('\n')
  await previewed(view, {}, { emailAvailable: true })
  done = confirmButton($).onclick()
  srv.answer(200, { results: lines.slice(0, 5).map(address => ({ email: address, name: '', line: address, status: 'created', password: 'X', email_sent: !address.startsWith('bounce') })) }); await flush()
  assert.equal(srv.pending.length, 1)
  answers.confirm = false
  assert.equal(step('adminAccountsMayLeave()'), false)
  assert.equal(confirms.at(-1), 'Accounts are still being created, or a password set: if you leave now, the passwords not shown yet cannot be shown again, '
    + 'and 1 new password shown here was neither emailed nor copied. Leave anyway?')
  step('clearAdminAccountSecrets()')
  assert.equal($('admin-acct-add-text').value, lines.slice(5).join('\n'))
  assert.deepEqual(status($, 'admin-acct-add-status'), ['Creating accounts stopped when you left this view. 2 lines were not confirmed and are back in the box; '
    + 'anyone already created shows as “Already exists” if you retry (give them a new password from the table).', 'acct-status err'])
  assert.deepEqual(status($), ['The new password of bounce1@example.org was cleared without being emailed or copied: give this account a new password from the table.', 'acct-status err'])
  srv.answer(200, { results: lines.slice(5).map(address => ({ email: address, name: '', line: address, status: 'created', password: 'X', email_sent: true })) }); await done
  assert.deepEqual([$('admin-acct-results').hidden, srv.pending.length, step('ACCOUNTS.creating'), $('admin-acct-add-create').textContent], [true, 0, null, 'Create accounts'])
  // A preview is dropped too, and a late answer for it is not shown.
  done = $('admin-acct-add-create').onclick()
  step('clearAdminAccountSecrets()')
  srv.answer(200, { results: previewOf(view) }); await done
  assert.deepEqual([$('admin-acct-preview').hidden, $('admin-acct-add-create').textContent], [true, 'Create accounts'])

  // Where the page calls it: closing the view, leaving the Admin tab, another run; and the accounts load with the usage.
  assert.match(fn('closeLibrary'), /if\(!adminAccountsMayLeave\(\)\)return false;\n\s*\$\('library-modal'\)\.classList\.remove\('open'\);\n\s*clearAdminAccountSecrets\(\);\n\s*return true;/)
  assert.match(fn('switchHub'), /if\(hubTab==='admin'&&next!=='admin'\)\{ if\(!adminAccountsMayLeave\(\)\)return; clearAdminAccountSecrets\(\); \}/)
  assert.match(fn('switchHub'), /\}else if\(hubTab==='admin'\)\{\n(?:\s*\/\/.*\n)?\s*loadAdminAccounts\(\);\n\s*loadAdmin\(\);\n\s*\}/)
  assert.match(fn('selectRun'), /if\(runId!==rid\)\{[^}]*if\(typeof clearAdminAccountSecrets==='function'\)clearAdminAccountSecrets\(\);/)
  assert.match(consoleScript, /a\.onclick=\(\)=>\{ if\(!closeLibrary\(\)\)return; selectRun\(a\.dataset\.adminRun,null\);/)
  assert.deepEqual(view.logged, [])
})

test('the Accounts section comes first in the admin view, with plain buttons, status regions, and no password kept outside it', () => {
  const pane = html.slice(html.indexOf('<section class="hub-pane" id="hub-admin-pane">'), html.indexOf('<section class="hub-pane" id="hub-agent-pane">'))
  const section = pane.slice(pane.indexOf('id="admin-accounts"'), pane.indexOf('<div class="hub-section-h admin-usage-h">'))
  assert.ok(pane.indexOf('id="admin-accounts"') > 0 && pane.indexOf('id="admin-accounts"') < pane.indexOf('id="admin-filters"'))
  assert.match(section, /<div class="hub-section-h"><b id="admin-acct-title">Accounts<\/b><span class="acct-counts" id="admin-acct-counts"><\/span><\/div>/)
  const buttons = section.match(/<button\b[^>]*>/g)
  assert.equal(buttons.length, 12)
  assert.deepEqual(buttons.filter(tag => !tag.includes('type="button"')), [])
  // Nothing to add or apply until the list is read; email waits for it too.
  for (const id of ['admin-acct-list-open', 'admin-acct-add-create']) assert.match(section, new RegExp(`id="${id}"[^>]*disabled`))
  assert.match(section, /<input type="checkbox" id="admin-acct-add-send" disabled\/> Email each person their sign-in details<\/label>/)
  assert.match(section, /<p class="acct-status" id="admin-acct-status" role="status" aria-live="polite"><\/p>/)
  assert.match(section, /<p class="acct-status" id="admin-acct-add-status" role="status" aria-live="polite"><\/p>/)
  assert.match(section, /<p class="acct-note" id="admin-acct-add-nomail" hidden>This site cannot send email \(SMTP is not set up\) — copy the details below and send them yourself\.<\/p>/)
  assert.match(section, /<p class="acct-note" id="admin-acct-add-nourl" hidden>Emails need the site's address \(AGENT_MONITOR_APP_URL\), which is not set\./)
  // Group names are cut and cleaned by the server only (finding #5): no maxlength in the page.
  assert.match(section, /<input type="text" id="admin-acct-add-group" list="admin-acct-groups" placeholder="e\.g\. Field test" autocomplete="off"/)
  assert.doesNotMatch(section, /maxlength/)
  // ...kept in sight beside the passwords wherever the section is scrolled.
  assert.match(html, /#admin-acct-session \{ position: sticky; top: 0; z-index: 5;[^}]*background: color-mix\(in srgb, var\(--warn\) 14%, var\(--bg2\)\);/)
  assert.match(section, /<p class="acct-warn" id="admin-acct-session" role="alert" hidden><b>Your session has ended\.<\/b> Copy the passwords shown here first \(they cannot be shown again\), then sign in again\.<\/p>/)
  assert.match(section, /<select id="admin-acct-filter" aria-label="Show the accounts of a group, or those with full access"><option value="all">All groups<\/option><option value="none">No group<\/option><option value="full">Full access<\/option><\/select>/)
  assert.match(section, /<datalist id="admin-acct-groups"><\/datalist>/)
  // Under the bar: the question about selected rows not shown (round 3, #4), and the ways in a pause leaves open;
  // the same note in the list's panel.
  assert.match(section, /id="admin-acct-group-remove" disabled>Remove from group<\/button>\n\s*<span class="acct-bulk-space" aria-hidden="true"><\/span>\n\s*<button type="button" class="acct-btn" id="admin-acct-full-on" disabled>Give full access<\/button>\n\s*<button type="button" class="acct-btn" id="admin-acct-full-off" disabled>Remove full access<\/button>\n\s*<\/div>\n\s*<div class="acct-warn acct-ask" id="admin-acct-bulk-ask" role="group" aria-label="Confirm the change for the selected accounts" hidden><\/div>\n\s*<p class="acct-doors" id="admin-acct-doors" hidden><\/p>/)
  assert.match(section, / Administrators always keep access\.<\/p>\n\s*<p class="acct-doors" id="admin-acct-list-doors" hidden><\/p>\n\s*<textarea class="acct-mono" id="admin-acct-list-text"/)
  assert.match(html, /\.acct-ask\[hidden\] \{ display: none; \}/)
  assert.match(section, /<input type="number" id="admin-acct-add-length" min="8" max="32" step="1" value="12"/)
  for (const placeholder of ['{name}', '{email}', '{password}', '{url}', '{sender}']) assert.ok(section.includes(`<code>${placeholder}</code>`), placeholder)
  // The section's script: no storage, no console, no URL of the page, no HTML from strings; requests carry nothing in their URL.
  for (const banned of [/\blocalStorage\b/, /\bsessionStorage\b/, /\bindexedDB\b/, /\bconsole\.[a-z]/, /\blocation\b/, /\bhistory\./, /innerHTML/, /insertAdjacentHTML/, /outerHTML/, /document\.write/])
    assert.doesNotMatch(block, banned)
  assert.deepEqual([...new Set(block.match(/'\/api\/[^']*'/g))].sort(),
    ["'/api/admin/accounts'", "'/api/admin/accounts/access'", "'/api/admin/accounts/full-access'", "'/api/admin/accounts/group'", "'/api/admin/accounts/password'"])
  assert.equal(block.match(/fetch\(/g).length, 1)
  // The Admin tab gets the hub's own width; a new-password panel stays in view in a table that scrolls sideways.
  assert.match(html, /\.modal\.monitor-hub:has\(#hub-admin-pane\.active\) \{ width: min\(1220px, 100%\); \}/)
  assert.match(html, /\.acct-table-wrap \{ container-type: inline-size; \}\n\.acct-reset-row \.acct-reset, \.acct-ask-row \.acct-reset \{ position: sticky; left: 9px; box-sizing: border-box; max-width: calc\(100cqi - 18px\); \}/)
})
