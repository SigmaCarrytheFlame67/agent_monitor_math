"""Deterministic Markdown report of an analysis (``analysis/report.md``).

Same graph, manuscript and receipt in, same bytes out: no clock is read here.
Sections: source card, verdict summary, main results with closure verdicts,
proof architecture, findings, external dependencies, results not checked,
formalization, and how the analysis was made (with its honesty caveats).
Model-written and manuscript text is escaped for Markdown tables and carries
no links: URLs are removed and link or image syntax (``[text](target)``,
``![alt](target)``) is broken up, whatever the target's scheme.  Only the
report's own source links (validated ``https`` URLs) are links.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Mapping

from agent_monitor.analyze.graph import CLAIM_KINDS

VERDICT_TEXT = {
    "checked": "checked (no issue found)",
    "gap": "gap",
    "error": "error",
    "unclear": "unclear",
    "not_checked": "not checked",
}
UNINTERPRETABLE_TEXT = "no usable verifier output"
_LINK_RE = re.compile(r"(?i)\b(?:(?:https?|ftp)://|www\.|(?:javascript|vbscript|data|file):)\S+")
# "[text](target)" is a Markdown link (and "![alt](target)" an image) only
# when "(" follows "]" directly; a space in between leaves plain text.
_LINK_SYNTAX_RE = re.compile(r"\]\s*\(")


def _cell(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    text = _LINK_RE.sub("[link removed]", text)
    text = _LINK_SYNTAX_RE.sub("] (", text)
    text = text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _line(value: object, limit: int = 600) -> str:
    return _cell(value, limit)


def _url(value: object) -> str:
    text = str(value or "")
    return text if text.startswith("https://") and not re.search(r"[\s<>()\[\]]", text) else ""


def _label(node: Mapping[str, Any]) -> str:
    return _cell(node.get("label") or node.get("id"), 120)


def _depth_and_path(graph: Mapping[str, Any]) -> tuple[int, list[str], list[tuple[str, int]]]:
    """Longest dependency chain (over strong edges), its path, and the most
    used results."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    predecessors: dict[str, set[str]] = defaultdict(set)
    uses: Counter = Counter()
    for edge in graph["edges"]:
        if edge.get("provenance") in ("ref", "inferred") and not edge.get("weak"):
            predecessors[edge["to"]].add(edge["from"])
            uses[edge["from"]] += 1
    memo: dict[str, tuple[int, list[str]]] = {}

    def longest(node_id: str, depth: int = 0) -> tuple[int, list[str]]:
        if node_id in memo:
            return memo[node_id]
        if depth > 300:
            return 1, [node_id]
        best = (1, [node_id])
        for used in sorted(predecessors[node_id]):
            length, path = longest(used, depth + 1)
            if length + 1 > best[0]:
                best = (length + 1, path + [node_id])
        memo[node_id] = best
        return best

    targets = graph.get("main_results") or list(nodes)
    depth, path = max((longest(node_id) for node_id in targets), key=lambda item: (item[0], item[1]), default=(0, []))
    overall = max((longest(node_id)[0] for node_id in nodes), default=0)
    most_used = [(node_id, count) for node_id, count in sorted(uses.items(), key=lambda item: (-item[1], item[0]))[:5]]
    return overall, path if depth > 1 else [], most_used


