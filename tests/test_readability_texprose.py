"""Text-mode LaTeX left in a readable version, written out as Markdown
(``agent_monitor.readability.texprose``)."""
from __future__ import annotations

import time

import pytest

from agent_monitor.readability import report, texprose

convert = texprose.convert


def test_a_statement_environment_opens_with_its_printed_name():
    text = ("[[result:col:coding]]\n\\begin{lemma}[Coding]\nAssume CH. Then $\\iota_k$ is injective.\n"
            "\\end{lemma}\nNext paragraph.")
    assert convert(text, {"col:coding": "Lemma 3.6"}) == (
        "[[result:col:coding]]\n\n**Lemma 3.6 (Coding).** Assume CH. Then $\\iota_k$ is injective.\n\nNext paragraph.")
    # Without an anchor, or with one of another kind, the environment names it.
    assert convert("\\begin{definition}[Valid diagrams] A diagram is valid.\\end{definition}", {}) == (
        "**Definition (Valid diagrams).** A diagram is valid.")
    assert convert("[[result:x]]\n\\begin{lemma} A.\\end{lemma}", {"x": "Theorem 2"}).endswith("**Lemma.** A.")
    # A printed label that has the title already is not given it twice.
    assert "**Definition (Valid diagrams).**" in convert(
        "[[result:d]]\n\\begin{definition}[Valid diagrams] A.\\end{definition}", {"d": "Definition (Valid diagrams)"})
    # An anchor written just inside the environment moves in front of its name.
    assert convert("\\begin{lemma}[Coding]\n[[result:col:coding]]\nAssume CH.\n\\end{lemma}", {"col:coding": "Lemma 3.6"}) == (
        "[[result:col:coding]]\n\n**Lemma 3.6 (Coding).** Assume CH.")
    # Starred and abbreviated environment names.
    assert convert("\\begin{thm*} A. \\end{thm*}", {}) == "**Theorem.** A."


def test_a_proof_opens_with_proof_and_ends_with_a_box():
    assert convert("\\begin{proof}\nBy induction on $n$.\n\\end{proof}\nAfter.", {}) == (
        "*Proof.* By induction on $n$. ∎\n\nAfter.")
    assert convert("\\begin{proof}[Proof of Lemma 2.]\nClear.\\\\\n\\end{proof}", {}) == "*Proof of Lemma 2.* Clear. ∎"


def test_lists_become_markdown_lists():
    assert convert("Intro.\n\\begin{itemize}\n\\item one\n\\item two\n\\end{itemize}\nAfter.", {}) == (
        "Intro.\n\n- one\n- two\n\nAfter.")
    assert convert("\\begin{enumerate}\n\\item one\n\\item two\n\\end{enumerate}", {}) == "1. one\n1. two"
    # The manuscript's own labels: enumerate's short form and enumitem's.
    assert convert("\\begin{enumerate}[(i)]\n\\item a\n\\item b\n\\end{enumerate}", {}) == "- (i) a\n- (ii) b"
    assert convert("\\begin{enumerate}[label=(\\alph*)]\n\\item a\n\\item b\n\\end{enumerate}", {}) == (
        "- (a) a\n- (b) b")
    assert convert("\\begin{enumerate}[A.]\n\\item a\n\\item b\n\\end{enumerate}", {}) == "- A. a\n- B. b"
    # An item named by hand keeps the list whole: the others are numbered by hand.
    assert convert("\\begin{enumerate}\n\\item a\n\\item[(b)] b\n\\item c\n\\end{enumerate}", {}) == (
        "- 1\\. a\n- (b) b\n- 2\\. c")
    assert convert("\\begin{description}\n\\item[Term] meaning\n\\end{description}", {}) == "- **Term** meaning"


def test_nested_and_adjacent_lists_stay_apart():
    nested = convert("\\begin{enumerate}\n\\item one\n  \\begin{itemize}\n  \\item inner\n  \\end{itemize}\n"
                     "\\item two\n\\end{enumerate}", {})
    assert nested == "1. one\n   - inner\n1. two"
    # Two lists one after the other would be one list with the same bullet.
    adjacent = convert("\\begin{itemize}\n\\item a\n\\end{itemize}\n\\begin{itemize}\n\\item b\n\\end{itemize}", {})
    assert adjacent == "- a\n\n+ b"


def test_text_markup_reads_as_markdown():
    assert convert("An \\emph{arity-$k$ diagram} is \\textbf{valid} and \\textit{tame}.", {}) == (
        "An *arity-$k$ diagram* is **valid** and *tame*.")
    assert convert("A note\\footnote{See \\emph{the appendix}.} here.", {}) == "A note (See *the appendix*.) here."
    assert convert("See \\href{https://example.org}{the site} or \\url{https://example.org}.", {}) == (
        "See the site or https://example.org.")
    assert convert("\\text{x}, \\textsc{Name}, \\mbox{y}", {}) == "x, Name, y"
    # Emphasis that would not read as emphasis is left as text.
    assert convert("Some \\emph{ spaced } and \\emph{} text", {}) == "Some  *spaced*  and  text"


