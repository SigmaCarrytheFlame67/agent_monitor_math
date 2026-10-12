"""Offline tests: readable titles from TeX / HTML (run list, previews, picker, shares)."""
from __future__ import annotations

import pytest

from agent_monitor.analyze.sources import parse_readme
from agent_monitor.analyze.titles import plain_title, tex_to_text


@pytest.mark.parametrize("raw, shown", [
    # openai/math CONTENTS.md and README titles (GitHub math, <sub>/<sup>, entities)
    ("Exact derandomization of logarithmic space: $`\\mathsf L=\\mathsf{RL}=\\mathsf{BPL}`$.",
     "Exact derandomization of logarithmic space: L = RL = BPL."),
    ("Integer multiplication below $`n\\log n`$.", "Integer multiplication below n log n."),
    ("The Quasi-Riemann Hypothesis: A Zero-Free Half-Plane $`\\Re s\\gt 7/8`$ ",
     "The Quasi-Riemann Hypothesis: A Zero-Free Half-Plane ℜs > 7/8"),
    ("Kirchberg's $`\\mathcal O_2`$ norm-ultrapower embedding problem.", "Kirchberg's O₂ norm-ultrapower embedding problem."),
    ("Subset Sum in $`O(2^{0.49n})`$ time.", "Subset Sum in O(2^(0.49n)) time."),
    ("Interior $`C^{1,\\alpha}`$ regularity", "Interior C^(1,α) regularity"),
    ("An <i>L</i><sup>3</sup> bound for the trilinear Hilbert transform", "An L³ bound for the trilinear Hilbert transform"),
    ("Subpolynomial dimension reduction in <i>L</i><sub><i>p</i></sub>.", "Subpolynomial dimension reduction in Lₚ."),
    ("<i>P</i> = <i>W</i> for fixed-determinant SL<sub><i>n</i></sub> moduli spaces.",
     "P = W for fixed-determinant SLₙ moduli spaces."),
    ("Row&#8211;column symmetry", "Row–column symmetry"),
    ("Square-lattice FK interfaces and nested loops for 1 <= q < 4",
     "Square-lattice FK interfaces and nested loops for 1 <= q < 4"),
    # arXiv metadata and \title{...}
    ("The irrationality exponent of $\\pi$ is 2", "The irrationality exponent of π is 2"),
    ("A short proof that $R(3,k)=\\Theta(k^2/\\log k)$", "A short proof that R(3, k) = Θ(k²/log k)"),
    ("On $\\mathrm{SL}_2(\\mathbb{Z})$ and $\\zeta(3)$", "On SL₂(ℤ) and ζ(3)"),
    ("Abelian varieties over $\\overline{\\mathbb Q}$", "Abelian varieties over ℚ̅"),
    ("Zeros in $\\operatorname{Re} s>1$ and $\\mathbb Q(\\sqrt{-3})$", "Zeros in Re s > 1 and ℚ(√−3)"),
    ("$p \\nmid |W_G|$ and $3 \\pmod 4$ and $\\frac{n+1}{2}$", "p ∤ |W_G| and 3 (mod 4) and (n + 1)/2"),
    ("A \\(\\mathbb F_\\ell^\\times\\)-torsor", "A 𝔽_ℓ^×-torsor"),
    # literal dollars are not math
    ("A $5 prize and a $10 prize", "A $5 prize and a $10 prize"),
])
def test_plain_title(raw, shown):
    assert plain_title(raw) == shown


def test_unknown_macros_keep_their_name_and_hostile_input_never_raises():
    assert tex_to_text("\\R^n") == "Rⁿ"
    assert tex_to_text("\\frobnicate") == "frobnicate"
    assert isinstance(tex_to_text("{" * 5000 + "x"), str)
    assert isinstance(plain_title("$" + "{" * 1500 + "$"), str)
    assert plain_title("a\u0000b‮c", 2) == "ab"


def test_openai_math_readme_titles_are_readable():
    readme = "# [Subset Sum in Time $`O(2^{0.49n})`$ ](paper.pdf)\n\n**Date:** October 2, 2026\n"
    assert parse_readme(readme)["title"] == "Subset Sum in Time O(2^(0.49n))"
