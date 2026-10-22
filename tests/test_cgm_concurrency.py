"""Concurrency, determinism, budget, cancellation and compatibility contracts
of the CGM pipeline runner (receipt schema 3).

The fake route is prompt-keyed: every reply is a pure function of (system
prompt, user prompt, effort), so a scenario yields the same replies whatever
order concurrent calls are issued or completed in.  Proof texts carry
``<<wN-LABEL-revK>>`` markers that drive the fake verifier and repair.

``tests/fixtures/cgm_phase1_artifacts.json`` holds artifacts written by the
sequential phase-1 (schema-2) writer: the 'fake-route' cases were produced
from ``SCENARIOS`` and ``REORDERED_SCENARIOS`` below with the same fake route
and a fixed timestamp, and 'live-oddsum-kimi-k3' is a real kimi-k3 run.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

from agent_monitor import engine_graph, kimi_k3
from agent_monitor.cgm import closure as cgm_closure
from agent_monitor.cgm import prompts as cgm_prompts
from agent_monitor.cgm import schema as cgm_schema
from agent_monitor.runners import math_harness_runner as runner

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cgm_phase1_artifacts.json"
RECEIPT = "math_harness/project/engine_receipt.json"

PROBLEM = "Prove that for every natural number n, 1 + 3 + ... + (2n-1) = n^2."
FIXED_TIMESTAMP = "2026-09-27T10:00:00.000000Z"

STRATEGY = json.dumps(
    {
        "immediate_conclusions": [
            {"statement": "For n = 0 the sum is empty.", "justification": "Definition."}
        ],
        "obstacles": ["Index shifts are easy to get wrong."],
        "plans": [
            {
                "label": "Telescoping",
                "technique": "reduction",
                "approach": "Reduce to the telescoping identity k^2 - (k-1)^2 = 2k - 1.",
                "subgoals": [
                    {"id": "S1", "statement": "k^2 - (k-1)^2 = 2k - 1 for k >= 1."},
                    {"id": "S2", "statement": "(k-1)^2 >= 0 for every integer k."},
                    {
                        "id": "S3",
                        "statement": "The partial sums telescope.",
                        "depends_on": ["S1", "S2"],
                    },
                ],
                "risks": "Off-by-one errors.",
            },
            {
                "label": "Induction",
                "technique": "plain",
                "approach": "Induct on n.",
                "subgoals": [],
                "risks": "Base case.",
            },
            {
                "label": "Pairing",
                "technique": "invariant",
                "approach": "Pair the k-th and (n+1-k)-th odd numbers.",
                "subgoals": [{"id": "S1", "statement": "Each pair sums to 2n."}],
                "risks": "Parity of n.",
            },
        ],
    }
)

_WORKER_BLOCKS = {
    1: (
        (
            "L1",
            "S1",
            (),
            "For every integer k >= 1, k^2 - (k-1)^2 = 2k - 1.",
            "Expanding, (k-1)^2 = k^2 - 2k + 1, hence k^2 - (k-1)^2 = 2k - 1.",
        ),
        (
            "L2",
            "S2",
            (),
            "For every integer k, (k-1)^2 >= 0.",
            "A square of an integer is a nonnegative integer.",
        ),
        (
            "L3",
            "S3",
            ("L1", "L2"),
            "For every integer n >= 1, the sum of 2k - 1 over k = 1..n equals n^2.",
            "By L1: 2k - 1 = k^2 - (k-1)^2 for each k >= 1, so the sum telescopes "
            "to n^2 - 0^2 = n^2; L2 shows every subtracted term is a square.",
        ),
        (
            "THEOREM",
            None,
            ("L3",),
            "(the problem)",
            "For n = 0 both sides are 0. For n >= 1, by L3 the sum equals n^2.",
        ),
    ),
    2: (
        (
            "L1",
            None,
            (),
            "If 1 + 3 + ... + (2n-1) = n^2 then adding 2n + 1 gives (n+1)^2.",
            "n^2 + 2n + 1 = (n+1)^2 by expanding the square.",
        ),
        (
            "THEOREM",
            None,
            ("L1",),
            "(the problem)",
            "Induct on n: the base case n = 0 is the empty sum; the step is L1.",
        ),
    ),
    3: (
        (
            "THEOREM",
            None,
            (),
            "(the problem)",
            "Pair the k-th and (n+1-k)-th terms: each pair sums to 2n, so twice "
            "the sum is n * 2n and the sum is n^2.",
        ),
    ),
}
# Worker 1 written in topological but not level order (the live sqrt2
# shape): L1; L2 uses L1; L3 is independent; the THEOREM uses L2 and L3.
# Phase 1 verified it as written (L1, L2, L3, THEOREM); its levels are
# [L1, L3], [L2], [THEOREM].
_REORDERED_BLOCKS = {
    **_WORKER_BLOCKS,
    1: (
        _WORKER_BLOCKS[1][0],
        (
            "L2",
            "S3",
            ("L1",),
            "For every integer n >= 1, the sum of 2k - 1 over k = 1..n equals n^2.",
            "By L1: 2k - 1 = k^2 - (k-1)^2 for each k >= 1, so the sum telescopes "
            "to n^2 - 0^2 = n^2.",
        ),
        (
            "L3",
            "S2",
            (),
            "For every integer k, (k-1)^2 >= 0.",
            "A square of an integer is a nonnegative integer.",
        ),
        (
            "THEOREM",
            None,
            ("L2", "L3"),
            "(the problem)",
            "For n = 0 both sides are 0. For n >= 1, by L2 the sum equals n^2; "
            "L3 shows every telescoped term is a square.",
        ),
    ),
}
_WORKER_STATUS = {1: "solved", 2: "partial", 3: "solved"}
_WORKER_FINDING = {
    1: "=== FINDING obstacle ===\nClaim: Pairing first and last terms needs parity cases.\n"
    "Evidence:\nFor odd n the middle term is unpaired.\n=== END ===",
    2: "=== FINDING example ===\nClaim: n = 3 gives 1 + 3 + 5 = 9\nEvidence:\n9 = 3^2.\n=== END ===",
    3: "",
}
_MARKER_RE = re.compile(r"<<w(?P<worker>[1-9])-(?P<label>[A-Z0-9]+)-rev(?P<rev>[0-9]+)>>")

OK = json.dumps(
    {
        "verification_report": {"summary": "Sound.", "critical_errors": [], "gaps": []},
        "verdict": "correct",
        "repair_hints": "",
    }
)
WRONG = json.dumps(
    {
        "verification_report": {
            "summary": "One gap.",
            "critical_errors": [],
            "gaps": [{"location": "step 2", "issue": "The key identity is asserted, not shown."}],
        },
        "verdict": "wrong",
        "repair_hints": "Derive the identity explicitly.",
    }
)
# Aliases follow the curator snapshot order: N1 root, N2 plan 1, N3-N5 its
# subgoals, N6 plan 2, N7 plan 3, N8 its subgoal.
CURATOR = json.dumps(
    {
        "annotations": [
            {"node": "N6", "status": "active", "score": 0.7, "comment": "Promising (N1)."},
            {"node": "N7", "status": "open", "score": 0.4, "comment": "Untested."},
        ]
    }
)
EMPTY_ERROR = "Kimi returned an empty response for kimi-k3"


def worker_reply(
    worker: int,
    revs: dict[str, int] | None = None,
    blocks: dict[int, tuple] | None = None,
) -> str:
    revs = revs or {}
    parts = []
    for label, subgoal, uses, statement, proof in (blocks or _WORKER_BLOCKS)[worker]:
        marker = f"<<w{worker}-{label}-rev{revs.get(label, 0)}>>"
        head = "=== THEOREM ===" if label == "THEOREM" else f"=== LEMMA {label} ==="
        lines = [head]
        if subgoal:
            lines.append(f"Subgoal: {subgoal}")
        lines.append(f"Uses: {', '.join(uses) if uses else 'none'}")
        lines += ["Statement:", statement, "Proof:", f"{proof} {marker}", "=== END ==="]
        parts.append("\n".join(lines))
    if _WORKER_FINDING[worker]:
        parts.append(_WORKER_FINDING[worker])
    parts.append(f"=== STATUS ===\n{_WORKER_STATUS[worker]}\n=== END ===")
    return "\n\n".join(parts)


def _section(user: str, name: str) -> str:
    match = re.search(rf"<{name}>\n(.*?)\n</{name}>", user, flags=re.S)
    return match.group(1) if match else ""


def _marker(user: str) -> re.Match[str]:
    markers = list(_MARKER_RE.finditer(_section(user, "PROOF")))
    assert len(markers) == 1, "every verified proof carries one marker"
    return markers[0]


class Scenario:
    """Replies for one scenario.

    ``wrong``: markers ``w<N>-<LABEL>`` (any revision) or ``w<N>-<LABEL>-rev<K>``
    the fake verifier rejects.  ``malformed_first``: worker indices whose first
    reply (no format reminder) is unparsable.  ``empty_high_retry``: worker
    indices whose format retry is empty at high effort (then answered at low).
    ``blocks``: the workers' lemma layout (default ``_WORKER_BLOCKS``).
    ``provider_down``: markers whose verifier call fails at the provider.
    """

    def __init__(
        self,
        *,
        wrong: tuple[str, ...] = (),
        malformed_first: tuple[int, ...] = (),
        empty_high_retry: tuple[int, ...] = (),
        garbled_verifier: tuple[str, ...] = (),
        blocks: dict[int, tuple] | None = None,
        provider_down: tuple[str, ...] = (),
    ) -> None:
        self.wrong = wrong
        self.malformed_first = malformed_first
        self.empty_high_retry = empty_high_retry
        self.garbled_verifier = garbled_verifier
        self.blocks = blocks
        self.provider_down = provider_down

    def _verdict(self, user: str) -> str:
        found = _marker(user)
        short = f"w{found['worker']}-{found['label']}"
        full = f"{short}-rev{found['rev']}"
        if short in self.provider_down or full in self.provider_down:
            raise RuntimeError("verifier provider unavailable")
        if short in self.garbled_verifier or full in self.garbled_verifier:
            return "The proof looks fine to me."  # no JSON: an 'error' verdict
        return WRONG if (short in self.wrong or full in self.wrong) else OK

    def reply(self, system: str, user: str, effort: str | None) -> str:
        """The reply text, or raise ``RuntimeError(EMPTY_ERROR)`` for an empty one."""
        if system == cgm_prompts.STRATEGIST_SYSTEM:
            return STRATEGY
        if system == cgm_prompts.VERIFIER_SYSTEM:
            return self._verdict(user)
        if system == cgm_prompts.CURATOR_SYSTEM:
            return CURATOR
        if "<VERIFIER_REPORT" in user:  # a repair of the current candidate
            current = _section(user, "CURRENT_CANDIDATE") or user.split("<CURRENT_CANDIDATE>", 1)[1]
            revs = {m["label"]: int(m["rev"]) for m in _MARKER_RE.finditer(current)}
            worker = int(next(_MARKER_RE.finditer(current))["worker"])
            rejected = re.search(r'<VERIFIER_REPORT block="([A-Z0-9]+)">', user).group(1)
            revs[rejected] = revs.get(rejected, 0) + 1
            return worker_reply(worker, revs, self.blocks)
        match = re.search(r"Carry out assigned plan (\d+) of (\d+)", user)
        assert match, "unexpected prompt"
        worker = int(match.group(1))
        reminded = "<FORMAT_REMINDER>" in user
        if worker in self.malformed_first and not reminded:
            return "I will think about this problem later."
        if worker in self.empty_high_retry and reminded and effort != "low":
            raise RuntimeError(EMPTY_ERROR)
        return worker_reply(worker, blocks=self.blocks)


SCENARIOS = {
    # Worker 1 is accepted: waves [L1, L2] -> [L3] -> [THEOREM].
    "accepted": Scenario(),
    # L2 of worker 1 is always wrong: two repairs, then worker 3 is rejected.
    "repairs-unattested": Scenario(wrong=("w1-L2", "w3-THEOREM")),
    # Worker 2 needs a format retry that is first empty; a garbled verdict.
    "retries": Scenario(
        malformed_first=(2,), empty_high_retry=(2,), garbled_verifier=("w1-L3-rev0",)
    ),
}
# Worker 1 is written in topological, not level, order and a later-level
# block fails.  Phase 1 verified in written order, so these reproduce it only
# one call at a time; with concurrency the same blocks are rejected and
# repaired in the same order, and the calls follow the configured waves.
REORDERED_SCENARIOS = {
    # L2 (level 1, written second) is wrong once: phase 1 made 2 verifier
    # calls (L1, L2) before its repair.
    "reordered-repair": Scenario(blocks=_REORDERED_BLOCKS, wrong=("w1-L2-rev0",)),
    # L2 and the later-written L3 are both wrong once: L2 is repaired first.
    "reordered-two-failures": Scenario(
        blocks=_REORDERED_BLOCKS, wrong=("w1-L2-rev0", "w1-L3-rev0")
    ),
}
FIXTURE_DESCRIPTION = (
    "Artifacts written by the phase-1 (schema-2, sequential) CGM writer, "
    "captured before the E3 concurrency change. 'fake-route' cases come "
    "from tests/test_cgm_concurrency.py's prompt-keyed scenarios with a "
    "fixed timestamp; 'live-kimi-k3' is a real phase-1 kimi-k3 run. The "
    "'reordered-*' cases were added later from the same phase-1 writer "
    "source (sha256 = runner_source_sha256), rebuilt from the saved "
    "pre-change files."
)
# Not in the phase-1 fixture: the first of two independent blocks fails, so
# a wave of two verifies its sibling too (concurrency >= 2 only).
SIBLING_FAILURE = Scenario(wrong=("w1-L1-rev0",))
# Every worker needs its format retry and that retry's low-effort retry.
RETRY_HUNGRY = Scenario(malformed_first=(1, 2, 3), empty_high_retry=(1, 2, 3))


def _worker_of(user: str) -> int:
    match = re.search(r"Carry out assigned plan (\d+) of", user)
    return int(match.group(1)) if match else 0


class FakeRoute:
    """A thread-safe prompt-keyed fake ``api_chat`` with controllable timing.

    ``delay(system, user)`` returns seconds to sleep before replying (default:
    uniform random jitter); ``hold(system, user)`` names calls that block
    until ``release`` is set.  Usage numbers are a pure function of the texts.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        jitter: float = 0.0,
        seed: int | None = None,
        delay: Callable[[str, str], float] | None = None,
        hold: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.scenario = scenario
        self.jitter = jitter
        self.rng = random.Random(seed)
        self.delay = delay
        self.hold = hold
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.held = threading.Event()
        self.release = threading.Event()
        self.returned: list[str] = []
        self.environ_snapshot: dict[str, str] | None = None

    def __call__(self, system: str, user: str, *, model: str, timeout: int = 240, **kwargs: Any):
        effort = kwargs.get("effort")
        with self.lock:
            if self.environ_snapshot is not None:
                assert dict(os.environ) == self.environ_snapshot, "os.environ changed"
            self.calls.append(
                {"system": system, "user": user, "effort": effort, "env": kwargs.get("env")}
            )
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            pause = (
                self.delay(system, user)
                if self.delay is not None
                else self.rng.uniform(0, self.jitter)
            )
        try:
            if self.hold is not None and self.hold(system, user):
                self.held.set()
                self.release.wait(timeout=30)
            if pause:
                time.sleep(pause)
            try:
                text = self.scenario.reply(system, user, effort)
            except RuntimeError as exc:
                raise runner.APIBackendError(str(exc)) from None
        finally:
            with self.lock:
                self.in_flight -= 1
        with self.lock:
            self.returned.append(system[:20])
        return SimpleNamespace(
            text=text,
            usage={"input_tokens": len(user) % 997, "output_tokens": len(text) % 991},
            model="kimi-k3",
            provider="kimi",
        )


