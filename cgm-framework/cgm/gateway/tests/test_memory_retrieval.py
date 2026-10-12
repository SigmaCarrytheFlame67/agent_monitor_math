"""Search previews and selected full reads, through the role-gated gateway."""
import asyncio
import json

import pytest

from cgm.core import GlobalMemory
from cgm.gateway import server


def test_search_keeps_full_text_ranking_but_returns_only_previews(tmp_path, monkeypatch):
    monkeypatch.setenv("CGM_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CGM_AGENTS_ROOT", raising=False)
    gm = GlobalMemory(tmp_path)
    long_claim = "A bounded preview " * 100
    evidence = "spectral obstruction " * 1000
    first = gm.append("plan", claim=long_claim, evidence=evidence, author="w",
                      glossary={"X": "a space"}, links={"subgoal": "G"})
    second = gm.append("plan", claim="Another route", evidence="spectral obstruction", author="w")
    report = gm.append("verification", claim="A claim", evidence="spectral obstruction", author="w",
                       verdict="wrong", verification_report={"gaps": [{"issue": evidence}]})
    gm.set_status(first, "supported", fact_id="0123456789abcdef")
    full = gm.search("spectral", kinds=["plan", "verification"], limit_per_kind=2)
    preview = server.gm_search("spectral", kinds=["plan", "verification"], limit_per_kind=2)
    assert preview["view"] == "summary"
    for kind, original in full["results_by_kind"].items():
        compact = preview["results_by_kind"][kind]
        assert compact["count"] == original["count"]
        assert [(h["score"], h["entry"]["id"]) for h in compact["results"]] == [
            (h["score"], h["entry"]["id"]) for h in original["results"]]
        for hit in compact["results"]:
            assert not {"evidence", "glossary", "links", "verification_report"} & hit["entry"].keys()
    by_id = {h["entry"]["id"]: h["entry"] for h in preview["results_by_kind"]["plan"]["results"]}
    assert by_id[first]["claim"] == long_claim[:600]
    assert by_id[first]["claim_truncated"] is True
    assert by_id[first]["status"] == "supported"
    assert by_id[first]["fact_id"] == "0123456789abcdef"
    assert by_id[second]["claim_truncated"] is False
    assert preview["results_by_kind"]["verification"]["results"][0]["entry"]["verdict"] == "wrong"
    assert len(json.dumps(preview)) < len(json.dumps(full)) / 10
    details = server.gm_get([report, first, "unknown", first, "unknown"])
    assert details == {"entries": gm.get_many([report, first]), "missing_ids": ["unknown"]}
    assert details["entries"][1]["claim"] == long_claim
    assert details["entries"][1]["evidence"] == evidence
    assert details["entries"][0]["verification_report"]["gaps"][0]["issue"] == evidence
    assert server.gm_get([]) == {"entries": [], "missing_ids": []}
    assert server.gm_search("zzznomatch", kinds=["plan"])["results_by_kind"]["plan"]["count"] == 0


def test_get_uses_same_project_selector_as_search(tmp_path, monkeypatch):
    monkeypatch.setenv("CGM_AGENTS_ROOT", str(tmp_path))
    monkeypatch.delenv("CGM_PROJECT_DIR", raising=False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    entry_id = GlobalMemory(a).append("plan", claim="Route", evidence="Details", author="w")
    assert server.gm_get([entry_id], project="a")["entries"] == GlobalMemory(a).read("plan")
    assert server.gm_get([entry_id], project="b") == {"entries": [], "missing_ids": [entry_id]}
    for invalid in ("../a", "/a", "missing", ""):
        with pytest.raises(RuntimeError):
            server.gm_get([entry_id], project=invalid)


@pytest.mark.parametrize("mode", [None, "CGM_FACT_TEXT_MODE", "CGM_SELF_VERIFY_GATE"])
def test_get_is_registered_only_for_memory_readers(monkeypatch, mode):
    for name in ("CGM_FACT_TEXT_MODE", "CGM_SELF_VERIFY_GATE"):
        monkeypatch.delenv(name, raising=False)
    if mode:
        monkeypatch.setenv(mode, "1")
    for role in ("worker", "main", "curator", "all", "verifier", "unknown"):
        tools = asyncio.run(server.build_app(role).list_tools())
        names = {tool.name for tool in tools}
        assert ("gm_get" in names) == ("gm_search" in names) == (role in {"worker", "main", "curator", "all"})
