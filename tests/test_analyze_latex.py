"""Offline tests: LaTeX statement DAG extraction for Analyze (pylatexenc)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("pylatexenc")

from agent_monitor.analyze import graph as ag  # noqa: E402
from agent_monitor.analyze import latex_structure as ls  # noqa: E402
from agent_monitor.proof_graph import _sanitize  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "analyze" / "openai-math"

SYNTHETIC = r"""\documentclass{amsart}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{prop}[theorem]{Proposition}
\newtheorem{claim}{Claim}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\newcommand{\R}{\mathbb{R}}
\DeclareMathOperator{\tr}{tr}
\title{A \emph{test} paper\thanks{none}}
\begin{document}
\begin{abstract}
We prove   things.
\end{abstract}
\maketitle
\section{Introduction}
\begin{theorem}[Main theorem]\label{thm:main}
Every $X$ is $Y$.
\end{theorem}
\section{Preliminaries}
\input{sections/defs}
\begin{lemma}\label{Lem.Key Step}
Every $x$ as in Definition~\ref{def:x} satisfies
\[ a = b \label{eq:key} \]
\end{lemma}
\begin{proof}
By \cite[Thm 2.1]{smith} and \eqref{eq:key}. % \ref{prop:a} is a comment
\begin{claim}\label{cl:inner}
An inner step.
\end{claim}
\begin{proof}
Trivial.
\end{proof}
\end{proof}
\begin{comment}
\begin{lemma} hidden \ref{thm:main}
\end{comment}
\iffalse
\begin{lemma}\label{lem:dead} dead \end{lemma}
\fi
\begin{prop}\label{prop:a}
This is a step toward Theorem~\ref{thm:main}.
\end{prop}
\begin{proof}
By Lemma~\ref{Lem.Key Step}.
\end{proof}
\section{Proof of the main theorem}
We now prove Theorem~\ref{thm:main}. By Proposition~\ref{prop:a} we are done.
This completes the proof of Theorem~\ref{thm:main}.
\appendix
\section{Extra}
\begin{lemma}\label{lem:app}
An appendix lemma.
\end{lemma}
\begin{proof}
By \hyperref[prop:a]{the proposition}.
\end{proof}
\input{../escape}
\input{/etc/passwd}
\begin{thebibliography}{9}
\bibitem{smith} J. Smith, \emph{A fine theorem}, 2020.
\end{thebibliography}
\end{document}
"""

DEFS = r"""\begin{definition}\label{def:x}
An $x$ is good.
\end{definition}
"""


def write_bundle(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def by_label(document: dict) -> dict[str, dict]:
    return {f'{node["display"]} {node["number"]}': node for node in document["nodes"]}


def edge_pairs(document: dict, *, weak: bool = False) -> set[tuple[str, str]]:
    name = {node["id"]: f'{node["display"]} {node["number"]}' for node in document["nodes"]}
    return {(name[edge["from"]], name[edge["to"]]) for edge in document["edges"] if bool(edge.get("weak")) == weak}


@pytest.fixture()
def synthetic(tmp_path):
    bundle = write_bundle(tmp_path / "bundle", {"main.tex": SYNTHETIC, "sections/defs.tex": DEFS})
    (tmp_path / "escape.tex").write_text("\\begin{lemma}outside\\end{lemma}")
    return ls.parse_project(bundle)


@pytest.mark.parametrize("paper", ["irrationality-exponent-of-pi", "kaplansky-char2"])
def test_parity_with_the_kg_tools_prototype_on_openai_math_papers(paper):
    expected = json.loads((FIXTURES / "expected_prototype.json").read_text())[paper]
    document = ls.parse_project(FIXTURES / paper)
    labels = [f'{node["display"]} {node["number"]}' for node in document["nodes"]]
    assert labels == expected["statements"]
    assert sorted(map(list, edge_pairs(document))) == expected["uses"]
    assert sorted(map(list, edge_pairs(document, weak=True))) == expected["weak"]
    assert sorted(f'{n["display"]} {n["number"]}' for n in document["nodes"] if n["has_proof"]) == expected["with_proof"]
    assert document["parser"].startswith("pylatexenc-")
    assert all(ls.NODE_ID_RE.fullmatch(node["id"]) for node in document["nodes"])


def test_pi_paper_structure_and_projection_survives_the_console_sanitizer():
    document = ls.parse_project(FIXTURES / "irrationality-exponent-of-pi")
    nodes = by_label(document)
    assert [node["id"] for node in document["nodes"] if node["main_result"]] == [nodes["Theorem 1.1"]["id"]]
    assert document["root_file"] == "main.tex"
    assert document["title"].startswith("The irrationality exponent of")
    assert document["abstract"]
    assert "Theorem 1.1" in document["intro"] or "\\begin{theorem}" in document["intro"]
    assert nodes["Theorem 1.1"]["file"] == "main.tex"
    assert nodes["Theorem 1.1"]["lines"][0] >= 1
    graph = {"nodes": [{"id": node["id"], "kind": "lemma", "label": f'{node["display"]} {node["number"]}',
                        "statement": node["statement"], "citations": [c["key"] for c in node["cites"]][:6]}
                       for node in document["nodes"]],
             "edges": [{"from": e["from"], "to": e["to"], "label": "uses"} for e in document["edges"]]}
    clean = _sanitize(graph)
    assert len(clean["nodes"]) == len(graph["nodes"]) and len(clean["edges"]) == len(graph["edges"])


def test_synthetic_numbering_kinds_and_hidden_blocks(synthetic):
    nodes = by_label(synthetic)
    assert sorted(nodes) == sorted([
        "Theorem 1.1", "Definition 2.1", "Lemma 2.2", "Claim 1", "Proposition 2.3", "Lemma A.1",
    ])
    assert nodes["Theorem 1.1"]["title"] == "Main theorem" and nodes["Theorem 1.1"]["main_result"]
    assert nodes["Definition 2.1"]["file"] == "sections/defs.tex"
    assert not any("hidden" in node["statement"] or "dead" in node["statement"] for node in synthetic["nodes"])
    ids = {label: node["id"] for label, node in nodes.items()}
    assert ids == {
        "Theorem 1.1": "thm:main", "Definition 2.1": "def:x", "Lemma 2.2": "lem-key-step",
        "Claim 1": "cl:inner", "Proposition 2.3": "prop:a", "Lemma A.1": "lem:app",
    }
    assert nodes["Claim 1"]["nested_in"] == "lem-key-step"
    assert synthetic["title"] == "A test paper"
    assert synthetic["abstract"] == "We prove things."
    assert synthetic["macros"]["\\R"] == "\\mathbb{R}"
    assert synthetic["macros"]["\\tr"] == "\\operatorname{tr}"


def test_synthetic_edges_proofs_prose_and_cycle_breaking(synthetic):
    uses = edge_pairs(synthetic)
    assert ("Definition 2.1", "Lemma 2.2") in uses           # statement reference
    assert ("Claim 1", "Lemma 2.2") in uses                   # nested claim
    assert ("Lemma 2.2", "Proposition 2.3") in uses           # proof reference
    assert ("Proposition 2.3", "Theorem 1.1") in uses         # prose proof
    assert ("Proposition 2.3", "Lemma A.1") in uses           # \hyperref[...]
    # The weaker statement-level back edge closed a cycle and was dropped.
    assert ("Theorem 1.1", "Proposition 2.3") not in uses
    assert len(synthetic["dropped_for_acyclicity"]) == 1
    edges = {(e["from"], e["to"]): e for e in synthetic["edges"]}
    assert edges[("cl:inner", "lem-key-step")]["provenance"] == "inferred"
    assert edges[("cl:inner", "lem-key-step")]["basis"] == "nested-claim"
    assert edges[("def:x", "lem-key-step")]["context"] == "statement"
    assert edges[("prop:a", "thm:main")]["basis"] == "prose-proof"
    assert edges[("prop:a", "thm:main")]["provenance"] == "ref"
    nodes = by_label(synthetic)
    assert nodes["Theorem 1.1"]["has_proof"] and "Proposition~\\ref{prop:a}" in nodes["Theorem 1.1"]["proof"]
    assert nodes["Theorem 1.1"]["proof_link"] == ["prose"]
    assert "Trivial" in nodes["Claim 1"]["proof"]
    assert "\\label" not in nodes["Lemma 2.2"]["statement"]
    assert "comment" not in nodes["Lemma 2.2"]["proof"]
    cites = nodes["Lemma 2.2"]["cites"]
    assert cites == [{"key": "smith", "note": "Thm 2.1", "title": "J. Smith, A fine theorem, 2020."}]


def test_includes_cannot_leave_the_bundle(synthetic):
    assert not any(node["statement"] == "outside" for node in synthetic["nodes"])
    joined = " ".join(synthetic["warnings"])
    assert "escape" in joined and "refused" in joined
    assert synthetic["files"] == ["main.tex", "sections/defs.tex"]


def test_declaretheorem_numberwithin_and_aliascnt(tmp_path):
    source = r"""\documentclass{article}
