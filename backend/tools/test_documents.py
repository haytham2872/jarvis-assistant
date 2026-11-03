"""Documents Sam sends, read locally (4 October 2026). Files are built in memory."""
import io

import pytest

from backend.tools import documents as D


def _docx():
    import docx
    d = docx.Document()
    d.add_heading("Lease renewal", 1)
    d.add_paragraph("Rent rises to 1,240 EUR from 1 November.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Item", "Amount"
    t.cell(1, 0).text, t.cell(1, 1).text = "Deposit", "2,480"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Month", "Spend"])
    ws.append(["Sept", 1830.5])
    wb.create_sheet("Notes").append(["fine"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx():
    from pptx import Presentation
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Q3 review"
    slide.placeholders[1].text = "Revenue up 12%"
    slide.notes_slide.notes_text_frame.text = "mention churn"
    buf = io.BytesIO()
    deck.save(buf)
    return buf.getvalue()


def text_pdf(lines):
    """A minimal PDF with a real text layer, one page per entry."""
    objs, kids = [b"<< /Type /Catalog /Pages 2 0 R >>", None], []
    for text in lines:
        stream = b"BT /F1 18 Tf 72 720 Td (" + text.encode() + b") Tj ET"
        page_no = len(objs) + 1
        kids.append(page_no)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 FONT 0 R >> >> >>" % (page_no + 1))
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    font = len(objs) + 1
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objs = [o.replace(b"FONT", str(font).encode()) if o else o for o in objs]
    objs[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    pdf, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return pdf + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def scanned_pdf(pages=1):
    from PIL import Image, ImageDraw
    images = []
    for n in range(pages):
        img = Image.new("RGB", (620, 877), "white")
        ImageDraw.Draw(img).text((60, 60), f"Receipt page {n + 1} total 42.10", fill="black")
        images.append(img)
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_word_keeps_headings_and_tables_in_order():
    doc = D.convert(_docx(), "lease.docx")
    assert doc.kind == "docx" and not doc.error
    assert doc.text.index("## Lease renewal") < doc.text.index("Rent rises to 1,240 EUR")
    assert "| Deposit | 2,480 |" in doc.text


def test_excel_gives_one_section_per_sheet():
    doc = D.convert(_xlsx(), "budget.xlsx")
    assert doc.sections == 2
    assert "## Sheet: Budget" in doc.text and "| Sept | 1830.5 |" in doc.text and "## Sheet: Notes" in doc.text


def test_powerpoint_gives_slides_and_speaker_notes():
    doc = D.convert(_pptx(), "deck.pptx")
    assert "## Slide 1" in doc.text and "Revenue up 12%" in doc.text and "Notes: mention churn" in doc.text


def test_a_pdf_with_a_text_layer_is_read_page_by_page():
    doc = D.convert(text_pdf(["Invoice total 850.65 EUR due 14 October", "Page two text here"]), "invoice.pdf")
    assert doc.sections == 2 and doc.scanned == []
    assert "## Page 1\n\nInvoice total 850.65 EUR due 14 October" in doc.text and "## Page 2" in doc.text


def test_a_short_real_line_is_not_mistaken_for_a_scan():
    """Found while building it: a 39-character page was sent to the vision model for nothing."""
    assert D.convert(text_pdf(["Total 12.40"]), "t.pdf").scanned == []


def test_a_scanned_page_is_marked_and_renders_for_the_vision_model():
    data = scanned_pdf(2)
    doc = D.convert(data, "scan.pdf")
    assert doc.scanned == [1, 2] and doc.text.count("(no text layer)") == 2
    images = D.render_pages(data, doc.scanned)
    assert len(images) == 2 and all(i[:2] == b"\xff\xd8" for i in images)   # JPEG


def test_scanned_pages_sent_to_the_model_are_capped():
    data = scanned_pdf(3)
    assert len(D.render_pages(data, [1, 2, 3], scale=0.3)) == 3
    assert D.MAX_SCANNED_PAGES == 12


def test_csv_html_json_and_text():
    assert "| 2026-10-01 | 12.5 |" in D.convert(b"date;amount\n2026-10-01;12.5\n", "s.csv").text
    html = b"<html><body><article><h1>Notice</h1><p>The office closes on Friday at noon for works.</p>" \
           b"<p>Please collect your parcels before then.</p></article></body></html>"
    assert "closes on Friday" in D.convert(html, "n.html").text
    assert '"total": 3' in D.convert(b'{"total":3}', "x.json").text
    assert D.convert("café crème".encode("utf-16"), "note.txt").text == "café crème"


def test_a_very_long_document_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(D, "MAX_CHARS", 50)
    doc = D.convert(b"x" * 500, "long.txt")
    assert len(doc.text) == 50 and doc.cut


@pytest.mark.parametrize("name", ["archive.zip", "song.flac", "noext"])
def test_an_unknown_kind_is_refused_by_name(name):
    doc = D.convert(b"data", name)
    assert doc.error.startswith("I can't read")


def test_a_corrupt_file_never_raises():
    doc = D.convert(b"not really a docx", "broken.docx")
    assert doc.error and doc.kind == "docx"


def test_a_page_with_only_a_stray_mark_is_read_as_a_scan():
    doc = D.convert(text_pdf(["x."]), "scan-with-a-mark.pdf")
    assert doc.scanned == [1] and "(no text layer)" in doc.text and "x." not in doc.text
