"""Explicit, revocable read-only snapshots; never expose a workspace tree."""
from __future__ import annotations

import base64
import html as html_lib
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
from urllib.parse import parse_qs, urlsplit

from agent_monitor import DATA_DIR, RUNS_DIR, CACHE_DIR

DASHBOARD_WEB = Path(__file__).resolve().parent.parent / 'monitor_core' / 'harness_dashboard' / 'web'

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


RECORD_LIMIT = 64_000_000


def _record_folders():
    return (Path(os.environ.get('LLM_DASHBOARD_CACHE', str(CACHE_DIR))) / 'harness', RUNS_DIR)


def record(run_id):
    if not RUN_ID.fullmatch(run_id) or '..' in run_id:
        return None
    for folder in _record_folders():
        try:
            value = json.loads(_file(folder, run_id + '.json', RECORD_LIMIT))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError, RecursionError):
            pass
    return None


def _record_exists(run_id):
    """Whether the run's record is still there (a regular file, no links),
    without reading it: a large record must not make its link look revoked."""
    if not RUN_ID.fullmatch(run_id) or '..' in run_id:
        return False
    for folder in _record_folders():
        try:
            descriptor = os.open(folder / (run_id + '.json'), os.O_RDONLY | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
        except OSError:
            continue
        try:
            if stat.S_ISREG(os.fstat(descriptor).st_mode) and not folder.is_symlink():
                return True
        finally:
            os.close(descriptor)
    return False


def _items(value, limit):
    return value[:limit] if isinstance(value, list) else []


# Absolute server paths (a workspace, a home directory, a toolchain) in saved
# logs keep only their last part: a share never shows the host's layout.
_SERVER_PATH = re.compile(r"(?:file://|(?<![\w.:/~-]))/(?:home|var|opt|tmp|etc|root|srv|usr|mnt|run|private|Users)/[^\s'\"`<>|)\]]*")


def _redact(value, limit=100_000):
    if not isinstance(value, str):
        return ''
    text = _SERVER_PATH.sub(lambda m: '…/' + m.group(0).rstrip('/').rsplit('/', 1)[-1], value[:limit * 2])
    return text[:limit]


def _number(value, upper=1e15):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= upper)


def _numbers(source, keys, upper=1e15):
    source = source if isinstance(source, dict) else {}
    return {key: source[key] for key in keys if _number(source.get(key), upper)}


def _agent_details(item):
    """What the console's activity and Monitor views show for one model call:
    its stage, role, model, timing and token counts. Never its prompt,
    thinking, tool arguments or cost."""
    out = {key: _text(item.get(key), 200) for key in ('stage_name', 'pipeline_stage', 'role', 'agent_name')
           if isinstance(item.get(key), str)}
    out.update(_numbers(item, ('call_seq', 'round', 'input_tokens', 'total_input_tokens', 'cached_input_tokens',
                               'output_tokens', 'reasoning_tokens', 'latency_s', 'duration_s', 'exit_code'), 1e12))
    tools = [_text(name, 80) for name in _items(item.get('tools'), 20) if isinstance(name, str)]
    if tools:
        out['tools'] = tools
    monitor = item.get('monitor') if isinstance(item.get('monitor'), dict) else {}
    outcome = {key: _text(monitor.get(key), 80) for key in ('outcome', 'outcome_label') if isinstance(monitor.get(key), str)}
    if outcome:
        out['monitor'] = outcome
    return out


_TOOL_ROLES = {'tool', 'tools', 'exec', 'shell'}
_COST = re.compile(r'(?<=· )\$\d+(?:\.\d+)?(?= ·|$)', re.M)


# Earlier snapshots kept only a label: "turn-2 (exec)", "turn-1 (message+tool)", "iter-3 (read_file)".
_TOOL_LABEL = re.compile(r'\((?:[^)]*\b(?:exec|edit|tool)\b[^)]*)\)|^iter-\d+ \(')


def _agent_output(item):
    """One step's output as a share shows it. A tool step keeps its calls (the
    command, or Hermes' '▸ name(args)' lines) but not what they returned:
    that can be anything the agent read, such as an attachment or another
    workspace file, which the share dialog promises to leave out. Run logs
    drop their cost figures."""
    text = item.get('summary') or item.get('output_text') or item.get('response_text') or item.get('final_text') or item.get('output')
    text = text if isinstance(text, str) else ''
    role = str(item.get('role') or '').lower()
    tool = (role in _TOOL_ROLES or str(item.get('pipeline_stage') or '').lower() == 'act' or bool(item.get('tools'))
            or text.lstrip().startswith(('$ ', '▸ ', 'edited: ')))
    if tool:
        lines = [line for line in text.strip().splitlines() if line.strip()]
        calls = [line for line in lines if line.startswith('▸ ')] or lines[:1]
        head = '\n'.join(_redact(line, 300) for line in calls)
        return head + ('\n(tool output is not included in shared snapshots)' if len(lines) > len(calls) else '')
    text = _redact(text, 400_000)
    if len(text) > 20_000:
        # Logs end with the result: keep their start and their end.
        text = text[:8_000] + '\n…\n' + text[-12_000:]
    return _COST.sub('…', text)


