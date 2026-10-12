"""Source adapters for the problem catalogue, on small synthetic fixtures (no network).

The Kourovka fixture mimics the layout of the arXiv PDF text (running heads,
issue sections, header and part markers, notation asterisks, URLs broken across
lines) with made-up problem text.
"""
import json

import pytest

from agent_monitor import problem_sources as ps

ERDOS_YAML = '''- number: "1"
  prize: "$500"
  informal_status:
    state: "disproved"
    last_update: "2025-08-31"
  formal_status:
    state: "Lean"
    last_update: "2026-09-03"
  formalized:
    state: "yes"
  tags: ["number theory"]

- number: "7"
  prize: "no"
  informal_status:
    state: "verifiable"
    last_update: "2025-08-31"
  formal_status:
    state: "unformalized"
  formalized:
    state: "no"
  tags: ["number theory", "covering systems"]
  comments: "Erdős-Straus conjecture"

- number: "411"
  prize: "no"
  informal_status:
    state: "open"
  formal_status:
    state: "Lean"
    url: "https://github.com/example/proof/blob/abc/P.lean#L1"
    note: "Not yet digested by a human reader."
  formalized:
    state: "yes"
  tags: ["number theory"]
  comments: "partial r=2 result"

- number: "1163"
  prize: "no"
  informal_status:
    state: "open"
  formal_status:
    state: "unformalized"
  formalized:
    state: "no"
  tags: ["group theory"]
  comments: "ambiguous statement"
'''


def test_erdos_yaml_keeps_every_state_and_separates_names_from_remarks():
    problems = ps.parse_erdos_yaml(ERDOS_YAML)
    assert list(problems) == ["1", "7", "411", "1163"]
    assert problems["1"]["state"] == "disproved" and problems["1"]["prize"] == "$500"
    assert problems["7"]["names"] == ["Erdős-Straus conjecture"] and "prize" not in problems["7"]
    assert "names" not in problems["411"]  # "partial r=2 result" is a status remark, not a name
    assert problems["411"]["formal_url"].startswith("https://github.com/") and problems["411"]["formal_state"] == "Lean"
    assert problems["1163"]["ambiguous"] is True and "names" not in problems["1163"]
    assert problems["7"]["line"] == ERDOS_YAML.splitlines().index('- number: "7"') + 1


def test_erdos_fact_file_pins_the_commit_and_fails_closed():
    fact = ps.erdos_fact_file(ERDOS_YAML, commit="a" * 40, fetched_at="2026-10-11T00:00:00Z")
    assert fact["revision"] == "a" * 40 and fact["counts"]["problems"] == 4
    assert fact["sha256"] == ps.sha256_bytes(ERDOS_YAML.encode())
    with pytest.raises(ps.SourceError, match="commit SHA"):
        ps.erdos_fact_file(ERDOS_YAML, commit="main", fetched_at="x")
    previous = {"problems": {str(n): {} for n in range(1, 101)}}
    with pytest.raises(ps.SourceError, match="refusing"):
        ps.erdos_fact_file(ERDOS_YAML, commit="a" * 40, fetched_at="x", previous=previous)
    with pytest.raises(ps.SourceError, match="unknown state"):
        ps.parse_erdos_yaml(ERDOS_YAML.replace('"verifiable"', '"probably true"'))


AI_WIKI = """**The wiki is no longer updated. The latest data is as of Jun 30, 2026.**

## 1. Primary contributions

### 1(a). AI standalone

| Problem | AI systems | Date | Outcome |
| ------- | ---------- | ---- | ------- |
| [[1]](https://www.erdosproblems.com/1) | System A | 24 Jan, 2026 | 🟢 Full solution
| [[7]](https://www.erdosproblems.com/7) | System B, System C | 3 Jun, 2026 | 🟡 Partial result (Lean)
| [[8]](https://www.erdosproblems.com/8) | System B | 3 Jun, 2026 | 🟡 Solution to variant problem
| [[9]](https://www.erdosproblems.com/9) | System A | 11 Jan, 2026 | 🔴 Incorrect proof found
| [[10]](https://www.erdosproblems.com/10) | System A | 12 Jan, 2026 | 🟢 New proof found (Lean)

### 1(d). AI collaborating with humans

| Problem | AI systems | Date | Outcome |
| ------- | ---------- | ---- | ------- |
| [[7]](https://www.erdosproblems.com/7) | System D | 23 Apr-26 May, 2026 | ⚪ Candidate full solution
| [[11]](https://www.erdosproblems.com/11) | System D | 2 Feb, 2026 | 🟡 Partial result
| [[12]](https://www.erdosproblems.com/12) | System D | 2 Feb, 2026 | 🟡 Partial result
| [[13]](https://www.erdosproblems.com/13) | System D | 2 Feb, 2026 | 🟡 Partial result
| [[14]](https://www.erdosproblems.com/14) | System D | 2 Feb, 2026 | 🟡 Partial result
| [[15]](https://www.erdosproblems.com/15) | System D | 2 Feb, 2026 | 🟡 Partial result

## 2. Secondary contributions

### 2(b). Formalization

| Problem | Proof to formalize | AI systems | Date |
| ------- | ------------------ | ---------- | -----|
| [[1]](https://www.erdosproblems.com/1) | 🟢 Somebody (2023) | System E | 25 Feb, 2026
"""


