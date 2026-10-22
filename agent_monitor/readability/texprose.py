"""Text-mode LaTeX left in a readable version, written as Markdown.

A rewrite is asked for Markdown with LaTeX only inside math.  A model that
keeps the manuscript's markup anyway ("\\begin{lemma}[Coding] ...
\\end{lemma}", "\\emph{valid}", "\\item") would leave it on screen as raw
text, so outside math (``parse.math_spans``) :func:`convert` writes it out:

* a theorem-like environment opens with its name in bold, "**Lemma 3.6
  (Coding).**" (the printed label of the result anchored just before it,
  else the environment's name), and its end closes the paragraph;
* ``proof`` opens with "*Proof.*" (or its optional title) and ends with "∎";
* ``itemize``, ``enumerate`` and ``description`` become Markdown lists;
* ``\\emph`` and ``\\textit`` become emphasis, ``\\textbf`` strong
  emphasis, ``\\text``-like wrappers and ``\\url`` and ``\\href`` their
  text, and ``\\footnote{x}`` " (x)";
* ``\\section`` and its kin become headings, ``\\paragraph`` a bold run-in;
* TeX quotes (two backquotes, text, two apostrophes) become “text”, a tie
  between words a space, ``\\S`` "§", ``\\ldots`` "…", and spacing
  commands (``\\medskip``, ``\\noindent``, a line-ending ``\\\\``) go.

Unknown environments and every other command are kept as written, and so is
math.  Every pattern is linear in the text.
"""
from __future__ import annotations

import re
from typing import Callable, Mapping

from agent_monitor.readability import parse

# Theorem-like environments by the names manuscripts give them.
THEOREM_NAMES = {
    "theorem": "Theorem", "thm": "Theorem", "lemma": "Lemma", "lem": "Lemma",
    "proposition": "Proposition", "prop": "Proposition", "corollary": "Corollary", "cor": "Corollary",
    "definition": "Definition", "defn": "Definition", "dfn": "Definition", "defi": "Definition",
    "remark": "Remark", "rem": "Remark", "rmk": "Remark", "example": "Example", "exa": "Example",
    "claim": "Claim", "conjecture": "Conjecture", "conj": "Conjecture", "observation": "Observation",
    "fact": "Fact", "notation": "Notation", "assumption": "Assumption", "hypothesis": "Hypothesis",
    "problem": "Problem", "question": "Question", "exercise": "Exercise", "convention": "Convention",
    "construction": "Construction", "axiom": "Axiom", "property": "Property", "condition": "Condition",
}
# A list item's marker; an item's text starts where its marker ends, which
# is where a list nested in it must be indented to.
_LIST_MARKERS = {"itemize": "- ", "enumerate": "1. ", "description": "- "}