_PIPELINE_STAGES = ('informal_dag', 'lean', 'formal_dag', 'coverage')


def _pipeline_view(state):
    """The derived-view chips (Informal DAG, Lean, Formal DAG, Coverage)."""
    if not isinstance(state, dict) or not isinstance(state.get('stages'), dict):
        return None
    stages = {}
    for name in _PIPELINE_STAGES:
        stage = state['stages'].get(name)
        if not isinstance(stage, dict):
            continue
        entry = {key: _text(stage.get(key), 80) for key in ('status', 'updated_at') if isinstance(stage.get(key), str)}
        if isinstance(stage.get('detail'), str):
            entry['detail'] = _redact(stage['detail'], 400)
        result = stage.get('result') if isinstance(stage.get('result'), dict) else {}
        kept = {key: _text(result.get(key), 80) for key in ('status', 'scope') if isinstance(result.get(key), str)}
        if kept:
            entry['result'] = kept
        stages[name] = entry
    out = {key: _text(state.get(key), 120) for key in ('status', 'run_kind', 'proof_engine', 'selected_harness', 'updated_at')
           if isinstance(state.get(key), str)}
    out['stages'] = stages
    return out


def _coverage_view(value):
    """Which informal facts the Lean source covers (the proof's check marks)."""
    if not isinstance(value, dict):
        return None
    items = []
    for item in _items(value.get('items'), 200):
        if not isinstance(item, dict):
            continue
        entry = {key: _text(item.get(key), 2000 if key == 'statement' else 240)
                 for key in ('id', 'fact_id', 'label', 'status', 'lean_name', 'declaration', 'statement', 'kind')
                 if isinstance(item.get(key), str)}
        for key in ('statement_audited', 'verified', 'compiled'):
            if isinstance(item.get(key), bool):
                entry[key] = item[key]
        items.append(entry)
    out = {key: _text(value.get(key), 240) for key in ('status', 'trust_reason', 'generated_at', 'source_revision')
           if isinstance(value.get(key), str)}
    for key in ('formal_verified',):
        if isinstance(value.get(key), bool):
            out[key] = value[key]
    out['summary'] = _numbers(value.get('summary'), ('total', 'verified', 'compiled', 'statement_audited', 'missing', 'stale'), 1e6)
    out['items'] = items
    return out


def _run_view(data, read):
    """Run-level facts the console shows around the proof: token totals (not
    cost), the model chips, the derived-view stages and Lean coverage. Never
    the owner, account, credentials, billing route, prompts or chat."""
    view = {key: _text(data.get(key), 160) for key in ('model', 'requested_model', 'reasoning_effort', 'speed_mode',
                                                       'created_at', 'updated_at', 'run_kind')
            if isinstance(data.get(key), str)}
    view.update(_numbers(data, ('created_at', 'updated_at', 'wall_s')))
    view['totals'] = _numbers(data.get('totals'), ('input_tokens', 'cached_input_tokens', 'output_tokens',
                                                   'reasoning_tokens', 'calls', 'wall_s', 'agents', 'latency_s'))
    # The Monitor's pipeline stages and agent flow (structure and counts, no cost).
    view['pipeline'] = [{key: _text(stage.get(key), 160) for key in ('id', 'label', 'title', 'color') if isinstance(stage.get(key), str)}
                        for stage in _items(data.get('pipeline'), 20) if isinstance(stage, dict)]
    view['stages_present'] = [_text(item, 80) for item in _items(data.get('stages_present'), 20) if isinstance(item, str)]
    analysis = data.get('analysis') if isinstance(data.get('analysis'), dict) else {}
    flow = analysis.get('agent_flow') if isinstance(analysis.get('agent_flow'), dict) else {}
    compact = flow.get('compact') if isinstance(flow.get('compact'), dict) else {}
    strip = []
    for stage in _items(compact.get('pipeline_strip'), 20):
        if not isinstance(stage, dict):
            continue
        entry = {key: _text(stage.get(key), 160) for key in ('id', 'label', 'title', 'color') if isinstance(stage.get(key), str)}
        entry.update(_numbers(stage, ('agent_count',), 1e6))
        entry['roles'] = [{**{key: _text(role.get(key), 80) for key in ('role', 'label') if isinstance(role.get(key), str)},
                           **_numbers(role, ('count', 'waste_count'), 1e6)}
                          for role in _items(stage.get('roles'), 20) if isinstance(role, dict)]
        strip.append(entry)
    if strip:
        links = [{**{key: _text(row.get(key), 160) for key in ('from_label', 'to_label', 'type') if isinstance(row.get(key), str)},
                  **_numbers(row, ('count',), 1e6)}
                 for row in _items(compact.get('link_rows'), 60) if isinstance(row, dict)]
        nodes = [{key: _text(node.get(key), 200) for key in ('label', 'role', 'pipeline_stage') if isinstance(node.get(key), str)}
                 for node in _items(flow.get('nodes'), 100) if isinstance(node, dict)]
        view['agent_flow'] = {'compact': {'pipeline_strip': strip, 'link_rows': links}, 'nodes': nodes}
    live = data.get('live') if isinstance(data.get('live'), dict) else {}
    chips = {key: _text(live.get(key), 160) for key in ('model', 'model_requested', 'auth_mode', 'subagent_model')
             if isinstance(live.get(key), str)}
    if isinstance(live.get('model_requested'), bool):
        chips['model_requested'] = live['model_requested']
    if isinstance(live.get('subagents'), bool):
        chips['subagents'] = live['subagents']
    chips.update(_numbers(live, ('tool_calls', 'iteration'), 1e9))
    recent = [{'name': _text(entry.get('name'), 80)} for entry in _items(live.get('tools_recent'), 20)
              if isinstance(entry, dict) and isinstance(entry.get('name'), str)]
    if recent:
        chips['tools_recent'] = recent
    view['live'] = chips
    plan = data.get('route_plan') if isinstance(data.get('route_plan'), dict) else {}
    roles = {role: {'model': _text(plan[role].get('model'), 160)} for role in ('advisor', 'worker')
             if isinstance(plan.get(role), dict) and isinstance(plan[role].get('model'), str)}
    if roles:
        view['route_plan'] = roles
    if isinstance(data.get('error'), str) and data['error'].strip():
        view['error'] = _redact(data['error'], 2000)
    interruption = data.get('interruption') if isinstance(data.get('interruption'), dict) else {}
    if isinstance(interruption.get('reason'), str):
        view['interruption'] = {'reason': _redact(interruption['reason'], 400)}
    view['edges'] = [{key: _text(edge.get(key), 240) for key in ('from', 'to', 'source', 'target', 'label', 'type')
                      if isinstance(edge.get(key), str)}
                     for edge in _items(data.get('edges'), 300) if isinstance(edge, dict)]
    pipeline = _pipeline_view(_json_read(read, 'auto_pipeline.json'))
    if pipeline:
        view['auto_pipeline'] = pipeline
    coverage = _coverage_view(_json_read(read, 'proof_coordination.json'))
    if coverage:
        view['proof_coverage'] = coverage
    return view