def test_ai_wiki_rows_summarize_primary_outcomes_without_variants_or_new_proofs():
    fact = ps.ai_wiki_fact_file(AI_WIKI, commit="c" * 40, fetched_at="2026-10-11T00:00:00Z")
    problems = fact["problems"]
    assert fact["frozen_on"] == "2026-06-30" and fact["url"].endswith("/" + "c" * 40)
    assert problems["1"]["best_primary"] == "full" and problems["1"]["full_solution"] is True
    assert problems["1"]["sections"] == ["1(a)", "2(b)"]  # the formalization row is secondary
    assert problems["7"]["partial_result"] is True and problems["7"]["best_primary"] == "partial"
    assert problems["7"]["systems"] == ["System B", "System C", "System D"]
    assert problems["7"]["last_primary_date"] == "2026-06-03"
    assert "partial_result" not in problems["8"]  # a variant is not a partial result of the problem
    assert problems["9"]["best_primary"] == "incorrect"
    assert "full_solution" not in problems["10"]  # a new proof of a solved problem
    with pytest.raises(ps.SourceError):
        ps.parse_ai_wiki("# nothing here\n")


def kourovka_pages():
    """Synthetic pages laid out like the arXiv PDF text (made-up problem text)."""
    return [
        "Contents\nProblems from the 1st Issue (1965) . . . . 2\nArchive of Solved Problems . . . . 5",
        "2\nProblems from the 1st Issue (1965)\n"
        "1.3.(Well-known problem). Is every widget of type G ∗ H a gadget? A. Author\n"
        "1.5.Let W∗(F k) be the widget algebra. Is W∗(F 2)∼=W∗(F 3)? B. Author\n"
        "∗1.6.Is every finite widget cyclic? C. Author\n"
        "∗No, not always (AI solution confirmed by D. Expert: E. Model,Preprint, 2026,https:\n"
        "//zenodo.org/records/123).\n"
        "♯1.8.Does a widget with property (∗) exist? F. Author\n"
        "♯Yes, it does (unverified AI solution: G. Claimant,Preprint, 2026,https://zenodo.\n"
        "org/records/456).\n",
        "1st Issue (1965) 3\n"
        "1.9.a) Classify the widgets.\n"
        "♯b) Describe the gadgets of rank two. H. Author\n"
        "♯b) These gadgets are described (unverified AI solution: I. Claimant,Preprint,\n"
        "2026,https://zenodo.org/records/789).\n"
        "1.10.Let G be a widget.∗a) Is G finite?\n"
        "b) Is G cyclic? J. Author\n"
        "∗a) Yes (K. Person,Preprint, 2026,https://arxiv.org/abs/2601.\n"
        "00001; unverified AI solution: L. Claimant,Preprint, 2026,https://zenodo.org/records/1011).\n"
        "1.11.Let δ ∗(x)f be the twisted map and τ(a ∗ij) its image. Is it trivial? M. Author\n",
        "4\nProblems from the 2nd Issue (1966)\n"
        "♯2.1.(Well-known problem). Is every gadget a widget? N. Author\n"
        "♯a) Is it true for finite gadgets?\n"
        "∗b) Is it true for cyclic gadgets?\n"
        "♯a) Yes (unverified AI solution: O. Claimant,Preprint, 2026,https://zenodo.org/records/1213).\n"
        "∗b) Yes (P. Person,Preprint, 2024,https://arxiv.org/abs/2406.00002).\n"
        "1.77. A reference line that is not a heading of this issue.\n"
        "2.4.Is every gadget finite? Q. Author\n",
        "5\nArchive of solved problems\n1.1.An archived question.\n",
    ]


def test_kourovka_markers_on_header_and_parts_are_separate_and_notation_is_ignored():
    parsed = ps.parse_kourovka_pages(kourovka_pages())
    problems = parsed["problems"]
    assert list(problems) == ["1.3", "1.5", "1.6", "1.8", "1.9", "1.10", "1.11", "2.1", "2.4"]
    assert parsed["archive_page"] == 5 and parsed["stray_headings"] == ["1.77"]
    # ∗ inside mathematics is notation: G ∗ H, W∗(F_k), property (∗), δ ∗(x)f, a ∗ij.
    for number in ("1.3", "1.5", "1.11"):
        assert problems[number]["header_marker"] is None and problems[number]["part_markers"] == []
    assert problems["1.8"]["header_marker"] == "#"
    assert problems["1.6"]["header_marker"] == "*" and problems["1.6"]["ai"] == "confirmed"
    assert problems["1.6"]["confirmer"] == "D. Expert"
    assert problems["1.9"]["header_marker"] is None
    assert problems["1.9"]["part_markers"] == [{"part": "b", "marker": "#"}]
    # A part marker may follow sentence punctuation mid-line ("...widget.∗a) Is ...").
    assert problems["1.10"]["part_markers"] == [{"part": "a", "marker": "*"}]
    assert problems["2.1"]["header_marker"] == "#"
    assert problems["2.1"]["part_markers"] == [{"part": "a", "marker": "#"}, {"part": "b", "marker": "*"}]
    assert problems["1.3"]["page"] == 2 and problems["1.9"]["page"] == 3 and problems["2.4"]["page"] == 4


