"""Catalogue v3 builder: precedence, honesty guards and determinism on synthetic inputs."""
from copy import deepcopy
import json

import pytest

from agent_monitor.problem_catalogue import (
    Builder, CatalogueError, canonical_json, empty_overlay, encode_catalogue, evidence_item, expand_row,
    manuscript_link, validate_catalogue,
)

CLAY_UNSOLVED = ["birch-and-swinnerton-dyer-conjecture", "hodge-conjecture", "p-vs-np", "riemann-hypothesis",
                 "yang-mills-the-maths-gap"]
FC = "https://github.com/google-deepmind/formal-conjectures/blob/" + "d" * 40


def facts():
    return {
        "erdos": {"revision": "a" * 40, "sha256": "0" * 64, "fetched_at": "2026-10-11T01:00:00Z", "problems": {
            "1": {"state": "proved", "last_update": "2025-08-31", "formal_state": "unformalized", "tags": ["number theory"], "line": 1},
            "2": {"state": "open", "formal_state": "unformalized", "tags": ["number theory"], "line": 10, "names": ["Widget conjecture"]},
            "3": {"state": "falsifiable", "formal_state": "Lean", "formal_url": "https://github.com/example/proof", "tags": ["graphs"], "line": 20},
            "4": {"state": "open", "formal_state": "unformalized", "tags": ["graphs"], "line": 30, "prize": "$100", "ambiguous": True},
        }},
        "ai_wiki": {"url": "https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems/" + "c" * 40,
                    "frozen_on": "2026-06-30", "problems": {
                        "1": {"rows": [{"section": "1(a)", "outcome": "full", "systems": ["System A"]}], "best_primary": "full",
                              "full_solution": True, "systems": ["System A"], "last_primary_date": "2026-01-24", "sections": ["1(a)"]},
                        "4": {"rows": [{"section": "1(d)", "outcome": "partial", "systems": ["System B"]}], "best_primary": "partial",
                              "partial_result": True, "systems": ["System B"], "sections": ["1(d)"]}}},
        "kourovka": {"edition": "No. 21 (2026)", "arxiv_id": "1401.0300", "arxiv_version": 48, "pdf_sha256": "f" * 64,
                     "published": "2026-10-06", "fetched_at": "2026-10-11T01:00:00Z", "problems": {
                         "1.3": {"page": 5, "header_marker": None, "part_markers": [], "ai": None, "topics": ["group rings"]},
                         "1.6": {"page": 5, "header_marker": "*", "part_markers": [], "ai": "confirmed", "confirmer": "D. Expert",
                                 "topics": [], "solutions": [{"marker": "*", "url": "https://zenodo.org/records/1", "ai": "confirmed"}]},
                         "1.8": {"page": 6, "header_marker": "#", "part_markers": [], "ai": "unverified", "topics": [],
                                 "solutions": [{"marker": "#", "url": "https://zenodo.org/records/2", "ai": "unverified"}]},
                         "1.9": {"page": 6, "header_marker": None, "part_markers": [{"part": "b", "marker": "#"}], "ai": "unverified", "topics": []},
                         "1.10": {"page": 7, "header_marker": None, "part_markers": [{"part": "a", "marker": "*"}], "ai": None, "topics": []},
                         "21.93": {"page": 9, "header_marker": None, "part_markers": [], "ai": None, "topics": []},
                     }},
        "clay": {"checked_at": "2026-10-11T00:27:00Z", "sha256": "1" * 64,
                 "sections": {"active": ["navier-stokes-equation"], "unsolved": CLAY_UNSOLVED, "solved": ["poincare-conjecture"]}},
        "formal_conjectures": {"url": "https://github.com/google-deepmind/formal-conjectures", "revision": "d" * 40,
                               "committed_at": "2026-10-10T21:54:33Z", "fetched_at": "2026-10-11T00:19:00Z",
                               "erdos": {"4": {"url": FC + "/FormalConjectures/ErdosProblems/4.lean", "category": "research solved"}},
                               "wikipedia": {"gadget": FC + "/FormalConjectures/Wikipedia/Gadget.lean"},
                               "millennium": {}, "hilbert": {}, "kourovka": {}},
        "openai_math": {"url": "https://github.com/openai/math", "revision": "e" * 40, "fetched_at": "2026-10-11T01:55:00Z",
                        "withdrawn": [], "families": {}, "mapping": []},
        "wikipedia": {"page": "List of unsolved problems in mathematics", "revision_id": 1379514120,
                      "revision_timestamp": "2026-10-10T12:20:03Z", "fetched_at": "2026-10-11T00:23:14Z",
                      "permalink": "https://en.wikipedia.org/w/index.php?title=List_of_unsolved_problems_in_mathematics&oldid=1379514120",
                      "entries": [
                          {"wp_id": "wpu-000", "name": "Widget–gadget conjecture", "field": "Number theory", "article_title": "Widget conjecture",
                           "article_url": "https://en.wikipedia.org/wiki/Widget_conjecture", "article_revid": 1, "slug": "wiki-widget-gadget-conjecture"},
                          {"wp_id": "wpu-001", "name": "Sprocket problem", "field": "Geometry", "article_title": "Sprocket",
                           "article_url": "https://en.wikipedia.org/wiki/Sprocket", "article_revid": 2, "slug": "wiki-sprocket-problem"},
                          {"wp_id": "wpu-002", "field": "Geometry", "subfield": "Discrete", "article_title": "Gadget",
                           "article_url": "https://en.wikipedia.org/wiki/Gadget", "article_revid": 3, "slug": "wiki-gadget"},
                          {"wp_id": "wpu-003", "name": "Problems about gizmos", "group_header": True, "slug": "wiki-problems-about-gizmos"}],
                      "solved": [{"wp_id": "wps-000", "name": "Cog theorem", "field": "Algebra", "year": 2026, "outcome": "proved",
                                  "article_title": "Cog", "article_url": "https://en.wikipedia.org/wiki/Cog", "slug": "solved-cog-theorem"},
                                 {"wp_id": "wps-001", "name": "Gear lemma", "field": "Algebra", "slug": "solved-gear-lemma"}]},
    }