_LEAN_ATTEMPT_LOG = 12_000


def _lean_check(check):
    check = check if isinstance(check, dict) else {}
    out = {key: _text(check.get(key), 80) for key in ('status', 'toolchain') if isinstance(check.get(key), str)}
    for key in ('ok', 'reports_sorry', 'timed_out'):
        if isinstance(check.get(key), bool):
            out[key] = check[key]
    out.update(_numbers(check, ('exit_code', 'duration_s', 'sorry_count'), 1e9))
    if isinstance(check.get('log'), str):
        out['log'] = _redact(check['log'], _LEAN_ATTEMPT_LOG)
    axioms = check.get('axioms') if isinstance(check.get('axioms'), dict) else None
    if axioms:
        # The #print axioms probe: ok, the declaration and the axioms it uses.
        kept = {}
        for key, value in axioms.items():
            if key not in {'ok', 'decl', 'axioms', 'unexpected', 'error', 'status', 'nonstandard', 'standard', 'message'}:
                continue
            if isinstance(value, bool):
                kept[key] = value
            elif isinstance(value, list):
                kept[key] = [_text(name, 200) for name in value[:40] if isinstance(name, str)]
            elif isinstance(value, str):
                kept[key] = _redact(value, 400)
        out['axioms'] = kept
    return out


def _lean_record(read):
    """The saved Lean checker record (as the Lean Verification tab shows it):
    source, checker output and attempts, fidelity and citations. Never the
    Lean chat, harness prompts, model credentials or server paths."""
    record = _json_read(read, 'lean_verify.json')
    if not record:
        return None
    out = {key: _text(record.get(key), 240) for key in ('status', 'action', 'title', 'model', 'scope', 'generated_at',
                                                        'toolchain_note', 'result_status')
           if isinstance(record.get(key), str)}
    out['lean'] = _text(record.get('lean'), 300_000)
    for key in ('log', 'notes'):
        if isinstance(record.get(key), str):
            out[key] = _redact(record[key], 40_000)
    for key in ('uses_mathlib', 'verified', 'has_result'):
        if isinstance(record.get(key), bool):
            out[key] = record[key]
    out.update(_numbers(record, ('sorry_count', 'attempt_count', 'duration_s'), 1e9))
    out['lean_path'] = 'lean/Proof.lean'
    toolchain = record.get('toolchain') if isinstance(record.get('toolchain'), dict) else {}
    kept = {key: _text(toolchain.get(key), 160) for key in ('lean_version', 'mathlib_rev', 'mathlib_version', 'version', 'label', 'status')
            if isinstance(toolchain.get(key), str)}
    if kept:
        out['toolchain'] = kept
    targets = [_text(item, 400) for item in _items(record.get('targets'), 50) if isinstance(item, str)]
    if targets:
        out['targets'] = targets
    attempts = []
    every = record.get('attempts') if isinstance(record.get('attempts'), list) else []
    # The console shows the latest attempts; keep the newest 24 and the count.
    out['attempt_count'] = len(every)
    for attempt in every[-24:]:
        if not isinstance(attempt, dict):
            continue
        entry = {key: _text(attempt.get(key), 160) for key in ('action', 'result_status', 'title', 'target', 'model')
                 if isinstance(attempt.get(key), str)}
        entry.update(_numbers(attempt, ('sorry_count', 'exit_code', 'duration_s', 'round'), 1e9))
        if isinstance(attempt.get('uses_mathlib'), bool):
            entry['uses_mathlib'] = attempt['uses_mathlib']
        if isinstance(attempt.get('log'), str):
            entry['log'] = _redact(attempt['log'], _LEAN_ATTEMPT_LOG)
        entry['check'] = _lean_check(attempt.get('check'))
        attempts.append(entry)
    out['attempts'] = attempts
    fidelity = record.get('fidelity') if isinstance(record.get('fidelity'), dict) else None
    if fidelity:
        out['fidelity'] = _plain(fidelity, depth=4)
    citations = [_plain(item, depth=3) for item in _items(record.get('citations'), 60) if isinstance(item, dict)]
    if citations:
        out['citations'] = citations
    return out