def test_kourovka_solution_links_are_attributed_to_their_marker_part_and_ai_qualifier():
    problems = ps.parse_kourovka_pages(kourovka_pages())["problems"]
    assert problems["1.6"]["solutions"] == [{"marker": "*", "url": "https://zenodo.org/records/123", "ai": "confirmed"}]
    assert problems["1.8"]["solutions"] == [{"marker": "#", "url": "https://zenodo.org/records/456", "ai": "unverified"}]
    # The first "♯b)" is the part's statement; the second is its answer.
    assert problems["1.9"]["solutions"] == [{"marker": "#", "part": "b", "url": "https://zenodo.org/records/789", "ai": "unverified"}]
    # One citation can mix a human solution with an unverified AI one.
    assert problems["1.10"]["solutions"] == [
        {"marker": "*", "part": "a", "url": "https://arxiv.org/abs/2601.00001"},
        {"marker": "*", "part": "a", "url": "https://zenodo.org/records/1011", "ai": "unverified"}]
    assert problems["2.1"]["solutions"] == [
        {"marker": "#", "part": "a", "url": "https://zenodo.org/records/1213", "ai": "unverified"},
        {"marker": "*", "part": "b", "url": "https://arxiv.org/abs/2406.00002"}]
    assert "solutions" not in problems["1.3"]


def test_kourovka_answer_links_are_trimmed_found_outside_parentheses_and_long_labels_are_parts():
    pages = kourovka_pages()
    pages[3] = pages[3].replace("2.4.Is every gadget finite? Q. Author\n", (
        "∗2.4.Is every gadget finite? Q. Author\n"
        "∗Yes, for n ⩾ 5 (R. Person,Preprint, 2019,https://\narxiv.org/abs/1910.00712forn= 5).\n"
        "2.5.Let G be a gizmo.\n∗(ae) What is the value for even n?\n(ao) What is the value for odd n? S. Author\n"
        "∗(ae) It is 1/6 (T. Person,Bull. Gizmo Soc.(2025),https://doi.org/10.1000/g).\n"
        "∗2.6.Is every gizmo finite? U. Author\n"
        "∗Yes, it is true (AI solutions confirmed by V. Expert: W. Model,Preprint, 2026,https://zenodo.org/records/77);\n"
        "X. Model,Preprint, 2026,https://arxiv.org/abs/2608.12432); see also (Y. Person,Preprint, 2026,\n"
        "https://arxiv.org/abs/2607.17477); see also a post athttps://mathoverflow.net/questions/1/2#3).\n"))
    problems = ps.parse_kourovka_pages(pages)["problems"]
    # PDF extraction glued "for n" to the identifier; the link keeps the identifier only.
    assert problems["2.4"]["solutions"] == [{"marker": "*", "url": "https://arxiv.org/abs/1910.00712"}]
    assert problems["2.5"]["part_markers"] == [{"part": "ae", "marker": "*"}]
    assert problems["2.5"]["solutions"] == [{"marker": "*", "part": "ae", "url": "https://doi.org/10.1000/g"}]
    # A ")" that closes the citation early does not hide the next AI solution; a new
    # citation group starts without the qualifier; text after the last group still counts.
    assert problems["2.6"]["solutions"] == [
        {"marker": "*", "url": "https://zenodo.org/records/77", "ai": "confirmed"},
        {"marker": "*", "url": "https://arxiv.org/abs/2608.12432", "ai": "confirmed"},
        {"marker": "*", "url": "https://arxiv.org/abs/2607.17477"},
        {"marker": "*", "url": "https://mathoverflow.net/questions/1/2#3"}]
    assert ps._cited_url("https://zenodo.org/records/123forthe") == "https://zenodo.org/records/123"
    assert ps._cited_url("https://zenodo.org/records/123/files/a.pdf") == "https://zenodo.org/records/123/files/a.pdf"
    assert ps._cited_url("https://arxiv.org/pdf/2304.08459.pdf") == "https://arxiv.org/pdf/2304.08459.pdf"
    assert ps._cited_url("https://arxiv.org/abs/1401.0300v48.") == "https://arxiv.org/abs/1401.0300v48"
    assert ps._cited_url("https://arxiv.org/abs/math/0211159") == "https://arxiv.org/abs/math/0211159"
    assert ps._cited_url("https://arxiv.org/abs/notanid") is None


