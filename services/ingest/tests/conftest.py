"""
Fixtures for ingest tests: real PDFs built on the fly, with text set in the
PDF standard fonts (Helvetica-Bold, Times-Italic, ...), plus the matching
DoclingDocument JSON a docling conversion would produce for them. The
heading-style measurement renders these pages exactly as it renders scans,
so bold, italic and size are measured for real, not mocked.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image
from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size

PAGE_W, PAGE_H = 595.0, 842.0
TOP_BASELINE = 760.0  # first item's baseline unless one is given

BODY_LINES = (
    "Project finance lends against the cash flows of a single asset rather",
    "than the balance sheet of its sponsors, so the lenders look closely at",
    "every contract that could interrupt those cash flows during the term.",
)


@dataclass
class Item:
    label: str                 # "section_header" or "text"
    lines: tuple[str, ...]
    font: str = "Times-Roman"
    size: float = 10.0
    step: float = 40.0         # baseline distance from the previous item's last line
    y: float | None = None     # absolute baseline instead of step

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def H(text: str, font: str = "Helvetica-Bold", size: float = 11.0, step: float = 40.0, y: float | None = None) -> Item:
    return Item("section_header", (text,), font, size, step, y)


def BODY(step: float = 30.0) -> Item:
    """A three-line paragraph (> 150 chars): the body-text stroke reference."""
    return Item("text", BODY_LINES, "Times-Roman", 10.0, step)


def add_text(pdf: pdfium.PdfDocument, page: pdfium.PdfPage, text: str, x: float, y: float, font: str,
             size: float, invisible: bool = False, angle: int = 0) -> tuple[float, float, float, float]:
    """Places one line of text; returns its (left, bottom, right, top) in PDF
    points. angle turns the line counter-clockwise, as a book does with a wide
    table typeset sideways on a portrait page."""
    f = pdfium_c.FPDFText_LoadStandardFont(pdf.raw, font.encode())
    obj = pdfium_c.FPDFPageObj_CreateTextObj(pdf.raw, f, ctypes.c_float(size))
    buf = ctypes.create_string_buffer((text + "\0").encode("utf-16-le"))
    pdfium_c.FPDFText_SetText(obj, ctypes.cast(buf, ctypes.POINTER(pdfium_c.FPDF_WCHAR)))
    if invisible:
        pdfium_c.FPDFTextObj_SetTextRenderMode(obj, pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE)
    cos, sin = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[angle % 360]
    pdfium_c.FPDFPageObj_Transform(obj, cos, sin, -sin, cos, x, y)
    pdfium_c.FPDFPage_InsertObject(page.raw, obj)
    l, b, r, t = (ctypes.c_float() for _ in range(4))
    pdfium_c.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in (l, b, r, t)))
    return l.value, b.value, r.value, t.value


def add_image(pdf: pdfium.PdfDocument, page: pdfium.PdfPage, x: float, y: float, w: float, h: float) -> None:
    """Places a grey rectangle of w x h points with its bottom-left at (x, y):
    a chart or a table pasted onto the page as a picture, with no text of its
    own. Triage only measures the area it covers, so the pixels don't matter."""
    image = pdfium.PdfImage.new(pdf)
    image.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("L", (60, 80), 200)))
    image.set_matrix(pdfium.PdfMatrix().scale(w, h).translate(x, y))
    page.insert_obj(image)


def build(path: Path, pages: list[list[Item]]) -> tuple[Path, dict]:
    """Writes the PDF and returns it with its DoclingDocument JSON (every
    heading at level 1, as docling produces)."""
    pdf = pdfium.PdfDocument.new()
    doc = DoclingDocument(name=path.stem)
    for page_no, items in enumerate(pages, start=1):
        page = pdf.new_page(PAGE_W, PAGE_H)
        doc.add_page(page_no=page_no, size=Size(width=PAGE_W, height=PAGE_H))
        y = TOP_BASELINE + 40
        for item in items:
            y = item.y if item.y is not None else y - item.step
            boxes = []
            for i, line in enumerate(item.lines):
                boxes.append(add_text(pdf, page, line, 60, y - i * item.size * 1.3, item.font, item.size))
            y -= (len(item.lines) - 1) * item.size * 1.3
            bbox = BoundingBox(l=min(b[0] for b in boxes), b=min(b[1] for b in boxes),
                               r=max(b[2] for b in boxes), t=max(b[3] for b in boxes),
                               coord_origin=CoordOrigin.BOTTOMLEFT)
            prov = ProvenanceItem(page_no=page_no, bbox=bbox, charspan=(0, len(item.text)))
            if item.label == "section_header":
                doc.add_heading(text=item.text, level=1, prov=prov)
            else:
                doc.add_text(label=DocItemLabel.TEXT, text=item.text, prov=prov)
        page.gen_content()
    pdf.save(path)
    pdf.close()
    return path, doc.export_to_dict()


@pytest.fixture
def make_doc(tmp_path):
    counter = iter(range(1000))

    def _make(*pages: list[Item]) -> tuple[Path, dict]:
        return build(tmp_path / f"doc{next(counter)}.pdf", list(pages))

    return _make


def headings_of(doc: dict) -> dict[str, dict]:
    """Section headers still in the body, by text."""
    return {t["text"]: t for t in doc["texts"]
            if t["label"] == "section_header" and t.get("content_layer", "body") == "body"}


def levels_of(doc: dict) -> dict[str, int]:
    return {text: t["level"] for text, t in headings_of(doc).items()}
