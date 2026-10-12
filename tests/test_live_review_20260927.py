"""Regressions for the 2026-09-27 live review of https://ansatze.ai.

Each test names the finding it pins. UI behaviour of console.html lives in
test_live_review_20260927_ui.py (run under node).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    guest_console,
    request,
    running_server,
)
from agent_monitor import (
    auto_pipeline,
    engine_graph,
    lean_broker,
    lean_verify,
    proof_bridge,
    proof_graph,
    settings,
)
from agent_monitor import console_server as console
from agent_monitor.cgm import curation as cgm_curation
from monitor_core.harness_dashboard import analysis

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "ansatze"


# ── helpers ────────────────────────────────────────────────────────────────


def _release_fields() -> dict[str, str]:
    toolchain_tree = "3" * 64
    mathlib_tree = "4" * 64
    identity = {
        "schema": 1,
        "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
        "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
        "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
        "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }
    encoded = (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return {
        "release_id": hashlib.sha256(encoded).hexdigest(),
        "toolchain_tree_sha256": toolchain_tree,
        "mathlib_tree_sha256": mathlib_tree,
    }


def _bound_check(lean_dir: Path, uses_mathlib: bool, main_decl=None, *, stdout: str = "") -> dict:
    """A successful `_run_lean_check` result bound to the exact file bytes."""
    source = (Path(lean_dir) / lean_verify.PROOF_FILENAME).read_bytes()
    check = {
        "ok": True,
        "status": "verified",
        "exit_code": 0,
        "duration_s": 0.01,
        "command": ["lean-broker", "mathlib" if uses_mathlib else "core"],
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "sandbox": lean_broker.SANDBOX_MARKER,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "checker_profile": "mathlib" if uses_mathlib else "core",
        "stdout": stdout,
        "stderr": "",
        **_release_fields(),
    }
    text = source.decode("utf-8")
    if "sorry" in text:
        check["reports_sorry"] = True
    else:
        check["axioms"] = {
            "ok": True,
            "decl": "main",
            "axioms": ["propext"],
            "unexpected": [],
            "error": "",
        }
    return check


# ── lean-sandbox: attempts, statuses, refusal, timeout message ─────────────


class LeanAttemptRecordTests(unittest.TestCase):
    def test_compile_check_attempt_keeps_the_axioms_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            lean_verify, "_run_lean_check", side_effect=_bound_check
        ):
            result = lean_verify.compile_or_check(
                workspace=Path(raw), lean="theorem main : 1 = 1 := rfl", mode="check"
            )
            summary = lean_verify.monitor_summary(Path(raw))
        attempt = result["attempts"][-1]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(attempt["result_status"], "verified")
        self.assertEqual(
            attempt["check"]["axioms"],
            {"ok": True, "decl": "main", "axioms": ["propext"], "unexpected": [], "error": ""},
        )
        self.assertEqual(summary["attempts"][-1]["axioms"]["axioms"], ["propext"])

    def test_a_sorry_skeleton_attempt_is_never_labelled_verified(self) -> None:
        source = "theorem main : 2 + 2 = 4 := by\n  sorry\n"
        with tempfile.TemporaryDirectory() as raw, patch.object(
            lean_verify, "_run_lean_check", side_effect=_bound_check
        ):
            result = lean_verify.compile_or_check(workspace=Path(raw), lean=source, mode="check")
            summary = lean_verify.monitor_summary(Path(raw))
        attempt = result["attempts"][-1]
        # The broker's own word is still "verified" (exit 0) ...
        self.assertEqual(attempt["check"]["status"], "verified")
        # ... but what the round established is not.
        self.assertEqual(attempt["result_status"], "incomplete")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(summary["attempts"][-1]["status"], "incomplete")

    def test_generate_attempt_records_carry_a_result_status(self) -> None:
        record = lean_verify._attempt_record(
            round_i=0,
            action="statement",
            lean_src="theorem main : True := by\n  sorry\n",
            uses_mathlib=False,
            check={"ok": True, "status": "verified", "reports_sorry": True},
        )
        self.assertEqual(record["result_status"], "incomplete")
        compiled = lean_verify._attempt_record(
            round_i=1, action="compile", lean_src="theorem main : True := trivial",
            uses_mathlib=False, check={"ok": True, "status": "verified"},
        )
        self.assertEqual(compiled["result_status"], "compiled")

    def test_editor_code_execution_is_refused_before_the_broker(self) -> None:
        sources = {
            "run_cmd": 'run_cmd do\n  IO.println "agent-monitor run_cmd probe"\ntheorem main : True := trivial\n',
            "eval": '#eval IO.println "agent-monitor sandbox io probe"\ntheorem main : True := trivial\n',
            "initialize": "initialize pure ()\ntheorem main : True := trivial\n",
        }
        for name, source in sources.items():
            for mode in ("check", "compile"):
                with self.subTest(name=name, mode=mode), tempfile.TemporaryDirectory() as raw, patch.object(
                    lean_verify, "_run_lean_check"
                ) as broker:
                    workspace = Path(raw)
                    result = lean_verify.compile_or_check(workspace=workspace, lean=source, mode=mode)
                    broker.assert_not_called()
                    self.assertEqual(result["status"], "failed")
                    self.assertIsNone(result["verified_source_sha256"])
                    self.assertIsNone(result["kernel_receipt"])
                    self.assertIn("Refused before checking", result["notes"])
                    self.assertTrue(result["attempts"][-1]["check"]["refused_before_check"])
                    # The file and the cached result describe the same text.
                    on_disk = (workspace / "lean" / lean_verify.PROOF_FILENAME).read_text()
                    self.assertEqual(on_disk.strip(), source.strip())

    def test_ordinary_files_still_reach_the_broker(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.object(
            lean_verify, "_run_lean_check", side_effect=_bound_check
        ) as broker:
            lean_verify.compile_or_check(
                workspace=Path(raw),
                lean="/-- no run_cmd or #eval here -/\ntheorem main : 1 = 1 := rfl",
                mode="check",
            )
        broker.assert_called_once()

    def test_pasted_source_does_not_inherit_the_previous_model(self) -> None:
        generated = "theorem main : 1 = 1 := rfl\n"
        with tempfile.TemporaryDirectory() as raw, patch.object(
            lean_verify, "_run_lean_check", side_effect=_bound_check
        ):
            workspace = Path(raw)
            lean_verify._write_cached(
                workspace, {"lean": generated, "model": "kimi-k3", "source": "proof.md", "attempts": []}
            )
            same = lean_verify.compile_or_check(workspace=workspace, lean=generated, mode="check")
            self.assertEqual(same["model"], "kimi-k3")
            pasted = lean_verify.compile_or_check(
                workspace=workspace, lean="theorem main : 2 = 2 := rfl", mode="compile"
            )
        self.assertIsNone(pasted["model"])
        self.assertEqual(pasted["source"], "editor")

    def test_timeout_message_names_the_profile_limit(self) -> None:
        def timed_out(source: str, **_kwargs) -> dict:
            return {
                "status": "timed_out",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "duration": 60.0,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "sandbox": lean_broker.SANDBOX_MARKER,
                "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
                **_release_fields(),
            }

        for uses_mathlib, expected in ((False, "about 60 seconds for core Lean"), (True, "about 90 seconds for Mathlib")):
            with self.subTest(uses_mathlib=uses_mathlib), tempfile.TemporaryDirectory() as raw, patch.object(
                lean_broker, "check_source", side_effect=timed_out
            ):
                ldir = Path(raw)
                (ldir / lean_verify.PROOF_FILENAME).write_text("theorem main : True := trivial\n")
                result = lean_verify._run_lean_check(ldir, uses_mathlib)
            self.assertEqual(result["status"], "failed")
            self.assertIn(expected, result["stderr"])


# ── guest: explore graph AND plans ─────────────────────────────────────────


class ExploreGraphTests(unittest.TestCase):
    def test_leftover_and_members_stay_non_terminal_once_the_target_closes(self) -> None:
        """GJ18: plan closed, S2/S4 open. The console shows such members as
        "not needed" (test_live_review_20260927_ui); the curator annotation
        text itself is a pinned phase-one artifact and stays as it was."""
        rows = cgm_curation.propagate(
            plans=[cgm_curation.PlanShape("plan-1", ("S2", "S4", "S5"), accepted=True)],
            closed_goals={"S5": ["f5"]},
            root_closed_by="7ac28a5a823dd9a4",
            notes={},
        )
        by_node = {row.node_id: row for row in rows}
        self.assertEqual(by_node["goal:root"].status, "closed")
        self.assertEqual(by_node["plan-1"].status, "closed")
        self.assertEqual(by_node["S5"].status, "closed")
        for goal in ("S2", "S4"):
            self.assertEqual(by_node[goal].status, "open")
            self.assertEqual(by_node[goal].comment, "Deterministic: no verifier-accepted fact yet.")


# ── guest: per-lemma "✓ Lean" and coverage wording ─────────────────────────


class CoverageAuditScopeTests(unittest.TestCase):
    def test_only_the_audited_main_statement_counts_as_statement_audited(self) -> None:
        lean = (
            "/-- Informal: 62018a4d2785e13b -/\n"
            "lemma int_add_closure (m n : ℤ) : (∃ s : ℤ, s = m + n) ∧ (∃ t : ℤ, t = m + n + 1) :=\n"
            "  ⟨⟨_, rfl⟩, ⟨_, rfl⟩⟩\n\n"
            "/-- Informal: target -/\n"
            "theorem main (a b : ℤ) (ha : Odd a) (hb : Odd b) : Even (a + b) := by\n  exact Odd.add_odd ha hb\n"
        )
        informal = {
            "graph": {
                "nodes": [
                    {"id": "62018a4d2785e13b", "kind": "lemma", "label": "Closure under addition",
                     "statement": "integers are closed under addition"},
                    {"id": "target", "kind": "conclusion", "label": "Conclusion",
                     "statement": "the sum of two odd integers is even"},
                ],
                "edges": [{"from": "62018a4d2785e13b", "to": "target"}],
            }
        }
        verified = {
            "status": "verified",
            "sorry_count": 0,
            "lean": lean,
            "main_decl": "main",
            "fidelity": {"severity": "ok", "audit": {"ok": True, "faithful": True, "scope": "statement"}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof_graph.json").write_text(json.dumps(informal), encoding="utf-8")
            (workspace / "lean_proof_graph.json").write_text(
                json.dumps({"graph": proof_graph.graph_from_lean(lean)}), encoding="utf-8"
            )
            (workspace / "lean_verify.json").write_text(json.dumps(verified), encoding="utf-8")
            result = proof_bridge.build(workspace)
        items = {item["informal_id"]: item for item in result["items"]}
        lemma, main = items["62018a4d2785e13b"], items["target"]
        self.assertEqual((lemma["status"], main["status"]), ("verified", "verified"))
        self.assertFalse(lemma["statement_audited"])
        self.assertEqual(lemma["evidence"], "kernel")
        self.assertIn("not audited", lemma["reason"])
        self.assertTrue(main["statement_audited"])
        self.assertEqual(result["summary"]["statement_audited"], 1)
        self.assertEqual(result["summary"]["verified"], 2)

    def test_coverage_stage_detail_does_not_say_formally_verified(self) -> None:
        detail = auto_pipeline._coverage_detail({"verified": 4, "total": 4, "statement_audited": 1}, "success")
        self.assertEqual(
            detail,
            "4/4 informal statements linked to kernel-verified Lean declarations (1 with an audited statement)",
        )
        self.assertNotIn("formally verified", detail)
        self.assertEqual(
            auto_pipeline._coverage_detail({"verified": 1, "total": 4}, "attention"),
            "1/4 informal statements with conservative formal links",
        )


# ── admin: Facts card titles never cut inside math ─────────────────────────


class FactLabelTests(unittest.TestCase):
    def test_fact_labels_end_before_an_unfinished_math_span(self) -> None:
        statement = (
            "Definition: an integer m is even if and only if there exists an integer t "
            "with m = 2t. For all natural numbers $n \\geq 1$ and all $k$, the sum is even."
        )
        for limit in (100, 104, 108, 112, 120):
            label = engine_graph._bounded_math_label(statement, limit)
            self.assertLessEqual(len(label), limit)
            self.assertEqual(label.count("$") % 2, 0, label)
        self.assertEqual(engine_graph._bounded_math_label("short $x$", 120), "short $x$")


# ── guest: Monitor per-agent usage ─────────────────────────────────────────


class MonitorUsageTests(unittest.TestCase):
    def test_usage_less_marker_nodes_are_untracked_not_critical(self) -> None:
        agents = [
            {"trace_id": "t1", "stage_name": "turn-1 (reasoning)", "role": "reasoning",
             "pipeline_stage": "work", "input_tokens": None, "output_tokens": None,
             "total_input_tokens": 0},
            {"trace_id": "t2", "stage_name": "turn-2 (message)", "role": "assistant",
             "pipeline_stage": "work", "input_tokens": 1644, "output_tokens": 2381,
             "total_input_tokens": 1644, "cost_usd": 0.02792},
            {"trace_id": "t3", "stage_name": "turn-3 (reasoning)", "role": "reasoning",
             "pipeline_stage": "work", "input_tokens": None, "output_tokens": None,
             "total_input_tokens": 0},
        ]
        result = analysis.compute_run_analysis(
            agents,
            {"cost_usd": 0.176, "input_tokens": 21112, "output_tokens": 10982},
            pipeline=[{"id": "work", "label": "2 Explore", "title": "Independent proof search"}],
        )
        tiers = {a["trace_id"]: a["monitor"]["cost_tier"] for a in agents}
        self.assertEqual(tiers["t1"], "untracked")
        self.assertEqual(tiers["t3"], "untracked")
        self.assertNotEqual(tiers["t2"], "untracked")
        self.assertEqual([t["trace_id"] for t in result["top_expensive"]], ["t2"])
        self.assertEqual(result["per_agent_usage"], {"tracked": 1, "agents": 3})

    def test_all_zero_cost_run_ranks_nobody_critical(self) -> None:
        agents = [
            {"trace_id": f"t{i}", "stage_name": f"turn-{i}", "role": "reasoning",
             "pipeline_stage": "work", "input_tokens": None, "output_tokens": None}
            for i in range(20)
        ]
        result = analysis.compute_run_analysis(agents, {"cost_usd": 0})
        self.assertEqual({a["monitor"]["cost_tier"] for a in agents}, {"untracked"})
        self.assertEqual(result["top_expensive"], [])


# ── ops-security: guests never see the upstream Kimi endpoint ──────────────


def test_guest_settings_hide_the_upstream_base_url(monkeypatch):
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {})
    monkeypatch.setattr(settings, "sponsored_kimi_available", lambda: True)
    monkeypatch.setattr(settings, "hosted_guest_kimi_available", lambda: True)
    payload = settings.get_settings({"id": 9, "guest": True})
    blob = json.dumps(payload)
    assert "flashflame" not in blob
    for provider in payload["providers"]:
        assert provider["base_url"] == "" and provider["default_base"] == ""
    member = settings.get_settings({"id": 1, "guest": False})
    kimi = next(p for p in member["providers"] if p["id"] == "kimi")
    assert kimi["default_base"]  # members may still configure their own endpoint


# ── ops-security / guest: console HTTP server behaviour ────────────────────


def test_console_server_has_a_real_listen_backlog():
    assert console.ConsoleHTTPServer.request_queue_size >= 64
    assert console.ConsoleHTTPServer.daemon_threads is True


def test_console_output_is_line_buffered():
    class Stream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    out, err = Stream(), Stream()
    with patch.object(console.sys, "stdout", out), patch.object(console.sys, "stderr", err):
        console._line_buffered_output()
    assert out.calls == [{"line_buffering": True}] and err.calls == [{"line_buffering": True}]


def test_a_client_that_disconnects_mid_reply_is_not_a_traceback():
    handler = console.Handler.__new__(console.Handler)
    handler.close_connection = False
    with patch.object(
        console.BaseHTTPRequestHandler, "handle_one_request", side_effect=BrokenPipeError(32, "Broken pipe")
    ):
        handler.handle_one_request()
    assert handler.close_connection is True
    with patch.object(
        console.BaseHTTPRequestHandler, "handle_one_request", side_effect=ConnectionResetError(104, "reset")
    ):
        handler.handle_one_request()


def _raw_exchange(port: int, head: str, body: bytes, *, send_all: bool = True) -> tuple[bytes, float]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    started = time.monotonic()
    try:
        sock.sendall(head.encode("latin-1"))
        if body:
            if send_all:
                sock.sendall(body)
            else:
                sock.sendall(body[:1000])
        received = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += chunk
        return received, time.monotonic() - started
    finally:
        sock.close()


def test_oversized_body_gets_a_complete_413_not_a_reset(guest_console):
    """Live: 2 MB login bodies drew 502 / HTTP/2 PROTOCOL_ERROR plus BrokenPipe."""
    size = 2_000_000
    head = (
        f"POST {APP_MOUNT}/api/auth/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\nContent-Length: {size}\r\n\r\n"
    )
    with running_server() as port:
        whole, _ = _raw_exchange(port, head, b"x" * size)
        short, elapsed = _raw_exchange(port, head, b"x" * size, send_all=False)
    for received in (whole, short):
        status_line, _, rest = received.partition(b"\r\n")
        assert status_line.startswith(b"HTTP/1.") and b" 413 " in status_line + b" "
        headers, _, payload = rest.partition(b"\r\n\r\n")
        assert b"connection: close" in headers.lower()
        length = int(re.search(rb"(?i)content-length:\s*(\d+)", headers).group(1))
        assert len(payload) == length
        assert json.loads(payload) == {"error": "request body too large"}
    # The reply is complete (FIN) before the bounded drain gives up.
    assert elapsed < console.REFUSED_BODY_DRAIN_SECONDS + 3


# ── deploy bundle: Caddy admin API, units, docs polish ────────────────────


def test_caddy_admin_api_is_not_a_tcp_listener():
    caddy = (BUNDLE / "Caddyfile").read_text(encoding="utf-8")
    directives = re.sub(r"(?m)^\s*#.*$", "", caddy)
    assert re.search(r"(?m)^\s*admin unix//var/lib/caddy/admin\.sock\s*$", directives)
    assert "2019" not in directives and "admin off" not in directives
    # The global block precedes every site and survives --acme-email omission.
    assert caddy.index("admin unix//") < caddy.index("(edge_headers) {")
    installer = (BUNDLE / "install.sh").read_text(encoding="utf-8")
    assert 'apply=restart' in installer and 'systemctl "${apply}" caddy.service' in installer


def test_port_80_redirects_carry_the_edge_headers():
    caddy = (BUNDLE / "Caddyfile").read_text(encoding="utf-8")
    block = caddy[caddy.index("http://ansatze.ai {") :]
    block = block[: block.index("\n}") + 2]
    assert "import edge_headers" in block and "redir https://ansatze.ai{uri} 308" in block


def test_units_write_logs_through_and_hide_host_agent_state():
    console_unit = (BUNDLE / "agent-monitor.service").read_text(encoding="utf-8")
    kimi_unit = (BUNDLE / "ansatze-kimi.service").read_text(encoding="utf-8")
    assert "Environment=PYTHONUNBUFFERED=1" in console_unit
    assert "Environment=PYTHONUNBUFFERED=1" in kimi_unit
    inaccessible = [
        line.partition("=")[2].split()
        for line in console_unit.splitlines()
        if line.startswith("InaccessiblePaths=")
    ]
    assert len(inaccessible) == 1
    for path in ("-/home/ubuntu/.claude", "-/home/ubuntu/.claude.json", "-/home/ubuntu/.codex"):
        assert path in inaccessible[0]


def test_docs_publish_robots_and_favicon_without_the_mount():
    public = ROOT / "docs" / "public"
    robots = (public / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://ansatze.ai/sitemap.xml" in robots
    assert "/proof-" not in robots  # never advertise the console mount
    icon = (public / "favicon.ico").read_bytes()
    assert icon[:4] == b"\x00\x00\x01\x00"
    assert (public / "favicon.svg").is_file()
    config = (ROOT / "docs" / ".vitepress" / "config.mjs").read_text(encoding="utf-8")
    assert "/favicon.svg" in config


@pytest.mark.parametrize("path", ["README.md"])
def test_bundle_readme_documents_the_owner_actions(path):
    readme = (BUNDLE / path).read_text(encoding="utf-8")
    for phrase in ("admin.sock", "PYTHONUNBUFFERED", "~ubuntu/.claude", "SSM Session Manager"):
        assert phrase in readme, phrase
    assert "which refuse" not in readme