def test_kourovka_issue_numbers_and_headers_split_by_extraction():
    pages = kourovka_pages()
    # A bare "New Problems" heading takes its issue from the contents...
    pages[0] += "\nNew Problems (21st Issue, 2026) . . . . 5"
    pages[3] += "New Problems\n♯21.7.Is every widget tame? Z. Author\n"
    assert ps.parse_kourovka_pages(pages)["problems"]["21.7"]["header_marker"] == "#"
    # ...else it is the issue after the last one, and an unknown issue fails closed.
    pages = kourovka_pages()
    pages[3] += "New Problems\n3.2.Is every widget tame? Z. Author\n"
    assert "3.2" in ps.parse_kourovka_pages(pages)["problems"]
    with pytest.raises(ps.SourceError, match="New Problems"):
        ps.parse_kourovka_pages(["Contents", "1\nNew Problems\n21.1.Is it? A. Author\n", "2\nArchive of solved problems\n"])
    # Problems filed under the wrong issue become stray headings; many of them fail closed.
    stray = "".join(f"9.{n}.Is it? A. Author\n" for n in range(1, 30))
    with pytest.raises(ps.SourceError, match="outside their issue section"):
        ps.parse_kourovka_pages(["Contents", "1\nProblems from the 1st Issue (1965)\n1.1.Q? A. Author\n" + stray,
                                 "2\nArchive of solved problems\n"])
    # A header marker that extraction put on its own line still belongs to the next heading.
    pages = kourovka_pages()
    pages[3] = pages[3].replace("2.4.Is every gadget finite?", "∗\n2.4.Is every gadget finite?")
    assert ps.parse_kourovka_pages(pages)["problems"]["2.4"]["header_marker"] == "*"


def test_kourovka_drift_check_refuses_extraction_changes_not_editorial_ones():
    base = ps.kourovka_fact_file(kourovka_pages(), pdf_sha256="f" * 64, arxiv_version=48,
                                 fetched_at="2026-10-11T00:00:00Z", edition="No. 21 (2026)")
    big = {"problems": {f"1.{n}": {"header_marker": "*", "part_markers": []} for n in range(1, 101)},
           "anomalies": [], "stray_headings": []}
    # An edition that drops a refuted ♯ (one marker) or adds markers is accepted.
    edited = {"problems": {key: dict(value) for key, value in big["problems"].items()}}
    edited["problems"]["1.1"]["header_marker"] = None
    edited["problems"]["1.200"] = {"header_marker": "#", "part_markers": []}
    ps.check_kourovka_drift(edited, big)
    # A glyph that no longer extracts reopens many marked problems at once: refused.
    glyph = {"problems": {key: {"header_marker": None, "part_markers": []} for key in big["problems"]}}
    with pytest.raises(ps.SourceError, match="lost a ∗/♯ marker"):
        ps.check_kourovka_drift(glyph, big)
    # Notation read as markers would mark far more problems answered than an edition does.
    unmarked = {"problems": {f"1.{n}": {"header_marker": None, "part_markers": []} for n in range(1, 101)}}
    starred = {"problems": {key: {"header_marker": "*", "part_markers": []} for key in unmarked["problems"]}}
    with pytest.raises(ps.SourceError, match="gained a ∗/♯ marker"):
        ps.check_kourovka_drift(starred, unmarked)
    # Old problems must survive, not only the total.
    swapped = {"problems": {f"2.{n}": {"header_marker": "*", "part_markers": []} for n in range(1, 101)}}
    with pytest.raises(ps.SourceError, match="missing"):
        ps.check_kourovka_drift(swapped, big)
    grown = dict(big, anomalies=[{"number": f"1.{n}", "issue": "x"} for n in range(1, 8)])
    with pytest.raises(ps.SourceError, match="anomalies"):
        ps.check_kourovka_drift(grown, big)
    # The refresh script's fact file applies the same check against the committed file.
    stripped = [page.replace("∗", "").replace("♯", "") for page in kourovka_pages()]
    marked = {"problems": {number: {"header_marker": "*", "part_markers": []} for number in base["problems"]}}
    with pytest.raises(ps.SourceError, match="lost a ∗/♯ marker"):
        ps.kourovka_fact_file(stripped, pdf_sha256="f" * 64, arxiv_version=49, fetched_at="x", edition="No. 21",
                              previous=marked)


def test_kourovka_fact_file_counts_markers_and_fails_closed():
    fact = ps.kourovka_fact_file(kourovka_pages(), pdf_sha256="f" * 64, arxiv_version=48,
                                 fetched_at="2026-10-11T00:00:00Z", edition="No. 21 (2026)")
    assert fact["counts"]["header_solved"] == 1 and fact["counts"]["header_ai_proposed"] == 2
    assert fact["counts"]["part_solved_only"] == 1 and fact["counts"]["part_ai_proposed_only"] == 1
    assert fact["pdf_url"] == "https://arxiv.org/pdf/1401.0300v48"
    with pytest.raises(ps.SourceError, match="refusing"):
        ps.kourovka_fact_file(kourovka_pages(), pdf_sha256="f" * 64, arxiv_version=49, fetched_at="x",
                              edition="No. 21", previous={"problems": {str(n): {} for n in range(20)}})
    with pytest.raises(ps.SourceError, match="archive"):
        ps.parse_kourovka_pages(kourovka_pages()[:4])
    # No problem text is stored: only pages, markers, AI qualifiers, links and derived topic labels.
    allowed = {"page", "header_marker", "part_markers", "ai", "in_main_section", "topics", "confirmer", "solutions"}
    assert all(set(row) <= allowed for row in fact["problems"].values())
    assert "widget" not in json.dumps(fact["problems"]).replace("widgets", "")