def curation():
    return {"schema_version": 1, "as_of": "2026-10-11",
            "sources": [{"id": "hilbert", "name": "Hilbert’s problems", "url": "https://doi.org/10.1090/S0002-9904-1902-00923-3",
                         "attribution": "David Hilbert", "license": "Original summaries", "revision": "1900 list"}],
            "problems": [], "overlays": {},
            "merged": {"kourovka-21-093": {"into": "erdos-0002", "relation": "same"}},
            "retained": {"kourovka-20-003": {"source": "kourovka", "number": "20.3", "title": "Kourovka problem 20.3",
                                             "source_url": "https://arxiv.org/pdf/1401.0300v46#page=146",
                                             "source_locator": "Kourovka 20.3, arXiv:1401.0300v46, page 146",
                                             "topics": ["group theory"], "source_status": "Not in the October edition"}}}


def evidence(**overrides):
    item = {"kind": "accepted-result", "actor": "human", "scope": "full", "outcome": "proof", "date": "2025-05",
            "title": "A refereed proof", "url": "https://doi.org/10.1000/proof", "acceptance": "refereed"}
    item.update(overrides)
    return item


def research(entries):
    return {"schema_version": 1, "generated_at": "2026-10-11T03:00:00Z", "entries": entries}


def build(research_file=None, decisions=None, curated=None, fact_files=None):
    document = Builder(fact_files or facts(), curated or curation(), research_file, decisions,
                       rating_method={"name": "Exploration fit"}, input_hashes={"x": "0" * 64}).build()
    validate_catalogue(document)
    return document, {row["id"]: expand_row(document, row) for row in document["problems"]}


def test_source_of_record_markers_decide_categories_and_badges():
    document, rows = build()
    assert rows["erdos-0001"]["category"] == "solved" and rows["erdos-0001"]["category_rule"] == "S1"
    assert "ai-assisted-solution" in rows["erdos-0001"]["badges"]
    assert rows["erdos-0002"]["title"] == "Erdős #2 · Widget conjecture" and rows["erdos-0002"]["category"] == "open"
    assert rows["erdos-0003"]["category"] == "open"
    assert {"upstream-substatus", "lean-pending"} <= set(rows["erdos-0003"]["badges"])
    assert {"ai-partial", "sources-conflict", "prize", "ambiguous-statement", "formal-statement"} <= set(rows["erdos-0004"]["badges"])
    assert rows["kourovka-01-006"]["category"] == "solved" and "ai-confirmed" in rows["kourovka-01-006"]["badges"]
    assert rows["kourovka-01-008"]["category"] == "ai-proposed" and rows["kourovka-01-008"]["category_rule"] == "A1"
    assert rows["kourovka-01-009"]["category"] == "open" and "ai-partial" in rows["kourovka-01-009"]["badges"]
    assert rows["kourovka-01-010"]["category"] == "partial" and rows["kourovka-01-010"]["category_rule"] == "P1"
    assert rows["clay-poincare-conjecture"]["category"] == "solved"
    assert rows["clay-navier-stokes-equation"]["category"] == "open"
    assert rows["kourovka-01-003"]["source_url"] == "https://arxiv.org/pdf/1401.0300v48#page=5"
    assert document["category_counts"] == {"solved": 3, "ai-proposed": 1, "partial": 1, "open": 12}
    # Every non-open category shows primary evidence.
    for row in rows.values():
        if row["category"] != "open":
            assert row["status_evidence"] and all(item["url"].startswith("https://") for item in row["status_evidence"])


def test_merged_duplicates_and_retained_ids_keep_their_links():
    _, rows = build()
    assert "kourovka-21-093" not in rows
    assert rows["erdos-0002"]["additional_sources"] == [
        {"label": "Same problem · Kourovka 21.93", "url": "https://arxiv.org/pdf/1401.0300v48#page=9", "relation": "same-problem"}]
    assert rows["kourovka-20-003"]["category"] == "open" and rows["kourovka-20-003"]["badges"] == ["not-in-current-edition"]


def test_research_cannot_make_a_row_solved_without_qualifying_evidence():
    weak = research({"erdos-0002": {"category": "solved", "category_rule": "S2", "status_headline": "Solved, says a model.",
                                    "evidence": [evidence(acceptance="preprint")], "badges": [], "verified": "unchecked"}})
    _, rows = build(weak)
    assert rows["erdos-0002"]["category"] == "open" and rows["erdos-0002"]["needs_review"] is True
    strong = research({"erdos-0002": {"category": "solved", "category_rule": "S2", "status_headline": "Proved in 2025.",
                                      "evidence": [evidence()], "badges": [], "verified": "confirmed"}})
    _, rows = build(strong)
    assert rows["erdos-0002"]["category"] == "solved" and rows["erdos-0002"]["category_rule"] == "S2"
    # The source of record still lists it as open: flag the disagreement for review.
    assert "sources-conflict" in rows["erdos-0002"]["badges"] and rows["erdos-0002"]["needs_review"] is True
    uncertain = research({"erdos-0002": {"category": "solved", "category_rule": "S2", "evidence": [evidence()],
                                         "verified": "uncertain"}})
    assert build(uncertain)[1]["erdos-0002"]["category"] == "open"


def test_research_never_overrides_source_markers_or_asserts_a_marker():
    override = research({"kourovka-01-008": {"category": "solved", "category_rule": "S2", "evidence": [evidence()]},
                         "kourovka-01-003": {"category": "ai-proposed", "category_rule": "A1",
                                             "evidence": [evidence(kind="claim", actor="ai", acceptance="preprint")]}})
    _, rows = build(override)
    assert rows["kourovka-01-008"]["category"] == "ai-proposed" and "sources-conflict" in rows["kourovka-01-008"]["badges"]
    # No ♯ in the Notebook: the researched A1 is read as A2 (a documented AI claim) and flagged.
    assert (rows["kourovka-01-003"]["category"], rows["kourovka-01-003"]["category_rule"]) == ("ai-proposed", "A2")
    assert rows["kourovka-01-003"]["needs_review"] is True
    # A research "source marker" on erdosproblems.com is not a parsed source of record: a P1
    # resting on it is not applied; one backed by a refereed answer to a part becomes P2, flagged.
    remark = evidence(kind="source-marker", actor="maintainer", scope="part", acceptance="accepted-by-maintainer",
                      title="erdosproblems.com #2 remarks", url="https://www.erdosproblems.com/2")
    marker_only = research({"erdos-0002": {"category": "partial", "category_rule": "P1", "evidence": [remark]}})
    _, rows = build(marker_only)
    assert rows["erdos-0002"]["category"] == "open" and rows["erdos-0002"]["needs_review"] is True
    assert [item["kind"] for item in rows["erdos-0002"]["status_evidence"]] == ["other"]
    refereed = evidence(kind="accepted-result", scope="part", title="Part (a) in a journal")
    _, rows = build(research({"erdos-0002": {"category": "partial", "category_rule": "P1", "evidence": [remark, refereed]}}))
    assert (rows["erdos-0002"]["category"], rows["erdos-0002"]["category_rule"]) == ("partial", "P2")
    assert rows["erdos-0002"]["needs_review"] is True
    # A researched S1 is an S2 that must show a qualifying full answer.
    _, rows = build(research({"erdos-0004": {"category": "solved", "category_rule": "S1", "evidence": [evidence()]}}))
    assert (rows["erdos-0004"]["category"], rows["erdos-0004"]["category_rule"]) == ("solved", "S2")


