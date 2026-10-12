"""Deterministic problem candidates for news items (catalogue schema 2 today, 3 later)."""
import json
from pathlib import Path

import pytest

from agent_monitor import news_links

ROOT = Path(__file__).resolve().parents[1]

V2 = {"schema_version": 2, "problems": [
    {"id": "erdos-0003", "source": "erdos", "number": "3", "title": "Erdős #3 · divergent reciprocal sums and arithmetic progressions"},
    {"id": "erdos-0431", "source": "erdos", "number": "431", "title": "Erdős problem #431"},
    {"id": "erdos-0522", "source": "erdos", "number": "522", "title": "Erdős problem #522"},
    {"id": "erdos-0952", "source": "erdos", "number": "952", "title": "Erdős problem #952"},
    {"id": "kourovka-12-020", "source": "kourovka", "number": "12.20", "title": "Kourovka 12.20 · Is Thompson’s group F amenable?"},
    {"id": "kourovka-12-002", "source": "kourovka", "number": "12.2", "title": "Kourovka 12.2 · Another question"},
    {"id": "kourovka-19-012", "source": "kourovka", "number": "19.12", "title": "Kourovka 19.12 · A question"},
    {"id": "clay-hodge-conjecture", "source": "clay", "number": "hodge-conjecture", "title": "Hodge conjecture"},
    {"id": "clay-p-vs-np", "source": "clay", "number": "p-vs-np", "title": "P versus NP"},
    {"id": "clay-navier-stokes-equation", "source": "clay", "number": "navier-stokes-equation",
     "title": "Navier–Stokes existence and smoothness"},
    {"id": "hilbert-16-ii", "source": "hilbert", "number": "16, part II", "title": "Hilbert 16, part II: A uniform bound on limit cycles"},
    {"id": "hilbert-10-q", "source": "hilbert", "number": "10 over ℚ (extension)", "title": "Hilbert’s tenth problem over ℚ (open extension)"},
    {"id": "smale-06", "source": "smale", "number": "6", "title": "Smale 6: Finiteness of planar central configurations"},
    {"id": "bad id", "source": "erdos", "number": "7", "title": "Ignored"},
]}


def ids(index, text, **kwargs):
    return [candidate.problem_id for candidate in index.candidates(text, **kwargs)]


@pytest.mark.parametrize(("text", "expected"), [
    ("Universal Radial Laws for Random Polynomials: Erdős Problem #522 and Non-Universal Fluctuations", ["erdos-0522"]),
    ("feat(ErdosProblems/522): `erdos_522` is false (#7050)", ["erdos-0522"]),
    ("Bloom lists Erdős problems #3, #431 and #952 among the claims", ["erdos-0003", "erdos-0431", "erdos-0952"]),
    ("Erdos #3 and erdosproblems.com/forum/thread/431", ["erdos-0003", "erdos-0431"]),
    ("posed by Erdős in 1961; Erdős problem 99999; the Erdős problems web site", []),
    ("Is Thompson's group F amenable? Kourovka Notebook problem 12.20.", ["kourovka-12-020"]),
    ("On some problems from the Kourovka Notebook: we settle Problems 12.2 and 19.12", ["kourovka-12-002", "kourovka-19-012"]),
    ("We settle Problems 12.2 and 19.12 (without naming the source)", []),
    ("The second part of Hilbert's 16th problem asks for a uniform bound", ["hilbert-16-ii"]),
    ("Hilbert’s tenth problem over Q is undecidable", ["hilbert-10-q"]),
    ("Smale's sixth problem and Smale problem 6", ["smale-06"]),
    ("A proof of the Hodge conjecture for CM abelian varieties; also P vs NP", ["clay-hodge-conjecture", "clay-p-vs-np"]),
    ("Finite-time blowup for Navier–Stokes with smooth forcing", ["clay-navier-stokes-equation"]),
])
def test_candidates_from_names_and_numbers(text, expected):
    assert ids(news_links.AliasIndex(V2), text) == expected


def test_schema_3_rows_add_aliases_and_new_collections():
    v3 = {"schema_version": 3, "content_sha256": "a" * 64, "snapshot_date": "2026-10-11", "problems": [
        {"id": "wiki-collatz-conjecture", "source": "wikipedia", "title": "Collatz conjecture", "aliases": ["3x + 1 problem", "Syracuse problem"]},
        {"id": "hilbert-16", "source": "hilbert", "number": "16", "title": "Hilbert’s sixteenth problem"},
        {"id": "hilbert-16-ii", "source": "hilbert", "number": "16, part II", "title": "Hilbert 16, part II"},
        {"id": "clay-poincare-conjecture", "source": "clay", "number": "poincare-conjecture", "title": "Poincaré conjecture"},
        {"id": "erdos-1201", "source": "erdos", "number": "1201", "title": "Erdős #1201 · a named problem", "aliases": ["Sunflower bound"]}]}
    index = news_links.AliasIndex(v3)
    assert index.sha256 == "a" * 64 and index.schema_version == 3 and len(index) == 5
    assert ids(index, "The 3x+1 problem, also called the Syracuse problem") == ["wiki-collatz-conjecture"]
    assert ids(index, "Hilbert's sixteenth problem, part II") == ["hilbert-16", "hilbert-16-ii"]
    assert ids(index, "An AI-assisted formalization of the Poincaré conjecture and the sunflower bound") == [
        "clay-poincare-conjecture", "erdos-1201"]


def test_candidates_are_bounded_and_unknown_catalogues_are_refused():
    index = news_links.AliasIndex(V2)
    text = "Erdős problems #3, #431, #522 and #952; Hodge conjecture; P vs NP; Kourovka 12.20; Smale's 6th problem"
    assert len(ids(index, text)) == 8 and len(ids(index, text, limit=2)) == 2
    assert "bad id" not in index and "erdos-0522" in index and index.title("erdos-0522") == "Erdős problem #522"
    for bad in [{"schema_version": 9, "problems": []}, {"schema_version": 2}, []]:
        with pytest.raises(ValueError):
            news_links.AliasIndex(bad)


def test_shipped_catalogue_is_indexed_from_the_checkout(tmp_path):
    index = news_links.load_index()
    assert len(index) > 1000 and index.schema_version in (2, 3) and len(index.sha256) == 64
    assert ids(index, "A claimed resolution of Erdős Problem #522") == ["erdos-0522"]
    broken = tmp_path / "catalogue.json"
    broken.write_text("{")
    with pytest.raises(ValueError):
        news_links.load_index(broken)
    with pytest.raises(ValueError):
        news_links.load_index(tmp_path / "missing.json")
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(V2))
    assert news_links.load_index(copy).sha256 and len(news_links.load_index(copy)) == 13