\usepackage{thmtools,aliascnt}
\declaretheorem[name=Theorem, numberwithin=section]{thm}
\declaretheorem[name=Lemma, sibling=thm]{lem}
\newtheorem{corollary}{Corollary}
\numberwithin{corollary}{section}
\newtheorem{fct}{Fact}
\newtheorem{genericthm}{Generic}
\newaliascnt{rem}{fct}
\newtheorem{remark}[rem]{Remark}
\begin{document}
\section{A}
\begin{thm}\label{t1}T\end{thm}
\begin{proof}x\end{proof}
\begin{lem}L\end{lem}
\begin{corollary}C\end{corollary}
\begin{fct}F\end{fct}
\begin{genericthm}not theorem-like\end{genericthm}
\begin{remark}R\end{remark}
\section*{Unnumbered}
\section{B}
\begin{lem}L2\end{lem}
\begin{corollary}C2\end{corollary}
\end{document}
"""
    bundle = write_bundle(tmp_path / "b", {"paper.tex": source})
    document = ls.parse_project(bundle)
    assert [f'{n["display"]} {n["number"]}' for n in document["nodes"]] == [
        "Theorem 1.1", "Lemma 1.2", "Corollary 1.1", "Fact 1", "Remark 2", "Lemma 2.1", "Corollary 2.1",
    ]


def test_pasted_fragment_without_documentclass_is_its_own_root(tmp_path):
    bundle = write_bundle(tmp_path / "p", {"pasted.tex": "\\begin{theorem}\\label{t}A.\\end{theorem}\n\\begin{proof}B.\\end{proof}\n"})
    document = ls.parse_project(bundle)
    assert [node["kind"] for node in document["nodes"]] == ["theorem"]
    assert document["nodes"][0]["has_proof"] and document["nodes"][0]["id"] == "t"


def test_root_detection_prefers_documentclass_and_usual_names(tmp_path):
    bundle = write_bundle(tmp_path / "r", {
        "build/main.tex": "\\documentclass{article}\\begin{document}\\input{body}\\end{document}",
        "build/body.tex": "\\begin{lemma}x\\end{lemma}",
        "notes/draft.tex": "\\documentclass{article}\\begin{document}draft\\end{document}" + "x" * 5000,
        "other.tex": "% \\documentclass{article}\\begin{document}",
    })
    assert ls.find_root(bundle).relative_to(bundle.resolve()).as_posix() == "build/main.tex"
    document = ls.parse_project(bundle)
    assert document["files"] == ["build/main.tex", "build/body.tex"]


def test_node_cap_keeps_the_main_result_closure_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "MAX_NODES", 5)
    lemmas = "".join(
        f"\\begin{{lemma}}\\label{{l{index}}}L{index}\\end{{lemma}}\n\\begin{{proof}}p\\end{{proof}}\n"
        for index in range(1, 9)
    )
    source = ("\\documentclass{article}\\begin{document}\n" + lemmas
              + "\\begin{theorem}\\label{main}T\\end{theorem}\n"
              + "\\begin{proof}By Lemma~\\ref{l7} and Lemma~\\ref{l8}.\\end{proof}\n\\end{document}\n")
    document = ls.parse_project(write_bundle(tmp_path / "c", {"main.tex": source}))
    kept = [node["id"] for node in document["nodes"]]
    assert len(kept) == 5 and {"main", "l7", "l8"} <= set(kept)
    assert document["truncated"] == {"statements": 9, "kept": 5}
    assert all(edge["from"] in kept and edge["to"] in kept for edge in document["edges"])


def test_slugs_are_informal_link_safe():
    assert ls.slug("Lem.Key Step") == "lem-key-step"
    assert ls.slug("thm:main") == "thm:main"
    assert ls.slug("  :Weird//Label__ ") == "weird-label"
    assert ls.slug("é") == ""
    assert len(ls.slug("x" * 200)) == 80
    nodes = [{"labels": ["A"], "kind": "lemma", "number": "1"}, {"labels": ["a"], "kind": "lemma", "number": "1"},
             {"labels": [], "kind": "lemma", "number": None}]
    ls.assign_ids(nodes)
    assert [node["id"] for node in nodes] == ["a", "lemma-1", "n3"]


def test_latin1_sources_are_read_with_a_warning(tmp_path):
    bundle = tmp_path / "l"
    bundle.mkdir()
    (bundle / "main.tex").write_bytes("\\documentclass{article}\\begin{document}\\begin{lemma}caf\xe9\\end{lemma}\\end{document}".encode("latin-1"))
    document = ls.parse_project(bundle)
    assert document["nodes"][0]["statement"] == "caf\xe9"
    assert any("Latin-1" in warning for warning in document["warnings"])


def test_sandboxed_parse_matches_in_process_and_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "secret-should-not-leak")
    env = ls.sandbox_env()
    assert "KIMI_API_KEY" not in env and set(env) <= {
        "PATH", "LC_ALL", "PYTHONIOENCODING", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"}
    out = tmp_path / "segments.json"
    sandboxed = ls.run_sandboxed(FIXTURES / "kaplansky-char2", out)
    in_process = ls.parse_project(FIXTURES / "kaplansky-char2")
    assert sandboxed["nodes"] == json.loads(json.dumps(in_process["nodes"]))
    assert sandboxed["edges"] == json.loads(json.dumps(in_process["edges"]))


def test_hostile_nesting_never_crashes_the_caller(tmp_path):
    source = "\\documentclass{article}\\begin{document}" + "{" * 20000 + "x" + "}" * 20000 + "\\end{document}"
    bundle = write_bundle(tmp_path / "h", {"main.tex": source})
    try:
        document = ls.run_sandboxed(bundle, tmp_path / "out.json")
    except ls.LatexStructureError:
        return
    assert document["nodes"] == []


def test_no_tex_files_is_a_clear_error(tmp_path):
    (tmp_path / "e").mkdir()
    with pytest.raises(ls.LatexStructureError, match="No TeX file"):
        ls.parse_project(tmp_path / "e")
    with pytest.raises(ls.LatexStructureError):
        ls.run_sandboxed(tmp_path / "e", tmp_path / "o.json")
    assert os.path.exists(tmp_path / "e")


# ---------------------------------------------------------------------------
# proof attachment, restatable results, prose proofs, numbering
# (phrasings modelled on openai/math manuscripts; no text is copied)
# ---------------------------------------------------------------------------

def parse(tmp_path, source, name="main.tex", extra=None):
    return ls.parse_project(write_bundle(tmp_path / "doc", {name: source, **(extra or {})}))


def node_by_id(document, node_id):
    return next(node for node in document["nodes"] if node["id"] == node_id)


def uses(document):
    return {(edge["from"], edge["to"]) for edge in document["edges"] if not edge.get("weak")}


PREAMBLE = r"""\documentclass{amsart}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{corollary}[theorem]{Corollary}
\theoremstyle{remark}
\newtheorem{remark}[theorem]{Remark}
\begin{document}
"""


def test_a_proof_titled_by_number_reaches_its_theorem_not_the_last_lemma(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:main} Every X is Y. \end{theorem}
\section{Lemmas}
\begin{lemma}\label{lem:a} A holds. \end{lemma}
\begin{proof} Trivial computation for A. \end{proof}
\begin{lemma}\label{lem:b} B holds. \end{lemma}
\begin{proof} By Lemma~\ref{lem:a}, B. \end{proof}
\begin{proof} An untitled second proof that belongs to nothing above. \end{proof}
\section{Proof of the main theorem}
\begin{proof}[Proof of Theorem 1.1]
Combine Lemma~\ref{lem:a} and Lemma~\ref{lem:b}.
\end{proof}
\begin{proof}[Proof of Theorem 9.9] A result that does not exist. \end{proof}
\end{document}
""")
    main, lemma_b = node_by_id(document, "thm:main"), node_by_id(document, "lem:b")
    assert main["has_proof"] and main["proof_link"] == ["title-number"]
    assert main["proof"].startswith("Combine Lemma")
    assert lemma_b["proof"] == "By Lemma~\\ref{lem:a}, B." and lemma_b["proof_link"] == ["preceding"]
    assert {("lem:a", "thm:main"), ("lem:b", "thm:main"), ("lem:a", "lem:b")} <= uses(document)
    assert document["stats"]["proofs_unattached"] == 2
    assert any("2 proof environment(s) could not be matched" in warning for warning in document["warnings"])