# Generic keys that never leave the owner's console, wherever they appear.
_PRIVATE_KEYS = re.compile(r'(?i)(prompt|thinking|reasoning_text|chat|conversation|message_history|owner|email|user|account|'
                           r'credential|token(?!s?_?(count|s)$)|secret|api_?key|password|cookie|session|auth|billing|cost|'
                           r'env|command|argv|cwd|home|path|workspace|url)')


def _plain(value, depth=3):
    """A bounded JSON copy of model-written review data (fidelity findings,
    citations): text and numbers only, private-looking keys dropped."""
    if isinstance(value, str):
        return _redact(value, 4000)
    if isinstance(value, bool) or _number(value):
        return value
    if depth <= 0:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:60]:
            if not isinstance(key, str) or _PRIVATE_KEYS.search(key):
                continue
            kept = _plain(item, depth - 1)
            if kept is not None:
                out[_text(key, 80)] = kept
        return out
    if isinstance(value, list):
        return [kept for kept in (_plain(item, depth - 1) for item in value[:60]) if kept is not None]
    return None


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
    from agent_monitor.analyze.graph import ref_labels
    from agent_monitor.analyze.titles import tex_text_refs

    # The viewer shows statements as text: \ref{x} reads as the result's number.
    refs = ref_labels(graph)
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
            'statement': _text(' '.join(tex_text_refs(node.get('statement'), refs).split()), 1200),
            'status': status, 'verdict': verdict,
            'closure': VERDICT_STATUS[closure] if closure else '',
            'main_result': bool(meta.get('main_result')),
        }
        if uninterpreted:
            entry['uninterpreted'] = True
        # Numbering, labels and the names of cited outside results (as the
        # console's cards show them); never the model's notes or excerpts.
        if isinstance(meta.get('number'), str):
            entry['number'] = _text(meta['number'], 40)
        labels = [_text(label, 120) for label in _items(meta.get('labels'), 10) if isinstance(label, str)]
        if labels:
            entry['labels'] = labels
        external = [{'citation': _text(item.get('citation'), 300)} for item in _items(meta.get('external'), 10)
                    if isinstance(item, dict) and isinstance(item.get('citation'), str)]
        if external:
            entry['external'] = external
        citations = [_text(item, 240) for item in _items(node.get('citations'), 8) if isinstance(item, str)]
        if citations:
            entry['citations'] = citations
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
    out.update(_numbers(summary, ('calls',), 1e9))
    coverage = _numbers(summary.get('coverage'), ('total', 'checked', 'located', 'uninterpretable', 'statements'), 1e9)
    if coverage:
        out['coverage'] = coverage
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
    from agent_monitor.analyze.titles import plain_title, tex_text_refs

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
        'version': 3, 'type': 'shared', 'kind': 'analysis',
        'title': _text(f'Analyze · {title}', 240),
        'status': _text(data.get('status'), 80), 'engine': 'analyze',
        'problem': problem,
        # The report quotes labels and model findings, never the manuscript;
        # a report written before cross-references were resolved reads as text too.
        'proof': tex_text_refs(read('analysis/report.md'), analysis_graph.ref_labels(manuscript_graph)
                               if manuscript_graph else None),
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
        limit = 3_000_000 if name in {'proof_graph.json', 'lean_proof_graph.json', 'analysis/analysis_graph.json', 'lean_verify.json'} else 400_000
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
        result['run_view'] = _run_view(data, read)
        result['lean_record'] = _lean_record(read)
        result['formal_graph'] = result['graphs']['formal']['graph']
        if len(json.dumps(result, ensure_ascii=False).encode()) > 4_000_000:
            raise ValueError('This snapshot is too large to share (4 MB maximum).')
        return result
    agents = []
    steps = [item for item in (data.get('agents') or []) if isinstance(item, dict)]
    # A long run keeps its first steps and its last ones, which carry the
    # final answer and any follow-ups.
    steps = steps if len(steps) <= 100 else steps[:30] + steps[-70:]
    proof, proof_source = read('proof.md'), 'proof.md'
    if not proof:
        proof, proof_source = read('proof.tex'), 'proof.tex'
    if not proof:
        proof, proof_source = _text(data.get('proof_text')), ''
    for item in steps:
        if not isinstance(item, dict):
            continue
        agents.append({
            'id': _text(item.get('agent_id') or item.get('id') or item.get('trace_id'), 200),
            'label': _text(item.get('agent_name') or item.get('name') or item.get('stage_name') or item.get('stage') or item.get('agent'), 200),
            'status': _text(item.get('status'), 80),
            'model': _text(item.get('model'), 160),
            'summary': _agent_output(item),
            **_agent_details(item),
        })
    result = {
        'version': 3, 'type': 'shared', 'title': _text(data.get('title') or data.get('problem_text_preview') or run_id, 240),
        'status': _text(data.get('status'), 80), 'engine': _text(data.get('engine'), 100),
        'problem': _text(data.get('problem_text') or data.get('problem_text_preview')),
        'proof': proof, 'proof_source': proof_source,
        'agents': agents, 'informal_graph': graph('proof_graph.json'),
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': read('lean/Proof.lean'),
        'notice': 'Read-only snapshot selected by the run owner. It does not update as the run continues. Graphs describe dependencies; they are not, by themselves, proof certificates.',
    }
    result['graphs'] = _graph_modes(ws, data, read, result, safe_workspace=safe_ws)
    result['run_view'] = _run_view(data, read)
    result['lean_record'] = _lean_record(read)
    result['informal_graph'] = result['graphs']['outline']['graph']
    result['formal_graph'] = result['graphs']['formal']['graph']
    if len(json.dumps(result, ensure_ascii=False).encode()) > 4_000_000:
        raise ValueError('This snapshot is too large to share (4 MB maximum).')
    return result


