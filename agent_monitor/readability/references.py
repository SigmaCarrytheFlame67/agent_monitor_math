"""Cross-references and citations in a readable version.

Each rewrite gets a ``<REFERENCES>`` block (:func:`prompt_block`): every
label its part cites, with the name the manuscript prints for it
("sec:aec -> Section 3", "lem:x -> Lemma 2.6"), from the segmenter's
labelled results and sections (:func:`label_map`; equation numbers are not
known to it).  What a rewrite or an answer still writes in LaTeX is written
out deterministically (:func:`resolve`):

* ``\\ref``, ``\\eqref``, ``\\cref``, ``\\Cref``, ``\\autoref``, ``\\pageref``
  and ``\\nameref`` become the printed name ("Section 3"; only the number
  after a word that already names it, as in "Section~\\ref{sec:aec}";
  "(3)" for ``\\eqref``), else the label in brackets ("[sec:aec]");
* ``\\cite[x]{a,b}`` becomes "[a, b, x]" (``\\cite[p][x]{a}``: "[p a, x]");
* ``\\label{...}`` is dropped.

Inside math the replacement is ``\\text{...}``, except that math holding
nothing but one reference or citation becomes plain text; a ``\\tag`` that
names a label shows its number, or is dropped when the label has none.  A
bracket written in prose is never left to open a Markdown link.  Everything
else is kept as written.  Math is found as the console finds it
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
# A \tag that holds a label key ("col:restriction-composition"), not a number.
_TAG_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*:[^\s{}\\]+")
_MARKDOWN_SPECIAL_RE = re.compile(r"([`*_<>$|~])")
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
    """A label key or a citation as Markdown text: no TeX escapes, and no
    character that would start emphasis, code, math or HTML."""
    return _MARKDOWN_SPECIAL_RE.sub(r"\\\1", text.replace("\\", ""))


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


def _reference(match: "re.Match[str]", refs: Mapping[str, tuple[str, str]], before: str,
               escape: Callable[[str], str]) -> str:
    """A reference command as text; ``before`` is the text in front of it."""
    command = match.group("cmd")
    word = _WORD_BEFORE_RE.search(before)
    named = ""
    if word:
        token = word.group(1).rstrip(".").lower()
        named = "§" if token in ("§", "\\s") else LABEL_KINDS.get(token, token).lower()
    out = []
    for position, key in enumerate(key.strip() for key in match.group("keys").split(",")):
        if not key:
            continue
        if key not in refs:
            out.append(f"[{escape(key)}]")
            continue
        name, number = refs[key]
        head, _space, rest = name.partition(" ")
        if command == "eqref":
            text = f"({number})" if number else name
        elif position == 0 and rest and named in ("§", head.lower(), head.lower() + "s"):
            # "Section~\ref{sec:aec}": the word is there already.
            text = number or rest
        else:
            text = name
        out.append(escape(text))
    return ", ".join(out)


def _unlinked(written: str, text: str, end: int) -> str:
    """``written`` (Markdown text in place of ``text[..end]``) followed by a
    space where its closing bracket and the next character would make a link
    or a link definition ("[lem:a](ii)", "[a]: ...")."""
    return written + " " if written.endswith("]") and text[end:end + 1] in ("(", "[", ":") else written


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
    out: list[str] = []
    position = 0

    def in_prose(match: "re.Match[str]") -> str:
        lead = " " if match.group("tie") else ""
        if match.group("cite"):
            return lead + _unlinked(_citation(match, _plain), text, match.end())
        if match.group("cmd"):
            reference = _reference(match, refs, text[max(0, match.start() - 40):match.start()], _plain)
            return lead + _unlinked(reference, text, match.end())
        return ""  # \label

    def in_math(match: "re.Match[str]") -> str:
        if match.group("cite"):
            return "\\text{" + _citation(match, _tex_text) + "}"
        if match.group("cmd"):
            return "\\text{" + _reference(match, refs, "", _tex_text) + "}"
        tag = match.group("tag")
        if tag is not None:
            if tag in refs and refs[tag][1]:
                return "\\tag{" + _tex_text(refs[tag][1]) + "}"
            return "" if _TAG_KEY_RE.fullmatch(tag) else match.group(0)
        return ""  # \label

    for start, end in parse.math_spans(text):
        out.append(_substitute(_PROSE_RE, text, position, start, in_prose))
        width = _delimiter(text, start)
        lone = _LONE_RE.fullmatch(text, start + width, end - width) if width else None
        if lone:
            # Math that is only a reference ("Section $\ref{sec:aec}$") is text.
            if out[-1].endswith("~"):
                out[-1] = out[-1][:-1] + " "
            out.append(_unlinked(_citation(lone, _plain) if lone.group("cite")
                                 else _reference(lone, refs, text[max(0, start - 40):start], _plain), text, end))
        else:
            out.append(_substitute(_MATH_RE, text, start, end, in_math))
        position = end
    out.append(_substitute(_PROSE_RE, text, position, len(text), in_prose))
    return "".join(out)
