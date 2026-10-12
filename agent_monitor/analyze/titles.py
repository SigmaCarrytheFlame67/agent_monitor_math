"""Readable one-line titles from manuscript titles that carry TeX or HTML.

Titles reach plain-text places (the run list, link previews, the openai/math
picker, shared pages) from arXiv metadata (``$\\pi$``), GitHub Markdown
(``$`n\\log n`$``, ``<i>L</i><sup>3</sup>``, ``&#8211;``) and ``\\title{...}``.
:func:`plain_title` turns inline math into Unicode text the way it reads when
typeset (``\\pi`` -> π, ``\\mathbb Q`` -> ℚ, ``x^2`` -> x², ``\\le`` -> ≤) and
leaves everything else alone.  It never raises: an unexpected construct keeps
its command name without the backslash.
"""
from __future__ import annotations

import html
import re

__all__ = ["plain_title", "tex_to_text"]

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
}
_RELATIONS = {
    "le": "≤", "leq": "≤", "leqslant": "≤", "ge": "≥", "geq": "≥", "geqslant": "≥", "lt": "<", "gt": ">",
    "ne": "≠", "neq": "≠", "to": "→", "rightarrow": "→", "longrightarrow": "→", "leftarrow": "←", "gets": "←",
    "mapsto": "↦", "Rightarrow": "⇒", "implies": "⇒", "Leftrightarrow": "⇔", "iff": "⇔", "in": "∈",
    "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "subsetneq": "⊊", "supset": "⊃", "supseteq": "⊇",
    "equiv": "≡", "sim": "∼", "simeq": "≃", "approx": "≈", "cong": "≅", "asymp": "≍", "ll": "≪", "gg": "≫",
    "mid": "|", "nmid": "∤", "perp": "⊥", "parallel": "∥", "propto": "∝", "models": "⊨", "vdash": "⊢",
}
_BINARY = {
    "times": "×", "pm": "±", "mp": "∓", "cup": "∪", "cap": "∩", "setminus": "∖", "otimes": "⊗", "oplus": "⊕",
    "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨", "circ": "∘", "div": "÷", "ast": "∗", "star": "⋆",
}
_SYMBOLS = {
    "infty": "∞", "cdot": "·", "partial": "∂", "nabla": "∇", "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫",
    "oint": "∮", "ldots": "…", "dots": "…", "dotsc": "…", "cdots": "⋯", "forall": "∀", "exists": "∃", "neg": "¬",
    "lnot": "¬", "emptyset": "∅", "varnothing": "∅", "hbar": "ℏ", "ell": "ℓ", "aleph": "ℵ", "Re": "ℜ", "Im": "ℑ",
    "wp": "℘", "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "prime": "′", "colon": ":", "vert": "|", "lvert": "|", "rvert": "|", "Vert": "‖", "lVert": "‖", "rVert": "‖",
    "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁", "bigotimes": "⨂", "square": "□", "Box": "□", "triangle": "△",
    "dagger": "†", "flat": "♭", "sharp": "♯", "natural": "♮", "angle": "∠", "S": "§", "lbrace": "{",
    "rbrace": "}", "lbrack": "[", "rbrack": "]", "backslash": "\\",
}
_OPERATORS = {
    "log", "ln", "lg", "exp", "sin", "cos", "tan", "sec", "csc", "cot", "sinh", "cosh", "tanh", "arcsin",
    "arccos", "arctan", "arg", "det", "dim", "gcd", "hom", "ker", "max", "min", "sup", "inf", "lim", "liminf",
    "limsup", "Pr", "deg",
}
_IGNORED = {
    "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "nolimits",
    "limits", "displaystyle", "textstyle", "scriptstyle", "nonumber", "notag", "middle",
}
_SPACES = {",": " ", ";": " ", ":": " ", ">": " ", " ": " ", "!": "", "quad": " ", "qquad": " ", "enspace": " ",
           "thinspace": " ", "medspace": " ", "thickspace": " "}
_ESCAPED = {"{": "{", "}": "}", "%": "%", "&": "&", "_": "_", "#": "#", "$": "$", "|": "‖"}
_PLAIN_FONTS = {"mathrm", "mathbf", "mathsf", "mathit", "mathtt", "mathcal", "mathscr", "mathfrak", "boldsymbol",
                "bm", "pmb", "operatorname", "mathop", "mathbin", "mathrel", "mathord", "textnormal"}
