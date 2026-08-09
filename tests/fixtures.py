"""
Fixture documents for the doc2md test suite.

Everything is generated at test time rather than committed as binary blobs, so
the repository stays small and the fixtures stay readable and adjustable.
"""

from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont


def make_text_image(path: Path, text: str = "INVOICE TOTAL: 4250 USD") -> Path:
    """
    A clean black-on-white image with legible text — the OCR target.

    The font is deliberately large: Pillow's 11px bitmap default is small enough
    that tesseract misreads digits, which would make OCR assertions flaky for
    reasons that have nothing to do with this project.
    """
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 70), text, fill="black", font=ImageFont.load_default(size=48))
    img.save(path)
    return path


def make_diagram_image(path: Path) -> Path:
    """Few colours, dense straight edges — the shape a flowchart has."""
    img = Image.new("RGB", (500, 400), "white")
    draw = ImageDraw.Draw(img)
    for i in range(3):
        draw.rectangle([50, 40 + i * 120, 250, 120 + i * 120], outline="black", width=4)
        draw.line([150, 120 + i * 120, 150, 160 + i * 120], fill="black", width=4)
    img.save(path)
    return path


def make_photo_image(path: Path) -> Path:
    """
    A deterministic stand-in for a photograph: a full-range tonal gradient with
    fine grain on top. The wide tonal range is what distinguishes a photo from
    line art — pure noise does not, because averaging keeps its grayscale
    spread well below the threshold.
    """
    img = Image.new("RGB", (400, 300))
    px = img.load()
    seed = 12345
    for y in range(300):
        for x in range(400):
            seed = (1103515245 * seed + 12345) % (2**31)
            grain = seed % 24
            base = int(x / 400 * 231)
            px[x, y] = (
                min(255, base + grain),
                min(255, base + (grain * 2) % 24),
                min(255, base + (grain * 3) % 24),
            )
    img.save(path)
    return path


def make_wide_banner(path: Path) -> Path:
    """
    A wide, hard-edged, few-colour graphic (aspect 4.5). Exercises the
    aspect-ratio branch of classify_image, which discards long thin graphics as
    decorative rules.
    """
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    # Full-height solid bars, so downsampling introduces almost no intermediate
    # tones and the unique-colour count stays under MAX_COLORS_DIAGRAM.
    for i in range(3):
        draw.rectangle([i * 300, 0, i * 300 + 150, 200], fill="black")
    img.save(path)
    return path


def make_icon_image(path: Path) -> Path:
    """Below MIN_IMAGE_SIZE — should be skipped as a bullet or spacer."""
    Image.new("RGB", (16, 16), "blue").save(path)
    return path


def make_pdf(path: Path, workdir: Path) -> Path:
    """A two-page PDF with a text layer plus one text image and one diagram."""
    text_img = make_text_image(workdir / "_pdf_text.png")
    diagram = make_diagram_image(workdir / "_pdf_diagram.png")

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Quarterly Report", fontsize=20)
    page.insert_text((72, 140), "Revenue grew across all regions this quarter.", fontsize=11)
    page.insert_image(fitz.Rect(72, 180, 372, 280), filename=str(text_img))

    page2 = doc.new_page()
    page2.insert_text((72, 100), "Process Overview", fontsize=16)
    page2.insert_image(fitz.Rect(72, 130, 372, 370), filename=str(diagram))

    doc.save(str(path))
    doc.close()
    return path


def make_multicolumn_pdf(path: Path) -> Path:
    """Two text columns side by side — the layout naive extractors interleave."""
    doc = fitz.open()
    page = doc.new_page()
    left = ["Left column line one.", "Left column line two.", "Left column line three."]
    right = ["Right column line one.", "Right column line two.", "Right column line three."]
    for i, line in enumerate(left):
        page.insert_text((60, 120 + i * 20), line, fontsize=10)
    for i, line in enumerate(right):
        page.insert_text((320, 120 + i * 20), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def make_table_pdf(path: Path) -> Path:
    """A ruled grid with cell text — exercises table detection."""
    doc = fitz.open()
    page = doc.new_page()
    rows = [
        ("Region", "Revenue", "Growth"),
        ("North", "1200", "12%"),
        ("South", "980", "8%"),
    ]
    x0, y0, cw, rh = 60, 100, 120, 24
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = fitz.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), cell, fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def make_scanned_pdf(path: Path, workdir: Path) -> Path:
    """A page that is one big image with no text layer — must go through OCR."""
    text_img = make_text_image(workdir / "_scan.png", "SCANNED PAGE CONTENT 9876")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(40, 40, 560, 260), filename=str(text_img))
    doc.save(str(path))
    doc.close()
    return path


