"""Kimi K3 tasks for the public news feed, through the credential-isolated broker.

Every task is one strict-JSON call (``kimi_tasks.call_json``, client id
``ansatze-news``, reasoning effort "low", no tools).  Source text is supplied
as untrusted data and every reply row must name a supplied id.  The model may
write short summaries and story labels, rate and group records, and relate a
record to candidate catalogue problems with a verbatim quote; it never writes
titles, URLs, dates, categories, statuses or links.  Schema violations reject
a whole reply; a row that is too long, contains links or markup, or uses a
number that is not in its source text is dropped on its own.
"""
from __future__ import annotations

from functools import partial
import html
import os
import re
import unicodedata

from agent_monitor import kimi_tasks
from agent_monitor import sponsored_kimi_client as client  # noqa: F401 - the broker transport (tests patch it here)
from agent_monitor.kimi_tasks import KimiTaskError

CLIENT_ID = 'ansatze-news'
CACHE_KEY = 'news-refresh'
MAX_ITEMS = 6
MAX_SUMMARY = 420
MAX_TRIAGE = 20
MAX_LINK_RECORDS = 8
MAX_STORY_ITEMS = 30
DAILY_CALLS = 60
# A refresh is optional work: it keeps a short timeout, and tells the broker so
# (X-Ansatze-Timeout) that the broker and gateway give up before it does.
TIMEOUT_SECONDS = 90
TOPICS = ('ai-math-result', 'formalization', 'model-release', 'community-debate', 'prize-or-award',
          'problem-status', 'tooling', 'other')
RELATIONS = ('mentions', 'claims-partial-progress', 'claims-resolution', 'claims-counterexample',
             'disputes-claim', 'withdraws-claim', 'formalization')
STORY_ROLES = ('primary', 'journalism', 'repository')

SUMMARY_SYSTEM = (
    'Write concise, factual news summaries using only the supplied article records. '
    'You have no tools and must not add facts, dates, sources, links, or verification. '
    'Preserve uncertainty: a proof announcement, preprint, or claimed result is not an accepted proof. '
    'Preprint does not establish whether a paper has been peer reviewed. Never assert that a paper '
    'is or is not peer reviewed unless the supplied excerpt explicitly establishes that fact. '
    'Attribute performance claims to the announcing researchers or company, and attribute opinions, '
    'criticism and commentary to the person or organization that expressed them; a guest post or a '
    'reposted statement belongs to its stated author, not to the blog that hosts it. '
    'Avoid promotional language. If the record has little detail, describe its topic without inventing '
    'specifics. Write about the article itself: never mention the excerpt, the supplied record or what '
    'the text does not say. Use only numbers that appear in that record\'s title or excerpt. '
    'Return only JSON: {"items":[{"id":"supplied ID","summary":"one or two sentences"}]}. '
    'Each summary must be 20 to 420 characters; use plain text, no HTML or Markdown. '
    'Explain mathematical content in words; do not emit TeX commands or formula markup. '
    'Do not return titles, URLs, categories, status, or publication dates.'
)
TRIAGE_SYSTEM = (
    'You triage candidate records for a public news feed about mathematics, AI systems that prove, '
    'formalize or discover mathematics, formal proof tools, model releases relevant to mathematical '
    'reasoning, and the mathematical community\'s debate about them. For every supplied record decide: '
    'decision "include" or "exclude" (exclude product marketing, customer stories, politics and items '
    'unrelated to mathematics); importance 0-3 (3: a major result, release, withdrawal or correction '
    'about named problems, or a major model release; 2: notable research, journalism or debate; '
    '1: routine but relevant; 0: marginal); and one topic from: ' + ', '.join(TOPICS) + '. '
    'Judge only from the supplied text; a claim is not an accepted result. '
    'Return only JSON {"items":[{"id":"supplied ID","decision":"include|exclude","importance":0,'
    '"topic":"…","reason":"at most 160 characters, plain text, no links"}]} with one row per record.'
)
LINK_SYSTEM = (
    'Each record lists candidate problems from a problem catalogue that its text may refer to. For each '
    'record, keep only the candidates the text actually discusses and say how, using one relation: '
    + ', '.join(RELATIONS) + '. Use claims-resolution for any claimed full answer, however confident the '
    'wording, and claims-counterexample for a claimed disproof; a claim is not an accepted result. '
    'Use withdraws-claim only when the text withdraws a claim, and disputes-claim only when it disputes '
    'one. "evidence" must be copied verbatim from the record\'s text (8 to 240 characters) and must show '
    'the relation. Use only the supplied record ids and candidate problem ids; at most three links per record. '
    'Return only JSON {"links":[{"id":"record ID","problem_id":"candidate ID","relation":"…","evidence":"…"}]}; '
    'return {"links":[]} when no candidate is discussed.'
)
STORY_SYSTEM = (
    'Group news records that cover the same event or development into stories. You may assign a '
    'record to one of the supplied stories by its story_id, or propose a new story of at least two '
    'supplied records, at least one of which has role primary, journalism or repository. Each record '
    'belongs to at most one story; leave unrelated records unassigned. A label is a short neutral '
    'description of the event (at most 80 characters, plain text, no links, no claims beyond the '
    'records, numbers only if they appear in the members\' titles or excerpts). Return only JSON '
    '{"assign":[{"id":"record ID","story_id":"supplied story ID"}],'
    '"new":[{"key":"n1","label":"…","member_ids":["record ID","record ID"]}]}.'
)
CHANGELOG_SYSTEM = (
    'Summarize one dated section of a public repository\'s change history for a news feed in one to '
    'three sentences (40 to 600 characters). Say what changed (for example withdrawals, revisions, '
    'corrections, additions) and attribute it to the repository. Reproduce counts, names and numbers '
    'exactly as written; do not round, add, combine or infer numbers. Do not describe results as '
    'verified, accepted or correct. Plain text only: no Markdown, HTML, TeX or links. '
    'Return only JSON {"summary":"…"}.'
)


