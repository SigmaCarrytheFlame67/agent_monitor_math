#!/usr/bin/env python3
"""Opt-in browser smoke test of shared runs (the console's own read-only view).

Runs the real console handler on loopback with synthetic snapshots only (no
account database, credentials, saved runs, models or checkers) and checks, in
Chromium, that a shared page renders every tab, makes no third-party request,
sends nothing but GETs for its own snapshot, and never executes markup that a
snapshot carries.

Requires Playwright for Python and an installed Chromium, for example:
    python tests/shared_viewer_browser.py --chromium /path/to/chromium
"""
import argparse
import asyncio
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('AGENT_MONITOR_DATA_DIR', tempfile.mkdtemp(prefix='shared-viewer-'))
os.environ.setdefault('AGENT_MONITOR_ENV_PATH', os.path.join(os.environ['AGENT_MONITOR_DATA_DIR'], 'server.env'))

from playwright.async_api import async_playwright  # noqa: E402

from agent_monitor import console_server as console, run_sharing as shares  # noqa: E402

ATTACK = '<img src=x onerror="window.ATTACK=1"><script>window.ATTACK=1</script><svg onload="window.ATTACK=1"></svg>'
PROOF = '# Proof\n\n**Readable mathematics:** for $n \\in \\mathbb{N}$, $$a+b=b+a.$$\n\n' + ATTACK + '\n[x](javascript:window.ATTACK=1)\n'


def graph(role, nodes, edges=(), **extra):
    return {'status': 'recorded', 'graph_role': role, 'graph': {'nodes': nodes, 'edges': list(edges)}, **extra}


FACTS = [
    {'id': 'lemma', 'kind': 'fact', 'label': 'Lemma ' + ATTACK, 'statement': '$a+b=b+a$. ' + ATTACK, 'meta': {'verifier_accepted': True}},
    {'id': 'target', 'kind': 'fact', 'label': 'Target', 'statement': 'The stated result.', 'meta': {'is_target': True, 'verifier_accepted': True}},
]
EDGE = {'from': 'lemma', 'to': 'target', 'label': 'fact dependency'}
SNAPSHOTS = {
    'P' * 43: {
        'version': 3, 'type': 'shared', 'title': 'Synthetic ' + ATTACK, 'status': 'finished', 'engine': 'math_harness',
        'problem': 'Prove it. ' + ATTACK, 'proof': PROOF, 'lean_source': 'theorem t : True := trivial -- ' + ATTACK,
        'agents': [{'label': 'Strategist ' + ATTACK, 'status': 'completed', 'model': 'm' + ATTACK, 'summary': 'plan ' + ATTACK,
                    'stage_name': '2 Explore ' + ATTACK, 'role': 'reasoning'}],
        'run_view': {'totals': {'input_tokens': 10, 'output_tokens': 5, 'agents': 1}, 'live': {'model': 'm' + ATTACK},
                     'pipeline': [{'id': 'work', 'label': '2 Explore', 'title': 'Search ' + ATTACK}],
                     'auto_pipeline': {'status': 'done', 'proof_engine': 'math_harness' + ATTACK,
                                       'stages': {'informal_dag': {'status': 'done', 'detail': ATTACK}}}},
        'lean_record': {'status': 'verified', 'lean': 'theorem t : True := trivial', 'log': 'ok ' + ATTACK, 'attempts': [],
                        'notes': ATTACK, 'lean_path': 'lean/Proof.lean'},
        'graphs': {
            'outline': graph('proof-outline', FACTS, [EDGE]),
            'explore': graph('workflow', [
                {'id': 'goal', 'kind': 'goal', 'label': 'Root ' + ATTACK, 'meta': {'workflow_kind': 'goal', 'status': 'closed'}},
                {'id': 'plan', 'kind': 'plan', 'label': 'Plan', 'meta': {'workflow_kind': 'plan', 'status': 'closed'}},
            ], [{'from': 'goal', 'to': 'plan', 'label': 'decomposes-into'}], selection={'target_ids': ['goal']}),
            'facts': graph('fact-dependency', FACTS, [EDGE]),
            'formal': graph('formal-structure', [{'id': 't', 'label': 't', 'lean_kind': 'theorem'}]),
        },
    },
    'A' * 43: {
        'version': 3, 'type': 'shared', 'kind': 'analysis', 'title': 'Analyze · Paper', 'status': 'finished', 'engine': 'analyze',
        'proof': '# Report\n\n' + ATTACK, 'source_url': 'https://example.org/paper',
        'analysis': {'summary': {'nodes': 2, 'checked': 1, 'not_checked': 1, 'coverage': {'checked': 1, 'total': 2}}},
        'graphs': {'manuscript': graph('manuscript-dependency', [
            {'id': 'main', 'kind': 'theorem', 'label': 'Main result ' + ATTACK, 'verdict': 'checked',
             'status': 'checked by a model verifier', 'statement': '$x=x$. ' + ATTACK, 'main_result': True},
            {'id': 'lem', 'kind': 'lemma', 'label': 'Lemma 1', 'verdict': 'not_checked', 'status': 'not checked'},
        ], [{'from': 'lem', 'to': 'main', 'label': 'uses', 'provenance': 'reference'}])},
    },
}