FC_ERDOS = '''/-!
# Erdős Problem 7

*References:*
- [erdosproblems.com/7](https://www.erdosproblems.com/7)
-/
namespace Erdos7

@[category research solved, AMS 11] -- a trailing comment
theorem erdos_7.parts.i : True := by
  trivial

@[category research open, AMS 11,
  formal_proof using lean4 at
    "https://github.com/example/proof"]
theorem erdos_7.parts.ii : True := by
  sorry

@[category research solved, AMS 11]
theorem erdos_7.variants.easy : True := by
  trivial

@[category textbook, AMS 11]
theorem helper : True := by
  trivial
'''
FC_WIKI = '''/-!
# Collatz

*Reference:* [Wikipedia](https://en.wikipedia.org/wiki/Collatz_conjecture)
-/
namespace Collatz

@[category research open, AMS 11]
theorem collatz : True := by
  sorry
'''


def test_formal_conjectures_aggregates_core_statements_and_maps_wikipedia_articles():
    fact = ps.formal_conjectures_fact_file(
        {"FormalConjectures/ErdosProblems/7.lean": FC_ERDOS,
         "FormalConjectures/Wikipedia/Collatz.lean": FC_WIKI,
         "FormalConjectures/Kourovka/1_35c.lean": FC_WIKI.replace("Collatz", "K"),
         "FormalConjectures/HilbertProblems/17.lean": FC_ERDOS.replace("erdos_7", "hilbert_17")},
        commit="d" * 40, committed_at="2026-10-10T00:00:00Z", fetched_at="2026-10-11T00:00:00Z")
    erdos = fact["erdos"]["7"]
    assert erdos["category"] == "partially solved" and erdos["statements"] == 2 and erdos["solved"] == 1
    assert erdos["formal_proofs"] == 1  # the variant does not count as a core statement
    assert erdos["url"] == f"https://github.com/google-deepmind/formal-conjectures/blob/{'d' * 40}/FormalConjectures/ErdosProblems/7.lean"
    assert fact["wikipedia"]["collatz conjecture"].endswith("/FormalConjectures/Wikipedia/Collatz.lean")
    assert set(fact["kourovka"]) == {"1.35"} and set(fact["hilbert"]) == {"17"}
    with pytest.raises(ps.SourceError):
        ps.parse_formal_conjectures({}, commit="main")


CONTENTS_NOW = """# Mathematics manuscript collection

**001. First family.** Proves a first thing. ([Lean](lean/docs/001.md))

&emsp;[First paper](preprints/First-paper-September-23-2026/paper.pdf)

**002. Second family.** Proves things about $`\\mathrm{CAT}(0)`$ spaces.

&emsp;[Sharp fillings in CAT(0) spaces](preprints/Sharp-fillings-in-CAT(0)-spaces-September-23-2026/paper.pdf)

&emsp;[A companion](preprints/A-companion-October-7-2026/main.pdf) — secondary writeup
""" + "".join(f"\n**{n:03d}. Family {n}.** Text.\n\n&emsp;[Paper {n}](preprints/Paper-{n}-September-1-2026/paper.pdf)\n" for n in range(3, 12))
CONTENTS_BEFORE = CONTENTS_NOW.replace(
    "&emsp;[A companion](preprints/A-companion-October-7-2026/main.pdf) — secondary writeup",
    "&emsp;[A companion](preprints/A-companion-October-1-2026/main.pdf)\n\n"
    "&emsp;[Algebraicity of things](preprints/Algebraicity-of-things-September-18-2026/paper.pdf)")
HISTORY = """# History

## October 7, 2026

**Withdrawals**

A sign error invalidates an argument. We have withdrawn the following manuscript:

- Algebraicity of things

**Fixes**

This brings the total to 3 / 12 = 25%.
"""