CONSOLE_RUN = 'shared'
_RUN_STATES = {'running', 'finished', 'failed', 'stopped', 'interrupted'}
_VERDICT_CODE = {label: code for code, label in VERDICT_STATUS.items()}


def _console_graph(record, kind, **extra):
    if not isinstance(record, dict):
        return None
    graph = record.get('graph') if isinstance(record.get('graph'), dict) else {}
    if not graph.get('nodes'):
        return {'status': 'none', 'kind': kind, **({'reason': record['reason']} if isinstance(record.get('reason'), str) else {})}
    out = {key: record[key] for key in ('status', 'graph_role', 'model', 'dependency_basis', 'selection', 'counts', 'truncated', 'provenance')
           if key in record}
    out.update({'kind': kind, 'graph': graph, **extra})
    return out


def _console_formal(graphs):
    formal = _console_graph(graphs.get('formal'), 'formal') or {'status': 'none', 'kind': 'formal'}
    compiler = graphs.get('compiler') if isinstance(graphs.get('compiler'), dict) else {}
    nodes = (compiler.get('graph') or {}).get('nodes') if isinstance(compiler.get('graph'), dict) else None
    if nodes:
        # The console finds compiler facts beside the structural Formal graph.
        provenance = dict(compiler.get('provenance') or {})
        provenance['kind'] = 'lean-compiler'
        formal['knowledge_graph'] = {'status': 'ok'}
        formal['compiler_graph'] = {**compiler['graph'], 'provenance': provenance}
        if formal.get('status') == 'none':
            formal['status'] = 'recorded'
    return formal


def _console_manuscript(record, summary):
    """The shared Manuscript graph in the console's analysis payload shape."""
    if not isinstance(record, dict) or not (record.get('graph') or {}).get('nodes'):
        return {'status': 'none', 'kind': 'analysis'}
    graph = record['graph']
    nodes = []
    for node in graph.get('nodes') or []:
        verdict = node.get('verdict') if node.get('verdict') in VERDICT_STATUS else _VERDICT_CODE.get(node.get('status'), 'not_checked')
        meta = {'verdict': verdict, 'main_result': bool(node.get('main_result')), 'interpreted': not node.get('uninterpreted')}
        closure = _VERDICT_CODE.get(node.get('closure'))
        if closure:
            meta['closure'] = closure
        for key in ('number', 'labels', 'external'):
            if key in node:
                meta[key] = node[key]
        nodes.append({key: node[key] for key in ('id', 'kind', 'label', 'statement', 'citations') if key in node} | {'meta': meta})
    return {
        'status': 'ready', 'kind': 'analysis', 'certificate': False,
        'basis': record.get('dependency_basis') or '', 'summary': summary or {},
        'graph': {'nodes': nodes, 'edges': graph.get('edges') or [], 'source': {'title': graph.get('title') or ''}},
    }