_TEXT_FONTS = {"text", "textrm", "textit", "textbf", "textsf", "texttt", "mbox", "hbox", "emph", "textup"}
_ACCENTS = {"overline": "\u0305", "bar": "\u0304", "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303",
            "widetilde": "\u0303", "dot": "\u0307", "ddot": "\u0308", "vec": "\u20d7", "check": "\u030c",
            "breve": "\u0306", "underline": "\u0332"}
_DOUBLE_STRUCK = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}
_SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ",
    "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ",
    "z": "ᶻ", "α": "ᵅ", "β": "ᵝ", "γ": "ᵞ", "δ": "ᵟ", "ε": "ᵋ", "θ": "ᶿ", "φ": "ᵠ", "χ": "ᵡ", "′": "′",
}
_SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ",
    "j": "ⱼ", "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ",
    "u": "ᵤ", "v": "ᵥ", "x": "ₓ", "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ",
}
_OPENERS = "([{⟨⌊⌈|‖,;:"
_MAX_TEX = 2000
_TOKEN = re.compile(r"\\[A-Za-z]+\*?|\\.|\s+|.", re.S)


def _double_struck(text: str) -> str:
    out = []
    for char in text:
        if char in _DOUBLE_STRUCK:
            out.append(_DOUBLE_STRUCK[char])
        elif "A" <= char <= "Z":
            out.append(chr(0x1D538 + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            out.append(chr(0x1D552 + ord(char) - ord("a")))
        elif char == "1":
            out.append("𝟙")
        else:
            out.append(char)
    return "".join(out)


def _atom(text: str) -> bool:
    return bool(re.fullmatch(r"[−-]?[\w.′]+", text))


def _script(text: str, superscript: bool) -> str:
    text = text.strip()
    if not text:
        return ""
    table = _SUPERSCRIPT if superscript else _SUBSCRIPT
    if all(char in table for char in text):
        return "".join(table[char] for char in text)
    mark = "^" if superscript else "_"
    return mark + (text if len(text) == 1 or _atom(text) and len(text) <= 2 else f"({text})")


class _Parser:
    def __init__(self, tex: str) -> None:
        self.tokens = _TOKEN.findall(tex[:_MAX_TEX])
        self.index = 0
        self.depth = 0  # inside a sub- or superscript: no spacing around operators

    def _next(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _skip_space(self) -> None:
        while self.index < len(self.tokens) and self.tokens[self.index].isspace():
            self.index += 1

    def argument(self, *, text_mode: bool = False) -> str:
        self._skip_space()
        token = self._next()
        if token is None:
            return ""
        if token == "{":
            return self.sequence("}", text_mode=text_mode)
        if token.startswith("\\"):
            return self.command(token, [])
        return token

    def optional(self) -> str | None:
        self._skip_space()
        if self.index < len(self.tokens) and self.tokens[self.index] == "[":
            self.index += 1
            return self.sequence("]")
        return None

    def sequence(self, until: str | None = None, *, text_mode: bool = False) -> str:
        parts: list[str] = []
        while True:
            token = self._next()
            if token is None or token == until:
                break
            if token.isspace():
                if text_mode:
                    parts.append(" ")
                continue
            if token == "{":
                parts.append(self.sequence("}", text_mode=text_mode))
            elif token == "}":
                continue
            elif token in ("^", "_"):
                self.depth += 1
                try:
                    parts.append(_script(self.argument(), token == "^"))
                finally:
                    self.depth -= 1
            elif token.startswith("\\"):
                parts.append(self.command(token, parts))
            elif text_mode:
                parts.append(token)
            elif token == "-":
                parts.append(self.binary("−", parts))
            elif token == "+":
                parts.append(self.binary("+", parts))
            elif token in "=<>":
                parts.append(self.relation(token))
            elif token == ",":
                parts.append("," if self.depth else ", ")
            elif token == "~":
                parts.append(" ")
            elif token == "&":
                parts.append(" ")
            elif token == "'":
                parts.append("′")
            else:
                parts.append(token)
        return "".join(parts)

    def relation(self, symbol: str) -> str:
        return symbol if self.depth else f" {symbol} "

    def binary(self, symbol: str, parts: list[str]) -> str:
        if self.depth:
            return symbol
        before = "".join(parts).rstrip()
        if not before or before[-1] in _OPENERS or before[-1] in "=<>≤≥≠→∈⊂⊆≡∼≈+−×±":
            return symbol  # unary
        return f" {symbol} "

    def command(self, token: str, parts: list[str]) -> str:
        name = token[1:].rstrip("*") if len(token) > 2 or token[1:].isalpha() else token[1:]
        if name in _SPACES:
            return _SPACES[name]
        if name in _ESCAPED:
            return _ESCAPED[name]
        if name == "\\":
            return " "
        if name in _IGNORED:
            if self.index < len(self.tokens) and self.tokens[self.index] == ".":
                self.index += 1  # \left. is an invisible delimiter
            return ""
        if name in _GREEK:
            return _GREEK[name]
        if name in _RELATIONS:
            return self.relation(_RELATIONS[name])
        if name in _BINARY:
            return self.binary(_BINARY[name], parts)
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        if name in _OPERATORS:
            return name if self.depth else f" {name} "
        if name in ("bmod", "mod"):
            return " mod "
        if name == "pmod":
            return f" (mod {self.argument().strip()})"
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            numerator, denominator = self.argument().strip(), self.argument().strip()
            numerator = numerator if _atom(numerator) else f"({numerator})"
            denominator = denominator if _atom(denominator) else f"({denominator})"
            return f"{numerator}/{denominator}"
        if name == "binom":
            top, bottom = self.argument().strip(), self.argument().strip()
            return f"C({top}, {bottom})"
        if name == "sqrt":
            root = self.optional()
            body = self.argument().strip()
            prefix = {"3": "∛", "4": "∜"}.get((root or "").strip(), "√" if not root else f"{_script(root, True)}√")
            return prefix + (body if _atom(body) else f"({body})")
        if name == "mathbb" or name == "Bbb":
            return _double_struck(self.argument().strip())
        if name in _PLAIN_FONTS:
            body = self.argument().strip()
            return f" {body} " if name in ("mathop", "operatorname") and not self.depth else body
        if name in _TEXT_FONTS:
            return self.argument(text_mode=True)
        if name in _ACCENTS:
            body = self.argument().strip()
            return "".join(char + _ACCENTS[name] if not char.isspace() else char for char in body)
        if name == "not":
            following = self.argument().strip()
            return self.relation({"=": "≠", "<": "≮", ">": "≯", "∈": "∉", "≡": "≢", "⊂": "⊄", "⊆": "⊈"}.get(
                following, following + "\u0338"))
        if name in ("label", "ref", "eqref", "cite", "tag", "hspace", "vspace", "phantom", "hphantom", "vphantom"):
            self.argument()
            return ""
        return name


def tex_to_text(tex: str) -> str:
    """Unicode text for the inline TeX math ``tex`` (without its ``$``)."""
    try:
        text = _Parser(str(tex or "")).sequence()
    except (RecursionError, IndexError, ValueError):
        text = re.sub(r"\\([A-Za-z]+)", r"\1", str(tex or "")).replace("{", "").replace("}", "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([(\[{⟨/]) ", r"\1", text)
    text = re.sub(r" ([)\]}⟩,;:])", r"\1", text)
    return text.strip()


_HTML_SCRIPT = re.compile(r"<(sup|sub)>(.*?)</\1>", re.I | re.S)
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]{0,200}>")
_GITHUB_MATH = re.compile(r"\$`(.+?)`\$", re.S)
_DOLLAR_MATH = re.compile(r"(?<![\\$])\$(?=\S)([^$\n]{1,600}?)(?<=\S)\$(?!\d)")
_PAREN_MATH = re.compile(r"\\\((.{1,600}?)\\\)", re.S)


def plain_title(value: object, limit: int | None = None) -> str:
    """One line of readable text for a title that may hold inline TeX or HTML."""
    text = str(value or "")
    text = _HTML_SCRIPT.sub(lambda match: _script(_HTML_TAG.sub("", match.group(2)), match.group(1).lower() == "sup"),
                            text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = _GITHUB_MATH.sub(lambda match: tex_to_text(match.group(1)), text)
    text = _PAREN_MATH.sub(lambda match: tex_to_text(match.group(1)), text)
    text = _DOLLAR_MATH.sub(lambda match: tex_to_text(match.group(1)), text)
    text = text.replace("`", "")
    text = "".join(char for char in text if char.isprintable() or char.isspace())
    text = " ".join(text.split())
    return text[:limit] if limit else text
