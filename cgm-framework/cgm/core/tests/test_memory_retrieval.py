"""Selected global-memory details preserve full content and current status."""
from cgm.core import GlobalMemory


def test_get_many_preserves_full_entries_order_and_status(tmp_path):
    gm = GlobalMemory(tmp_path)
    plan = gm.append("plan", claim="A route", evidence="Full rationale", author="w",
                     links={"subgoal": "G"}, glossary={"X": "a set"})
    rejection = gm.append("verification", claim="A claim", evidence="Wrong", author="w",
                          verdict="wrong", verification_report={"gaps": [{"issue": "missing step"}]})
    gm.set_status(plan, "supported", fact_id="0123456789abcdef")
    expected_plan = gm.read("plan")[0]
    expected_rejection = gm.read("verification")[0]
    assert gm.get_many([rejection, "unknown", plan, rejection]) == [expected_rejection, expected_plan]
    assert gm.get_many([]) == []
    assert gm.get_many(["unknown"]) == []
    gm.set_status(plan, "challenged")
    assert gm.get_many([plan]) == gm.read("plan")
