#!/usr/bin/env python3
"""Opt-in browser smoke test using only synthetic snapshots and loopback assets.

Requires Playwright for Python and an installed Chromium, for example:
    python tests/shared_viewer_browser.py --chromium /path/to/chromium
No app database, credentials, saved runs, or model/checker calls are used.
"""
import argparse
import ast
import asyncio
import json
import mimetypes
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'agent_monitor' / 'web'


def graph(role, nodes, edges=(), **extra):
    return {'status': 'ready', 'graph_role': role, 'graph': {'nodes': nodes, 'edges': list(edges)}, **extra}


PROOF = r'''# Synthetic proof
**Readable mathematics:** for $n \in \mathbb{N}$, consider
\begin{equation} a+b=b+a. \end{equation}
\begin{align} x &= 1 \\ y &= 2. \end{align}

<script>window.smokeExecuted = true</script>
<img src="https://example.invalid/pixel" onerror="window.smokeExecuted = true">
<a href="javascript:window.smokeExecuted=true">Unsafe link</a>
'''
FACTS = [
    {'id': 'lemma', 'kind': 'fact', 'label': 'Lemma', 'statement': '$a+b=b+a$.', 'meta': {'verifier_accepted': True}},
    {'id': 'target', 'kind': 'fact', 'label': 'Target', 'statement': 'The stated result.', 'meta': {'is_target': True, 'verifier_accepted': True}},
]
EDGE = {'from': 'lemma', 'to': 'target', 'label': 'fact dependency'}
BASE = {'version': 2, 'title': 'Synthetic shared run', 'status': 'finished', 'problem': 'A test statement.', 'proof': PROOF, 'agents': []}
SNAPSHOTS = {
    'v2': {**BASE, 'graphs': {
        'outline': graph('outline', FACTS, [EDGE]),
        'explore': graph('workflow', [
            {'id': 'goal', 'kind': 'goal', 'label': 'Root problem', 'meta': {'workflow_kind': 'goal', 'status': 'closed'}},
            {'id': 'plan', 'kind': 'plan', 'label': 'Plan', 'meta': {'workflow_kind': 'plan', 'status': 'closed'}},
        ], [{'from': 'goal', 'to': 'plan', 'label': 'decomposes-into'}], selection={'target_ids': ['goal']}),
        'facts': graph('fact-dependency', FACTS, [EDGE]),
    }},
    'v1': {**BASE, 'version': 1, 'informal_graph': {'nodes': FACTS, 'edges': [EDGE]}},
    'analysis': {**BASE, 'kind': 'analysis', 'graphs': {'manuscript': graph('analysis', [
        {'id': 'premise', 'kind': 'lemma', 'label': 'Unchecked premise', 'verdict': 'not_checked', 'closure': 'not checked'},
        {'id': 'main', 'kind': 'theorem', 'label': 'Main result', 'verdict': 'checked', 'closure': 'not checked', 'statement': '$x=x$.'},
    ], [{'from': 'premise', 'to': 'main', 'provenance': 'model'}], dependency_basis='model')}},
}


@contextmanager
def preview_server():
    # Read the production policy literal so a CSP change is exercised here too.
    tree = ast.parse((ROOT / 'agent_monitor' / 'run_sharing.py').read_text())
    csp = next(value.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
               for key, value in zip(node.keys, node.values)
               if isinstance(key, ast.Constant) and key.value == 'Content-Security-Policy'
               and isinstance(value, ast.Constant) and isinstance(value.value, str))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            name = path.rsplit('/', 1)[-1]
            if path.startswith('/shared/') and name in SNAPSHOTS:
                data = (WEB / 'run-viewer.html').read_text().replace('__DATA_URL__', '/snapshot/' + name).replace('__ASSET_BASE__', '/static').replace('__CONSOLE_URL__', '/')
                body, mime = data.encode(), 'text/html; charset=utf-8'
            elif path.startswith('/snapshot/') and name in SNAPSHOTS:
                body, mime = json.dumps(SNAPSHOTS[name]).encode(), 'application/json'
            elif path.startswith('/static/'):
                file = (WEB / path.removeprefix('/static/')).resolve()
                if not file.is_relative_to(WEB) or not file.is_file():
                    self.send_error(404)
                    return
                body, mime = file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            for key, value in {'Content-Type': mime, 'Content-Length': str(len(body)), 'Content-Security-Policy': csp, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def run(chromium):
    with preview_server() as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1280, 'height': 900})
            errors, unsafe_requests, csp_errors = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: csp_errors.append(message.text)
                    if 'Content Security Policy' in message.text or 'Refused to' in message.text else None)

            async def guard(route):
                request = route.request
                if request.method != 'GET' or urlsplit(request.url).netloc != urlsplit(base).netloc:
                    unsafe_requests.append((request.method, request.url))
                    await route.abort()
                else:
                    await route.continue_()
            await page.route('**/*', guard)
            await page.goto(base + '/shared/v2')
            await page.wait_for_selector('#rv-workspace[aria-busy="false"]')
            proof = page.locator('#rv-proof-text')
            assert await proof.locator('strong').inner_text() == 'Readable mathematics:'
            assert await proof.locator('.katex').count() >= 3
            assert await proof.locator('.katex-display').count() >= 2
            assert await proof.locator('.katex-error, script, img, [onerror], [onclick], a[href^="javascript:"]').count() == 0
            assert not await page.evaluate('Boolean(window.smokeExecuted)')

            await page.locator('[data-tab="dag"]').click()
            assert await page.locator('.sg-title').inner_text() == 'Explore'
            await page.locator('[data-graph-mode="outline"]').click()
            assert await page.locator('.sg-title').inner_text() == 'Informal outline'
            await page.locator('[data-graph-mode="engine"]').click()
            await page.locator('[data-engine-mode="facts"]').click()
            assert await page.locator('.sg-title').inner_text() == 'Facts'
            await page.locator('.sg-search').fill('Lemma')
            await page.locator('.sg-search').press('Enter')
            assert await page.locator('.sg-inspector').is_visible()
            await page.keyboard.press('Escape')
            await page.get_by_role('button', name='Switch to node list', exact=True).click()
            assert await page.locator('.sg-list .sg-node:visible').count() == 1

            await page.goto(base + '/shared/v1?view=facts')
            await page.wait_for_selector('.sg-empty')
            assert 'owner can update the shared snapshot' in await page.locator('.sg-empty').inner_text()
            assert await page.locator('.sg-node').count() == 0
            await page.goto(base + '/shared/analysis?view=manuscript')
            card = page.locator('.sg-nodes .sg-node').filter(has_text='Main result')
            await card.click()
            assert 'with deps: not checked' in await card.inner_text()
            detail = await page.locator('.sg-detail-body').inner_text()
            assert 'Local verdict\nchecked' in detail and 'With dependencies\nnot checked' in detail
            assert 'Model-proposed links do not establish dependencies' in detail
            await page.set_viewport_size({'width': 390, 'height': 844})
            for mode in ['sandbox', 'explore', 'facts', 'outline']:
                await page.goto(base + '/shared/v2?view=' + mode)
                await page.wait_for_selector('#rv-workspace[aria-busy="false"]')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), mode
            await browser.close()
            assert not errors, errors
            assert not unsafe_requests, unsafe_requests
            assert not csp_errors, csp_errors
            print('PASS: graph modes, legacy notice, search/list/inspector, verdicts, sanitizer, math, mobile layout, GET-only local requests.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium', help='Chromium executable; defaults to the Playwright installation.')
    asyncio.run(run(parser.parse_args().chromium))
