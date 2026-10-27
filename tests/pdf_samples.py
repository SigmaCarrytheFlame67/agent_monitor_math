"""Small PDFs made at test time with pypdf (no fixture files).

``text_pdf`` writes one Helvetica text line per input line, so pypdf's own
extractor reads the text back; an empty page has no text at all, like a
scanned page.
"""
from __future__ import annotations

import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def text_pdf(pages: list[str], *, password: str | None = None) -> bytes:
    """A PDF whose page ``k`` shows the lines of ``pages[k - 1]``; encrypted
    (RC4, no extra dependency) with ``password`` when one is given."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        commands = []
        y = 770
        for line in text.split("\n") if text else []:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"BT /F1 8 Tf 20 {y} Td ({safe}) Tj ET")
            y -= 9
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    if password:
        writer.encrypt(password, algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def paper(pages: int, *, tag: str = "Paper", line: int = 90, lines: int = 25) -> list[str]:
    """``pages`` pages of distinct, recognisable ASCII text: page ``k`` starts
    with "<tag> page k" and has ``lines`` lines of about ``line`` characters."""
    out = []
    for number in range(1, pages + 1):
        body = [f"{tag} page {number}: Definition {number}.1 defines the module M_{number}."]
        filler = f"{tag} p{number} " + "x" * line
        body += [filler[:line] for _ in range(lines - 1)]
        out.append("\n".join(body))
    return out
