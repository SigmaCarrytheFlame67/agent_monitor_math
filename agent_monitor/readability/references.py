"""Cross-references and citations in a readable version.

Each rewrite gets a ``<REFERENCES>`` block (:func:`prompt_block`): every
label its part cites, with the name the manuscript prints for it
("sec:aec -> Section 3", "lem:x -> Lemma 2.6"), from the segmenter's
labelled results and sections (:func:`label_map`; equation numbers are not
known to it).  What a rewrite or an answer still writes in LaTeX is written
out deterministically (:func:`resolve`):

* ``\\ref``, ``\\eqref``, ``\\cref``, ``\\Cref``, ``\\autoref``, ``\\pageref``
  and ``\\nameref`` become the printed name ("Section 3"; only the number
  after a word that already names it, as in "Section~\\ref{sec:aec}", and in
  the rest of a list such a plural begins: "Lemmas 3.4 and 3.5"; "(3)" for
  ``\\eqref``), else the label in brackets ("[sec:aec]");
* an equation's label has a number only where the same text tags its
  display ("\\tag{2.1}\\label{x}" in one row: "(2.1)" for ``\\eqref{x}``);
* ``\\cite[x]{a,b}`` becomes "[a, b, x]" (``\\cite[p][x]{a}``: "[p a, x]");
* ``\\label{...}`` is dropped.

Inside math the replacement is ``\\text{...}``, except that math holding
nothing but one reference or citation becomes plain text, and math left
empty goes; a ``\\tag`` that names a label shows its number, or is dropped
when the label has none.  A bracket written here never opens a Markdown
link, nor a link definition at the start of a line.  Everything else is
kept as written.  Math is found as the console finds it
(``parse.math_spans``), and every pattern is linear in the text.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from agent_monitor.analyze import latex_structure
from agent_monitor.analyze.graph import section_ref_labels
from agent_monitor.analyze.prompts import fence, utf8_len
from agent_monitor.analyze.titles import LABEL_KINDS, plain_title
from agent_monitor.readability import parse
from agent_monitor.readability.prompts import NO_REFERENCES, printed

MAX_LABELS = 400
_KEY_RE = re.compile(r"[^\s{}\\]{1,120}")
_REF = r"\\(?P<cmd>ref|eqref|cref|Cref|autoref|pageref|nameref)\*?[ \t]*+\{(?P<keys>[^{}\n]{1,300})\}"
_CITE = (r"\\(?P<cite>[Cc]ite[a-z]{0,8}|parencite|textcite|autocite|footcite)\*?"
         r"(?:[ \t]*+\[(?P<first>[^\[\]\n]{0,200})\])?(?:[ \t]*+\[(?P<second>[^\[\]\n]{0,200})\])?"
         r"[ \t]*+\{(?P<cites>[^{}\n]{1,500})\}")
_LABEL = r"\\label[ \t]*+\{[^{}\n]{0,300}\}"
_REF_RE = re.compile(_REF)
# A tie before a reference or a citation is the space it stands for.
_PROSE_RE = re.compile(rf"(?P<tie>~)?(?:{_REF}|{_CITE})|{_LABEL}")
_MATH_RE = re.compile(rf"{_REF}|{_CITE}|{_LABEL}|\\tag\*?[ \t]*+\{{(?P<tag>[^{{}}\n]{{1,120}})\}}")
_LONE_RE = re.compile(rf"[ \t\n]*+~?(?:{_REF}|{_CITE})[ \t\n]*+")
# The word a reference follows ("Section~", "Thm.~", "§"), on its line or at
# the end of the line before (a line break is a space; a blank line is not).
_WORD_BEFORE_RE = re.compile(r"(§|\\S|[A-Za-z]{1,20}\.?)[ \t~]*(?:\n[ \t]*)?\Z")
# Between the references of a list ("Lemmas~\ref{a}, \ref{b} and~\ref{c}").
_SPACE = r"[ \t~]*+(?:\n[ \t~]*+)?"
_LIST_GAP_RE = re.compile(rf"{_SPACE}(?:,|,?{_SPACE}(?:and|or)|\\?&|--|–){_SPACE}")
# A \tag that holds a label key ("col:restriction-composition"), not a number.
_TAG_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*:[^\s{}\\]+")
# A \tag and a \label in one row of a display.
_TAG_RE = re.compile(r"\\tag\*?[ \t]*+\{([^{}\n]{1,120})\}")
_LABEL_KEY_RE = re.compile(r"\\label[ \t]*+\{([^{}\n]{1,300})\}")
_MARKDOWN_SPECIAL_RE = re.compile(r"([`*_<>$|~])")
# A closing bracket that the next character makes part of a link, or of a
# link definition: "[a](b)", "[a][b]", "[a]: b".
_LINK_AFTER_RE = re.compile(r"\](?=[(\[:])")
# What may come before a link definition on its line: an indent, and quote
# and list markers.
_LINE_START_RE = re.compile(r"[ \t]*+(?:(?:>|[-*+]|\d{1,9}[.)])[ \t]*+)*+")
_TEX_TEXT = {"$": "\\$", "%": "\\%", "&": "\\&", "#": "\\#", "_": "\\_", "^": "\\textasciicircum{}",
             "~": "\\textasciitilde{}", "\\": ""}
_TEX_SPECIAL_RE = re.compile(r"[$%&#_^~\\]")


def label_map(segments: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """``{label: (printed name, number)}`` of the manuscript's labelled
    results ("Lemma 2.6", or "Lemma (Title)" without a number) and sections
    ("Section 3", "Appendix A", or the title in quotes); the number is ""
    when there is none."""
    refs: dict[str, tuple[str, str]] = {}
    for node in segments.get("nodes") or []:
        if not isinstance(node, Mapping):
            continue
        number = " ".join(str(node.get("number") or "").split())
        # A title is TeX ("\emph{Coding} of $\omega_1$, \cite{X}"): as text.
        title = plain_title(latex_structure._plain(resolve(str(node.get("title") or ""), {}), 400), 80)
        if number:
            name = printed(node)
        elif title:
            name = f"{printed(node)} ({title})"
        else:
            continue
        for label in node.get("labels") or []:
            if isinstance(label, str) and _KEY_RE.fullmatch(label) and len(refs) < MAX_LABELS:
                refs.setdefault(label, (name, number))
    for label, value in section_ref_labels(segments).items():
        if len(refs) < MAX_LABELS:
            refs.setdefault(label, (value["name"], "" if value["number"] == value["name"] else value["number"]))
    return refs


def prompt_block(refs: Mapping[str, tuple[str, str]], text: str, limit: int) -> str:
    """The ``<REFERENCES>`` of a part: each label ``text`` cites that the
    manuscript numbers, with its printed name, within ``limit`` bytes."""
    lines: list[str] = []
    seen: set[str] = set()
    used = 0
    for match in _REF_RE.finditer(text):
        for key in (key.strip() for key in match.group("keys").split(",")):
            if key in seen or key not in refs:
                continue
            seen.add(key)
            line = fence(f"{key} -> {refs[key][0]}")
            if used + utf8_len(line) + 1 > limit:
                return "\n".join(lines + ["[... further labels omitted for length; describe them in words ...]"])
            lines.append(line)
            used += utf8_len(line) + 1
    return "\n".join(lines) if lines else NO_REFERENCES


# ---------------------------------------------------------------------------
# the post-pass
# ---------------------------------------------------------------------------


def _plain(text: str) -> str:
    """A label key or a citation as Markdown text: no TeX escapes, no
    character that would start emphasis, code, math or HTML, and no bracket
    that would end a link ("a](b" reads "a] (b")."""
    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", _LINK_AFTER_RE.sub("] ", text.replace("\\", "")))


def _tex_text(text: str) -> str:
    """Text for ``\\text{...}`` in math."""
    return _TEX_SPECIAL_RE.sub(lambda match: _TEX_TEXT[match.group(0)], text)


def _note(text: str) -> str:
    """A citation's locator as text: ties are spaces, -- and --- dashes."""
    return " ".join(text.replace("~", " ").split()).replace("---", "—").replace("--", "–")