def test_openai_contents_and_history_resolve_withdrawn_and_superseded_directories():
    fact = ps.openai_math_fact_file(CONTENTS_NOW, HISTORY, commit="e" * 40, fetched_at="2026-10-11T00:00:00Z",
                                    previous_contents_md=CONTENTS_BEFORE, previous_commit="b" * 40)
    assert fact["counts"]["families"] == 11 and fact["counts"]["manuscripts"] == 12
    assert fact["families"]["001"]["lean_doc"] is True and fact["families"]["002"]["lean_doc"] is False
    assert [row["dir"] for row in fact["families"]["002"]["manuscripts"]] == [
        "Sharp-fillings-in-CAT(0)-spaces-September-23-2026", "A-companion-October-7-2026"]
    assert fact["families"]["002"]["manuscripts"][1]["date"] == "2026-10-07"
    assert fact["withdrawn"] == [{"family": "002", "dir": "Algebraicity-of-things-September-18-2026",
                                  "title": "Algebraicity of things", "date": "2026-09-18", "withdrawn_on": "2026-10-07",
                                  "archived_url": "https://github.com/openai/math/tree/" + "b" * 40
                                  + "/preprints/Algebraicity-of-things-September-18-2026"}]
    assert [row["dir"] for row in fact["superseded"]] == ["A-companion-October-1-2026"]
    assert fact["history"][0]["ratios"] == ["3/12"]
    # Later revisions keep the withdrawal (it is no longer in the newer "previous" contents).
    later = ps.openai_math_fact_file(CONTENTS_NOW, HISTORY, commit="f" * 40, fetched_at="x",
                                     previous_contents_md=CONTENTS_NOW, previous_commit="e" * 40, previous=fact)
    assert [row["dir"] for row in later["withdrawn"]] == ["Algebraicity-of-things-September-18-2026"]
    assert later["unmatched_withdrawals"] == [] and fact["unmatched_withdrawals"] == []
    # A withdrawal that matches no removed manuscript (added after the previous revision, or
    # worded differently) is recorded for review; the matched withdrawals still apply.
    added_later = HISTORY.replace("- Algebraicity of things\n", "- Algebraicity of things\n- A gadget paper\n")
    fact = ps.openai_math_fact_file(CONTENTS_NOW, added_later, commit="e" * 40, fetched_at="x",
                                    previous_contents_md=CONTENTS_BEFORE, previous_commit="b" * 40)
    assert [row["dir"] for row in fact["withdrawn"]] == ["Algebraicity-of-things-September-18-2026"]
    assert fact["unmatched_withdrawals"] == [{"title": "A gadget paper", "withdrawn_on": "2026-10-07"}]
    assert fact["counts"]["unmatched_withdrawals"] == 1
    assert ps.openai_manuscript_dir("https://github.com/openai/math/blob/" + "e" * 40
                                    + "/preprints/First-paper-September-23-2026/paper.pdf") == "First-paper-September-23-2026"
    assert ps.openai_manuscript_dir("https://raw.githubusercontent.com/openai/math/" + "e" * 40
                                    + "/preprints/First-paper-September-23-2026/paper.pdf") == "First-paper-September-23-2026"


def test_wikipedia_fact_file_keeps_names_not_sentences_and_unique_slugs():
    link = {"title": "Birch–Tate conjecture", "url": "https://en.wikipedia.org/wiki/Birch–Tate_conjecture",
            "resolved_title": "Birch–Tate conjecture", "resolved_url": "https://en.wikipedia.org/wiki/Birch–Tate_conjecture",
            "article_revid": 11}
    entries = [{"id": f"wpu-{n:03d}", "name": f"Conjecture {n}", "field": "Algebra", "primary_link": None} for n in range(110)]
    entries[0] = {"id": "wpu-000", "name": "Birch–Tate conjecture", "field": "Algebra", "subfield": None, "primary_link": link}
    entries[1] = {"id": "wpu-001", "name": "Is every widget a gadget, for all n?", "field": "Algebra", "primary_link": link}
    entries[2] = {"id": "wpu-002", "name": "Conjecture 3", "field": "Algebra", "primary_link": None}
    unsolved = {"source": {"page": "List", "revision_id": 1379514120, "permalink": "https://en.wikipedia.org/w/index.php?oldid=1379514120"},
                "entries": entries}
    solved = {"entries": [{"id": "wps-000", "name": "Widget theorem for n>2", "field": "Algebra", "year": 2026,
                           "outcome": "disproved", "ai_involved": True, "primary_link": link}]}
    fact = ps.wikipedia_fact_file(unsolved, solved, fetched_at="2026-10-11T00:00:00Z")
    rows = {row["wp_id"]: row for row in fact["entries"]}
    assert rows["wpu-000"]["name"] == "Birch–Tate conjecture" and rows["wpu-000"]["slug"] == "wiki-birch-tate-conjecture"
    assert rows["wpu-000"]["article_url"] == "https://en.wikipedia.org/wiki/Birch%E2%80%93Tate_conjecture"
    assert "name" not in rows["wpu-001"]  # a question is page text, not a name
    assert rows["wpu-001"]["slug"] == "wiki-birch-tate-conjecture-2"
    assert rows["wpu-002"]["slug"] == "wiki-conjecture-3" and rows["wpu-003"]["slug"] == "wiki-conjecture-3-2"
    assert len({row["slug"] for row in fact["entries"]}) == len(fact["entries"])
    assert fact["solved"][0]["slug"] == "solved-widget-theorem-for-n-2" and fact["solved"][0]["ai_involved"] is True
    assert "description" not in json.dumps(fact) and "text" not in fact["entries"][0]


CLAY_HTML = """<h2>The Millennium Prize Problems</h2>
<h3>Active problems</h3><a href="https://www.claymath.org/millennium/navier-stokes-equation/">NS</a>
<h3>Unsolved problems</h3>""" + "".join(
    f'<a href="https://www.claymath.org/millennium/{slug}/">x</a>' for slug in
    ("birch-and-swinnerton-dyer-conjecture", "hodge-conjecture", "p-vs-np", "riemann-hypothesis", "yang-mills-the-maths-gap")) + """
<h3>Solved problems</h3><a href="https://www.claymath.org/millennium/poincare-conjecture/">P</a>"""