def render(graph: Mapping[str, Any], manuscript: Mapping[str, Any], receipt: Mapping[str, Any],
           *, paper_map: Mapping[str, Any] | None = None, formalize: bool = False) -> str:
    """The analysis report as Markdown."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    source = graph.get("source") or {}
    title = manuscript.get("title") or source.get("title") or "Untitled manuscript"
    model = receipt.get("model") or "unknown model"
    route = receipt.get("route") or "unknown route"
    lines: list[str] = [f"# Analysis: {_line(title, 300)}", ""]
    lines.append(
        f"> Checked by a model verifier ({_cell(model, 80)} via {_cell(route, 40)}) — not a certificate. "
        "Each result was checked on its own, given the statements it cites; verdicts are model judgements "
        "that can be wrong in both directions."
    )
    lines.append("")

    # Source card -------------------------------------------------------------
    lines += ["## Source", ""]
    link = _url(manuscript.get("resolved_url") or source.get("resolved_url"))
    rows = [("Title", _line(title, 300))]
    if manuscript.get("version"):
        rows.append(("Version", _cell(manuscript["version"], 80)))
    if link:
        rows.append(("Link", f"<{link}>"))
    if manuscript.get("canonical_id"):
        rows.append(("Identifier", f"`{_cell(manuscript['canonical_id'], 200)}`"))
    if manuscript.get("license"):
        rows.append(("Licence", _cell(manuscript["license"], 200)))
    fmt = manuscript.get("format") or ""
    if fmt:
        fidelity = " (text extracted from a PDF; formulas may be garbled)" if graph.get("math_fidelity") == "pdf-text" else ""
        rows.append(("Format", f"{_cell(fmt, 10)}{fidelity}"))
    if manuscript.get("bundle_sha256"):
        rows.append(("Source bundle", f"`{str(manuscript['bundle_sha256'])[:16]}`"))
    for key, value in rows:
        lines.append(f"- **{key}:** {value}")
    for warning in manuscript.get("warnings") or []:
        lines.append(f"- **Note:** {_line(warning, 400)}")
    upstream = (manuscript.get("upstream") or {}).get("openai_math") or {}
    if upstream:
        if upstream.get("withdrawn"):
            notice = _url(upstream.get("notice_url"))
            lines.append(
                "- **Withdrawn upstream"
                + (f" on {_cell(upstream.get('withdrawn_on'), 40)}" if upstream.get("withdrawn_on") else "")
                + ":** the authors withdrew this manuscript"
                + (f" (notice: <{notice}>)" if notice else "")
                + ". The archived revision was analyzed; the notice was not shown to the verifier."
            )
        if upstream.get("revised"):
            lines.append(f"- **Revised upstream:** {_line(upstream.get('version_note'), 400)}")
        if upstream.get("family"):
            lines.append(f"- **openai/math family:** {_cell(upstream['family'], 10)} — {_line(upstream.get('family_title'), 200)}")
    computational = manuscript.get("computational") or []
    if computational:
        lines.append(f"- **Not executed:** {len(computational)} computational file(s) in the source were skipped.")
    lines.append("")

    # Summary ---------------------------------------------------------------------
    coverage = graph.get("coverage") or {}
    counts = graph.get("counts") or {}
    unreadable = [node for node in graph["nodes"] if node["meta"].get("interpreted", True) is False]
    structural = graph.get("dependency_basis") != "model"
    lines += ["## Summary", ""]
    lines.append(
        f"Checked **{coverage.get('checked', 0)} of {coverage.get('total', 0)}** results whose proofs were located "
        f"in the manuscript ({len(nodes)} statements in total)."
    )
    lines.append("")
    lines.append("| Verdict | Results |")
    lines.append("| --- | --- |")
    for verdict in ("checked", "gap", "error", "unclear", "not_checked"):
        count = counts.get(verdict, 0)
        if verdict == "unclear":
            count -= sum(1 for node in unreadable if node["meta"]["verdict"] == "unclear")
        lines.append(f"| {VERDICT_TEXT[verdict]} | {count} |")
    if unreadable:
        lines.append(f"| {UNINTERPRETABLE_TEXT} | {len(unreadable)} |")
    lines.append("")
    if paper_map and paper_map.get("summary"):
        lines.append(f"*Overview (model-written from the abstract and introduction):* {_line(paper_map['summary'], 900)}")
        lines.append("")

    # Main results ---------------------------------------------------------------
    lines += ["## Main results", ""]
    mains = [nodes[node_id] for node_id in graph.get("main_results") or [] if node_id in nodes]
    if not mains:
        lines.append("No main result was identified.")
    for node in mains:
        meta = node["meta"]
        closure = meta.get("closure")
        text = f"- **{_label(node)}** — own verdict: {VERDICT_TEXT[meta['verdict']]}"
        if meta.get("interpreted", True) is False:
            text += f" ({UNINTERPRETABLE_TEXT})"
        elif meta["verdict"] == "not_checked" and meta.get("reason"):
            text += f" ({_cell(meta['reason'], 160)})"
        if closure:
            text += f"; with everything it depends on: **{VERDICT_TEXT[closure]}**"
        elif not structural and node["kind"] in CLAIM_KINDS:
            text += ("; its dependencies were not established (the statements were located by the model), "
                     "so no verdict with dependencies is given")
        path = meta.get("closure_path") or []
        if closure in ("error", "gap") and path and path != [node["id"]]:
            text += " (blocked by " + " → ".join(_label(nodes[step]) for step in path if step in nodes) + ")"
        conditions = []
        if meta.get("closure_external"):
            conditions.append(f"{meta['closure_external']} cited external result(s)")
        if meta.get("closure_assumptions"):
            conditions.append(f"{len(meta['closure_assumptions'])} assumed statement(s)")
        if conditions:
            text += "; conditional on " + " and ".join(conditions)
        lines.append(text)
    lines.append("")

    # Architecture -------------------------------------------------------------
    depth, path, most_used = _depth_and_path(graph)
    lines += ["## Proof architecture", ""]
    strong = sum(1 for edge in graph["edges"] if edge.get("provenance") in ("ref", "inferred") and not edge.get("weak"))
    inferred = sum(1 for edge in graph["edges"] if edge.get("weak") or edge.get("provenance") == "model")
    lines.append(f"- {len(nodes)} statements, {strong} dependencies from the manuscript's own references"
                 + (f", {inferred} weaker inferred or model-proposed links" if inferred else "") + ".")
    lines.append(f"- Longest dependency chain: {depth} result(s).")
    if path:
        lines.append("- Critical path to the main result: " + " → ".join(_label(nodes[step]) for step in path if step in nodes) + ".")
    if most_used:
        lines.append("- Most used: " + ", ".join(f"{_label(nodes[node_id])} ({count})" for node_id, count in most_used) + ".")
    lines.append("")

    # Findings --------------------------------------------------------------------
    # Unusable verifier output is no finding; it is listed under "Not checked".
    flagged = [node for node in graph["nodes"] if node["meta"]["verdict"] in ("error", "gap", "unclear")
               and node["meta"].get("interpreted", True)]
    order = {"error": 0, "gap": 1, "unclear": 2}
    flagged.sort(key=lambda node: (order[node["meta"]["verdict"]], graph["nodes"].index(node)))
    lines += ["## Findings", ""]
    if not flagged:
        lines.append("The verifier reported no errors, gaps or unclear results.")
    else:
        lines.append("| Result | Location | Verdict | With dependencies | Finding |")
        lines.append("| --- | --- | --- | --- | --- |")
        for node in flagged:
            meta = node["meta"]
            first = (meta.get("issues") or [{}])[0]
            finding = first.get("issue") or meta.get("notes") or meta.get("summary") or ""
            if first.get("location"):
                finding = f"{first['location']}: {finding}"
            location = meta.get("locator") or ""
            url = _url((meta.get("source_ref") or {}).get("url"))
            location_cell = f"[{_cell(location, 80)}]({url})" if url else _cell(location, 80)
            lines.append(f"| {_label(node)} | {location_cell} | {VERDICT_TEXT[meta['verdict']]} | "
                         f"{VERDICT_TEXT.get(meta.get('closure') or '', '—')} | {_cell(finding, 300)} |")
        lines.append("")
        for node in flagged:
            issues = node["meta"].get("issues") or []
            if len(issues) <= 1:
                continue
            lines.append(f"**{_label(node)}** — all findings:")
            for issue in issues:
                lines.append(f"- {issue['type']}: {_line(issue.get('location'), 120)} — {_line(issue.get('issue'), 600)}")
            lines.append("")
    if lines[-1] != "":
        lines.append("")

    # External ---------------------------------------------------------------------
    external: dict[str, list[str]] = defaultdict(list)
    for node in graph["nodes"]:
        for item in node["meta"].get("external") or []:
            key = f"{_cell(item.get('citation'), 120)} — hypotheses checked: {item.get('hypotheses_checked', 'unknown')}"
            external[key].append(_label(node))
    lines += ["## External results relied on", ""]
    if not external:
        lines.append("None were reported.")
    for key in sorted(external):
        users = ", ".join(sorted(set(external[key])))
        lines.append(f"- {key} (used in {users})")
    lines.append("")

    # Not checked --------------------------------------------------------------------
    unchecked = [node for node in graph["nodes"] if node["meta"]["verdict"] == "not_checked"]
    reasons = Counter(node["meta"].get("reason") or "not checked" for node in unchecked)
    lines += ["## Not checked", ""]
    if not unchecked and not unreadable:
        lines.append("Every statement was checked.")
    for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {count} × {_line(reason, 200)}")
    if unreadable:
        lines.append(f"- {len(unreadable)} × The verifier output could not be interpreted, so no judgement is "
                     f"recorded ({', '.join(_label(node) for node in unreadable[:8])}"
                     + (", …" if len(unreadable) > 8 else "") + ")")
    lines.append("")

    # Formalization -------------------------------------------------------------------
    lines += ["## Formalization", ""]
    if formalize:
        lines.append("Statement formalization in Lean 4.14 was requested: selected statements are translated and "
                     "type-checked, proofs are not attempted. See the Lean tab.")
    else:
        lines.append("No formalization was requested for this analysis.")
    if upstream.get("lean_docs_url") or upstream.get("comparator"):
        docs = _url(upstream.get("lean_docs_url"))
        if docs:
            lines.append(f"- Upstream Lean scope: <{docs}>")
        for url in upstream.get("comparator") or []:
            if _url(url):
                lines.append(f"- Upstream Comparator statement: <{url}>")
        lines.append(f"- {_line(upstream.get('lean_note') or 'Upstream Lean; not re-checked by this server', 200)}")
    lines.append("")

    # Method -------------------------------------------------------------------------
    lines += ["## How this analysis was made", ""]
    parser = receipt.get("parser") or graph.get("parser") or "unknown"
    if structural:
        lines.append(f"- Structure: deterministic parse of the manuscript ({_cell(parser, 60)}); dependencies come "
                     "from its own labels and references (inferred and model-proposed links are marked as such).")
    else:
        lines.append(f"- Structure: the model located the statements ({_cell(parser, 60)}; every quote was checked "
                     "against the text). The links between them are model proposals, so verdicts cover each result "
                     "on its own, not what it depends on.")
    lines.append(f"- Checks: {receipt.get('calls', 0)} model call(s) to {_cell(model, 80)} via {_cell(route, 40)}; "
                 "each result was checked independently, assuming the statements it cites.")
    lines.append("- A verdict of \"checked\" means the verifier found no error or gap in the proof as written. "
                 "It is not a certificate, a refereed review or a Lean proof.")
    if receipt.get("stopped"):
        lines.append("- The analysis was stopped before every planned check finished.")
    if receipt.get("degraded"):
        lines.append("- Limits reached: " + ", ".join(_cell(item, 60) for item in receipt["degraded"]) + ".")
    finished = str(receipt.get("finished_at") or "")[:10]
    if finished:
        lines.append(f"- Analyzed on {finished}.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
