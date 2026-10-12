"""Operator review CLI: decisions record who, when, why and evidence; signals can be rejected."""
import json

import pytest

from agent_monitor.problem_catalogue import Builder, encode_catalogue
from scripts import review_problem_status as review
from tests.test_problem_catalogue_build import curation, facts


@pytest.fixture()
def files(tmp_path):
    document = Builder(facts(), curation(), None, None, rating_method={}, input_hashes={}).build()
    (tmp_path / "catalogue.json").write_text(encode_catalogue(document))
    (tmp_path / "decisions.json").write_text(json.dumps({"schema_version": 1, "decisions": {}, "rejected_signals": {}}))
    (tmp_path / "research.json").write_text(json.dumps({"schema_version": 1, "entries": {
        "erdos-0002": {"category": "solved", "category_rule": "S2", "needs_human_review": True, "review_reason": "Scope unclear"}}}))
    overlay = {"signals": {"erdos-0003": [{"title": "Palomar: Erdős 3", "url": "https://palomar-registry.org/entry?id=P&version=1",
                                           "relation": "formalization", "source": "palomar", "matched_by": "deterministic",
                                           "first_seen": "2026-10-12T00:00:00Z"}]}}
    (tmp_path / "overlay.json").write_text(json.dumps(overlay))
    return tmp_path


def cli(files, *argv):
    return review.main(["--catalogue", str(files / "catalogue.json"), "--decisions", str(files / "decisions.json"),
                        "--research", str(files / "research.json"), "--overlay", str(files / "overlay.json"), *argv])


def decisions(files):
    return json.loads((files / "decisions.json").read_text())


def test_list_and_show_cover_signals_research_and_rows(files, capsys):
    assert cli(files, "list") == 0
    out = capsys.readouterr().out
    assert "erdos-0003|" in out and "palomar" in out and "research · erdos-0002 · solved · Scope unclear" in out
    assert cli(files, "show", "kourovka-01-006") == 0
    out = capsys.readouterr().out
    assert "solved (S1)" in out and "https://zenodo.org/records/1" in out
    with pytest.raises(SystemExit):
        cli(files, "show", "no-such-row")


def test_accept_records_an_evidence_backed_decision_and_rejects_weak_ones(files, capsys):
    assert cli(files, "accept", "erdos-0002", "--category", "ai-proposed", "--rule", "A2",
               "--evidence-url", "https://zenodo.org/records/42", "--evidence-title", "An AI proof of Erdős #2",
               "--actor", "ai", "--acceptance", "preprint", "--note", "Full claim checked against the statement",
               "--by", "reviewer") == 0
    assert "build_problem_catalogue.py" in capsys.readouterr().out
    decision = decisions(files)["decisions"]["erdos-0002"]
    assert (decision["category"], decision["category_rule"], decision["decided_by"]) == ("ai-proposed", "A2", "reviewer")
    assert decision["evidence"][0]["kind"] == "claim" and decision["evidence"][0]["url"] == "https://zenodo.org/records/42"
    assert len(decision["decided_at"]) == 10 and decision["note"]
    for argv in (["--category", "solved", "--rule", "S2", "--evidence-url", "https://zenodo.org/records/42",
                  "--evidence-title", "A preprint", "--acceptance", "preprint"],          # solved needs accepted evidence
                 ["--category", "solved", "--rule", "A2"],                                 # rule from another category
                 ["--category", "partial", "--rule", "P2"],                                # no evidence at all
                 ["--category", "solved", "--rule", "S2", "--evidence-url", "http://insecure.example/x",
                  "--evidence-title", "x", "--acceptance", "refereed"]):                   # not https
        with pytest.raises(SystemExit):
            cli(files, "accept", "kourovka-01-003", *argv, "--note", "n", "--by", "reviewer")
    assert "kourovka-01-003" not in decisions(files)["decisions"]
    # "Solved" rests on the decision's own evidence: a part already marked ∗ (kourovka-01-010)
    # plus a bare link, or an accepted answer to one part only, is not enough.
    for argv in (["--evidence-url", "https://example.org/blog-post"],
                 ["--evidence-url", "https://doi.org/10.1/a", "--evidence-title", "Part (a)", "--acceptance", "refereed",
                  "--scope", "part"]):
        with pytest.raises(SystemExit, match="qualifying evidence"):
            cli(files, "accept", "kourovka-01-010", "--category", "solved", "--rule", "S2", *argv, "--note", "n", "--by", "r")
    assert "kourovka-01-010" not in decisions(files)["decisions"]
    assert cli(files, "accept", "erdos-0004", "--category", "solved", "--rule", "S2", "--evidence-url", "https://doi.org/10.1/x",
               "--evidence-title", "Refereed proof", "--acceptance", "refereed", "--outcome", "proof",
               "--note", "Published in a journal", "--by", "reviewer") == 0


def test_set_reject_and_revoke(files):
    assert cli(files, "set", "erdos-0003", "--badge-add", "disputed", "--headline", "A claim is disputed.",
               "--note", "Public dispute", "--by", "reviewer") == 0
    assert decisions(files)["decisions"]["erdos-0003"]["badges_add"] == ["disputed"]
    with pytest.raises(SystemExit):
        cli(files, "set", "erdos-0003", "--note", "nothing to set", "--by", "reviewer")
    assert cli(files, "reject", "erdos-0003|0123456789abcdef", "--note", "Not about this problem", "--by", "reviewer") == 0
    assert decisions(files)["rejected_signals"]["erdos-0003|0123456789abcdef"]["note"] == "Not about this problem"
    with pytest.raises(SystemExit):
        cli(files, "reject", "erdos-0003", "--note", "n", "--by", "reviewer")
    assert cli(files, "revoke", "erdos-0003") == 0 and "erdos-0003" not in decisions(files)["decisions"]
    with pytest.raises(SystemExit):
        cli(files, "revoke", "erdos-0003")


def test_reject_needs_a_signal_key_of_a_catalogue_row_and_a_reason(files):
    for key, note in (("nonsense|x", "not a key"), ("no-such-row|0123456789abcdef", "unknown row"),
                      ("erdos-0003|0123456789ABCDEF", "upper case"), ("erdos-0003|0123456789abcdef", "  ")):
        with pytest.raises(SystemExit):
            cli(files, "reject", key, "--note", note, "--by", "reviewer")
    assert decisions(files)["rejected_signals"] == {}


def test_a_decision_cannot_claim_a_source_marker_rule(files):
    # S1, A1 and P1 say "the source of record marks it"; only the parsers may set them.
    for category, rule in (("solved", "S1"), ("ai-proposed", "A1"), ("partial", "P1")):
        with pytest.raises(SystemExit):
            cli(files, "accept", "erdos-0004", "--category", category, "--rule", rule, "--evidence-url", "https://doi.org/10.1/x",
                "--evidence-title", "Refereed proof", "--acceptance", "refereed", "--outcome", "proof",
                "--scope", "full" if category != "partial" else "part", "--note", "n", "--by", "reviewer")
    assert "erdos-0004" not in decisions(files)["decisions"]
