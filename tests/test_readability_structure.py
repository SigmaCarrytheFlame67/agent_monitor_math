"""How Readability finds the results of a Markdown or text manuscript.

A verified fact closure of an Ansatz CGM run (``proof.md``: the numbered
``verified-closure@1``, the labelled ``verified-closure@2``, and the rendering
of a candidate the verifier did not accept) is read by its own grammar, also
after a copy from the console; anything that does not keep that shape falls
back to Analyze's segmentation, which (for Readability only) also reads
results from Markdown headings.  The fixtures are real proof.md files of live
CGM runs (Kimi K3 and Claude Code, 2026-10-22).
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor.analyze import text_structure  # noqa: E402
from agent_monitor.cgm import closure as cgm_closure  # noqa: E402
from agent_monitor.readability import prompts, structure  # noqa: E402
from agent_monitor.runners import math_harness_runner as harness  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "readability"
V1_EVEN = (FIXTURES / "closure_v1_even.md").read_text(encoding="utf-8")
V2_EVEN = (FIXTURES / "closure_v2_even.md").read_text(encoding="utf-8")
V1_PRIMES = (FIXTURES / "closure_v1_primes.md").read_text(encoding="utf-8")


def segment(text: str, fmt: str = "md") -> dict:
    return structure.segment_text(text, file="pasted." + fmt, fmt=fmt)


def edges(document: dict) -> set[tuple[str, str]]:
    return {(edge["from"], edge["to"]) for edge in document["edges"]}


def as_copied(text: str) -> str:
    """proof.md as copied from the console's rendered view: no heading
    marks, no bold marks."""
    return re.sub(r"(?m)^#{1,6} ", "", text).replace("**", "")


# ---------------------------------------------------------------------------
# fact closures
# ---------------------------------------------------------------------------


def test_a_numbered_closure_gives_one_result_per_fact_with_its_uses():
    document = segment(V1_EVEN)
    assert document["parser"] == "text-1+cgm-closure" and document["title"] == "Proof"
    assert document["warnings"] == []
    nodes = {node["id"]: node for node in document["nodes"]}
    assert list(nodes) == ["lemma-1", "lemma-2", "lemma-3", "main-theorem"]
    assert [prompts.printed(node) for node in document["nodes"]] == ["Lemma 1", "Lemma 2", "Lemma 3", "Main theorem"]
    assert [node["kind"] for node in document["nodes"]] == ["lemma", "lemma", "lemma", "theorem"]
    assert [node["main_result"] for node in document["nodes"]] == [False, False, False, True]
    assert nodes["lemma-2"]["statement"] == ("For every natural number n (a nonnegative integer), there exists a "
                                             "natural number k such that n = 2k or n = 2k + 1.")
    assert nodes["lemma-2"]["proof"].startswith("By induction on n.\nBase case n = 0")
    assert nodes["lemma-2"]["proof"].endswith("By induction it holds for every natural n.")
    main = nodes["main-theorem"]
    assert main["statement"] == "Prove that for every natural number n, n^2 + n is even."
    assert main["proof"].startswith("Cited facts: L1 = fact a9621d30a1c72370")
    # Lines are 1-based: the heading, the last statement line; the proof's own.
    lines = V1_EVEN.split("\n")
    assert lines[main["lines"][0] - 1] == "## Main theorem · fact 1914e15b87add6fe"
    assert lines[main["lines"][1] - 1] == main["statement"]
    assert lines[main["proof_lines"][0] - 1] == "**Proof.**"
    assert lines[main["proof_lines"][1] - 1].startswith("In either case n^2 + n is even.")
    # The Uses line names facts by id: every lemma is used by the main theorem.
    assert edges(document) == {("lemma-1", "main-theorem"), ("lemma-2", "main-theorem"),
                               ("lemma-3", "main-theorem")}
    assert {(edge["provenance"], edge["context"]) for edge in document["edges"]} == {("ref", "proof")}


def test_a_labelled_closure_keeps_the_labels_its_proofs_cite():
    document = segment(V2_EVEN)
    assert [node["id"] for node in document["nodes"]] == ["lemma-l1", "lemma-l2", "lemma-l3", "main-theorem"]
    assert [prompts.printed(node) for node in document["nodes"]] == ["Lemma L1", "Lemma L2", "Lemma L3",
                                                                     "Main theorem"]
    assert edges(document) == {("lemma-l1", "main-theorem"), ("lemma-l2", "main-theorem"),
                               ("lemma-l3", "main-theorem")}


def test_uses_between_lemmas_become_edges():
    # Lemma 3 uses Lemma 2; the main theorem uses Lemmas 1 and 3 (its proof's
    # own "L2"/"L3" names do not matter: the Uses line names the facts).
    document = segment(V1_PRIMES)
    assert edges(document) == {("lemma-2", "lemma-3"), ("lemma-1", "main-theorem"), ("lemma-3", "main-theorem")}
    statement = next(node for node in document["nodes"] if node["id"] == "lemma-1")["statement"]
    assert statement.startswith("Let k ≥ 1 be an integer") and statement.endswith("p_i does not divide N.")


@pytest.mark.parametrize("text", [V1_EVEN, V2_EVEN, V1_PRIMES])
def test_a_closure_copied_from_the_console_reads_the_same(text):
    expected = segment(text)
    for fmt in ("md", "txt"):
        copied = segment(as_copied(text), fmt)
        assert copied["parser"] == "text-1+cgm-closure"
        assert [(node["id"], node["statement"], node["main_result"]) for node in copied["nodes"]] == [
            (node["id"], node["statement"], node["main_result"]) for node in expected["nodes"]]
        assert edges(copied) == edges(expected)


FACTS = [
    {"fact_id": "a" * 16, "statement": "Every n is 2k or 2k + 1.", "proof": "Induction.", "predecessors": []},
    {"fact_id": "b" * 16, "statement": "n(n + 1) is even.", "proof": "By the parity lemma.",
     "predecessors": ["a" * 16]},
    {"fact_id": "c" * 16, "statement": "Prove that n^2 + n is even.", "proof": "n^2 + n = n(n + 1).",
     "predecessors": ["b" * 16, "a" * 16]},
]


@pytest.mark.parametrize("labels", [None, {"a" * 16: "P1", "b" * 16: "EVEN"}])
def test_both_closure_renderings_parse(labels):
    # verified-closure@1 numbers the lemmas, @2 names them by their labels.
    text = cgm_closure.render_verified_closure(FACTS, "c" * 16, labels=labels)
    document = segment(text)
    first, second = ("lemma-p1", "lemma-even") if labels else ("lemma-1", "lemma-2")
    assert [node["id"] for node in document["nodes"]] == [first, second, "main-theorem"]
    assert edges(document) == {(first, second), (first, "main-theorem"), (second, "main-theorem")}
    assert [node["statement"] for node in document["nodes"]] == [
        "Every n is 2k or 2k + 1.", "n(n + 1) is even.", "Prove that n^2 + n is even."]


def _candidate(*, theorem: bool) -> str:
    """The rendering of a candidate the verifier did not accept, from the
    runner's own renderer."""
    block = SimpleNamespace
    lemmas = [block(label="L1", uses=(), statement="Every n is 2k or 2k + 1.", proof="Induction on n."),
              block(label="L2", uses=("L1",), statement="n(n + 1) is even.", proof="By L1, in two cases.")]
    candidate = SimpleNamespace(
        lemmas=lemmas, status="partial",
        theorem=block(label="THEOREM", uses=("L1", "L2"), statement="", proof="n^2 + n = n(n + 1); use L2.")
        if theorem else None,
        findings=[SimpleNamespace(kind="gap", claim="The case split in L2 is not justified.")])
    result = SimpleNamespace(best_attempt=SimpleNamespace(candidate=candidate, verified_labels=["L1"]),
                             problem="Prove that for every natural number n, n^2 + n is even.")
    return harness._render_unverified(result, "the verifier rejected L2.")