def test_a_document_order_guess_never_closes_a_cycle(tmp_path):
    # The main theorem is stated first and proved last.  The lemma after it
    # cites nothing, but guessing that it uses the theorem would close a cycle
    # (theorem -> proposition -> lemma) and cost the theorem its real
    # dependency once the graph is sanitized.
    document = parse(tmp_path, PREAMBLE + r"""
\section{Results}
\begin{theorem}\label{thm:main} No integer congruent to $3 \bmod 4$ is a sum of two squares. \end{theorem}
\begin{lemma}\label{lem:sq} For every integer $x$, $x^2 \equiv 0$ or $1 \pmod 4$. \end{lemma}
\begin{proof} Write $x=2k$ or $x=2k+1$. \end{proof}
\begin{proposition}\label{prop:sums} A sum of two squares is $0$, $1$ or $2 \bmod 4$. \end{proposition}
\begin{proof} Add the two residues of Lemma~\ref{lem:sq}. \end{proof}
\begin{lemma}\label{lem:odd} An odd square is $1 \bmod 8$. \end{lemma}
\begin{proof} Expand $(2k+1)^2$. \end{proof}
\begin{proof}[Proof of Theorem~\ref{thm:main}] Immediate from Proposition~\ref{prop:sums}. \end{proof}
\end{document}
""")
    assert uses(document) == {("lem:sq", "prop:sums"), ("prop:sums", "thm:main")}
    # The guess is still made where it closes nothing.
    assert {(edge["from"], edge["to"]) for edge in document["edges"] if edge.get("weak")} == {("prop:sums", "lem:odd")}
    graph = ag.sanitize(ag.build(document, {"title": "Squares", "format": "tex", "canonical_id": "paste:x", "warnings": []},
                                 generated_at="2026-10-11T00:00:00Z"))
    assert {(edge["from"], edge["to"]) for edge in graph["edges"] if not edge.get("weak")} == uses(document)


