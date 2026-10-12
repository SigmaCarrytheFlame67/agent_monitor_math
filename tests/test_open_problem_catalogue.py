"""Per-source invariants of the shipped catalogue, tied to the pinned source fact files."""
import hashlib
import json
from pathlib import Path

import pytest

from agent_monitor import problem_sources as ps
from agent_monitor.problem_catalogue import encode_catalogue, expand_row, validate_catalogue

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / "agent_monitor/data/problem_sources"
COPIES = ("agent_monitor/data/open_problems.json", "docs/public/open-problems.json", "docs/public/research/open-problems.json")


@pytest.fixture(scope="module")
def catalogue():
    return json.loads((ROOT / COPIES[0]).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(catalogue):
    return {row["id"]: expand_row(catalogue, row) for row in catalogue["problems"]}


@pytest.fixture(scope="module")
def facts():
    return {name: json.loads((FACTS / f"{name}.json").read_text(encoding="utf-8"))
            for name in ("erdos", "erdos_ai_wiki", "kourovka", "formal_conjectures", "openai_math", "wikipedia", "clay")}


def test_three_copies_are_identical_and_the_bundled_overlay_matches(catalogue):
    content = (ROOT / COPIES[0]).read_bytes()
    assert all((ROOT / path).read_bytes() == content for path in COPIES[1:])
    validate_catalogue(catalogue)
    overlay = json.loads((ROOT / "docs/public/research/problem-status.json").read_text(encoding="utf-8"))
    assert overlay["schema_version"] == 1 and overlay["catalogue_sha256"] == catalogue["content_sha256"]
    assert overlay["entries"] == {} and overlay["signals"] == {} and overlay["category_counts"] == catalogue["category_counts"]


def test_a_fresh_build_reproduces_the_committed_files_byte_for_byte():
    from scripts.build_problem_catalogue import OUTPUTS, build

    document, _ = build()
    # Compare digests: a text diff of the 3 MB single-line file takes minutes.
    fresh = hashlib.sha256(encode_catalogue(document).encode("utf-8")).hexdigest()
    assert fresh == hashlib.sha256(OUTPUTS[0].read_bytes()).hexdigest(), "stale catalogue: run scripts/build_problem_catalogue.py"


def test_every_previously_published_id_survives(rows):
    lines = (ROOT / "tests/fixtures/problem_catalogue_v2_ids.txt").read_text(encoding="utf-8").splitlines()
    published = [line for line in lines if line and not line.startswith("#")]
    assert len(published) == 1756
    assert set(published) <= set(rows)


def test_collection_counts_follow_the_pinned_source_revisions(catalogue, rows, facts):
    curation = json.loads((ROOT / "agent_monitor/data/open_problem_curation.json").read_text(encoding="utf-8"))
    merged = sum(1 for key in curation.get("merged", {}) if key.startswith("kourovka-"))
    retained = sum(1 for key in curation.get("retained", {}) if key.startswith("kourovka-"))
    assert catalogue["counts"]["erdos"] == len(facts["erdos"]["problems"])
    assert catalogue["counts"]["kourovka"] == len(facts["kourovka"]["problems"]) - merged + retained
    assert catalogue["counts"]["clay"] == 7
    assert catalogue["total"] == len(rows) == sum(catalogue["counts"].values())
    sources = {item["id"]: item for item in catalogue["sources"]}
    assert sources["erdos"]["revision"] == facts["erdos"]["revision"]
    assert sources["kourovka"]["url"] == f"https://arxiv.org/abs/1401.0300v{facts['kourovka']['arxiv_version']}"
    assert sources["kourovka"]["sha256"] == facts["kourovka"]["pdf_sha256"]
    assert sources["wikipedia"]["revision"] == str(facts["wikipedia"]["revision_id"])
    assert {item["id"] for item in catalogue["sources"] if item.get("collection") is False} == {"formal-conjectures", "openai-math", "erdos-ai-wiki"}
    assert len({(row["source"], row["number"]) for row in rows.values()}) == len(rows)


def test_erdos_categories_follow_the_database_state(rows, facts):
    for number, item in facts["erdos"]["problems"].items():
        row = rows[f"erdos-{int(number):04d}"]
        assert row["source_status"] == item["state"]
        if item["state"] in ps.ERDOS_SOLVED_STATES:
            assert (row["category"], row["category_rule"]) == ("solved", "S1"), number
            assert row["status_evidence"][0]["url"].startswith(f"https://github.com/teorth/erdosproblems/blob/{facts['erdos']['revision']}/")
        elif row["category"] == "solved":
            # Only research or a decision can disagree with the database, and it is flagged.
            assert row["category_rule"] == "S2" and "sources-conflict" in row["badges"], number
        if item["state"] in ps.ERDOS_OPEN_STATES and item["state"] != "open":
            assert "upstream-substatus" in row["badges"]
        if item["state"] in ps.ERDOS_OPEN_STATES and item.get("formal_state") == "Lean":
            assert "lean-pending" in row["badges"]
    assert rows["erdos-0001"]["category"] == "solved" and rows["erdos-0004"]["category"] == "solved"
    assert "ambiguous-statement" in rows["erdos-1163"]["badges"] and rows["erdos-1163"]["category"] == "open"
    assert rows["erdos-1135"]["title"] == "Erdős #1135 · Collatz conjecture"
    assert rows["erdos-0003"]["title"].startswith("Erdős #3 · divergent reciprocal sums")


def test_kourovka_markers_decide_categories_and_links_point_into_the_pinned_pdf(catalogue, rows, facts):
    kourovka = facts["kourovka"]
    version = kourovka["arxiv_version"]
    rules = {"S1": 0, "A1": 0, "P1": 0}
    for number, item in kourovka["problems"].items():
        issue, problem = (int(part) for part in number.split("."))
        row = rows.get(f"kourovka-{issue:02d}-{problem:03d}")
        if row is None:
            continue  # merged into another collection's entry
        assert row["source_url"] == f"https://arxiv.org/pdf/1401.0300v{version}#page={item['page']}"
        expected = ("S1" if item["header_marker"] == "*" else "A1" if item["header_marker"] == "#" else
                    "P1" if any(part["marker"] == "*" for part in item["part_markers"]) else None)
        if expected:
            assert row["category_rule"] == expected, number
            rules[expected] += 1
        else:
            assert row["category_rule"] not in ("S1", "A1", "P1"), number
    assert rules == {"S1": kourovka["counts"]["header_solved"], "A1": kourovka["counts"]["header_ai_proposed"],
                     "P1": kourovka["counts"]["part_solved_only"]}
    # October 2026 edition (v48): 176 answered, 37 unverified AI solutions, 22 answered parts.
    assert (version, rules) == (48, {"S1": 176, "A1": 37, "P1": 22})
    # ♯ on one part only (8.54) is a badge on an open problem, not "AI proposed".
    assert rows["kourovka-08-054"]["category"] == "open" and "ai-partial" in rows["kourovka-08-054"]["badges"]
    # Notation asterisks are not markers (10.43: "δ ∗(x)"; 14.9: W∗(F_k)).
    assert rows["kourovka-10-043"]["category_rule"] == "A1" and rows["kourovka-14-009"]["category_rule"] != "S1"
    assert rows["kourovka-20-003"]["source_url"] == "https://arxiv.org/pdf/1401.0300v46#page=146"
    assert "not-in-current-edition" in rows["kourovka-20-003"]["badges"]
    assert "kourovka-21-093" not in rows
    assert rows["erdos-0274"]["additional_sources"][0] == {
        "label": "Same problem · Kourovka 21.93", "url": f"https://arxiv.org/pdf/1401.0300v{version}#page=191",
        "relation": "same-problem"}
    # Answers published in 2026 updates stay in the main section as solved entries.
    for problem_id in ("kourovka-06-030", "kourovka-10-034", "kourovka-11-009", "kourovka-12-015", "kourovka-14-085"):
        assert rows[problem_id]["category"] == "solved", problem_id


def test_named_problems_and_honesty_invariants(rows):
    poincare = rows["clay-poincare-conjecture"]
    assert (poincare["category"], poincare["category_rule"]) == ("solved", "S1")
    assert any("math/0211159" in item["url"] for item in poincare["status_evidence"])
    navier = rows["clay-navier-stokes-equation"]
    assert navier["category"] == "ai-proposed" and navier["category_rule"] == "A2"
    assert any(item["actor"] == "ai" and item["kind"] == "claim" for item in navier["status_evidence"])
    assert "disputed" in navier["badges"] and "prize" in navier["badges"]
    for row in rows.values():
        assert row["status"] == row["category"]
        assert row["source_url"].startswith("https://") and row["status_headline"]
        if row["category"] == "solved" and row["category_rule"] == "S2":
            assert any(item["acceptance"] in ("refereed", "accepted-by-maintainer", "broadly-accepted")
                       for item in row["status_evidence"])
        for item in row["status_evidence"]:
            if item["acceptance"] in ("withdrawn", "refuted"):
                assert "withdrawn-claim" in row["badges"] or item["acceptance"] == "refuted"


def test_restricted_sources_contribute_no_problem_text(rows):
    curated = {"erdos-0003", "kourovka-01-003", "kourovka-12-020"}
    for row in rows.values():
        if row["source"] == "kourovka" and row["id"] not in curated:
            assert row["title"] == f"Kourovka problem {row['number']}" and not row.get("statement", {}).get("kind") == "exact"
        if row["source"] in ("kourovka", "erdos", "wikipedia", "wikipedia-solved") and row.get("statement"):
            assert row["statement"]["kind"] == "editorial"