# Math is set aside as a token while the text around it is rewritten.
_TOKEN_RE = re.compile("\ue000(\\d{1,7})\ue001")
_PRIVATE_RE = re.compile("[\ue000\ue001]")
_STRUCTURE_RE = re.compile(
    r"\\begin[ \t]*+\{(?P<begin>[A-Za-z]{1,30})\*?\}(?:[ \t]*+\[(?P<option>[^\[\]\n]{0,300})\])?"
    r"|\\end[ \t]*+\{(?P<end>[A-Za-z]{1,30})\*?\}"
    r"|\\item(?![A-Za-z])(?:[ \t]*+\[(?P<label>[^\[\]\n]{0,200})\])?"
)
# The rewrite's anchor of a result right before an environment, or right
# after its opening (where it moves to the front).
_ANCHOR = r"\[\[\s*result\s*:\s*([A-Za-z0-9][A-Za-z0-9:_-]{0,79})\s*\]\]"
_ANCHOR_BEFORE_RE = re.compile(_ANCHOR + r"[ \t\n]*+$")
_ANCHOR_AFTER_RE = re.compile(r"[ \t\n]*+" + _ANCHOR)
_WRAP_RE = re.compile(
    r"\\(?P<cmd>emph|textit|textsl|textbf|textrm|textsf|textup|textnormal|textmd|textsc|texttt|text|mbox"
    r"|underline|url|footnote)[ \t]*+\{(?P<body>[^{}]{0,4000})\}"
)
_HREF_RE = re.compile(r"\\href[ \t]*+\{[^{}\n]{0,1000}\}[ \t]*+\{(?P<body>[^{}]{0,1000})\}")
_SECTION_RE = re.compile(
    r"\\(?P<level>section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"(?:[ \t]*+\[[^\[\]\n]{0,300}\])?[ \t]*+\{(?P<title>[^{}\n]{1,300})\}[ \t]*+"
)
_HEADING_LEVELS = {"section": "## ", "subsection": "### ", "subsubsection": "#### "}
_QUOTE_RE = re.compile(r"``(?P<quoted>[^`'\n]{1,400})''")
# A tie between words ("Lemma~3", "Section~$\ref{x}$"); "~5" after a space
# ("about ~5") and "~~" (strikethrough) are not ties.
_TIE_RE = re.compile("(?<=[A-Za-z0-9.,;:)\\]])~(?=[A-Za-z0-9(\\[\ue000§\\\\])")
_SECTION_SIGN_RE = re.compile(r"\\S(?![A-Za-z])[ \t~]*+")
_DOTS_RE = re.compile(r"\\(?:ldots|dots|textellipsis)(?![A-Za-z])(?:\{\})?")
_SPACING_RE = re.compile(
    r"\\(?:medskip|smallskip|bigskip|noindent|indent|newpage|clearpage|centering|hfill|vfill)(?![A-Za-z])[ \t]*+"
    r"|\\[vh]space\*?[ \t]*+\{[^{}\n]{0,40}\}[ \t]*+"
)
# Each starts where a run of spaces does, so a long run is read once.
_PAR_RE = re.compile(r"(?<![ \t])[ \t]*+\\par(?![A-Za-z])[ \t]*+")
_QUAD_RE = re.compile(r"\\q?quad(?![A-Za-z])")
# "\\" (with an optional "[2pt]") at the end of a line of prose.
_LINE_BREAK_RE = re.compile(r"(?<![ \t])[ \t]*+\\\\(?:\[[^\[\]\n]{0,20}\])?[ \t]*+(?=\n|$)")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _emphasis(body: str, mark: str) -> str:
    """``body`` between ``mark``s, outside the spaces around it (a "* x *"
    is no emphasis in Markdown); as it is when it cannot be emphasised."""
    inner = body.strip()
    if not inner or "*" in inner or inner.startswith("_") or inner.endswith("_"):
        return body
    start = body.index(inner[0])
    return f"{body[:start]}{mark}{inner}{mark}{body[start + len(inner):]}"


def _wrapped(match: "re.Match[str]") -> str:
    command, body = match.group("cmd"), match.group("body")
    if command in ("emph", "textit", "textsl"):
        return _emphasis(body, "*")
    if command == "textbf":
        return _emphasis(body, "**")
    if command == "footnote":
        return f" ({body.strip()})" if body.strip() else ""
    return body


def _section(match: "re.Match[str]") -> str:
    title = " ".join(match.group("title").split())
    level = match.group("level")
    if level in _HEADING_LEVELS:
        return f"\n\n{_HEADING_LEVELS[level]}{title}\n\n"
    return f"\n\n**{title.rstrip('.')}.** "


def _header(name: str, option: str | None, printed: str | None) -> str:
    """The bold run-in name of a statement: the anchored result's printed
    label when it names the same kind ("Lemma 3.6"), else ``name``; the
    optional title follows in parentheses unless the label has it."""
    title = " ".join((option or "").split())
    head = name
    if printed and printed.split(" ", 1)[0].lower() == name.lower():
        head = printed
    if title and "(" not in head:
        head = f"{head} ({title})"
    return f"**{head.rstrip('.')}.**"


def _roman(number: int) -> str:
    out = ""
    for value, digits in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while number >= value:
            out += digits
            number -= value
    return out


def _alph(number: int) -> str:
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