def test_solved_needs_a_full_answer_and_partial_an_explicitly_stated_part():
    part_only = evidence(kind="partial-result", scope="part", title="Part (a) only")
    _, rows = build(research({"erdos-0002": {"category": "solved", "category_rule": "S2", "evidence": [part_only],
                                             "verified": "confirmed"}}))
    assert rows["erdos-0002"]["category"] == "open" and rows["erdos-0002"]["needs_review"] is True
    # Also where no source of record could object (a Wikipedia row).
    _, rows = build(research({"wpu-001": {"category": "solved", "category_rule": "S2", "name": "Sprocket problem",
                                          "statement": "Is every sprocket a gear?",
                                          "statement_sources": [{"label": "Paper", "url": "https://doi.org/10.1000/s"}],
                                          "evidence": [part_only]}}))
    assert rows["wiki-sprocket-problem"]["category"] == "open" and rows["wiki-sprocket-problem"]["needs_review"] is True
    # A special case is not a stated part: classical special cases never make a problem partial.
    special = evidence(scope="special-case", title="Proved for primes only")
    _, rows = build(research({"erdos-0002": {"category": "partial", "category_rule": "P2", "evidence": [special]}}))
    assert rows["erdos-0002"]["category"] == "open" and rows["erdos-0002"]["needs_review"] is True


def test_manuscripts_openai_math_withdrew_never_count():
    directory = "Algebraicity-of-Weil-classes-on-split-abelian-eightfolds-September-18-2026"
    fact_files = facts()
    fact_files["openai_math"]["withdrawn"] = [{"family": "032", "dir": directory, "title": "Algebraicity of Weil classes",
                                               "withdrawn_on": "2026-10-07"}]
    for url in ("https://github.com/openai/math/tree/" + "a" * 40 + "/preprints/" + directory,
                "https://raw.githubusercontent.com/openai/math/" + "a" * 40 + "/preprints/" + directory + "/paper.pdf"):
        claim = evidence(kind="claim", actor="ai", acceptance="preprint", title="Weil classes", url=url)
        _, rows = build(research({"erdos-0002": {"category": "ai-proposed", "category_rule": "A2", "evidence": [claim]}}),
                        fact_files=fact_files)
        row = rows["erdos-0002"]
        assert row["category"] == "open" and "withdrawn-claim" in row["badges"]
        assert [item["acceptance"] for item in row["status_evidence"] if item["url"] == url] == ["withdrawn"]
        notice = next(item for item in row["status_evidence"] if item["kind"] == "withdrawal")
        assert notice["url"] == "https://github.com/openai/math/blob/" + "e" * 40 + "/history.md"
        assert notice["date"] == "2026-10-07" and "Algebraicity of Weil classes" in notice["title"]


def ai_claim(date, number, **more):
    return evidence(**{"kind": "claim", "actor": "ai", "acceptance": "preprint", "title": f"AI proof {number}",
                       "url": f"https://arxiv.org/abs/2610.{number:05d}", "date": date, **more})


def test_status_changed_dates_the_first_answer_or_claim_behind_a_researched_category():
    entries = {
        # The first of several AI claims made the problem AI proposed; a withdrawn claim never counts.
        "erdos-0002": {"category": "ai-proposed", "category_rule": "A2", "evidence": [
            ai_claim("2026-10-06", 1), ai_claim("2026-09-24", 2), ai_claim("2026-04-01", 3, acceptance="withdrawn")]},
        # Mixed precisions give the finest date that surely contains the first answer.
        "erdos-0003": {"category": "solved", "category_rule": "S2", "evidence": [
            evidence(date="2005-06-01", url="https://doi.org/10.1000/a"), evidence(date="2005", url="https://doi.org/10.1000/b")]},
        "erdos-0004": {"category": "partial", "category_rule": "P2", "evidence": [
            evidence(kind="partial-result", scope="part", date="2005", url="https://doi.org/10.1000/c"),
            evidence(scope="part", date="2004-11", url="https://doi.org/10.1000/d")]},
        # An undated qualifying answer may be the first one: no date at all.
        "kourovka-01-003": {"category": "solved", "category_rule": "S2", "evidence": [
            evidence(date=None, url="https://doi.org/10.1000/e"), evidence(date="2020", url="https://doi.org/10.1000/f")]},
        # Solved on an accepted formalization alone: a formalization is dated by when it was written.
        "wps-000": {"name": "Cog theorem", "category": "solved", "category_rule": "S2", "statement": "Every cog turns.",
                    "statement_sources": [{"label": "Paper", "url": "https://doi.org/10.1000/cog"}],
                    "evidence": [evidence(kind="formalization", actor="maintainer", acceptance="accepted-by-maintainer", date="2026-08")]},
        # A source marker decides these: its date is its edition's, and a dated answer from research does not date it.
        "kourovka-01-006": {"category": "solved", "category_rule": "S2", "evidence": [evidence(date="2026-03-02")]},
        "erdos-0001": {"category": "solved", "category_rule": "S2", "evidence": [evidence(date="2025-07")]},
    }
    document, rows = build(research(entries))
    assert (rows["solved-cog-theorem"]["category"], rows["kourovka-01-003"]["category"], rows["kourovka-01-006"]["category_rule"]) == \
        ("solved", "solved", "S1")
    assert {row["id"]: row["status_changed"] for row in document["problems"] if "status_changed" in row} == {
        "erdos-0002": "2026-09-24", "erdos-0003": "2005", "erdos-0004": "2004-11"}
    # A decision is not a source event: the reviewer's cited link dates nothing, the claim it rests on does.
    who = {"decided_by": "reviewer", "decided_at": "2026-10-12", "note": "Checked the claim"}
    decided = {"schema_version": 1, "decisions": {
        "kourovka-01-009": {"category": "ai-proposed", "category_rule": "A2", "evidence_urls": ["https://zenodo.org/records/77"], **who},
        "kourovka-01-003": {"category": "ai-proposed", "category_rule": "A2", "evidence": [ai_claim("2026-09-29", 4)], **who}}}
    document, rows = build(decisions=decided)
    assert rows["kourovka-01-009"]["category"] == "ai-proposed" and "status_changed" not in rows["kourovka-01-009"]
    assert rows["kourovka-01-003"]["status_changed"] == "2026-09-29"
    assert [row["id"] for row in document["problems"] if "status_changed" in row] == ["kourovka-01-003"]
    # Only a non-open entry may carry a change date, and only a valid one.
    import hashlib
    for problem_id, value in (("erdos-0002", "2026-10-01"), ("kourovka-01-003", "Sep 2026")):
        broken = deepcopy(document)
        next(row for row in broken["problems"] if row["id"] == problem_id)["status_changed"] = value
        broken["content_sha256"] = hashlib.sha256(canonical_json(broken["problems"]).encode()).hexdigest()
        with pytest.raises(CatalogueError, match="status_changed"):
            validate_catalogue(broken)


