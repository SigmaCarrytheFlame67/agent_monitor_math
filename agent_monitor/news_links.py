"""Deterministic problem-name candidates for news items.

The index is built at run time from the public problem catalogue in the
checkout (schema 1, 2 or 3).  It only proposes candidate problem ids for a
piece of source text -- "Erdős Problem #522", "Kourovka 12.20", "Hilbert's
16th problem", "Riemann hypothesis", names and aliases of catalogue rows.
Which candidates a story is really about, and how, is decided elsewhere
(Kimi K3 with a verbatim quote); nothing here changes a problem's status.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATHS = (ROOT / "docs/public/research/open-problems.json",
                   ROOT / "agent_monitor/data/open_problems.json")
MAX_CATALOGUE_BYTES = 64 * 1024 * 1024
PROBLEM_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,159}")
MAX_CANDIDATES = 8

ORDINALS = {name: index for index, name in enumerate((
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
    "eighteenth", "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third"), 1)}
_ORDINAL = "|".join(sorted((re.escape(name).replace(r"\ ", "[ -]") for name in ORDINALS), key=len, reverse=True))

# Common names that the catalogue titles do not spell out.  Used only for ids
# that exist in the catalogue being indexed.
EXTRA_ALIASES = {
    "clay-riemann-hypothesis": ("Riemann hypothesis",),
    "clay-p-vs-np": ("P versus NP", "P vs NP", "P vs. NP"),
    "clay-navier-stokes-equation": ("Navier-Stokes existence and smoothness", "Navier-Stokes problem",
                                    "Navier-Stokes Millennium Prize", "Navier-Stokes"),
    "clay-yang-mills-the-maths-gap": ("Yang-Mills existence and mass gap", "Yang-Mills mass gap"),
    "clay-birch-and-swinnerton-dyer-conjecture": ("Birch and Swinnerton-Dyer", "Birch-Swinnerton-Dyer",
                                                  "BSD conjecture"),
    "clay-hodge-conjecture": ("Hodge conjecture",),
    "clay-poincare-conjecture": ("Poincare conjecture",),
    "ramanujan-brocard": ("Brocard's problem", "Brocard-Ramanujan"),
    "ramanujan-lehmer": ("Lehmer's conjecture",),
}

_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "‐": "-", "‑": "-",
                         "‒": "-", "–": "-", "—": "-", "−": "-", " ": " "})


def fold(text: str) -> str:
    """Accent-free, case-folded text that keeps punctuation (for number patterns)."""
    text = unicodedata.normalize("NFKD", str(text or "").translate(_QUOTES))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def words(text: str) -> str:
    """Space-padded word sequence used for phrase matching ("Hilbert's" -> "hilberts")."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text).replace("'", "")).split()) + " "


@dataclass(frozen=True)
class Candidate:
    problem_id: str
    title: str


def _leading_number(value: object) -> int | None:
    match = re.match(r"\s*(\d{1,4})\b", str(value or ""))
    return int(match.group(1)) if match else None


def _name_part(title: str) -> str:
    # "Erdős #3 · divergent sums" -> "divergent sums"; "Smale 6: Finiteness ..." -> "Finiteness ..."
    for separator in (" · ", ": "):
        if separator in title:
            return title.split(separator, 1)[1]
    return title


