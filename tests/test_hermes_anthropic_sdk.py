"""The bundled Hermes engine and the console share one Anthropic SDK.

Agent Monitor pins anthropic 1.13.0 for its own Claude API calls (Max (API)).
Hermes runs in the same venv: its on-demand installer must never reinstall
another version over it (anthropic 0.87.0 broke the console's calls), and its
Anthropic client must work with that SDK (an httpx Timeout made every request
fail with "Connection error." once openai was imported).  Offline: loopback
only.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "engines" / "hermes_core"


def _console_pin() -> str:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    [pin] = [dep for dep in deps if re.fullmatch(r"anthropic==[\d.]+", dep.strip())]
    return pin.strip()


class HermesAnthropicPinTests(unittest.TestCase):
    def test_hermes_requires_exactly_the_console_sdk(self) -> None:
        pin = _console_pin()
        tree = ast.parse((HERMES / "tools" / "lazy_deps.py").read_text(encoding="utf-8"))
        [lazy] = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LAZY_DEPS"
        ]
        self.assertEqual(ast.literal_eval(lazy)["provider.anthropic"], (pin,))
        extras = tomllib.loads((HERMES / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]
        self.assertEqual(extras["anthropic"], [pin])

    def test_a_hermes_anthropic_client_works_on_the_console_sdk(self) -> None:
        # In a child process: Hermes's modules expect its own import root.
        program = r"""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, sys.argv[1])
import openai  # noqa: F401  (Hermes imports it first; it re-exports httpx's Timeout)
from agent import anthropic_adapter

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        raw = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
                          "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
                          "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

server = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=server.serve_forever, daemon=True).start()
client = anthropic_adapter.build_anthropic_client(
    "sk-ant-api03-test-not-a-key", base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=30)
reply = client.messages.create(model="claude-test", max_tokens=16, messages=[{"role": "user", "content": "ping"}])
print(json.dumps({"text": reply.content[0].text}))
server.shutdown()
"""
        env = {"PATH": "/usr/bin:/bin", "HERMES_DISABLE_LAZY_INSTALLS": "1", "NO_PROXY": "*", "no_proxy": "*"}
        result = subprocess.run([sys.executable, "-c", program, str(HERMES)], capture_output=True, text=True,
                                timeout=120, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), {"text": "pong"})


if __name__ == "__main__":
    unittest.main()