_COUNTERS = {
    "arabic": str, "roman": _roman, "Roman": lambda number: _roman(number).upper(),
    "alph": _alph, "Alph": lambda number: _alph(number).upper(),
}
_ENUMITEM_LABEL_RE = re.compile(r"label\s*=\s*\{?([^,{}]{0,40}?)\}?\s*(?:,|$)")
_ENUMITEM_COUNTER_RE = re.compile(r"\\(arabic|roman|Roman|alph|Alph)\*")
_SHORT_COUNTER_RE = re.compile(r"(?<![A-Za-z])([1iIaA])(?![A-Za-z])")


def _item_label(option: str | None) -> "Callable[[int], str] | None":
    """How an ``enumerate`` with this option numbers its items: "(i)" and
    "a)" (enumerate's short form) or "label=(\\roman*)" (enumitem's), else
    ``None`` (plain numbers)."""
    option = " ".join((option or "").split())
    if not option:
        return None
    keyed = _ENUMITEM_LABEL_RE.search(option)
    if keyed:
        template = keyed.group(1)
        counter = _ENUMITEM_COUNTER_RE.search(template)
        if not counter:
            return None
        head, tail = template[:counter.start()], template[counter.end():]
        return lambda number: f"{head}{_COUNTERS[counter.group(1)](number)}{tail}"
    if "=" in option:
        return None
    short = _SHORT_COUNTER_RE.search(option)
    if not short or len(option) > 12:
        return None
    head, tail = option[:short.start()], option[short.end():]
    kind = {"1": "arabic", "i": "roman", "I": "Roman", "a": "alph", "A": "Alph"}[short.group(1)]
    return lambda number: f"{head}{_COUNTERS[kind](number)}{tail}"


class _List:
    """An open list: its kind, and for ``enumerate`` how its items are numbered."""

    def __init__(self, kind: str, option: str | None, labelled: bool, bullet: str = "-") -> None:
        self.kind = kind
        # Lists next to each other alternate "-" and "+": Markdown would
        # read two lists with one bullet as one.
        self.bullet = bullet
        self.count = 0
        self.number = _item_label(option) if kind == "enumerate" else None
        # Numbers written by hand when the manuscript names them (or names
        # some items): one Markdown list, never "1." beside "(b)".
        self.bullets = kind != "enumerate" or self.number is not None or labelled

    @property
    def marker(self) -> str:
        return f"{self.bullet} " if self.bullets else "1. "

    def item(self, label: str) -> str:
        if self.kind == "description":
            return f"{self.marker}**{label}** " if label else self.marker
        if self.kind != "enumerate" or label:
            return f"{self.marker}{label} " if label else self.marker
        self.count += 1
        if not self.bullets:
            return self.marker
        # An escaped dot ("1\\.") is no ordered list nested in the bullet.
        return f"{self.marker}{self.number(self.count) if self.number else str(self.count) + chr(92) + '.'} "


def _labelled_lists(work: str) -> set[int]:
    """Where the ``enumerate`` environments that name one of their own items
    (``\\item[(b)]``) begin."""
    labelled: set[int] = set()
    stack: list[tuple[str, int]] = []
    for match in _STRUCTURE_RE.finditer(work):
        begin, end = match.group("begin"), match.group("end")
        if begin is not None and begin.lower() in _LIST_MARKERS:
            stack.append((begin.lower(), match.start()))
        elif end is not None and end.lower() in _LIST_MARKERS:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == end.lower():
                    del stack[index:]
                    break
        elif begin is None and end is None and match.group("label") is not None and stack:
            labelled.add(stack[-1][1])
    return labelled