def test_the_proof_of_a_result_written_in_prose_reaches_it_and_the_result_joined_to_it(tmp_path):
    # arXiv 2609.01782: "The proof of Theorem~\ref{thm:alpha} and thus Theorem~\ref{thm:lower} follows easily."
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:lower} $R(3,k)$ grows like $k^2/\log k$. \end{theorem}
\begin{theorem}\label{thm:alpha} Some triangle-free graph has small independence number. \end{theorem}
\section{Exposure}
\begin{lemma}\label{lem:exposure} A set is rarely independent. \end{lemma}
\begin{proof} Union bound. \end{proof}
The proof of Theorem~\ref{thm:alpha} and thus Theorem~\ref{thm:lower} follows easily.
Otherwise, Lemma~\ref{lem:exposure} applies and a union bound finishes. \qed
\section{Shearer}
Unrelated text about Lemma~\ref{lem:exposure}.
\end{document}
""")
    for target in ("thm:alpha", "thm:lower"):
        node = node_by_id(document, target)
        assert node["has_proof"] and node["proof_link"] == ["prose"], target
        assert "a union bound finishes" in node["proof"] and "Unrelated" not in node["proof"]
    assert {("lem:exposure", "thm:alpha"), ("lem:exposure", "thm:lower")} <= uses(document)


def test_a_proof_announced_for_elsewhere_is_not_a_prose_proof(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Results}
\begin{theorem}\label{thm:a} A holds. \end{theorem}
\begin{theorem}\label{thm:b} B holds. \end{theorem}
The proof of Theorem~\ref{thm:a} is given in Section~\ref{sec:proofs}.
The proof of Theorem~\ref{thm:b} is deferred to the appendix.
\section{Other}\label{sec:proofs}
Text.
\end{document}
""")
    assert not node_by_id(document, "thm:a")["has_proof"] and not node_by_id(document, "thm:b")["has_proof"]