def test_answers_and_claims_say_whether_their_link_is_a_manuscript_about_the_problem():
    sha = "e" * 40
    links = {
        "https://arxiv.org/abs/2607.17477v2": True, "https://arxiv.org/pdf/math/0211159.pdf": True,
        f"https://github.com/openai/math/tree/{sha}/preprints/Widget-paper-September-18-2026": True,
        f"https://raw.githubusercontent.com/openai/math/{sha}/preprints/Widget-paper-September-18-2026/paper.pdf": True,
        "https://github.com/openai/NavierStokesAndEuler": True, "https://github.com/izaidi/rm-reciprocity/tree/f5a01e1": True,
        "https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/kourovka_10.46_solution.pdf": True,
        # Indexes and collections, whatever their links look like.
        "https://github.com/openai/math": False, f"https://github.com/openai/math/blob/{sha}/CONTENTS.md": False,
        "https://raw.githubusercontent.com/wiki/teorth/erdosproblems/AI-contributions-to-Erd%C5%91s-problems.md": False,
        "https://github.com/teorth/erdosproblems/wiki/AI-contributions-to-Erd%C5%91s-problems": False,
        f"https://raw.githubusercontent.com/google-deepmind/formal-conjectures/{sha}/FormalConjectures/ErdosProblems/12.lean": False,
        "https://arxiv.org/pdf/1401.0300v48#page=10": False, "https://arxiv.org/list/math.GR/new": False,
        # Landing and web pages are not the manuscript file itself.
        "https://zenodo.org/records/22721372": False, "https://doi.org/10.1007/s00605-025-02116-8": False,
        "https://www.erdosproblems.com/3": False, "https://github.com/openai": False,
    }
    for url, expected in links.items():
        assert manuscript_link(url) is expected, url
    # A document linked as the answer for more than ten entries is a collection, counted once per entry
    # whatever the page fragment; one linked for ten is still a manuscript.
    fact_files = facts()
    for number in range(5, 16):
        fact_files["erdos"]["problems"][str(number)] = {"state": "open", "formal_state": "unformalized", "tags": ["graphs"], "line": 40 + number}
    compilation, paper = "https://example.org/solutions.pdf", "https://example.org/ten-problems.pdf"

    def part(url, number):
        return evidence(kind="partial-result", scope="part", acceptance="unknown", title=f"Part of #{number}", url=url)
    entries = {f"erdos-{number:04d}": {"category": "open", "category_rule": "O",
                                       "evidence": [part(f"{compilation}#page={number}", number), *([part(paper, number)] if number < 15 else [])]}
               for number in range(5, 16)}
    document, rows = build(research(entries), fact_files=fact_files)
    verdicts = [(item["url"].split("#")[0], item.get("manuscript")) for row in rows.values() for item in row["status_evidence"]
                if item["kind"] in ("accepted-result", "claim", "partial-result")]
    assert {verdict for url, verdict in verdicts if url == compilation} == {False}
    assert {verdict for url, verdict in verdicts if url == paper} == {True}
    assert all(isinstance(verdict, bool) for _, verdict in verdicts)
    assert [item.get("manuscript") for item in rows["kourovka-01-006"]["status_evidence"]] == [None, False], "a Zenodo landing page"
    assert all("manuscript" not in item for row in rows.values() for item in row["status_evidence"]
               if item["kind"] not in ("accepted-result", "claim", "partial-result"))
    import hashlib
    for problem_id, index, value in (("kourovka-01-006", 1, "yes"), ("kourovka-01-006", 0, True)):
        broken = deepcopy(document)
        next(row for row in broken["problems"] if row["id"] == problem_id)["status_evidence"][index]["manuscript"] = value
        broken["content_sha256"] = hashlib.sha256(canonical_json(broken["problems"]).encode()).hexdigest()
        with pytest.raises(CatalogueError, match="manuscript"):
            validate_catalogue(broken)


def test_upstream_lean_notes_always_say_not_rechecked():
    fact_files = facts()
    fact_files["erdos"]["problems"]["3"]["formal_note"] = (
        "Lean 4 formalisation accompanying E. Li, arXiv:2606.23659v2; exported theorem Erdos3.erdos_3. Independently "
        "rebuilt from a clean clone (8160 jobs, no errors) with #print axioms reporting only propext, Classical.choice, "
        "Quot.sound. The informal status is left open pending human review of the paper.")
    _, rows = build(fact_files=fact_files)
    note = next(item["note"] for item in rows["erdos-0003"]["status_evidence"] if item["kind"] == "formalization")
    assert note == ("Lean 4 formalisation accompanying E. Li, arXiv:2606.23659v2; exported theorem Erdos3.erdos_3. "
                    "Not re-checked by this server.")


