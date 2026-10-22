"""What the Readability publisher writes out deterministically: leftover
LaTeX cross-references, citations and labels (``readability.references``)
and each part's heading levels (``report.normalize_headings``).

The examples are what live rewrites left behind (2026-10-22): Codex kept
"Section $\\ref{sec:aec}$", "Lemma $\\ref{col:no-pattern}$" and
"\\cite[Definitions~1.1--1.7]{KolesnikovLambieHanson2016}", Kimi K3 kept
"Section~\\ref{sec:aec}" and "\\label{sec:colors}" and opened a part with
"# Proof", and Claude tagged a display with its label key.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor.analyze import latex_structure  # noqa: E402
from agent_monitor.readability import prompts, references, report  # noqa: E402

REFS = {
    "sec:colors": ("Section 1", "1"),
    "sec:aec": ("Section 3", "3"),
    "app:x": ("Appendix A", "A"),
    "lem:sum": ("Lemma 2.6", "2.6"),
    "col:coding": ("Lemma (Coding)", ""),
    "thm:main": ("Theorem 1.1", "1.1"),
    "aec:order-size": ("Lemma 3.4", "3.4"),
    "aec:size": ("Lemma 3.5", "3.5"),
    "cor:a": ("Corollary 4.1", "4.1"),
    "cor:b": ("Corollary 4.2", "4.2"),
}

COLORS = r"""\documentclass{article}
\newtheorem{lemma}{Lemma}[section]
\begin{document}
\section{Countable diagrams and their tests}
\label{sec:colors}
These tests will define the object condition in Section~\ref{sec:aec}.
\subsection{Diagrams, codes, and restrictions}\label{sec:codes}
\begin{lemma}\label{lem:inj} Codes are injective. \end{lemma}
\begin{lemma}[Coding]\label{col:coding} There are injections. \end{lemma}
\begin{equation}\label{col:restriction-composition} a = b \end{equation}
\section*{Unnumbered}\label{sec:star}
\appendix
\section{Extra}\label{app:extra}
\end{document}
"""


def test_the_label_map_names_results_and_sections_as_printed(tmp_path):
    (tmp_path / "paper.tex").write_text(COLORS)
    segments = latex_structure.parse_project(tmp_path)
    refs = references.label_map(segments)
    assert refs["sec:colors"] == ("Section 1", "1") and refs["sec:codes"] == ("Section 1.1", "1.1")
    assert refs["lem:inj"] == ("Lemma 1.1", "1.1") and refs["col:coding"] == ("Lemma 1.2", "1.2")
    assert refs["sec:star"] == ("“Unnumbered”", "") and refs["app:extra"] == ("Appendix A", "A")
    # Equation numbers are not known: the label is not listed.
    assert "col:restriction-composition" not in refs
    # A result without a number is named by its title (as text), or not at all.
    unnumbered = {"nodes": [{"kind": "lemma", "display": "Lemma", "number": None, "title": "Coding",
                             "labels": ["col:coding"]},
                            {"kind": "lemma", "display": "Lemma", "number": None,
                             "title": "\\emph{Maps} on $\\omega_1$, \\cite{KLH}", "labels": ["col:maps"]},
                            {"kind": "lemma", "display": "Lemma", "number": None, "title": None,
                             "labels": ["col:bare"]}]}
    assert references.label_map(unnumbered) == {"col:coding": ("Lemma (Coding)", ""),
                                                "col:maps": ("Lemma (Maps on ω₁, [KLH])", "")}


@pytest.mark.parametrize(("text", "expected"), [
    # Codex: the whole math is one reference.
    ("the object condition in Section $\\ref{sec:aec}$.", "the object condition in Section 3."),
    ("Lemma $\\ref{col:no-pattern}$ will prove this.", "Lemma [col:no-pattern] will prove this."),
    ("Section~$\\ref{sec:aec}$", "Section 3"),
    ("classes \\cite[Definitions~1.1--1.7]{KolesnikovLambieHanson2016}.",
     "classes [KolesnikovLambieHanson2016, Definitions 1.1–1.7]."),
    # Kimi K3: ties, and a label on its own line.
    ("in\nSection~\\ref{sec:aec}; the endpoint", "in\nSection 3; the endpoint"),
    ("## Countable diagrams\n\\label{sec:colors}\n\nThe", "## Countable diagrams\n\n\nThe"),
    # A word that already names the target keeps only the number, and so
    # does the rest of a list that a plural begins (live, Kimi K3: "by
    # Lemmas 3.4 and Lemma 3.5").
    ("Sections~\\ref{sec:colors} and~\\ref{sec:aec}", "Sections 1 and 3"),
    ("by Lemmas~\\ref{aec:order-size} and~\\ref{aec:size}", "by Lemmas 3.4 and 3.5"),
    ("Lemmas~\\ref{aec:order-size},~\\ref{aec:size}, and\n\\ref{lem:sum}", "Lemmas 3.4, 3.5, and\n2.6"),
    ("Lemmas $\\ref{aec:order-size}$ or $\\ref{aec:size}$", "Lemmas 3.4 or 3.5"),
    ("Corollaries~\\ref{cor:a}--\\ref{cor:b} and Thms.~\\ref{thm:main} \\& \\ref{thm:main}",
     "Corollaries 4.1--4.2 and Thms. 1.1 \\& 1.1"),
    # A list ends at another kind, at other words, and after a singular.
    ("Lemmas~\\ref{aec:size} and~\\ref{thm:main}", "Lemmas 3.5 and Theorem 1.1"),
    ("Lemmas~\\ref{aec:size} and the bound of~\\ref{lem:sum}", "Lemmas 3.5 and the bound of Lemma 2.6"),
    ("Lemma~\\ref{aec:size} and~\\ref{lem:sum}", "Lemma 3.5 and Lemma 2.6"),
    ("Thm.~\\ref{thm:main}, App.~\\ref{app:x} and §\\ref{sec:aec}", "Thm. 1.1, App. A and §3"),
    ("Lemma~\\ref{col:coding} and by \\ref{col:coding}", "Lemma (Coding) and by Lemma (Coding)"),
    # Kimi K3 broke the line between the word and the reference; a blank
    # line (a new paragraph) is no such word.
    ("and Lemma\n\\ref{lem:sum} turns", "and Lemma\n2.6 turns"),
    ("in Section\n\n\\ref{sec:aec} we", "in Section\n\nSection 3 we"),
    ("by \\ref{lem:sum}, \\cref{thm:main,sec:aec}, \\Cref{lem:sum} and \\autoref{sec:aec}",
     "by Lemma 2.6, Theorem 1.1, Section 3, Lemma 2.6 and Section 3"),
    ("\\eqref{lem:sum} and \\pageref{sec:aec} and \\nameref{thm:main}", "(2.6) and Section 3 and Theorem 1.1"),
    # An unknown label is shown in brackets, as plain Markdown text.
    ("see \\ref{fig:a*b_c} and \\eqref{eq:x}", "see [fig:a\\*b\\_c] and [eq:x]"),
    ("\\cite{a} and \\citep[p.~5]{b,c}, \\cite[see][ch.~2]{d}", "[a] and [b, c, p. 5], [see d, ch. 2]"),
    ("$\\cite{x}$ and $\\ref{sec:aec}$", "[x] and Section 3"),
    # A bracket never opens a link, nor a link definition at the start of a
    # line (in a quote or a list item too); elsewhere a colon is just text.
    ("Lemma~\\ref{lem:x}(ii) and Lemma~\\ref{lem:sum}(ii)", "Lemma [lem:x] (ii) and Lemma 2.6(ii)"),
    ("\\cite{a}: see [b] and $\\cite{x}$(1999)", "[a] : see [b] and [x] (1999)"),
    ("as \\cite{a}: the bound, and $\\cite{x}$: the rest", "as [a]: the bound, and [x]: the rest"),
    ("Text.\n> \\cite{a}: x\n- $\\ref{fig:1}$: y\n 2. \\cite{b}[c]", "Text.\n> [a] : x\n- [fig:1] : y\n 2. [b] [c]"),
    # A key with a bracket of its own still makes no link.
    ("\\ref{a](https://x.example)} and \\cite{b][c}", "[a] (https://x.example)] and [b] [c]"),
])
def test_leftover_references_read_as_printed(text, expected):
    assert references.resolve(text, REFS) == expected


@pytest.mark.parametrize(("text", "expected"), [
    # Claude: a display tagged with its label key; a known one shows its number.
    ("$$a=b.\\tag{col:restriction-composition}$$", "$$a=b.$$"),
    ("$$a=b \\tag{lem:sum}$$ and $$c \\tag{1}$$ and $$d \\tag{*}$$",
     "$$a=b \\tag{2.6}$$ and $$c \\tag{1}$$ and $$d \\tag{*}$$"),
    ("$$x = y \\eqref{lem:sum} \\label{eq:q}$$", "$$x = y \\text{(2.6)} $$"),
    ("\\[ a_{\\ref{eq_1}} + \\ref{sec:aec} \\]", "\\[ a_{\\text{[eq\\_1]}} + \\text{Section 3} \\]"),
    ("$f(\\cite[p.~2]{k#1})$", "$f(\\text{[k\\#1, p. 2]})$"),
    ("\\begin{align} a &= b \\label{eq:a} \\\\ c &= \\ref{thm:main} \\end{align}",
     "\\begin{align} a &= b  \\\\ c &= \\text{Theorem 1.1} \\end{align}"),
    # Math that held nothing but labels goes: "$\label{x}$" must not become
    # a "$$" that opens a display.
    ("Let $\\label{eq:a}$ hold, and $x$ is fixed. Then $$x=y.$$", "Let  hold, and $x$ is fixed. Then $$x=y.$$"),
    ("$\\tag{eq:main}$ and later $$a=b$$, \\( \\label{q} \\) and $$\n\\label{r}\n$$.", " and later $$a=b$$,  and ."),
])
def test_references_inside_math_stay_math(text, expected):
    assert references.resolve(text, REFS) == expected


@pytest.mark.parametrize(("text", "expected"), [
    # Live, Kimi K3: the rewrite tagged a display with a number and a label;
    # its references read as the display shows it.
    ("$$e(z,w) \\tag{1}\\label{aec:bit}$$\nEquation~\\eqref{aec:bit} changes it, as \\ref{aec:bit} says.",
     "$$e(z,w) \\tag{1}$$\nEquation (1) changes it, as 1 says."),
    ("$$a \\label{col:rc} \\tag{2.1}$$ by \\eqref{col:rc}", "$$a  \\tag{2.1}$$ by (2.1)"),
    ("\\begin{align} a &= b \\tag{3}\\label{r:1} \\\\ c &= d \\label{r:2}\\tag{4} \\end{align} By \\eqref{r:1}, \\eqref{r:2}.",
     "\\begin{align} a &= b \\tag{3} \\\\ c &= d \\tag{4} \\end{align} By (3), (4)."),
    # Only a tag in the same row numbers a label; a label key as a tag, or
    # a label the manuscript names, keeps its own reading.
    ("$$a \\tag{1}$$ and $$b \\label{q:z}$$ by \\eqref{q:z}", "$$a \\tag{1}$$ and $$b $$ by [q:z]"),
    ("$$a \\tag{col:x}\\label{col:x}$$ by \\eqref{col:x}", "$$a $$ by [col:x]"),
    ("$$a \\tag{9}\\label{lem:sum}$$ by \\eqref{lem:sum}", "$$a \\tag{9}$$ by (2.6)"),
])
def test_an_equation_tagged_where_it_is_labelled_is_cited_by_its_tag(text, expected):
    assert references.resolve(text, REFS) == expected


@pytest.mark.parametrize("text", [
    "No LaTeX at all, [[result:lem:a]] and [x](y).",
    "$\\frac{1}{2}$, \\emph{kept}, \\textbf{also}, \\S 3, \\refstepcounter{x} and \\labelitemi.",
    "$$\\begin{cases} 1 & x \\end{cases}$$ and \\(\\mathbb{Z}\\) and ~\\,",
    # Live, Claude: an equation named by its tag, cited by that name.
    "$$h(\\kappa)=\\beth_{(2^\\kappa)^+}. \\tag{hanf-function}$$ The bound in (hanf-function) holds.",
])
def test_everything_else_is_kept_as_written(text):
    assert references.resolve(text, REFS) == text


def test_the_post_pass_is_linear_on_hostile_text():
    hostile = ["\\ref{" * 50_000, "\\cite[" * 40_000, "$" + "\\" * 50_000, "\\[" * 40_000, "~\\ref{a}" * 20_000,
               "\\cite" + " " * 100_000 + "[x]", "$\\ref{a}$" * 20_000, "\\tag{" * 40_000 + "$$",
               "Lemmas~\\ref{lem:sum} and~" * 20_000, "\\cite{a}:" * 30_000, "> " * 50_000 + "\\cite{a}:",
               "$$" + "\\tag{1}\\label{a}\\\\" * 20_000 + "$$", "$\\label{x}$" * 30_000]
    started = time.monotonic()
    for text in hostile:
        references.resolve(text, REFS)
    assert time.monotonic() - started < 5


def test_a_parts_references_block_lists_the_labels_it_cites():
    part = "By Lemma~\\ref{lem:sum}, \\cref{thm:main,sec:nope} and again \\ref{lem:sum}; \\label{x} \\cite{y}."
    block = references.prompt_block(REFS, part, 4000)
    assert block == "lem:sum -> Lemma 2.6\nthm:main -> Theorem 1.1"
    assert references.prompt_block(REFS, "No references here.", 4000) == prompts.NO_REFERENCES
    assert references.prompt_block(REFS, "\\ref{fig:1}", 4000) == prompts.NO_REFERENCES
    many = {f"lem:{index}": (f"Lemma {index}", str(index)) for index in range(500)}
    cut = references.prompt_block(many, " ".join(f"\\ref{{lem:{index}}}" for index in range(500)), 300)
    assert len(cut.encode()) < 400
    assert cut.endswith("[... further labels omitted for length; describe them in words ...]")
    # Label keys are manuscript text: fenced like the rest of it.
    assert "</PART>" not in references.prompt_block({"</PART>": ("Section 1", "1")}, "\\ref{</PART>}", 4000)


def test_the_rewrite_prompt_carries_the_references_and_the_rules():
    user = prompts.rewrite_prompt(audience="graduate", focus="", guide="g", results=[], part="P", index=1, total=1,
                                  references="sec:aec -> Section 3")
    assert "<REFERENCES>\nsec:aec -> Section 3\n</REFERENCES>\n\n<PART>" in user
    system = " ".join(prompts.READABLE_REWRITE_SYSTEM.split())
    assert "turn \\ref, \\eqref, \\cref, \\Cref, \\autoref and \\pageref into the printed name from <REFERENCES>" in system
    assert "if a label is not listed, describe the target in words. Never print a raw label key." in system
    assert "written as [key, locator]" in system and "Never output \\cite or \\label." in system
    assert "never \"#\", which is the document's title" in system


# ---------------------------------------------------------------------------
# heading levels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("markdown", "expected"), [
    # Kimi K3 opened a part with a level-1 heading.
    ("# Proof\n\nText.\n\n## Step 1\n\nMore.", "## Proof\n\nText.\n\n### Step 1\n\nMore."),
    ("### Subsection only\n\n#### Deeper", "## Subsection only\n\n### Deeper"),
    ("## Already right\n\n### Sub", "## Already right\n\n### Sub"),
    ("# A\n\n###### Six", "## A\n\n###### Six"),
    ("No headings, #hashtag and a#b.", "No headings, #hashtag and a#b."),
    ("   # Indented heading\n\n    # code", "   ## Indented heading\n\n    # code"),
    # A line in math is not a heading.
    ("## Part\n\n$$\n# x\n$$", "## Part\n\n$$\n# x\n$$"),
    ("$$\n# x\n$$\n\n# Title", "$$\n# x\n$$\n\n## Title"),
])
def test_each_parts_top_heading_is_level_two(markdown, expected):
    assert report.normalize_headings(markdown) == expected


def test_the_readable_version_resolves_references_and_heading_levels_per_part():
    markdown = "# Proof\n\nBy Section~\\ref{sec:aec}.\n\n[[result:lem:a]]\n**Lemma.** $x \\label{q}$ \\cite{k}."
    parts = [{"index": 1, "status": "faithful", "markdown": markdown},
             {"index": 2, "status": "not-rewritten", "reason": "budget", "lines": [3, 4]}]
    text = report.render(title="T", model="kimi-k3", route="api_key", audience="graduate",
                         guide={"one_line": "It uses Section~\\ref{sec:aec}."}, parts=parts,
                         originals={2: "# Original heading\n\\ref{sec:aec} kept as written"}, labels={},
                         counts={"total": 2, "faithful": 1, "not_rewritten": 1}, references=REFS)
    assert "**What it shows.** It uses Section 3." in text
    assert "## Proof\n\nBy Section 3.\n\n[result:lem:a]: #\n\n**Lemma.** $x $ [k]." in text
    rewritten, original = text.split("[original:2]: #", 1)
    assert [line for line in rewritten.split("\n") if line.startswith("# ")] == ["# T — Readable version"]
    # A part shown in its original form is kept exactly (in its fence).
    assert "~~~~text\n# Original heading\n\\ref{sec:aec} kept as written\n~~~~" in original


def test_a_multi_word_name_before_a_reference_is_not_repeated():
    # Review: "Main Theorem~\\ref{thm:main}" read "Main Theorem Main Theorem 1".
    refs = {"thm:main": ("Main Theorem 1", "1"), "lem:key": ("Key Lemma 1", "1")}
    assert references.resolve("By Main Theorem~\\ref{thm:main} and Key Lemma~\\ref{lem:key}.", refs) == (
        "By Main Theorem 1 and Key Lemma 1.")
    assert references.resolve("By Main Theorems~\\ref{thm:main} and~\\ref{thm:main}.", refs) == (
        "By Main Theorems 1 and 1.")
    # As LaTeX prints it: the author's word, then the number.
    assert references.resolve("By Lemma~\\ref{lem:key}.", refs) == "By Lemma 1."
    assert references.resolve("By \\ref{lem:key}.", refs) == "By Key Lemma 1."