def test_each_result_carries_the_running_text_before_it_for_its_notation(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:main} The construction works. \end{theorem}
\section{The construction}
% a comment that never reaches the verifier
Sample $G_R$ and $G_B$ on $V_R=V_B=[m]$.\label{eq:hidden}
\begin{figure}\caption{A figure}\end{figure}
For $x\in V_R$ let $F(x)$ denote its fiber.
\section{Edge deletions}
Define $X_x:=\bigcup_{xz\in E(G_R)}F(z)$.
\begin{lemma}\label{lem:fiber} $|F(x)|\le \log^2 n$ for all $x\in V_R$. \end{lemma}
\begin{proof} Chernoff, with a long body that is not context. \end{proof}
\begin{lemma}\label{lem:next} $|X_x|\le pn$. \end{lemma}
\end{document}
""")
    context = node_by_id(document, "lem:fiber")["context"]
    assert "Sample $G_R$ and $G_B$ on $V_R=V_B=[m]$." in context and "let $F(x)$ denote its fiber" in context
    assert context.rstrip().endswith(r"Define $X_x:=\bigcup_{xz\in E(G_R)}F(z)$.")
    assert "§ The construction" in context and "§ Edge deletions" in context
    for absent in ("comment", "\\label", "A figure", "The construction works", "documentclass", "newtheorem"):
        assert absent not in context, absent
    following = node_by_id(document, "lem:next")["context"]
    assert "Define $X_x" in following and "Chernoff" not in following and "|F(x)|" not in following
    assert node_by_id(document, "thm:main")["context"] == "§ Introduction"
    assert all(len(node["context"]) <= ls.MAX_CONTEXT_CHARS for node in document["nodes"])


def test_titled_parts_still_join_their_statement(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{One}
\begin{theorem}\label{thm:t} (a) and (b). \end{theorem}
\begin{proof}[Proof of (a)] First part. \end{proof}
\begin{proof}[Proof of (b)] Second part. \end{proof}
\begin{lemma}\label{lem:l} L. \end{lemma}
\begin{proof}
\begin{claim} Inner. \end{claim}
\end{proof}
\begin{proof} A proof after a finished proof belongs to no claim inside it. \end{proof}
\end{document}
""", extra=None)
    theorem = node_by_id(document, "thm:t")
    assert theorem["proof"] == "First part.\n\nSecond part." and theorem["proof_link"] == ["preceding", "preceding"]
    assert "belongs to no claim" not in node_by_id(document, "lem:l")["proof"]
    assert document["stats"]["proofs_unattached"] == 1


def test_restatable_results_keep_their_number_and_their_late_proof(tmp_path):
    source = r"""\documentclass{article}
\usepackage{amsthm,thmtools}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\begin{document}
\section{Intro}
\begin{restatable}[Main bound]{theorem}{mainthm}\label{thm:main}
Every X is Y.
\end{restatable}
\section{Body}
\begin{lemma}\label{lem:a} A. \end{lemma}
\begin{proof} trivial \end{proof}
\begin{lemma}\label{lem:b} B. \end{lemma}
\begin{proof} By Lemma \ref{lem:a}. \end{proof}
\mainthm*
\begin{proof} By Lemma~\ref{lem:a} and Lemma~\ref{lem:b}. \end{proof}
\end{document}
"""
    document = parse(tmp_path, source)
    assert [f'{node["display"]} {node["number"]}' for node in document["nodes"]] == [
        "Theorem 1.1", "Lemma 2.1", "Lemma 2.2"]
    main = node_by_id(document, "thm:main")
    assert main["env"] == "theorem" and main["title"] == "Main bound" and main["main_result"]
    assert main["statement"] == "Every X is Y."
    assert main["proof"].startswith("By Lemma~\\ref{lem:a} and") and main["proof_link"] == ["preceding"]
    assert node_by_id(document, "lem:b")["proof"] == "By Lemma \\ref{lem:a}."
    assert {("lem:a", "thm:main"), ("lem:b", "thm:main")} <= uses(document)