def test_clay_listing_is_parsed_by_section_and_layout_changes_fail_closed():
    sections = ps.parse_clay_listing(CLAY_HTML)
    assert sections["solved"] == ["poincare-conjecture"] and sections["active"] == ["navier-stokes-equation"]
    assert len(sections["unsolved"]) == 5
    with pytest.raises(ps.SourceError):
        ps.parse_clay_listing(CLAY_HTML.replace("Solved problems", "Other problems"))
    with pytest.raises(ps.SourceError):
        ps.parse_clay_listing(CLAY_HTML.replace("poincare-conjecture", "hodge-conjecture"))


def test_palomar_arxiv_and_commit_feeds_parse_strictly():
    palomar = json.dumps({"schema_version": 1, "entries": [
        {"id": "PALOMAR-2026-10-09-000006", "version": 2, "title": "Erdős Problem 809: a proof", "abstract": "We prove it.",
         "published_at": "2026-10-09T06:28:55Z", "status": "registered", "formalization": {"theorem_names": ["Erdos809.main"]}},
        {"id": "not-an-id", "title": "ignored"}]}).encode()
    rows = ps.parse_palomar_results(palomar)
    assert rows == [{"id": "PALOMAR-2026-10-09-000006", "version": 2, "title": "Erdős Problem 809: a proof",
                     "abstract": "We prove it.", "published_at": "2026-10-09T06:28:55Z", "status": "registered",
                     "theorem_names": ["Erdos809.main"],
                     "url": "https://palomar-registry.org/entry?id=PALOMAR-2026-10-09-000006&version=2"}]
    with pytest.raises(ps.SourceError):
        ps.parse_palomar_results(b'{"schema_version": 2, "entries": []}')
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1401.0300v48</id>
      <title>Unsolved Problems in Group Theory</title><summary>A collection.</summary><published>2014-01-01T00:00:00Z</published>
      <updated>2026-10-06T00:00:00Z</updated></entry></feed>"""
    assert ps.parse_arxiv_feed(atom)[0] | {} == {"id": "1401.0300", "version": "v48", "title": "Unsolved Problems in Group Theory",
                                                 "summary": "A collection.", "published": "2014-01-01T00:00:00Z",
                                                 "updated": "2026-10-06T00:00:00Z", "url": "https://arxiv.org/abs/1401.0300v48"}
    with pytest.raises(ps.SourceError, match="DTD"):
        ps.parse_arxiv_feed(b'<!DOCTYPE x [<!ENTITY a "b">]><feed/>')
    commits = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:github.com,2008:Grit::Commit/""" + b"1" * 40 + \
        b"""</id><title>feat(ErdosProblems/3): link a proof</title><updated>2026-10-10T00:00:00Z</updated></entry></feed>"""
    assert ps.parse_commits_atom(commits) == [{"sha": "1" * 40, "title": "feat(ErdosProblems/3): link a proof",
                                               "updated": "2026-10-10T00:00:00Z"}]


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status, self.headers, self.body = status, headers or {}, body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class FakeConnection:
    responses = []

    def __init__(self, host, port, timeout, context):
        self.host, self.sock = host, None

    def request(self, method, path, headers):
        self.path, self.headers = path, headers

    def getresponse(self):
        return FakeConnection.responses.pop(0)

    def close(self):
        pass


def public_resolver(host, port, type):
    return [(None, None, None, None, ("140.82.112.3", port))]


def test_fetcher_rejects_private_addresses_foreign_redirects_and_oversize_bodies():
    fetcher = ps.HTTPSFetcher(resolver=lambda *a, **k: [(None, None, None, None, ("10.0.0.5", 443))],
                              connection_factory=FakeConnection)
    with pytest.raises(ps.SourceError, match="non-public"):
        fetcher.get("https://raw.githubusercontent.com/x", allowed_hosts=("raw.githubusercontent.com",))
    fetcher = ps.HTTPSFetcher(resolver=public_resolver, connection_factory=FakeConnection)
    with pytest.raises(ps.SourceError, match="trusted"):
        fetcher.get("http://raw.githubusercontent.com/x", allowed_hosts=("raw.githubusercontent.com",))
    FakeConnection.responses = [FakeResponse(302, {"Location": "https://evil.example/x"})]
    with pytest.raises(ps.SourceError, match="trusted"):
        fetcher.get("https://raw.githubusercontent.com/x", allowed_hosts=("raw.githubusercontent.com",))
    FakeConnection.responses = [FakeResponse(200, {"Content-Length": "999"}, b"x" * 999)]
    with pytest.raises(ps.SourceError, match="size"):
        fetcher.get("https://raw.githubusercontent.com/x", allowed_hosts=("raw.githubusercontent.com",), max_bytes=100)
    FakeConnection.responses = [FakeResponse(200, {"ETag": '"v1"'}, b"hello")]
    result = fetcher.get("https://raw.githubusercontent.com/x", allowed_hosts=("raw.githubusercontent.com",))
    assert (result.status, result.body, result.etag) == (200, b"hello", '"v1"')
    assert ps.safe_url("https://user:pw@example.org/") is None and ps.safe_url("https://127.0.0.1/") is None
    assert ps.safe_url("javascript:alert(1)") is None and ps.safe_url("https://example.org/a b") is None


