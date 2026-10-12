"""fact text — ABLATION variant of the fact store: a plain sequential text file.

Used only when ``CGM_FACT_TEXT_MODE`` is set (see ``cgm.gateway``): verified
results are appended to one human-readable markdown file,
``<project>/fact_text/facts.md``, numbered in submission order ("Fact 1",
"Fact 2", ...) the way a person keeps a notebook of established results.

Deliberately ABSENT relative to ``factgraph.py`` (this is the point of the
ablation): no content-addressed ids, no predecessor DAG, no per-fact files, no
glossary machinery, no cascade revocation. A retraction is just an appended
retraction note; a citation is an informal "Fact N" mention in prose.

The verifier gate is NOT part of this module and is unchanged: whether a claim
deserves to enter the store stays the verify service's call (the gate lives in
``fact submit``, which calls ``add`` only on accept).
"""

from __future__ import annotations

import fcntl
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import bm25
from ._util import utc_now

_FACT_HEAD_RE = re.compile(r"^## Fact (\d+)\b")
_RETRACT_HEAD_RE = re.compile(r"^## Retraction of Fact (\d+)\b")

_HEADER = (
    "# Verified results (plain-text store)\n"
    "\n"
    "Append-only notebook of verifier-accepted results, in submission order.\n"
    "A result is referred to as \"Fact N\". A retracted result stays in place\n"
    "with a retraction note appended later in the file; do not build on it.\n"
)


class FactText:
    """Rooted at the project directory; the only correctness source (text mode)."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "fact_text"
        self.path = self.dir / "facts.md"
        self._lock_path = self.dir / ".facts.lock"

    # ------------------------------------------------------------------ lock
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Cross-process exclusive lock: several worker gateways append
        concurrently and numbering must never collide."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ parse
    def _read(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def _entries(self, text: Optional[str] = None) -> List[Dict[str, object]]:
        """Parse facts.md into entries: ``{number, body}`` for each ``## Fact N``
        block (body = full block text incl. heading), plus the retracted set."""
        raw = self._read() if text is None else text
        entries: List[Dict[str, object]] = []
        retracted: set = set()
        cur_num: Optional[int] = None
        cur_lines: List[str] = []
        for line in raw.splitlines():
            m = _FACT_HEAD_RE.match(line)
            r = _RETRACT_HEAD_RE.match(line)
            if m or r:
                if cur_num is not None:
                    entries.append({"number": cur_num, "body": "\n".join(cur_lines)})
                    cur_num, cur_lines = None, []
                if m:
                    cur_num = int(m.group(1))
                    cur_lines = [line]
                else:
                    retracted.add(int(r.group(1)))
            elif cur_num is not None:
                cur_lines.append(line)
        if cur_num is not None:
            entries.append({"number": cur_num, "body": "\n".join(cur_lines)})
        for e in entries:
            e["retracted"] = e["number"] in retracted
        return entries

    @staticmethod
    def _statement_of(body: str) -> str:
        """The entry's ``### Statement`` section flattened to one line."""
        out: List[str] = []
        in_stmt = False
        for line in body.splitlines():
            if line.strip().startswith("### "):
                if in_stmt:
                    break
                in_stmt = line.strip().lower() == "### statement"
                continue
            if in_stmt:
                out.append(line.strip())
        return " ".join(s for s in out if s).strip()

    # ------------------------------------------------------------------ write
    def add(self, *, author: str, statement: str, proof: str, intuition: str = "") -> int:
        """Append one verified result; return its sequence number."""
        with self._locked():
            raw = self._read()
            nums = [int(m.group(1)) for m in
                    (_FACT_HEAD_RE.match(l) for l in raw.splitlines()) if m]
            n = (max(nums) + 1) if nums else 1
            parts = [
                f"## Fact {n}  (author: {author}, verified: {utc_now()})",
                "",
                "### Statement",
                statement.strip(),
                "",
                "### Proof",
                proof.strip(),
            ]
            if intuition.strip():
                parts += ["", "### Intuition", intuition.strip()]
            block = "\n".join(parts) + "\n\n"
            if not raw:
                raw = _HEADER + "\n"
            if not raw.endswith("\n\n"):
                raw = raw.rstrip("\n") + "\n\n"
            self.path.write_text(raw + block, encoding="utf-8")
            return n

    def retract(self, number: int, *, author: str, reason: str) -> List[int]:
        """Append a retraction note for ``number``. No cascade (there is no
        dependency structure to cascade over). Returns ``[number]``."""
        with self._locked():
            raw = self._read()
            if not any(int(m.group(1)) == number for m in
                       (_FACT_HEAD_RE.match(l) for l in raw.splitlines()) if m):
                raise ValueError(f"unknown fact number: {number}")
            block = (
                f"## Retraction of Fact {number}  (author: {author}, retracted: {utc_now()})\n"
                f"\nReason: {reason.strip()}\n\n"
            )
            if not raw.endswith("\n\n"):
                raw = raw.rstrip("\n") + "\n\n"
            self.path.write_text(raw + block, encoding="utf-8")
            return [number]

    # ------------------------------------------------------------------- read
    def count(self) -> int:
        """Number of non-retracted results."""
        return sum(1 for e in self._entries() if not e["retracted"])

    def search(self, query: str, limit: int = 10) -> List[Dict[str, object]]:
        """BM25 over the non-retracted entries (statement + proof + intuition),
        rebuilt on demand from ``facts.md``. Returns the top matches as
        ``{fact_number, score, statement}``."""
        entries = [e for e in self._entries() if not e["retracted"]]
        if not entries:
            return []
        docs = [bm25.tokenize(str(e["body"])) for e in entries]
        scores = bm25.bm25_scores(query, docs)
        ranked: List[Dict[str, object]] = []
        for e, score in sorted(zip(entries, scores), key=lambda t: -t[1]):
            if score <= 0:
                break
            ranked.append({
                "fact_number": e["number"],
                "score": score,
                "statement": self._statement_of(str(e["body"])),
            })
            if len(ranked) >= limit:
                break
        return ranked
