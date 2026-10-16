"""Deterministic statement DAG of a LaTeX manuscript (pylatexenc front end).

Ported from the math-kg-tools evaluation prototypes (``pyfallback.py`` +
``kg_to_dag.py``), whose statement/edge vocabulary is a design adaptation of
LLNL math-kg-tools (``latex-tree-dump`` / ``latex-kg``, Apache-2.0; see
``ATTRIBUTION.md``).  Nothing from that project is bundled or executed; the
front end is pylatexenc (MIT).

What it does, for a bundle folder of TeX sources:

* finds the root file (``\\documentclass`` + ``\\begin{document}``) and expands
  ``\\input`` / ``\\include`` / ``\\subfile`` / ``\\import`` in document order,
  confined to the bundle (a path that leaves it is refused before any access);
* blanks ``comment`` / verbatim-like environments and ``\\iffalse`` blocks,
  then walks the pylatexenc node tree (nested environments, labels inside
  ``\\[...\\]``, refs inside macro arguments);
* theorem-like environments become statements, with kinds resolved through
  ``\\newtheorem`` / ``\\declaretheorem`` / ``\\spnewtheorem`` display names;
* proofs attach by a ``\\ref`` in their title ("Proof of Theorem~\\ref{x}"),
  else to the nearest preceding provable statement; prose proofs ("We now
  prove Theorem~\\ref{x} ... This completes the proof of Theorem~\\ref{x}") are
  recognized too;
* labels belong to the statement (or its proof) they sit in; ``\\ref``,
  ``\\cref``, ``\\eqref``, ``\\autoref``, ``\\hyperref[...]`` ... between
  statements become ``uses`` edges with a strength, cycles are broken by
  dropping the weakest edge, and a weak document-order edge is added for a
  proved statement that cites nothing;
* statement numbers reproduce section-reset and shared counters,
  ``\\numberwithin``, ``aliascnt`` and appendix letters (they matched the
  compiled PDFs on 1,344/1,344 statements of the evaluation corpus);
* main results, cited external results, bibliography titles, the title,
  abstract and introduction, and author macros (for KaTeX) are extracted.

Nothing is compiled or executed.  The runner calls :func:`run_sandboxed`,
which parses in an isolated child (``python -I``, an empty working directory,
an explicit import path, no credentials) under ``RLIMIT_AS`` 1 GiB and 60 s
of CPU; a stopped run kills it at once.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "ansatze-analyze-segments-v1"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PROJECT_BYTES = 16 * 1024 * 1024
MAX_FILES = 400
MAX_INCLUDE_DEPTH = 8
MAX_NODES = 256
MAX_SCANNED_STATEMENTS = 4000
MAX_CYCLE_BREAKS = 500
MAX_STATEMENT_CHARS = 64_000
MAX_PROOF_CHARS = 512_000
# The running text a verifier sees before each result (its setup and notation).
MAX_CONTEXT_CHARS = 6_000
MAX_INTRO_CHARS = 24_000
MAX_ABSTRACT_CHARS = 4_000
MAX_BIB_ENTRY_CHARS = 300
SANDBOX_CPU_SECONDS = 60
SANDBOX_MEMORY_BYTES = 1024 * 1024 * 1024
SANDBOX_WALL_SECONDS = 90

CANON = ["theorem", "lemma", "proposition", "corollary", "definition", "conjecture",
         "claim", "remark", "example", "assumption", "hypothesis", "fact",
         "observation", "question", "problem", "axiom", "notation", "exercise",
         "construction", "condition", "principle", "convention", "setting", "step"]
PROVABLE = frozenset({"theorem", "lemma", "proposition", "corollary", "claim", "fact", "observation", "step"})
ALIASES = {
    "thm": "theorem", "theo": "theorem", "mainthm": "theorem", "maintheorem": "theorem",
    "theorema": "theorem", "lem": "lemma", "lemm": "lemma", "sublemma": "lemma",
    "keylemma": "lemma", "prop": "proposition", "pro": "proposition", "propn": "proposition",
    "cor": "corollary", "coro": "corollary", "corr": "corollary", "defn": "definition",
    "def": "definition", "dfn": "definition", "df": "definition", "conj": "conjecture",
    "clm": "claim", "rem": "remark", "rmk": "remark", "ex": "example", "exa": "example",
    "eg": "example", "obs": "observation", "ass": "assumption", "assm": "assumption",
    "hyp": "hypothesis", "ques": "question", "qn": "question", "prob": "problem",
    "ax": "axiom", "nota": "notation", "exer": "exercise", "exo": "exercise",
}
MATH_ENVS = frozenset({"equation", "align", "gather", "multline", "eqnarray", "split", "aligned",
                       "gathered", "cases", "array", "matrix", "pmatrix", "bmatrix", "subequations",
                       "flalign", "alignat", "dmath"})
SECTION_LEVELS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2,
                  "subsubsection": 3, "paragraph": 4, "subparagraph": 5}
REF_MACROS = ("ref", "eqref", "cref", "Cref", "autoref", "pageref", "vref", "Vref",
              "labelcref", "nameref", "crefrange", "Crefrange")
CITE_MACROS = ("cite", "citep", "citet", "Cite", "citealp", "citeauthor", "parencite",
               "textcite", "autocite")
SKIP_ENVS = frozenset({"comment", "verbatim", "verbatim*", "lstlisting", "minted", "Verbatim"})
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,79}$")

REF_RE = re.compile(r"\\(?:ref|cref|Cref|autoref|eqref|labelcref|vref|Vref|nameref|hyperref)\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
NEWTHM_RE = re.compile(r"\\newtheorem(\*?)\s*\{([^}]+)\}\s*(?:\[([^\]]+)\])?\s*\{([^}]+)\}\s*(?:\[([^\]]+)\])?")
DECLTHM_RE = re.compile(r"\\declaretheorem\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")
SPNEWTHM_RE = re.compile(r"\\spnewtheorem(\*?)\s*\{([^}]+)\}\s*(?:\[([^\]]+)\])?\s*\{([^}]+)\}")
INCLUDE_RE = re.compile(r"\\(input|include|subfile|subfileinclude)\s*\{([^}]*)\}")
BARE_INPUT_RE = re.compile(r"\\input\s+([A-Za-z0-9_./-]+)")
IMPORT_RE = re.compile(r"\\(import|subimport|inputfrom|subinputfrom)\s*\{([^}]*)\}\s*\{([^}]*)\}")
IFFALSE_RE = re.compile(r"\\iffalse\b.*?\\fi\b", re.S)
VERBATIM_RE = re.compile(
    r"\\begin\s*\{(comment|verbatim\*?|Verbatim|lstlisting|minted)\}.*?\\end\s*\{\1\}", re.S)
COMMENT_LINE_RE = re.compile(r"(?m)(?<!\\)%.*$")
ROOT_NAMES = ("main", "paper", "ms", "article", "manuscript")
RESTATABLE_BEGIN_RE = re.compile(r"\\begin\s*\{restatable\*?\}")
# Numbered display math (each counts once; the align family once per row).
NUMBERED_MATH = frozenset({"equation", "multline", "dmath", "subequations"})
NUMBERED_ROWS = frozenset({"align", "gather", "flalign", "alignat", "eqnarray", "xalignat", "xxalignat"})
COUNTER_WITHIN_RE = re.compile(r"\\(numberwithin|counterwithin)(\*?)\s*\{([^}]+)\}\s*\{([^}]+)\}")
THE_COUNTER_RE = re.compile(r"\\(?:renewcommand\*?|def)\s*\{?\s*\\the([A-Za-z]+)\s*\}?\s*")

# Prose proofs.  A marker is read from the text just before a \ref to a
# provable statement (lower-cased, whitespace collapsed).  A start opens a
# proof that runs to an end marker for the same result or to the next heading
# at its own level; an end marker with no open start closes a proof that began
# with its section (or after the last theorem or proof environment there).
# Roadmaps ("Section 4 proves Theorem 1", "We prove Theorem 1 below") and
# introductions are not proofs.
_PROSE_PART = (
    r"(?:(?:the|a|an|our|its|this|each|every|both|all)\s+)?(?:[a-z][\w-]*\s+){0,2}"
    r"(?:part|parts|half|halves|implication|implications|direction|directions|assertion|assertions|case|cases|"
    r"claim|claims|statement|statements|conclusion|conclusions|bound|bounds|estimate|estimates|inequality|"
    r"inequalities|identity|identities|equality|equivalence|item|items)\s+(?:of|in)\s+"
)
_PROSE_KIND = r"(?:(?:the|our)\s+)?(?:main\s+)?(?:theorem|lemma|proposition|corollary|claim|result|fact|observation)s?"
_PROSE_TARGET = rf"(?:{_PROSE_PART})?(?:{_PROSE_KIND})?\s*~?\s*$"
_START_VERB = (
    r"(?:prove|establish|show|deduce|derive|"
    r"(?:complete|finish|begin|start|give|present)\s+(?:the\s+|our\s+)?proofs?\s+of|"
    r"turn\s+to\s+(?:the\s+)?proofs?\s+of|proceed\s+(?:to|with)\s+(?:the\s+)?proofs?\s+of)"
)
PROSE_START_RE = re.compile(
    r"(?:"
    r"(?:^\s*|[.;:!?]\s|\b(?:now|here|next|then|finally|first|below|section|subsection|part|chapter|appendix|"
    r"paragraph)\s*,?\s)(?:we|let\s+us)\s+(?:(?:now|first|next|then|finally|will|shall|can|may|must)\s+){0,3}"
    + _START_VERB + r"\s+"
    r"|(?:^\s*|[.;:!?]\s)(?:in\s+order\s+)?to\s+(?:prove|establish|show|deduce|derive|(?:complete|finish)\s+the\s+"
    r"proofs?\s+of)\s+"
    r"|\b(?:it\s+)?(?:remains|suffices)\s+to\s+(?:prove|establish|show|verify)\s+"
    r"|(?:^\s*|[.;:!?]\s)(?:the|our)\s+(?:remaining\s+|final\s+|full\s+)?proofs?\s+of\s+"
    r"|\b(?:goal|aim|purpose|task)\s+(?:of|in)\s+this\s+(?:section|subsection|part|chapter|appendix)\s+is\s+to\s+"
    r"(?:prove|establish|show)\s+"
    r"|\b(?:this|the\s+(?:rest|remainder)\s+of\s+this)\s+(?:section|subsection|part|chapter|appendix)\s+"
    r"(?:is\s+devoted\s+to\s+(?:the\s+proofs?\s+of|proving)|proves|establishes|contains\s+the\s+proofs?\s+of|"
    r"gives\s+the\s+proofs?\s+of)\s+"
    r")" + _PROSE_TARGET
)
PROSE_END_RE = re.compile(
    r"(?:"
    r"\b(?:completes|completed|completing|finishes|finished|finishing|concludes|concluded|concluding|ends|ended)\s+"
    r"(?:(?:the|our|this)\s+)?(?:proofs?|arguments?|deduction|derivation)\s+(?:of|for)\s+"
    r"|\b(?:proves|establishes|completes|proving|establishing|completing)\s+"
    r"|\b(?:have|has)\s+(?:now\s+|thus\s+|therefore\s+|hence\s+)?(?:proved|proven|established|shown|"
    r"completed\s+the\s+proofs?\s+of)\s+"
    # A plural subject: "Propositions 3 and 4 prove Theorem~", "... complete Theorem~".
    r"|(?<!\bwe\s)(?<!\bto\s)(?<!\bus\s)(?<!\bthey\s)(?<!\bwill\s)(?<!\bshall\s)(?<!\bcan\s)(?<!\bmay\s)"
    r"(?<!\bmust\s)(?<!\bwould\s)(?<!\bcould\s)(?<!\bshould\s)\b(?:prove|establish|complete)\s+"
    r")" + _PROSE_TARGET
)
# "We will combine ... and so conclude the proof of Theorem~" announces a proof.
FUTURE_RE = re.compile(r"(?i)\b(?:will|shall|going\s+to)\b")
# "..., this proves Proposition~": the result just before the sentence is used.
ANAPHORA_RE = re.compile(r"(?i)\b(?:this|these|which|it|they)\b")
BOUNDARY_MACROS = frozenset({"bibliography", "printbibliography", "bibliographystyle"})
# Commands whose arguments are labels or citation keys, never manuscript text.
KEY_MACROS = frozenset({"label", *REF_MACROS, *CITE_MACROS})
# Commands whose arguments are file names, never manuscript text.
FILE_MACROS = frozenset({"input", "include", "subfile", "subfileinclude", "import", "subimport", "inputfrom",
                         "subinputfrom", "includegraphics", "bibliography", "bibliographystyle", "addbibresource"})
# "Proof of Theorem~\ref{x}." at the start of a paragraph (emphasized or not).
PROSE_LEAD_RE = re.compile(
    r"^\s*(?:Proofs?|Completion\s+of\s+the\s+proofs?|Conclusion\s+of\s+the\s+proofs?)\s+of\s+"
    r"(?i:" + _PROSE_TARGET + r")"
)
# A heading that announces a proof ("Proof of Theorem~\ref{x}", "Choice of
# parameters and proof of Theorem~\ref{x}").
TITLE_PROOF_RE = re.compile(r"(?i)\bproofs?\s+of\b[^.;:]{0,60}$")
ROADMAP_TITLE_RE = re.compile(
    r"(?i)\b(?:introduction|overview|outline|organi[sz]ation|summary|strategy|road\s*map|motivation|background|"
    r"preliminar(?:y|ies)|main\s+results?|statements?\s+of\s+(?:the\s+)?(?:main\s+)?results?|our\s+results?|"
    r"structure\s+of\s+the\s+paper|notation)\b"
)
# Another place in the paper named in the same sentence: a roadmap.
SECTION_MENTION_RE = re.compile(
    r"(?i)(?:\b(?:sections?|subsections?|appendix|appendices|chapters?)\s*~?\s*(?:\d|[A-Z](?![a-z]))|§"
    r"|\b(?:next|following|last|final|second|third|fourth|fifth|later|subsequent|remaining|other|previous)\s+"
    r"(?:sections?|subsections?|chapters?|parts?|appendix)\b)"
)
SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
LATER_RE = re.compile(r"(?i)\b(?:below|later|subsequently|further\s+on|deferred|postponed)\b")
# "The proof of Theorem~\ref{a} and thus Theorem~\ref{b} follows": a result
# joined to the one a proof opens with is proved there too.
CONJOINED_RE = re.compile(
    r"^\s*(?:,\s*)?(?:(?:and|as\s+well\s+as|together\s+with)\s+)?(?:(?:thus|hence|so|therefore|consequently|"
    r"then|also)\s+)?(?:" + _PROSE_KIND + r")?\s*~?\s*$")
# Proof titles that name a result by number ("Proof of Theorem 1.1").
TITLE_NUMBER_RE = re.compile(
    r"(?i)\b(?P<word>theorem|lemma|proposition|corollary|claim|fact|observation|step|thm|lem|prop|cor)s?\.?"
    r"\s*~?\s*(?P<number>(?:[A-Z]\.)?\d+(?:\.\d+)*[a-z]?|[A-Z](?![a-z]))"
)
TITLE_MAIN_RE = re.compile(r"(?i)\bproofs?\s+of\s+(?:the\s+)?main\s+(?:theorem|result)s?\b")


class LatexStructureError(ValueError):
    """The bundle has no usable LaTeX structure (public-safe message)."""


class ParserUnavailable(LatexStructureError):
    """A parser package (pylatexenc or pypdf) is not installed: a server
    configuration problem, not a property of the manuscript."""


class SandboxCancelled(Exception):
    """The run was stopped while a sandboxed parser was working."""


# ---------------------------------------------------------------------------
# reading and include order
# ---------------------------------------------------------------------------


def read_text(path: Path) -> tuple[str, bool]:
    """Text of a TeX file (UTF-8, else Latin-1) with LF line ends; and whether
    the Latin-1 fallback was used."""
    raw = path.read_bytes()
    try:
        text, fallback = raw.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        text, fallback = raw.decode("latin-1"), True
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", ""), fallback


def strip_comments(source: str) -> str:
    """Drop TeX ``%`` comments (not ``\\%``) line by line, keeping lines."""
    out = []
    for line in source.split("\n"):
        index = 0
        cut = None
        while index < len(line):
            char = line[index]
            if char == "\\":
                index += 2
                continue
            if char == "%":
                cut = index
                break
            index += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return True


def find_root(project: Path, files: Iterable[Path] | None = None) -> Path | None:
    """The root ``.tex`` file of a bundle.

    A file with ``\\documentclass`` and ``\\begin{document}``, preferring
    shallow paths, the usual names and larger files.  Without one (a pasted
    fragment), the largest ``.tex`` file that no other file includes.  Raises
    :class:`LatexStructureError` when the only TeX files exceed the size cap.
    """
    project = project.resolve()
    tex = sorted(path for path in (files or project.rglob("*.tex")) if path.is_file() and not path.is_symlink())
    candidates = []
    texts: dict[Path, str] = {}
    oversize: list[Path] = []
    for path in tex:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                oversize.append(path)
                continue
            text = strip_comments(read_text(path)[0])
        except OSError:
            continue
        texts[path] = text
        if re.search(r"\\documentclass", text) and re.search(r"\\begin\s*\{document\}", text):
            score = (len(path.relative_to(project).parts),
                     0 if path.stem in ROOT_NAMES else 1,
                     -path.stat().st_size)
            candidates.append((score, path))
    if candidates:
        candidates.sort()
        return candidates[0][1]
    if oversize:
        # The real root is most likely the file that could not be read.
        largest = max(oversize, key=lambda path: (path.stat().st_size, str(path)))
        raise LatexStructureError(
            f"{largest.relative_to(project).as_posix()[:120]} exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MiB "
            "limit of the LaTeX parser")
    included: set[str] = set()
    for text in texts.values():
        for match in INCLUDE_RE.finditer(text):
            included.add(Path(match.group(2).strip()).stem)
    loose = [path for path in texts if path.stem not in included]
    if not loose:
        loose = list(texts)
    if not loose:
        return None
    return max(loose, key=lambda path: (path.stat().st_size, str(path)))


def resolve_includes(root: Path, project: Path):
    """Depth-first include expansion in source order, confined to ``project``.

    LaTeX resolves ``\\input`` paths relative to the root's directory (the
    compile directory); ``\\import{dir}{file}`` switches directory.  Returns
    ``(order, missing, total_bytes, inserted_at)`` where ``inserted_at`` maps
    an included file to ``(parent, row, column)`` of the command that pulled
    it in.  Containment is checked on the resolved path before any stat or
    read, so absolute paths, ``..`` and links that leave the bundle are
    refused without probing.
    """
    order: list[Path] = []
    missing: list[str] = []
    seen: set[Path] = set()
    total = 0
    inserted_at: dict[Path, tuple[Path, int, int]] = {}
    project_root = project.resolve()

    def candidates(base: Path, name: str) -> list[Path]:
        name = name.strip()
        if not name:
            return []
        path = base / name
        return [path] if path.suffix == ".tex" else [path.with_name(path.name + ".tex"), path]

    def visit(path: Path, base: Path, depth: int, at=None) -> None:
        nonlocal total
        if depth > MAX_INCLUDE_DEPTH or len(order) >= MAX_FILES:
            missing.append(f"{path.name} (include depth or file cap)")
            return
        resolved = path.resolve()
        if not _inside(resolved, project_root) or path.is_symlink():
            missing.append(f"{path.name} (outside the bundle, refused)")
            return
        if at is not None and resolved not in seen:
            inserted_at[resolved] = at
        if resolved in seen:
            return
        seen.add(resolved)
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_PROJECT_BYTES:
            missing.append(f"{path.name} (size cap)")
            return
        total += size
        order.append(resolved)
        source = strip_comments(read_text(resolved)[0])
        events = []
        for match in INCLUDE_RE.finditer(source):
            events.append((match.start(), "inc", match.group(2), None))
        for match in BARE_INPUT_RE.finditer(source):
            events.append((match.start(), "inc", match.group(1), None))
        for match in IMPORT_RE.finditer(source):
            events.append((match.start(), "imp", match.group(3), match.group(2)))
        for offset, kind, name, folder in sorted(events, key=lambda event: event[0]):
            row = source.count("\n", 0, offset)
            column = offset - (source.rfind("\n", 0, offset) + 1)
            directory = base if kind == "inc" else (resolved.parent / folder)
            for candidate in candidates(directory, name):
                if not _inside(candidate, project_root):
                    missing.append(f"{Path(name).name} (outside the bundle, refused)")
                    break
                if candidate.is_file():
                    visit(candidate, base if kind == "inc" else candidate.parent, depth + 1,
                          at=(resolved, row, column))
                    break
            else:
                missing.append(name[:120])

    visit(root, root.parent, 0)
    return order, missing, total, inserted_at


# ---------------------------------------------------------------------------
# entity extraction (pylatexenc)
# ---------------------------------------------------------------------------


def blank_skipped(text: str) -> str:
    """Blank ``\\iffalse...\\fi`` blocks and verbatim-like environments,
    preserving offsets and newlines (pylatexenc would otherwise parse their
    bodies, and an unbalanced ``\\begin`` there swallows the document)."""
    def blank(match: re.Match) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return VERBATIM_RE.sub(blank, IFFALSE_RE.sub(blank, text))


_CONTEXT: list[Any] = []


def _context():
    if _CONTEXT:
        return _CONTEXT[0]
    from pylatexenc import latexwalker, macrospec

    database = latexwalker.get_default_latex_context_db()
    database.add_context_category(
        "ansatze-refs",
        prepend=True,
        macros=[macrospec.MacroSpec(name, "*{") for name in REF_MACROS if name not in ("crefrange", "Crefrange")]
        + [macrospec.MacroSpec(name, "*{{") for name in ("crefrange", "Crefrange")]
        + [macrospec.MacroSpec("hyperref", "[{")]
        + [macrospec.MacroSpec(name, "*[[{") for name in CITE_MACROS]
        + [macrospec.MacroSpec("label", "{")]
        + [macrospec.MacroSpec(name, "{") for name in ("input", "include", "subfile", "subfileinclude",
                                                       "bibliography", "bibliographystyle")]
        + [macrospec.MacroSpec(name, "*{{") for name in ("import", "subimport", "inputfrom", "subinputfrom")]
        + [macrospec.MacroSpec(name, "*[{") for name in SECTION_LEVELS]
        + [macrospec.MacroSpec("newtheorem", "*{[{[")],
    )
    _CONTEXT.append(database)
    return database


def _arg_text(node: Any, index: int) -> str | None:
    try:
        argument = node.nodeargd.argnlist[index]
    except (AttributeError, IndexError, TypeError):
        return None
    if argument is None:
        return None
    text = argument.latex_verbatim()
    if text[:1] in "{[" and text[-1:] in "}]":
        text = text[1:-1]
    return text


class _Emitter:
    def __init__(self, ns: str, text: str, restate_macros: frozenset[str] = frozenset()) -> None:
        self.ns = ns
        self.text = text
        self.restate_macros = restate_macros
        self.count = 0
        self.entities: list[dict[str, Any]] = []
        self.line_starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                self.line_starts.append(index + 1)

    def row(self, position: int) -> int:
        return bisect.bisect_right(self.line_starts, position) - 1

    def point(self, position: int) -> tuple[int, int]:
        row = self.row(position)
        return row, position - self.line_starts[row]

    def add(self, kind: str, start: int, end: int, **fields: Any) -> dict[str, Any]:
        self.count += 1
        entity = {"id": f"{self.ns}:F{self.count:05d}", "kind": kind, "ns": self.ns, "_s": start, "_e": end}
        entity.update({key: value for key, value in fields.items() if value is not None})
        self.entities.append(entity)
        return entity


def _walk(nodes: Any, emitter: _Emitter) -> None:
    from pylatexenc import latexwalker

    for node in nodes or []:
        if node is None:
            continue
        if node.isNodeType(latexwalker.LatexEnvironmentNode):
            if node.environmentname in SKIP_ENVS:
                continue
            title = None
            try:
                for argument in node.nodeargd.argnlist or []:
                    if argument is not None:
                        value = argument.latex_verbatim()
                        if value.startswith("["):
                            title = value[1:-1]
                            break
            except AttributeError:
                pass
            if title is None:
                after = emitter.text[node.pos + len("\\begin{" + node.environmentname + "}"): node.pos + 400]
                stripped = after.lstrip()
                if stripped.startswith("["):
                    depth, index = 0, 0
                    for index, char in enumerate(stripped):
                        depth += char == "["
                        depth -= char == "]"
                        if depth == 0:
                            break
                    title = stripped[1:index]
            emitter.add("environment", node.pos, node.pos + node.len, subkind=node.environmentname, title=title)
            _walk(node.nodelist, emitter)
        elif node.isNodeType(latexwalker.LatexMacroNode):
            name = node.macroname
            if name == "label":
                emitter.add("label_def", node.pos, node.pos + node.len, content=(_arg_text(node, 0) or "").strip())
            elif name in REF_MACROS:
                labels = [_arg_text(node, index) for index in (1, 2)]
                emitter.add("label_ref", node.pos, node.pos + node.len,
                            content=",".join(label.strip() for label in labels if label))
            elif name == "hyperref":
                label = (_arg_text(node, 0) or "").strip()
                if label:
                    emitter.add("label_ref", node.pos, node.pos + node.len, content=label)
            elif name in CITE_MACROS:
                note = (_arg_text(node, 2) or _arg_text(node, 1) or "").strip()
                emitter.add("citation", node.pos, node.pos + node.len,
                            content=(_arg_text(node, 3) or "").strip(), note=note or None)
            elif name in SECTION_LEVELS:
                emitter.add("section", node.pos, node.pos + node.len, subkind=name,
                            title=_arg_text(node, 2), star=bool(_arg_text(node, 0)))
            elif name in emitter.restate_macros:
                # \mainthm* restates a thmtools restatable result.
                emitter.add("restate", node.pos, node.pos + node.len, content=name)
            elif name in BOUNDARY_MACROS:
                emitter.add("boundary", node.pos, node.pos + node.len)
            if name in FILE_MACROS or name in KEY_MACROS:
                continue  # file names, labels and citation keys are not text
            try:
                arguments = list(node.nodeargd.argnlist or [])
                if name == "hyperref":
                    arguments = arguments[1:]  # [label]{link text}
                for argument in arguments:
                    if argument is not None and hasattr(argument, "nodelist"):
                        _walk(argument.nodelist, emitter)
            except AttributeError:
                pass
        elif node.isNodeType(latexwalker.LatexGroupNode):
            _walk(node.nodelist, emitter)
        elif node.isNodeType(latexwalker.LatexMathNode):
            emitter.add("display_math" if node.displaytype == "display" else "inline_math",
                        node.pos, node.pos + node.len)
            _walk(node.nodelist, emitter)
        elif node.isNodeType(latexwalker.LatexCharsNode):
            if node.chars.strip():
                emitter.add("text", node.pos, node.pos + node.len, content=node.chars)


def _assign_parents(emitter: _Emitter, doc_end: int) -> None:
    """Section extents (to the next section of the same or a higher level, or
    the end of an enclosing environment), then each entity's innermost
    enclosing section/environment/math entity."""
    entities = emitter.entities
    sections = [entity for entity in entities if entity["kind"] == "section"]
    # A sectioning command inside an environment (\paragraph in a proof) ends
    # with the innermost enclosing environment.  Environments nest, so one
    # sweep with a stack of open environments finds it.
    events = sorted(
        [(entity["_s"], 0, -entity["_e"], index, entity)
         for index, entity in enumerate(entities) if entity["kind"] == "environment"]
        + [(entity["_s"], 1, 0, index, entity) for index, entity in enumerate(sections)],
        key=lambda event: event[:4],
    )
    enclosing_end: dict[str, int] = {}
    open_envs: list[dict[str, Any]] = []
    for position, kind, _order, _index, entity in events:
        while open_envs and open_envs[-1]["_e"] <= position:
            open_envs.pop()
        if kind == 0:
            open_envs.append(entity)
        elif open_envs and open_envs[-1]["_s"] < position:
            enclosing_end[entity["id"]] = open_envs[-1]["_e"]
    for index, section in enumerate(sections):
        level = SECTION_LEVELS.get(section["subkind"], 9)
        end = doc_end
        for later in sections[index + 1:]:
            if SECTION_LEVELS.get(later["subkind"], 9) <= level:
                end = later["_s"]
                break
        if section["id"] in enclosing_end:
            end = min(end, enclosing_end[section["id"]])
        section["_head_e"] = section["_e"]  # the end of the heading command (its title)
        section["_e"] = end
    ordered = sorted(entities, key=lambda entity: (entity["_s"], -entity["_e"]))
    stack: list[dict[str, Any]] = []
    for entity in ordered:
        while stack and not (stack[-1]["_s"] <= entity["_s"] and entity["_e"] <= stack[-1]["_e"]):
            stack.pop()
        entity["parent"] = stack[-1]["id"] if stack else None
        if entity["kind"] in ("section", "environment", "display_math", "inline_math"):
            stack.append(entity)


def extract_entities(ns: str, text: str, *, restate_macros: Iterable[str] = ()) -> tuple[list[dict[str, Any]], str | None]:
    """KG-shaped entities of one file (``environment``, ``section``,
    ``label_def``, ``label_ref``, ``citation``, ``text``, math, ``restate``),
    with char offsets ``_s``/``_e`` into ``text``; and a parse error, if any."""
    from pylatexenc import latexwalker

    emitter = _Emitter(ns, text, frozenset(restate_macros))
    error = None
    try:
        walker = latexwalker.LatexWalker(blank_skipped(text), latex_context=_context(), tolerant_parsing=True)
        nodes, _, _ = walker.get_latex_nodes(pos=0)
        _walk(nodes, emitter)
    except RecursionError:
        error = "nesting too deep to parse"
    except Exception as exc:  # noqa: BLE001 - tolerant parser; keep what was found
        error = f"{type(exc).__name__}"[:120]
    _assign_parents(emitter, len(text))
    return emitter.entities, error


# ---------------------------------------------------------------------------
# theorem declarations, macros, bibliography
# ---------------------------------------------------------------------------


def canon_kind(env: str, display: str | None) -> str | None:
    base = env.rstrip("*").lower()
    if display:
        lowered = display.lower()
        for kind in sorted(CANON, key=len, reverse=True):
            if kind in lowered:
                return kind
    if base in CANON:
        return base
    return ALIASES.get(base)


def parse_theorem_decls(texts: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``env -> {display, counter, within, numbered}``; ``\\newaliascnt``
    counters resolve to the aliased counter."""
    declarations: dict[str, dict[str, Any]] = {}
    alias: dict[str, str] = {}
    for text in texts:
        text = COMMENT_LINE_RE.sub("", text)
        for first, second in re.findall(r"\\newaliascnt\s*\{([^}]+)\}\s*\{([^}]+)\}", text):
            alias[first.strip()] = second.strip()
        for star, env, shared, display, within in NEWTHM_RE.findall(text):
            declarations[env] = {"display": display.strip(), "counter": shared or env,
                                 "within": within or None, "numbered": star != "*"}
        for star, env, shared, display in SPNEWTHM_RE.findall(text):
            declarations[env] = {"display": display.strip(), "counter": shared or env,
                                 "within": None, "numbered": star != "*"}
        for options, envs in DECLTHM_RE.findall(text):
            parsed = dict(re.findall(r"(\w+)\s*=\s*\{?([^,}]+)\}?", options or ""))
            for env in envs.split(","):
                env = env.strip()
                declarations[env] = {"display": parsed.get("name", env.capitalize()),
                                     "counter": parsed.get("sibling") or parsed.get("sharenumber") or env,
                                     "within": parsed.get("numberwithin") or parsed.get("parent"),
                                     "numbered": parsed.get("numbered", "yes") != "no"}
    for declaration in declarations.values():
        seen: set[str] = set()
        while declaration["counter"] in alias and declaration["counter"] not in seen:
            seen.add(declaration["counter"])
            declaration["counter"] = alias[declaration["counter"]]
    return declarations