def test_a_candidate_the_verifier_did_not_accept_parses_with_its_statuses():
    text = _candidate(theorem=True)
    assert "## Lemma L2 (not verifier-accepted)" in text and "## Findings (unverified)" in text
    document = segment(text)
    assert document["parser"] == "text-1+cgm-closure"
    assert [(node["id"], node["title"]) for node in document["nodes"]] == [
        ("lemma-l1", "verifier-accepted"), ("lemma-l2", "not verifier-accepted"),
        ("main-theorem", "not verifier-accepted")]
    # "Uses: L1, L2." names the lemmas by label.
    assert edges(document) == {("lemma-l1", "lemma-l2"), ("lemma-l1", "main-theorem"), ("lemma-l2", "main-theorem")}
    main = document["nodes"][-1]
    assert main["statement"] == "Prove that for every natural number n, n^2 + n is even."
    # The findings that follow are not part of the main theorem's proof.
    assert main["proof"] == "n^2 + n = n(n + 1); use L2." and main["main_result"] is True
    # Without a proof of the main theorem, the lemmas are still results.
    partial = segment(_candidate(theorem=False))
    assert [node["id"] for node in partial["nodes"]] == ["lemma-l1", "lemma-l2"]
    assert partial["nodes"][1]["proof"] == "By L1, in two cases."
    assert [node["main_result"] for node in partial["nodes"]] == [False, True]


