#!/usr/bin/env python3
"""Review problem statuses and record operator decisions.

    review_problem_status.py list                      # unreviewed signals and research flagged for review
    review_problem_status.py show erdos-0003           # a row's status, evidence, research and signals
    review_problem_status.py accept erdos-0003 --category ai-proposed --rule A2 \\
        --evidence-url https://... --evidence-title "..." --acceptance preprint --actor ai --note "..."
    review_problem_status.py set erdos-0003 --badge-add disputed --note "..."   # badges or headline only
    review_problem_status.py reject 'erdos-0003|1a2b3c4d5e6f7a8b' --note "not about this problem"
    review_problem_status.py revoke erdos-0003

Decisions are written to agent_monitor/data/problem_status_decisions.json and
override research and automatic categories when the catalogue is rebuilt; a
decision with a category also keeps the live overlay from changing that
category.  A decision records who, when, why and its evidence; "solved" needs
evidence of its own (--evidence-title with --acceptance refereed,
accepted-by-maintainer or broadly-accepted and --scope full).  Rejected signals
stop appearing in the live overlay.  `show --state <state.json>` also prints
the privately kept quote of a Wikipedia report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_monitor.problem_catalogue import (  # noqa: E402
    ACCEPTANCE, ACTORS, CATALOGUE_BADGES, EVIDENCE_KINDS, OUTCOMES, RULES, SCOPES, CatalogueError, evidence_item,
    expand_row, normalize_decisions,
)
from agent_monitor.problem_status import signal_key  # noqa: E402

DATA = ROOT / "agent_monitor/data"
REBUILD = "python scripts/build_problem_catalogue.py   # then commit, build the docs and deploy the catalogue"


def _load(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        if default is not None:
            return json.loads(json.dumps(default))
        raise SystemExit(f"{path} does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _rows(args) -> dict[str, dict]:
    catalogue = _load(args.catalogue)
    return {row["id"]: expand_row(catalogue, row) for row in catalogue.get("problems", [])}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _decisions(args) -> dict:
    data = _load(args.decisions, {"schema_version": 1, "decisions": {}, "rejected_signals": {}})
    data.setdefault("decisions", {})
    data.setdefault("rejected_signals", {})
    return data


def command_list(args) -> int:
    rows = _rows(args)
    overlay = _load(args.overlay, {}) if args.overlay else {}
    decisions = _decisions(args)
    research = _load(args.research, {"entries": {}})
    pending = []
    for problem_id, signals in sorted((overlay.get("signals") or {}).items()):
        for signal in signals:
            key = signal_key(problem_id, signal["url"])
            if key not in decisions["rejected_signals"]:
                pending.append({"type": "signal", "key": key, "id": problem_id, "relation": signal["relation"],
                                "source": signal["source"], "title": signal["title"], "url": signal["url"]})
    for key, entry in sorted((research.get("entries") or {}).items()):
        if entry.get("needs_human_review") or entry.get("verified") == "uncertain":
            pending.append({"type": "research", "key": key, "category": entry.get("category"),
                            "reason": (entry.get("review_reason") or "")[:200]})
    for problem_id, row in sorted(rows.items()):
        if row.get("needs_review") and problem_id not in decisions["decisions"]:
            pending.append({"type": "row", "id": problem_id, "category": row["category"], "headline": row["status_headline"][:200]})
    if args.json:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    else:
        for item in pending:
            print(" · ".join(str(value) for value in item.values()))
        print(f"{len(pending)} item(s) to review")
    return 0


def command_show(args) -> int:
    rows = _rows(args)
    row = rows.get(args.id)
    if row is None:
        raise SystemExit(f"Unknown problem id {args.id}")
    overlay = _load(args.overlay, {}) if args.overlay else {}
    research = _load(args.research, {"entries": {}}).get("entries", {})
    decisions = _decisions(args)["decisions"]
    print(f"{row['id']} — {row['title']}")
    print(f"  category: {row['category']} ({row['category_rule']}) · {row['status_headline']}")
    print(f"  source: {row['source_url']} · {row.get('source_status', '')}")
    if row.get("badges"):
        print("  badges: " + ", ".join(row["badges"]))
    for item in row.get("status_evidence", []):
        print(f"  evidence: [{item['kind']} · {item['actor']} · {item['acceptance']}] {item['title']} — {item['url']}")
    key = row.get("source_key") or row["id"]
    if key in research:
        entry = research[key]
        print(f"  research {key}: {entry.get('category')} ({entry.get('category_rule')}) · verified {entry.get('verified', 'unchecked')}"
              f" · review: {bool(entry.get('needs_human_review'))} {entry.get('review_reason', '')}")
    if args.id in decisions:
        print("  decision: " + json.dumps(decisions[args.id], ensure_ascii=False))
    entry = (overlay.get("entries") or {}).get(args.id)
    if entry:
        print("  live entry: " + json.dumps(entry, ensure_ascii=False))
    private = (_load(args.state, {}).get("signals") or {}) if args.state else {}
    for signal in (overlay.get("signals") or {}).get(args.id, []):
        key = signal_key(args.id, signal["url"])
        print(f"  signal {key}: {signal['relation']} via {signal['source']} — {signal['title']} {signal['url']}")
        if (private.get(key) or {}).get("private_quote"):
            # Wikipedia text is kept out of the public overlay; shown here for review only.
            print(f"    quote (private): {private[key]['private_quote']}")
    return 0


def _record(args, decision: dict) -> int:
    rows = _rows(args)
    if args.id not in rows:
        raise SystemExit(f"Unknown problem id {args.id}")
    data = _decisions(args)
    decision.update(decided_by=args.by, decided_at=_today(), note=args.note)
    candidate = json.loads(json.dumps(data))
    candidate["decisions"][args.id] = decision
    try:
        # Also checks that "solved" rests on the decision's own qualifying evidence
        # (a refereed, maintainer-accepted or broadly accepted full answer), never on
        # the row's existing markers or a bare link.
        normalize_decisions(candidate)
    except CatalogueError as error:
        raise SystemExit(f"Decision rejected: {error}")
    data["decisions"][args.id] = decision
    data["decisions"] = dict(sorted(data["decisions"].items()))
    _write(args.decisions, data)
    print(f"Recorded the decision for {args.id}. Rebuild the catalogue:\n  {REBUILD}")
    return 0


def command_accept(args) -> int:
    if args.rule not in RULES[args.category]:
        raise SystemExit(f"Rule {args.rule} does not belong to category {args.category}")
    evidence = []
    if args.evidence_title:
        if not args.evidence_url:
            raise SystemExit("--evidence-title needs --evidence-url")
        kind = args.kind or {"solved": "accepted-result", "ai-proposed": "claim", "partial": "partial-result"}.get(args.category, "other")
        item = {"kind": kind, "actor": args.actor, "scope": args.scope, "outcome": args.outcome,
                "title": args.evidence_title, "url": args.evidence_url[0], "acceptance": args.acceptance}
        if args.date:
            item["date"] = args.date
        if args.claimant:
            item["claimant"] = args.claimant
        try:
            evidence.append(evidence_item(item))
        except CatalogueError as error:
            raise SystemExit(f"Evidence rejected: {error}")
    decision = {"category": args.category, "category_rule": args.rule, "evidence_urls": args.evidence_url or [],
                "evidence": evidence, "badges_add": args.badge_add or [], "badges_remove": args.badge_remove or []}
    if args.headline:
        decision["status_headline"] = args.headline
    return _record(args, decision)


def command_set(args) -> int:
    if not (args.headline or args.badge_add or args.badge_remove):
        raise SystemExit("Nothing to set: pass --headline, --badge-add or --badge-remove")
    decision = {"badges_add": args.badge_add or [], "badges_remove": args.badge_remove or []}
    if args.headline:
        decision["status_headline"] = args.headline
    return _record(args, decision)


SIGNAL_KEY = re.compile(r"([a-z0-9][a-z0-9._-]{0,159})\|[0-9a-f]{16}")


def command_reject(args) -> int:
    match = SIGNAL_KEY.fullmatch(args.key)
    if not match:
        raise SystemExit("A signal key looks like 'erdos-0003|1a2b3c4d5e6f7a8b' (see the list command)")
    if match.group(1) not in _rows(args):
        raise SystemExit(f"Unknown problem id {match.group(1)}")
    if not args.note.strip():
        raise SystemExit("A rejection records why (--note)")
    data = _decisions(args)
    data["rejected_signals"][args.key] = {"decided_by": args.by, "decided_at": _today(), "note": args.note}
    data["rejected_signals"] = dict(sorted(data["rejected_signals"].items()))
    _write(args.decisions, data)
    print(f"Rejected {args.key}; it disappears from the overlay on the next timer run.")
    return 0


def command_revoke(args) -> int:
    data = _decisions(args)
    if data["decisions"].pop(args.id, None) is None:
        raise SystemExit(f"No decision recorded for {args.id}")
    _write(args.decisions, data)
    print(f"Removed the decision for {args.id}. Rebuild the catalogue:\n  {REBUILD}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalogue", type=Path, default=DATA / "open_problems.json")
    parser.add_argument("--research", type=Path, default=DATA / "problem_status_research.json")
    parser.add_argument("--decisions", type=Path, default=DATA / "problem_status_decisions.json")
    parser.add_argument("--overlay", type=Path, help="a published problem-status.json (for signals)")
    parser.add_argument("--state", type=Path, help="the updater's private state.json (quotes of Wikipedia reports)")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("id")

    def who(command):
        command.add_argument("--note", required=True, help="why (≤ 400 characters)")
        command.add_argument("--by", default=os.environ.get("ANSATZE_REVIEWER") or getpass.getuser())

    def badges(command):
        command.add_argument("--headline", help="status headline (≤ 280 characters)")
        command.add_argument("--badge-add", action="append", choices=CATALOGUE_BADGES)
        command.add_argument("--badge-remove", action="append", choices=CATALOGUE_BADGES)

    accept = sub.add_parser("accept")
    accept.add_argument("id")
    accept.add_argument("--category", required=True, choices=tuple(RULES))
    accept.add_argument("--rule", required=True)
    accept.add_argument("--evidence-url", action="append")
    accept.add_argument("--evidence-title")
    accept.add_argument("--kind", choices=EVIDENCE_KINDS)
    accept.add_argument("--actor", default="human", choices=ACTORS)
    accept.add_argument("--scope", default="full", choices=SCOPES)
    accept.add_argument("--outcome", default="other", choices=OUTCOMES)
    accept.add_argument("--acceptance", default="unknown", choices=ACCEPTANCE)
    accept.add_argument("--date")
    accept.add_argument("--claimant")
    badges(accept)
    who(accept)
    setter = sub.add_parser("set")
    setter.add_argument("id")
    badges(setter)
    who(setter)
    reject = sub.add_parser("reject")
    reject.add_argument("key")
    who(reject)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("id")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return {"list": command_list, "show": command_show, "accept": command_accept, "set": command_set,
            "reject": command_reject, "revoke": command_revoke}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