def _braced(text: str, index: int) -> tuple[str | None, int]:
    """``(content, end)`` of the balanced ``{...}`` group at ``text[index]``."""
    if index >= len(text) or text[index] != "{":
        return None, index
    depth, position = 0, index
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[index + 1:position], position + 1
        position += 1
    return None, index


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\n":
        index += 1
    return index


def restatable_args(text: str, index: int) -> tuple[str, str] | None:
    """``(environment, macro)`` of a thmtools ``restatable`` whose
    ``\\begin{restatable}`` ends at ``index``: ``[title]{theorem}{mainthm}``."""
    position = _skip_space(text, index)
    if position < len(text) and text[position] == "[":
        depth = 0
        for offset in range(position, min(len(text), position + 4000)):
            depth += text[offset] == "["
            depth -= text[offset] == "]"
            if depth == 0:
                position = offset + 1
                break
        else:
            return None
    env, position = _braced(text, _skip_space(text, position))
    if env is None:
        return None
    macro, _end = _braced(text, _skip_space(text, position))
    env, macro = env.strip(), (macro or "").strip()
    if not re.fullmatch(r"[A-Za-z]+\*?", env) or not re.fullmatch(r"[A-Za-z]+", macro):
        return None
    return env, macro


def find_restatables(texts: Iterable[str]) -> dict[str, str]:
    """``macro -> environment`` for every ``restatable`` in ``texts``."""
    found: dict[str, str] = {}
    for text in texts:
        text = blank_skipped(_blank_comments(text))
        for match in RESTATABLE_BEGIN_RE.finditer(text):
            args = restatable_args(text, match.end())
            if args:
                found.setdefault(args[1], args[0])
    return found