def test_refresh_script_writes_fact_files_and_keeps_the_last_good_file_on_a_bad_parse(tmp_path, capsys):
    from scripts import refresh_problem_sources as refresh

    (tmp_path / "problems.yaml").write_text(ERDOS_YAML)
    (tmp_path / "wiki.md").write_text(AI_WIKI)
    (tmp_path / "clay.html").write_text(CLAY_HTML)
    out = tmp_path / "facts"
    common = ["--output-dir", str(out), "--fetched-at", "2026-10-11T00:00:00Z"]
    assert refresh.main([*common, "--only", "erdos,ai-wiki,clay", "--erdos-yaml", str(tmp_path / "problems.yaml"),
                         "--erdos-commit", "a" * 40, "--ai-wiki", str(tmp_path / "wiki.md"),
                         "--clay-html", str(tmp_path / "clay.html")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True and result["sources"]["erdos"]["revision"] == "a" * 40
    erdos = json.loads((out / "erdos.json").read_text())
    assert erdos["counts"]["problems"] == 4 and oct((out / "erdos.json").stat().st_mode & 0o777) == "0o644"
    assert json.loads((out / "erdos_ai_wiki.json").read_text())["revision"] == refresh.AI_WIKI_FROZEN_COMMIT
    # A parse that loses most problems is refused and the committed file stays.
    (tmp_path / "problems.yaml").write_text(ERDOS_YAML.split('- number: "7"')[0])
    before = (out / "erdos.json").read_bytes()
    assert refresh.main([*common, "--only", "erdos", "--erdos-yaml", str(tmp_path / "problems.yaml"),
                         "--erdos-commit", "b" * 40]) == 1
    assert "refusing" in json.loads(capsys.readouterr().out)["sources"]["erdos"]["error"]
    assert (out / "erdos.json").read_bytes() == before
    # Without a local input and without --fetch nothing is downloaded.
    assert refresh.main([*common, "--only", "kourovka"]) == 1
    assert "--fetch" in json.loads(capsys.readouterr().out)["sources"]["kourovka"]["error"]


@pytest.mark.parametrize("name", ["import_open_problems.py", "curate_open_problems.py", "promote_open_problems.py"])
def test_retired_catalogue_scripts_refuse_to_run(name):
    import subprocess
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / name
    text = path.read_text(encoding="utf-8")
    assert "open(" not in text and "write" not in text and "build_problem_catalogue.py" in text
    completed = subprocess.run([sys.executable, str(path), "--apply"], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2 and "build_problem_catalogue.py" in completed.stderr


def test_tls_context_advertises_http_1_1(monkeypatch):
    offered = []

    class Context:
        def set_alpn_protocols(self, protocols):
            offered.append(list(protocols))

    monkeypatch.setattr(ps.ssl, "create_default_context", Context)
    ps.HTTPSFetcher(resolver=public_resolver, connection_factory=FakeConnection)
    assert offered == [["http/1.1"]]


class TitleQueryConnection(FakeConnection):
    """Answers MediaWiki revision queries for the titles a request names, recording each request path."""
    paths: list = []

    def getresponse(self):
        from urllib.parse import parse_qs, urlsplit

        TitleQueryConnection.paths.append(self.path)
        titles = parse_qs(urlsplit(self.path).query)["titles"][0].split("|")
        return FakeResponse(200, {}, json.dumps({"query": {"pages": [{"title": t, "lastrevid": 9} for t in titles]}}).encode())


def test_api_queries_may_be_longer_than_published_links_and_title_batches_fit_the_fetcher():
    # Real batches of 50 Wikipedia titles produce 1,200-1,600 character query URLs.
    titles = [f"Conjecture of Erdős–Szekeres type on convex polygons, variant {index}" for index in range(130)]
    fact = {"entries": [{"wp_id": f"wpu-{index:03d}", "article_title": title} for index, title in enumerate(titles)], "solved": []}
    TitleQueryConnection.paths = []
    fetcher = ps.HTTPSFetcher(resolver=public_resolver, connection_factory=TitleQueryConnection)
    current = ps.refresh_article_revids(fact, fetcher, sleeper=lambda seconds: None)
    assert current == {title: 9 for title in titles}
    assert max(len("https://en.wikipedia.org" + path) for path in TitleQueryConnection.paths) > ps.MAX_URL
    assert all(len("https://en.wikipedia.org" + path) <= ps.MAX_REQUEST_URL for path in TitleQueryConnection.paths)
    # Links that are published keep the 500-character limit.
    assert ps.safe_url("https://example.org/" + "x" * 600) is None
    long_query = "https://en.wikipedia.org/w/api.php?" + "x" * 1500
    assert ps.safe_url(long_query) is None and ps.safe_url(long_query, max_length=ps.MAX_REQUEST_URL) == long_query