class NewsSummaryError(KimiTaskError):
    """A public-safe failure of a news model task (``kind`` as in :class:`KimiTaskError`)."""

    def __init__(self, message: str, kind: str = 'invalid') -> None:
        super().__init__(message, kind)


class NewsBudget:
    """At most ``run_cap`` calls in this run and ``daily_cap`` per UTC day (persisted)."""

    def __init__(self, state: dict, daily_cap: int, run_cap: int, *, today: str | None = None) -> None:
        self.daily = kimi_tasks.DailyBudget(state, daily_cap, today=today)
        self.run_cap = max(0, int(run_cap))
        self.run_calls = 0

    def take(self) -> bool:
        if self.run_calls >= self.run_cap or not self.daily.take():
            return False
        self.run_calls += 1
        return True


def daily_cap(env=None) -> int:
    """``ANSATZE_NEWS_MODEL_CALLS_PER_DAY`` (default 60, at most 500); 0 turns the model off."""
    value = str((os.environ if env is None else env).get('ANSATZE_NEWS_MODEL_CALLS_PER_DAY') or '').strip()
    if not value:
        return DAILY_CALLS
    try:
        return max(0, min(500, int(value)))
    except ValueError:
        return DAILY_CALLS


_NUMBER = re.compile(r'\d+(?:[.,]\d+)*')
# "The supplied excerpt gives no details": text about the model's input, not about the source.
INPUT_TALK = re.compile(r'\b(?:excerpts?|supplied (?:text|record|article|item)s?|(?:article|source) records?)\b', re.I)


def _numbers(text: str) -> set[str]:
    found = set(_NUMBER.findall(text))
    return found | {value.replace(',', '') for value in found}


def numbers_consistent(text: str, source: str) -> bool:
    """Every number in model-written ``text`` appears in the supplied ``source``."""
    allowed = _numbers(source)
    return all(value in allowed or value.replace(',', '') in allowed for value in _NUMBER.findall(text))


# "$x^2$" and "$n$" are formula markup; "$100 for a proof and $250 for a disproof" is money.
_DOLLAR_SPAN = re.compile(r'\$([^\s$](?:[^$]{0,200}?[^\s$])?)\$')