def _blank_comments(text: str) -> str:
    """``%`` comments replaced by spaces (offsets and lines preserved)."""
    out = []
    for line in text.split("\n"):
        index = 0
        cut = None
        while index < len(line):
            if line[index] == "\\":
                index += 2
                continue
            if line[index] == "%":
                cut = index
                break
            index += 1
        out.append(line if cut is None else line[:cut] + " " * (len(line) - cut))
    return "\n".join(out)


def math_numbers(env: str, body: str) -> int:
    """How many equation numbers a display-math environment prints."""
    base = env.rstrip("*")
    if env.endswith("*") or (base not in NUMBERED_MATH and base not in NUMBERED_ROWS):
        return 0
    if base == "subequations":
        return 1
    if base in NUMBERED_MATH:
        return 0 if re.search(r"\\(?:nonumber|notag|tag\*?)(?![A-Za-z])", body) else 1
    rows: list[str] = []
    depth = envs = 0
    current = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            if body.startswith("\\\\", index) and depth == 0 and envs == 0:
                rows.append("".join(current))
                current = []
                index += 2
                continue
            word = re.match(r"\\(begin|end)\s*\{", body[index:index + 12])
            if word:
                envs += 1 if word.group(1) == "begin" else -1
            current.append(body[index:index + 2])
            index += 2
            continue
        depth += char == "{"
        depth -= char == "}"
        current.append(char)
        index += 1
    rows.append("".join(current))
    numbered = 0
    for row in rows:
        content = re.sub(r"^\s*\*?\s*(?:\[[^\]]*\])?", "", row)  # \\* and \\[2pt]
        content = re.sub(r"\\label\s*\{[^}]*\}", "", content).strip()
        if content and not re.search(r"\\(?:nonumber|notag|tag\*?)(?![A-Za-z])", content):
            numbered += 1
    return numbered


