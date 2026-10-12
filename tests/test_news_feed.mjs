import test from 'node:test'
import assert from 'node:assert/strict'
import { entryDate, filterNews, groupNews, newsDateLabel, newsFreshness, newsHighlights, parseNewsFeed, problemHref, problemLabel, safeNewsUrl, summarySources } from '../docs/.vitepress/theme/newsFeed.mjs'

const item = (id, published_at, category = 'research') => ({ id, title: `Source headline ${id}`, summary: 'A source-attributed summary.', url: `https://example.org/news/${id}`, source: 'example', category, published_at, status: 'Published' })
const snapshot = items => ({ schema_version: 1, refresh_interval_minutes: 30, items, sources: [{ id: 'example', name: 'Example source', url: 'https://example.org' }] })

test('news is chronological, deduplicated, and filtered by the requested category', () => {
  const oldest = item('oldest', '2026-09-12T00:00:00Z', 'mathematics')
  const newest = item('newest', '2026-09-16T12:00:00Z', 'models')
  const feed = parseNewsFeed(snapshot([oldest, newest, { ...newest }]))
  assert.deepEqual(feed.items.map(value => value.id), ['newest', 'oldest'])
  assert.deepEqual(filterNews(feed.items, 'mathematics').map(value => value.id), ['oldest'])
  assert.equal(filterNews(feed.items), feed.items)
  assert.equal(filterNews(feed.items, 'research').length, 0)
})

test('source URLs permit HTTPS and reject active schemes and embedded credentials', () => {
  assert.equal(safeNewsUrl('https://example.org/news?item=1#claim'), 'https://example.org/news?item=1#claim')
  for (const value of ['http://example.org', 'javascript:alert(1)', '//example.org', 'https://user:secret@example.org', '/relative', null]) assert.equal(safeNewsUrl(value), null)
})

test('malformed stories are excluded without discarding other source stories', () => {
  const valid = item('valid', '2026-09-16T00:00:00Z')
  const feed = parseNewsFeed({ ...snapshot([valid, { ...valid, id: 'unsafe', url: 'javascript:alert(1)' }, { ...valid, id: 'undated', published_at: '' }, { ...valid, id: 'untitled', title: {} }, { ...valid, id: 'unknown', category: 'rumour' }]), sources: [null, { name: 'Unsafe', url: 'https://user:secret@example.org' }, { id: 'example', name: 'Example', url: 'https://example.org/' }] })
  assert.deepEqual(feed.items.map(value => value.id), ['valid'])
  assert.deepEqual(feed.sources.map(value => value.id), ['example'])
})

test('unsupported or entirely malformed snapshots fail, while a valid empty feed is allowed', () => {
  for (const value of [null, {}, { schema_version: 2, items: [] }, snapshot([{}])]) assert.throws(() => parseNewsFeed(value), /could not be read/)
  assert.deepEqual(parseNewsFeed(snapshot([])).items, [])
})

test('related source statements are preserved with the same HTTPS URL checks', () => {
  const story = { ...item('claim', '2026-09-16T00:00:00Z'), related_links: [{ label: 'Institution response', url: 'https://example.org/response' }, { label: 'Unsafe', url: 'https://user:secret@example.org' }, null] }
  assert.deepEqual(parseNewsFeed(snapshot([story])).items[0].related_links, [{ label: 'Institution response', url: 'https://example.org/response' }])
})

test('a dated source headline remains available when its feed provides no summary', () => {
  const story = { ...item('headline-only', '2026-09-16T00:00:00Z'), summary: '' }
  const parsed = parseNewsFeed(snapshot([story]))
  assert.equal(parsed.items[0].title, story.title)
  assert.equal(parsed.items[0].summary, '')
})

test('freshness follows successful checks, not repeated failed refresh attempts', () => {
  const now = Date.parse('2026-09-16T12:00:00Z')
  const feed = { last_checked_at: '2026-09-16T11:59:00Z', last_successful_refresh_at: '2026-09-16T09:00:00Z', refresh_interval_minutes: 30, source_errors: [{ source_id: 'example', error: 'Source unavailable' }] }
  assert.equal(newsFreshness(feed, now).stale, true)
  assert.equal(newsFreshness({ ...feed, last_successful_refresh_at: '2026-09-16T11:30:00Z' }, now).stale, false)
  assert.equal(newsFreshness({ last_checked_at: feed.last_checked_at, source_errors: [] }, now).stale, true)
  assert.equal(newsFreshness(null, now).stale, true)
})