def _structure(work: str, labels: Mapping[str, str]) -> str:
    labelled = _labelled_lists(work)
    out: list[str] = []
    lists: list[_List] = []
    top_lists = 0
    cursor = 0
    # After a header or an item marker, the space that ended its line goes.
    skip = False
    # A list's first item keeps the blank line that sets the list apart.
    opened = False

    def drop_trailing_space() -> None:
        while out and not out[-1].strip():
            out.pop()
        if out:
            out[-1] = out[-1].rstrip()

    for match in _STRUCTURE_RE.finditer(work):
        before = work[cursor:match.start()]
        out.append(before.lstrip(" \t\n") if skip else before)
        cursor = match.end()
        skip = False
        just_opened, opened = opened, False
        begin, end = match.group("begin"), match.group("end")
        if begin is not None:
            kind = begin.lower()
            if kind in THEOREM_NAMES:
                anchor = _ANCHOR_BEFORE_RE.search(work, max(0, match.start() - 200), match.start())
                after = None if anchor else _ANCHOR_AFTER_RE.match(work, match.end())
                if after:
                    out.append(f"\n\n{after.group(0).strip()}")
                    cursor = after.end()
                key = (anchor or after).group(1) if anchor or after else ""
                printed = labels.get(key) or labels.get(key.lower()) if key else None
                out.append("\n\n" + _header(THEOREM_NAMES[kind], match.group("option"), printed) + " ")
                skip = True
            elif kind == "proof":
                option = " ".join((match.group("option") or "").split()).rstrip(".")
                out.append(f"\n\n*{option or 'Proof'}.* ")
                skip = True
            elif kind in _LIST_MARKERS:
                if not lists:
                    top_lists += 1
                lists.append(_List(kind, match.group("option"), match.start() in labelled,
                                   "-" if len(lists) or top_lists % 2 else "+"))
                if len(lists) == 1:
                    out.append("\n\n")
                else:
                    drop_trailing_space()  # a nested list follows its item's line
                skip = opened = True
            else:
                out.append(match.group(0))
        elif end is not None:
            kind = end.lower()
            if kind in THEOREM_NAMES:
                out.append("\n\n")
            elif kind == "proof":
                drop_trailing_space()
                out.append(" ∎\n\n")
            elif kind in _LIST_MARKERS:
                drop_trailing_space()
                for index in range(len(lists) - 1, -1, -1):
                    if lists[index].kind == kind:
                        del lists[index:]
                        break
                out.append("\n\n" if not lists else "\n")
            else:
                out.append(match.group(0))
        else:
            label = " ".join((match.group("label") or "").split())
            current = lists[-1] if lists else _List("itemize", None, False)
            indent = " " * sum(len(open_list.marker) for open_list in lists[:-1])
            if not just_opened:
                drop_trailing_space()
            out.append(f"\n{indent}{current.item(label)}")
            skip = True
    rest = work[cursor:]
    out.append(rest.lstrip(" \t\n") if skip else rest)
    return "".join(out)


def _inline(work: str) -> str:
    # Innermost first: "\footnote{see \emph{x}}" needs two rounds.
    for _ in range(4):
        changed = _HREF_RE.sub(lambda match: match.group("body"), _WRAP_RE.sub(_wrapped, work))
        if changed == work:
            break
        work = changed
    work = _SECTION_RE.sub(_section, work)
    work = _QUOTE_RE.sub(lambda match: f"“{match.group('quoted')}”", work)
    work = _SECTION_SIGN_RE.sub("§", work)
    work = _DOTS_RE.sub("…", work)
    work = _SPACING_RE.sub("", work)
    work = _PAR_RE.sub("\n\n", work)
    work = _QUAD_RE.sub(" ", work)
    return _TIE_RE.sub(" ", work)


def convert(text: str, labels: Mapping[str, str] | None = None) -> str:
    """``text`` (Markdown with math) with its text-mode LaTeX written out as
    Markdown (see the module docstring); ``labels`` maps a result's id to its
    printed label."""
    if "\\" not in text and "``" not in text and "~" not in text:
        return text
    spans = parse.math_spans(text)
    maths: list[str] = []
    pieces: list[str] = []
    position = 0
    for start, end in spans:
        pieces.append(_PRIVATE_RE.sub("", text[position:start]))
        pieces.append(f"\ue000{len(maths)}\ue001")
        maths.append(text[start:end])
        position = end
    pieces.append(_PRIVATE_RE.sub("", text[position:]))
    work = "".join(pieces)
    converted = _inline(_structure(_LINE_BREAK_RE.sub("", work), labels or {}))
    if converted == work:
        return text
    # The text keeps the space it began and ended with, and no more.
    converted = text[:len(text) - len(text.lstrip())] + converted.strip() + text[len(text.rstrip()):]
    converted = _BLANK_RUN_RE.sub("\n\n", converted)
    return _TOKEN_RE.sub(lambda match: maths[int(match.group(1))], converted)