def plain_text_ok(text: str) -> bool:
    """No links, markup, TeX or character references, also after NFKC (full-width forms)."""
    if any(ord(character) < 32 for character in text) or html.unescape(text) != text:
        # "&lt;b&gt;" or "https&colon;&sol;&sol;" would be decoded into markup or a link when published.
        return False
    for form in {text, unicodedata.normalize('NFKC', text)}:
        if re.search(r'https?://|www\.|<[A-Za-z/!]|\]\(|`|\\[A-Za-z]+', form, re.I) or any(
                not re.search(r'\s', span) or re.search(r'[\\^_{}=]', span) for span in _DOLLAR_SPAN.findall(form)):
            return False
    return True


def _call(task, system, payload, validate, *, budget=None, request=None, max_tokens=4096):
    return kimi_tasks.call_json(task, system, payload, client_id=CLIENT_ID, cache_key=CACHE_KEY, validate=validate,
                                max_tokens=max_tokens, effort='low', timeout=TIMEOUT_SECONDS, budget=budget,
                                request=request)


def _summary_rows(value: object, allowed_ids: set[str], texts: dict[str, str] | None = None) -> list[dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
        raise NewsSummaryError('Invalid summary schema')
    if len(value['items']) > len(allowed_ids):
        raise NewsSummaryError('Too many summary records')
    edits, seen = [], set()
    for row in value['items']:
        if not isinstance(row, dict) or set(row) != {'id', 'summary'}:
            raise NewsSummaryError('Summaries cannot change source metadata')
        key, summary = row['id'], row['summary']
        if not isinstance(key, str) or key not in allowed_ids or key in seen:
            raise NewsSummaryError('Unknown or duplicate source identifier')
        if not isinstance(summary, str):
            raise NewsSummaryError('Invalid summary text')
        seen.add(key)
        text = ' '.join(summary.split())
        # Row-level checks: a rejected summary leaves that record with its source text.
        if (not 20 <= len(text) <= MAX_SUMMARY or not plain_text_ok(text) or INPUT_TALK.search(text) or
                (texts is not None and not numbers_consistent(text, texts.get(key, '')))):
            continue
        edits.append({'id': key, 'summary': text})
    return edits


def parse_summaries(content: str, allowed_ids: set[str], texts: dict[str, str] | None = None) -> list[dict[str, str]]:
    if not isinstance(content, str) or len(content) > 32000:
        raise NewsSummaryError('Invalid summary response')
    try:
        value = kimi_tasks.extract_json_object(content)
    except KimiTaskError:
        raise NewsSummaryError('Invalid summary JSON') from None
    return _summary_rows(value, allowed_ids, texts)


def _public(error: KimiTaskError) -> NewsSummaryError:
    return error if isinstance(error, NewsSummaryError) else NewsSummaryError(str(error), error.kind)


def summarize(candidates: list[dict], *, budget=None, request=None) -> list[dict[str, str]]:
    if not candidates:
        return []
    rows = []
    for item in candidates[:MAX_ITEMS]:
        key = item.get('id')
        if not isinstance(key, str) or not 1 <= len(key) <= 160:
            raise NewsSummaryError('Invalid source identifier')
        rows.append({'id': key, 'title': str(item.get('title', ''))[:350],
                     'source': str(item.get('source', ''))[:120],
                     'published_at': str(item.get('published_at', ''))[:40],
                     'status': str(item.get('status', 'Published'))[:80],
                     'excerpt': str(item.get('excerpt') or item.get('summary') or '')[:1800]})
    texts = {row['id']: row['title'] + '\n' + row['excerpt'] for row in rows}
    try:
        return _call('summarize', SUMMARY_SYSTEM, {'articles': rows},
                     lambda value: _summary_rows(value, set(texts), texts), budget=budget, request=request)
    except KimiTaskError as error:
        # Never send provider errors, credentials or response text to the public
        # feed or the journal. The updater can retain the source-backed excerpt.
        raise _public(error) from None


def _triage_rows(value: object, allowed_ids: set[str]) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {'items'} or not isinstance(value['items'], list):
        raise NewsSummaryError('Invalid triage schema')
    if len(value['items']) > len(allowed_ids):
        raise NewsSummaryError('Too many triage records')
    result, seen = [], set()
    for row in value['items']:
        if not isinstance(row, dict) or set(row) != {'id', 'decision', 'importance', 'topic', 'reason'}:
            raise NewsSummaryError('Triage cannot add or drop fields')
        if row['id'] not in allowed_ids or row['id'] in seen:
            raise NewsSummaryError('Unknown or duplicate source identifier')
        if (row['decision'] not in ('include', 'exclude') or row['importance'] not in (0, 1, 2, 3) or
                isinstance(row['importance'], bool) or row['topic'] not in TOPICS or not isinstance(row['reason'], str)):
            raise NewsSummaryError('Triage value out of range')
        seen.add(row['id'])
        reason = ' '.join(row['reason'].split())
        if not 3 <= len(reason) <= 160 or not plain_text_ok(reason):
            continue
        result.append({'id': row['id'], 'decision': row['decision'], 'importance': row['importance'], 'topic': row['topic']})
    return result


def triage(rows: list[dict], *, budget=None, request=None) -> list[dict]:
    records = []
    for row in rows[:MAX_TRIAGE]:
        if not isinstance(row.get('id'), str) or not 1 <= len(row['id']) <= 160:
            raise NewsSummaryError('Invalid source identifier')
        records.append({'id': row['id'], 'source': str(row.get('source', ''))[:120], 'role': str(row.get('role', ''))[:20],
                        'title': str(row.get('title', ''))[:350], 'published_at': str(row.get('published_at', ''))[:40],
                        'categories': [str(value)[:100] for value in (row.get('categories') or [])[:10]],
                        'excerpt': str(row.get('excerpt') or '')[:1200]})
    if not records:
        return []
    allowed = {record['id'] for record in records}
    try:
        return _call('triage', TRIAGE_SYSTEM, {'records': records}, lambda value: _triage_rows(value, allowed),
                     budget=budget, request=request)
    except KimiTaskError as error:
        raise _public(error) from None


def _link_rows(value: object, records: dict[str, dict]) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {'links'} or not isinstance(value['links'], list):
        raise NewsSummaryError('Invalid link schema')
    if len(value['links']) > 3 * len(records):
        raise NewsSummaryError('Too many links')
    result, seen, per_record = [], set(), {}
    for row in value['links']:
        if not isinstance(row, dict) or set(row) != {'id', 'problem_id', 'relation', 'evidence'}:
            raise NewsSummaryError('Links cannot add or drop fields')
        record = records.get(row['id']) if isinstance(row['id'], str) else None
        if record is None or row['problem_id'] not in record['allowed'] or row['relation'] not in RELATIONS:
            raise NewsSummaryError('Unknown record, problem or relation')
        key = (row['id'], row['problem_id'], row['relation'])
        if key in seen or per_record.get(row['id'], 0) >= 3:
            continue
        try:
            evidence = kimi_tasks.exact_quote(row['evidence'], record['text'])
        except ValueError:
            continue
        seen.add(key)
        per_record[row['id']] = per_record.get(row['id'], 0) + 1
        result.append({'id': row['id'], 'problem_id': row['problem_id'], 'relation': row['relation'], 'evidence': evidence})
    return result


def link_problems(rows: list[dict], *, budget=None, request=None) -> list[dict]:
    records, payload = {}, []
    for row in rows[:MAX_LINK_RECORDS]:
        candidates = [candidate for candidate in (row.get('candidates') or [])[:8]
                      if isinstance(candidate, dict) and isinstance(candidate.get('problem_id'), str)]
        if not isinstance(row.get('id'), str) or not candidates:
            continue
        text = str(row.get('text') or '')[:4400]
        records[row['id']] = {'text': text, 'allowed': {candidate['problem_id'] for candidate in candidates}}
        payload.append({'id': row['id'], 'text': text,
                        'candidates': [{'problem_id': candidate['problem_id'], 'title': str(candidate.get('title') or '')[:160]}
                                       for candidate in candidates]})
    if not payload:
        return []
    try:
        return _call('link_problems', LINK_SYSTEM, {'records': payload}, lambda value: _link_rows(value, records),
                     budget=budget, request=request)
    except KimiTaskError as error:
        raise _public(error) from None


def _story_rows(value: object, items: dict[str, dict], story_ids: set[str]) -> dict:
    if (not isinstance(value, dict) or set(value) != {'assign', 'new'} or not isinstance(value['assign'], list)
            or not isinstance(value['new'], list) or len(value['new']) > 10):
        raise NewsSummaryError('Invalid story schema')
    used, assign, new = set(), [], []
    for row in value['assign']:
        if (not isinstance(row, dict) or set(row) != {'id', 'story_id'} or row['id'] not in items or
                row['story_id'] not in story_ids or row['id'] in used):
            raise NewsSummaryError('Unknown or duplicate story assignment')
        used.add(row['id'])
        assign.append({'id': row['id'], 'story_id': row['story_id']})
    for row in value['new']:
        if (not isinstance(row, dict) or set(row) != {'key', 'label', 'member_ids'} or
                not isinstance(row['key'], str) or not re.fullmatch(r'n[0-9]{1,2}', row['key']) or
                not isinstance(row['member_ids'], list) or not isinstance(row['label'], str)):
            raise NewsSummaryError('Invalid proposed story')
        members = list(dict.fromkeys(row['member_ids']))
        if any(member not in items or member in used for member in members):
            raise NewsSummaryError('Unknown or duplicate story member')
        label = ' '.join(row['label'].split())
        evidence = ' '.join(items[member]['title'] + ' ' + items[member].get('excerpt', '') for member in members)
        if (len(members) < 2 or not any(items[member].get('role') in STORY_ROLES for member in members) or
                not 3 <= len(label) <= 80 or not plain_text_ok(label) or not numbers_consistent(label, evidence)):
            continue
        used.update(members)
        new.append({'label': label, 'member_ids': members})
    return {'assign': assign, 'new': new}


def group_stories(payload: dict, *, budget=None, request=None) -> dict:
    items = {row['id']: row for row in (payload.get('items') or [])[:MAX_STORY_ITEMS]
             if isinstance(row, dict) and isinstance(row.get('id'), str)}
    stories = [row for row in (payload.get('stories') or [])[:10] if isinstance(row, dict) and isinstance(row.get('story_id'), str)]
    if len(items) < 2:
        return {'assign': [], 'new': []}
    data = {'stories': [{'story_id': row['story_id'], 'label': str(row.get('label') or '')[:120],
                         'members': [{'title': str(member.get('title') or '')[:200], 'source': str(member.get('source') or '')[:120]}
                                     for member in (row.get('members') or [])[:8] if isinstance(member, dict)]}
                        for row in stories],
            'records': [{'id': key, 'title': str(row.get('title') or '')[:350], 'source': str(row.get('source') or '')[:120],
                         'role': str(row.get('role') or '')[:20], 'published_at': str(row.get('published_at') or '')[:40],
                         'excerpt': str(row.get('excerpt') or '')[:300]} for key, row in items.items()]}
    story_ids = {row['story_id'] for row in stories}
    try:
        return _call('stories', STORY_SYSTEM, data, lambda value: _story_rows(value, items, story_ids),
                     budget=budget, request=request)
    except KimiTaskError as error:
        raise _public(error) from None


def summarize_changelog(item: dict, *, budget=None, request=None) -> str:
    title, text = str(item.get('title') or '')[:350], str(item.get('excerpt') or item.get('summary') or '')[:4000]
    if not text:
        raise NewsSummaryError('Changelog section is empty')

    def validate(value):
        if not isinstance(value, dict) or set(value) != {'summary'} or not isinstance(value['summary'], str):
            raise NewsSummaryError('Invalid changelog summary schema')
        summary = ' '.join(value['summary'].split())
        if not 40 <= len(summary) <= 600 or not plain_text_ok(summary) or not numbers_consistent(summary, title + '\n' + text):
            raise NewsSummaryError('Changelog summary failed its checks')
        return {'summary': summary}

    try:
        return _call('changelog_summary', CHANGELOG_SYSTEM, {'section': {'title': title, 'text': text}}, validate,
                     budget=budget, request=request)['summary']
    except KimiTaskError as error:
        raise _public(error) from None


def bound_tasks(budget: NewsBudget) -> dict:
    """The refresh() task callables, all drawing on one run and daily budget."""
    return {'summarizer': partial(summarize, budget=budget), 'triager': partial(triage, budget=budget),
            'linker': partial(link_problems, budget=budget), 'grouper': partial(group_stories, budget=budget),
            'changelog_summarizer': partial(summarize_changelog, budget=budget)}
