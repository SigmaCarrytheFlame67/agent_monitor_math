"""Offline tests: Markdown / plain / PDF text segmentation for Analyze."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_monitor.analyze import text_structure as ts

MARKDOWN = """# On sums of evens

## Abstract
We show that sums of even integers are even.

## Introduction
Our main result is Theorem 1. Lemma 2 shows that every even integer has a half.

**Theorem 1 (Main).** The sum of two even integers is even.

*Proof.* By Lemma 2 and Lemma 3 the claim follows. ∎

## Preliminaries

**Lemma 2.** Every even integer is $2k$.

Proof. Definition. QED

Lemma 3. If $a=2k$ and $b=2m$ then $a+b=2(k+m)$:
(i) first part;
(ii) second part.

Proof. Distributivity, using Lemmas 2 and 3.
∎

conjecture. this lower-case line is running text.
"""


def labels(document):
    return [f'{node["display"]} {node["number"]}' for node in document["nodes"]]


def test_markdown_statements_proofs_and_references():
    document = ts.segment_text(ts.normalize(MARKDOWN), file="pasted.md", fmt="md")
    assert labels(document) == ["Theorem 1", "Lemma 2", "Lemma 3"]
    theorem, lemma2, lemma3 = document["nodes"]
    assert theorem["title"] == "Main" and theorem["main_result"]
    assert theorem["proof"].startswith("By Lemma 2 and Lemma 3")
    assert lemma2["proof"] == "Definition. QED"
    assert "(ii) second part." in lemma3["statement"]
    assert lemma3["proof_lines"] == [23, 24]
    edges = {(edge["from"], edge["to"]) for edge in document["edges"]}
    assert edges == {("lemma-2", "theorem-1"), ("lemma-3", "theorem-1"), ("lemma-2", "lemma-3")}
    assert all(edge["provenance"] == "inferred" and edge["basis"] == "text-reference" for edge in document["edges"])
    assert document["title"] == "On sums of evens"
    assert document["abstract"] == "We show that sums of even integers are even."
    assert "Our main result" in document["intro"]
    assert theorem["section"][-1]["title"] == "Introduction"


def test_plain_text_uppercase_headers_and_proof_of_attachment():
    text = (
        "THEOREM 1. Every bounded sequence has a convergent subsequence.\n\n"
        "LEMMA 2 (Bisection). Every interval can be halved.\n\n"
        "Some remarks in between.\n\n"
        "Proof of Theorem 1. Apply Lemma 2 repeatedly. □\n"
        "Proof. For Lemma 2: midpoint. □\n"
    )
    document = ts.segment_text(ts.normalize(text), file="pasted.txt", fmt="txt")
    assert labels(document) == ["THEOREM 1", "LEMMA 2"]
    theorem, lemma = document["nodes"]
    assert theorem["proof"].startswith("Apply Lemma 2") and theorem["proof_link"] == ["title-ref"]
    assert lemma["proof"].startswith("For Lemma 2") and lemma["proof_link"] == ["preceding"]
    assert lemma["title"] == "Bisection"
    assert [(edge["from"], edge["to"]) for edge in document["edges"]] == [("lemma-2", "theorem-1")]


def test_running_text_is_not_mistaken_for_headers():
    text = "Lemma 2 shows that the claim holds for all n.\nTheorem 1.1, together with Lemma 2, gives the bound.\nlemma 4. lower case\n"
    document = ts.segment_text(text, file="x.txt", fmt="txt")
    assert document["nodes"] == []


def test_cycles_from_text_references_are_broken():
    text = (
        "Lemma 1. A, as used in Lemma 2.\n\nProof. Direct.\n\n"
        "Lemma 2. B.\n\nProof. By Lemma 1.\n"
    )
    document = ts.segment_text(text, file="x.txt", fmt="txt")
    assert [(edge["from"], edge["to"]) for edge in document["edges"]] == [("lemma-1", "lemma-2")]
    assert len(document["dropped_for_acyclicity"]) == 1
    # The statement-level reference is the weakest and is the one dropped.
    assert document["dropped_for_acyclicity"][0]["where"] == ["statement"]


def _pdf(pages: list[list[str]]) -> bytes:
    """A minimal valid PDF with one text line per entry (Helvetica)."""
    objects: list[bytes] = []
    kids = []
    for index, lines in enumerate(pages):
        page_id = 3 + 2 * index
        kids.append(f"{page_id} 0 R")
        ops = ["BT", "/F1 11 Tf", "72 720 Td", "14 TL"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_id + 1} 0 R "
            f"/Resources << /Font << /F1 {3 + 2 * len(pages)} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    tree = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    every = [catalog, tree, *objects, font]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(every, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(every) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(every) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_text_is_extracted_in_the_sandbox_and_segmented(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf([
        ["A Paper Title", "Theorem 1.1. Every even number is even.", "Proof. By Lemma 2.1. QED"],
        ["Lemma 2.1. Two is even.", "Proof. Obvious. QED", "by", "Lemma 2.1. Thus wrapped reference."],
    ]))
    document = ts.pdf_document(pdf, tmp_path / "paper.pdf.txt", file="paper.pdf")
    assert labels(document) == ["Theorem 1.1", "Lemma 2.1"]
    assert document["math_fidelity"] == "pdf-text"
    assert document["parser"].startswith("pypdf-") and document["format"] == "pdf"
    assert [node["pages"] for node in document["nodes"]] == [[1, 1], [2, 2]]
    assert document["nodes"][0]["main_result"]
    assert [(edge["from"], edge["to"]) for edge in document["edges"]] == [("lemma-2-1", "theorem-1-1")]
    assert (tmp_path / "paper.pdf.txt").read_text().count(ts.PAGE_MARK) == 2


def test_unreadable_pdf_is_a_clear_error(tmp_path):
    pytest.importorskip("pypdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot really a pdf")
    with pytest.raises(ts.TextStructureError, match="No text"):
        ts.pdf_document(bad, tmp_path / "bad.txt", file="bad.pdf")


def test_model_segments_must_quote_the_text_verbatim():
    text = (
        "Intro words here.\n\nOur key claim: every widget has a frobnicator attached to it.\n"
        "To see this, inspect the assembly line and count carefully. This ends it.\n\n"
        "Second claim: frobnicators never detach once fitted.\nIt follows from the key claim.\n"
    )
    items = [
        {"kind": "lemma", "label": "Claim 1", "statement_quote": "every widget has a frobnicator attached to it",
         "proof_start_quote": "To see this, inspect", "proof_end_quote": "This ends it."},
        {"kind": "theorem", "label": "Claim 2", "statement_quote": "frobnicators never   detach once fitted",
         "uses": ["Lemma 1"]},
        {"kind": "theorem", "label": "Invented", "statement_quote": "a sentence the model made up entirely"},
        {"kind": "not-a-kind", "label": "x", "statement_quote": "Intro words here."},
    ]
    document = ts.apply_model_segments(text, items, file="pasted.txt", fmt="txt")
    assert [node["statement"] for node in document["nodes"]] == [
        "every widget has a frobnicator attached to it",
        "frobnicators never detach once fitted",
    ]
    lemma, theorem = document["nodes"]
    assert lemma["proof"] == "To see this, inspect the assembly line and count carefully. This ends it."
    assert lemma["lines"] == [3, 3]
    assert document["edges"] == [{"from": "lemma-1", "to": "theorem-2", "label": "uses", "provenance": "model",
                                  "context": "proof", "basis": "model", "strength": 1}]
    assert theorem["main_result"]


def test_chunks_are_paragraph_aligned_and_bounded():
    text = ("para " * 50 + "\n\n") * 400
    pieces = ts.chunks(text, size=1000, limit=4)
    assert len(pieces) == 4
    assert all(len(piece) <= 1000 for _, piece in pieces)
    assert all(text[start:start + len(piece)] == piece for start, piece in pieces)
    assert pieces[1][0] == len(pieces[0][1])


def test_text_files_decode_with_fallback(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes("Theorem 1. Caf\xe9 is even.\n".encode("latin-1"))
    document = ts.parse_text_file(path, file="notes.txt", fmt="txt")
    assert document["nodes"][0]["statement"] == "Caf\xe9 is even."
    assert any("Latin-1" in warning for warning in document["warnings"])
    assert Path(path).exists()


def test_hostile_lines_are_segmented_in_linear_time():
    import time

    lines = []
    for index in range(40):
        lines += ["Theorem" + " " * 400 + "x", "Proof" + "\t" * 400 + "x", "QED" + " " * 400 + "x",
                  "**Lemma " + " _*" * 200 + "q", f"Lemma {index}. " + "Lemma 1 and " * 50]
    text = "\n".join(lines) + "\n# " + "#" * 19_990 + "x\n"
    started = time.monotonic()
    document = ts.segment_text(ts.normalize(text), file="pasted.md", fmt="md")
    assert time.monotonic() - started < 3
    assert len(document["nodes"]) == 40


def test_dense_references_and_many_statements_stay_bounded():
    import random
    import time

    random.seed(7)
    parts = []
    for index in range(1, 801):
        cited = ", ".join(str(number) for number in sorted(random.sample(range(1, 801), 90)))
        parts.append(f"Lemma {index}. Statement {index} holds.\n\nProof. By Lemmas {cited}. QED\n")
    started = time.monotonic()
    document = ts.segment_text(ts.normalize("\n".join(parts)), file="x.txt", fmt="txt")
    assert time.monotonic() - started < 10
    assert len(document["nodes"]) == 256 and document["truncated"] == {"statements": 800, "kept": 256}
    assert any("first 256 are kept" in warning for warning in document["warnings"])
    targets = {}
    for edge in document["edges"]:
        targets.setdefault(edge["to"], set()).add(edge["from"])
    assert max(len(used) for used in targets.values()) <= ts.MAX_REFERENCES


def test_untitled_proofs_never_pile_onto_one_statement():
    text = "Lemma 1. A.\n\nProof. First.\n\n" + "Proof. Another.\n\n" * 500 + "Proof of Lemma 1. Named.\n"
    document = ts.segment_text(text, file="x.txt", fmt="txt")
    lemma = document["nodes"][0]
    assert lemma["proof"] == "First.\n\nNamed." and lemma["proof_link"] == ["preceding", "title-ref"]
    assert any("500 proof(s) could not be matched" in warning for warning in document["warnings"])


def _pdf_with_broken_unicode_map(pages: list[str]) -> bytes:
    """Helvetica with a ToUnicode map that sends "~" to a lone surrogate."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def "
            b"1 begincodespacerange <00> <FF> endcodespacerange 1 beginbfchar <7E> <D835> endbfchar "
            b"endcmap CMapName currentdict /CMap defineresource pop end end")
    objects: list[bytes] = [b"<< /Length %d >>stream\n" % len(cmap) + cmap + b"\nendstream",
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 1 0 R >>"]
    contents = []
    for text in pages:
        stream = b"BT /F1 12 Tf 72 720 Td (" + text.encode() + b") Tj ET"
        objects.append(b"<< /Length %d >>stream\n" % len(stream) + stream + b"\nendstream")
        contents.append(len(objects))
    tree = len(objects) + len(pages) + 1
    kids = []
    for content in contents:
        objects.append(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                       b"/Resources << /Font << /F1 2 0 R >> >> >>" % (tree, content))
        kids.append(len(objects))
    objects.append(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % kid for kid in kids)
                   + b"] /Count %d >>" % len(kids))
    objects.append(b"<< /Type /Catalog /Pages %d 0 R >>" % tree)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer << /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, len(objects), xref)
    return bytes(out)


