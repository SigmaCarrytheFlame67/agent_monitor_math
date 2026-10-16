"""Shared runs use the console's own run interface, read-only.

/shared/<token> serves console.html with shared-console.js, which answers the
console's run-view requests from the saved snapshot; every inline script is
pinned by hash and nothing loads from a third party.
"""
import base64
import hashlib
import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_monitor import console_server as console, jobs, run_sharing as shares

TOKEN = 'T' * 43


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(shares, 'DB_PATH', tmp_path / 'shares.sqlite3')
    monkeypatch.setattr(shares, 'RUNS_DIR', tmp_path / 'runs')
    monkeypatch.setattr(shares, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.delenv('LLM_DASHBOARD_CACHE', raising=False)
    (tmp_path / 'runs').mkdir()


def _page(template='console', base='/proof-abc'):
    path = shares.DASHBOARD_WEB / 'index.html' if template == 'monitor' else shares.WEB / 'console.html'
    return shares.shared_page(path.read_text(), base=base, data_url=base + '/api/shared/' + TOKEN,
                              monitor_url=base + '/shared/' + TOKEN + '/monitor', view='dag',
                              inject_base=console._inject_base)


@pytest.mark.parametrize('template', ['console', 'monitor'])
def test_shared_page_pins_every_script_and_loads_nothing_third_party(template):
    html, policy = _page(template)
    directives = dict(part.strip().split(' ', 1) for part in policy.split(';'))
    scripts = directives['script-src'].split()
    assert "'unsafe-inline'" not in scripts and "'unsafe-eval'" not in scripts
    assert directives['default-src'] == "'none'" and directives['object-src'] == "'none'"
    assert directives['connect-src'] == "'self'" and directives['base-uri'] == "'none'"
    inline = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    assert inline, 'the console has inline scripts'
    for body in inline:
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in scripts
    assert not re.search(r'<(?:script|link)\b[^>]*\b(?:src|href)="(?:https?:)?//', html)
    assert 'fonts.googleapis.com' not in html and 'cdn.jsdelivr.net' not in html and 'pdf.min.js' not in html
    # The shim runs before the console's own scripts and knows where the snapshot is.
    shim = html.index('<script src="/proof-abc/static/shared-console.js">')
    console_scripts = [m.start() for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>', html)
                       if '__PC_BASE__' not in html[m.end():m.end() + 80]]
    assert console_scripts and shim < min(console_scripts)
    config = json.loads(re.search(r'<meta name="ansatze-shared" content="([^"]+)"', html)[1].replace('&quot;', '"'))
    assert config['data'] == '/proof-abc/api/shared/' + TOKEN and config['view'] == 'dag'
    assert config['monitor'] == '/proof-abc/shared/' + TOKEN + '/monitor'
    assert 'id="sharing-loader"' not in html


def test_shared_page_refuses_a_template_that_would_load_third_party_script():
    with pytest.raises(ValueError):
        shares.shared_page('<html><head><script src="https://evil.test/x.js"></script></head></html>',
                           base='', data_url='/api/shared/x')


def test_console_vendor_copies_match_the_cdn_versions_the_console_uses():
    html = (shares.WEB / 'console.html').read_text()
    manifest = {item['file']: item['version'] for item in json.loads((shares.WEB / 'vendor/shared/manifest.json').read_text())}
    assert 'marked@' + manifest['marked.min.js'] + '/' in html
    assert 'katex@' + manifest['katex.min.js'] + '/' in html
    for pattern, _replacement in shares._THIRD_PARTY[:-1]:
        assert pattern.search(html), pattern.pattern


def _snapshot_v2():
    return {
        'version': 2, 'type': 'shared', 'title': 'Odd sums', 'status': 'finished', 'engine': 'math_harness',
        'problem': 'Prove $1+3+\\dots+(2n-1)=n^2$.', 'proof': '# Proof\n\nBy induction.',
        'agents': [{'id': 'r::1', 'label': 'Strategist', 'status': 'completed', 'model': 'm', 'summary': 'plan'}],
        'lean_source': 'theorem t : True := trivial',
        'graphs': {
            'outline': {'kind': 'outline', 'status': 'parsed', 'graph_role': 'proof-outline',
                        'graph': {'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []}},
            'explore': {'kind': 'explore', 'status': 'recorded', 'graph_role': 'workflow',
                        'graph': {'nodes': [{'id': 'g', 'kind': 'goal', 'meta': {'workflow_kind': 'goal'}}], 'edges': []}},
            'facts': {'kind': 'facts', 'status': 'unavailable', 'graph_role': 'fact-dependency', 'graph': {'nodes': [], 'edges': []},
                      'reason': 'No facts.'},
            'formal': {'kind': 'formal', 'status': 'parsed', 'graph_role': 'formal-structure',
                       'graph': {'nodes': [{'id': 't', 'label': 't'}], 'edges': []}},
            'compiler': {'kind': 'compiler', 'status': 'recorded', 'graph_role': 'compiler-dependency',
                         'graph': {'nodes': [{'id': 't'}], 'edges': [{'from': 't', 'to': 't', 'label': 'proof uses'}]},
                         'provenance': {'kind': 'lean-compiler', 'toolchain': 'Lean 4.14'}},
        },
    }


def test_console_view_answers_each_console_request_from_a_v2_snapshot():
    view = shares.console_view(_snapshot_v2())
    run = view['run']
    assert run['run_id'] == 'shared' and run['status'] == 'finished' and run['run_kind'] == 'prove'
    assert run['problems'] == [{'text': 'Prove $1+3+\\dots+(2n-1)=n^2$.', 'source': 'user'}]
    assert run['sandbox_preview']['text'] == '# Proof\n\nBy induction.' and run['sandbox_preview']['source'] == 'proof.md'
    assert view['files'] == [{'path': 'proof.md', 'kind': 'text', 'size': 22, 'mtime': 0}]
    agent = run['agents'][0]
    assert agent['trace_id'] == 'shared::1' and agent['stage_name'] == 'Strategist' and agent['output'] == 'plan'
    graphs = view['graphs']
    assert graphs['informal']['kind'] == 'informal' and graphs['informal']['graph']['nodes'][0]['id'] == 'a'
    assert graphs['engine:workflow']['kind'] == 'engine' and graphs['engine:workflow']['graph_role'] == 'workflow'
    assert graphs['engine:facts'] == {'status': 'none', 'kind': 'engine', 'reason': 'No facts.'}
    formal = graphs['formal']
    assert formal['graph']['nodes'][0]['id'] == 't' and formal['knowledge_graph'] == {'status': 'ok'}
    assert formal['compiler_graph']['provenance']['kind'] == 'lean-compiler'
    assert view['lean']['lean'] == 'theorem t : True := trivial' and view['lean']['status'] == 'saved'
    assert run['lean_verification']['has_result'] is True


def test_console_view_reads_version_1_and_worked_examples():
    example = json.loads((shares.EXAMPLES / 'erdos-straus.json').read_text())
    view = shares.console_view(example)
    assert view['run']['status'] == 'finished'  # a descriptive status is not a run state
    assert view['graphs']['informal']['graph']['nodes'] and view['graphs']['formal']['graph']['nodes']
    assert 'does not depend on any axioms' in view['lean']['log']
    payload = shares.shared_payload(example)
    assert payload['type'] == 'example' and len(payload['witnesses']) == 499 and 'graphs' not in payload


def test_console_view_maps_the_shared_manuscript_back_to_verdict_codes():
    snap = {'version': 2, 'kind': 'analysis', 'title': 'Analyze · Paper', 'status': 'finished', 'engine': 'analyze',
            'proof': '# Report', 'source_url': 'https://arxiv.org/abs/1', 'analysis': {'summary': {'nodes': 2, 'checked': 1}},
            'graphs': {'manuscript': {'kind': 'manuscript', 'status': 'recorded', 'graph_role': 'manuscript-dependency',
                                      'graph': {'title': 'Paper', 'nodes': [
                                          {'id': 'thm1', 'label': 'Theorem 1', 'verdict': 'checked', 'status': 'checked by a model verifier',
                                           'closure': 'gap', 'main_result': True},
                                          {'id': 'lem1', 'label': 'Lemma 1', 'status': 'not checked (no usable verifier output)',
                                           'verdict': 'not_checked', 'uninterpreted': True}],
                                          'edges': [{'from': 'lem1', 'to': 'thm1', 'label': 'uses'}]}}}}
    view = shares.console_view(snap)
    manuscript = view['graphs']['analysis']
    assert manuscript['status'] == 'ready' and manuscript['certificate'] is False
    first, second = manuscript['graph']['nodes']
    assert first['meta'] == {'verdict': 'checked', 'closure': 'gap', 'main_result': True, 'interpreted': True}
    assert second['meta']['verdict'] == 'not_checked' and second['meta']['interpreted'] is False
    preview = view['run']['sandbox_preview']
    assert preview['kind'] == 'analysis' and preview['has_report'] and preview['source'] == 'analysis/report.md'
    assert preview['source_url'] == 'https://arxiv.org/abs/1' and view['run']['run_kind'] == 'analyze'


def test_new_snapshots_keep_console_details_but_nothing_private(tmp_path):
    ws = shares.RUNS_DIR / 'workspaces' / 'run-9'
    (ws / 'lean').mkdir(parents=True)
    (ws / 'proof.md').write_text('Proof text')
    (ws / 'lean' / 'Proof.lean').write_text('theorem x : True := trivial')
    (ws / 'auto_pipeline.json').write_text(json.dumps({'status': 'done', 'proof_engine': 'math_harness', 'stages': {
        'informal_dag': {'status': 'done', 'detail': 'built from /var/lib/ansatze/console/runs/workspaces/run-9/proof.md'},
        'lean': {'status': 'done', 'result': {'status': 'verified', 'scope': 'proof', 'command': 'lake env lean'}},
        'secret_stage': {'status': 'done'}}}))
    (ws / 'lean_verify.json').write_text(json.dumps({
        'status': 'verified', 'lean': 'theorem x : True := trivial', 'model': 'kimi-k3',
        'log': '/home/ubuntu/ansatze/lean/Proof.lean:1:0: info', 'chat': [{'role': 'user', 'text': 'PRIVATE CHAT'}],
        'harness': {'prompt': 'PRIVATE PROMPT'}, 'toolchain': {'lean_version': '4.14.0', 'path': '/opt/lean/bin/lean'},
        'attempts': [{'action': 'check', 'sorry_count': 0, 'prompt': 'PRIVATE PROMPT',
                      'check': {'ok': True, 'status': 'verified', 'log': 'ok', 'env': {'KEY': 'PRIVATE KEY'}}}],
        'fidelity': {'verdict': 'faithful', 'issues': [], 'auditor_prompt': 'PRIVATE PROMPT'}}))
    record = {
        'run_id': 'run-9', 'status': 'finished', 'engine': 'math_harness', 'problem_text': 'P', 'owner_email': 'o@example.com',
        'credential_source': 'PRIVATE ROUTE', 'route_plan': {'advisor': {'model': 'claude-opus', 'auth': 'PRIVATE KEY'}},
        'totals': {'input_tokens': 10, 'output_tokens': 5, 'cost_usd': 12.5},
        'live': {'model': 'kimi-k3', 'auth_mode': 'claude_subscription', 'tool_calls': 3, 'api_key': 'PRIVATE KEY',
                 'tools_recent': [{'name': 'edit', 'args': 'PRIVATE ARGS'}]},
        'agents': [{'trace_id': 'run-9::s1', 'stage_name': '3 Verify', 'role': 'verifier', 'model': 'kimi-k3', 'status': 'completed',
                    'output': 'wrote /var/lib/ansatze/console/runs/workspaces/run-9/proof.md', 'prompt': 'PRIVATE PROMPT',
                    'thinking': 'PRIVATE THINKING', 'cost_usd': 9.0, 'input_tokens': 7, 'tools': ['edit']},
                   {'trace_id': 'run-9::s2', 'stage_name': 'turn-2 (exec)', 'role': 'tools', 'pipeline_stage': 'act',
                    'output': '$ cat attachments/notes.txt\nPRIVATE ATTACHMENT TEXT'},
                   {'trace_id': 'run-9::s3', 'stage_name': 'claude session summary', 'role': 'orchestrator',
                    'output': 'tool · Read attachments/notes.txt\n✓ result · 2 turns · $0.0421 · done'}],
        'error': 'failed in /home/ubuntu/.codex/sessions/x.jsonl',
    }
    snap = shares.snapshot('run-9', record)
    encoded = json.dumps(snap) + json.dumps(shares.shared_payload(snap))
    for secret in ('PRIVATE', 'o@example.com', '/var/lib/', '/home/ubuntu', '/opt/lean', 'cost_usd', '12.5', 'secret_stage',
                   'lake env lean', '0.0421', '.codex/sessions'):
        assert secret not in encoded, secret
    view = shares.console_view(snap)
    run = view['run']
    assert run['totals'] == {'input_tokens': 10, 'output_tokens': 5}
    # The model chips, never how the owner pays for model access.
    assert run['live'] == {'model': 'kimi-k3', 'tool_calls': 3, 'tools_recent': [{'name': 'edit'}]}
    assert 'claude_subscription' not in encoded
    assert run['route_plan'] == {'advisor': {'model': 'claude-opus'}}
    assert run['auto_pipeline']['stages']['lean'] == {'status': 'done', 'result': {'status': 'verified', 'scope': 'proof'}}
    assert run['auto_pipeline']['stages']['informal_dag']['detail'] == 'built from …/proof.md'
    agent = run['agents'][0]
    assert agent['stage_name'] == '3 Verify' and agent['role'] == 'verifier' and agent['input_tokens'] == 7
    assert agent['output'] == 'wrote …/proof.md' and agent['tools'] == ['edit']
    # A tool step keeps its command, never what it returned; logs drop costs.
    assert run['agents'][1]['output'] == '$ cat attachments/notes.txt\n(tool output is not included in shared snapshots)'
    assert run['agents'][2]['output'] == 'tool · Read attachments/notes.txt\n✓ result · 2 turns · … · done'
    assert view['chat'] == [{'role': 'assistant', 'content': run['agents'][2]['output']}]
    lean = view['lean']
    assert lean['status'] == 'verified' and lean['lean'] == 'theorem x : True := trivial' and lean['log'] == '…/Proof.lean:1:0: info'
    assert lean['toolchain'] == {'lean_version': '4.14.0'} and lean['fidelity'] == {'verdict': 'faithful', 'issues': []}
    assert lean['attempts'][0]['check'] == {'status': 'verified', 'ok': True, 'log': 'ok'}
    # The error can name the owner's subscription: shares keep only that it failed.
    assert run['error'] == 'The run stopped with an error. Its details are not included in shared snapshots.'


def test_redaction_keeps_urls_and_relative_paths():
    text = 'see https://example.org/home/x, lean/Proof.lean and /tmp/build/out.txt'
    assert shares._redact(text) == 'see https://example.org/home/x, lean/Proof.lean and …/out.txt'


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(console, 'BASE_PATH', '/test')
    monkeypatch.setattr(console.Handler, '_current_user', lambda self: None)
    server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path):
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        conn.request('GET', '/test' + path)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        body = json.loads(raw) if 'application/json' in res.getheader('Content-Type', '') else raw.decode()
        return res.status, dict(res.getheaders()), body
    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_shared_routes_serve_the_console_its_monitor_and_the_snapshot(api, monkeypatch):
    snap = _snapshot_v2()
    monkeypatch.setattr(shares, 'lookup', lambda token: snap if token == TOKEN else None)
    status, headers, html = api('/shared/' + TOKEN + '?view=lean')
    assert status == 200 and headers['Content-Type'].startswith('text/html')
    assert "script-src 'self' 'sha256-" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store' and headers['Referrer-Policy'] == 'no-referrer'
    assert headers['X-Robots-Tag'].startswith('noindex')
    assert 'Proving Console' in html and '/test/static/shared-console.js' in html
    assert '&quot;view&quot;: &quot;lean&quot;' in html and '&quot;data&quot;: &quot;/test/api/shared/' + TOKEN in html
    status, headers, monitor = api('/shared/' + TOKEN + '/monitor')
    assert status == 200 and 'Harness Pipeline Monitor' in monitor and '/test/static/shared-console.js' in monitor
    status, headers, data = api('/api/shared/' + TOKEN)
    assert status == 200 and data['title'] == 'Odd sums' and data['console']['run']['run_id'] == 'shared'
    assert 'graphs' not in data and 'proof' not in data
    # An unavailable link still gets the console, which explains; its data is a 404.
    status, _headers, html = api('/shared/' + 'U' * 43)
    assert status == 404 and '/test/static/shared-console.js' in html
    assert api('/api/shared/' + 'U' * 43)[0] == 404
    assert api('/api/shared/' + TOKEN + '/monitor')[0] == 401


def test_worked_example_uses_the_shared_console(api):
    status, headers, html = api('/examples/erdos-straus')
    assert status == 200 and '/test/api/examples/erdos-straus' in html
    status, _headers, data = api('/api/examples/erdos-straus')
    assert status == 200 and data['console']['graphs']['formal']['graph']['nodes']


def test_shim_serves_only_reads_of_the_shared_run():
    shim = (shares.WEB / 'shared-console.js').read_text()
    # Writes and requests outside the shared run are refused locally.
    assert "method !== 'GET' && method !== 'HEAD'" in shim and 'return refused()' in shim
    assert "if (!target.path.startsWith('/api/')) return refused();" in shim
    assert 'window.fetch = sharedFetch' in shim


def test_request_log_hides_share_tokens_but_not_shared_asset_paths():
    seen = []
    handler = type('Handler', (), {'command': 'GET', 'request_version': 'HTTP/1.1',
                                   'log_message': lambda self, fmt, *args: seen.append(fmt % args)})()
    for path in ('/proof-abc/shared/' + TOKEN + '/monitor?view=dag', '/api/shared/' + TOKEN,
                 '/proof-abc/static/vendor/shared/katex.min.js', '/invite/abc'):
        handler.path = path
        console.Handler.log_request(handler, 200)
    assert TOKEN not in ' '.join(seen)
    assert seen[0].startswith('"GET /proof-abc/private-link/[redacted]/monitor ')
    assert seen[2].startswith('"GET /proof-abc/static/vendor/shared/katex.min.js ')
    assert seen[3].startswith('"GET /private-link/[redacted] ')


def test_deleting_a_run_ends_its_links_even_if_a_worker_writes_it_back(tmp_path, monkeypatch):
    cache = tmp_path / 'cache' / 'harness'
    cache.mkdir(parents=True)
    monkeypatch.setattr(jobs, '_cache_dir', lambda: cache)
    monkeypatch.setattr(jobs, 'RUNS_DIR', shares.RUNS_DIR)
    monkeypatch.setattr(shares, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(jobs, '_DELETED_RUNS', set())
    record = {'run_id': 'run-7', 'owner_id': 1, 'status': 'running', 'problem_text': 'P'}
    (cache / 'run-7.json').write_text(json.dumps(record))
    token = shares.create('run-7', 1, {'title': 'P'})['token']
    assert shares.lookup(token) == {'title': 'P', 'shared_at': shares.lookup(token)['shared_at']}
    jobs.delete_run('run-7')
    assert shares.lookup(token) is None and shares.owner_share('run-7', 1) is None
    # The still-running worker finishes after the delete: nothing comes back,
    # not even a workspace holding its closing chat lines.
    jobs._write_run({**record, 'status': 'finished'})
    jobs._append_chat('run-7', 'system', '✗ run stopped')
    assert not (cache / 'run-7.json').exists() and shares.lookup(token) is None
    assert not (shares.RUNS_DIR / 'workspaces' / 'run-7').exists()


def test_a_large_run_record_keeps_its_link_and_can_be_shared(tmp_path, monkeypatch):
    cache = tmp_path / 'cache' / 'harness'
    cache.mkdir(parents=True)
    agents = [{'name': f'step {i}', 'output': 'x' * 4000} for i in range(3000)]
    (cache / 'big.json').write_text(json.dumps({'run_id': 'big', 'agents': agents}))
    assert (cache / 'big.json').stat().st_size > 10_000_000
    token = shares.create('big', 1, {'title': 'Big'})['token']
    assert shares.lookup(token)['title'] == 'Big'
    # Sharing reads the large record; frequent polls (Projects) keep the 10 MB bound.
    assert shares.record('big', shares.RECORD_LIMIT)['run_id'] == 'big'
    assert shares.record('big') is None


def test_snapshot_limits_match_the_console_and_count_utf8_bytes():
    ws = shares.RUNS_DIR / 'workspaces' / 'run-3'
    ws.mkdir(parents=True)
    (ws / 'proof.md').write_text('p' * 390_000)
    steps = [{'name': f'step {i}', 'output': f'output {i}'} for i in range(150)]
    snap = shares.snapshot('run-3', {'problem_text': '∑' * 1000, 'agents': steps})
    assert len(snap['proof']) == 390_000
    # A long run keeps its first steps and its last ones (the final answer).
    labels = [agent['label'] for agent in snap['agents']]
    assert len(labels) == 100 and labels[:2] == ['step 0', 'step 1'] and labels[-1] == 'step 149'
    # Size counts UTF-8 bytes: 100 steps of 5,000 '∑' (shown twice: steps and the
    # execution graph) are 3 MB, but 6 MB as \u escapes.
    math = [{'name': f'step {i}', 'output': '∑' * 5_000} for i in range(100)]
    snap = shares.snapshot('run-3', {'problem_text': 'P', 'agents': math})
    assert len(json.dumps(snap).encode()) > 4_000_000 > len(json.dumps(snap, ensure_ascii=False).encode())


def test_hermes_tool_iterations_keep_their_calls_but_not_their_results():
    hermes = {'stage_name': 'iter-3 (read_file, terminal)', 'role': 'reader', 'pipeline_stage': 'understand',
              'tools': ['read_file', 'terminal'],
              'output': '▸ read_file({"path": "attachments/notes.txt"})\nPRIVATE NOTES\n\n▸ terminal({"command": "ls"})\nproof.md'}
    assert shares._agent_output(hermes) == ('▸ read_file({"path": "attachments/notes.txt"})\n▸ terminal({"command": "ls"})\n'
                                            '(tool output is not included in shared snapshots)')


def test_shares_made_before_version_3_are_sanitized_when_viewed():
    snap = {'version': 2, 'title': 'Old', 'status': 'finished', 'proof': 'P', 'agents': [
        {'id': 'r::2', 'label': 'turn-2 (exec)', 'summary': '$ cat attachments/a.txt\nPRIVATE ATTACHMENT'},
        {'id': 'r::3', 'label': 'iter-4 (read_file)', 'summary': '▸ read_file(x)\nPRIVATE FILE'},
        {'id': 'r::4', 'label': 'claude session summary', 'summary': 'edit · /home/ubuntu/w/proof.md\n✓ result · 2 turns · $0.0421 · ok'}]}
    outputs = [agent['output'] for agent in shares.console_view(snap)['run']['agents']]
    assert outputs == ['$ cat attachments/a.txt\n(tool output is not included in shared snapshots)',
                       '▸ read_file(x)\n(tool output is not included in shared snapshots)',
                       'edit · …/proof.md\n✓ result · 2 turns · … · ok']
    assert 'PRIVATE' not in json.dumps(shares.shared_payload(snap))


def test_lean_record_keeps_the_newest_attempts_their_count_and_axiom_probe(tmp_path):
    ws = shares.RUNS_DIR / 'workspaces' / 'run-4'
    ws.mkdir(parents=True)
    attempts = [{'action': 'repair', 'round': i, 'check': {'ok': False, 'status': 'failed'}} for i in range(30)]
    attempts.append({'action': 'check', 'round': 30, 'check': {'ok': True, 'status': 'verified', 'axioms': {
        'ok': True, 'decl': 'main', 'axioms': ['propext'], 'unexpected': [], 'error': ''}}})
    (ws / 'lean_verify.json').write_text(json.dumps({'status': 'verified', 'lean': 'theorem main : True := trivial',
                                                     'attempts': attempts,
                                                     'fidelity': {'flags': [{'lines': [3, 4], 'note': 'n'}]}}))
    (ws / 'proof.tex').write_text('\\section{Proof} $x=x$.')
    snap = shares.snapshot('run-4', {'problem_text': 'P'})
    lean = snap['lean_record']
    assert lean['attempt_count'] == 31 and len(lean['attempts']) == 24 and lean['attempts'][-1]['round'] == 30
    assert lean['attempts'][-1]['check']['axioms'] == {'ok': True, 'decl': 'main', 'axioms': ['propext'], 'unexpected': [], 'error': ''}
    assert lean['fidelity'] == {'flags': [{'lines': [3, 4], 'note': 'n'}]}
    view = shares.console_view(snap)
    assert view['run']['lean_verification']['attempt_count'] == 31
    # A LaTeX proof is shown as proof.tex, not as a proof.md file.
    preview = view['run']['sandbox_preview']
    assert preview['source'] == 'proof.tex' and preview['has_proof_tex'] and not preview['has_proof_md'] and view['files'] == []


def test_redaction_covers_file_urls():
    assert shares._redact('open file:///home/ubuntu/w/proof.md now') == 'open …/proof.md now'


def test_request_log_hides_a_share_token_with_trailing_text():
    seen = []
    handler = type('Handler', (), {'command': 'GET', 'request_version': 'HTTP/1.1',
                                   'log_message': lambda self, fmt, *args: seen.append(fmt % args)})()
    handler.path = '/shared/' + TOKEN + 'thanks'
    console.Handler.log_request(handler, 404)
    assert TOKEN not in seen[0]


def test_every_static_asset_a_served_page_links_exists():
    pages = list(shares.WEB.glob('*.html')) + [shares.DASHBOARD_WEB / 'index.html']
    for page in pages:
        for name in re.findall(r'(?:__BASE__|__ASSET_BASE__)?/static/([\w./-]+\.(?:css|js))', page.read_text()):
            assert (shares.WEB / name).is_file() or (shares.DASHBOARD_WEB / name).is_file(), (page.name, name)



def test_snapshots_carry_no_run_id_and_keep_step_links():
    ws = shares.RUNS_DIR / 'workspaces' / 'codex_adhoc_9f3a_abcdef0123'
    ws.mkdir(parents=True)
    (ws / 'proof.md').write_text('P')
    rid = 'codex_adhoc_9f3a_abcdef0123'
    record = {'run_id': rid, 'status': 'finished', 'engine': 'codex', 'problem_text': 'P',
              'agents': [{'trace_id': rid + '::turn-1', 'stage_name': 'turn-1 (message)', 'role': 'assistant', 'output': 'hi'},
                         {'trace_id': rid + '::turn-2', 'stage_name': 'turn-2 (message)', 'role': 'assistant',
                          'output': 'see /home/u/runs/workspaces/' + rid + '/proof.md'}],
              'edges': [{'from': rid + '::turn-1', 'to': rid + '::turn-2', 'type': 'next'}]}
    # A transcript line shortened inside the id, and Monitor's cache counts.
    record['agents'].append({'trace_id': rid + '::turn-3', 'stage_name': 'turn-3 (message)', 'role': 'assistant',
                             'output': 'tool · Write /home/u/runs/workspaces/' + rid[:20] + '…',
                             'input_tokens': 1388, 'cache_read_tokens': 2000, 'cache_write_tokens': 300})
    snap = shares.snapshot(rid, record)
    payload = json.dumps(shares.shared_payload(snap))
    assert rid not in json.dumps(snap) and rid not in payload
    assert rid[:12] not in json.dumps(snap) and 'Write …/shared…' in json.dumps(snap, ensure_ascii=False)
    turn = shares.console_view(snap)['run']['agents'][2]
    assert (turn['cache_read_tokens'], turn['cache_write_tokens']) == (2000, 300)
    run = shares.console_view(snap)['run']
    assert [agent['trace_id'] for agent in run['agents']] == ['shared::turn-1', 'shared::turn-2', 'shared::turn-3']
    assert run['edges'] == [{'from': 'shared::turn-1', 'to': 'shared::turn-2', 'type': 'next'}]


def test_older_shares_clean_their_explore_graph_text_and_ids():
    rid = 'hermes_adhoc_21758d22_99cf6a0431'
    snap = {'version': 2, 'title': 'Old', 'status': 'finished', 'agents': [{'id': rid + '::iter-1', 'label': 'iter-1 (write_file)', 'summary': '▸ write_file(x)\nPRIVATE'}],
            'graphs': {'explore': {'kind': 'explore', 'status': 'recorded', 'graph_role': 'execution', 'graph': {
                'nodes': [{'id': rid + '::iter-1', 'label': 'iter-1 (write_file)',
                           'statement': '▸ write_file({"path": "/home/ubuntu/w/' + rid + '/proof.md"})\n{"bytes_written": 617, "PRIVATE": 1}'}],
                'edges': []}}}}
    view = shares.console_view(snap)
    node = view['graphs']['engine:workflow']['graph']['nodes'][0]
    assert node['id'] == 'shared::iter-1'
    assert node['statement'] == '▸ write_file({"path": "…/proof.md"})\n(tool output is not included in shared snapshots)'
    assert rid not in json.dumps(view) and 'PRIVATE' not in json.dumps(view)



def test_mathematics_starting_with_a_formula_is_never_taken_for_tool_output():
    math = '$ m+n $ is even.\nProof: write m = 2a and n = 2b.'
    assert shares._agent_output({'role': 'assistant', 'output': math}) == math
    snap = {'version': 2, 'title': 'Old', 'status': 'finished', 'agents': [],
            'graphs': {'outline': {'kind': 'outline', 'status': 'parsed', 'graph_role': 'proof-outline',
                                   'graph': {'nodes': [{'id': 'a', 'label': 'Lemma (tool)', 'statement': math}], 'edges': []}}}}
    assert shares.console_view(snap)['graphs']['informal']['graph']['nodes'][0]['statement'] == math


def test_shares_withhold_provider_errors_from_stage_notes_old_links_included():
    auth_error = ('Claude Code subscription authentication failed: OAuth session expired; '
                  'reconnect Claude Code in Settings (the linked account needs a new sign-in) and retry.')
    state = {'status': 'done', 'stages': {
        'informal_dag': {'status': 'done', 'detail': 'Built structurally; semantic model analysis failed: ' + auth_error},
        'lean': {'status': 'error', 'detail': auth_error},
        'formal_dag': {'status': 'done', 'detail': '12 Lean nodes · shared formal DAG'},
    }}
    view = shares._pipeline_view(state)
    assert 'OAuth' not in json.dumps(view) and 'Settings' not in json.dumps(view)
    assert view['stages']['informal_dag']['detail'].startswith('Built structurally; semantic model analysis failed (details')
    assert view['stages']['lean'] == {'status': 'error', 'detail': shares._WITHHELD_STAGE}
    assert view['stages']['formal_dag']['detail'] == '12 Lean nodes · shared formal DAG'
    # A link made before errors were withheld stored them raw: they are withheld when viewed.
    stored = {'version': 3, 'title': 'P', 'proof': 'x', 'status': 'failed', 'run_view': {
        'status': 'failed', 'engine': 'claude', 'error': 'Claude Code exited with code 1: ' + auth_error,
        'auto_pipeline': {'status': 'done', 'stages': {'lean': {'status': 'error', 'detail': auth_error}}}}}
    payload = json.dumps(shares.shared_payload(stored))
    assert 'OAuth' not in payload and 'reconnect' not in payload
    run = shares.console_view(stored)['run']
    assert run['error'] == shares._WITHHELD_ERROR and run['status'] == 'failed'