def _lean_summary(record):
    """The run payload's compact Lean state (as lean_verify.monitor_summary)."""
    if not record:
        return {'status': 'none', 'has_result': False, 'attempt_count': 0, 'attempts': [], 'chat': [], 'harness_running': False}
    attempts = []
    for attempt in record.get('attempts') or []:
        check = attempt.get('check') or {}
        status = attempt.get('result_status') or check.get('status') or ('verified' if check.get('ok') else 'failed')
        attempts.append({'action': attempt.get('action') or 'check', 'status': status,
                         'sorry_count': attempt.get('sorry_count') or 0, 'ok': bool(check.get('ok'))})
    count = record.get('attempt_count') if isinstance(record.get('attempt_count'), int) else len(attempts)
    return {'status': record.get('status') or 'none', 'has_result': bool(record.get('lean')),
            'attempt_count': max(count, len(attempts)), 'attempts': attempts, 'chat': [], 'harness_running': False,
            'generated_at': record.get('generated_at') or ''}


def console_view(snap):
    """What the console's own run interface requests for one run, answered
    from a saved snapshot (any version): the run record, the proof document,
    each graph view and the Lean record. Shared pages render it with the
    console's code; nothing here reads the workspace."""
    snap = snap if isinstance(snap, dict) else {}
    analysis = snap.get('kind') == 'analysis'
    saved = snap.get('run_view') if isinstance(snap.get('run_view'), dict) else {}
    graphs = snap.get('graphs') if isinstance(snap.get('graphs'), dict) else {}
    if not graphs:
        # Version 1 snapshots kept only the two original graphs.
        legacy = {'formal': snap.get('formal_graph'), ('manuscript' if analysis else 'outline'): snap.get('informal_graph')}
        graphs = {mode: {'status': 'recorded', 'graph': graph} for mode, graph in legacy.items()
                  if isinstance(graph, dict) and graph.get('nodes')}
    title = _text(snap.get('title'), 240)
    problem = _text(snap.get('problem'))
    proof = _text(snap.get('proof'), 400_000)
    status = _text(snap.get('status'), 80)
    # Worked examples carry a descriptive status; the console knows run states.
    status = status if status in _RUN_STATES else 'finished'
    lean = snap.get('lean_record') if isinstance(snap.get('lean_record'), dict) else None
    if lean is None and isinstance(snap.get('lean_source'), str) and snap['lean_source'].strip():
        verification = snap.get('verification') if isinstance(snap.get('verification'), dict) else {}
        recorded = ' · '.join(_text(verification.get(key), 300) for key in ('version', 'result') if verification.get(key))
        lean = {'status': 'saved', 'lean': snap['lean_source'], 'lean_path': 'lean/Proof.lean', 'attempts': [],
                'log': _redact(recorded, 2000),
                'notes': recorded and 'Checker result as recorded with the saved source.'
                or 'The Lean source as saved. Its checker record was not included in this snapshot.'}
    legacy = not isinstance(snap.get('version'), int) or snap['version'] < 3
    agents = []
    for index, agent in enumerate(_items(snap.get('agents'), 100), 1):
        if not isinstance(agent, dict):
            continue
        entry = {key: agent[key] for key in agent if key not in {'id', 'label', 'summary'}}
        output = agent.get('summary') if isinstance(agent.get('summary'), str) else ''
        if legacy:
            # Shares made before version 3 kept raw step output: apply today's
            # rules (tool output withheld, server paths and costs removed).
            tool = bool(_TOOL_LABEL.search(str(agent.get('label') or '')))
            output = _agent_output({'summary': output, 'role': 'tool' if tool else ''})
        entry.update({'trace_id': f'{CONSOLE_RUN}::{index}', 'call_seq': agent.get('call_seq') or index,
                      'stage_name': agent.get('stage_name') or agent.get('label') or 'Research step',
                      'output': output, 'model': agent.get('model') or ''})
        agents.append(entry)
    analysis_block = snap.get('analysis') if isinstance(snap.get('analysis'), dict) else {}
    run = {
        'run_id': CONSOLE_RUN, 'status': status, 'engine': _text(snap.get('engine'), 100),
        'run_kind': 'analyze' if analysis else 'prove', 'title': title,
        'problem_text': problem, 'problem_preview': title, 'problem_id': CONSOLE_RUN,
        'problems': [{'text': problem, 'source': 'user'}] if problem else [],
        'agents': agents, 'edges': saved.get('edges') or [],
        'totals': saved.get('totals') or {}, 'live': saved.get('live') or {},
        'lean_verification': _lean_summary(lean),
        'auto_pipeline': saved.get('auto_pipeline'), 'proof_coverage': saved.get('proof_coverage'),
        'shared': True,
    }
    for key in ('route_plan', 'error', 'interruption', 'model', 'requested_model', 'reasoning_effort',
                'created_at', 'updated_at', 'wall_s', 'pipeline', 'stages_present'):
        if key in saved:
            run[key] = saved[key]
    if isinstance(saved.get('agent_flow'), dict):
        flow = saved['agent_flow']
        nodes = [{**node, 'id': f'{CONSOLE_RUN}::flow-{index}', 'trace_id': f'{CONSOLE_RUN}::flow-{index}'}
                 for index, node in enumerate(flow.get('nodes') or [], 1) if isinstance(node, dict)]
        run['analysis'] = {'agent_flow': {**flow, 'nodes': nodes}}
    files = []
    if analysis:
        source = analysis_block.get('source') if isinstance(analysis_block.get('source'), dict) else {}
        run['analyze'] = {'summary': analysis_block.get('summary') or {}, 'source': source}
        run['sandbox_preview'] = {
            'kind': 'analysis', 'text': proof, 'source': 'analysis/report.md' if proof else '',
            'has_report': bool(proof), 'has_proof_md': False, 'has_proof_tex': False, 'editable': False,
            'revision': 'shared', 'mtime': 0, 'title': _text(source.get('title'), 300),
            'source_url': snap['source_url'] if str(snap.get('source_url') or '').startswith('https://') else None,
            'source_file': None,
        }
    elif snap.get('proof_source') == 'proof.tex':
        # The console compiles proof.tex to a PDF; a share shows its source.
        run['sandbox_preview'] = {'text': proof, 'source': 'proof.tex', 'has_proof_md': False,
                                  'has_proof_tex': True, 'revision': 'shared', 'mtime': 0}
    else:
        run['sandbox_preview'] = {'text': proof, 'source': 'proof.md' if proof else '', 'has_proof_md': bool(proof),
                                  'has_proof_tex': False, 'revision': 'shared', 'mtime': 0}
        if proof:
            files.append({'path': 'proof.md', 'kind': 'text', 'size': len(proof.encode()), 'mtime': 0})
    payloads = {
        'informal': _console_graph(graphs.get('outline'), 'informal') or {'status': 'none', 'kind': 'informal'},
        'formal': _console_formal(graphs),
        'engine:workflow': _console_graph(graphs.get('explore'), 'engine', view='workflow') or {'status': 'none', 'kind': 'engine'},
        'engine:facts': _console_graph(graphs.get('facts'), 'engine', view='facts') or {'status': 'none', 'kind': 'engine'},
    }
    if analysis:
        payloads['analysis'] = _console_manuscript(graphs.get('manuscript'), analysis_block.get('summary'))
        payloads['informal'] = _console_graph(graphs.get('outline') or graphs.get('manuscript'), 'informal') or payloads['informal']
    # The panel beside the proof shows the engine's closing message, which is
    # its last step's output; human messages and system lines stay private.
    closing = agents[-1]['output'] if agents and agents[-1].get('output') else ''
    chat = [{'role': 'assistant', 'content': closing}] if closing else []
    if analysis:
        from agent_monitor import jobs

        # The console's own closing note for an analysis, from its summary.
        chat = [{'role': 'system', 'content': jobs._analyze_chat_summary(run, status)}]
    return {
        'title': title, 'notice': _text(snap.get('notice'), 1000), 'shared_at': snap.get('shared_at'),
        'run': run, 'files': files, 'graphs': payloads, 'lean': lean or {'status': 'none'}, 'chat': chat,
    }


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
    return json.loads(row['snapshot']) if row and _record_exists(row['run_id']) else None


