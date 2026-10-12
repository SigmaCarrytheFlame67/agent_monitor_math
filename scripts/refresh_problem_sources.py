#!/usr/bin/env python3
"""Rebuild the committed problem-source fact files from pinned upstream inputs.

Every source can be read from a local copy (offline and reproducible) or, with
--fetch, from upstream: teorth's raw YAML at the latest main commit, the frozen
AI-contributions wiki, the Kourovka Notebook PDF from arXiv, openai/math raw
files, the Clay Millennium page and Wikipedia's API (article revision ids
only).  formal-conjectures is read from a local checkout at a pinned commit.

Each fact file is written atomically and only when its source parsed cleanly;
a parse that loses more than 2% of the previous records keeps the previous file
(override with --force after review).  Fact files contain numbers, states,
markers, pages, dates, names, links and revision ids only.

    python scripts/refresh_problem_sources.py --fetch --only erdos,openai-math
    python scripts/refresh_problem_sources.py --kourovka-pdf 1401.0300v48.pdf --kourovka-version 48
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_monitor import problem_sources as ps  # noqa: E402

OUTPUT = ROOT / "agent_monitor/data/problem_sources"
SOURCES = ("erdos", "ai-wiki", "kourovka", "formal-conjectures", "openai-math", "wikipedia", "clay")
FILES = {"erdos": "erdos.json", "ai-wiki": "erdos_ai_wiki.json", "kourovka": "kourovka.json",
         "formal-conjectures": "formal_conjectures.json", "openai-math": "openai_math.json",
         "wikipedia": "wikipedia.json", "clay": "clay.json"}
# The wiki was frozen on 2026-06-30 at this commit; a raw download with the same
# sha256 as the committed file keeps it.
AI_WIKI_FROZEN_COMMIT = "c8ad4309d20120c67cb97faa86daa1443acee018"
FC_DIRECTORIES = ("ErdosProblems", "Wikipedia", "Millennium", "HilbertProblems", "Kourovka")


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


def previous(args, source: str) -> dict | None:
    path = args.output_dir / FILES[source]
    return ps.load_json(path) if path.exists() and not args.force else None


def latest_commit(fetcher, atom_url: str) -> str:
    body = ps.fetch_ok(fetcher, atom_url, allowed_hosts=("github.com",), max_bytes=512 * 1024)
    return ps.parse_commits_atom(body)[0]["sha"]


def raw(fetcher, url: str, max_bytes: int = ps.MAX_BYTES) -> bytes:
    return ps.fetch_ok(fetcher, url, allowed_hosts=("raw.githubusercontent.com",), max_bytes=max_bytes)


def need(value, flag: str, args) -> None:
    if value is None and not args.fetch:
        raise ps.SourceError(f"pass {flag} or --fetch")


def build_erdos(args, fetcher) -> dict:
    need(args.erdos_yaml, "--erdos-yaml", args)
    if args.erdos_yaml:
        text = args.erdos_yaml.read_text(encoding="utf-8")
        if not args.erdos_commit:
            raise ps.SourceError("--erdos-yaml needs --erdos-commit")
        commit = args.erdos_commit
    else:
        commit = args.erdos_commit or latest_commit(fetcher, ps.TEORTH_COMMITS_ATOM)
        text = raw(fetcher, ps.TEORTH_RAW.format(commit=commit)).decode("utf-8")
    return ps.erdos_fact_file(text, commit=commit, fetched_at=args.fetched_at, previous=previous(args, "erdos"))


def build_ai_wiki(args, fetcher) -> dict:
    need(args.ai_wiki, "--ai-wiki", args)
    if args.ai_wiki:
        markdown = args.ai_wiki.read_text(encoding="utf-8")
    else:
        markdown = ps.fetch_ok(fetcher, ps.AI_WIKI_RAW, allowed_hosts=("raw.githubusercontent.com",)).decode("utf-8")
    commit = args.ai_wiki_commit
    if not commit:
        old = previous(args, "ai-wiki") or {}
        same = old.get("sha256") == ps.sha256_bytes(markdown.encode("utf-8"))
        commit = old.get("revision") if same else (AI_WIKI_FROZEN_COMMIT if args.ai_wiki else None)
    return ps.ai_wiki_fact_file(markdown, commit=commit, fetched_at=args.fetched_at)


def build_kourovka(args, fetcher) -> dict:
    need(args.kourovka_pdf, "--kourovka-pdf", args)
    version = args.kourovka_version
    if args.kourovka_pdf:
        data = args.kourovka_pdf.read_bytes()
        if version is None:
            raise ps.SourceError("--kourovka-pdf needs --kourovka-version")
    else:
        version = version or ps.kourovka_latest_version(fetcher)
        data = ps.fetch_ok(fetcher, ps.KOUROVKA_PDF.format(version=version), allowed_hosts=("arxiv.org",),
                           max_bytes=ps.MAX_BYTES, accept="application/pdf")
    return ps.kourovka_fact_file(ps.pdf_pages(data), pdf_sha256=ps.sha256_bytes(data), arxiv_version=version,
                                 fetched_at=args.fetched_at, edition=args.kourovka_edition,
                                 published=args.kourovka_published, previous=previous(args, "kourovka"))


def build_formal_conjectures(args, fetcher) -> dict:
    root = args.formal_conjectures_dir
    if root is None:
        raise ps.SourceError("pass --formal-conjectures-dir (a checkout at the pinned commit)")
    commit = args.formal_conjectures_commit
    committed_at = args.formal_conjectures_committed_at
    if not commit or not committed_at:
        def git(*argv: str) -> str:
            return subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        commit = commit or git("rev-parse", "HEAD")
        committed_at = committed_at or git("log", "-1", "--format=%cI", commit)
    files = {}
    for directory in FC_DIRECTORIES:
        for path in sorted((root / "FormalConjectures" / directory).glob("*.lean")):
            if path.is_symlink() or not path.is_file():
                continue
            files[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return ps.formal_conjectures_fact_file(files, commit=commit, committed_at=committed_at,
                                           fetched_at=args.fetched_at, previous=previous(args, "formal-conjectures"))


def build_openai_math(args, fetcher) -> dict:
    need(args.openai_contents, "--openai-contents", args)
    old = previous(args, "openai-math") or {}
    if args.openai_contents:
        if not args.openai_history or not args.openai_commit:
            raise ps.SourceError("--openai-contents needs --openai-history and --openai-commit")
        commit = args.openai_commit
        contents = args.openai_contents.read_text(encoding="utf-8")
        history = args.openai_history.read_text(encoding="utf-8")
    else:
        commit = args.openai_commit or latest_commit(fetcher, ps.OPENAI_COMMITS_ATOM)
        contents = raw(fetcher, ps.OPENAI_RAW.format(commit=commit, path="CONTENTS.md")).decode("utf-8")
        history = raw(fetcher, ps.OPENAI_RAW.format(commit=commit, path="history.md")).decode("utf-8")
    previous_commit = args.openai_previous_commit or (old.get("revision") if old.get("revision") != commit else old.get("previous_revision"))
    previous_contents = None
    if args.openai_previous_contents:
        previous_contents = args.openai_previous_contents.read_text(encoding="utf-8")
    elif previous_commit and args.fetch:
        previous_contents = raw(fetcher, ps.OPENAI_RAW.format(commit=previous_commit, path="CONTENTS.md")).decode("utf-8")
    mapping = json.loads(args.openai_mapping.read_text(encoding="utf-8")) if args.openai_mapping else old.get("mapping") or []
    return ps.openai_math_fact_file(contents, history, commit=commit, fetched_at=args.fetched_at,
                                    previous_contents_md=previous_contents, previous_commit=previous_commit,
                                    mapping=mapping, previous=old or None)


def build_wikipedia(args, fetcher) -> dict:
    if not args.wikipedia_unsolved or not args.wikipedia_solved:
        old = previous(args, "wikipedia")
        if old is None:
            raise ps.SourceError("pass --wikipedia-unsolved and --wikipedia-solved (the pinned list parse)")
        fact = old
    else:
        fact = ps.wikipedia_fact_file(json.loads(args.wikipedia_unsolved.read_text(encoding="utf-8")),
                                      json.loads(args.wikipedia_solved.read_text(encoding="utf-8")),
                                      fetched_at=args.fetched_at)
    if args.fetch and args.wikipedia_refresh_revids:
        current = ps.refresh_article_revids(fact, fetcher)
        for row in [*fact["entries"], *fact["solved"]]:
            if row.get("article_title") in current:
                row["article_revid"] = current[row["article_title"]]
        fact["articles_checked_at"] = args.fetched_at
    return fact


def build_clay(args, fetcher) -> dict:
    need(args.clay_html, "--clay-html", args)
    if args.clay_html:
        html = args.clay_html.read_text(encoding="utf-8")
    else:
        html = ps.fetch_ok(fetcher, ps.CLAY_PAGE, allowed_hosts=("www.claymath.org",), max_bytes=2 * 1024 * 1024).decode("utf-8")
    return ps.clay_fact_file(html, checked_at=args.clay_checked_at or args.fetched_at)


BUILDERS = {"erdos": build_erdos, "ai-wiki": build_ai_wiki, "kourovka": build_kourovka,
            "formal-conjectures": build_formal_conjectures, "openai-math": build_openai_math,
            "wikipedia": build_wikipedia, "clay": build_clay}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--only", default=",".join(SOURCES), help="comma-separated: " + ",".join(SOURCES))
    parser.add_argument("--fetch", action="store_true", help="read sources without a local input from upstream")
    parser.add_argument("--fetched-at", default=ps.timestamp(), help="UTC time recorded in the fact files")
    parser.add_argument("--force", action="store_true", help="ignore the previous files (no shrink check, no carry-over)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--erdos-yaml", type=Path)
    parser.add_argument("--erdos-commit")
    parser.add_argument("--ai-wiki", type=Path, help="AI-contributions wiki markdown")
    parser.add_argument("--ai-wiki-commit")
    parser.add_argument("--kourovka-pdf", type=Path)
    parser.add_argument("--kourovka-version", type=int)
    parser.add_argument("--kourovka-edition", default="No. 21 (2026)")
    parser.add_argument("--kourovka-published")
    parser.add_argument("--formal-conjectures-dir", type=Path)
    parser.add_argument("--formal-conjectures-commit")
    parser.add_argument("--formal-conjectures-committed-at")
    parser.add_argument("--openai-contents", type=Path)
    parser.add_argument("--openai-history", type=Path)
    parser.add_argument("--openai-commit")
    parser.add_argument("--openai-previous-contents", type=Path)
    parser.add_argument("--openai-previous-commit")
    parser.add_argument("--openai-mapping", type=Path, help="JSON list of candidate family-to-catalogue links")
    parser.add_argument("--wikipedia-unsolved", type=Path)
    parser.add_argument("--wikipedia-solved", type=Path)
    parser.add_argument("--wikipedia-refresh-revids", action="store_true")
    parser.add_argument("--clay-html", type=Path)
    parser.add_argument("--clay-checked-at")
    args = parser.parse_args(argv)
    args.only = [item.strip() for item in args.only.split(",") if item.strip()]
    unknown = sorted(set(args.only) - set(SOURCES))
    if unknown:
        parser.error("unknown source: " + ", ".join(unknown))
    return args


def main(argv=None, fetcher=None) -> int:
    args = parse_args(argv)
    fetcher = fetcher or ps.HTTPSFetcher()
    summary, failed = {}, False
    for source in args.only:
        try:
            fact = BUILDERS[source](args, fetcher)
        except (ps.SourceError, OSError, ValueError, subprocess.SubprocessError) as error:
            failed = True
            summary[source] = {"ok": False, "error": str(error)[:240]}
            continue
        summary[source] = {"ok": True, "file": FILES[source], "revision": fact.get("revision") or fact.get("arxiv_version")
                           or fact.get("revision_id") or fact.get("checked_at"), "counts": fact.get("counts")}
        if not args.dry_run:
            write_atomic(args.output_dir / FILES[source], ps.dump_json(fact))
    print(json.dumps({"ok": not failed, "dry_run": args.dry_run, "sources": summary}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