def make_eml_with_image(path: Path, workdir: Path) -> Path:
    """An email carrying a PNG screenshot attachment."""
    import base64

    png = make_text_image(workdir / "_mail_shot.png", "ATTACHED TOTAL: 777")
    encoded = base64.b64encode(png.read_bytes()).decode()
    wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
    path.write_text(
        "From: Ana <ana@example.com>\n"
        "Subject: With a screenshot\n"
        'Content-Type: multipart/mixed; boundary="B"\n'
        "MIME-Version: 1.0\n"
        "\n"
        "--B\n"
        "Content-Type: text/plain\n"
        "\n"
        "See attached.\n"
        "--B\n"
        "Content-Type: image/png\n"
        'Content-Disposition: attachment; filename="shot.png"\n'
        "Content-Transfer-Encoding: base64\n"
        "\n" + wrapped + "\n"
        "--B--\n",
        encoding="utf-8",
    )
    return path


def make_eml(path: Path) -> Path:
    """A multipart email with headers, an HTML body, and one attachment."""
    path.write_text(
        "From: Ana <ana@example.com>\n"
        "To: Beto <beto@example.com>\n"
        "Subject: Quarterly numbers\n"
        "Date: Mon, 3 Mar 2025 09:12:00 -0600\n"
        'Content-Type: multipart/mixed; boundary="BOUND"\n'
        "MIME-Version: 1.0\n"
        "\n"
        "--BOUND\n"
        "Content-Type: text/html; charset=utf-8\n"
        "\n"
        "<p>Revenue was <b>1200</b> in the north region.</p>\n"
        "--BOUND\n"
        'Content-Type: text/plain; name="notes.txt"\n'
        'Content-Disposition: attachment; filename="notes.txt"\n'
        "\n"
        "attached notes\n"
        "--BOUND--\n",
        encoding="utf-8",
    )
    return path


def make_html(path: Path) -> Path:
    path.write_text(
        "<html><body><h1>Test Doc</h1><p>Hello <b>world</b>.</p>"
        "<ul><li>one</li><li>two</li></ul></body></html>",
        encoding="utf-8",
    )
    return path


def make_csv(path: Path) -> Path:
    path.write_text("region,revenue\nNorth,1200\nSouth,980\n", encoding="utf-8")
    return path


def make_docx(path: Path) -> Path:
    """
    A minimal but valid .docx written by hand.

    Avoids depending on python-docx, which is not a runtime dependency of this
    project and would otherwise be installed only to build a test fixture.
    """
    import zipfile

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Design Notes</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>The pipeline converts documents to Markdown.</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    return path


def build_all(workdir: Path) -> dict[str, Path]:
    """Generate every fixture into workdir and return them by name."""
    workdir.mkdir(parents=True, exist_ok=True)
    return {
        "pdf": make_pdf(workdir / "sample.pdf", workdir),
        "pdf_multicolumn": make_multicolumn_pdf(workdir / "multicolumn.pdf"),
        "pdf_table": make_table_pdf(workdir / "table.pdf"),
        "pdf_scanned": make_scanned_pdf(workdir / "scanned.pdf", workdir),
        "docx": make_docx(workdir / "notes.docx"),
        "html": make_html(workdir / "page.html"),
        "eml": make_eml(workdir / "message.eml"),
        "eml_image": make_eml_with_image(workdir / "with_image.eml", workdir),
        "csv": make_csv(workdir / "data.csv"),
        "png": make_text_image(workdir / "screenshot.png"),
        "diagram": make_diagram_image(workdir / "diagram.png"),
        "photo": make_photo_image(workdir / "photo.png"),
        "icon": make_icon_image(workdir / "icon.png"),
        "banner": make_wide_banner(workdir / "banner.png"),
    }