def _citation(match: "re.Match[str]", escape: Callable[[str], str]) -> str:
    keys = [key.strip() for key in match.group("cites").split(",") if key.strip()]
    first, second = match.group("first"), match.group("second")
    prenote, postnote = (first, second) if second is not None else (None, first)
    body = ", ".join(keys)
    if prenote and prenote.strip():
        body = f"{_note(prenote)} {body}"
    if postnote and postnote.strip():
        body = f"{body}, {_note(postnote)}"
    return f"[{escape(body)}]"


def _word(before: str) -> str:
    """The word in front of a reference (the end of ``before``), in lower
    case and written out ("section", "theorems" for "Thms.~", "§"), or ""."""
    found = _WORD_BEFORE_RE.search(before)
    if not found:
        return ""
    token = found.group(1).rstrip(".").lower()
    if token in ("§", "\\s"):
        return "§"
    if token not in LABEL_KINDS and token.endswith("s") and token[:-1] in LABEL_KINDS:
        return LABEL_KINDS[token[:-1]].lower() + "s"
    return LABEL_KINDS.get(token, token).lower()


def _names(word: str, head: str) -> str:
    """How ``word`` names a result whose printed name begins with ``head``:
    "one" ("Lemma~"), "many" ("Lemmas~", which a list goes on with), or
    ""."""
    head = head.lower()
    if word in ("§", head):
        return "one"
    return "many" if word == head + "s" or (head.endswith("y") and word == head[:-1] + "ies") else ""