test('freshness respects slower source schedules and normalizes invalid intervals', () => {
  const now = Date.parse('2026-09-16T12:00:00Z')
  const feed = { last_successful_refresh_at: '2026-09-16T09:30:00Z', refresh_interval_minutes: 120 }
  assert.equal(newsFreshness(feed, now).stale, false)
  assert.equal(newsFreshness({ ...feed, refresh_interval_minutes: 'bad' }, now).interval, 30)
  assert.equal(newsFreshness({ ...feed, refresh_interval_minutes: -20 }, now).interval, 5)
})

test('optional fields from newer feeds are validated and malformed ones dropped without losing the story', () => {
  const good = { ...item('good', '2026-10-07T00:00:00Z', 'mathematics'), importance: 3, topic: 'ai-math-result', source_role: 'repository', date_precision: 'day', status: 'Correction', updated_at: '2026-10-08T05:03:50Z', problem_links: [
    { problem_id: 'erdos-0522', relation: 'claims-resolution', evidence: 'We resolve Erdős Problem #522', title: 'Erdős problem #522' },
    { problem_id: 'Bad Id', relation: 'mentions', evidence: 'x' },
    { problem_id: 'clay-hodge-conjecture', relation: 'solved', evidence: 'The Hodge conjecture is solved' },
    { problem_id: 'kourovka-12-020', relation: 'mentions', evidence: '' }] }
  const bad = { ...item('bad', '2026-10-06T00:00:00Z'), importance: 7, topic: 'gossip', source_role: 'blog', date_precision: 'hour', story_id: 'Not Valid', updated_at: '2026-01-01T00:00:00Z', problem_links: 'none' }
  const [first, second] = parseNewsFeed(snapshot([good, bad])).items
  assert.equal(first.importance, 3)
  assert.equal(first.topic, 'ai-math-result')
  assert.equal(first.source_role, 'repository')
  assert.equal(first.date_precision, 'day')
  assert.equal(first.updated_at, '2026-10-08T05:03:50Z')
  assert.deepEqual(first.problem_links, [{ problem_id: 'erdos-0522', relation: 'claims-resolution', evidence: 'We resolve Erdős Problem #522', title: 'Erdős problem #522' }])
  for (const key of ['importance', 'topic', 'source_role', 'date_precision', 'story_id', 'updated_at']) assert.equal(key in second, false, key)
  assert.deepEqual(second.problem_links, [])
})

const storyFeed = () => parseNewsFeed({ ...snapshot([
  { ...item('release', '2026-10-06T12:00:00Z', 'mathematics'), status: 'Proof announced', importance: 3, source_role: 'primary' },
  { ...item('update', '2026-10-07T00:00:00Z', 'mathematics'), status: 'Correction', importance: 3, source_role: 'repository' },
  { ...item('reaction', '2026-10-08T00:02:51Z', 'mathematics'), source_role: 'commentary' },
  { ...item('journal', '2026-10-07T15:08:46Z', 'research'), source_role: 'journalism', importance: 2 },
  { ...item('model', '2026-10-09T00:00:00Z', 'models'), importance: 2 },
  { ...item('lonely', '2026-10-01T00:00:00Z') }]),
  stories: [
    { id: 'openai-math', label: 'OpenAI’s openai/math release and updates', item_ids: ['release', 'update', 'reaction', 'journal', 'missing'], lead_id: 'release', updated_at: '2026-10-08T00:02:51Z' },
    { id: 'duplicate', label: 'Claims an item twice', item_ids: ['release', 'lonely'], lead_id: 'lonely' },
    { id: 'Bad id', label: 'x', item_ids: ['model', 'lonely'] },
    { id: 'single', label: 'One member', item_ids: ['lonely'] }] })

test('stories keep only published members, give each item one story and group the list by story', () => {
  const feed = storyFeed()
  assert.deepEqual(feed.stories.map(story => [story.id, story.item_ids, story.lead_id]), [['openai-math', ['release', 'update', 'reaction', 'journal'], 'release']])
  assert.equal(feed.items.find(row => row.id === 'lonely').story_id, undefined)
  const entries = groupNews(feed)
  assert.deepEqual(entries.map(entry => [entry.kind, entry.lead.id]), [['item', 'model'], ['story', 'release'], ['item', 'lonely']])
  const story = entries[1]
  assert.deepEqual(story.updates.map(row => row.id), ['update'])
  assert.deepEqual(story.related.map(row => row.id), ['reaction', 'journal'])
  // The newest blog reaction joins the story without moving it; journalism does move it.
  assert.equal(new Date(story.date).toISOString(), '2026-10-07T15:08:46.000Z')
  assert.equal(feed.stories[0].updated_at, '2026-10-07T15:08:46.000Z')
  // A story is filed, whole, under its lead's category: the research view does not
  // show its journalism member on its own, and category counts add up to the total.
  assert.deepEqual(groupNews(feed, 'mathematics').map(entry => [entry.kind, entry.lead.id, entry.related.length]), [['story', 'release', 2]])
  assert.deepEqual(groupNews(feed, 'research').map(entry => [entry.kind, entry.lead.id]), [['item', 'lonely']])
  assert.equal(['mathematics', 'models', 'research'].reduce((sum, id) => sum + groupNews(feed, id).length, 0), entries.length)
})