def test_withdrawn_claims_never_count_and_mark_the_row():
    claim = evidence(kind="claim", actor="ai", outcome="proof", acceptance="withdrawn", title="A withdrawn AI proof",
                     url="https://github.com/openai/math/tree/" + "e" * 40 + "/preprints/Withdrawn-paper-September-18-2026")
    _, rows = build(research({"erdos-0002": {"category": "ai-proposed", "category_rule": "A2", "evidence": [claim]}}))
    assert rows["erdos-0002"]["category"] == "open" and "withdrawn-claim" in rows["erdos-0002"]["badges"]
    live = evidence(kind="claim", actor="ai", acceptance="preprint", title="An AI proof", url="https://zenodo.org/records/9")
    _, rows = build(research({"erdos-0002": {"category": "ai-proposed", "category_rule": "A2", "evidence": [claim, live]}}))
    assert rows["erdos-0002"]["category"] == "ai-proposed" and "withdrawn-claim" in rows["erdos-0002"]["badges"]


def test_decisions_override_research_and_markers_and_must_cite_evidence():
    decided = {"schema_version": 1, "decisions": {"kourovka-01-008": {
        "category": "solved", "category_rule": "S2", "evidence": [evidence()], "decided_by": "reviewer",
        "decided_at": "2026-10-12", "note": "Refereed confirmation", "status_headline": "Confirmed and published."}}}
    _, rows = build(decisions=decided)
    row = rows["kourovka-01-008"]
    assert (row["category"], row["category_rule"], row["status_headline"], row["status_as_of"]) == \
        ("solved", "S2", "Confirmed and published.", "2026-10-12")
    missing = deepcopy(decided)
    del missing["decisions"]["kourovka-01-008"]["evidence"]
    with pytest.raises(CatalogueError, match="cite evidence"):
        build(decisions=missing)
    anonymous = deepcopy(decided)
    del anonymous["decisions"]["kourovka-01-008"]["decided_by"]
    with pytest.raises(CatalogueError, match="who, when and why"):
        build(decisions=anonymous)
    # Bare URLs become reviewer-cited evidence; they never justify "solved".
    cited = {"schema_version": 1, "decisions": {"kourovka-01-003": {
        "category": "ai-proposed", "category_rule": "A2", "evidence_urls": ["https://zenodo.org/records/77"],
        "decided_by": "reviewer", "decided_at": "2026-10-12", "note": "AI claim checked against the statement"}}}
    row = build(decisions=cited)[1]["kourovka-01-003"]
    assert row["category"] == "ai-proposed"
    assert [(item["title"], item["acceptance"]) for item in row["status_evidence"]] == [("Evidence cited by the reviewer", "unknown")]
    # The decision's reasoning and date are not evidence: the bare link carries neither.
    assert not {"note", "date"} & set(row["status_evidence"][0])
    # A link the entry already cites is not repeated as reviewer-cited evidence.
    known = deepcopy(cited)
    known["decisions"]["kourovka-01-003"]["evidence_urls"] = ["https://zenodo.org/records/1"]
    known["decisions"]["kourovka-01-006"] = known["decisions"].pop("kourovka-01-003")
    row = build(decisions=known)[1]["kourovka-01-006"]
    assert [item["url"] for item in row["status_evidence"]].count("https://zenodo.org/records/1") == 1
    assert all(item["title"] != "Evidence cited by the reviewer" for item in row["status_evidence"])
    cited["decisions"]["kourovka-01-003"].update(category="solved", category_rule="S2")
    with pytest.raises(CatalogueError, match="qualifying evidence"):
        build(decisions=cited)


def wikipedia_research():
    return research({
        # Same problem as an existing row: an alias and merged evidence, no new row.
        "wpu-000": {"name": "Widget conjecture (Wikipedia)", "maps_to": {"catalogue_id": "erdos-0002", "relation": "same"},
                    "category": "open", "category_rule": "O", "statement": "Is every widget a gadget?",
                    "statement_sources": [{"label": "Paper", "url": "https://doi.org/10.1000/w"}],
                    "evidence": [evidence(kind="partial-result", scope="part", title="Widgets of rank two")]},
        # A new problem: a wiki-<slug> row with an original statement and research category.
        "wpu-002": {"name": "Gadget packing problem", "category": "ai-proposed", "category_rule": "A2",
                    "status_headline": "An AI system claims a construction; unreviewed.",
                    "statement": "Can gadgets tile the plane? A second sentence explains the setting.",
                    "statement_sources": [{"label": "Bad", "url": "http://example.org/x"},
                                          {"label": "Survey", "url": "https://doi.org/10.1000/g"}],
                    "evidence": [evidence(kind="claim", actor="ai", acceptance="preprint", title="Gadget tilings")],
                    "maps_to": {"catalogue_id": "erdos-0004", "relation": "related"}, "topics": ["tilings"]},
        "wpu-001": {"include": False, "name": "Sprocket problem"},
        "wps-001": {"name": "Gear lemma", "maps_to": {"catalogue_id": "erdos-0999", "relation": "same"},
                    "statement": "Every gear meshes.", "statement_sources": [{"label": "Paper", "url": "https://doi.org/10.1000/gear"}]},
        "wpu-003": {"name": "Gizmos", "statement": "x", "statement_sources": [{"label": "s", "url": "https://doi.org/1"}]},
        "wps-000": {"name": "Cog theorem", "category": "solved", "category_rule": "S2", "statement": "Every cog turns.",
                    "statement_sources": [{"label": "Paper", "url": "https://doi.org/10.1000/c"}], "evidence": [evidence()]},
        "hilbert-03": {"name": "Hilbert 3: Equal volumes", "category": "solved", "category_rule": "S2",
                       "statement": "Are equal-volume tetrahedra equidecomposable?",
                       "statement_sources": [{"label": "Hilbert 1902", "url": "https://doi.org/10.1090/S0002-9904-1902-00923-3"}],
                       "evidence": [evidence(outcome="disproof")]},
    })