def test_a_uses_line_naming_a_fact_outside_the_text_is_reported():
    text = V1_EVEN.replace("**Uses:** none.", "**Uses:** Lemma 9 (fact 0123456789abcdef).", 1)
    document = segment(text)
    assert document["parser"] == "text-1+cgm-closure"
    assert document["warnings"] == ["1 fact(s) named in Uses lines are not in the text"]


@pytest.mark.parametrize("change", [
    # A block without its proof.
    lambda text: text.replace("**Proof.**\n\nFix natural k", "Fix natural k", 1),
    # Text between a heading and its statement.
    lambda text: text.replace("**Uses:** none.", "**Uses:** none.\n\nAn aside.", 1),
    # A result of its own before the closure.
    lambda text: text.replace("# Proof\n", "# Proof\n\n**Theorem 1.** Every even number is even.\n", 1),
    # The main theorem before a lemma.
    lambda text: text.replace("## Lemma 1 · fact", "## Main theorem · fact 0123456789abcdef\n\n**Uses:** none.\n\n"
                              "**Statement.**\n\nS.\n\n**Proof.**\n\nP.\n\n## Lemma 1 · fact", 1),
    # Two lemmas with one label.
    lambda text: text.replace("## Lemma 2 · fact", "## Lemma 1 · fact", 1),
    # A fact id that is not 16 hex digits.
    lambda text: text.replace("fact 57e467472fd97ed4", "fact 57e46747", 1),
])
def test_anything_short_of_a_closure_falls_back_to_the_generic_reading(change):
    changed = change(V1_EVEN)
    assert changed != V1_EVEN
    document = segment(changed)
    assert document["parser"] == "text-1"
    # The generic reading still finds the headings that name results.
    assert "lemma-3" in {node["id"] for node in document["nodes"]}


FURTHER = ("\n## Further results\n\n**Theorem 5.** Every odd square is $1 \\bmod 8$.\n\n"
           "**Proof.** Write it as $(2k + 1)^2$. QED\n\n**Corollary 6.** No square is $3 \\bmod 4$.\n")


@pytest.mark.parametrize(("closure", "tail"), [
    pytest.param(V1_EVEN, FURTHER, id="heading"),
    pytest.param(V1_EVEN, "\n**Theorem 5.** Every odd square is $1 \\bmod 8$.\n", id="no-heading"),
    # After a candidate's closing sections.
    pytest.param(_candidate(theorem=True), FURTHER, id="after-findings"),
    pytest.param(_candidate(theorem=False), "\n**Remark 7.** The findings are open.\n", id="after-no-proof"),
])
def test_results_after_a_closure_are_not_swallowed_by_its_last_proof(closure, tail):
    document = segment(closure + tail)
    assert document["parser"] == "text-1"
    nodes = document["nodes"]
    assert {node["id"] for node in nodes} & {"theorem-5", "remark-7"}
    assert not any(text in node["proof"] for node in nodes for text in ("Every odd square", "findings are open"))
    # The closure's lemmas are still read, with their statements.
    lemmas = [node["statement"] for node in segment(closure)["nodes"] if node["kind"] == "lemma"]
    assert lemmas and set(lemmas) <= {node["statement"] for node in nodes}


def test_steps_and_claims_in_the_last_proof_keep_the_closure():
    text = V1_EVEN.replace("In either case n^2 + n is even.",
                           "Step 1: n is even or odd.\n**Claim 2.** Both cases work.\nIn either case n^2 + n is even.")
    document = segment(text)
    assert document["parser"] == "text-1+cgm-closure"
    assert "Step 1: n is even or odd.\n**Claim 2.** Both cases work." in document["nodes"][-1]["proof"]


def test_the_sandboxed_segmenter_reads_a_closure(tmp_path):
    path = tmp_path / "pasted.md"
    path.write_text(V2_EVEN, encoding="utf-8")
    document = structure.segment_sandboxed(path, tmp_path / "out.json", file="pasted.md", fmt="md")
    assert document["parser"] == "text-1+cgm-closure"
    assert [node["id"] for node in document["nodes"]] == ["lemma-l1", "lemma-l2", "lemma-l3", "main-theorem"]
    assert not (tmp_path / "out.json").exists()