def _files(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run(
    route: FakeRoute,
    workspace: Path,
    *,
    concurrency: int | None = None,
    env: dict[str, str] | None = None,
    budgets: runner.Budgets | None = None,
    events: list[dict] | None = None,
    problem_id: str = "odd-sum-e3",
) -> dict[str, Any]:
    run_env = {"AGENT_MONITOR_PROBLEM_ID": problem_id, **(env or {})}
    if concurrency is not None:
        run_env["AGENT_MONITOR_CGM_CONCURRENCY"] = str(concurrency)
    sink = events if events is not None else []
    with (
        patch.dict(os.environ, run_env, clear=True),
        patch.object(runner, "api_chat", new=route),
        patch.object(runner, "_now", return_value=FIXED_TIMESTAMP),
    ):
        result = runner.run_pipeline(
            workspace,
            PROBLEM,
            runner.Route("api", "kimi-k3", "api_chat"),
            emit=sink.append,
            budgets=budgets,
        )
    for key in ("proof_path", "receipt_path"):
        result.pop(key)
    return result


def _run_files(route: FakeRoute, **kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        result = _run(route, workspace, **kwargs)
        return result, _files(workspace)


def _receipt(files: dict[str, str]) -> dict[str, Any]:
    return json.loads(files[RECEIPT])


def _views(workspace: Path) -> dict[str, Any]:
    facts = engine_graph.build(workspace, {"engine": "math_harness"}, view="facts")
    workflow = engine_graph.build(workspace, {"engine": "math_harness"}, view="workflow")
    return {
        "facts_status": facts["status"],
        "attestation": facts["provenance"].get("attestation"),
        "receipt_status": facts["provenance"].get("receipt_status"),
        "workflow_status": workflow["status"],
        "workflow_nodes": len(workflow["graph"]["nodes"]),
        "workflow_edges": len(workflow["graph"]["edges"]),
    }


def _write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text, encoding="utf-8")


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _labelled_closure(files: dict[str, str]) -> str:
    """The verified-closure@2 proof.md of a stored accepted generation: its
    fact files rendered with its receipt's worker labels."""
    receipt = _receipt(files)
    facts = []
    for path, text in files.items():
        if path.startswith("math_harness/project/fact_graph/facts/"):
            fact_id = Path(path).stem
            facts.append(engine_graph._fact_mapping(engine_graph._parse_fact(text.encode(), fact_id, path)))
    labels = {item["fact_id"]: item["label"] for item in receipt["facts"] if not item["is_target"]}
    return cgm_closure.render_verified_closure(facts, receipt["target_fact_id"], labels=labels)


class PhaseOneCompatibilityTests(unittest.TestCase):
    """Artifacts written before the concurrency change keep their attestation."""

    def test_fixture_is_schema_two_from_the_sequential_writer(self) -> None:
        fixture = _load_fixture()
        self.assertEqual(
            set(fixture["cases"]),
            {*SCENARIOS, *REORDERED_SCENARIOS, "live-oddsum-kimi-k3"},
        )
        for name, case in fixture["cases"].items():
            with self.subTest(case=name):
                receipt = _receipt(case["files"])
                self.assertEqual(receipt["schema_version"], 2)
                self.assertEqual(
                    receipt["workflow"]["verification_policy"],
                    cgm_schema.VERIFICATION_POLICIES[2],
                )
                self.assertNotIn("call_order", receipt["workflow"])
                self.assertNotIn("concurrency", receipt["workflow"]["budgets"])

    def test_previously_written_runs_still_attest_and_render(self) -> None:
        for name, case in _load_fixture()["cases"].items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _write_files(workspace, case["files"])
                self.assertEqual(_views(workspace), case["views"])
                receipt = _receipt(case["files"])
                dag = engine_graph.load_fact_dag(workspace)
                assert dag is not None
                self.assertEqual(
                    sorted(item["fact_id"] for item in dag),
                    sorted(item["fact_id"] for item in receipt["facts"]),
                )
                self.assertEqual(
                    [item["attested"] for item in dag],
                    [receipt["verdict"] == "accepted"] * len(dag),
                )

    def test_previous_generation_is_retired_by_a_continuation(self) -> None:
        fixture = _load_fixture()
        for name in ("accepted", "repairs-unattested", "live-oddsum-kimi-k3"):
            case = fixture["cases"][name]
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                _write_files(workspace, case["files"])
                prior = _receipt(case["files"])
                self.assertEqual(
                    sorted(runner._prior_generated_fact_ids(workspace)),
                    sorted(item["fact_id"] for item in prior["facts"]),
                )
                _run(FakeRoute(SCENARIOS["accepted"]), workspace, concurrency=3)
                receipt = json.loads((workspace / RECEIPT).read_text())
                self.assertEqual(receipt["schema_version"], 3)
                self.assertEqual(
                    sorted(p.stem for p in (workspace / "math_harness/project/fact_graph/facts").iterdir()),
                    sorted(item["fact_id"] for item in receipt["facts"]),
                )
                self.assertEqual(_views(workspace)["attestation"], "harness-gate-receipt")

    def _assert_reproduces(
        self,
        result: dict[str, Any],
        files: dict[str, str],
        old: dict[str, Any],
        concurrency: int,
    ) -> None:
        self.assertEqual(set(files), set(old["files"]))
        new_receipt, old_receipt = _receipt(files), _receipt(old["files"])
        # An accepted proof.md now names its lemmas by their labels
        # (verified-closure@2): the @2 rendering of the very same fact files
        # with the phase-1 receipt's labels.  Its hash, its rendering id and
        # the result's proof text follow; every other byte is phase 1's.
        accepted = old_receipt["verdict"] == "accepted"
        expected_proof = _labelled_closure(old["files"]) if accepted else old["files"]["proof.md"]
        for path, text in files.items():
            if path not in {RECEIPT, "proof.md"}:
                self.assertEqual(text, old["files"][path], path)
        self.assertEqual(files["proof.md"], expected_proof)
        expected_result = dict(old["result"])
        if accepted:
            self.assertEqual(old_receipt["proof_rendering"], cgm_schema.PROOF_RENDERING_VERIFIED)
            self.assertEqual(new_receipt["proof_rendering"], cgm_schema.PROOF_RENDERING_VERIFIED_V2)
            self.assertEqual(
                new_receipt["proof_sha256"],
                hashlib.sha256(expected_proof.encode("utf-8")).hexdigest(),
            )
            for receipt in (new_receipt, old_receipt):
                receipt.pop("proof_rendering")
                receipt.pop("proof_sha256")
            expected_result["proof_text"] = expected_proof
        # Exactly the schema-3 fields differ; calls (in order), verdicts,
        # counts, retries and degradations are identical.
        self.assertEqual(new_receipt["schema_version"], 3)
        self.assertEqual(new_receipt["workflow"]["call_order"], cgm_schema.CALL_ORDER)
        self.assertEqual(
            new_receipt["workflow"]["verification_policy"],
            cgm_schema.VERIFICATION_POLICY,
        )
        self.assertEqual(
            new_receipt["workflow"]["budgets"],
            {**old_receipt["workflow"]["budgets"], "concurrency": concurrency},
        )
        for receipt in (new_receipt, old_receipt):
            for key in ("schema_version", "source_sha256", "binding_sha256"):
                receipt.pop(key)
            for key in ("call_order", "verification_policy", "budgets"):
                receipt["workflow"].pop(key, None)
        self.assertEqual(new_receipt, old_receipt)
        self.assertEqual(result, expected_result)

    def test_new_writer_reproduces_phase_one_artifacts_at_any_concurrency(self) -> None:
        fixture = _load_fixture()
        for name, scenario in SCENARIOS.items():
            old = fixture["cases"][name]
            for concurrency, seed in ((1, 1), (2, 2), (3, 3), (3, 4)):
                with self.subTest(case=name, concurrency=concurrency, seed=seed):
                    result, files = _run_files(
                        FakeRoute(scenario, jitter=0.02, seed=seed),
                        concurrency=concurrency,
                    )
                    self._assert_reproduces(result, files, old, concurrency)

    def test_one_call_at_a_time_verifies_in_the_written_order_as_phase_one(self) -> None:
        # Worker 1's lemmas are in topological but not level order and a
        # later-level block fails: phase 1 verified them as written (L1, L2
        # before the repair; never L3 first), and so does concurrency 1.
        fixture = _load_fixture()
        for name, scenario in REORDERED_SCENARIOS.items():
            with self.subTest(case=name):
                result, files = _run_files(FakeRoute(scenario), concurrency=1)
                self._assert_reproduces(result, files, fixture["cases"][name], 1)
        old = _receipt(fixture["cases"]["reordered-repair"]["files"])
        self.assertEqual(
            [(v["call"], v["label"], v["verdict"]) for v in old["verdicts"][:2]],
            [("verify-1", "L1", "correct"), ("verify-2", "L2", "wrong")],
        )

    def test_the_first_failing_block_in_written_order_decides_at_any_concurrency(self) -> None:
        # Concurrency may verify an independent later block early, but the
        # block that is rejected (and so repaired) is always the one phase 1
        # rejected: the first failing block in the written topological order.
        fixture = _load_fixture()
        for name, scenario in REORDERED_SCENARIOS.items():
            old = fixture["cases"][name]
            old_receipt = _receipt(old["files"])
            label_of = {v["call"]: v["label"] for v in old_receipt["verdicts"]}
            phase_one_repairs: list[str] = []
            last_verified = None
            for call in old_receipt["calls"]:
                if call["stage"] in label_of:
                    last_verified = label_of[call["stage"]]
                elif call["stage"].startswith("repair-"):
                    phase_one_repairs.append(last_verified)
            self.assertEqual(
                phase_one_repairs,
                {"reordered-repair": ["L2"], "reordered-two-failures": ["L2", "L3"]}[name],
            )
            for concurrency, seed in ((1, 1), (2, 2), (3, 3), (3, 4)):
                with self.subTest(case=name, concurrency=concurrency, seed=seed):
                    route = FakeRoute(scenario, jitter=0.02, seed=seed)
                    result, files = _run_files(route, concurrency=concurrency)
                    repaired = [
                        re.search(r'<VERIFIER_REPORT block="([A-Z0-9]+)">', call["user"]).group(1)
                        for call in route.calls
                        if "<VERIFIER_REPORT" in call["user"]
                    ]
                    self.assertEqual(repaired, phase_one_repairs)
                    self.assertEqual(result["status"], "accepted")
                    self.assertEqual(result["fact_ids"], old["result"]["fact_ids"])
                    self.assertEqual(result["calls"], old["result"]["calls"])
                    receipt = _receipt(files)
                    self.assertEqual(
                        receipt["workflow"]["calls"], old_receipt["workflow"]["calls"]
                    )
                    self.assertIsNotNone(engine_graph._cgm_receipt(files[RECEIPT].encode()))
                    if concurrency > 1:
                        # The independent L3 is verified alongside L1.
                        self.assertEqual(
                            [(v["call"], v["label"]) for v in receipt["verdicts"][:3]],
                            [("verify-1", "L1"), ("verify-2", "L3"), ("verify-3", "L2")],
                        )


class DeterminismTests(unittest.TestCase):
    def _adversarial_delay(self, system: str, user: str) -> float:
        # Calls launched later finish first: worker 3 before 1, THEOREM and
        # later lemmas before earlier ones.
        if system == cgm_prompts.VERIFIER_SYSTEM:
            label = _marker(user)["label"]
            return {"L1": 0.06, "L2": 0.04, "L3": 0.02}.get(label, 0.0)
        worker = _worker_of(user)
        return (4 - worker) * 0.03 if worker else 0.0

    def test_completion_order_never_changes_any_artifact(self) -> None:
        for name, scenario in {
            **SCENARIOS,
            **REORDERED_SCENARIOS,
            "sibling-failure": SIBLING_FAILURE,
            "retry-hungry": RETRY_HUNGRY,
        }.items():
            with self.subTest(case=name):
                baseline = _run_files(
                    FakeRoute(scenario, delay=self._adversarial_delay), concurrency=3
                )
                for seed in range(4):
                    again = _run_files(
                        FakeRoute(scenario, jitter=0.03, seed=seed), concurrency=3
                    )
                    self.assertEqual(again, baseline)

    def test_a_later_provider_failure_never_preempts_an_earlier_rejection(self) -> None:
        # L3's verifier call always fails at the provider and L2 is wrong
        # once.  One at a time, L2 is rejected and repaired before L3 is ever
        # tried.  In a wave, L3 fails first, yet L2 (written before it) is
        # still verified and decides: the same repair, then the stop at L3.
        scenario = Scenario(
            blocks=_REORDERED_BLOCKS, wrong=("w1-L2-rev0",), provider_down=("w1-L3",)
        )
        for concurrency, seed in ((1, 1), (2, 2), (3, 3)):
            with self.subTest(concurrency=concurrency):
                route = FakeRoute(scenario, jitter=0.02, seed=seed)
                result, files = _run_files(route, concurrency=concurrency)
                repaired = [
                    re.search(r'<VERIFIER_REPORT block="([A-Z0-9]+)">', call["user"]).group(1)
                    for call in route.calls
                    if "<VERIFIER_REPORT" in call["user"]
                ]
                self.assertEqual(repaired, ["L2"])
                self.assertEqual(result["status"], "unattested")
                receipt = _receipt(files)
                self.assertIn("verifier-provider-failure", receipt["workflow"]["degraded"])
                self.assertIsNotNone(engine_graph._cgm_receipt(files[RECEIPT].encode()))
                stages = [call["stage"] for call in receipt["calls"]]
                self.assertEqual(stages[-1], "curator")
                self.assertEqual(
                    stages,
                    [
                        "strategist", "worker-1", "worker-2", "worker-3",
                        "verify-1", "verify-2", "repair-1", "verify-3", "verify-4",
                        "curator",
                    ]
                    if concurrency == 1
                    else [
                        "strategist", "worker-1", "worker-2", "worker-3",
                        "verify-1", "verify-2", "verify-3", "repair-1", "verify-4",
                        "verify-5", "curator",
                    ],
                )

    def test_sibling_verdicts_depend_only_on_the_configured_concurrency(self) -> None:
        one = _receipt(_run_files(FakeRoute(SIBLING_FAILURE), concurrency=1)[1])
        three = _receipt(_run_files(FakeRoute(SIBLING_FAILURE, jitter=0.02), concurrency=3)[1])
        # One at a time: L1 fails, its sibling L2 is never reached.
        self.assertEqual(
            [(v["call"], v["label"], v["verdict"]) for v in one["verdicts"]][:1],
            [("verify-1", "L1", "wrong")],
        )
        self.assertEqual(one["calls"][4]["stage"], "verify-1")
        self.assertEqual(one["calls"][5]["stage"], "repair-1")
        # A wave of two: L2 is verified alongside; L1 still decides the outcome.
        self.assertEqual(
            [(v["call"], v["label"], v["verdict"]) for v in three["verdicts"]][:2],
            [("verify-1", "L1", "wrong"), ("verify-2", "L2", "correct")],
        )
        self.assertEqual(three["calls"][6]["stage"], "repair-1")
        for receipt in (one, three):
            self.assertEqual(receipt["verdict"], "accepted")
            self.assertTrue(cgm_schema.canonical_call_order([c["stage"] for c in receipt["calls"]]))
        # The repaired candidate reuses L2's cached verdict at concurrency 3.
        self.assertEqual(one["workflow"]["calls"]["verify"], 5)
        self.assertEqual(three["workflow"]["calls"]["verify"], 5)

    def test_calls_in_flight_are_bounded_by_the_concurrency(self) -> None:
        def slow(_system: str, _user: str) -> float:
            return 0.05

        for concurrency, expected in ((1, 1), (2, 2), (3, 3)):
            with self.subTest(concurrency=concurrency):
                route = FakeRoute(SCENARIOS["accepted"], delay=slow)
                _run_files(route, concurrency=concurrency)
                self.assertEqual(route.max_in_flight, expected)
        route = FakeRoute(SCENARIOS["accepted"], delay=slow)
        _, files = _run_files(
            route,
            env={"AGENT_MONITOR_GUEST": "1", "AGENT_MONITOR_CGM_CONCURRENCY": "5"},
        )
        self.assertLessEqual(route.max_in_flight, 2)
        self.assertEqual(_receipt(files)["workflow"]["budgets"]["concurrency"], 2)

    def test_concurrency_budget_resolution(self) -> None:
        api = runner.Route("api", "kimi-k3", "api_chat")
        codex = runner.Route("codex_subscription", "gpt-5.6-sol", "codex_exec")
        claude = runner.Route("claude_subscription", "sonnet", "claude_code")
        default = runner.Budgets.from_env({})
        self.assertIsNone(default.concurrency)
        self.assertEqual(default.resolved_concurrency(api), 3)
        self.assertEqual(default.resolved_concurrency(codex), 1)
        self.assertEqual(default.resolved_concurrency(claude), 1)
        guest = runner.Budgets.from_env({"AGENT_MONITOR_SPONSORED_KIMI": "1"})
        self.assertEqual(guest.resolved_concurrency(api), 2)
        self.assertEqual(guest.resolved_concurrency(codex), 1)
        configured = runner.Budgets.from_env({"AGENT_MONITOR_CGM_CONCURRENCY": "4"})
        self.assertEqual(configured.resolved_concurrency(api), 4)
        self.assertEqual(configured.resolved_concurrency(codex), 4)
        self.assertEqual(
            runner.Budgets.from_env({"AGENT_MONITOR_CGM_CONCURRENCY": "99"}).concurrency,
            cgm_schema.MAX_CONCURRENCY,
        )
        self.assertEqual(
            runner.Budgets.from_env(
                {"AGENT_MONITOR_GUEST": "1", "AGENT_MONITOR_CGM_CONCURRENCY": "6"}
            ).concurrency,
            2,
        )
        self.assertEqual(runner.Budgets(guest=True, concurrency=6).resolved_concurrency(api), 2)
        with self.assertRaises(runner.MathHarnessError):
            runner.Budgets.from_env({"AGENT_MONITOR_CGM_CONCURRENCY": "many"})

    def test_turn_limits_share_spare_calls_round_robin(self) -> None:
        self.assertEqual(runner._turn_limits(3, 0), [1, 1, 1])
        self.assertEqual(runner._turn_limits(3, 2), [2, 2, 1])
        self.assertEqual(runner._turn_limits(3, 7), [4, 3, 3])
        self.assertEqual(runner._turn_limits(3, 8), [4, 4, 3])
        self.assertEqual(runner._turn_limits(2, 99), [4, 4])
        self.assertEqual(runner._turn_limits(1, -3), [1])


class CallOrderContractTests(unittest.TestCase):
    """The shared grammar that both the writer and engine_graph apply."""

    def test_canonical_turn_order(self) -> None:
        valid = (
            ["strategist", "worker-1"],
            # Worker 1 may have made no call (deadline); gaps are allowed.
            ["strategist", "worker-2", "curator"],
            [
                "strategist", "strategist-retry", "worker-1", "worker-1-low",
                "worker-1-retry", "worker-1-retry-low", "worker-3", "verify-1",
                "verify-2", "repair-1", "verify-3", "repair-2", "verify-5", "curator",
            ],
        )
        for stages in valid:
            with self.subTest(stages=stages):
                self.assertTrue(cgm_schema.canonical_call_order(stages))
        invalid = (
            [],
            ["strategist-low", "worker-1"],  # a turn starts with its base call
            ["worker-1", "strategist"],
            ["strategist", "worker-2", "worker-1"],
            ["strategist", "worker-1-retry", "worker-1"],
            ["strategist", "worker-1", "worker-1-retry", "worker-1-low"],
            ["strategist", "worker-1", "verify-1", "worker-2"],
            ["strategist", "worker-1", "verify-2", "verify-1"],
            ["strategist", "worker-1", "verify-1", "worker-1"],
            ["strategist", "worker-1", "curator", "verify-1"],
            ["strategist", "worker-1", "curator", "curator-low", "curator"],
            ["strategist", "worker-1", "verify-1", "verify-1"],
            ["strategist", "worker-1", "critic"],
            ["strategist", "strategist", "worker-1"],
        )
        for stages in invalid:
            with self.subTest(stages=stages):
                self.assertFalse(cgm_schema.canonical_call_order(stages))

    def test_retry_counts_follow_the_stage_grammar(self) -> None:
        self.assertEqual(
            cgm_schema.retry_counts(
                ["strategist-low", "worker-1-retry", "worker-1-retry-low", "verify-2-retry"]
            ),
            {"format": 2, "empty": 2},
        )
        self.assertEqual(cgm_schema.call_turn("verify-12-retry-low"), ("verify", 12, 3))
        self.assertIsNone(cgm_schema.call_turn("verify"))
        self.assertIsNone(cgm_schema.call_turn("curator-1"))


class BudgetExactnessTests(unittest.TestCase):
    def test_calls_never_exceed_the_budget_and_every_call_is_recorded(self) -> None:
        for max_calls in range(4, 17):
            for concurrency in (1, 3):
                with self.subTest(max_calls=max_calls, concurrency=concurrency):
                    outcomes = []
                    for seed in (1, 2):
                        route = FakeRoute(RETRY_HUNGRY, jitter=0.01, seed=seed)
                        env = {"AGENT_MONITOR_CGM_MAX_CALLS": str(max_calls)}
                        try:
                            result, files = _run_files(route, concurrency=concurrency, env=env)
                        except runner.MathHarnessError as exc:
                            self.assertLessEqual(len(route.calls), max_calls)
                            outcomes.append(("failed", str(exc), len(route.calls)))
                            continue
                        receipt = _receipt(files)
                        self.assertLessEqual(len(route.calls), max_calls)
                        self.assertEqual(len(receipt["calls"]), len(route.calls))
                        self.assertEqual(receipt["workflow"]["calls"]["total"], len(route.calls))
                        self.assertEqual(result["calls"], len(route.calls))
                        self.assertIsNotNone(engine_graph._cgm_receipt(files[RECEIPT].encode()))
                        outcomes.append(("published", files))
                    self.assertEqual(outcomes[0], outcomes[1])

    def test_worker_retries_use_only_their_own_reservation(self) -> None:
        # Each worker needs 3 calls (reply, format retry, its low-effort
        # retry).  With 11 calls: strategist 1, then 3 workers keeping one
        # verifier and the curator call leaves 8 = 3 first calls + 5 spare,
        # shared round-robin as [3, 3, 2] -- whatever order they finish in.
        route = FakeRoute(RETRY_HUNGRY, jitter=0.02, seed=7)
        _, files = _run_files(
            route, concurrency=3, env={"AGENT_MONITOR_CGM_MAX_CALLS": "11"}
        )
        receipt = _receipt(files)
        stages = [call["stage"] for call in receipt["calls"]]
        self.assertEqual(
            stages,
            [
                "strategist",
                "worker-1", "worker-1-retry", "worker-1-retry-low",
                "worker-2", "worker-2-retry", "worker-2-retry-low",
                "worker-3", "worker-3-retry",
                "verify-1",
                "curator",
            ],
        )
        self.assertEqual(len(route.calls), 11)
        self.assertEqual(receipt["workflow"]["retries"], {"format": 3, "empty": 2})
        self.assertIn("worker-provider-failure", receipt["workflow"]["degraded"])
        self.assertIn("verification-stopped-by-budget", receipt["workflow"]["degraded"])

    def test_too_small_a_budget_fails_closed_identically(self) -> None:
        # 8 calls leave [2, 2, 1]: no worker can finish its retries.
        for seed in (1, 2):
            route = FakeRoute(RETRY_HUNGRY, jitter=0.02, seed=seed)
            with tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                with self.assertRaisesRegex(runner.MathHarnessError, "no worker produced"):
                    _run(route, workspace, concurrency=3, env={"AGENT_MONITOR_CGM_MAX_CALLS": "8"})
                self.assertEqual(len(route.calls), 6)
                self.assertFalse((workspace / "proof.md").exists())
                self.assertFalse((workspace / "math_harness").exists())


class _RefusedWorkers(FakeRoute):
    """Sponsored Kimi refuses each plan's first worker call unprocessed (a
    fast upstream 503 the transport retries could not get through)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refused: set[int] = set()

    def __call__(self, system: str, user: str, *, model: str, timeout: int = 240, **kwargs: Any):
        plan = _worker_of(user) if "<VERIFIER_REPORT" not in user else 0
        with self.lock:
            first = bool(plan) and plan not in self.refused
            if first:
                self.refused.add(plan)
                self.calls.append(
                    {
                        "system": system,
                        "user": user,
                        "effort": kwargs.get("effort"),
                        "env": kwargs.get("env"),
                    }
                )
        if first:
            failure = kimi_k3.call_failure(
                503,
                {"error": {"type": "upstream_rejected", "forwarded": True,
                           "upstream_status": 503, "upstream_elapsed_ms": 200}},
            )
            raise runner.APIBackendError(kimi_k3.failure_message(failure), failure=failure)
        return super().__call__(system, user, model=model, timeout=timeout, **kwargs)


class SponsoredBusyProviderTests(unittest.TestCase):
    def test_the_rescue_worker_keeps_the_canonical_order_at_any_concurrency(self) -> None:
        sponsored = {"AGENT_MONITOR_SPONSORED_KIMI": "1"}
        with patch.object(runner, "_wait_for_provider", return_value=True):
            for concurrency in (1, 2):
                with self.subTest(concurrency=concurrency):
                    route = _RefusedWorkers(SCENARIOS["accepted"])
                    baseline = _run_files(route, concurrency=concurrency, env=sponsored)
                    for seed in (1, 2):
                        again = _run_files(
                            _RefusedWorkers(SCENARIOS["accepted"], jitter=0.02, seed=seed),
                            concurrency=concurrency,
                            env=sponsored,
                        )
                        self.assertEqual(again, baseline)
                    result, files = baseline
                    receipt = _receipt(files)
                    stages = [call["stage"] for call in receipt["calls"]]
                    # The guest profile runs two workers; both are refused,
                    # then plan 1 runs again as worker 3.
                    self.assertEqual(stages[:4], ["strategist", "worker-1", "worker-2", "worker-3"])
                    self.assertTrue(cgm_schema.canonical_call_order(stages))
                    self.assertEqual(len(receipt["calls"]), len(route.calls))
                    self.assertEqual(result["status"], "accepted")
                    self.assertEqual(receipt["selected_worker"], 1)
                    self.assertIn("worker-busy-rescue", receipt["workflow"]["degraded"])
                    self.assertIsNotNone(engine_graph._cgm_receipt(files[RECEIPT].encode()))


class CancellationTests(unittest.TestCase):
    def _assert_nothing_published(self, workspace: Path) -> None:
        self.assertFalse((workspace / "proof.md").exists())
        self.assertFalse((workspace / RECEIPT).exists())
        leftovers = [
            path for path in workspace.rglob("*") if path.is_file() and ".tmp-" in path.name
        ]
        self.assertEqual(leftovers, [])

    def _hold_worker_two(self, system: str, user: str) -> bool:
        return system != cgm_prompts.STRATEGIST_SYSTEM and _worker_of(user) == 2

    def _stopped_run(self, stop: Callable[[], None]) -> None:
        events: list[dict] = []
        route = FakeRoute(SCENARIOS["accepted"], hold=self._hold_worker_two)

        def stop_when_held() -> None:
            if route.held.wait(timeout=20):
                stop()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            helper = threading.Thread(target=stop_when_held, daemon=True)
            helper.start()
            started = time.monotonic()
            with self.assertRaises(runner.RunCancelled):
                _run(route, workspace, concurrency=3, events=events)
            self.assertLess(time.monotonic() - started, 10)
            helper.join(timeout=5)
            self._assert_nothing_published(workspace)
            seen = len(events)
            # The abandoned call now completes: its reply reaches nothing.
            route.release.set()
            deadline = time.monotonic() + 5
            while len(route.returned) < len(route.calls) and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(len(events), seen, "an abandoned call emitted an event")
            self._assert_nothing_published(workspace)

    def test_sigterm_stops_in_flight_calls_and_publishes_nothing(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers need the main thread")
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
            self.skipTest("the test runner handles SIGTERM itself")
        self._stopped_run(lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

    def test_stop_request_from_another_thread_cancels_the_run(self) -> None:
        self._stopped_run(runner._stop_active_runs)

    def test_stop_during_publication_completes_the_generation(self) -> None:
        original = runner._atomic_write
        stopped: list[bool] = []

        def stop_then_write(root, parts, payload):
            if not stopped:
                stopped.append(True)
                runner._stop_active_runs()
            return original(root, parts, payload)

        with tempfile.TemporaryDirectory() as raw, patch.object(
            runner, "_atomic_write", side_effect=stop_then_write
        ):
            workspace = Path(raw)
            result = _run(FakeRoute(SCENARIOS["accepted"]), workspace, concurrency=3)
            self.assertEqual(stopped, [True])
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(_views(workspace)["attestation"], "harness-gate-receipt")

    def test_sigterm_to_the_runner_process_exits_without_artifacts(self) -> None:
        driver = (
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from agent_monitor.runners import math_harness_runner as runner\n"
            "marker = Path(sys.argv[2])\n"
            "def fake_api(system, user, **kwargs):\n"
            "    if 'Carry out assigned plan' in user:\n"
            "        marker.write_text('held')\n"
            "        time.sleep(60)\n"
            "    return SimpleNamespace(text=json.dumps({'plans': [{'label': 'A', 'technique': 'plain',"
            " 'approach': 'Direct.', 'subgoals': [], 'risks': 'none'}]}), usage={},"
            " model='kimi-k3', provider='kimi')\n"
            "runner.api_chat = fake_api\n"
            "sys.exit(runner.main())\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "ws"
            workspace.mkdir()
            (workspace / "problem.txt").write_text(PROBLEM, encoding="utf-8")
            marker = Path(raw) / "held"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "AGENT_MONITOR_ENV_PATH": os.devnull,
                "AGENT_MONITOR_DATA_DIR": str(Path(raw) / "data"),
                "AGENT_MONITOR_SELECTED_MODEL": "kimi-k3",
                "AGENT_MONITOR_CGM_CONCURRENCY": "3",
            }
            process = subprocess.Popen(
                [sys.executable, "-c", driver, str(REPO), str(marker)],
                cwd=workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 30
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "the worker call never started")
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr[-2000:])
            self.assertLess(time.monotonic() - started, 10)
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            terminal = [
                event["item"]
                for event in events
                if (event.get("item") or {}).get("terminal") is True
            ]
            self.assertEqual(len(terminal), 1)
            self.assertIn("stopped", terminal[0]["message"])
            self._assert_nothing_published(workspace)
            self.assertFalse((workspace / "math_harness").exists())

    def test_deadline_abandons_in_flight_calls_and_publishes_what_settled(self) -> None:
        events: list[dict] = []
        route = FakeRoute(SCENARIOS["accepted"], hold=self._hold_worker_two)
        budgets = runner.Budgets(max_seconds=1, max_workers=3)
        with tempfile.TemporaryDirectory() as raw, patch.object(
            runner, "_DEADLINE_GRACE_SECONDS", 0.3
        ):
            workspace = Path(raw)
            started = time.monotonic()
            result = _run(route, workspace, concurrency=3, budgets=budgets, events=events)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(result["status"], "unattested")
            files = _files(workspace)
            receipt = _receipt(files)
            worker_two = [call for call in receipt["calls"] if call["stage"] == "worker-2"]
            self.assertEqual(len(worker_two), 1)
            self.assertIsNone(worker_two[0]["response_sha256"])
            self.assertEqual(worker_two[0]["usage"], {})
            for reason in (
                "deadline-abandoned-calls",
                "worker-abandoned-at-deadline",
                "verification-stopped-by-budget",
                "curator-skipped-by-budget",
            ):
                self.assertIn(reason, receipt["workflow"]["degraded"])
            self.assertEqual(len(receipt["calls"]), len(route.calls))
            self.assertEqual(_views(workspace)["workflow_status"], "partial")
            seen = len(events)
            # The abandoned worker's reply arrives after publication and
            # changes nothing.
            route.release.set()
            deadline = time.monotonic() + 5
            while len(route.returned) < len(route.calls) and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            self.assertEqual(_files(workspace), files)
            self.assertEqual(len(events), seen)


class ThreadSafetyTests(unittest.TestCase):
    def test_pipeline_never_touches_the_process_environment(self) -> None:
        source = Path(runner.__file__).read_text(encoding="utf-8")
        for pattern in (
            r"os\.environ\[[^\]]+\]\s*=",
            r"os\.environ\.(pop|update|setdefault|clear)\(",
            r"os\.putenv|os\.unsetenv",
            r"del os\.environ",
        ):
            self.assertIsNone(re.search(pattern, source), pattern)
        route = FakeRoute(SCENARIOS["retries"], jitter=0.01, seed=3)
        env = {"AGENT_MONITOR_PROBLEM_ID": "odd-sum-e3", "AGENT_MONITOR_CGM_CONCURRENCY": "3"}
        with tempfile.TemporaryDirectory() as raw:
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(runner, "api_chat", new=route),
            ):
                route.environ_snapshot = dict(os.environ)
                runner.run_pipeline(
                    Path(raw),
                    PROBLEM,
                    runner.Route("api", "kimi-k3", "api_chat"),
                    emit=lambda event: None,
                )
                self.assertEqual(dict(os.environ), env)
        self.assertTrue(route.calls)
        for call in route.calls:
            # Each call gets the run's read-only settings snapshot explicitly.
            self.assertEqual(call["env"], env)
            self.assertIsNot(call["env"], os.environ)

    def test_reasoning_effort_is_a_per_call_argument_under_concurrency(self) -> None:
        route = FakeRoute(SCENARIOS["retries"], jitter=0.02, seed=5)
        _, files = _run_files(
            route,
            concurrency=3,
            env={"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "4096", "KIMI_REASONING_EFFORT": "max"},
        )
        by_kind: dict[str, set[str | None]] = {}
        for call in route.calls:
            kind = {
                cgm_prompts.STRATEGIST_SYSTEM: "strategist",
                cgm_prompts.VERIFIER_SYSTEM: "verify",
                cgm_prompts.CURATOR_SYSTEM: "curator",
            }.get(call["system"], "worker")
            by_kind.setdefault(kind, set()).add(call["effort"])
        # Small output ceiling: workers at low effort, verifiers keep the
        # configured effort, the curator is always low.
        self.assertEqual(
            by_kind,
            {"strategist": {"max"}, "worker": {"low"}, "verify": {"max"}, "curator": {"low"}},
        )
        efforts = {call["stage"]: call["effort"] for call in _receipt(files)["calls"]}
        self.assertEqual(efforts["worker-2-retry"], "low")
        self.assertEqual(efforts["verify-1"], "max")

    def test_concurrent_runs_in_one_process_are_independent(self) -> None:
        solo_events: list[dict] = []
        _, solo = _run_files(FakeRoute(SCENARIOS["accepted"]), concurrency=3, events=solo_events)
        route = FakeRoute(SCENARIOS["accepted"], jitter=0.02, seed=11)
        env = {"AGENT_MONITOR_PROBLEM_ID": "odd-sum-e3", "AGENT_MONITOR_CGM_CONCURRENCY": "3"}
        outcomes: dict[int, tuple[list[dict], dict[str, str]]] = {}
        errors: list[BaseException] = []

        def one(slot: int, workspace: Path) -> None:
            events: list[dict] = []
            try:
                runner.run_pipeline(
                    workspace,
                    PROBLEM,
                    runner.Route("api", "kimi-k3", "api_chat"),
                    emit=events.append,
                )
                outcomes[slot] = (events, _files(workspace))
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
            patch.dict(os.environ, env, clear=True),
            patch.object(runner, "api_chat", new=route),
            patch.object(runner, "_now", return_value=FIXED_TIMESTAMP),
        ):
            threads = [
                threading.Thread(target=one, args=(slot, Path(raw)))
                for slot, raw in enumerate((first, second))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(errors, [])
        solo_kinds = sorted(json.dumps(event, sort_keys=True) for event in solo_events)
        for slot in (0, 1):
            events, files = outcomes[slot]
            self.assertEqual(files, solo)
            # Each sink saw exactly its own run's events.
            self.assertEqual(
                sorted(json.dumps(event, sort_keys=True) for event in events), solo_kinds
            )

    def test_a_blocked_sink_never_delays_another_run(self) -> None:
        _, solo = _run_files(FakeRoute(SCENARIOS["accepted"]), concurrency=3)
        route = FakeRoute(SCENARIOS["accepted"])
        env = {"AGENT_MONITOR_PROBLEM_ID": "odd-sum-e3", "AGENT_MONITOR_CGM_CONCURRENCY": "3"}
        blocked, release = threading.Event(), threading.Event()
        results: dict[str, Any] = {}

        def blocking_sink(_event: dict) -> None:
            # Run A's sink blocks on its first event until run B has finished.
            if not blocked.is_set():
                blocked.set()
                release.wait(timeout=30)

        def one(slot: str, workspace: Path, sink: Callable[[dict], None]) -> None:
            try:
                runner.run_pipeline(
                    workspace, PROBLEM, runner.Route("api", "kimi-k3", "api_chat"), emit=sink
                )
                results[slot] = _files(workspace)
            except BaseException as exc:  # pragma: no cover - reported below
                results[slot] = exc

        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
            patch.dict(os.environ, env, clear=True),
            patch.object(runner, "api_chat", new=route),
            patch.object(runner, "_now", return_value=FIXED_TIMESTAMP),
        ):
            slow = threading.Thread(
                target=one, args=("a", Path(first), blocking_sink), daemon=True
            )
            slow.start()
            self.assertTrue(blocked.wait(timeout=10))
            fast = threading.Thread(
                target=one, args=("b", Path(second), lambda _event: None), daemon=True
            )
            fast.start()
            fast.join(timeout=20)
            finished_while_blocked = not fast.is_alive() and not release.is_set()
            release.set()
            slow.join(timeout=30)
        self.assertTrue(finished_while_blocked, "run B waited for run A's sink")
        self.assertEqual(results["b"], solo)
        self.assertEqual(results["a"], solo)

    def test_abandonment_never_waits_for_a_blocked_sink(self) -> None:
        entered, release = threading.Event(), threading.Event()
        delivered: list[dict] = []

        def sink(event: dict) -> None:
            delivered.append(event)
            entered.set()
            release.wait(timeout=10)

        emitter = runner._RunEmitter(sink)
        ctx = runner._Ctx(
            route=runner.Route("api", "kimi-k3", "api_chat"),
            budgets=runner.Budgets(),
            started=time.monotonic(),
            emitter=emitter,
        )
        turn = ctx.open_turn("worker-1", 1)
        first = threading.Thread(target=ctx.turn_out, args=(turn, {"n": 1}), daemon=True)
        first.start()
        self.assertTrue(entered.wait(timeout=5))
        started = time.monotonic()
        ctx.close_turn(turn, abandon=True)  # the sink is still blocked
        self.assertLess(time.monotonic() - started, 1.0)
        # An event of the abandoned turn is never delivered, even once the
        # sink is free again.
        second = threading.Thread(target=ctx.turn_out, args=(turn, {"n": 2}), daemon=True)
        second.start()
        release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual(delivered, [{"n": 1}])
        # A closed run delivers nothing more.
        emitter.deliver({"n": 3})
        self.assertEqual(delivered, [{"n": 1}, {"n": 3}])
        emitter.close()
        emitter.deliver({"n": 4})
        self.assertEqual(delivered, [{"n": 1}, {"n": 3}])


if __name__ == "__main__":
    unittest.main()