def test_wikipedia_entries_merge_as_aliases_or_become_original_rows():
    document, rows = build(wikipedia_research())
    # The pinned list name becomes an alias (a name identical to the title would not).
    assert rows["erdos-0002"]["aliases"] == ["Widget–gadget conjecture"]
    assert any(item["title"] == "Widgets of rank two" for item in rows["erdos-0002"]["status_evidence"])
    assert rows["erdos-0002"]["category"] == "open"  # a mapped entry never sets the category of its target
    assert "wiki-widget-gadget-conjecture" not in rows and "wiki-sprocket-problem" not in rows
    assert "wiki-problems-about-gizmos" not in rows  # list group headers are not problems
    gadget = rows["wiki-gadget"]
    assert (gadget["source"], gadget["number"], gadget["category"], gadget["category_rule"]) == ("wikipedia", "wpu-002", "ai-proposed", "A2")
    assert gadget["title"] == "Gadget packing problem" and gadget["source_url"] == "https://en.wikipedia.org/wiki/Gadget"
    assert gadget["statement"]["sources"] == [{"label": "Survey", "url": "https://doi.org/10.1000/g"}]
    assert gadget["related"] == [{"id": "erdos-0004", "relation": "related"}]
    assert gadget["formal_statement_url"].endswith("/Wikipedia/Gadget.lean") and "formal-statement" in gadget["badges"]
    assert "revision 1379514120" in gadget["source_locator"]
    assert rows["solved-cog-theorem"]["source"] == "wikipedia-solved" and rows["solved-cog-theorem"]["category"] == "solved"
    assert rows["hilbert-03"]["category"] == "solved" and rows["hilbert-03"]["number"] == "3"
    # A "same" mapping to a missing entry is listed separately and flagged.
    assert rows["solved-gear-lemma"]["needs_review"] is True
    assert document["counts"]["wikipedia"] == 1 and document["counts"]["wikipedia-solved"] == 2


def test_build_is_deterministic_compact_and_self_consistent():
    first, _ = build(wikipedia_research())
    second, _ = build(wikipedia_research())
    assert encode_catalogue(first) == encode_catalogue(second)
    import hashlib
    assert first["content_sha256"] == hashlib.sha256(canonical_json(first["problems"]).encode()).hexdigest()
    erdos = next(row for row in first["problems"] if row["id"] == "erdos-0002")
    assert "source_name" not in erdos and "rating" not in erdos  # stored once in sources[].defaults
    assert expand_row(first, erdos)["source_name"] == "Erdős Problems"
    assert expand_row(first, next(row for row in first["problems"] if row["id"] == "kourovka-01-003"))["badges"] == []
    overlay = empty_overlay(first)
    assert overlay["entries"] == {} and overlay["catalogue_sha256"] == first["content_sha256"]
    assert sum(overlay["category_counts"].values()) == first["total"]
    assert [item["id"] for item in first["categories"]] == ["solved", "ai-proposed", "partial", "open"]
    assert first["categories"][1]["description"].startswith("An AI system produced a claimed full solution")
    broken = deepcopy(first)
    broken["problems"][0]["category"] = "partial"
    with pytest.raises(CatalogueError):
        validate_catalogue(broken)


def test_evidence_items_are_validated_and_bounded():
    item = evidence_item(evidence(note="n" * 500, ai_systems=["System A"] * 12))
    assert len(item["note"]) == 240 and len(item["ai_systems"]) == 8 and item["date_precision"] == "month"
    for bad in (evidence(url="http://doi.org/x"), evidence(url="https://user:pw@doi.org/x"), evidence(kind="rumour"),
                evidence(acceptance="certain"), evidence(date="last week"), evidence(title="")):
        with pytest.raises(CatalogueError):
            evidence_item(bad)


def test_curated_overlays_cannot_change_identity_and_new_rows_need_metadata():
    curated = curation()
    curated["overlays"]["erdos-0002"] = {"source_url": "https://example.org/x"}
    with pytest.raises(CatalogueError, match="canonical source identity"):
        build(curated=curated)
    curated = curation()
    curated["problems"] = [{"id": "hilbert-16-ii", "number": "16, part II", "title": "", "summary": "s", "source": "hilbert",
                            "source_url": "https://arxiv.org/abs/2411.09594", "source_locator": "Hilbert 16 II",
                            "source_status": "open", "status_as_of": "2026-09-16", "topics": ["dynamics"],
                            "statement": {"kind": "editorial", "text": "t", "sources": [{"label": "l", "url": "https://doi.org/1"}]}}]
    with pytest.raises(CatalogueError, match="nonempty title"):
        build(curated=curated)
    curated["problems"][0]["title"] = "Hilbert 16, part II"
    curated["problems"][0]["statement"]["sources"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(CatalogueError, match="HTTPS"):
        build(curated=curated)


def test_build_script_refuses_to_drop_a_published_id(tmp_path, capsys):
    from scripts import build_problem_catalogue as script

    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    for name, filename in script.FACT_FILES.items():
        (facts_dir / filename).write_text(json.dumps(facts()[name]))
    (tmp_path / "curation.json").write_text(json.dumps(curation()))
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"problems": [{"id": "erdos-0002"}, {"id": "kourovka-99-001"}]}))
    arguments = ["--facts", str(facts_dir), "--curation", str(tmp_path / "curation.json"),
                 "--research", str(tmp_path / "none.json"), "--decisions", str(tmp_path / "none2.json"), "--dry-run"]
    assert script.main([*arguments, "--previous", str(previous)]) == 1
    assert "kourovka-99-001" in capsys.readouterr().out
    previous.write_text(json.dumps({"problems": [{"id": "erdos-0002"}]}))
    assert script.main([*arguments, "--previous", str(previous)]) == 0


def test_a_decision_wins_over_research_copies_of_its_evidence():
    # Research cites a counterexample as a preprint; the reviewer records that
    # independent experts confirmed it.  The decision's copy must be the one kept.
    claim = evidence(kind="accepted-result", actor="ai-assisted", acceptance="preprint", outcome="counterexample",
                     title="A second counterexample", url="https://arxiv.org/abs/2609.15080v1")
    researched = research({"kourovka-01-003": {"category": "solved", "category_rule": "S2", "evidence": [claim]}})
    decided = {"schema_version": 1, "decisions": {"kourovka-01-003": {
        "category": "solved", "category_rule": "S2", "evidence": [{**claim, "acceptance": "broadly-accepted"}],
        "decided_by": "reviewer", "decided_at": "2026-10-12", "note": "Independent experts confirmed it"}}}
    row = build(researched, decided)[1]["kourovka-01-003"]
    assert row["category"] == "solved"
    assert [item["acceptance"] for item in row["status_evidence"] if item["url"] == claim["url"]] == ["broadly-accepted"]