@contextmanager
def console_server():
    shares.lookup = lambda token: SNAPSHOTS.get(token)
    server = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def run(chromium):
    with console_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors, unsafe, csp = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: csp.append(message.text)
                    if 'Content Security Policy' in message.text or 'Refused to' in message.text else None)

            async def guard(route):
                request = route.request
                # Only GETs to this origin; the only API request is the snapshot.
                if (request.method != 'GET' or not request.url.startswith(base + '/')
                        or ('/api/' in request.url and '/api/shared/' not in request.url)):
                    unsafe.append((request.method, request.url))
                    await route.abort()
                else:
                    await route.continue_()
            await page.route('**/*', guard)

            await page.goto(base + '/shared/' + 'P' * 43)
            await page.wait_for_selector('#md-view .katex')
            assert 'Readable mathematics' in await page.locator('#md-view').inner_text()
            assert await page.locator('#md-view .katex-display').count() >= 1
            assert await page.locator('#shared-notice').is_visible()
            for hidden in ('#chat-input', '#proof-edit-btn', '#new-proof-btn', '#settings-btn', '#sess-cont'):
                assert not await page.locator(hidden).is_visible(), hidden
            for view in ('dag', 'lean', 'monitor', 'sandbox'):
                await page.locator(f'.seg-control button[data-view="{view}"]').click()
                await page.wait_for_timeout(700)
            await page.locator('.seg-control button[data-view="dag"]').click()
            await page.wait_for_selector('#dag-nodes .dag-node')
            await page.locator('#dag-engine-view button[data-engine-view="facts"]').click()
            await page.wait_for_timeout(500)
            await page.locator('#dag-nodes .dag-node').first.click()
            await page.wait_for_timeout(300)
            assert await page.locator('#lean-code').evaluate('el => el.readOnly')

            await page.goto(base + '/shared/' + 'A' * 43 + '?view=dag')
            await page.wait_for_selector('#dag-nodes .dag-node')
            assert 'Main result' in await page.locator('#dag-nodes').inner_text()

            await page.goto(base + '/shared/' + 'U' * 43)
            await page.wait_for_selector('.shared-unavailable')

            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.goto(base + '/shared/' + 'P' * 43)
            await page.wait_for_selector('#md-view .katex')
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            attacked = await page.evaluate('Boolean(window.ATTACK)')
            await browser.close()
            assert not attacked, 'snapshot markup executed'
            assert not errors, errors
            assert not unsafe, unsafe
            assert not csp, csp
            print('PASS: console tabs and DAG modes from snapshots, read-only controls, unavailable link, mobile width, '
                  'no executed snapshot markup, GET-only snapshot requests, no CSP violations.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', help='Chromium executable; defaults to the Playwright installation.')
    asyncio.run(run(parser.parse_args().chromium))