def test_a_page_with_a_broken_unicode_map_does_not_stop_the_rest(tmp_path):
    pytest.importorskip("pypdf")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_with_broken_unicode_map([
        "Theorem 1. Every even number is even. Proof. Trivial. QED",
        "Lemma 2. A tilde ~ here. Proof. Clear. QED",
        "Theorem 3. The main result. Proof. By Lemma 2. QED",
        "Corollary 4. Also. Proof. By Theorem 3. QED",
    ]))
    document = ts.pdf_document(pdf, tmp_path / "paper.txt", file="paper.pdf")
    assert labels(document) == ["Theorem 1", "Lemma 2", "Theorem 3", "Corollary 4"]
    assert not any("stopped early" in warning for warning in document["warnings"])
    assert [node["pages"] for node in document["nodes"]] == [[1, 1], [2, 2], [3, 3], [4, 4]]


def test_a_character_cut_by_the_size_cap_keeps_the_text_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "MAX_TEXT_CHARS", 100)
    path = tmp_path / "notes.txt"
    text = "Theorem 1. " + "é" * 300  # the 400-byte read cap falls inside a two-byte character
    path.write_bytes(text.encode("utf-8")[:401])
    content, warnings = ts.read_text_file(path, file="notes.txt")
    assert content.startswith("Theorem 1. éé") and "Ã" not in content
    assert not any("Latin-1" in warning for warning in warnings)
    assert any("Only the first 100 characters" in warning for warning in warnings)


def test_segmentation_in_the_sandbox_matches_in_process(tmp_path):
    path = tmp_path / "pasted.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    sandboxed = ts.segment_sandboxed(path, tmp_path / "out.json", file="pasted.md", fmt="md")
    in_process = ts.segment_file(path, file="pasted.md", fmt="md")
    assert sandboxed["nodes"] == in_process["nodes"] and sandboxed["edges"] == in_process["edges"]
    assert not (tmp_path / "out.json").exists()