class AliasIndex:
    def __init__(self, catalogue: dict, *, sha: str = "") -> None:
        if not isinstance(catalogue, dict) or catalogue.get("schema_version") not in (1, 2, 3):
            raise ValueError("Unsupported problem catalogue")
        rows = catalogue.get("problems")
        if not isinstance(rows, list):
            raise ValueError("Problem catalogue has no problems")
        self.schema_version = catalogue["schema_version"]
        self.sha256 = str(catalogue.get("content_sha256") or sha)[:64]
        self.snapshot_date = str(catalogue.get("snapshot_date") or "")[:32]
        self.titles: dict[str, str] = {}
        self.erdos: dict[int, str] = {}
        self.kourovka: dict[str, str] = {}
        self.numbered: dict[str, dict[int, list[str]]] = {"hilbert": {}, "smale": {}}
        phrases: dict[str, list[str]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not PROBLEM_ID.fullmatch(row["id"]):
                continue
            pid, source = row["id"], str(row.get("source") or "")
            title = " ".join(str(row.get("title") or pid).split())[:160]
            self.titles[pid] = title
            number = row.get("number")
            if source == "erdos" and str(number).strip().isdigit():
                self.erdos.setdefault(int(str(number).strip()), pid)
            elif source == "kourovka" and isinstance(number, str) and re.fullmatch(r"\d{1,2}\.\d{1,3}", number.strip()):
                self.kourovka.setdefault(number.strip(), pid)
            elif source in self.numbered and _leading_number(number) is not None:
                self.numbered[source].setdefault(_leading_number(number), []).append(pid)
            names = []
            if source not in ("erdos", "kourovka"):
                names.append(_name_part(title))
            elif source == "erdos" and " · " in title:
                names.append(_name_part(title))
            aliases = row.get("aliases")
            if isinstance(aliases, list):
                names.extend(alias for alias in aliases[:40] if isinstance(alias, str) and len(alias) <= 160)
            names.extend(EXTRA_ALIASES.get(pid, ()))
            for name in names:
                parts = words(name).split()
                if len(parts) > 2 and parts[0] in ("the", "a", "an"):
                    parts = parts[1:]
                # Single words and very short names would match unrelated text.
                if len(parts) >= 3 or (len(parts) == 2 and len(" ".join(parts)) >= 8):
                    ids = phrases.setdefault(" " + " ".join(parts) + " ", [])
                    if pid not in ids:
                        ids.append(pid)
        self.phrases = {phrase: tuple(ids) for phrase, ids in phrases.items()}
        self.by_first_word: dict[str, list[str]] = {}
        for phrase in self.phrases:
            self.by_first_word.setdefault(phrase.split()[0], []).append(phrase)

    def __len__(self) -> int:
        return len(self.titles)

    def __contains__(self, problem_id: object) -> bool:
        return problem_id in self.titles

    def title(self, problem_id: str) -> str:
        return self.titles.get(problem_id, "")

    def candidates(self, text: str, limit: int = MAX_CANDIDATES) -> list[Candidate]:
        folded = fold(str(text or "")[:20000])
        found: list[tuple[int, str]] = []

        def add(position: int, problem_id: str | None) -> None:
            if problem_id:
                found.append((position, problem_id))

        numbers = r"#?\s*(\d{1,4})((?:\s*(?:,|and|&|or)\s*#?\s*\d{1,4}\b)*)"
        for match in re.finditer(r"\berdos(?:'s)?\s+problems?\s*(?:no\.?\s*|number\s+)?" + numbers, folded):
            for value in [match.group(1), *re.findall(r"\d{1,4}", match.group(2))]:
                add(match.start(), self.erdos.get(int(value)))
        for pattern in (r"\berdos(?:'s)?\s*#\s*(\d{1,4})\b", r"\berdos_(\d{1,4})\b",
                        r"\berdosproblems/(\d{1,4})\b", r"\berdosproblems\.com/(?:forum/thread/)?(\d{1,4})\b"):
            for match in re.finditer(pattern, folded):
                add(match.start(), self.erdos.get(int(match.group(1))))
        kourovka = r"(\d{1,2}\.\d{1,3})(?!\d)"
        for match in re.finditer(r"\bkourovka(?:\s+notebook)?(?:\s*,)?\s+(?:problems?\s+)?" + kourovka, folded):
            add(match.start(), self.kourovka.get(match.group(1)))
        if "kourovka" in folded:
            # "Problems 19.12, 20.3 and 21.7" in a text that names the Notebook.
            listed = r"\bproblems?\s+" + kourovka + r"((?:\s*(?:,|and|&)\s*\d{1,2}\.\d{1,3}(?!\d))*)"
            for match in re.finditer(listed, folded):
                for value in [match.group(1), *re.findall(r"\d{1,2}\.\d{1,3}", match.group(2))]:
                    add(match.start(), self.kourovka.get(value))
        for source in ("hilbert", "smale"):
            for match in re.finditer(r"\b" + source + r"(?:'s)?\s+(?:(\d{1,2})(?:st|nd|rd|th)?|(" + _ORDINAL + r"))\s+problem\b"
                                     r"|\b" + source + r"(?:'s)?\s+problem\s+(?:no\.?\s*)?(\d{1,2})\b", folded):
                value = match.group(1) or match.group(3)
                index = int(value) if value else ORDINALS.get(match.group(2).replace("-", " "))
                for problem_id in self.numbered[source].get(index, ()):
                    add(match.start(), problem_id)
        if self.phrases:
            text_words = words(folded)
            for first in set(text_words.split()):
                for phrase in self.by_first_word.get(first, ()):
                    position = text_words.find(phrase)
                    if position >= 0:
                        for problem_id in self.phrases[phrase]:
                            add(position, problem_id)
        result, seen = [], set()
        for _, problem_id in sorted(found, key=lambda pair: pair[0]):
            if problem_id not in seen:
                seen.add(problem_id)
                result.append(Candidate(problem_id, self.titles.get(problem_id, problem_id)))
            if len(result) >= max(0, limit):
                break
        return result


def load_index(path: Path | None = None) -> AliasIndex:
    """Build the index from ``path`` or the first catalogue copy in the checkout."""
    paths = (path,) if path is not None else CATALOGUE_PATHS
    for candidate in paths:
        if candidate is None or not candidate.is_file():
            continue
        if candidate.stat().st_size > MAX_CATALOGUE_BYTES:
            raise ValueError("Problem catalogue exceeds size limit")
        raw = candidate.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("Problem catalogue is not valid JSON") from None
        return AliasIndex(data, sha=sha256(raw).hexdigest())
    raise ValueError("Problem catalogue not found")