def _reference(match: "re.Match[str]", refs: Mapping[str, tuple[str, str]], word: str,
               escape: Callable[[str], str]) -> tuple[str, str]:
    """A reference command as text, and the plural word that a list it
    begins goes on with ("lemmas" after "Lemmas~\\ref{a}"), or "";
    ``word`` is the word in front of it (:func:`_word`)."""
    command = match.group("cmd")
    out = []
    carry = ""
    for position, key in enumerate(key.strip() for key in match.group("keys").split(",")):
        if not key:
            continue
        if key not in refs:
            out.append(f"[{escape(key)}]")
            continue
        name, number = refs[key]
        head, _space, rest = name.partition(" ")
        names = _names(word, head)
        if command == "eqref":
            text = f"({number})" if number else name
        elif position == 0 and rest and names:
            # "Section~\ref{sec:aec}": the word is there already.
            text = number or rest
            carry = word if names == "many" else ""
        else:
            text = name
        out.append(escape(text))
    return ", ".join(out), carry


def _starts_line(text: str, start: int) -> bool:
    """Whether only an indent and quote or list markers come before
    ``start`` on its line, as far as a bounded look back shows."""
    return bool(_LINE_START_RE.fullmatch(text[max(0, start - 40):start].rpartition("\n")[2]))


def _unlinked(written: str, text: str, start: int, end: int) -> str:
    """``written`` (Markdown text in place of ``text[start:end]``) followed by
    a space where its closing bracket and the next character would make a
    link ("[lem:a](ii)", "[a][b]") or, at the start of a line, a link
    definition ("[a]: ...")."""
    following = text[end:end + 1]
    if written.endswith("]") and (following in ("(", "[") or following == ":" and _starts_line(text, start)):
        return written + " "
    return written


