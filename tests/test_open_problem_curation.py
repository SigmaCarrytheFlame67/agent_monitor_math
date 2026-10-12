"""Reviewed statements, claims and evidence survive the v3 build with valid provenance."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_monitor.problem_catalogue import CatalogueError, expand_row, link_list

ROOT = Path(__file__).resolve().parents[1]
CURATION = ROOT / "agent_monitor/data/open_problem_curation.json"


def catalogue():
    data = json.loads((ROOT / "agent_monitor/data/open_problems.json").read_text(encoding="utf-8"))
    return {row["id"]: expand_row(data, row) for row in data["problems"]}


def build_with(tmp_path, change):
    from scripts.build_problem_catalogue import build

    curation = json.loads(CURATION.read_text(encoding="utf-8"))
    change(curation)
    path = tmp_path / "curation.json"
    path.write_text(json.dumps(curation, ensure_ascii=False), encoding="utf-8")
    return build(curation=path)


def test_every_curated_statement_and_result_survives_with_https_sources():
    curation = json.loads(CURATION.read_text(encoding="utf-8"))
    rows = catalogue()
    curated = [*curation["overlays"], *(row["id"] for row in curation["problems"])]
    statements = [key for key in curated if (curation["overlays"].get(key) or {}).get("statement") or key in
                  {row["id"] for row in curation["problems"]}]
    assert len(statements) == 19  # 18 reviewed in September 2026, plus Poincaré
    for key in statements:
        row = rows[key]
        assert row["statement"]["kind"] == "editorial" and row["statement"]["text"]
        source = curation["overlays"].get(key) or next(item for item in curation["problems"] if item["id"] == key)
        assert row["statement"]["text"] == source["statement"]["text"]
        assert len(row.get("partial_results", [])) >= len(source.get("partial_results", []))
        for item in row.get("partial_results", []) + row.get("proof_claims", []):
            assert item["status"] and all(link["url"].startswith("https://") for link in item["sources"])
    # Kourovka statements point into the pinned arXiv edition, not the withdrawn upload.
    blob = json.dumps(curation, ensure_ascii=False)
    assert "21tkt.pdf" not in blob and "1401.0300v48#page=61" in blob


def test_claims_keep_their_reported_status_and_never_become_proofs():
    rows = catalogue()
    navier = rows["clay-navier-stokes-equation"]
    assert navier["proof_claims"][0]["status"] == "announced" and navier["category"] == "ai-proposed"
    assert "nu>0" in navier["statement"]["text"]
    assert rows["ramanujan-lehmer"]["proof_claims"][0]["status"] == "Withdrawn proof claim"
    assert rows["hilbert-16-ii"]["proof_claims"][0]["status"] == "Refuted proof claim"
    assert "open extension" in rows["hilbert-10-q"]["title"] and "complex" in rows["smale-11"]["title"]
    assert "nonholomorphic" in rows["ramanujan-maass"]["detail_note"]
    assert "polynomial-length certificates" in rows["clay-p-vs-np"]["statement"]["text"]
    for key in ("hilbert-16-ii", "smale-06", "ramanujan-brocard"):
        assert rows[key]["category"] != "solved" and rows[key]["source_status"]
    # The six editorial source-status notes are kept (the page shows source_status_note).
    for key in ("hilbert-16-ii", "hilbert-10-q", "smale-06", "smale-07", "smale-09", "smale-11"):
        assert rows[key]["source_status_note"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.com", "https://user:password@example.com", "/relative"])
def test_curated_links_reject_unsafe_or_unlabelled_urls(url):
    with pytest.raises(CatalogueError, match="HTTPS"):
        link_list([{"label": "Source", "url": url}], context="statement")
    with pytest.raises(CatalogueError):
        link_list([{"label": "", "url": "https://example.org/"}], context="statement")


@pytest.mark.parametrize("field", ["id", "number", "source", "source_url", "source_name", "source_locator"])
def test_overlays_cannot_replace_canonical_source_identity(tmp_path, field):
    def change(curation):
        curation["overlays"]["erdos-0003"][field] = "https://example.com"
    with pytest.raises(CatalogueError, match="canonical source identity"):
        build_with(tmp_path, change)


@pytest.mark.parametrize("field", ["title", "summary", "source_url", "source_locator", "source_status", "status_as_of"])
def test_new_curated_problems_require_metadata(tmp_path, field):
    def change(curation):
        del curation["problems"][0][field]
    with pytest.raises(CatalogueError, match="nonempty " + field):
        build_with(tmp_path, change)


def test_curated_category_needs_qualifying_evidence(tmp_path):
    def change(curation):
        overlay = curation["overlays"]["clay-hodge-conjecture"]
        overlay.update(category="solved", category_rule="S2",
                       status_evidence=[{"kind": "claim", "actor": "ai", "scope": "full", "outcome": "proof",
                                         "title": "A claim", "url": "https://example.org/claim", "acceptance": "preprint"}])
    with pytest.raises(CatalogueError, match="qualifying evidence"):
        build_with(tmp_path, change)


def test_claims_require_a_statement(tmp_path):
    def change(curation):
        problem = curation["problems"][0]
        problem["proof_claims"] = deepcopy(problem.get("proof_claims") or [{"title": "t", "text": "x", "status": "s",
                                                                           "sources": [{"label": "l", "url": "https://doi.org/1"}]}])
        del problem["statement"]
    with pytest.raises(CatalogueError, match="statement"):
        build_with(tmp_path, change)
