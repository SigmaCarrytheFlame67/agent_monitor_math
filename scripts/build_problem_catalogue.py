#!/usr/bin/env python3
"""Build the v3 problem catalogue from the source fact files, curation, research and decisions.

Reads local files only (refresh the fact files first with
scripts/refresh_problem_sources.py) and writes the three byte-identical
catalogue copies plus the bundled, empty status overlay:

  agent_monitor/data/open_problems.json
  docs/public/open-problems.json
  docs/public/research/open-problems.json
  docs/public/research/problem-status.json

The same inputs always produce the same bytes.  Every problem id of the
previously published catalogue must still exist (forum threads, DAG memory and
the solve hand-off store ids); the build fails otherwise.

    python scripts/build_problem_catalogue.py            # rebuild
    python scripts/build_problem_catalogue.py --check    # verify the committed files
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_monitor.problem_catalogue import (  # noqa: E402
    FACT_FILES, Builder, CatalogueError, empty_overlay, encode_catalogue, validate_catalogue,
)

DATA = ROOT / "agent_monitor/data"
FACTS = DATA / "problem_sources"
CURATION = DATA / "open_problem_curation.json"
RESEARCH = DATA / "problem_status_research.json"
DECISIONS = DATA / "problem_status_decisions.json"
OUTPUTS = (DATA / "open_problems.json", ROOT / "docs/public/open-problems.json",
           ROOT / "docs/public/research/open-problems.json")
OVERLAY = ROOT / "docs/public/research/problem-status.json"


def _read(path: Path, *, optional: bool = False) -> tuple[dict | None, str | None]:
    if optional and not path.exists():
        return None, None
    content = path.read_bytes()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise CatalogueError(f"{path.name} is not a JSON object")
    return value, hashlib.sha256(content).hexdigest()


def build(facts_dir: Path = FACTS, curation: Path = CURATION, research: Path = RESEARCH,
          decisions: Path = DECISIONS) -> tuple[dict, list[str]]:
    from scripts.problem_expansion import RATING_METHOD

    facts, hashes = {}, {}
    for name, filename in FACT_FILES.items():
        facts[name], hashes[f"problem_sources/{filename}"] = _read(facts_dir / filename)
    curated, hashes["editorial_curation"] = _read(curation)
    if curated.get("schema_version") != 1:
        raise CatalogueError("Unsupported editorial curation schema")
    researched, research_hash = _read(research, optional=True)
    decided, decision_hash = _read(decisions, optional=True)
    if research_hash:
        hashes["problem_status_research"] = research_hash
    if decision_hash:
        hashes["problem_status_decisions"] = decision_hash
    builder = Builder(facts, curated, researched, decided, rating_method=RATING_METHOD, input_hashes=hashes)
    document = builder.build()
    validate_catalogue(document)
    return document, builder.warnings


def previous_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {row["id"] for row in data.get("problems", []) if isinstance(row, dict) and "id" in row}


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--facts", type=Path, default=FACTS)
    parser.add_argument("--curation", type=Path, default=CURATION)
    parser.add_argument("--research", type=Path, default=RESEARCH)
    parser.add_argument("--decisions", type=Path, default=DECISIONS)
    parser.add_argument("--previous", type=Path, default=OUTPUTS[0],
                        help="published catalogue whose ids must all survive (default: the committed copy)")
    parser.add_argument("--check", action="store_true", help="exit 1 unless the committed outputs equal a fresh build")
    parser.add_argument("--dry-run", action="store_true", help="build and report without writing")
    args = parser.parse_args(argv)
    try:
        document, warnings = build(args.facts, args.curation, args.research, args.decisions)
        lost = sorted(previous_ids(args.previous) - {row["id"] for row in document["problems"]})
        if lost:
            raise CatalogueError("These published ids would disappear: " + ", ".join(lost[:20]))
    except (CatalogueError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)[:500]}, ensure_ascii=False))
        return 1
    catalogue = encode_catalogue(document)
    overlay = json.dumps(empty_overlay(document), ensure_ascii=False, indent=2) + "\n"
    summary = {"ok": True, "total": document["total"], "counts": document["counts"],
               "category_counts": document["category_counts"], "content_sha256": document["content_sha256"],
               "bytes": len(catalogue.encode("utf-8")), "warnings": len(warnings)}
    if args.check:
        stale = [str(path.relative_to(ROOT)) for path in OUTPUTS if not path.exists() or path.read_text(encoding="utf-8") != catalogue]
        if not OVERLAY.exists() or OVERLAY.read_text(encoding="utf-8") != overlay:
            stale.append(str(OVERLAY.relative_to(ROOT)))
        summary.update(ok=not stale, stale=stale)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if not stale else 1
    if not args.dry_run:
        for path in OUTPUTS:
            write_atomic(path, catalogue)
        write_atomic(OVERLAY, overlay)
    for warning in warnings:
        print("warning: " + warning, file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