# ---------------------------------------------------------------------------
# result headings (Readability only)
# ---------------------------------------------------------------------------

HEADINGS = """# Notes

## Theorem 2.1 (Main)

Let $G$ be a group. Then $G$ is fine.

**Proof.** By Lemma L2 and Lemma 3. QED

### Lemma L2 · the key step

Every subgroup is fine.

## Lemma 3: Bounding

**Lemma 3.** The bound holds.

Proof. By Lemma L2. ∎

## Main theorem

All is well.

## Proposition A.1.

A remark that is a result.

## Lemma 4 bounds the error

## Theorem

## lemma 5

## Definitions
"""


def test_markdown_headings_that_name_results_open_them():
    document = text_structure.segment_text(HEADINGS, file="notes.md", fmt="md", heading_results=True)
    found = [(node["id"], prompts.printed(node), node["title"], node["statement"]) for node in document["nodes"]]
    assert found == [
        ("theorem-2-1", "Theorem 2.1", "Main", "Let $G$ be a group. Then $G$ is fine."),
        ("lemma-l2", "Lemma L2", "the key step", "Every subgroup is fine."),
        # The header repeating the heading's result is the same result.
        ("lemma-3", "Lemma 3", "Bounding", "The bound holds."),
        ("n4", "Main theorem", None, "All is well."),
        ("proposition-a-1", "Proposition A.1", None, "A remark that is a result."),
    ]
    nodes = {node["id"]: node for node in document["nodes"]}
    assert nodes["n4"]["main_result"] and nodes["theorem-2-1"]["main_result"]
    assert nodes["theorem-2-1"]["has_proof"] and nodes["lemma-3"]["has_proof"]
    # Each heading is still a section; a result's own heading is not its section.
    assert [section["title"] for section in nodes["lemma-l2"]["section"]] == ["Notes", "Theorem 2.1 (Main)"]
    assert "Lemma 4 bounds the error" in [section["title"] for section in document["sections"]]
    # Letter labels are cited too.
    assert edges(document) == {("lemma-l2", "theorem-2-1"), ("lemma-3", "theorem-2-1"), ("lemma-l2", "lemma-3")}


def test_a_header_under_a_result_heading_is_the_same_result():
    text = ("## Main theorem\n\n**Theorem 1.1.** Every even number is even.\n\n"
            "## Lemma 3\n\n**Theorem 3.** A different result.\n")
    document = text_structure.segment_text(text, file="notes.md", fmt="md", heading_results=True)
    found = [(prompts.printed(node), node["title"], node["statement"], node["main_result"])
             for node in document["nodes"]]
    assert found == [
        # The heading's result, numbered as its header numbers it.
        ("Theorem 1.1", "Main theorem", "Every even number is even.", True),
        # Another kind is another result; the heading, with nothing under it,
        # is a section only.
        ("Theorem 3", None, "A different result.", False),
    ]
    assert [section["title"] for section in document["sections"]] == ["Main theorem", "Lemma 3"]


@pytest.mark.parametrize("title", [
    # A dot followed by a digit belongs to the number, which is never cut
    # short to make the rest a title ("Lemma 4", titled "1 bounds ...").
    "Lemma 4.1 bounds the error", "Theorem 2.1.3 is the main result", "Proposition A.1 implies B",
    "Theorem 1.2a holds", "Lemma 2.1 (Name) extra", "Example 3.1 — rotations", "Claim 1.1 and why",
    "Lemma L2.1 more", "Lemma 4 bounds the error",
])
def test_a_heading_whose_number_runs_into_words_names_no_result(title):
    assert text_structure._heading_result(title) is None


@pytest.mark.parametrize(("title", "number", "name"), [
    ("Lemma 4.1.", "4.1", None), ("Theorem 1.2a. Bounds", "1.2a", "Bounds"), ("Proposition A.1: Bound", "A.1", "Bound"),
    ("Lemma L2.1 · the key step", "L2.1", "the key step"), ("Theorem 2.1.3 (Main)", "2.1.3", "Main"),
    ("Lemma A. Title", "A", "Title"),
])
def test_a_heading_keeps_its_whole_number(title, number, name):
    result = text_structure._heading_result(title)
    assert (result["number"], result["title"]) == (number, name)