def test_prose_proof_spans_its_subsections_and_ends_at_its_marker(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:mono} Monotone solutions are flat. \end{theorem}
We prove Theorem~\ref{thm:mono} in Section~\ref{sec:proof}.
\section{From profiles to the theorem}\label{sec:proof}
In this part we deduce Theorem~\ref{thm:mono} from Lemma~\ref{lem:ends}.
\subsection{Ends}
\begin{lemma}\label{lem:ends} Ends are planar. \end{lemma}
\begin{proof} Compactness. \end{proof}
\subsection{Conclusion}
By Lemma~\ref{lem:ends} the limit is a plane. This proves Theorem~\ref{thm:mono}, including the rigidity.
Afterwards an unrelated remark mentions Lemma~\ref{lem:ends}.
\section{Other}
Text.
\end{document}
""")
    main = node_by_id(document, "thm:mono")
    assert main["has_proof"] and main["proof_link"] == ["prose"]
    assert main["proof"].startswith("In this part we deduce")
    assert "the limit is a plane" in main["proof"] and main["proof"].endswith("This proves Theorem~\\ref{thm:mono}")
    assert "in Section" not in main["proof"] and "unrelated remark" not in main["proof"]
    assert ("lem:ends", "thm:mono") in uses(document)


def test_end_markers_without_a_start_close_proofs_from_their_section(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:main} Integral points are dense. \end{theorem}
\begin{theorem}\label{thm:gap} The gap is large. \end{theorem}
\begin{theorem}\label{thm:law} The interfaces converge. \end{theorem}
\begin{theorem}\label{thm:cap} Reconstruction holds iff capacity is positive. \end{theorem}
Section~\ref{sec:descent} proves Theorem~\ref{thm:main}.
\section{Descent}\label{sec:descent}
\begin{lemma}\label{lem:stratum} The stratum is open. \end{lemma}
\begin{proof} Semicontinuity. \end{proof}
By Lemma~\ref{lem:stratum} the exact locus is open, so the points are dense there.
This proves Theorem~\ref{thm:main} for exact classes.
Taking the finite union over strata, this establishes the unrestricted assertion of Theorem~\ref{thm:main}.
\section{Reduction}
\begin{lemma}\label{lem:dual} Duality holds. \end{lemma}
\begin{proof} Linear programming. \end{proof}
\begin{equation}\label{eq:gap} a \geq b. \end{equation}
\subsection{Vertices}
Since $n \leq m$, the sizes compare.
Combining this with \eqref{eq:gap} proves \Cref{thm:gap}.
\section{Localization}
\begin{proposition}\label{prop:bounded} Bounded domains work. \end{proposition}
\begin{proof} Uniqueness. \end{proof}
\begin{corollary}\label{cor:univ} Universality holds. \end{corollary}
\begin{proof} By Proposition~\ref{prop:bounded}. \end{proof}
The negative branch is symmetric.
Together with Proposition~\ref{prop:bounded}, Corollary~\ref{cor:univ} proves Theorem~\ref{thm:law}.
\section{Capacity}
\begin{lemma}\label{lem:flow} Flows exist. \end{lemma}
\begin{proof} Construction. \end{proof}
The labels satisfy Lemma~\ref{lem:flow}, which proves positive capacity and completes the first implication of Theorem~\ref{thm:cap}.
\section{Converse}
The losses telescope, so the information stays positive, proving the remaining implication of Theorem~\ref{thm:cap}.
\end{document}
""")
    main = node_by_id(document, "thm:main")
    assert main["proof_link"] == ["prose"] and main["proof"].count("Theorem~\\ref{thm:main}") == 2
    assert main["proof"].startswith("By Lemma~\\ref{lem:stratum} the exact locus")
    assert "Semicontinuity" not in main["proof"] and "Section~" not in main["proof"]
    gap = node_by_id(document, "thm:gap")
    assert gap["has_proof"] and gap["proof"].startswith("\\begin{equation}\\label{eq:gap}")
    law = node_by_id(document, "thm:law")
    assert law["proof"].startswith("The negative branch is symmetric.")
    cap = node_by_id(document, "thm:cap")
    assert "completes the first implication" in cap["proof"] and "proving the remaining implication" in cap["proof"]
    assert {("lem:stratum", "thm:main"), ("prop:bounded", "thm:law"), ("cor:univ", "thm:law"),
            ("lem:flow", "thm:cap")} <= uses(document)
    bases = {(edge["from"], edge["to"]): edge["basis"] for edge in document["edges"]}
    assert bases[("lem:stratum", "thm:main")] == "prose-proof"