def _tagged(text: str, spans: list[tuple[int, int]],
            refs: Mapping[str, tuple[str, str]]) -> Mapping[str, tuple[str, str]]:
    """``refs`` and each label that a ``\\tag`` numbers in one row of a
    display of ``text`` ("\\tag{2.1}\\label{x}", in either order), so that
    ``\\eqref{x}`` reads "(2.1)" as the display shows it."""
    found: dict[str, tuple[str, str]] = {}
    for start, end in spans:
        if "\\tag" not in text[start:end]:
            continue
        for row in text[start:end].split("\\\\"):
            tags, keys = _TAG_RE.findall(row), _LABEL_KEY_RE.findall(row)
            if len(tags) == 1 and len(keys) == 1:
                tag, key = " ".join(tags[0].split()), keys[0].strip()
                if tag and not _TAG_KEY_RE.fullmatch(tag) and key not in refs:
                    found.setdefault(key, (tag, tag))
    return {**refs, **found} if found else refs


def _substitute(pattern: "re.Pattern[str]", text: str, start: int, end: int,
                replace: Callable[["re.Match[str]"], str]) -> str:
    pieces = []
    cursor = start
    for match in pattern.finditer(text, start, end):
        pieces.append(text[cursor:match.start()])
        pieces.append(replace(match))
        cursor = match.end()
    pieces.append(text[cursor:end])
    return "".join(pieces)


def _delimiter(text: str, start: int) -> int:
    """The length of a math span's opening delimiter (0: an environment)."""
    if text.startswith("$$", start) or text.startswith("\\[", start) or text.startswith("\\(", start):
        return 2
    return 1 if text.startswith("$", start) else 0


def resolve(text: str, refs: Mapping[str, tuple[str, str]]) -> str:
    """``text`` (Markdown with math) with its LaTeX references, citations
    and labels written out (see the module docstring)."""
    if "\\" not in text:
        return text
    spans = parse.math_spans(text)
    refs = _tagged(text, spans, refs)
    out: list[str] = []
    position = 0
    # Where the last reference written as text ended, and the plural word
    # that a list it began goes on with.
    listed_end, listed_word = 0, ""

    def as_text(match: "re.Match[str]", start: int, end: int) -> str:
        """A reference or a citation (``text[start:end]``) as Markdown text."""
        nonlocal listed_end, listed_word
        if match.group("cite"):
            return _unlinked(_citation(match, _plain), text, start, end)
        if not (listed_word and _LIST_GAP_RE.fullmatch(text, listed_end, start)):
            listed_word = _word(text[max(0, start - 40):start])
        written, listed_word = _reference(match, refs, listed_word, _plain)
        listed_end = end
        return _unlinked(written, text, start, end)

    def in_prose(match: "re.Match[str]") -> str:
        if match.group("cite") or match.group("cmd"):
            return (" " if match.group("tie") else "") + as_text(match, match.start(), match.end())
        return ""  # \label

    def in_math(match: "re.Match[str]") -> str:
        if match.group("cite"):
            return "\\text{" + _citation(match, _tex_text) + "}"
        if match.group("cmd"):
            return "\\text{" + _reference(match, refs, "", _tex_text)[0] + "}"
        tag = match.group("tag")
        if tag is not None:
            if tag in refs and refs[tag][1]:
                return "\\tag{" + _tex_text(refs[tag][1]) + "}"
            return "" if _TAG_KEY_RE.fullmatch(tag) else match.group(0)
        return ""  # \label

    for start, end in spans:
        out.append(_substitute(_PROSE_RE, text, position, start, in_prose))
        width = _delimiter(text, start)
        lone = _LONE_RE.fullmatch(text, start + width, end - width) if width else None
        if lone:
            # Math that is only a reference ("Section $\ref{sec:aec}$") is text.
            if out[-1].endswith("~"):
                out[-1] = out[-1][:-1] + " "
            out.append(as_text(lone, start, end))
        else:
            math = _substitute(_MATH_RE, text, start, end, in_math)
            if width and math != text[start:end] and not math[width:len(math) - width].strip():
                math = ""  # it held only labels: "$\label{x}$" is no "$$"
            out.append(math)
        position = end
    out.append(_substitute(_PROSE_RE, text, position, len(text), in_prose))
    return "".join(out)
