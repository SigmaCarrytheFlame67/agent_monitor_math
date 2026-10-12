"""SS-1 regressions: a Lean file must not be reported "verified" by hiding an
admitted goal from the text screens, silencing Lean's own warnings, or
printing a forged ``#print axioms`` report and stopping before the probe.

The unit tests use broker doubles.  The end-to-end tests run the local Lean
4.14 toolchain (when installed) through ``lean_broker._exchange`` exactly the
way the sandboxed worker does (``lean <file>``), so the response passes
``lean_broker._validate_response`` unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_monitor import lean_broker, lean_verify

# The finding's reproductions (scratch audit files p1/p2/p3), verbatim.
P1 = (
    "def q1 : Char := '\"'\n"
    "#guard_msgs (drop warning) in\n"
    "theorem main : (2 : Nat) + 2 = 5 := by sorry\n"
    "#print \"'main' does not depend on any axioms\"\n"
    "#exit\n"
    "def q2 : Char := '\"'\n"
)
P2 = (
    "#guard_msgs (drop warning) in\n"
    "theorem main : (2 : Nat) + 2 = 5 := by\n"
    "  let _q : Char := '\"'\n"
    "  sorry\n"
    "#print \"'main' does not depend on any axioms\"\n"
    "#exit\n"
)
P3 = (
    "#guard_msgs (drop warning) in\n"
    "theorem main : (2 : Nat) + 2 = 5 := by\n"
    "  stop\n"
    "  rfl\n"
    "#print \"'main' does not depend on any axioms\"\n"
    "open Nat#exit\n"
)
CONTROL_OK = "theorem main : (2 : Nat) + 2 = 4 := by rfl\n"
CONTROL_SORRY = "theorem main : (2 : Nat) + 2 = 5 := by sorry\n"


def _flag_ids(source: str) -> dict[str, str]:
    return {flag["id"]: flag["severity"] for flag in lean_verify._fidelity_flags(source)}


class LeanLexerTests(unittest.TestCase):
    def test_a_char_literal_quote_does_not_hide_the_rest_of_the_file(self) -> None:
        for source in (P1, P2):
            executable = lean_verify._lean_executable_text(source)
            self.assertIn("sorry", executable)
            self.assertIn("#exit", executable)
            self.assertEqual(lean_verify._count_sorry(source), 1)
            self.assertEqual(len(executable), len(source))
            self.assertEqual(executable.count("\n"), source.count("\n"))

    def test_lean_token_rules(self) -> None:
        # (source, the whitespace-separated code Lean sees)
        cases = [
            # Primes continue identifiers; `''` is a notation token.
            ("theorem h' : x' = f '' s := sorry", "theorem h' : x' = f '' s := sorry"),
            # Every string/char/raw form is blanked, code after it is kept.
            ('def s := r"\\" sorry', "def s := sorry"),
            ('def t := r#"a"b"# sorry', "def t := sorry"),
            ("def c := ('\\'', '\\\\', '\\x41', 'a') sorry", "def c := ( , , , ) sorry"),
            ('def «a"b» := 1 sorry', 'def «a"b» := 1 sorry'),
            ('/- nested /- -/ "sorry -/ sorry', "sorry"),
        ]
        for source, code in cases:
            with self.subTest(source=source):
                text = lean_verify._lean_executable_text(source)
                self.assertEqual(len(text), len(source))
                self.assertEqual(" ".join(text.split()), code)
        # Interpolated strings run code; the code stays visible.
        text, anomalies = lean_verify._lean_lex('def s := s!"a{sorry}b" ++ "x"')
        self.assertIn("sorry", text)
        self.assertIn(("interpolated_string", 11), anomalies)

    def test_screens_refuse_the_reproduced_bypass_files(self) -> None:
        for name, source in (("p1", P1), ("p2", P2), ("p3", P3)):
            with self.subTest(name):
                flags = _flag_ids(source)
                self.assertEqual(flags.get("metaprogram"), "major", flags)  # #exit, #guard_msgs
                self.assertEqual(flags.get("print_string"), "major", flags)
                self.assertEqual(lean_verify._fidelity_severity(lean_verify._fidelity_flags(source), None), "major")
                self.assertEqual(
                    lean_verify._apply_fidelity("verified", lean_verify._fidelity_flags(source), None),
                    "unfaithful",
                )

    def test_hash_commands_and_glued_keywords_are_flagged_anywhere(self) -> None:
        for source in (
            "theorem main : 1 = 1 := rfl\nopen Nat#exit\n",
            "#guard_msgs in\ntheorem main : 1 = 1 := rfl\n",
            "theorem main : 1 = 1 := rfl\ndef x := 1#eval 2\n",
            "def x := 1macro_rules | `(tactic| trivial) => `(tactic| sorry)\ntheorem main : 1 = 1 := rfl\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    lean_verify._fidelity_severity(lean_verify._fidelity_flags(source), None), "major"
                )
        # Identifiers that merely contain a keyword are not commands.
        self.assertEqual(_flag_ids("theorem main (h1 : x.elab = 1) : h1elab = 1 := rfl\n").get("custom_syntax"), None)

    def test_ambiguous_char_literal_after_a_symbol_is_refused(self) -> None:
        source = "instance : Coe Char Bool := ⟨fun _ => true⟩\ndef b : Bool := !'\"'\ntheorem main : 1 = 1 := rfl\n"
        self.assertEqual(_flag_ids(source).get("ambiguous_char_literal"), "major")
        self.assertIsNone(_flag_ids("def c := ('a', 'b')\ntheorem main : 1 = 1 := rfl\n").get("ambiguous_char_literal"))


def _broker(stdout: str, source: str) -> dict:
    from test_lean_checker_hardening import _broker_result

    result = _broker_result(source)
    result["stdout"] = stdout
    return result


class AxiomsProbeMarkerTests(unittest.TestCase):
    def test_a_forged_report_before_a_stopped_probe_fails_closed(self) -> None:
        # What Lean prints for P2: the file's own fake report, then `#exit`
        # stops elaboration, so the appended probe never runs.
        def stopped(source: str, **_kwargs) -> dict:
            return _broker(
                "'main' does not depend on any axioms\n"
                "Proof.lean:6:0: warning: using 'exit' to interrupt Lean\n",
                source,
            )

        with patch.object(lean_broker, "check_source", side_effect=stopped):
            probe = lean_verify._axioms_probe(
                "theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5
            )
        self.assertFalse(probe["ok"])
        self.assertIn("markers are missing", probe["error"])

    def test_markers_with_a_fresh_nonce_bracket_the_only_trusted_reports(self) -> None:
        seen: list[str] = []

        def honest(source: str, **_kwargs) -> dict:
            seen.append(source)
            markers = re.findall(r'(?m)^#print "(AGENT_MONITOR_AXIOMS::[0-9a-f]{32}::(?:begin|end))"$', source)
            return _broker(
                # A forged "clean" report printed by the file itself comes first.
                "'main' does not depend on any axioms\n"
                + markers[0] + "\n"
                + "'main' depends on axioms: [sorryAx]\n"
                + markers[1] + "\n",
                source,
            )

        with patch.object(lean_broker, "check_source", side_effect=honest):
            first = lean_verify._axioms_probe("theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5)
            second = lean_verify._axioms_probe("theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5)
        self.assertFalse(first["ok"])
        self.assertEqual(first["axioms"], ["sorryAx"])
        nonces = [re.search(r"AXIOMS::([0-9a-f]{32})::begin", source).group(1) for source in seen]
        self.assertNotEqual(nonces[0], nonces[1])

    def test_repeated_or_reordered_markers_fail_closed(self) -> None:
        def replayed(source: str, **_kwargs) -> dict:
            begin, end = re.findall(r'(?m)^#print "(AGENT_MONITOR_AXIOMS::[0-9a-f]{32}::(?:begin|end))"$', source)
            return _broker(f"{end}\n{begin}\n'main' does not depend on any axioms\n{end}\n", source)

        with patch.object(lean_broker, "check_source", side_effect=replayed):
            probe = lean_verify._axioms_probe("theorem main : 1 = 1 := rfl\n", uses_mathlib=False, timeout=5)
        self.assertFalse(probe["ok"])

    def test_exit_warning_fails_the_check(self) -> None:
        def exits(source: str, **_kwargs) -> dict:
            return _broker("Proof.lean:2:0: warning: using 'exit' to interrupt Lean\n", source)

        with tempfile.TemporaryDirectory() as raw, patch.object(lean_broker, "check_source", side_effect=exits):
            ldir = Path(raw)
            (ldir / lean_verify.PROOF_FILENAME).write_text("theorem main : 1 = 1 := rfl\n#exit\n")
            result = lean_verify._run_lean_check(ldir, False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("#exit", result["stderr"])


class StatementFingerprintFailClosedTests(unittest.TestCase):
    def test_an_unconfirmed_fingerprint_is_drift_when_the_lock_has_one(self) -> None:
        loop = object.__new__(lean_verify._FormalSession)
        loop.lock = {"main_decl": "main", "fingerprint": "f" * 64}
        loop.main_decl = "main"
        loop._fingerprints = {}
        with patch.object(lean_verify, "_statement_fingerprint", return_value={"ok": False, "error": "no report"}):
            self.assertIn("could not confirm", loop._statement_drift("theorem main : True := trivial\n"))
        loop._fingerprints = {}
        with patch.object(lean_verify, "_statement_fingerprint", return_value={"ok": True, "fingerprint": "f" * 64}):
            self.assertEqual(loop._statement_drift("theorem main : True := trivial\n"), "")
        # Without a recorded lock fingerprint nothing changes.
        loop.lock = {"main_decl": "main", "fingerprint": None}
        self.assertEqual(loop._statement_drift("theorem main : True := trivial\n"), "")


# ── end to end with the real Lean 4.14 toolchain ──────────────────────────

_LEAN = Path.home() / ".elan/toolchains/leanprover--lean4---v4.14.0/bin/lean"
_IDENTITY = {
    "schema": 1,
    "lean_toolchain": lean_broker.EXPECTED_LEAN_TOOLCHAIN,
    "lean_version": lean_broker.EXPECTED_TOOLCHAIN,
    "mathlib_input_rev": lean_broker.EXPECTED_MATHLIB_INPUT_REV,
    "mathlib_rev": lean_broker.EXPECTED_MATHLIB_REV,
    "toolchain_tree_sha256": "1" * 64,
    "mathlib_tree_sha256": "2" * 64,
}


def _real_lean_exchange(_socket_path, *, profile, source, timeout):
    """The worker's fixed `lean <file>` run, answered like the broker."""
    with tempfile.TemporaryDirectory(prefix="lean-gate-") as tmp:
        path = Path(tmp) / "Proof.lean"
        path.write_bytes(source)
        proc = subprocess.run(
            [str(_LEAN), str(path)], cwd=tmp, capture_output=True, text=True, timeout=120,
            env={"PATH": "/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8"},
        )
    value = {
        "protocol": lean_broker.PROTOCOL_VERSION,
        "profile": profile,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "sandbox": lean_broker.SANDBOX_MARKER,
        "release_id": hashlib.sha256(
            (json.dumps(_IDENTITY, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
        "toolchain_tree_sha256": _IDENTITY["toolchain_tree_sha256"],
        "mathlib_tree_sha256": _IDENTITY["mathlib_tree_sha256"],
        "status": "completed",
        "exit_code": proc.returncode,
        "duration_ms": 10,
        "toolchain": lean_broker.EXPECTED_TOOLCHAIN,
        "stdout": proc.stdout.replace(str(path), "Proof.lean"),
        "stderr": proc.stderr.replace(str(path), "Proof.lean"),
        "error": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    return lean_broker._validate_response(value, profile=profile, source_sha256=value["source_sha256"])


@pytest.mark.skipif(not _LEAN.is_file(), reason="local Lean 4.14 not installed")
@pytest.mark.parametrize(
    "source, verified",
    [(CONTROL_OK, True), (CONTROL_SORRY, False), (P1, False), (P2, False), (P3, False)],
    ids=["control_ok", "control_sorry", "p1", "p2", "p3"],
)
def test_real_lean_check_never_verifies_the_bypass_files(source, verified, tmp_path, monkeypatch):
    monkeypatch.setattr(lean_broker, "_exchange", _real_lean_exchange)
    result = lean_verify.compile_or_check(workspace=tmp_path, lean=source, mode="check")
    assert (result["status"] == "verified") is verified, result.get("notes")
    if source in (P1, P2, P3):
        assert result["status"] != "verified"


@pytest.mark.skipif(not _LEAN.is_file(), reason="local Lean 4.14 not installed")
def test_real_lean_probe_markers_fail_closed_when_the_file_stops_early(monkeypatch):
    monkeypatch.setattr(lean_broker, "_exchange", _real_lean_exchange)
    # Without the text screens: the probe alone must refuse the forged report.
    probe = lean_verify._axioms_probe(P2, uses_mathlib=False, timeout=60)
    assert not probe["ok"] and "markers are missing" in probe["error"]
    honest = lean_verify._axioms_probe(CONTROL_SORRY, uses_mathlib=False, timeout=60)
    assert not honest["ok"] and honest["axioms"] == ["sorryAx"]
    clean = lean_verify._axioms_probe(CONTROL_OK, uses_mathlib=False, timeout=60)
    assert clean["ok"], clean["error"]