def forget(run_id):
    """Remove every shared link of a deleted run."""
    with _connect() as db:
        db.execute('DELETE FROM shares WHERE run_id=?', (run_id,))


# The console's third-party assets, replaced on shared pages by the identical
# self-hosted copies (marked 12.0.2, KaTeX 0.16.11, the same two fonts), so a
# shared page makes no third-party request. PDF.js is not needed: a share has
# no PDF.
_THIRD_PARTY = (
    (re.compile(r'<link rel="preconnect" href="https://fonts\.(?:googleapis|gstatic)\.com"[^>]*>\n?'), ''),
    (re.compile(r'<link href="https://fonts\.googleapis\.com/css2\?[^"]*" rel="stylesheet"/?>'),
     '<link rel="stylesheet" href="{static}/vendor/shared/fonts.css">'),
    (re.compile(r'<script src="https://cdnjs\.cloudflare\.com/ajax/libs/pdf\.js/[^"]*"[^>]*></script>\n?'), ''),
    (re.compile(r'<script src="https://cdn\.jsdelivr\.net/npm/marked@12\.0\.2/marked\.min\.js"[^>]*></script>'),
     '<script src="{static}/vendor/shared/marked.min.js"></script>'),
    (re.compile(r'<link rel="stylesheet" href="https://cdn\.jsdelivr\.net/npm/katex@0\.16\.11/dist/katex\.min\.css"[^>]*>'),
     '<link rel="stylesheet" href="{static}/vendor/shared/katex.min.css">'),
    (re.compile(r'<script src="https://cdn\.jsdelivr\.net/npm/katex@0\.16\.11/dist/katex\.min\.js"[^>]*></script>'),
     '<script src="{static}/vendor/shared/katex.min.js"></script>'),
    (re.compile(r'<script src="https://cdn\.jsdelivr\.net/npm/katex@0\.16\.11/dist/contrib/auto-render\.min\.js"[^>]*></script>'),
     '<script src="{static}/vendor/shared/auto-render.min.js"></script>'),
    # Sharing a shared page is not offered.
    (re.compile(r'<script id="sharing-loader">.*?</script>\n?', re.S), ''),
)
_EXTERNAL_ASSET = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)="(?:https?:)?//', re.I)
_INLINE_SCRIPT = re.compile(r'<script(?![^>]*\ssrc\s*=)[^>]*>(.*?)</script>', re.S | re.I)
# Console tabs, plus the graph names links from the earlier viewer used.
SHARED_VIEWS = ('sandbox', 'lean', 'monitor', 'dag', 'explore', 'facts', 'outline', 'formal', 'compiler', 'manuscript')


