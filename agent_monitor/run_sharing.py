"""Explicit, revocable read-only snapshots; never expose a workspace tree."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from agent_monitor import DATA_DIR, RUNS_DIR, CACHE_DIR

DB_PATH = DATA_DIR / 'run_shares.sqlite3'
EXAMPLES = Path(__file__).parent / 'examples'
WEB = Path(__file__).parent / 'web'
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Robots-Tag': 'noindex, nofollow, noarchive'}


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS shares (run_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL, snapshot TEXT NOT NULL, created REAL NOT NULL)')
    return db


def _text(value, limit=100_000):
    return value[:limit] if isinstance(value, str) else ''


def _file(root: Path, name: str, limit=300_000):
    p = root / name
    try:
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink():
            return ''
        with p.open('rb') as stream:
            return stream.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''


def record(run_id):
    if not RUN_ID.fullmatch(run_id) or '..' in run_id:
        return None
    cache = Path(os.environ.get('LLM_DASHBOARD_CACHE', str(CACHE_DIR))) / 'harness'
    for folder in (cache, RUNS_DIR):
        try:
            value = json.loads(_file(folder, run_id + '.json', 10_000_000))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
    return None


def graph_snapshot(value):
    if not isinstance(value, dict):
        return {'nodes': [], 'edges': []}
    raw = value.get('graph', value)
    if not isinstance(raw, dict):
        return {'nodes': [], 'edges': []}
    nodes = [{k: _text(n.get(k), 20_000 if k == 'statement' else 240)
              for k in ('id', 'kind', 'lean_kind', 'label', 'statement', 'status')}
             for n in (raw.get('nodes') or [])[:100] if isinstance(n, dict)]
    ids = {n['id'] for n in nodes}
    edges = [{k: _text(e.get(k), 240) for k in ('from', 'to', 'label')}
             for e in (raw.get('edges') or [])[:300]
             if isinstance(e, dict) and e.get('from') in ids and e.get('to') in ids]
    return {'title': _text(raw.get('title'), 240), 'nodes': nodes, 'edges': edges}


# How a shared Analyze snapshot names each verdict.  Model judgements are
# never called verified: "checked" means checked by a model verifier.
VERDICT_STATUS = {
    'checked': 'checked by a model verifier',
    'gap': 'gap',
    'error': 'error',
    'unclear': 'unclear',
    'not_checked': 'not checked',
}
ANALYSIS_NOTICE = (
    'Read-only snapshot of an analysis of an existing manuscript, selected by the run owner. '
    'Verdicts are checked by a model verifier — not a certificate, a refereed review or a Lean proof; '
    'they can be wrong in both directions. The manuscript itself is not included: follow the source link.'
)


def is_analysis(data):
    return isinstance(data, dict) and (data.get('run_kind') == 'analyze' or data.get('engine') == 'analyze')


def analysis_graph_snapshot(graph):
    """The shared Manuscript DAG: statements already in the graph, labels and
    verdicts; never proof excerpts, notes or anything from analysis/source."""
    if not isinstance(graph, dict):
        return {'nodes': [], 'edges': []}
    nodes = []
    for node in (graph.get('nodes') or [])[:100]:
        if not isinstance(node, dict):
            continue
        meta = node.get('meta') if isinstance(node.get('meta'), dict) else {}
        verdict = meta.get('verdict') if meta.get('verdict') in VERDICT_STATUS else 'not_checked'
        closure = meta.get('closure') if meta.get('closure') in VERDICT_STATUS else None
        # A reply the verifier's output could not be read from is no
        # judgement: it is "not checked" (as in the report), never "unclear".
        uninterpreted = meta.get('interpreted') is False
        if uninterpreted:
            verdict, status = 'not_checked', 'not checked (no usable verifier output)'
        else:
            status = VERDICT_STATUS[verdict]
        entry = {
            'id': _text(node.get('id'), 240), 'kind': _text(node.get('kind'), 240),
            'label': _text(node.get('label'), 240),
            'statement': _text(' '.join(str(node.get('statement') or '').split()), 1200),
            'status': status, 'verdict': verdict,
            'closure': VERDICT_STATUS[closure] if closure else '',
            'main_result': bool(meta.get('main_result')),
        }
        if uninterpreted:
            entry['uninterpreted'] = True
        nodes.append(entry)
    ids = {n['id'] for n in nodes}
    edges = [{'from': _text(e.get('from'), 240), 'to': _text(e.get('to'), 240), 'label': 'uses',
              'provenance': _text(e.get('provenance'), 20)}
             for e in (graph.get('edges') or [])[:300]
             if isinstance(e, dict) and e.get('from') in ids and e.get('to') in ids]
    title = ((graph.get('source') or {}).get('title') if isinstance(graph.get('source'), dict) else '') or ''
    return {'title': _text(title, 240), 'nodes': nodes, 'edges': edges}


_VERDICT_HEADER = (('checked', 'checked by a model verifier'), ('gap', 'gap'), ('error', 'error'),
                   ('unclear', 'unclear'), ('not_checked', 'not checked'))


def _uninterpretable(summary):
    coverage = summary.get('coverage') if isinstance(summary.get('coverage'), dict) else {}
    try:
        return max(0, int(coverage.get('uninterpretable') or 0))
    except (TypeError, ValueError):
        return 0


def _shared_summary(summary):
    """The summary block of a shared analysis, with unusable verifier output
    counted as not checked (the analysis counts it as "unclear")."""
    if not summary:
        return {}
    out = {key: summary.get(key) for key in ('nodes', 'checked', 'gap', 'error', 'unclear', 'not_checked',
                                             'main_closure', 'model', 'route') if key in summary}
    unusable = _uninterpretable(summary)
    if unusable and 'unclear' in out and 'not_checked' in out:
        out['unclear'] = max(0, int(out['unclear'] or 0) - unusable)
        out['not_checked'] = int(out['not_checked'] or 0) + unusable
        out['no_usable_output'] = unusable
    return out


def _verdict_counts(shared_graph, summary):
    """The "Verdicts:" line, counted as the DAG and the report count them
    (unusable verifier output is not checked): the stored summary covers
    every node; the shared nodes (at most 100) stand in when it is missing."""
    if summary:
        tally = _shared_summary(summary)
        unusable = int(tally.get('no_usable_output') or 0)
    elif shared_graph and shared_graph.get('nodes'):
        tally = {key: 0 for key, _label in _VERDICT_HEADER}
        unusable = 0
        for node in shared_graph['nodes']:
            tally[node['verdict']] += 1
            unusable += 1 if node.get('uninterpreted') else 0
    else:
        return ''
    parts = [f"{int(tally.get(key) or 0)} {label}" for key, label in _VERDICT_HEADER]
    if unusable:
        parts[-1] += f' ({unusable} with no usable verifier output)'
    return ', '.join(parts)


def _analysis_snapshot(run_id, data, read, graph):
    from agent_monitor.analyze import graph as analysis_graph
    from agent_monitor.analyze.titles import plain_title

    ws = RUNS_DIR / 'workspaces' / run_id
    block = data.get('analyze') if isinstance(data.get('analyze'), dict) else {}
    source = block.get('source') if isinstance(block.get('source'), dict) else {}
    summary = block.get('summary') if isinstance(block.get('summary'), dict) else {}
    link = next((str(source.get(key)) for key in ('resolved_url', 'input_url')
                 if str(source.get(key) or '').startswith('https://')), '')
    title = _text(plain_title(source.get('title')), 240) or 'manuscript'
    try:
        manuscript_graph = analysis_graph.load(ws)
    except Exception:  # noqa: BLE001 - a damaged graph shares nothing
        manuscript_graph = None
    shared_graph = analysis_graph_snapshot(manuscript_graph) if manuscript_graph else None
    counts = _verdict_counts(shared_graph, summary)
    problem = f'Analysis of {title}' + (f' ({_text(source.get("version"), 80)})' if source.get('version') else '')
    if source.get('license'):
        problem += f'\nLicence: {_text(source.get("license"), 200)}'
    if counts:
        problem += f'\nVerdicts: {counts}'
    lean = read('lean/Proof.lean')
    result = {
        'version': 1, 'type': 'shared', 'kind': 'analysis',
        'title': _text(f'Analyze · {title}', 240),
        'status': _text(data.get('status'), 80), 'engine': 'analyze',
        'problem': problem,
        # The report quotes labels and model findings, never the manuscript.
        'proof': read('analysis/report.md'),
        'agents': [],
        'informal_graph': shared_graph if shared_graph else graph('proof_graph.json'),
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': lean,
        'analysis': {
            'source': {key: _text(source.get(key), 300) for key in ('title', 'version', 'license', 'format', 'canonical_id')
                       if source.get(key)},
            'warnings': [_text(item, 400) for item in (source.get('warnings') or [])[:12] if isinstance(item, str)],
            'summary': _shared_summary(summary),
            'verdict_note': 'checked by a model verifier — not a certificate',
            'lean_note': 'Lean statements only (proofs not attempted) on Lean 4.14 / Mathlib; upstream Lean, if any, is not re-checked by this server.',
        },
        'notice': ANALYSIS_NOTICE,
    }
    if link:
        result['source_url'] = link
    return result


def snapshot(run_id, data):
    ws = RUNS_DIR / 'workspaces' / run_id
    # The workspace itself must not redirect into another user's files.
    safe_ws = not ws.is_symlink() and ws.resolve().is_relative_to((RUNS_DIR / 'workspaces').resolve())
    def read(name):
        return _file(ws, name) if safe_ws else ''
    def graph(name):
        try:
            return graph_snapshot(json.loads(read(name)))
        except (ValueError, TypeError):
            return graph_snapshot({})
    if is_analysis(data):
        # Never analysis/source/** and no manuscript text beyond the node
        # statements already in the graph; no agent transcripts either.
        result = _analysis_snapshot(run_id, data, read, graph)
        if not result['formal_graph']['nodes'] and result['lean_source']:
            from agent_monitor.proof_graph import graph_from_lean
            result['formal_graph'] = graph_snapshot(graph_from_lean(result['lean_source']))
        if len(json.dumps(result).encode()) > 4_000_000:
            raise ValueError('This snapshot is too large to share (4 MB maximum).')
        return result
    agents = []
    for item in (data.get('agents') or [])[:100]:
        if not isinstance(item, dict):
            continue
        agents.append({
            'id': _text(item.get('agent_id') or item.get('id') or item.get('trace_id'), 200),
            'label': _text(item.get('agent_name') or item.get('name') or item.get('stage_name') or item.get('stage') or item.get('agent'), 200),
            'status': _text(item.get('status'), 80),
            'model': _text(item.get('model'), 160),
            'summary': _text(item.get('summary') or item.get('output_text') or item.get('response_text') or item.get('final_text') or item.get('output'), 20_000),
        })
    result = {
        'version': 1, 'type': 'shared', 'title': _text(data.get('title') or data.get('problem_text_preview') or run_id, 240),
        'status': _text(data.get('status'), 80), 'engine': _text(data.get('engine'), 100),
        'problem': _text(data.get('problem_text') or data.get('problem_text_preview')),
        'proof': read('proof.md') or read('proof.tex') or _text(data.get('proof_text')),
        'agents': agents, 'informal_graph': graph('proof_graph.json'),
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': read('lean/Proof.lean'),
        'notice': 'Read-only snapshot selected by the run owner. It does not update as the run continues. Graphs describe dependencies; they are not, by themselves, proof certificates.',
    }
    if not result['formal_graph']['nodes'] and result['lean_source']:
        from agent_monitor.proof_graph import graph_from_lean
        result['formal_graph'] = graph_snapshot(graph_from_lean(result['lean_source']))
    if len(json.dumps(result).encode()) > 4_000_000:
        raise ValueError('This snapshot is too large to share (4 MB maximum).')
    return result


def create(run_id, owner_id, data):
    now = time.time()
    data = {**data, 'shared_at': now}
    with _connect() as db:
        old = db.execute('SELECT * FROM shares WHERE run_id=?', (run_id,)).fetchone()
        token = old['token'] if old and old['owner_id'] == owner_id else secrets.token_urlsafe(32)
        db.execute('INSERT OR REPLACE INTO shares VALUES (?, ?, ?, ?, ?)',
                   (run_id, token, owner_id, json.dumps(data, ensure_ascii=False), now))
    return {'token': token, 'shared_at': now}


def owner_share(run_id, owner_id):
    with _connect() as db:
        row = db.execute('SELECT token, created FROM shares WHERE run_id=? AND owner_id=?', (run_id, owner_id)).fetchone()
    return {'token': row['token'], 'shared_at': row['created']} if row else None


def revoke(run_id, owner_id):
    with _connect() as db:
        db.execute('DELETE FROM shares WHERE run_id=? AND owner_id=?', (run_id, owner_id))


def lookup(token):
    if not TOKEN.fullmatch(token):
        return None
    with _connect() as db:
        row = db.execute('SELECT run_id, snapshot FROM shares WHERE token=?', (token,)).fetchone()
    # Deleting the source run immediately disables the snapshot too.
    return json.loads(row['snapshot']) if row and record(row['run_id']) else None


class RunSharingMixin:
    def _share_send(self, code, value):
        self._send(code, json.dumps(value, ensure_ascii=False), headers=HEADERS)

    def _share_owner(self, run_id):
        user = self._current_user()
        if not user:
            self._share_send(401, {'error': 'Sign in to manage shared links.'})
            return None
        if user.get('guest'):
            self._share_send(403, {'error': 'Sign in to an account to share runs.'})
            return None
        data = record(run_id)
        from agent_monitor import jobs
        if not data or jobs.run_owner(run_id) != user.get("id"):
            self._share_send(404, {'error': 'Run not found.'})
            return None
        return user, data

    def _sharing_get(self, path):
        example = path in ('/examples/erdos-straus', '/api/examples/erdos-straus')
        match = re.fullmatch(r'/(api/)?shared/([A-Za-z0-9_-]{43})', path)
        if example or match:
            data = json.loads((EXAMPLES / 'erdos-straus.json').read_text()) if example else lookup(match[2])
            if data is None:
                self._share_send(404, {'error': 'This shared link is unavailable or has been revoked.'})
                return True
            if path.startswith('/api/'):
                self._share_send(200, data)
            else:
                base = self._request_base_path()
                url = base + ('/api/examples/erdos-straus' if example else '/api/shared/' + match[2])
                html = (WEB / 'run-viewer.html').read_text().replace('__DATA_URL__', url).replace('__ASSET_BASE__', base + '/static').replace('__CONSOLE_URL__', base + '/')
                self._send(200, html, 'text/html; charset=utf-8', headers={**HEADERS,
                    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"})
            return True
        match = re.fullmatch(r'/api/runs/([^/]+)/share', path)
        if not match:
            return False
        owned = self._share_owner(match[1])
        if owned:
            share = owner_share(match[1], owned[0]['id'])
            self._share_send(200, self._share_info(share))
        return True

    def _share_info(self, share):
        return {'shared': bool(share), **({'path': self._request_base_path() + '/shared/' + share['token'], 'shared_at': share['shared_at']} if share else {})}

    def _sharing_write(self, path, method, body):
        match = re.fullmatch(r'/api/runs/([^/]+)/share', path)
        if not match:
            return False
        # JSON plus same-origin checks prevent cross-site snapshot publication.
        origin = self.headers.get('Origin')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urlsplit(origin).netloc != self.headers.get('Host')):
            self._share_send(403, {'error': 'Use the console to manage sharing.'})
            return True
        if method == 'POST' and self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self._share_send(415, {'error': 'Expected application/json.'})
            return True
        owned = self._share_owner(match[1])
        if not owned:
            return True
        user, data = owned
        if method == 'DELETE':
            revoke(match[1], user['id'])
            self._share_send(200, {'shared': False})
        elif body.get('confirm_share') is not True:
            self._share_send(400, {'error': 'Confirm that this snapshot may be viewed by anyone with the link.'})
        else:
            try:
                share = create(match[1], user['id'], snapshot(match[1], data))
            except ValueError as exc:
                self._share_send(400, {'error': str(exc)})
            else:
                self._share_send(200, self._share_info(share))
        return True
