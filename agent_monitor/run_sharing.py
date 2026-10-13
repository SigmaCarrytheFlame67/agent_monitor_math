"""Explicit, revocable read-only snapshots; never expose a workspace tree."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import secrets
import sqlite3
import stat
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
    # Follow no links, including intermediate directories. An allowlisted
    # leaf such as lean/Proof.lean must not become a route into other files.
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or any(p in {'.', '..'} for p in parts):
        return ''
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
        parent = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(parent)
        for part in parts[:-1]:
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            descriptors.append(parent)
        descriptor = os.open(parts[-1], flags | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ''
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            return stream.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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


def _items(value, limit):
    return value[:limit] if isinstance(value, list) else []


def graph_snapshot(value, *, details=False):
    if not isinstance(value, dict):
        return {'nodes': [], 'edges': []}
    raw = value.get('graph', value)
    if not isinstance(raw, dict):
        return {'nodes': [], 'edges': []}
    nodes, seen = [], set()
    for node in _items(raw.get('nodes'), 100):
        if not isinstance(node, dict):
            continue
        nid = _text(node.get('id'), 240)
        if not nid or nid in seen:
            continue
        seen.add(nid)
        entry = {key: _text(node.get(key), 20_000 if key == 'statement' else 240)
                 for key in ('id', 'kind', 'lean_kind', 'label', 'statement', 'status')}
        entry['citations'] = [_text(item, 240) for item in _items(node.get('citations'), 8)
                              if isinstance(item, str)]
        meta = node.get('meta') if isinstance(node.get('meta'), dict) else {}
        # Mathematical node details only, never arbitrary workflow metadata,
        # authors, prompts, filesystem locations or provider request records.
        clean = {key: _text(meta.get(key), limit) for key, limit in (
            ('workflow_kind', 40), ('status', 80), ('entry_status', 80),
            ('verdict', 80), ('lemma', 80), ('technique', 80),
            ('decomposition_technique', 80), ('and_group', 120),
            ('fact_id', 240), ('label', 120), ('proof_excerpt', 6000), ('intuition', 2000),
        ) if isinstance(meta.get(key), str)}
        for key in ('verifiable', 'verifier_accepted', 'is_target', 'proof_truncated', 'receipt_attested'):
            if isinstance(meta.get(key), bool):
                clean[key] = meta[key]
        for key in ('score', 'depth'):
            item = meta.get(key)
            if (isinstance(item, (int, float)) and not isinstance(item, bool)
                    and 0 <= item <= (1 if key == 'score' else 1000) and math.isfinite(item)):
                clean[key] = item
        if clean and details:
            entry['meta'] = clean
        nodes.append(entry)
    ids = {n['id'] for n in nodes}
    edges = [{k: _text(e.get(k), 240) for k in ('from', 'to', 'label', 'kind', 'provenance')
              if isinstance(e.get(k), str)}
             for e in _items(raw.get('edges'), 300)
             if isinstance(e, dict) and e.get('from') in ids and e.get('to') in ids]
    return {'title': _text(raw.get('title'), 240), 'nodes': nodes, 'edges': edges}


GRAPH_MODES = {
    'outline': ('proof-outline', 'No proof outline was available when this snapshot was created.'),
    'explore': ('workflow', 'No exploration graph was available when this snapshot was created.'),
    'facts': ('fact-dependency', 'No fact dependency graph was available when this snapshot was created.'),
    'formal': ('formal-structure', 'No formal graph was available when this snapshot was created.'),
    'compiler': ('compiler-dependency', 'No validated compiler facts were available when this snapshot was created.'),
    'manuscript': ('manuscript-dependency', 'This snapshot has no manuscript dependency graph.'),
}


def _graph_record(mode, value=None, *, sanitized=None):
    """A separate presentation record; graph types never stand in for each other."""
    role, reason = GRAPH_MODES[mode]
    value = value if isinstance(value, dict) else {}
    if mode == 'explore' and value.get('graph_role') == 'execution':
        role = 'execution'
    graph = sanitized if isinstance(sanitized, dict) else graph_snapshot(value, details=mode in {'facts', 'explore'})
    out = {'kind': mode, 'status': 'unavailable', 'graph_role': role, 'graph': graph}
    raw = value.get('graph', value)
    raw = raw if isinstance(raw, dict) else {}
    counts = value.get('counts') if isinstance(value.get('counts'), dict) else {}
    totals = {key: len(raw[key]) if isinstance(raw.get(key), list) else len(graph.get(key, []))
              for key in ('nodes', 'edges')}
    for key in totals:
        if isinstance(counts.get(key), int) and not isinstance(counts[key], bool) and counts[key] >= totals[key]:
            totals[key] = counts[key]
    out['counts'] = {**totals, 'shown_nodes': len(graph.get('nodes', [])), 'shown_edges': len(graph.get('edges', []))}
    out['truncated'] = any(totals[key] > len(graph.get(key, [])) for key in totals)
    if not graph.get('nodes'):
        out['reason'] = reason
        return out
    out['status'] = _text(value.get('status'), 80) or 'recorded'
    for key in ('model', 'dependency_basis'):
        if isinstance(value.get(key), str):
            out[key] = _text(value[key], 160)
    selection = value.get('selection')
    if isinstance(selection, dict):
        ids = {node['id'] for node in graph['nodes']}
        out['selection'] = {'target_ids': [item for item in _items(selection.get('target_ids'), 100)
                                           if isinstance(item, str) and item in ids]}
    provenance = value.get('provenance')
    if not isinstance(provenance, dict) and isinstance(value.get('graph'), dict):
        provenance = value['graph'].get('provenance')
    if isinstance(provenance, dict):
        clean = {key: _text(provenance[key], 160) for key in (
            'kind', 'producer', 'adapter', 'semantics', 'attestation', 'receipt_status',
            'verifier_verdict', 'fact_receipt_status', 'toolchain',
        ) if isinstance(provenance.get(key), str)}
        for key in ('certificate', 'proof_logic', 'lean_verified', 'acyclic', 'truncated'):
            if isinstance(provenance.get(key), bool):
                clean[key] = provenance[key]
        if clean:
            out['provenance'] = clean
    return out


def _json_read(read, name):
    try:
        value = json.loads(read(name))
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _graph_modes(workspace, data, read, result, *, safe_workspace):
    """Snapshot stored artifacts using offline adapters; never run a model,
    Lean, a graph cache writer, or any provider/verification operation."""
    from agent_monitor import engine_graph, lean_kg, proof_graph

    graphs = {mode: _graph_record(mode) for mode in GRAPH_MODES}
    if is_analysis(data):
        # The specially sanitized Analyze graph is the only manuscript view.
        # Do not fall through into generic node metadata or manuscript text.
        shared = result['informal_graph']
        basis = {'dependency_basis': result['analysis'].get('dependency_basis', ''),
                 'counts': result['analysis'].get('graph_counts', {})}
        graphs['manuscript'] = _graph_record('manuscript', basis, sanitized=shared)
        graphs['outline'] = _graph_record('outline', basis, sanitized=shared)
    else:
        if safe_workspace:
            # The execution adapter only receives fields already selected for
            # this share plus bounded edge endpoints, never the raw run record.
            selected_run = {'engine': result['engine'], 'status': result['status'],
                            'agents': [{**agent, 'name': agent['label']} for agent in result['agents']],
                            'edges': [{key: _text(edge.get(key), 240) for key in ('from', 'to', 'source', 'target', 'label', 'type')}
                                      for edge in _items(data.get('edges'), 300) if isinstance(edge, dict)]}
            for mode, view, expected_role in (
                ('facts', 'facts', 'fact-dependency'), ('explore', 'workflow', 'workflow'),
            ):
                try:
                    artifact = engine_graph.build(workspace, selected_run, view=view)
                except (OSError, ValueError, engine_graph.EngineGraphError):
                    continue
                # Non-CGM engines expose their recorded execution topology in
                # Explore with its own role; it must never be labelled Facts.
                if artifact.get('graph_role') == expected_role or (mode == 'explore' and artifact.get('graph_role') == 'execution'):
                    graphs[mode] = _graph_record(mode, artifact)
        outline = _json_read(read, 'proof_graph.json')
        if safe_workspace and outline.get('model') == proof_graph.FACT_GRAPH_MODEL:
            # A fact-derived cache is a projection, not an independent saved
            # outline. Recompute it from the current validated facts without
            # rewriting the cache so Outline and Facts describe one source.
            outline = proof_graph.fact_graph_result(workspace) or {}
        elif not graph_snapshot(outline)['nodes'] and graphs['facts']['graph']['nodes']:
            outline = proof_graph.fact_graph_result(workspace) or {}
        if not graph_snapshot(outline)['nodes'] and result['proof']:
            try:
                outline = {'status': 'parsed', 'model': 'structural-text-parse',
                           'graph': proof_graph.graph_from_text(result['proof'])}
            except ValueError:
                outline = {}
        graphs['outline'] = _graph_record('outline', outline)

    lean = result['lean_source']
    formal = _json_read(read, 'lean_proof_graph.json')
    if lean:
        source_hashes = {hashlib.sha256(lean.encode()).hexdigest(), lean_kg.normalized_source_sha256(lean)}
        cached_source_hash = formal.get('source_sha256')
        if not isinstance(cached_source_hash, str) or cached_source_hash not in source_hashes:
            formal = {}
        cached_raw = formal.get('graph') if isinstance(formal.get('graph'), dict) else {}
        cached_compiler = (formal.get('model') == 'lean-compiler' or bool(cached_raw.get('provenance'))
                           or any(edge.get('label') in {'proof uses', 'type mentions'}
                                  for edge in _items(cached_raw.get('edges'), 300) if isinstance(edge, dict)))
        if not graph_snapshot(formal)['nodes'] or cached_compiler:
            try:
                formal = {'status': 'parsed', 'model': 'lean-parse', 'graph': proof_graph.graph_from_lean(lean)}
            except ValueError:
                formal = {}
        # Loading is deliberately separate from load_or_parse_formal(), which
        # is permitted to extract new compiler metadata. Sharing is read-only.
        if safe_workspace and read('lean_kg_snapshot.json') and read('lean_verify.json'):
            try:
                stored = lean_kg.load_snapshot(workspace, source=lean)
                compiler = proof_graph.graph_from_lean(lean, kg_snapshot=stored) if stored else {}
                if compiler.get('provenance'):
                    graphs['compiler'] = _graph_record('compiler', {
                        'status': 'recorded', 'model': 'lean-compiler', 'graph': compiler,
                    })
            except (OSError, ValueError):
                pass
    # Compiler provenance belongs only to the independently validated mode.
    # A historical saved Formal shape without source remains merely recorded.
    formal = dict(formal)
    formal.pop('provenance', None)
    if formal.get('model') == 'lean-compiler':
        formal['model'] = 'stored-formal'
    raw_formal = formal.get('graph', formal)
    formal['graph'] = dict(raw_formal) if isinstance(raw_formal, dict) else {}
    formal['graph'].pop('provenance', None)
    graphs['formal'] = _graph_record('formal', formal)
    return graphs


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
        # Reuse the no-symlink reader instead of following an analysis/
        # directory link through the generic graph loader.
        manuscript_graph = analysis_graph.sanitize(_json_read(read, 'analysis/analysis_graph.json'))
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
        'version': 2, 'type': 'shared', 'kind': 'analysis',
        'title': _text(f'Analyze · {title}', 240),
        'status': _text(data.get('status'), 80), 'engine': 'analyze',
        'problem': problem,
        # The report quotes labels and model findings, never the manuscript.
        'proof': read('analysis/report.md'),
        'agents': [],
        'informal_graph': shared_graph if shared_graph else {'title': '', 'nodes': [], 'edges': []},
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': lean,
        'analysis': {
            'dependency_basis': (manuscript_graph or {}).get('dependency_basis', ''),
            'graph_counts': {key: len((manuscript_graph or {}).get(key, [])) for key in ('nodes', 'edges')},
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
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or '..' in run_id:
        raise ValueError('Invalid run identifier.')
    ws = RUNS_DIR / 'workspaces' / run_id
    # The workspace itself must not redirect into another user's files.
    safe_ws = (not ws.is_symlink() and not ws.parent.is_symlink()
               and ws.resolve().is_relative_to((RUNS_DIR / 'workspaces').resolve()))
    def read(name):
        # Read bounded graph documents whole before applying the smaller share
        # limits; truncating JSON bytes would hide an otherwise usable graph.
        limit = 3_000_000 if name in {'proof_graph.json', 'lean_proof_graph.json', 'analysis/analysis_graph.json'} else 300_000
        return _file(ws, name, limit) if safe_ws else ''
    def graph(name):
        try:
            return graph_snapshot(json.loads(read(name)))
        except (ValueError, TypeError):
            return graph_snapshot({})
    if is_analysis(data):
        # Never analysis/source/** and no manuscript text beyond the node
        # statements already in the graph; no agent transcripts either.
        result = _analysis_snapshot(run_id, data, read, graph)
        result['graphs'] = _graph_modes(ws, data, read, result, safe_workspace=safe_ws)
        result['formal_graph'] = result['graphs']['formal']['graph']
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
        'version': 2, 'type': 'shared', 'title': _text(data.get('title') or data.get('problem_text_preview') or run_id, 240),
        'status': _text(data.get('status'), 80), 'engine': _text(data.get('engine'), 100),
        'problem': _text(data.get('problem_text') or data.get('problem_text_preview')),
        'proof': read('proof.md') or read('proof.tex') or _text(data.get('proof_text')),
        'agents': agents, 'informal_graph': graph('proof_graph.json'),
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': read('lean/Proof.lean'),
        'notice': 'Read-only snapshot selected by the run owner. It does not update as the run continues. Graphs describe dependencies; they are not, by themselves, proof certificates.',
    }
    result['graphs'] = _graph_modes(ws, data, read, result, safe_workspace=safe_ws)
    result['informal_graph'] = result['graphs']['outline']['graph']
    result['formal_graph'] = result['graphs']['formal']['graph']
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
                    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; font-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"})
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