def test_sections_quotes_ties_and_spacing():
    assert convert("\\section{Main estimates}\nText.", {}) == "## Main estimates\n\nText."
    assert convert("\\subsection*{Setup} Text.", {}) == "### Setup\n\nText."
    assert convert("\\paragraph{Why it works} Because.", {}) == "**Why it works.** Because."
    assert convert("as in ``valid'' diagrams", {}) == "as in “valid” diagrams"
    assert convert("Lemma~3 and Section~$2$; about ~5 steps, ~~struck~~.", {}) == (
        "Lemma 3 and Section $2$; about ~5 steps, ~~struck~~.")
    assert convert("By \\S2 and \\ldots{} more.", {}) == "By §2 and … more."
    assert convert("\\noindent Text.\\medskip\\par Next.", {}) == "Text.\n\nNext."
    assert convert("One line\\\\\nnext\\quad word", {}) == "One line\nnext  word"


def test_text_accents_and_dashes_read_as_letters():
    assert convert('Erd\\H{o}s and L\\"owenheim--Skolem, G\\"{o}del, Tur\\\'an, \\v{C}ech, Bj\\o rn, S\\o{}ren, '
                   'Stra\\ss e, {\\"o}, na\\"ive, \\\'{\\i}ndice', {}) == (
        "Erdős and Löwenheim–Skolem, Gödel, Turán, Čech, Bjørn, Søren, Straße, ö, naïve, índice")
    assert convert("pages 3--5, a---b", {}) == "pages 3–5, a—b"
    # Markdown escapes and spaced or table dashes are no accents and no dashes.
    for text in ("it\\'s fine", "1\\. Every item", "a -- b", "|---|---|", "$\\\"o$ and $a--b$", "\\. E"):
        assert convert(text, {}) == text, text


def test_math_and_unknown_environments_are_kept():
    for text in ("$\\emph{x}$ and $$\\text{a}~b$$", "\\begin{equation}\\label{x} a \\end{equation}",
                 "\\[ \\begin{cases} a \\\\ b \\end{cases} \\]", "\\begin{align*} a&=b \\\\ c&=d \\end{align*}",
                 "\\begin{center}kept\\end{center}", "\\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture}"):
        assert convert(text, {}) == text, text
    assert convert("\\begin{lemma} $\\begin{cases} a \\\\ b \\end{cases}$ \\end{lemma}", {}) == (
        "**Lemma.** $\\begin{cases} a \\\\ b \\end{cases}$")


def test_text_without_tex_is_unchanged():
    for text in ("", "Plain text with a hard break  \nand more.", "**Bold** and *em*", "Cost ~ 5"):
        assert convert(text, {}) is text
    # Private-use characters in the text never stand in for math.
    assert convert("a \ue0000\ue001 \\emph{b} $c$", {}) == "a 0 *b* $c$"


@pytest.mark.parametrize("hostile", [
    "\\begin{itemize}" * 20_000, "\\end{enumerate}" * 20_000, "\\item[" * 30_000, "\\emph{" * 50_000,
    "\\emph{a$x$" * 20_000, "``" * 60_000, "~" * 200_000, "\\section{" * 30_000, "[[result:a]]\n" * 20_000 + "\\begin{lemma}",
    "\\begin{enumerate}[label=(" * 10_000, "\\\\" * 100_000, "\\footnote{\\emph{" * 20_000,
    " " * 100_000 + "\\x", "\t" * 100_000 + "\\par", (" " * 50 + "\\\\a") * 4_000,
    "\\H" + " " * 100_000 + "x", "{\\'" * 50_000, "-" * 200_000 + "a", "a" + "-" * 200_000 + "a", "\\o" * 100_000,
])
def test_hostile_text_takes_linear_time(hostile):
    started = time.monotonic()
    convert(hostile, {})
    assert time.monotonic() - started < 3.0


def test_the_readable_version_writes_leftover_markup_out():
    part = {"index": 1, "status": "faithful", "issues": [], "missing": [],
            "markdown": "[[result:lem:a]]\n\\begin{lemma}\nEvery \\emph{even} $n$ is $2k$.\n\\end{lemma}"}
    text = report.render_part(part, labels={"lem:a": "Lemma 2.1"}, original="", original_room=[0])
    assert "[result:lem:a]: #\n\n**Lemma 2.1.** Every *even* $n$ is $2k$." in text
    glance = report.at_a_glance({"one_line": "It shows that \\emph{every} sum is even."}, {})
    assert "**What it shows.** It shows that *every* sum is even." in glance
