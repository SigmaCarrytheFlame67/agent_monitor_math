"""The ``run["readability"]`` block of a run record, built from the workspace.

``jobs.py`` stores it on the run record when the runner exits (and keeps the
questions asked about the run in it), so the console never has to read the
artifacts itself::

    {"schema": "ansatze-readability-v1",
     "source": {kind, input_url, resolved_url, canonical_id, title, version,
                license, format, warnings, bundle_sha256, math_fidelity},
     "caps": {max_parts, max_calls}, "audience": "graduate", "focus": "...",
     "summary": {parts, rewritten, faithful, issues, unchecked, not_rewritten,
                 kept, calls, model, route},
     "stopped": false,
     "ask": {...the question in flight...}, "asks": [...the last 50...]}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_monitor.analyze.record import _load
from agent_monitor.readability import request as readability_request

SCHEMA = "ansatze-readability-v1"
# ``parts`` (the receipt's ``total``) counts the parts meant to be rewritten;
# ``kept`` the references, which are kept as in the original.
COUNT_KEYS = ("total", "rewritten", "faithful", "issues", "unchecked", "not_rewritten", "kept")


def summary(receipt: dict[str, Any]) -> dict[str, Any]:
    """The record's summary of a published run, from its receipt."""
    parts = receipt.get("parts") if isinstance(receipt.get("parts"), dict) else {}
    out: dict[str, Any] = {"parts": int(parts.get("total") or 0)}
    for key in COUNT_KEYS[1:]:
        out[key] = int(parts.get(key) or 0)
    out.update(calls=int(receipt.get("calls") or 0), model=str(receipt.get("model") or ""),
               route=str(receipt.get("route") or ""))
    return out


def run_block(workspace: Path) -> dict[str, Any] | None:
    """The record block, or ``None`` before a readability request exists.

    ``summary`` is present only once the run was published.
    """
    root = Path(workspace)
    try:
        req = readability_request.load(root)
    except readability_request.RequestError:
        return None
    block: dict[str, Any] = {
        "schema": SCHEMA,
        "source": {"kind": req["source"]["kind"]},
        "caps": {key: value for key, value in req["caps"].items() if key in ("max_parts", "max_calls")},
        "audience": req["audience"],
        "focus": req["focus"],
    }
    if req["source"]["kind"] == "url":
        block["source"]["input_url"] = req["source"]["url"]
    manuscript = _load(root / "readability" / "manuscript.json")
    if manuscript:
        for key in ("input_url", "resolved_url", "canonical_id", "title", "version", "license", "format",
                    "bundle_sha256", "math_fidelity"):
            if manuscript.get(key):
                block["source"][key] = manuscript[key]
        block["source"]["warnings"] = list(manuscript.get("warnings") or [])[:12]
    receipt = _load(root / "readability" / "receipt.json")
    if receipt and receipt.get("schema") == "ansatze-readability-receipt-v1":
        caps = receipt.get("caps") or {}
        block["caps"] = {key: caps[key] for key in ("max_parts", "max_calls") if isinstance(caps.get(key), int)}
        block["summary"] = summary(receipt)
        block["stopped"] = bool(receipt.get("stopped"))
    return block
