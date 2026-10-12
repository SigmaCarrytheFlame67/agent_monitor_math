#!/usr/bin/env python3
"""Publish the live problem-status overlay (run by ansatze-problems.timer every six hours).

    update_problem_status.py --output /var/lib/ansatze-problems/problem-status.json \\
        --state /var/lib/ansatze-problems/state.json [--news /var/lib/ansatze-news/news.json]

The overlay is computed against the catalogue and source fact files of this
checkout.  It is written first (0644), then the private state (0600); both
atomically.  --dry-run writes nothing and makes no Kimi calls (calls it made
could not be counted in the persisted daily budget); to exercise the model,
run against copies of the output and state instead.  --no-model (or
ANSATZE_PROBLEMS_MODEL_CALLS_PER_DAY=0) makes no Kimi calls.  --only checks
some sources; the others keep their last published entries and status.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_monitor import problem_status as status  # noqa: E402


def main(argv=None, fetcher=None, model_request=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--catalogue", type=Path, default=ROOT / "agent_monitor/data/open_problems.json")
    parser.add_argument("--facts", type=Path, default=ROOT / "agent_monitor/data/problem_sources")
    parser.add_argument("--decisions", type=Path, default=ROOT / "agent_monitor/data/problem_status_decisions.json")
    parser.add_argument("--news", type=Path, help="public news.json whose problem links become signals")
    parser.add_argument("--only", help="comma-separated sources to check (the others keep their last result): "
                        + ",".join(status.SOURCES))
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="write nothing and make no model calls")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.state.resolve():
            raise status.StatusError("Output and private state must be different files")
        sources = status.SOURCES
        if args.only:
            sources = tuple(item.strip() for item in args.only.split(",") if item.strip())
            unknown = sorted(set(sources) - set(status.SOURCES))
            if unknown:
                raise status.StatusError("Unknown source: " + ", ".join(unknown))
        catalogue = status.load_optional(args.catalogue)
        if not catalogue:
            raise status.StatusError("The catalogue is missing")
        try:
            previous = status.load_optional(args.output)
        except status.StatusError:
            previous = {}  # only used for the last-success time; the new overlay replaces it
        news = None
        if args.news:
            try:
                # The news worker always writes this file: a missing one is an error, not an empty feed.
                news = status.load_optional(args.news) if args.news.is_file() else {"_load_error": "news.json is missing"}
            except (status.StatusError, OSError) as error:
                # Only the news source fails; the other sources still run.
                news = {"_load_error": str(error)[:160] if isinstance(error, status.StatusError) else "news.json is unreadable"}
        cap = 0 if args.no_model or args.dry_run else status.daily_cap()
        overlay, state = status.refresh(
            catalogue, status.load_facts(args.facts), state=status.load_optional(args.state, limit=64 * 1024 * 1024),
            previous=previous, decisions=status.load_optional(args.decisions),
            news=news, sources=sources, fetcher=fetcher,
            model_cap=cap or None, model_request=model_request,
        )
        if not args.dry_run:
            # Publish first: if publication fails, the state does not advance and
            # the next run repeats the same source and model work.
            status.atomic_json(args.output, overlay)
            status.atomic_json(args.state, state, mode=0o600)
        print(json.dumps({"ok": True, "dry_run": args.dry_run, "entries": len(overlay["entries"]),
                          "signals": sum(len(items) for items in overlay["signals"].values()),
                          "source_errors": len(overlay["source_errors"]), "model_calls_today": overlay["model"]["calls_today"],
                          "category_counts": overlay["category_counts"]}, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - one public-safe line for the journal
        message = str(error)[:200] if isinstance(error, (status.StatusError, ValueError, OSError)) else type(error).__name__
        print(json.dumps({"ok": False, "error": message}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