def extract_macros(texts: Iterable[str]) -> dict[str, str]:
    """Author macros for KaTeX's ``macros`` option (``\\newcommand``,
    ``\\renewcommand``, ``\\providecommand``, ``\\DeclareMathOperator``)."""
    out: dict[str, str] = {}
    pattern = re.compile(
        r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator)(\*?)\s*"
        r"(?:\{\s*(\\[A-Za-z]+)\s*\}|(\\[A-Za-z]+))\s*(?:\[(\d)\])?\s*(?:\[[^\]]*\])?\s*")
    for text in texts:
        text = COMMENT_LINE_RE.sub("", text)
        for match in pattern.finditer(text):
            name = match.group(3) or match.group(4)
            body, _ = _braced(text, match.end())
            if body is None or len(body) > 400 or "@" in body:
                continue
            if match.group(1) == "DeclareMathOperator":
                body = ("\\operatorname*{" if match.group(2) else "\\operatorname{") + body + "}"
            out.setdefault(name, body.strip())
            if len(out) >= 400:
                return out
    return out


def _plain(value: str, limit: int) -> str:
    """A rough plain-text rendering of a short TeX fragment for display."""
    text = COMMENT_LINE_RE.sub("", value)
    text = re.sub(r"\\(?:emph|textbf|textit|textsc|textrm|mathrm|text|bf|it|em)\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:thanks|footnote|label)\s*\{[^{}]*\}", "", text)
    text = text.replace("\\\\", " ").replace("~", " ")
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_bibliography(project: Path, texts: Mapping[str, str]) -> dict[str, str]:
    """Citation key -> short title or entry text (``.bib``, ``.bbl`` and
    ``thebibliography``)."""
    entries: dict[str, str] = {}
    for path in sorted(project.rglob("*.bib")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        text = read_text(path)[0]
        for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", text, re.S):
            title = re.search(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*,?\s*\n", match.group(2), re.S)
            if title:
                entries.setdefault(match.group(1), re.sub(r"[{}]|\s+", lambda m: " " if not m.group(0).strip() else "", title.group(1)).strip()[:MAX_BIB_ENTRY_CHARS])
    sources = list(texts.values())
    for path in sorted(project.rglob("*.bbl")):
        if not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            sources.append(read_text(path)[0])
    for text in sources:
        text = COMMENT_LINE_RE.sub("", text)
        items = list(re.finditer(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", text))
        for index, match in enumerate(items):
            end = items[index + 1].start() if index + 1 < len(items) else len(text)
            body = text[match.end():end]
            body = body.split("\\end{thebibliography}")[0]
            entries.setdefault(match.group(1).strip(), _plain(body, MAX_BIB_ENTRY_CHARS))
    return entries


# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------


def _roman_letter(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


def _roman_numeral(number: int) -> str:
    if number <= 0 or number >= 4000:
        return ""
    out = ""
    for value, letters in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                           (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while number >= value:
            out += letters
            number -= value
    return out


def prose_marker(text: str, sentence: str | None = None) -> str | None:
    """``"start"``, ``"end"`` or ``None``: what the text just before a
    reference to a provable result says about its proof ("We now prove
    Theorem~", "This completes the proof of Lemma~", "..., proving the
    remaining implication of Theorem~").  ``sentence`` is the sentence so far
    (``text`` by default): a mention of another section in it makes a
    roadmap (``None``), and "we will ... conclude the proof of" announces
    a proof (a start)."""
    normalized = re.sub(r"\s+", " ", str(text or ""))
    sentence = re.sub(r"\s+", " ", sentence) if sentence is not None else re.split(r"[.!?] ", normalized)[-1]
    if SECTION_MENTION_RE.search(sentence):
        return None
    if PROSE_LEAD_RE.search(normalized):
        return "start"
    tail = normalized[-300:].lower()
    if len(normalized) > 300:
        tail = "\x00" + tail  # never a sentence start
    if PROSE_START_RE.search(tail):
        return "start"
    if PROSE_END_RE.search(tail):
        return "start" if FUTURE_RE.search(sentence) else "end"
    return None


def clean_body(text: str, env: str, *, args: int = 0) -> str:
    """A statement or proof body for prompts and display: the environment's
    ``\\begin``/``\\end`` (with ``args`` mandatory arguments, as in
    ``restatable``) and labels removed, comments stripped, line structure
    kept (runs of blank lines collapsed)."""
    body = re.sub(r"^\s*\\begin\s*\{" + re.escape(env) + r"\}\s*(\[[^\]]*\])?", "", text)
    for _ in range(args):
        group, end = _braced(body, _skip_space(body, 0))
        if group is None:
            break
        body = body[end:]
    body = re.sub(r"\\end\s*\{" + re.escape(env) + r"\}\s*$", "", body.rstrip())
    body = re.sub(r"\\label\s*\{[^}]*\}", "", body)
    body = COMMENT_LINE_RE.sub("", body)
    lines = [line.rstrip() for line in body.split("\n")]
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


_CONTEXT_DROP_ENVS_RE = re.compile(
    r"\\begin\s*\{(figure|table|tikzpicture|abstract|thebibliography|titlepage|center)\*?\}.*?\\end\s*\{\1\*?\}", re.S)
_CONTEXT_HEADING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_CONTEXT_DROP_CMDS_RE = re.compile(
    r"\\(?:label|vspace|hspace|bibliography|bibliographystyle|addbibresource)\*?\s*\{[^{}]*\}"
    r"|\\(?:maketitle|tableofcontents|noindent|medskip|bigskip|smallskip|newpage|clearpage|appendix|centering)\b")


def _context_text(raw: str) -> str:
    """Running text of a manuscript for a verifier: no figures, labels or
    layout commands, headings as "§ title", blank lines collapsed."""
    text = _CONTEXT_DROP_ENVS_RE.sub(" ", raw)
    text = _CONTEXT_HEADING_RE.sub(lambda match: "\n\n§ " + " ".join(match.group(1).split()) + "\n", text)
    text = _CONTEXT_DROP_CMDS_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slug(value: str) -> str:
    text = str(value or "").strip().lower().replace(".", "-")
    text = re.sub(r"[^a-z0-9:_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-:_")
    text = text[:80].rstrip("-:_")
    return text if NODE_ID_RE.fullmatch(text) else ""


def assign_ids(nodes: list[dict[str, Any]]) -> None:
    """Stable, ``_informal_links``-safe node ids: the first label's slug, else
    ``<kind>-<number>``, else ``n<index>``; collisions get ``-2``, ``-3``..."""
    taken: set[str] = set()
    for index, node in enumerate(nodes, 1):
        options = [slug(label) for label in node.get("labels") or []]
        if node.get("number"):
            options.append(slug(f"{node['kind']}-{node['number']}"))
        options.append(f"n{index}")
        chosen = next((option for option in options if option and option not in taken), "")
        if not chosen:
            base = next(option for option in options if option)[:74]
            suffix = 2
            while f"{base}-{suffix}" in taken:
                suffix += 1
            chosen = f"{base}-{suffix}"
        taken.add(chosen)
        node["id"] = chosen


def build(project: Path, root: Path, *, frontend: str) -> dict[str, Any]:
    """Parse the include closure of ``root`` and build the segments document."""
    project = project.resolve()
    order, missing, total, inserted = resolve_includes(root, project)
    if not order:
        raise LatexStructureError("The root TeX file could not be read")
    rel = {path: str(path.relative_to(project)) for path in order}
    texts: dict[str, str] = {}
    warnings: list[str] = []
    file_errors: list[dict[str, str]] = []
    entities: list[dict[str, Any]] = []
    ns_file: dict[str, str] = {}
    ns_text: dict[str, str] = {}
    read: list[tuple[Path, str]] = []
    for path in order:
        text, latin = read_text(path)
        texts[rel[path]] = text
        if latin:
            warnings.append(f"{rel[path]} is not UTF-8; read as Latin-1")
        read.append((path, text))
    restatables = find_restatables(texts.values())
    for path, text in read:
        ns = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        if ns in ns_file:
            continue
        ns_file[ns] = rel[path]
        ns_text[ns] = text
        found, error = extract_entities(ns, text, restate_macros=restatables)
        if error:
            file_errors.append({"file": rel[path], "error": error})
        entities.extend(found)
    if missing:
        warnings.append("Some included files were not found or were refused: " + ", ".join(sorted(set(missing))[:12]))
    file_order = [rel[path] for path in order]
    all_text = [texts[name] for name in file_order]
    decls = parse_theorem_decls(all_text)

    def file_key(name: str, depth: int = 0) -> tuple:
        path = project / name
        resolved = path.resolve()
        if resolved not in inserted or depth > 40:
            return ()
        parent, row, column = inserted[resolved]
        return file_key(rel.get(parent, str(parent.relative_to(project))), depth + 1) + ((row, column),)

    fkeys = {ns: file_key(name) for ns, name in ns_file.items()}
    line_starts = {}
    for ns, text in ns_text.items():
        starts = [0]
        for index, char in enumerate(text):
            if char == "\n":
                starts.append(index + 1)
        line_starts[ns] = starts

    def point_at(ns: str, position: int) -> tuple[int, int]:
        starts = line_starts[ns]
        row = bisect.bisect_right(starts, position) - 1
        return row, position - starts[row]

    def point(entity: Mapping[str, Any], which: str = "_s") -> tuple[int, int]:
        return point_at(entity["ns"], entity[which])

    for entity in entities:
        entity["start"] = point(entity, "_s")

    by_id = {entity["id"]: entity for entity in entities}

    def okey(entity: Mapping[str, Any]) -> tuple:
        return fkeys.get(entity["ns"], ((10 ** 9, 0),)) + (entity["start"],)

    def ancestors(entity: Mapping[str, Any]):
        seen = 0
        parent = entity.get("parent")
        while parent and parent in by_id and seen < 10000:
            yield by_id[parent]
            parent = by_id[parent].get("parent")
            seen += 1

    def source_slice(entity: Mapping[str, Any]) -> str:
        return ns_text[entity["ns"]][entity["_s"]:entity["_e"]]

    # --- classify environments ---------------------------------------------
    stmt_kind: dict[str, str] = {}
    eff_env: dict[str, str] = {}   # a restatable -> the theorem environment it wraps
    restated: dict[str, str] = {}  # restatement macro (\mainthm*) -> its statement
    proofs: list[dict[str, Any]] = []
    for entity in entities:
        if entity["kind"] != "environment":
            continue
        env = (entity.get("subkind") or "").strip()
        base = env.rstrip("*")
        if base in MATH_ENVS:
            continue
        if "proof" in base.lower() or base.lower() in ("pf", "prf"):
            proofs.append(entity)
            continue
        macro = None
        if base == "restatable":
            text = source_slice(entity)
            begin = RESTATABLE_BEGIN_RE.match(text)
            args = restatable_args(text, begin.end()) if begin else None
            if args is None:
                continue
            env, macro = args
            base = env.rstrip("*")
        declaration = decls.get(env) or decls.get(base)
        kind = canon_kind(base, declaration["display"] if declaration else None)
        if kind:
            stmt_kind[entity["id"]] = kind
            if macro:
                eff_env[entity["id"]] = env
                restated.setdefault(macro, entity["id"])

    statements = sorted((by_id[identifier] for identifier in stmt_kind), key=okey)
    scanned_total = len(statements)
    if scanned_total > MAX_SCANNED_STATEMENTS:
        for entity in statements[MAX_SCANNED_STATEMENTS:]:
            stmt_kind.pop(entity["id"], None)
        statements = statements[:MAX_SCANNED_STATEMENTS]
        warnings.append(f"Only the first {MAX_SCANNED_STATEMENTS} statements were scanned")
    proofs.sort(key=okey)
    proof_ids = {proof["id"] for proof in proofs}
    section_entities = [entity for entity in entities if entity["kind"] == "section"]

    def env_of(entity: Mapping[str, Any]) -> str:
        return eff_env.get(entity["id"]) or (entity.get("subkind") or "")

    def declaration_of(entity: Mapping[str, Any]) -> dict[str, Any] | None:
        env = env_of(entity)
        return decls.get(env) or decls.get(env.rstrip("*"))

    # --- numbering --------------------------------------------------------------
    # Section-reset and shared counters, \numberwithin / \counterwithin on any
    # counter, theorems that share the equation counter (numbered display
    # math steps it), \the<counter> redefinitions and appendix letters.  A
    # format that cannot be reproduced gives no number rather than a wrong one.
    docclass = "article"
    match = re.search(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", all_text[0] if all_text else "")
    if match:
        docclass = match.group(1).strip()
    top = "chapter" if docclass in ("book", "report", "memoir", "amsbook") else "section"
    counters: dict[str, int] = defaultdict(int)
    parent_of: dict[str, str | None] = {"subsection": "section", "subsubsection": "subsection",
                                        "section": "chapter" if top == "chapter" else None}
    reset_only: set[str] = set()
    for env, declaration in decls.items():
        if declaration["counter"] == env and declaration["within"]:
            parent_of[env] = declaration["within"]
    for text in all_text:
        blanked = _blank_comments(text)
        for counter, parent in re.findall(r"\\@addtoreset\s*\{([^}]+)\}\s*\{([^}]+)\}", blanked):
            parent_of[counter.strip()] = parent.strip()
            reset_only.add(counter.strip())
        for command, star, counter, parent in COUNTER_WITHIN_RE.findall(blanked):
            parent_of[counter.strip()] = parent.strip()
            if command == "counterwithin" and star:
                reset_only.add(counter.strip())
            else:
                reset_only.discard(counter.strip())
    appendix_at = []
    for name in file_order:
        text = texts[name]
        for found in re.finditer(r"(?m)^[^%\n]*?\\appendix\b", text):
            row = text.count("\n", 0, found.end())
            appendix_at.append(file_key(name) + ((row, 0),))
    appendix_key = min(appendix_at) if appendix_at else None
    state = {"in_appendix": False}
    # counter -> (prefix counter, separator, style) from \renewcommand{\the<counter>}; None: not reproducible
    formats: dict[str, tuple[str | None, str, str] | None] = {}
    events: list[tuple[tuple, int, str, Any]] = [(okey(entity), 0, "entity", entity)
                                                 for entity in section_entities + statements]
    for ns, text in ns_text.items():
        blanked = blank_skipped(_blank_comments(text))
        for found in THE_COUNTER_RE.finditer(blanked):
            counter = found.group(1)
            body, _end = _braced(blanked, found.end())
            if body is None:
                continue
            simple = re.fullmatch(r"\s*(?:\\the([A-Za-z]+)\s*(?:\{\})?\s*([.\-:]|--)?\s*)?"
                                  r"\\(Alph|alph|Roman|roman|arabic)\s*\{\s*" + re.escape(counter) + r"\s*\}\s*", body)
            spec = (simple.group(1), simple.group(2) or "", simple.group(3)) if simple else None
            events.append((fkeys.get(ns, ((10 ** 9, 0),)) + (point_at(ns, found.start()),), 1, "format", (counter, spec)))
    if "equation" in {declaration["counter"] for declaration in decls.values()}:
        numbered = NUMBERED_MATH | NUMBERED_ROWS
        for entity in entities:
            env = entity.get("subkind") or ""
            if entity["kind"] != "environment" or env.rstrip("*") not in numbered:
                continue
            if any(ancestor["kind"] == "environment" and (ancestor.get("subkind") or "").rstrip("*") in numbered
                   for ancestor in ancestors(entity)):
                continue  # inside subequations or another numbered display
            body = re.sub(r"^\\begin\s*\{[^}]*\}", "", source_slice(entity))
            body = re.sub(r"\\end\s*\{[^}]*\}\s*$", "", body)
            count = math_numbers(env, body)
            if count:
                events.append((okey(entity), 0, "equation", count))
    unreproducible: set[str] = set()

    def render(value: int, style: str) -> str:
        if style in ("Alph", "alph"):
            letters = _roman_letter(value)
            return letters if style == "Alph" else letters.lower()
        if style in ("Roman", "roman"):
            numeral = _roman_numeral(value)
            return numeral if style == "Roman" else numeral.lower()
        return str(value)

    def fmt(counter: str, depth: int = 0) -> str | None:
        value = counters[counter]
        if counter == top and appendix_key is not None and state["in_appendix"]:
            return _roman_letter(value) or None
        if counter in formats:
            spec = formats[counter]
            if spec is None:
                unreproducible.add(counter)
                return None
            prefix_counter, separator, style = spec
            rendered = render(value, style)
            if not rendered:
                return None
            if prefix_counter and depth < 12:
                prefix = fmt(prefix_counter, depth + 1)
                return None if prefix is None else prefix + separator + rendered
            return rendered
        parent = parent_of.get(counter) if counter not in reset_only else None
        if parent and depth < 12:
            prefix = fmt(parent, depth + 1)
            return None if prefix is None else prefix + "." + str(value)
        return str(value)

    def step(counter: str) -> None:
        counters[counter] += 1
        for child, parent in list(parent_of.items()):
            if parent == counter:
                stack = [child]
                guard = 0
                while stack and guard < 200:
                    guard += 1
                    item = stack.pop()
                    counters[item] = 0
                    stack.extend(name for name, owner in parent_of.items() if owner == item)

    numbers: dict[str, str] = {}
    for key, _rank, what, payload in sorted(events, key=lambda event: (event[0], event[1])):
        if appendix_key is not None and not state["in_appendix"] and key > appendix_key:
            state["in_appendix"] = True
            counters[top] = 0
        if what == "format":
            formats[payload[0]] = payload[1]
            continue
        if what == "equation":
            for _ in range(payload):
                step("equation")
            continue
        entity = payload
        if entity["kind"] == "section":
            name = entity.get("subkind") or "section"
            if entity.get("star"):
                continue
            if name in SECTION_LEVELS:
                step(name)
                rendered = fmt(name)
                if rendered:
                    numbers[entity["id"]] = rendered
            continue
        declaration = declaration_of(entity)
        if not declaration or not declaration["numbered"] or env_of(entity).endswith("*"):
            continue
        counter = declaration["counter"]
        if counter not in SECTION_LEVELS and counter not in parent_of and decls.get(counter, {}).get("within"):
            parent_of[counter] = decls[counter]["within"]
        step(counter)
        rendered = fmt(counter)
        if rendered:
            numbers[entity["id"]] = rendered
    if unreproducible:
        warnings.append("Statement numbers that use a custom format (\\the" + ", \\the".join(sorted(unreproducible))[:80]
                        + ") are not shown")

    # --- labels -> owning statement / proof / section -----------------------
    label_owner: dict[str, tuple[str, str | None]] = {}
    for entity in entities:
        if entity["kind"] != "label_def":
            continue
        name = entity.get("content") or ""
        owner: tuple[str, str | None] = ("other", entity.get("parent"))
        via_math = via_env = False
        for ancestor in ancestors(entity):
            if ancestor["kind"] == "environment" and (ancestor.get("subkind") or "").rstrip("*") in MATH_ENVS:
                via_math = True
            if ancestor["id"] in stmt_kind:
                owner = ("stmt-eq" if via_math else "stmt", ancestor["id"])
                break
            if ancestor["id"] in proof_ids:
                owner = ("proof", ancestor["id"])
                break
            if ancestor["kind"] == "section":
                # A section's own label, not an equation, figure or item in it.
                owner = ("other" if via_env or via_math else "section", ancestor["id"])
                break
            if ancestor["kind"] in ("environment", "display_math", "inline_math"):
                via_env = True
        label_owner[name] = owner
    own_labels: dict[str, list[str]] = defaultdict(list)
    for label, (kind, owner_id) in label_owner.items():
        if kind == "stmt" and owner_id:
            own_labels[owner_id].append(label)

    # --- attach proofs to statements -----------------------------------------
    # A proof's title names its result by \ref, by number ("Proof of Theorem
    # 1.1") or as "the main theorem"; a title that names a result we cannot
    # find leaves the proof unattached.  An untitled proof belongs to the
    # nearest preceding provable statement that is not inside a finished proof
    # (or that a \restatement just repeated), unless that one already has a
    # proof.
    main_labelled = [statement["id"] for statement in statements
                     if stmt_kind[statement["id"]] == "theorem"
                     and (any("main" in label.lower() for label in own_labels.get(statement["id"], []))
                          or "main" in (statement.get("title") or "").lower())]

    def display_of(identifier: str) -> str:
        return str((declaration_of(by_id[identifier]) or {}).get("display") or stmt_kind[identifier]).lower()

    def by_number(word: str, number: str, before: tuple) -> str | None:
        kind = canon_kind(word.lower(), None)
        earlier = later = None
        for statement in statements:
            identifier = statement["id"]
            if numbers.get(identifier) != number:
                continue
            if stmt_kind[identifier] != kind and not display_of(identifier).startswith(word.lower()):
                continue
            if okey(statement) < before:
                earlier = identifier
            elif later is None:
                later = identifier
        return earlier or later

    def title_target(entity: Mapping[str, Any]) -> tuple[str | None, str]:
        title = entity.get("title") or ""
        if not title:
            head = source_slice(entity)[:300]
            match = re.match(r"\\begin\{[^}]*\}\s*\{([^}]*\})", head)
            title = match.group(1) if match else ""
        if not title.strip():
            return None, ""
        refs = REF_RE.findall(title)
        if refs:
            for labels in refs:
                for label in labels.split(","):
                    owner = label_owner.get(label.strip())
                    if owner and owner[0] in ("stmt", "stmt-eq"):
                        return owner[1], "title-ref"
            if re.search(r"(?i)(?:theorem|lemma|proposition|corollary|claim|result)s?\b|\\[cC]ref|\\autoref", title):
                return None, "unattached"
            return None, "titled"  # "Proof of \eqref{x}": the preceding statement
        match = TITLE_NUMBER_RE.search(_plain(title, 300))
        if match:
            found = by_number(match.group("word"), match.group("number"), okey(entity))
            return (found, "title-number") if found else (None, "unattached")
        if TITLE_MAIN_RE.search(title):
            return (main_labelled[0], "title-main") if len(main_labelled) == 1 else (None, "unattached")
        return None, "titled"

    nearest_proof = {statement["id"]: next((ancestor["id"] for ancestor in ancestors(statement)
                                            if ancestor["id"] in proof_ids), None)
                     for statement in statements}

    def preceding_for(proof: Mapping[str, Any], seen: list[dict[str, Any]]) -> dict[str, Any] | None:
        enclosing = {ancestor["id"] for ancestor in ancestors(proof) if ancestor["id"] in proof_ids}
        for candidate in reversed(seen[-64:]):
            owner_proof = nearest_proof.get(candidate["id"])
            if owner_proof is None or owner_proof in enclosing:
                return candidate
        return None

    proof_of: dict[str, str] = {}
    proof_how: dict[str, str] = {}
    unattached = 0
    provable_seen: list[dict[str, Any]] = []
    restatements = [entity for entity in entities if entity["kind"] == "restate"]
    for entity in sorted(statements + proofs + restatements, key=okey):
        if entity["kind"] == "restate":
            original = restated.get(entity.get("content") or "")
            if original in stmt_kind and stmt_kind[original] in PROVABLE:
                provable_seen.append(by_id[original])
            continue
        if entity["id"] in stmt_kind:
            if stmt_kind[entity["id"]] in PROVABLE:
                provable_seen.append(entity)
            continue
        target, how = title_target(entity)
        if target is None:
            candidate = preceding_for(entity, provable_seen) if how != "unattached" else None
            # A second untitled proof never joins a statement that has one.
            if candidate is None or (candidate["id"] in proof_of.values() and how != "titled"):
                unattached += 1
                continue
            target, how = candidate["id"], "preceding"
        proof_of[entity["id"]] = target
        proof_how[entity["id"]] = how
    if unattached:
        warnings.append(f"{unattached} proof environment(s) could not be matched to a statement and were left out")
    for label, (kind, owner_id) in list(label_owner.items()):
        if kind == "proof" and owner_id in proof_of:
            label_owner[label] = ("stmt-proof", proof_of[owner_id])
    proofs_by_stmt: dict[str, list[str]] = defaultdict(list)
    for proof_id, statement_id in sorted(proof_of.items(), key=lambda item: okey(by_id[item[0]])):
        proofs_by_stmt[statement_id].append(proof_id)

    nested_in: dict[str, str] = {}
    for statement in statements:
        for ancestor in ancestors(statement):
            if ancestor["id"] in proof_of:
                nested_in[statement["id"]] = proof_of[ancestor["id"]]
                break

    # --- prose proofs ----------------------------------------------------------
    # Only for results with no proof environment: an environment proof is
    # authoritative, and prose that mentions such a result is commentary.  A
    # start marker opens a proof that runs to an end marker for the same
    # result or to the next heading at its own level (across included files);
    # an end marker with no open start closes a proof that began with its
    # section, or after the last theorem or proof environment there.
    def in_env_context(entity: Mapping[str, Any]) -> bool:
        return any(ancestor["id"] in stmt_kind or ancestor["id"] in proof_ids for ancestor in ancestors(entity))

    def section_chain(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [ancestor for ancestor in ancestors(entity) if ancestor["kind"] == "section"]

    def level(section: Mapping[str, Any] | None) -> int:
        return SECTION_LEVELS.get(section.get("subkind") or "", 9) if section is not None else -2

    roadmap_ids = set()
    for section in section_entities:
        title = _plain(section.get("title") or "", 200)
        if ROADMAP_TITLE_RE.search(title) and not re.search(r"(?i)\bproofs?\s+of\b", title):
            roadmap_ids.add(section["id"])

    def roadmap(entity: Mapping[str, Any]) -> bool:
        return any(ancestor["id"] in roadmap_ids
                   or (ancestor["kind"] == "environment" and (ancestor.get("subkind") or "").rstrip("*") == "abstract")
                   for ancestor in ancestors(entity))

    def heading_of(entity: Mapping[str, Any]) -> dict[str, Any] | None:
        parent = by_id.get(entity.get("parent"))
        if parent is not None and parent["kind"] == "section" and entity["_s"] < parent.get("_head_e", parent["_s"]):
            return parent
        return None

    def ref_owners(entity: Mapping[str, Any]) -> list[tuple[str, str | None]]:
        return [label_owner[label.strip()] for label in (entity.get("content") or "").split(",")
                if label.strip() in label_owner]

    env_ends: dict[str, list[int]] = defaultdict(list)
    for entity in statements + proofs:
        env_ends[entity["ns"]].append(entity["_e"])
    for ends in env_ends.values():
        ends.sort()
    ordered = sorted(entities, key=okey)

    def roadmap_follows(position: int) -> bool:
        """Another place named later in the same sentence ("We prove Theorem 1 in Section 4")."""
        for later in ordered[position + 1:position + 16]:
            if later["kind"] == "section":
                return False
            if in_env_context(later):
                continue
            if later["kind"] == "text":
                content = " ".join(str(later.get("content") or "").split())
                head = SENTENCE_END_RE.split(content, maxsplit=1)[0]
                if SECTION_MENTION_RE.search(head) or LATER_RE.search(head):
                    return True
                if len(head) < len(content):
                    return False
            elif later["kind"] == "label_ref" and any(owner[0] == "section" for owner in ref_owners(later)):
                return True
        return False

    prose_spans: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    prose_ends: dict[str, list[int]] = defaultdict(list)
    prose_context: dict[str, list[str]] = defaultdict(list)  # reference -> the results whose proof it is in
    plain_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)  # per file, in document order
    plain_starts: dict[str, list[int]] = defaultdict(list)  # their offsets, for bisection
    cursor: dict[str, int] = defaultdict(int)
    span: dict[str, Any] | None = None
    chain_open = False  # the last reference opened (or joined) a prose proof in this sentence
    previous_text, previous_entity = "", None
    sentence_section_ref = False
    sentence_text = ""  # the current sentence of running text, across references
    sentence_start: tuple[str, int] | None = None  # (file, offset) where it began
    sentence_refs: list[dict[str, Any]] = []

    # The statement whose environment (or attached proof) ends at an offset:
    # "..., this proves Proposition~" right after it uses that result.
    ended_by: dict[tuple[str, int], str] = {}
    for entity in statements:
        ended_by[(entity["ns"], entity["_e"])] = entity["id"]
    for proof in proofs:
        if proof["id"] in proof_of:
            ended_by[(proof["ns"], proof["_e"])] = proof_of[proof["id"]]
    anaphora: dict[str, set[str]] = defaultdict(set)

    def record_span(targets: Iterable[str], ns: str, start: int, end: int) -> None:
        if end > start:
            for target in targets:
                prose_spans[target].append((ns, start, end))
            prose_ends[ns].append(end)

    def close_span(ns: str | None = None, end: int | None = None) -> None:
        nonlocal span
        if span is not None:
            stop = end if end is not None and ns == span["ns"] else cursor[span["ns"]]
            record_span(span["targets"], span["ns"], span["start"], stop)
        span = None

    def end_only_start(marker: Mapping[str, Any], target: str) -> int | None:
        ns = marker["ns"]
        chain = section_chain(marker)
        context = next((section for section in reversed(chain) if level(section) >= 1), chain[0] if chain else None)
        start = context["_s"] if context is not None and context["ns"] == ns else 0
        ends = env_ends.get(ns, [])
        index = bisect.bisect_right(ends, marker["_s"]) - 1
        if index >= 0:
            start = max(start, ends[index])
        start = max([start] + [end for end in prose_ends.get(ns, []) if end <= marker["_s"]])
        statement = by_id[target]
        if statement["ns"] == ns and statement["_e"] <= marker["_s"]:
            start = max(start, statement["_e"])
        elif okey(statement) > okey(marker):
            return None  # the result is stated later: this is no proof of it
        return start if start < marker["_s"] else None

    for position, entity in enumerate(ordered):
        kind = entity["kind"]
        ns = entity["ns"]
        if kind == "section" or kind == "boundary" or (
                kind == "environment" and (entity.get("subkind") or "") == "thebibliography"):
            # A heading at the proof's own level or above, or the bibliography, ends it.
            if span is not None and (kind != "section" or level(entity) <= span["level"]):
                close_span(ns, entity["_s"])
            previous_text, previous_entity, sentence_section_ref, sentence_text = "", None, False, ""
            sentence_start, sentence_refs = None, []
            continue
        if span is not None and ns != span["ns"]:
            # The proof continues in an included file (or back in its parent).
            record_span(span["targets"], span["ns"], span["start"], cursor[span["ns"]])
            span = dict(span, ns=ns, start=entity["_s"])
        cursor[ns] = max(cursor[ns], entity["_e"])
        if entity["id"] in stmt_kind or entity["id"] in proof_ids:
            # A theorem or proof environment interrupts the running text.
            previous_text, previous_entity, sentence_section_ref, sentence_text = "", None, False, ""
            sentence_start, sentence_refs = None, []
            continue
        if kind not in ("text", "label_ref") or in_env_context(entity):
            continue
        if kind == "text":
            previous_text, previous_entity = entity.get("content") or "", entity
            if heading_of(entity) is not None:
                sentence_text, sentence_start, sentence_refs = "", None, []  # a title is no sentence
                continue
            ends = list(SENTENCE_END_RE.finditer(previous_text))
            if ends:
                chain_open = False
                sentence_section_ref = False
                sentence_text = previous_text[ends[-1].end():]
                sentence_start, sentence_refs = (ns, entity["_s"] + ends[-1].end()), []
            else:
                sentence_text = (sentence_text + " " + previous_text)[-2000:]
                if sentence_start is None:
                    sentence_start = (ns, entity["_s"])
            continue
        owners = ref_owners(entity)
        target = next((owner[1] for owner in owners
                       if owner[0] == "stmt" and stmt_kind.get(owner[1] or "") in PROVABLE), None)
        heading = heading_of(entity)
        if heading is not None:
            # "Proof of Theorem~\ref{a} and Theorem~\ref{b}": the section proves
            # each; a reference in a heading is never part of a proof.
            if target is not None and target not in proofs_by_stmt:
                if span is not None and span.get("heading") == heading["id"]:
                    if target not in span["targets"]:
                        span["targets"].append(target)
                elif TITLE_PROOF_RE.search(" ".join(previous_text.split())):
                    close_span(ns, heading["_s"])
                    span = {"targets": [target], "level": level(heading), "ns": heading["ns"],
                            "start": heading["_s"], "heading": heading["id"]}
            previous_text = ""
            continue
        if (chain_open and span is not None and target is not None and target not in span["targets"]
                and target not in proofs_by_stmt and CONJOINED_RE.search(" ".join(previous_text.split()).lower())):
            span["targets"].append(target)
            previous_text = ""
            continue
        chain_open = False
        marker = None
        if target is not None and target not in proofs_by_stmt and not roadmap(entity) and not sentence_section_ref:
            marker = prose_marker(previous_text, sentence_text)
            if marker == "start" and roadmap_follows(position):
                marker = None
            if span is not None and target not in span["targets"]:
                # Inside another result's prose proof: the end of a different
                # proof, or the start of one for a result stated inside this
                # proof, is part of this proof (a reference it makes).
                stated = by_id[target]
                inside = stated["ns"] == span["ns"] and stated["_s"] >= span["start"]
                if marker == "end" or (marker == "start" and inside):
                    marker = None
        if marker == "start":
            # The proof begins with the sentence that announces it.
            begin = sentence_start[1] if sentence_start is not None and sentence_start[0] == ns else (
                previous_entity or entity)["_s"]
            close_span(ns, begin)
            span = {"targets": [target], "level": level(next(iter(section_chain(entity)), None)),
                    "ns": ns, "start": begin, "heading": None}
            for ref in sentence_refs:
                if target not in prose_context[ref["id"]]:
                    prose_context[ref["id"]].append(target)
            chain_open = True
            previous_text = ""
            continue
        if marker == "end":
            if span is not None and target in span["targets"]:
                close_span(ns, entity["_e"])
            elif span is None:
                start = end_only_start(entity, target)
                if start is not None:
                    record_span([target], ns, start, entity["_e"])
                    earlier = plain_refs[ns]
                    for ref in earlier[bisect.bisect_left(plain_starts[ns], start):]:
                        if ref["_s"] < entity["_s"] and target not in prose_context[ref["id"]]:
                            prose_context[ref["id"]].append(target)
                    used = ended_by.get((ns, start))
                    gap = ns_text[ns][start:entity["_s"]]
                    if (used and used != target and stmt_kind.get(used) in PROVABLE
                            and len("".join(gap.split())) < 300 and ANAPHORA_RE.search(gap)):
                        anaphora[target].add(used)
            previous_text = ""
            continue
        if span is not None:
            prose_context[entity["id"]] = list(span["targets"])
        plain_refs[ns].append(entity)
        plain_starts[ns].append(entity["_s"])
        sentence_refs.append(entity)
        if any(owner[0] == "section" for owner in owners):
            sentence_section_ref = True
        previous_text = ""
    close_span()
    prose_proved = {target for target, spans in prose_spans.items() if spans}

    # --- references -> edges ---------------------------------------------------
    edges: dict[tuple[str, str], dict[str, set]] = {}
    mentions: Counter = Counter()
    dangling: Counter = Counter()
    for entity in entities:
        if entity["kind"] != "label_ref":
            continue
        context_statement, where = None, None
        for ancestor in ancestors(entity):
            if ancestor["id"] in stmt_kind:
                context_statement, where = ancestor["id"], "statement"
                break
            if ancestor["kind"] == "environment" and ancestor["id"] in proof_of:
                context_statement, where = proof_of[ancestor["id"]], "proof"
                break
        contexts = [(context_statement, where)] if context_statement is not None else [
            (target, "proof-prose") for target in prose_context.get(entity["id"], [])]
        for label in (entity.get("content") or "").split(","):
            label = label.strip()
            if not label:
                continue
            owner = label_owner.get(label)
            if owner is None:
                dangling[label] += 1
                continue
            if not contexts:
                mentions[label] += 1
                continue
            for context_statement, where in contexts:
                if owner[0] in ("stmt", "stmt-eq", "stmt-proof") and owner[1] != context_statement:
                    record = edges.setdefault((context_statement, owner[1]), {"labels": set(), "where": set()})
                    record["labels"].add(label)
                    record["where"].add(where + {"stmt": "", "stmt-eq": "+eq-in-statement",
                                                 "stmt-proof": "+eq-in-proof"}[owner[0]])
    for inner, outer in nested_in.items():
        if inner != outer:
            record = edges.setdefault((outer, inner), {"labels": set(), "where": set()})
            record["where"].add("nested-claim")
    for user, used_ids in anaphora.items():
        for used in used_ids:
            edges.setdefault((user, used), {"labels": set(), "where": set()})["where"].add("proof-prose")

    def score(key: tuple[str, str]) -> int:
        where = edges[key]["where"]
        if "proof" in where or "proof-prose" in where or "nested-claim" in where:
            return 3
        if any(item.startswith("proof") for item in where):
            return 2
        if "statement" in where:
            return 1
        return 0

    # --- cycle breaking (weakest evidence first) --------------------------------
    dropped = []
    adjacency: dict[str, set[str]] = defaultdict(set)
    for first, second in edges:
        adjacency[first].add(second)

    def find_cycle() -> list[str] | None:
        color: dict[str, int] = {}
        for start in sorted(adjacency):
            if color.get(start) is not None:
                continue
            stack = [(start, iter(sorted(adjacency[start])))]
            path = [start]
            color[start] = 1
            while stack:
                node, children = stack[-1]
                advanced = False
                for child in children:
                    if color.get(child) == 1:
                        return path[path.index(child):] + [child]
                    if color.get(child) is None:
                        color[child] = 1
                        path.append(child)
                        stack.append((child, iter(sorted(adjacency[child]))))
                        advanced = True
                        break
                if not advanced:
                    color[node] = 2
                    path.pop()
                    stack.pop()
        return None

    while len(dropped) < MAX_CYCLE_BREAKS:
        cycle = find_cycle()
        if not cycle:
            break
        pairs = list(zip(cycle, cycle[1:]))
        worst = min(pairs, key=lambda key: (score(key), len(edges[key]["labels"])))
        dropped.append({"from": worst[0], "to": worst[1], "labels": sorted(edges[worst]["labels"]),
                        "where": sorted(edges[worst]["where"])})
        adjacency[worst[0]].discard(worst[1])
        del edges[worst]

    # --- document-order fallback ---------------------------------------------------
    def inner_section(entity: Mapping[str, Any]) -> str | None:
        for ancestor in ancestors(entity):
            if ancestor["kind"] == "section":
                return ancestor["id"]
        return None

    def reaches(start: str, target: str) -> bool:
        stack, seen = [start], {start}
        while stack:
            current = stack.pop()
            if current == target:
                return True
            for child in adjacency.get(current, ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return False

    # A weak guess never closes a cycle: when the previous statement already
    # depends on this one (a main theorem stated before its lemmas), the guess
    # is skipped rather than letting it displace a real reference later on.
    order_edges = []
    has_out = {first for (first, _) in edges}
    previous = None
    for statement in statements:
        identifier = statement["id"]
        if (stmt_kind[identifier] in PROVABLE and identifier not in has_out
                and (proofs_by_stmt.get(identifier) or identifier in prose_proved)
                and previous is not None and inner_section(previous) == inner_section(statement)
                and not reaches(previous["id"], identifier)):
            order_edges.append((identifier, previous["id"]))
            adjacency[identifier].add(previous["id"])
        previous = statement

    # --- running text before each result (setup and notation) -------------------
    # Papers introduce notation in prose ("Let V_R be ..."); the verifier sees
    # the text between theorem and proof environments before a result, nearest
    # last, so that notation set up there is not reported as missing.
    root_ns = next((ns for ns, name in ns_file.items() if name == file_order[0]), None)
    blocks: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for entity in [by_id[identifier] for identifier in stmt_kind] + proofs:
        blocks[entity["ns"]].append((entity["_s"], entity["_e"]))
    context_gaps: list[tuple[tuple, str]] = []
    for ns, raw in ns_text.items():
        visible = blank_skipped(_blank_comments(raw))
        low, high = 0, len(visible)
        if ns == root_ns:
            begin = re.search(r"\\begin\s*\{document\}", visible)
            end = re.search(r"\\end\s*\{document\}", visible)
            low = begin.end() if begin else 0
            high = end.start() if end else high
        cursor_at = low
        for start, stop in sorted(blocks.get(ns, [])) + [(high, high)]:
            if start > cursor_at:
                text = _context_text(visible[cursor_at:min(start, high)])
                if text:
                    context_gaps.append((fkeys.get(ns, ((10 ** 9, 0),)) + (point_at(ns, cursor_at),), text))
            cursor_at = max(cursor_at, stop)
            if cursor_at >= high:
                break
    context_gaps.sort(key=lambda item: item[0])
    context_keys = [key for key, _ in context_gaps]

    def context_before(entity: Mapping[str, Any]) -> str:
        parts: list[str] = []
        size = 0
        for index in range(bisect.bisect_left(context_keys, okey(entity)) - 1, -1, -1):
            parts.append(context_gaps[index][1])
            size += len(context_gaps[index][1]) + 2
            if size >= MAX_CONTEXT_CHARS:
                break
        return "\n\n".join(reversed(parts))[-MAX_CONTEXT_CHARS:]

    # --- bibliography, sections ------------------------------------------------
    bibliography = parse_bibliography(project, texts)
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        if entity.get("parent"):
            children[entity["parent"]].append(entity)

    def cites_within(root_id: str) -> list[tuple[str, str]]:
        """Citation keys (with their optional notes) below ``root_id``, in
        document order."""
        found: list[dict[str, Any]] = []
        stack = [root_id]
        while stack:
            current_id = stack.pop()
            for child in children.get(current_id, ()):
                if child["kind"] == "citation":
                    found.append(child)
                stack.append(child["id"])
        seen_keys: dict[str, str] = {}
        for citation in sorted(found, key=lambda entity: entity["_s"]):
            note = str(citation.get("note") or "")
            for key in (citation.get("content") or "").split(","):
                key = key.strip()
                if key and (key not in seen_keys or (note and not seen_keys[key])):
                    seen_keys[key] = note
        return list(seen_keys.items())

    def section_path(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
        path = []
        for ancestor in ancestors(entity):
            if ancestor["kind"] == "section":
                path.append({"level": ancestor.get("subkind"), "title": _plain(ancestor.get("title") or "", 160),
                             "number": numbers.get(ancestor["id"]), "_id": ancestor["id"]})
        return list(reversed(path))

    labels_by_owner: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    for label, owner in label_owner.items():
        labels_by_owner[owner].append(label)

    def lines(entity: Mapping[str, Any]) -> list[int]:
        start_row = point(entity, "_s")[0]
        end_row = point(entity, "_e")[0]
        return [start_row + 1, end_row + 1]

    nodes: list[dict[str, Any]] = []
    for statement in statements:
        identifier = statement["id"]
        env = statement.get("subkind") or ""
        body = clean_body(source_slice(statement), env, args=2 if identifier in eff_env else 0)
        declaration = declaration_of(statement)
        attached = proofs_by_stmt.get(identifier, [])
        proof_parts = []
        proof_lines = None
        for proof_id in attached:
            proof = by_id[proof_id]
            proof_parts.append(clean_body(source_slice(proof), proof.get("subkind") or "proof"))
            if proof_lines is None:
                proof_lines = lines(proof)
        prose = prose_spans.get(identifier) or []
        for ns, start, end in prose:
            proof_parts.append(COMMENT_LINE_RE.sub("", ns_text[ns][start:end]).strip())
            if proof_lines is None:
                starts = line_starts[ns]
                proof_lines = [bisect.bisect_right(starts, start), bisect.bisect_right(starts, max(start, end - 1))]
        cites = cites_within(identifier)
        for proof_id in attached:
            known = {key for key, _ in cites}
            cites += [(key, note) for key, note in cites_within(proof_id) if key not in known]
        title = statement.get("title")
        nodes.append({
            "entity": identifier,
            "kind": stmt_kind[identifier],
            "env": eff_env.get(identifier, env),
            "display": (declaration or {}).get("display") or stmt_kind[identifier].capitalize(),
            "number": numbers.get(identifier),
            "title": title,
            "labels": sorted(labels_by_owner.get(("stmt", identifier), [])),
            "inner_labels": sorted(labels_by_owner.get(("stmt-eq", identifier), [])
                                   + labels_by_owner.get(("stmt-proof", identifier), [])),
            "file": ns_file.get(statement["ns"]),
            "lines": lines(statement),
            "proof_file": ns_file.get(by_id[attached[0]]["ns"]) if attached else (ns_file.get(prose[0][0]) if prose else None),
            "proof_lines": proof_lines,
            "section": section_path(statement),
            "statement": body[:MAX_STATEMENT_CHARS],
            "statement_chars": len(body),
            "context": context_before(statement),
            "proof": "\n\n".join(part for part in proof_parts if part)[:MAX_PROOF_CHARS],
            "proof_chars": sum(len(part) for part in proof_parts),
            "has_proof": bool(attached) or identifier in prose_proved,
            "proof_link": [proof_how.get(proof_id) for proof_id in attached] + (["prose"] if identifier in prose_proved else []),
            "cites": [{"key": key, "note": note[:120] or None, "title": bibliography.get(key)} for key, note in cites][:24],
            "nested_in": nested_in.get(identifier),
            "external": bool(re.search(r"\\cite[a-z]*\*?\s*(\[[^\]]*\]\s*)*\{", title or "")),
        })

    entity_edges = [{"from": first, "to": second, "kind": "uses", "labels": sorted(record["labels"]),
                     "where": sorted(record["where"]), "strength": score((first, second))}
                    for (first, second), record in sorted(edges.items())]
    entity_edges += [{"from": first, "to": second, "kind": "order", "weak": True} for first, second in order_edges]

    # --- main results ----------------------------------------------------------
    used = {edge["to"] for edge in entity_edges if edge["kind"] == "uses"}
    theorems = [node for node in nodes if node["kind"] == "theorem" and not node["external"]]
    main = {node["entity"] for node in theorems
            if any("main" in label.lower() for label in node["labels"]) or "main" in (node["title"] or "").lower()}
    if not main:
        first_section = next((node["section"][0]["_id"] for node in nodes if node["section"]), None)
        main = {node["entity"] for node in theorems if node["section"] and node["section"][0]["_id"] == first_section}
    if not main:
        main = {node["entity"] for node in theorems if node["entity"] not in used}
    if not main:
        for kind in ("proposition", "corollary", "lemma", "claim"):
            candidates = [node["entity"] for node in nodes
                          if node["kind"] == kind and not node["external"] and node["entity"] not in used and node["has_proof"]]
            if candidates:
                main = set(candidates[:8])
                break
    for node in nodes:
        node["main_result"] = node["entity"] in main

    # --- cap: main-result closure first, then document order ----------------------
    truncated = None
    if len(nodes) > MAX_NODES:
        keep: list[str] = []
        uses_out: dict[str, list[str]] = defaultdict(list)
        for edge in entity_edges:
            if edge["kind"] == "uses":
                uses_out[edge["from"]].append(edge["to"])
        queue = [node["entity"] for node in nodes if node["main_result"]]
        seen_ids: set[str] = set()
        while queue and len(keep) < MAX_NODES:
            current_id = queue.pop(0)
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)
            keep.append(current_id)
            queue.extend(sorted(uses_out.get(current_id, ())))
        rest = [node["entity"] for node in nodes if node["entity"] not in seen_ids]
        rest.sort(key=lambda identifier: (stmt_kind[identifier] not in PROVABLE,))
        keep.extend(rest[: MAX_NODES - len(keep)])
        kept = set(keep)
        truncated = {"statements": len(nodes), "kept": len(kept)}
        warnings.append(f"The manuscript has {len(nodes)} statements; the first {len(kept)} by priority are kept")
        nodes = [node for node in nodes if node["entity"] in kept]
        entity_edges = [edge for edge in entity_edges if edge["from"] in kept and edge["to"] in kept]

    assign_ids(nodes)
    ids = {node["entity"]: node["id"] for node in nodes}
    for node in nodes:
        node["nested_in"] = ids.get(node["nested_in"]) if node["nested_in"] else None
        for section in node["section"]:
            section.pop("_id", None)
    graph_edges = []
    for edge in entity_edges:
        user, used_id = ids.get(edge["from"]), ids.get(edge["to"])
        if not user or not used_id:
            continue
        if edge["kind"] == "order":
            graph_edges.append({"from": used_id, "to": user, "label": "uses", "provenance": "inferred",
                                "context": "proof", "basis": "document-order", "strength": 0, "weak": True})
            continue
        where = edge["where"]
        nested = "nested-claim" in where
        context = "proof" if any(item.startswith("proof") for item in where) or nested else "statement"
        basis = "nested-claim" if nested and len(where) == 1 else (
            "prose-proof" if "proof-prose" in where else (
                "equation-ref" if all("+eq" in item for item in where if item != "nested-claim") else "label-ref"))
        graph_edges.append({"from": used_id, "to": user, "label": "uses",
                            "provenance": "inferred" if basis == "nested-claim" else "ref",
                            "context": context, "basis": basis, "strength": edge["strength"],
                            "labels": edge["labels"][:12]})
    graph_edges.sort(key=lambda edge: (edge["to"], edge["from"]))

    root_text = texts[file_order[0]]
    title = ""
    title_match = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*", COMMENT_LINE_RE.sub("", root_text))
    if title_match:
        body, _ = _braced(COMMENT_LINE_RE.sub("", root_text), title_match.end())
        title = _plain(body or "", 300)
    abstract = ""
    for name in file_order:
        found = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", COMMENT_LINE_RE.sub("", texts[name]), re.S)
        if found:
            abstract = collapse(found.group(1))[:MAX_ABSTRACT_CHARS]
            break
    sections_out = []
    for entity in sorted(section_entities, key=okey):
        sections_out.append({"level": entity.get("subkind"), "title": _plain(entity.get("title") or "", 160),
                             "number": numbers.get(entity["id"]), "file": ns_file.get(entity["ns"]),
                             "line": point(entity, "_s")[0] + 1,
                             # So prose that cites "Section~\ref{sec:x}" can name the section.
                             "labels": sorted(labels_by_owner.get(("section", entity["id"]), []))[:8]})
    intro = ""
    intro_section = next((entity for entity in sorted(section_entities, key=okey)
                          if re.search(r"intro", entity.get("title") or "", re.I)), None)
    if intro_section is None:
        intro_section = next(iter(sorted(section_entities, key=okey)), None)
    if intro_section is not None:
        intro = COMMENT_LINE_RE.sub("", source_slice(intro_section))[:MAX_INTRO_CHARS].strip()
    stats = {
        "statements": len(nodes),
        "statements_scanned": scanned_total,
        "statement_kinds": dict(Counter(node["kind"] for node in nodes)),
        "proof_envs": len(proofs),
        "proofs_attached": len(proof_of),
        "proofs_unattached": unattached,
        "proof_attach_modes": dict(Counter(proof_how.values())),
        "prose_proofs": len(prose_proved),
        "uses_edges": sum(1 for edge in graph_edges if not edge.get("weak")),
        "order_fallback_edges": sum(1 for edge in graph_edges if edge.get("weak")),
        "cycle_edges_dropped": len(dropped),
        "label_defs": len(label_owner),
        "refs_dangling": len(dangling),
        "refs_outside_statements": sum(mentions.values()),
        "numbered": sum(1 for node in nodes if node["number"]),
        "external_results": sum(1 for node in nodes if node["external"]),
    }
    for node in nodes:
        node.pop("entity", None)
    return {
        "schema": SCHEMA,
        "format": "tex",
        "parser": frontend,
        "root_file": file_order[0],
        "files": file_order,
        "missing_includes": sorted(set(missing))[:50],
        "file_errors": file_errors,
        "title": title,
        "abstract": abstract,
        "intro": intro,
        "sections": sections_out[:400],
        "theorem_decls": decls,
        "macros": extract_macros(all_text),
        "bibliography": dict(sorted(bibliography.items())[:2000]),
        "nodes": nodes,
        "edges": graph_edges,
        "dropped_for_acyclicity": dropped[:50],
        "truncated": truncated,
        "warnings": warnings,
        "stats": stats,
        "bytes": total,
    }


def parser_id() -> str:
    try:
        import pylatexenc

        return f"pylatexenc-{pylatexenc.__version__}"
    except ImportError:  # pragma: no cover - checked by run_sandboxed
        return "pylatexenc-missing"


def parse_project(project: Path, *, root_file: str | None = None) -> dict[str, Any]:
    """In-process parse of a bundle folder (see the module docstring)."""
    project = Path(project).resolve()
    if root_file:
        root = (project / root_file)
        if not _inside(root, project) or not root.is_file():
            raise LatexStructureError("The selected root file is not in the bundle")
    else:
        root = find_root(project)
    if root is None:
        raise LatexStructureError("No TeX file was found in the source")
    return build(project, root.resolve(), frontend=parser_id())


# ---------------------------------------------------------------------------
# sandboxed subprocess
# ---------------------------------------------------------------------------


def _limits() -> None:  # pragma: no cover - runs in the child
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (SANDBOX_MEMORY_BYTES, SANDBOX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (SANDBOX_CPU_SECONDS, SANDBOX_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def sandbox_paths() -> list[str]:
    """The import path of a sandboxed child: this repository, then the
    caller's ``PYTHONPATH`` entries (the venv's site-packages come from the
    interpreter itself)."""
    repository = str(Path(__file__).resolve().parents[2])
    inherited = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    return [repository, *[part for part in inherited if part != repository]]


def sandbox_env() -> dict[str, str]:
    """A minimal child environment: no credentials, only what imports need."""
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(sandbox_paths()),
    }


# The child runs isolated (-I: no PYTHON* variables, no user site and no
# script or working directory on sys.path), from an empty working directory;
# the import path is passed explicitly.  Nothing in a run workspace can
# shadow a module the parser imports.
_BOOTSTRAP = (
    "import json, runpy, sys\n"
    "paths, module = json.loads(sys.argv[1]), sys.argv[2]\n"
    "sys.path[:0] = [path for path in paths if isinstance(path, str)]\n"
    "sys.argv = [module, *sys.argv[3:]]\n"
    "runpy.run_module(module, run_name='__main__', alter_sys=True)\n"
)


def run_module_sandboxed(module: str, args: list[str], *, timeout: float, limits: Any = None,
                         cancel: threading.Event | None = None) -> subprocess.CompletedProcess:
    """Run ``module`` as ``__main__`` with ``args`` in an isolated child under
    the parser resource limits (``limits`` sets them in the child).

    Raises :class:`subprocess.TimeoutExpired` after ``timeout`` seconds and
    :class:`SandboxCancelled` as soon as ``cancel`` is set; the child's
    process group is killed in both cases.
    """
    command = [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, json.dumps(sandbox_paths()), module, *args]
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="analyze-sandbox-") as cwd:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sandbox_env(),
            cwd=cwd,
            preexec_fn=limits or _limits,
            start_new_session=True,
        )
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise SandboxCancelled("stopped")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(command[:3], timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.25, left))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.communicate()
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def run_sandboxed(project: Path, out_path: Path, *, root_file: str | None = None,
                  timeout: float = SANDBOX_WALL_SECONDS, cancel: threading.Event | None = None) -> dict[str, Any]:
    """Parse ``project`` in a resource-limited subprocess; return the document.

    Raises :class:`LatexStructureError` with a public-safe message when the
    parse fails, times out or is killed by its limits
    (:class:`ParserUnavailable` when pylatexenc is missing), and
    :class:`SandboxCancelled` when ``cancel`` is set.
    """
    args = [str(Path(project).resolve()), str(Path(out_path).resolve())]
    if root_file:
        args += ["--root", root_file]
    try:
        completed = run_module_sandboxed("agent_monitor.analyze.latex_structure", args, timeout=timeout,
                                         cancel=cancel)
    except subprocess.TimeoutExpired:
        raise LatexStructureError("Parsing the LaTeX source took too long") from None
    if completed.returncode == 4:
        raise ParserUnavailable("The LaTeX parser (pylatexenc) is not installed on this server")
    if completed.returncode == 3:
        message = completed.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["No TeX structure"]
        raise LatexStructureError(message[0][:200])
    if completed.returncode != 0:
        raise LatexStructureError("The LaTeX parser stopped (resource limit or malformed input)")
    try:
        raw = Path(out_path).read_bytes()
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError
        document = json.loads(raw)
    except (OSError, ValueError):
        raise LatexStructureError("The LaTeX parser produced no usable result") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise LatexStructureError("The LaTeX parser produced no usable result")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project")
    parser.add_argument("out")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    sys.setrecursionlimit(20000)
    threading.stack_size(256 * 1024 * 1024)
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["document"] = parse_project(Path(args.project), root_file=args.root)
        except LatexStructureError as exc:
            outcome["error"] = str(exc)
        except RecursionError:
            outcome["error"] = "The LaTeX source is nested too deeply"
        except ImportError:
            outcome["missing"] = True

    thread = threading.Thread(target=work, name="latex-structure")
    thread.start()
    thread.join()
    if outcome.get("missing"):
        print("pylatexenc is not installed", file=sys.stderr)
        return 4
    if "error" in outcome or "document" not in outcome:
        print(outcome.get("error") or "The LaTeX source could not be parsed", file=sys.stderr)
        return 3
    payload = json.dumps(outcome["document"], ensure_ascii=False, sort_keys=False)
    target = Path(args.out)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8", errors="replace")
    os.replace(temporary, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