def test_copies_of_one_paper_are_listed_once():
    unversioned = evidence(kind="other", acceptance="preprint", title="A response", url="https://arxiv.org/abs/2610.08144",
                           note="Questions the formalization.")
    versioned = {**unversioned, "url": "https://arxiv.org/abs/2610.08144v1", "claimant": "A. Author, B. Author", "note": "Longer note."}
    edition_48 = evidence(kind="other", acceptance="preprint", title="Edition 48", url="https://arxiv.org/abs/1401.0300v48")
    edition_46 = {**edition_48, "title": "Edition 46", "url": "https://arxiv.org/pdf/1401.0300v46"}
    anchored = evidence(kind="other", acceptance="preprint", title="Database line", url="https://example.org/data.yaml#L10")
    plain = {**anchored, "url": "https://example.org/data.yaml"}
    researched = research({"erdos-0002": {"category": "open", "category_rule": "O",
                                          "evidence": [unversioned, versioned, edition_48, edition_46, anchored, plain]}})
    items = build(researched)[1]["erdos-0002"]["status_evidence"]
    # An unversioned link and v1 are one paper: the first copy is kept and gains the missing claimant.
    response = [item for item in items if "2610.08144" in item["url"]]
    assert [(item["url"], item["note"], item.get("claimant")) for item in response] == \
        [("https://arxiv.org/abs/2610.08144", "Questions the formalization.", "A. Author, B. Author")]
    # Two explicitly different versions (Kourovka editions) stay separate; a line anchor does not make a new document.
    assert sorted(item["url"] for item in items if "1401.0300" in item["url"]) == \
        ["https://arxiv.org/abs/1401.0300v48", "https://arxiv.org/pdf/1401.0300v46"]
    assert [item["url"] for item in items if "example.org" in item["url"]] == ["https://example.org/data.yaml#L10"]


def test_summaries_never_cut_inside_a_formula():
    statement = ("With no full stop before the formulas, the cut falls inside the last one: "
                 "for each $n\\ge3$ and $\\epsilon>0$ there is a constant $C_{n,\\epsilon}$ such that whenever integers "
                 "$a_1+\\dots+a_n=0$ with $\\gcd(a_1,\\dots,a_n)=1$ and no vanishing proper subsum, one has "
                 "$\\max_i|a_i| \\le C_{n,\\epsilon}\\,\\operatorname{rad}(a_1\\cdots a_n)^{2n-5+\\epsilon}$ always.")
    researched = research({"hilbert-03": {"name": "Hilbert 3: Equal volumes", "category": "open", "category_rule": "O",
                                          "statement": statement,
                                          "statement_sources": [{"label": "Hilbert 1902", "url": "https://doi.org/10.1090/S0002-9904-1902-00923-3"}]}})
    summary = build(researched)[1]["hilbert-03"]["summary"]
    assert len(summary) <= 281 and summary.endswith("…")
    assert summary.replace("\\$", "").count("$") % 2 == 0, summary
    assert summary.endswith("one has…")  # the cut moved before the formula it would have split


def test_only_an_answer_supports_solved_or_partial():
    # A maintainer-accepted listing that says the problem is open, and an accepted
    # formal statement, have outcome "other": neither is an answer.
    listing = evidence(kind="source-marker", actor="maintainer", outcome="other", acceptance="accepted-by-maintainer",
                       title="Listed under unsolved problems", url="https://www.claymath.org/millennium-problems/")
    statement = evidence(kind="formalization", actor="maintainer", outcome="other", acceptance="accepted-by-maintainer",
                         title="A formal statement", url=FC + "/FormalConjectures/ErdosProblems/2.lean")
    for category, rule, scope in (("solved", "S2", "full"), ("partial", "P2", "part")):
        items = [{**listing, "scope": scope}, {**statement, "scope": scope}]
        row = build(research({"erdos-0002": {"category": category, "category_rule": rule, "evidence": items}}))[1]["erdos-0002"]
        assert row["category"] == "open", category
    # The same formal proof recorded as a proof does count.
    proof = {**statement, "outcome": "proof", "title": "Kernel-checked proof accepted by the maintainers"}
    row = build(research({"erdos-0002": {"category": "solved", "category_rule": "S2", "evidence": [proof]}}))[1]["erdos-0002"]
    assert row["category"] == "solved"
    # A source-of-record marker is the answer itself, even without saying how
    # (teorth's plain "solved" state is recorded with outcome "other").
    facts_solved = facts()
    facts_solved["erdos"]["problems"]["2"]["state"] = "solved"
    row = build(fact_files=facts_solved)[1]["erdos-0002"]
    assert (row["category"], row["category_rule"]) == ("solved", "S1")


def test_raw_and_blob_links_to_one_file_are_one_item():
    blob = evidence(kind="other", acceptance="preprint", title="Database record",
                    url="https://github.com/teorth/erdosproblems/blob/" + "a" * 40 + "/data/problems.yaml")
    raw = {**blob, "url": "https://raw.githubusercontent.com/teorth/erdosproblems/" + "a" * 40 + "/data/problems.yaml#L12",
           "claimant": "Erdős problems database"}
    items = build(research({"erdos-0002": {"category": "open", "category_rule": "O", "evidence": [blob, raw]}}))[1]["erdos-0002"]["status_evidence"]
    copies = [item for item in items if item["title"] == "Database record"]
    assert [(item["url"], item.get("claimant")) for item in copies] == [(blob["url"], "Erdős problems database")]