test('a filtered view never leads a story with a blog post or shows its members on their own', () => {
  const row = (id, category, role, date) => ({ ...item(id, date, category), source_role: role })
  const feed = parseNewsFeed({ ...snapshot([row('rel', 'models', 'primary', '2026-10-06T12:00:00Z'), row('c1', 'mathematics', 'commentary', '2026-10-07T12:00:00Z'),
    row('c2', 'mathematics', 'commentary', '2026-10-08T12:00:00Z')]), stories: [{ id: 'x', label: 'Model X release', item_ids: ['rel', 'c1', 'c2'], lead_id: 'rel' }] })
  assert.deepEqual(groupNews(feed, 'mathematics'), [])
  assert.deepEqual(groupNews(feed, 'models').map(entry => [entry.kind, entry.lead.id, entry.related.map(related => related.id)]), [['story', 'rel', ['c2', 'c1']]])
  // Blog posts alone do not move the story, so the card keeps its lead's date.
  const [entry] = groupNews(feed)
  assert.deepEqual(entryDate(entry), { updated: false, value: '2026-10-06T12:00:00Z', precision: undefined })
})

test('a story updated after its lead shows the update date in its date column', () => {
  const [, story] = groupNews(storyFeed())
  assert.deepEqual(entryDate(story), { updated: true, value: '2026-10-07T15:08:46.000Z', precision: undefined })
  const [, , lonely] = groupNews(storyFeed())
  assert.deepEqual(entryDate(lonely), { updated: false, value: '2026-10-01T00:00:00Z', precision: undefined })
})

test('summary provenance is kept only when it is known and the summary is not empty', () => {
  const rows = [{ ...item('model', '2026-10-07T00:00:00Z'), summary_source: 'model' }, { ...item('quote', '2026-10-06T00:00:00Z'), summary_source: 'source' },
    { ...item('editor', '2026-10-05T00:00:00Z'), summary_source: 'editor' }, { ...item('odd', '2026-10-04T00:00:00Z'), summary_source: 'oracle' },
    { ...item('empty', '2026-10-03T00:00:00Z'), summary: '', summary_source: 'model' }]
  assert.deepEqual(parseNewsFeed(snapshot(rows)).items.map(row => row.summary_source), ['model', 'source', 'editor', undefined, undefined])
  assert.deepEqual(Object.keys(summarySources), ['editor', 'model', 'source'])
})

test('highlights are important recent items, most important first', () => {
  const feed = storyFeed()
  const now = Date.parse('2026-10-11T00:00:00Z')
  assert.deepEqual(newsHighlights(feed.items, now).map(row => row.id), ['update', 'release', 'model', 'journal'])
  assert.deepEqual(newsHighlights(feed.items, now, { limit: 2 }).map(row => row.id), ['update', 'release'])
  assert.deepEqual(newsHighlights(feed.items, Date.parse('2026-10-25T00:00:00Z')).map(row => row.id), [])
})

test('problem chips link to the Problems page and dates follow their stated precision', () => {
  assert.equal(problemHref('erdos-0522'), '/problems?problem=erdos-0522')
  assert.equal(problemHref('a b&c'), '/problems?problem=a%20b%26c')
  assert.equal(problemLabel({ problem_id: 'erdos-0522' }), 'Erdős #522')
  assert.equal(problemLabel({ problem_id: 'kourovka-12-020' }), 'Kourovka 12.20')
  assert.equal(problemLabel({ problem_id: 'clay-hodge-conjecture' }), 'hodge conjecture')
  assert.equal(problemLabel({ problem_id: 'erdos-0522', title: 'Erdős problem #522' }), 'Erdős problem #522')
  assert.equal(newsDateLabel('2026-10-07T00:00:00Z', 'day'), 'Oct 7, 2026')
  assert.equal(newsDateLabel('2026-10-07T00:00:00Z', 'month'), 'October 2026')
  assert.equal(newsDateLabel('2026-10-07T00:00:00Z', 'year'), '2026')
  assert.equal(newsDateLabel('', 'day'), 'Not recorded')
})

test('feeds written before stories and links existed still parse', () => {
  const feed = parseNewsFeed(snapshot([item('old', '2026-09-16T00:00:00Z')]))
  assert.deepEqual(feed.stories, [])
  assert.deepEqual(feed.items[0].problem_links, [])
  assert.deepEqual(groupNews(feed).map(entry => entry.kind), ['item'])
})
