"""The Quickstart example builder typesets math without rewriting the run's words."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_quickstart_example", ROOT / "scripts/build_quickstart_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("plain", "typeset"),
    [
        ("For every integer x ∈ Z: (a) x is odd", r"For every integer $x \in \mathbb{Z}$: (a) $x$ is odd"),
        ("for all m ∈ Z and all n ∈ Z, m + n ∈ Z, and", r"for all $m \in \mathbb{Z}$ and all $n \in \mathbb{Z}$, $m + n \in \mathbb{Z}$, and"),
        ("the pair (m + n, 1) gives", r"the pair $(m + n, 1)$ gives"),
        ("Z is closed: it is a subring of R.", r"$\mathbb{Z}$ is closed: it is a subring of $\mathbb{R}$."),
        ("Since a and b were arbitrary", r"Since $a$ and $b$ were arbitrary"),
        ("By L1(a) and L1(b), i.e. the write-up's", "By L1(a) and L1(b), i.e. the write-up's"),
        ("x ≡ 1 (mod 2)", r"$x \equiv 1  \pmod{2}$"),
        ("addition in Z/2Z: 1+1=0 modulo 2.", r"addition in $\mathbb{Z}/2\mathbb{Z}$: $1+1=0$ modulo 2."),
        ("L1 = fact 4fd6fa17f43b8308", "L1 = fact 4fd6fa17f43b8308"),
    ],
)
def test_tex_marks_math_only(builder, plain, typeset):
    assert builder.tex(plain) == typeset


def test_committed_example_matches_its_lean_file():
    import hashlib
    import json

    example = json.loads((ROOT / "docs/.vitepress/theme/quickstartExample.json").read_text(encoding="utf-8"))
    lean = (ROOT / "docs/public/examples/odd-sum/Proof.lean").read_bytes()
    assert hashlib.sha256(lean).hexdigest() == example["lean"]["source_sha256"]
    assert example["lean_source"].encode("utf-8") == lean


def test_committed_example_records_why_each_plan_ended():
    import json

    example = json.loads((ROOT / "docs/.vitepress/theme/quickstartExample.json").read_text(encoding="utf-8"))
    closed, dead = example["explore"]["plans"]
    # Plan 1 closed because its attempt produced the accepted target, with two subgoals left open.
    assert (closed["status"], closed["rule"]) == ("closed", "accepted-target")
    assert [(goal["label"], goal["status"], goal["score"]) for goal in closed["subgoals"] if goal["status"] != "closed"] == [
        ("S2", "open", 0.6),
        ("S4", "open", 0.6),
    ]
    assert (dead["status"], dead["rule"]) == ("dead", "dead-member")
    assert all(goal["status"] == "dead" for goal in dead["subgoals"])


def test_plan_rules_match_the_curator(builder):
    curation = (ROOT / "agent_monitor/cgm/curation.py").read_text(encoding="utf-8")
    for reason in builder.PLAN_RULES:
        assert reason in curation


def test_parse_proof_reads_numbered_and_labelled_closures(builder):
    from agent_monitor.cgm import closure

    facts = [
        {"fact_id": "a" * 16, "statement": "A", "proof": "base", "predecessors": []},
        {"fact_id": "b" * 16, "statement": "B", "proof": "Cited facts: L1 = fact " + "a" * 16 + ".\n\nby L1", "predecessors": ["a" * 16]},
        {"fact_id": "c" * 16, "statement": "C", "proof": "by L2", "predecessors": ["a" * 16, "b" * 16]},
    ]
    numbered = closure.render_verified_closure(facts, "c" * 16)
    labelled = closure.render_verified_closure(facts, "c" * 16, labels={"a" * 16: "L1", "b" * 16: "L2"})
    for text, names in ((numbered, ["Lemma 1", "Lemma 2"]), (labelled, ["Lemma L1", "Lemma L2"])):
        intro, items = builder.parse_proof(text)
        assert intro.startswith("This proof is the verified fact closure")
        assert [item["label"] for item in items] == [*names, "Main theorem"]
        assert [item["fact"] for item in items] == ["a" * 16, "b" * 16, "c" * 16]
        assert [item["uses"] for item in items] == [[], names[:1], names]
        assert items[1]["proof"].endswith("by L1")