def test_results_named_with_dotted_numbers_keep_them():
    # A section heading that is a sentence about a result names none.
    text = ("## Lemma 4.1 bounds the error\n\n**Lemma 4.1.** The error is small.\n\n"
            "## Theorem 4.2 and its consequences\n\n**Theorem 4.2.** All is well.\n\nProof. By Lemma 4.1. QED\n\n"
            "## Lemma 4\n\nA different lemma.\n")
    document = text_structure.segment_text(text, file="notes.md", fmt="md", heading_results=True)
    assert [(node["id"], prompts.printed(node)) for node in document["nodes"]] == [
        ("lemma-4-1", "Lemma 4.1"), ("theorem-4-2", "Theorem 4.2"), ("lemma-4", "Lemma 4")]
    assert edges(document) == {("lemma-4-1", "theorem-4-2")}


def test_a_header_after_a_paragraph_under_its_heading_states_the_result():
    # The paragraph introduces the result; the header states it.
    text = ("# Paper\n\n## Main theorem\n\nWe now state our main result, which answers the question of Smith.\n\n"
            "**Theorem 1.1.** Every even number is even.\n\n**Proof.** Trivial. QED\n")
    found = [(prompts.printed(node), node["title"], node["statement"], node["main_result"], node["has_proof"])
             for node in text_structure.segment_text(text, file="n.md", fmt="md", heading_results=True)["nodes"]]
    assert found == [("Theorem 1.1", "Main theorem", "Every even number is even.", True, True)]
    # A proof in between makes the paragraph the heading's statement.
    proved = text.replace("Smith.\n\n", "Smith.\n\n**Proof.** Clear. QED\n\n")
    found = [(prompts.printed(node), node["statement"][:12])
             for node in text_structure.segment_text(proved, file="n.md", fmt="md", heading_results=True)["nodes"]]
    assert found == [("Main theorem", "We now state"), ("Theorem 1.1", "Every even n")]


def test_a_result_heading_with_nothing_under_it_is_a_section_only():
    text = ("## Theorem 2.1 (Fermat)\n\n### Statement\n\nFor all $n > 2$ there are no solutions.\n\n"
            "### Proof\n\nWiles.\n\n## Lemma 5\n")
    document = text_structure.segment_text(text, file="n.md", fmt="md", heading_results=True)
    assert document["nodes"] == []
    assert [section["title"] for section in document["sections"]] == [
        "Theorem 2.1 (Fermat)", "Statement", "Proof", "Lemma 5"]
    # A heading right above a proof still names the result it proves.
    proved = text_structure.segment_text("## Theorem 2 (Primes)\n\n**Proof.** Euclid. QED\n", file="n.md", fmt="md",
                                         heading_results=True)
    assert [(prompts.printed(node), node["title"], node["has_proof"]) for node in proved["nodes"]] == [
        ("Theorem 2", "Primes", True)]


def test_a_closure_read_generically_takes_each_statement_after_its_marker():
    # Not clearly a closure (an aside under a heading): the generic reading
    # passes over the Uses lines and starts each statement at its marker.
    text = V1_EVEN.replace("**Uses:** none.", "**Uses:** none.\n\nAn aside.", 1)
    document = segment(text)
    assert document["parser"] == "text-1"
    closure = segment(V1_EVEN)["nodes"]
    assert [(node["statement"], node["proof"], node["main_result"]) for node in document["nodes"]] == [
        (node["statement"], node["proof"], node["main_result"]) for node in closure]


def test_analyze_reads_markdown_headings_as_sections_only():
    # Analyze never passes heading_results: its segmentation is unchanged.
    document = text_structure.segment_text(HEADINGS, file="notes.md", fmt="md")
    assert [node["id"] for node in document["nodes"]] == ["lemma-3"]
    assert document["edges"] == []
    assert text_structure.segment_text(V1_EVEN, file="pasted.md", fmt="md")["nodes"] == []


def test_readability_reads_headings_when_the_text_is_no_closure():
    document = segment(HEADINGS)
    assert document["parser"] == "text-1"
    assert [node["id"] for node in document["nodes"]][:3] == ["theorem-2-1", "lemma-l2", "lemma-3"]


def test_heading_titles_are_matched_in_linear_time():
    import time

    hostile = ["Lemma " + "1." * 30_000 + "x", "Main " + " " * 50_000 + "theorem x", "**" * 40_000 + "Lemma 3 x",
               "Theorem 2 (" + "(" * 40_000, "Lemma L" + "1" * 60_000 + " y"]
    started = time.monotonic()
    for title in hostile:
        text_structure._heading_result(title)
    assert time.monotonic() - started < 2
