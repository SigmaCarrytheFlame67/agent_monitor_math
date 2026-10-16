"""Shared graph snapshots stay separate, bounded, offline and revocable."""
from __future__ import annotations

import json
import hashlib

import pytest

from agent_monitor import engine_graph, lean_kg, proof_graph, run_sharing as shares
from agent_monitor.analyze import graph as analysis_graph
from test_engine_graph import _fact_text, _project, _write_legacy_generation


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(shares, 'RUNS_DIR', tmp_path / 'runs')
    monkeypatch.setattr(shares, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.delenv('LLM_DASHBOARD_CACHE', raising=False)
    ws = shares.RUNS_DIR / 'workspaces' / 'run-1'
    ws.mkdir(parents=True)
    return ws


def forbidden(*args, **kwargs):
    pytest.fail('Snapshot generation invoked a model, checker, or cache writer')


@pytest.fixture(autouse=True)
def no_paid_or_mutating_graph_operations(monkeypatch):
    monkeypatch.setattr(proof_graph, 'generate', forbidden)
    monkeypatch.setattr(proof_graph, '_write_cached', forbidden)
    monkeypatch.setattr(engine_graph, 'rebuild', forbidden)
    monkeypatch.setattr(lean_kg, 'extract_snapshot', forbidden)


def test_real_stored_engine_graphs_are_separate_from_outline_and_formal(workspace):
    _write_legacy_generation(workspace)
    (workspace / 'proof_graph.json').write_text(json.dumps({'graph': {
        'title': 'Proof outline', 'nodes': [{'id': 'outline-only', 'statement': 'Proof sketch.'}], 'edges': [],
    }}))
    (workspace / 'lean').mkdir()
    (workspace / 'lean' / 'Proof.lean').write_text('theorem main : True := True.intro\n')
    before = {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    result = shares.snapshot('run-1', {'engine': 'math_harness', 'problem_text': 'An even integer.'})
    graphs = result['graphs']
    assert result['version'] == 3 and set(graphs) == set(shares.GRAPH_MODES)
    assert graphs['outline']['graph']['nodes'][0]['id'] == 'outline-only'
    assert graphs['facts']['graph_role'] == 'fact-dependency'
    assert graphs['explore']['graph_role'] == 'workflow'
    assert graphs['explore']['selection']['target_ids'] == ['goal:root']
    assert graphs['facts']['graph']['nodes'] and graphs['explore']['graph']['nodes']
    assert graphs['facts']['graph']['nodes'] != graphs['explore']['graph']['nodes']
    assert graphs['facts']['provenance']['certificate'] is False
    assert graphs['formal']['graph']['nodes'][0]['label'] == 'main'
    assert graphs['compiler']['status'] == graphs['manuscript']['status'] == 'unavailable'
    assert result['informal_graph'] == graphs['outline']['graph']
    assert result['formal_graph'] == graphs['formal']['graph']
    assert before == {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}


def test_plain_proof_never_becomes_facts_or_cgm_exploration(workspace):
    (workspace / 'proof.md').write_text('Let a and b be even. Their sum is even.')
    result = shares.snapshot('run-1', {'engine': 'plain', 'agents': [{'summary': 'Some execution activity'}]})
    assert result['graphs']['outline']['model'] == 'structural-text-parse'
    for mode in ('facts', 'compiler', 'manuscript'):
        assert result['graphs'][mode]['status'] == 'unavailable'
        assert result['graphs'][mode]['graph']['nodes'] == []
    assert result['graphs']['explore']['graph_role'] == 'execution'
    assert result['graphs']['explore']['provenance']['proof_logic'] is False
    assert 'Some execution activity' in result['graphs']['explore']['graph']['nodes'][0]['statement']


def test_cgm_outline_projects_facts_when_no_saved_outline_exists(workspace):
    _write_legacy_generation(workspace)
    result = shares.snapshot('run-1', {'engine': 'math_harness'})
    assert result['graphs']['outline']['model'] == 'cgm-fact-graph'
    assert {node['id'] for node in result['graphs']['outline']['graph']['nodes']} == {
        node['id'] for node in result['graphs']['facts']['graph']['nodes']}


def test_cached_cgm_outline_refreshes_from_current_facts_without_writes(workspace):
    project, facts = _project(workspace)
    old_id, old_payload = _fact_text(problem_id='demo', statement='The previous target.', proof='The previous proof.')
    old_path = facts / (old_id + '.md')
    old_path.write_text(old_payload)
    (project / 'TARGET.md').write_text(old_id + '\n')
    cached = proof_graph.fact_graph_result(workspace)
    assert cached and cached['graph']['nodes'][0]['id'] == old_id
    (workspace / 'proof_graph.json').write_text(json.dumps(cached))

    new_id, new_payload = _fact_text(problem_id='demo', statement='The current target.', proof='The current proof.')
    old_path.unlink()
    (facts / (new_id + '.md')).write_text(new_payload)
    (project / 'TARGET.md').write_text(new_id + '\n')
    (workspace / 'proof.md').write_text('The current proof.')
    before = {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    result = shares.snapshot('run-1', {'engine': 'math_harness'})
    outline, actual = result['graphs']['outline'], result['graphs']['facts']
    assert outline['model'] == proof_graph.FACT_GRAPH_MODEL
    assert [node['id'] for node in outline['graph']['nodes']] == [new_id]
    assert [node['id'] for node in actual['graph']['nodes']] == [new_id]
    assert old_id not in json.dumps(result)
    assert before == {str(p.relative_to(workspace)): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}


@pytest.mark.parametrize('source_hash', [None, '', []])
def test_formal_cache_without_source_hash_cannot_override_current_lean(workspace, source_hash):
    (workspace / 'lean').mkdir()
    lean = 'theorem current_target : True := True.intro\n'
    (workspace / 'lean' / 'Proof.lean').write_text(lean)
    stale = {'model': 'saved-model-outline', 'graph': {'nodes': [{'id': 'obsolete_target'}], 'edges': []}}
    if source_hash is not None:
        stale['source_sha256'] = source_hash
    cache_path = workspace / 'lean_proof_graph.json'
    cache_path.write_text(json.dumps(stale))
    before = cache_path.read_bytes()
    result = shares.snapshot('run-1', {'engine': 'plain'})
    formal = result['graphs']['formal']
    assert formal['model'] == 'lean-parse'
    assert [node['label'] for node in formal['graph']['nodes']] == ['current_target']
    assert 'obsolete_target' not in json.dumps(result)
    assert cache_path.read_bytes() == before


def test_missing_fact_artifacts_and_forged_compiler_cache_fail_closed(workspace):
    (workspace / 'proof_graph.json').write_text(json.dumps({'graph': {
        'nodes': [{'id': 'sketch', 'statement': 'An informal sketch.'}], 'edges': [],
    }}))
    (workspace / 'lean_proof_graph.json').write_text(json.dumps({'model': 'lean-compiler', 'graph': {
        'nodes': [{'id': 'decl', 'statement': 'A stored formal declaration.'}], 'edges': [],
        'provenance': {'kind': 'lean-compiler'},
    }, 'compiler_graph': {'nodes': [{'id': 'forged'}], 'edges': []}}))
    result = shares.snapshot('run-1', {'engine': 'math_harness'})
    assert result['graphs']['facts']['status'] == result['graphs']['explore']['status'] == 'unavailable'
    assert result['graphs']['compiler']['status'] == 'unavailable'
    assert result['graphs']['formal']['graph']['nodes'][0]['id'] == 'decl'


def test_only_prevalidated_source_bound_compiler_metadata_is_projected(workspace, monkeypatch):
    lean = 'lemma premise : True := True.intro\ntheorem main : True := premise\n'
    (workspace / 'lean').mkdir()
    (workspace / 'lean' / 'Proof.lean').write_text(lean)
    (workspace / 'lean_kg_snapshot.json').write_text('{}')
    (workspace / 'lean_verify.json').write_text('{}')
    (workspace / 'lean_proof_graph.json').write_text(json.dumps({
        'source_sha256': '0' * 64, 'graph': {'nodes': [{'id': 'stale'}], 'edges': []},
    }))
    calls = []

    def validated_loader(ws, *, source):
        # load_snapshot owns attestation validation. The sharing layer must
        # supply the exact selected source and only consume a successful load.
        calls.append((ws, source))
        return {'schema_version': lean_kg.SCHEMA_VERSION, 'producer': lean_kg.PRODUCER,
                'status': 'ok', 'sandbox': next(iter(lean_kg.TRUSTED_SANDBOXES)),
                'source_sha256': hashlib.sha256(lean.encode()).hexdigest(),
                'normalized_source_sha256': lean_kg.normalized_source_sha256(lean),
                'private_path': '/PRIVATE/ARTIFACT', 'proof_edge_count': 1,
                'declarations': [
                    {'line': 1, 'matched': True, 'qualified_name': 'premise', 'local_proof_uses': []},
                    {'line': 2, 'matched': True, 'qualified_name': 'main', 'local_proof_uses': ['premise']},
                ]}

    monkeypatch.setattr(lean_kg, 'load_snapshot', validated_loader)
    result = shares.snapshot('run-1', {'engine': 'plain'})
    assert calls == [(workspace, lean)]
    compiler = result['graphs']['compiler']
    assert compiler['status'] == 'recorded' and compiler['model'] == 'lean-compiler'
    assert compiler['graph']['edges'] == [{'from': 'premise', 'to': 'main', 'label': 'proof uses'}]
    assert 'stale' not in json.dumps(result) and 'PRIVATE' not in json.dumps(result)
    assert result['graphs']['formal']['model'] == 'lean-parse'


def test_allowlist_bounds_graph_metadata_and_drops_unknown_fields():
    node = {'id': 'n', 'statement': 'A mathematical claim', 'api_key': 'DO-NOT-SHARE',
            'meta': {'workflow_kind': 'proof_attempt', 'verdict': 'accepted', 'depth': 2,
                     'score': 0.75, 'proof_excerpt': 'A public mathematical proof.',
                     'system_prompt': 'DO-NOT-SHARE', 'author': 'PRIVATE-ACCOUNT',
                     'private_path': '/PRIVATE/FILE', 'provider_request': {'secret': 'DO-NOT-SHARE'}}}
    source = {'source': '/PRIVATE/FILE', 'request': 'DO-NOT-SHARE', 'graph': {
        'nodes': [node, {'id': 'n', 'statement': 'Duplicate'}, {'id': ''}],
        'edges': [{'from': 'n', 'to': 'missing'}, {'from': 'n', 'to': 'n', 'label': 'uses', 'prompt': 'DO-NOT-SHARE'}],
    }, 'provenance': {'semantics': 'exploration-workflow', 'credentials': 'DO-NOT-SHARE'}}
    result = shares._graph_record('explore', source)
    rendered = json.dumps(result)
    assert 'DO-NOT-SHARE' not in rendered and 'PRIVATE' not in rendered
    assert len(result['graph']['nodes']) == 1 and len(result['graph']['edges']) == 1
    assert result['graph']['nodes'][0]['meta']['score'] == 0.75
    assert result['graph']['nodes'][0]['meta']['proof_excerpt'] == 'A public mathematical proof.'
    oversized = {'nodes': [{'id': str(n), 'statement': 'x' * 21_000} for n in range(105)], 'edges': []}
    bounded = shares.graph_snapshot(oversized)
    assert len(bounded['nodes']) == 100 and len(bounded['nodes'][0]['statement']) == 20_000
    bounded_record = shares._graph_record('outline', oversized)
    assert bounded_record['truncated'] is True
    assert bounded_record['counts'] == {'nodes': 105, 'edges': 0, 'shown_nodes': 100, 'shown_edges': 0}
    assert shares.graph_snapshot({'nodes': {}, 'edges': {}})['nodes'] == []


def test_nested_symlinks_and_workspace_symlinks_are_not_followed(workspace, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'Proof.lean').write_text('PRIVATE-CREDENTIAL')
    (private / 'proof.md').write_text('PRIVATE-CREDENTIAL')
    (workspace / 'lean').symlink_to(private, target_is_directory=True)
    (workspace / 'proof.md').symlink_to(private / 'proof.md')
    result = shares.snapshot('run-1', {'engine': 'plain'})
    assert result['proof'] == result['lean_source'] == ''
    assert 'PRIVATE-CREDENTIAL' not in json.dumps(result)
    (workspace.parent / 'linked-run').symlink_to(private, target_is_directory=True)
    assert shares.snapshot('linked-run', {'engine': 'math_harness'})['proof'] == ''
    with pytest.raises(ValueError):
        shares.snapshot('../private', {})


def test_analysis_manuscript_keeps_verdicts_without_source_notes(workspace):
    path = workspace / 'analysis'
    path.mkdir()
    graph = {'schema': analysis_graph.SCHEMA, 'source': {'title': 'Paper'},
             'dependency_basis': 'structure', 'nodes': [
                 {'id': 'lemma', 'kind': 'lemma', 'label': 'Lemma', 'statement': 'n+n=2n',
                  'meta': {'verdict': 'checked', 'closure': 'checked', 'interpreted': True,
                           'main_result': True, 'proof_excerpt': 'PRIVATE-SOURCE-PROOF',
                           'notes': 'PRIVATE-NOTES', 'source_ref': {'file': '/PRIVATE/PATH'}}},
             ], 'edges': []}
    (path / 'analysis_graph.json').write_text(json.dumps(graph))
    (path / 'source').mkdir()
    (path / 'source' / 'manuscript.tex').write_text('PRIVATE-MANUSCRIPT')
    (path / 'report.md').write_text('One model-checked statement.')
    # Generic saved graphs may have rich metadata. Analyze must never use it.
    (workspace / 'lean_proof_graph.json').write_text(json.dumps({'graph': {
        'nodes': [{'id': 'decl', 'meta': {'proof_excerpt': 'PRIVATE-LEAN-NOTE'}}], 'edges': [],
    }}))
    result = shares.snapshot('run-1', {'engine': 'analyze', 'agents': [{'summary': 'PRIVATE-TRANSCRIPT'}]})
    node = result['graphs']['manuscript']['graph']['nodes'][0]
    assert node['verdict'] == 'checked' and node['main_result'] is True
    assert node['status'] == 'checked by a model verifier'
    assert result['graphs']['outline']['graph'] == result['graphs']['manuscript']['graph']
    assert result['agents'] == [] and 'PRIVATE' not in json.dumps(result)
    assert result['graphs']['facts']['status'] == result['graphs']['explore']['status'] == 'unavailable'


def test_symlinked_analysis_directory_is_not_followed(workspace, tmp_path):
    outside = tmp_path / 'other-analysis'
    outside.mkdir()
    (outside / 'report.md').write_text('PRIVATE-REPORT')
    (workspace / 'analysis').symlink_to(outside, target_is_directory=True)
    result = shares.snapshot('run-1', {'engine': 'analyze'})
    assert result['proof'] == '' and result['graphs']['manuscript']['status'] == 'unavailable'


def test_legacy_share_stays_frozen_and_only_owner_refresh_adds_graphs(workspace, monkeypatch):
    (shares.RUNS_DIR / 'run-1.json').write_text(json.dumps({'engine': 'plain'}))
    old = {'version': 1, 'type': 'shared', 'proof': 'Earlier proof',
           'informal_graph': {'nodes': [], 'edges': []}}
    token = shares.create('run-1', 7, old)['token']
    (workspace / 'proof.md').write_text('An updated proof with newly stored content.')
    fetched = shares.lookup(token)
    assert fetched['version'] == 1 and fetched['proof'] == 'Earlier proof' and 'graphs' not in fetched
    refreshed = shares.create('run-1', 7, shares.snapshot('run-1', {'engine': 'plain'}))
    assert refreshed['token'] == token and shares.lookup(token)['version'] == 3
    shares.revoke('run-1', 8)
    assert shares.lookup(token) is not None
    shares.revoke('run-1', 7)
    assert shares.lookup(token) is None


def test_missing_source_disables_snapshot_without_workspace_rebuild(workspace, monkeypatch):
    (shares.RUNS_DIR / 'run-1.json').write_text('{"engine":"plain"}')
    created = shares.create('run-1', 7, shares.snapshot('run-1', {'engine': 'plain'}))
    monkeypatch.setattr(engine_graph, 'build', forbidden)
    assert shares.lookup(created['token']) is not None
    (shares.RUNS_DIR / 'run-1.json').unlink()
    assert shares.lookup(created['token']) is None
