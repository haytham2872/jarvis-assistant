"""Documents the owner sends, turned into text JARVIS can read. Pure and local: no model call.

PDF (pypdf), Word (python-docx), Excel (openpyxl), PowerPoint (python-pptx), CSV, plain
text, Markdown, JSON and HTML (trafilatura). Sections become `## ` headings -- one per PDF
page, slide or sheet -- so vault_read can show a table of contents and read one part of a
long document instead of the whole thing.

A PDF page with no text layer (a scan, a photographed receipt) is returned as an image to be
read by the vision model: `Document.scanned` lists those pages, rendered by pypdfium2.

MarkItDown was considered and not used: its file-type detector pins onnxruntime 1.20, below
the 1.25 Parakeet, fastembed and the reranker run on (checked 4 October 2026).
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# What a document may be, by extension. Anything else is not read.
KINDS = {
    ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx",
    ".csv": "csv", ".tsv": "csv", ".txt": "text", ".md": "text", ".markdown": "text",
    ".log": "text", ".json": "json", ".html": "html", ".htm": "html", ".xml": "text",
}
MAX_CHARS = 300_000          # ~75k tokens kept per document; the rest is cut and said so
_SCAN_MIN_CHARS = 10         # a page with less text than this has no real text layer (a scan)
MAX_SCANNED_PAGES = 12       # pages sent to the vision model per document
_MAX_ROWS = 500              # per spreadsheet sheet
_MAX_COLS = 30


@dataclass
class Document:
    text: str = ""                     # Markdown, `## ` per page / slide / sheet
    kind: str = ""
    sections: int = 0                  # pages, slides or sheets
    scanned: list[int] = field(default_factory=list)   # 1-based PDF pages with no text layer
    cut: bool = False                  # longer than MAX_CHARS
    error: str = ""


def kind_of(filename: str) -> str:
    return KINDS.get(Path(filename or "").suffix.lower(), "")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _pdf(data: bytes) -> Document:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts, scanned = [], []
    for n, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if len(text) < _SCAN_MIN_CHARS:      # nothing, or a stray mark: read it as an image
            scanned.append(n)
            text = ""
        parts.append(f"## Page {n}\n\n{text}" if text else f"## Page {n}\n\n(no text layer)")
    return Document("\n\n".join(parts), "pdf", len(reader.pages), scanned)


def _docx(data: bytes) -> Document:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d = docx.Document(io.BytesIO(data))
    lines, headings = [], 0
    for block in d.element.body.iterchildren():      # paragraphs and tables, in order
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(block, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading") or style == "title":
                level = 1 if style == "title" else min(6, int("".join(c for c in style if c.isdigit()) or 1))
                lines.append("#" * max(2, level + 1) + " " + text)
                headings += 1
            else:
                lines.append(text)
        elif tag == "tbl":
            table = Table(block, d)
            rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in table.rows]
            lines.append(_table(rows))
    return Document("\n\n".join(lines), "docx", headings)


def _table(rows: list[list[str]]) -> str:
    rows = [r[:_MAX_COLS] for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [[str(c).replace("|", "/") for c in r] + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _xlsx(data: bytes) -> Document:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            if len(rows) >= _MAX_ROWS:
                break
            rows.append(["" if v is None else str(v) for v in row])
        more = f"\n\n(first {_MAX_ROWS} rows only)" if len(rows) >= _MAX_ROWS else ""
        parts.append(f"## Sheet: {sheet.title}\n\n{_table(rows) or '(empty)'}{more}")
    wb.close()
    return Document("\n\n".join(parts), "xlsx", len(parts))


def _pptx(data: bytes) -> Document:
    from pptx import Presentation
    deck = Presentation(io.BytesIO(data))
    parts = []
    for n, slide in enumerate(deck.slides, 1):
        texts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            elif getattr(shape, "has_table", False):
                texts.append(_table([[c.text for c in r.cells] for r in shape.table.rows]))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            texts.append("Notes: " + slide.notes_slide.notes_text_frame.text.strip())
        parts.append(f"## Slide {n}\n\n" + ("\n\n".join(texts) or "(no text)"))
    return Document("\n\n".join(parts), "pptx", len(parts))


def _csv(data: bytes) -> Document:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        rows = list(csv.reader(io.StringIO(text), dialect))
    except csv.Error:
        rows = list(csv.reader(io.StringIO(text)))
    more = f"\n\n(first {_MAX_ROWS} of {len(rows)} rows)" if len(rows) > _MAX_ROWS else ""
    return Document(_table(rows[:_MAX_ROWS]) + more, "csv", 1)


def _html(data: bytes) -> Document:
    import trafilatura
    text = trafilatura.extract(_decode(data), include_tables=True) or ""
    return Document(text, "html", 0)


def _json(data: bytes) -> Document:
    text = _decode(data)
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    return Document("```json\n" + text + "\n```", "json", 0)


_READERS = {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "csv": _csv,
            "html": _html, "json": _json,
            "text": lambda data: Document(_decode(data), "text", 0)}


def convert(data: bytes, filename: str) -> Document:
    """The document as text. Never raises: an unreadable file comes back with `error` set."""
    kind = kind_of(filename)
    if not kind:
        return Document(error=f"I can't read {Path(filename).suffix or 'this kind of'} files")
    try:
        doc = _READERS[kind](data)
    except Exception as e:
        log.warning("documents: could not read %s (%s)", filename, e)
        return Document(kind=kind, error=f"the file could not be read ({type(e).__name__})")
    if len(doc.text) > MAX_CHARS:
        doc.text, doc.cut = doc.text[:MAX_CHARS], True
    return doc


def render_pages(data: bytes, pages: list[int], scale: float = 1.6) -> list[bytes]:
    """The given 1-based PDF pages as JPEG images (~1300 px wide for A4), for the vision model."""
    import pypdfium2 as pdfium
    out = []
    pdf = pdfium.PdfDocument(data)
    try:
        for n in pages[:MAX_SCANNED_PAGES]:
            if 1 <= n <= len(pdf):
                image = pdf[n - 1].render(scale=scale).to_pil().convert("RGB")
                buf = io.BytesIO()
                image.save(buf, "JPEG", quality=85)
                out.append(buf.getvalue())
    finally:
        pdf.close()
    return out