def test_roadmaps_and_commentary_are_not_proofs(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Introduction}
\begin{theorem}\label{thm:a} A. \end{theorem}
\begin{theorem}\label{thm:b} B. \end{theorem}
\begin{theorem}\label{thm:c} C. \end{theorem}
We prove Theorem~\ref{thm:a} by a reduction. Section~\ref{sec:two} proves Theorem~\ref{thm:b}.
\section{Two}\label{sec:two}
We prove Theorem~\ref{thm:b} below. In Section~\ref{sec:two} we prove Theorem~\ref{thm:c}.
We prove Theorem~\ref{thm:c} in Section 4.
\begin{proof}[Proof of Theorem~\ref{thm:c}] The real proof. \end{proof}
We now prove Theorem~\ref{thm:c} once more, informally. This completes the proof of Theorem~\ref{thm:c}.
\end{document}
""")
    assert not node_by_id(document, "thm:a")["has_proof"]
    assert not node_by_id(document, "thm:b")["has_proof"]
    c = node_by_id(document, "thm:c")
    assert c["proof"] == "The real proof." and c["proof_link"] == ["title-ref"]


def test_announced_proofs_and_results_used_by_this(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Statements}
\begin{theorem}\label{thm:roth} Dense sets have progressions. \end{theorem}
\begin{proposition}\label{prop:int} The integrated period is an isomorphism. \end{proposition}
\begin{lemma}\label{lem:shapiro} Shapiro. \end{lemma}
\begin{proof} Induction. \end{proof}
\section{Concluding the proof}
Finally, we will combine Lemma~\ref{lem:shapiro} with a density increment, and so finally concluding the proof of Theorem~\ref{thm:roth}.
The increment argument is iterated.
\section{The period}
\begin{lemma}\label{lem:coh} The cohomology vanishes. \end{lemma}
\begin{proof} Weights. \end{proof}
Together with Lemma~\ref{lem:shapiro}, this proves Proposition~\ref{prop:int}.
\end{document}
""")
    roth = node_by_id(document, "thm:roth")
    assert roth["has_proof"] and "The increment argument is iterated." in roth["proof"]
    period = node_by_id(document, "prop:int")
    assert period["proof"] == "Together with Lemma~\\ref{lem:shapiro}, this proves Proposition~\\ref{prop:int}"
    assert {("lem:shapiro", "prop:int"), ("lem:coh", "prop:int"), ("lem:shapiro", "thm:roth")} <= uses(document)


def test_a_heading_that_proves_two_results_gives_the_section_to_both(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Results}
\begin{theorem}\label{thm:a} A. \end{theorem}
\begin{theorem}\label{thm:b} B. \end{theorem}
\begin{lemma}\label{lem:x} X. \end{lemma}
\begin{proof} Clear. \end{proof}
\section{Proof of Theorems~\ref{thm:a} and~\ref{thm:b}}
Both follow from Lemma~\ref{lem:x}.
\bibliographystyle{plain}
\end{document}
""")
    for node_id in ("thm:a", "thm:b"):
        node = node_by_id(document, node_id)
        assert node["has_proof"] and "Both follow from Lemma" in node["proof"] and "plain" not in node["proof"]
    assert ("thm:a", "thm:b") not in uses(document) and ("thm:b", "thm:a") not in uses(document)
    assert {("lem:x", "thm:a"), ("lem:x", "thm:b")} <= uses(document)


def test_numbering_with_the_shared_equation_counter_and_custom_formats(tmp_path):
    document = parse(tmp_path, r"""\documentclass{amsart}
\numberwithin{equation}{section}
\newtheorem{theorem}[equation]{Theorem}
\newtheorem{lemma}[equation]{Lemma}
\theoremstyle{remark}
\newtheorem{remark}[equation]{Remark}
\newtheorem{mainthm}{Theorem}
\renewcommand{\themainthm}{\Alph{mainthm}}
\newtheorem{fact}{Fact}
\renewcommand{\thefact}{\roman{section}-\arabic{fact}-\fnsymbol{fact}}
\begin{document}
\begin{mainthm}\label{thm:A} First. \end{mainthm}
\begin{mainthm}\label{thm:B} Second. \end{mainthm}
\section{One}
\begin{equation}\label{eq:1} a=b \end{equation}
\begin{lemma}\label{lem:a} A. \end{lemma}
\begin{proof} ok \end{proof}
\begin{align}
x &= y \\
y &= z \nonumber \\
z &= w \\
\end{align}
\begin{equation*} \text{unnumbered} \end{equation*}
\begin{remark}\label{rem:r} By Lemma \ref{lem:a}, something. \end{remark}
\begin{theorem}\label{thm:t} T. \end{theorem}
\begin{proof} By Remark \ref{rem:r}. \end{proof}
\begin{fact}\label{fact:x} F. \end{fact}
\section{Two}
\begin{subequations}
\begin{align} p &= q \\ q &= r \end{align}
\end{subequations}
\begin{lemma}\label{lem:c} C. \end{lemma}
\end{document}
""")
    numbers = {node["id"]: node["number"] for node in document["nodes"]}
    assert numbers == {"thm:a": "A", "thm:b": "B", "lem:a": "1.2", "rem:r": "1.5", "thm:t": "1.6",
                       "fact:x": None, "lem:c": "2.2"}
    assert any("custom format" in warning for warning in document["warnings"])
    with_counterwithin = parse(tmp_path / "cw", r"""\documentclass{article}