def test_audit_fixes_red_links_html_arxiv_partial_results_reviewer_links_and_headlines():
    # A Wikipedia entry whose article does not exist (a red link: no revision id) points at the list section.
    document, rows = build(wikipedia_research())
    cog = rows["solved-cog-theorem"]["source_url"]
    assert cog.startswith("https://en.wikipedia.org/w/index.php?title=List_of_unsolved_problems_in_mathematics&oldid=1379514120")
    assert cog.endswith("#Problems_solved_since_2015")
    assert rows["wiki-gadget"]["source_url"] == "https://en.wikipedia.org/wiki/Gadget"  # an existing article keeps its link
    # arXiv /html/ and /abs/ links name one paper; partial results already listed are not repeated.
    abs_item = evidence(kind="partial-result", scope="part", acceptance="preprint", title="Bound", url="https://arxiv.org/abs/2601.00001v2")
    html_item = {**abs_item, "url": "https://arxiv.org/html/2601.00001v2"}
    partial = {"title": "A bound", "text": "A bound.", "status": "preprint", "sources": [{"label": "arXiv", "url": "https://arxiv.org/abs/2601.00001"}]}
    researched = research({"erdos-0002": {"category": "open", "category_rule": "O", "evidence": [abs_item, html_item],
                                          "partial_results": [partial, {**partial, "sources": [{"label": "arXiv", "url": "https://arxiv.org/abs/2601.00001v2"}]}]},
                           "erdos-0004": {"category": "open", "category_rule": "O", "maps_to": {"catalogue_id": "wpu-777", "relation": "related"}}})
    builder = Builder(facts(), curation(), researched, None, rating_method={"name": "x"}, input_hashes={"x": "0" * 64})
    document = builder.build()
    row = {r["id"]: expand_row(document, r) for r in document["problems"]}["erdos-0002"]
    assert [item["url"] for item in row["status_evidence"] if "2601.00001" in item["url"]] == ["https://arxiv.org/abs/2601.00001v2"]
    assert len([item for item in row.get("partial_results") or [] if item["title"] == "A bound"]) == 1
    # A link to a research key instead of a catalogue id is reported, not silently dropped.
    assert any("maps_to target wpu-777 is not a catalogue id" in warning for warning in builder.warnings)
    # A reviewer-cited link the row already cites is not listed a second time.
    claim = evidence(kind="claim", actor="ai", acceptance="preprint", title="An AI claim", url="https://zenodo.org/records/9")
    decided = {"schema_version": 1, "decisions": {"erdos-0002": {
        "category": "ai-proposed", "category_rule": "A2", "evidence_urls": [claim["url"], claim["url"]],
        "decided_by": "reviewer", "decided_at": "2026-10-12", "note": "Checked against the statement"}}}
    row = build(research({"erdos-0002": {"category": "open", "category_rule": "O", "evidence": [claim]}}), decided)[1]["erdos-0002"]
    assert [item["title"] for item in row["status_evidence"] if item["url"] == claim["url"]] == ["An AI claim"]
    # Research that is not applied (an uncertain verdict) still supplies an open-reading headline.
    uncertain = research({"erdos-0002": {"category": "ai-proposed", "category_rule": "A2", "verified": "uncertain",
                                         "status_headline": "Open. An AI claim exists but is unreviewed.",
                                         "evidence": [{**claim, "scope": "full"}]}})
    assert build(uncertain)[1]["erdos-0002"]["status_headline"] == "Open. An AI claim exists but is unreviewed."


def test_ai_wiki_ratings_are_words_not_coloured_marks():
    # The site never shows a verdict as a colour; green is reserved for Lean-kernel results.
    _, rows = build()
    notes = [item.get("note", "") for row in rows.values() for item in row["status_evidence"] if "wiki" in item["title"].lower()]
    assert notes and all("rates problem" in note for note in notes if note.startswith("The wiki"))
    assert not any(mark in note for note in notes for mark in "🟢🟡🔴⚪")
    item = evidence_item({"kind": "claim", "actor": "ai", "scope": "full", "outcome": "proof", "acceptance": "unknown",
                          "title": "Wiki row 🟢 full solution", "url": "https://example.org/a", "note": "Listed as 🟡 partial result (⚪ unverified)."})
    assert (item["title"], item["note"]) == ("Wiki row full solution", "Listed as partial result (unverified).")


def test_a_decided_category_names_its_decision_and_research_dates_the_check():
    decided = {"schema_version": 1, "decisions": {"kourovka-01-008": {
        "category": "solved", "category_rule": "S2", "evidence": [evidence()], "decided_by": "A reviewer (AI); not yet signed off",
        "decided_at": "2026-10-12", "note": "Refereed confirmation", "status_headline": "Confirmed and published."}}}
    researched = research({"erdos-0002": {"category": "open", "category_rule": "O", "status_headline": "Open, checked in sources."}})
    builder = Builder(facts(), curation(), researched, decided, rating_method={"name": "x"}, input_hashes={"x": "0" * 64})
    document = builder.build()
    rows = {row["id"]: expand_row(document, row) for row in document["problems"]}
    assert rows["kourovka-01-008"]["decision"] == {"by": "A reviewer (AI); not yet signed off", "at": "2026-10-12",
                                                   "note": "Refereed confirmation"}
    assert "decision" not in rows["erdos-0002"]
    assert rows["erdos-0002"]["status_as_of"] == "2026-10-11"  # the research file's date, not an older curation date
    # A non-open category under a headline that starts with "Open" is reported.
    contradictory = research({"erdos-0004": {"category": "ai-proposed", "category_rule": "A2", "status_headline": "Open. An AI claims it.",
                                             "evidence": [evidence(kind="claim", actor="ai", acceptance="preprint", title="Claim")]}})
    builder = Builder(facts(), curation(), contradictory, None, rating_method={"name": "x"}, input_hashes={"x": "0" * 64})
    builder.build()
    assert any("headline starts with 'Open'" in warning for warning in builder.warnings)


def test_ascii_operators_in_our_prose_are_typeset_and_math_is_left_alone():
    item = evidence(kind="claim", actor="ai", acceptance="preprint", title="W(k) -> infinity (paper title kept)",
                    note="Claims W(k)^{1/k} -> infinity for k>=3; $a<=b$ is math, x != y.")
    researched = research({"erdos-0002": {"category": "open", "category_rule": "O", "evidence": [item],
                                          "status_headline": "Still listed as open; a claim gives f(n) <= n^{1/2} for n >= 2."}})
    row = build(researched)[1]["erdos-0002"]
    claim = next(entry for entry in row["status_evidence"] if entry["kind"] == "claim")
    assert claim["note"] == "Claims W(k)^{1/k} → infinity for k≥3; $a<=b$ is math, x ≠ y."
    assert claim["title"] == "W(k) -> infinity (paper title kept)"
    assert row["status_headline"] == "Still listed as open; a claim gives f(n) ≤ n^{1/2} for n ≥ 2."