def shared_page(html, *, base, data_url, monitor_url='', view='', inject_base=None):
    """The console (or its Monitor frame) as a read-only page for one shared
    snapshot, and the Content-Security-Policy that pins its scripts.

    Every script that may run is either served from this origin or is one of
    the page's own inline scripts, pinned by hash, so text inside a snapshot
    can never run as script even if it reached the page as markup."""
    static = base + '/static'
    for pattern, replacement in _THIRD_PARTY:
        html = pattern.sub(replacement.replace('{static}', static), html)
    if _EXTERNAL_ASSET.search(html):
        raise ValueError('A shared page must not load third-party assets.')
    config = {'data': data_url, 'base': base, 'console': base + '/', 'monitor': monitor_url,
              'view': view if view in SHARED_VIEWS else ''}
    head = ('<meta name="robots" content="noindex,nofollow,noarchive">'
            '<meta name="referrer" content="no-referrer">'
            f'<meta name="ansatze-shared" content="{html_lib.escape(json.dumps(config), quote=True)}">'
            f'<script src="{html_lib.escape(static, quote=True)}/shared-console.js"></script>')
    index = html.lower().find('<head>')
    if index < 0:
        raise ValueError('Shared page template has no <head>.')
    index += len('<head>')
    html = html[:index] + head + html[index:]
    if inject_base is not None:
        html = inject_base(html, base)
    hashes = sorted({"'sha256-" + base64.b64encode(hashlib.sha256(body.encode('utf-8')).digest()).decode() + "'"
                     for body in _INLINE_SCRIPT.findall(html)})
    policy = '; '.join((
        "default-src 'none'",
        "script-src 'self' " + ' '.join(hashes),
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self' data:",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "frame-src 'self'",
        "worker-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
    ))
    return html, policy


# Stored only to build the console view; the response carries that view.
_VIEW_ONLY = ('graphs', 'informal_graph', 'formal_graph', 'run_view', 'lean_record', 'agents', 'proof', 'lean_source')


def shared_payload(data):
    """The JSON a shared page loads: the snapshot's descriptive fields and
    the console view the page renders."""
    return {**{key: value for key, value in data.items() if key not in _VIEW_ONLY}, 'console': console_view(data)}


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
        from agent_monitor import jobs
        owner = jobs.run_owner(run_id)
        # The person who started a run publishes it; the operator also owns
        # legacy runs that have no owner. Project members and the operator can
        # open other people's runs, but publishing them is not theirs to do.
        mine = owner == user.get('id') if owner is not None else bool(user.get('is_admin'))
        visible = mine or getattr(self, '_owns_run', lambda *_: False)(run_id, user)
        data = record(run_id) if visible else None
        if not data:
            self._share_send(404, {'error': 'Run not found.'})
            return None
        if not mine:
            self._share_send(403, {'error': 'Only the person who started this run can share it.'})
            return None
        return user, data

    def _sharing_get(self, path):
        match = re.fullmatch(r'/(api/)?(?:shared/([A-Za-z0-9_-]{43})|examples/(erdos-straus))(/monitor)?', path)
        if match:
            api, token, example, monitor = match.groups()
            if api and monitor:
                return False
            data = json.loads((EXAMPLES / 'erdos-straus.json').read_text()) if example else lookup(token)
            base = self._request_base_path()
            page = base + ('/examples/erdos-straus' if example else '/shared/' + token)
            if api:
                if data is None:
                    self._share_send(404, {'error': 'This shared link is unavailable or has been revoked.'})
                else:
                    self._share_send(200, shared_payload(data))
                return True
            template = (DASHBOARD_WEB / 'index.html') if monitor else (WEB / 'console.html')
            query = parse_qs(urlsplit(self.path).query)
            view = (query.get('view') or [''])[0]
            data_url = base + ('/api/examples/erdos-straus' if example else '/api/shared/' + token)
            html, policy = shared_page(template.read_text(encoding='utf-8'), base=base, data_url=data_url,
                                       monitor_url=page + '/monitor', view=view,
                                       inject_base=getattr(self, '_inject_base_html', None))
            # An unavailable link still gets the console, which says so.
            self._send(200 if data is not None else 404, html, 'text/html; charset=utf-8',
                       headers={**HEADERS, 'Content-Security-Policy': policy})
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