\newtheorem{lemma}{Lemma}
\counterwithin{lemma}{section}
\newtheorem{claim}{Claim}
\counterwithin*{claim}{section}
\begin{document}
\section{A}
\begin{lemma}L\end{lemma}\begin{claim}C\end{claim}
\section{B}
\begin{lemma}L\end{lemma}\begin{claim}C\end{claim}
\end{document}
""")
    assert [f'{node["display"]} {node["number"]}' for node in with_counterwithin["nodes"]] == [
        "Lemma 1.1", "Claim 1", "Lemma 2.1", "Claim 1"]


def test_an_oversize_root_is_named_instead_of_reported_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "MAX_FILE_BYTES", 2000)
    bundle = write_bundle(tmp_path / "big", {"main.tex": "\\documentclass{article}\\begin{document}" + "x" * 3000
                                             + "\\end{document}", "macros.tex": "\\newcommand{\\R}{x}"})
    with pytest.raises(ls.LatexStructureError, match="main.tex exceeds the 0 MiB limit of the LaTeX parser"):
        ls.parse_project(bundle)


def test_the_sandboxed_parser_never_imports_from_the_run_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "imported"
    for module in ("argparse", "json", "runpy", "pylatexenc", "agent_monitor"):
        (workspace / f"{module}.py").write_text(f"open({str(marker)!r}, 'w').write('{module}')\n")
    monkeypatch.chdir(workspace)
    document = ls.run_sandboxed(FIXTURES / "kaplansky-char2", tmp_path / "out.json")
    assert document["nodes"] and not marker.exists()


def test_a_stopped_run_ends_the_parser_child_at_once(tmp_path, monkeypatch):
    import threading
    import time

    (tmp_path / "slow_child.py").write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]))
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(ls.SandboxCancelled):
        ls.run_module_sandboxed("slow_child", [], timeout=60, cancel=cancel)
    assert time.monotonic() - started < 5
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        ls.run_module_sandboxed("slow_child", [], timeout=0.5)


def test_a_missing_parser_package_is_a_server_problem(tmp_path, monkeypatch):
    import subprocess
    import sys

    repository = str(Path(ls.__file__).resolve().parents[2])
    probe = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import pylatexenc",
                            repository], capture_output=True)
    if probe.returncode == 0:
        pytest.skip("pylatexenc is installed in the interpreter itself")
    monkeypatch.setenv("PYTHONPATH", repository)
    with pytest.raises(ls.ParserUnavailable, match="not installed"):
        ls.run_sandboxed(FIXTURES / "kaplansky-char2", tmp_path / "out.json")


def test_markers_inside_another_prose_proof_stay_part_of_it(tmp_path):
    document = parse(tmp_path, PREAMBLE + r"""
\section{Statements}
\begin{theorem}\label{thm:one} One. \end{theorem}
\begin{theorem}\label{thm:two} Two. \end{theorem}
\begin{lemma}\label{lem:base} Base. \end{lemma}
\begin{proof} Clear. \end{proof}
\section{Proofs}
We now prove Theorem~\ref{thm:one}.
\begin{lemma}\label{lem:inner} Inner. \end{lemma}
We first prove Lemma~\ref{lem:inner} from Lemma~\ref{lem:base}; this proves Lemma~\ref{lem:inner}.
Hence the claim holds. This completes the proof of Theorem~\ref{thm:one}.
We now prove Theorem~\ref{thm:two}. It follows from Theorem~\ref{thm:one}.
\end{document}
""")
    one, two = node_by_id(document, "thm:one"), node_by_id(document, "thm:two")
    assert "Hence the claim holds" in one["proof"] and one["proof"].endswith("Theorem~\\ref{thm:one}")
    assert two["proof"].startswith("We now prove Theorem~\\ref{thm:two}") and "Hence" not in two["proof"]
    assert {("lem:inner", "thm:one"), ("lem:base", "thm:one"), ("thm:one", "thm:two")} <= uses(document)
    assert not node_by_id(document, "lem:inner")["has_proof"]


def test_a_real_main_theorem_proved_in_prose_across_two_sections():
    # openai/math, A Capacity Criterion for Four-State Potts Reconstruction on
    # Trees (Apache-2.0; see NOTICE.md): Theorem 1.1 has no proof environment;
    # "... completes the first implication of Theorem~\ref{thm:main}" and
    # "..., proving the remaining implication of Theorem~\ref{thm:main}" end
    # its two halves.
    document = ls.parse_project(FIXTURES / "potts-capacity")
    main = node_by_id(document, "thm:main")
    assert main["main_result"] and main["has_proof"] and main["proof_link"] == ["prose"]
    proof = " ".join(main["proof"].split())
    assert "completes the first implication" in proof and "proving the remaining implication" in proof
    assert {("lem:flow", "thm:main"), ("lem:local-moments", "thm:main")} <= uses(document)
    assert node_by_id(document, "lem:flow")["proof_link"] == ["preceding"]
