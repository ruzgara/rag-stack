import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from conftest import BODY, PAGE_H, PAGE_W, H, add_text, levels_of
from PIL import Image

import ingest


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point every data path ingest uses at a temp dir."""
    paths = {name: tmp_path / name.lower() for name in
             ("INBOX", "WORK", "PROCESSED", "FAILED", "MARKDOWN", "DOCLING_JSON")}
    for name, path in paths.items():
        path.mkdir()
        monkeypatch.setattr(ingest, name, path)
    monkeypatch.setattr(ingest, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(ingest, "REPO_ROOT", tmp_path)
    return paths


def config(**overrides) -> ingest.Config:
    cfg = ingest.Config.from_env({"MIN_CHARS_PER_PAGE": "10"}, force_vlm=False)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ------------------------------------------------------------------ triage --

def test_triage_classifies_each_page(tmp_path):
    pdf = pdfium.PdfDocument.new()
    digital = pdf.new_page(PAGE_W, PAGE_H)
    add_text(pdf, digital, "Born-digital text", 60, 700, "Times-Roman", 11)
    digital.gen_content()

    def scan_page(with_text: bool = False, invisible: bool = False):
        page = pdf.new_page(PAGE_W, PAGE_H)
        image = pdfium.PdfImage.new(pdf)
        image.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("L", (60, 80), 200)))
        image.set_matrix(pdfium.PdfMatrix().scale(PAGE_W, PAGE_H))
        page.insert_obj(image)
        if with_text:
            add_text(pdf, page, "OCR layer", 60, 700, "Times-Roman", 11, invisible=invisible)
        page.gen_content()

    scan_page()                               # bare scan -> scan
    scan_page(with_text=True, invisible=True)  # scan with an old OCR layer -> stale-ocr
    scan_page(with_text=True)                  # visible text stamped over a scan -> still scan
    pdf.new_page(PAGE_W, PAGE_H)               # empty
    invisible_only = pdf.new_page(PAGE_W, PAGE_H)
    add_text(pdf, invisible_only, "hidden", 60, 700, "Times-Roman", 11, invisible=True)
    invisible_only.gen_content()               # invisible text, no image -> stale-ocr
    path = tmp_path / "mixed.pdf"
    pdf.save(path)

    pages, counts, labels = ingest.triage_pdf(path)
    assert pages == 6
    assert counts == {"digital": 1, "scan": 2, "stale-ocr": 2, "empty": 1}
    assert labels == ["digital", "scan", "stale-ocr", "scan", "empty", "stale-ocr"]


def mixed_pdf(path, scans: int, digital: int, sideways: tuple[int, ...] = (), stale_ocr: int = 0):
    """A PDF of `scans` bare page-sized images, `stale_ocr` page-sized images
    carrying an old invisible OCR layer, then `digital` text pages."""
    pdf = pdfium.PdfDocument.new()
    for i in range(scans + stale_ocr):
        page = pdf.new_page(PAGE_W, PAGE_H)
        image = pdfium.PdfImage.new(pdf)
        image.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("L", (60, 80), 200)))
        image.set_matrix(pdfium.PdfMatrix().scale(PAGE_W, PAGE_H))
        page.insert_obj(image)
        if i >= scans:
            add_text(pdf, page, "old OCR layer", 60, 700, "Times-Roman", 11, invisible=True)
        page.gen_content()
    for i in range(digital):
        page = pdf.new_page(PAGE_W, PAGE_H)
        angle = 90 if scans + stale_ocr + i in sideways else 0
        add_text(pdf, page, "Born-digital text", 60, 700, "Times-Roman", 11, angle=angle)
        page.gen_content()
    pdf.save(path)
    pdf.close()
    return path


def test_a_scanned_cover_doesnt_send_the_whole_book_to_ocr(dirs, monkeypatch):
    """The bug this guards: one cover image used to OCR every page, and OCR
    turns the embedded text's £ into € and Baa2 into Baaz."""
    book = mixed_pdf(dirs["INBOX"] / "book.pdf", scans=1, digital=19)
    seen = {}

    def fake_digital(client, cfg, path, ocr_images=False):
        seen["ocr_images"] = ocr_images
        return "text " * 100, {"texts": []}

    monkeypatch.setattr(ingest, "convert_digital", fake_digital)
    monkeypatch.setattr(ingest, "convert_ocr", lambda *a: pytest.fail("the whole book went to OCR"))

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.status == "processed", result.error
    assert result.route == "digital+ocr-images"
    assert seen["ocr_images"] is True  # the cover's own image is still read


def test_a_fully_scanned_pdf_with_no_text_layer_takes_the_auto_route(dirs, monkeypatch):
    """No stale-ocr page means nothing needs overwriting, so even a book
    that's all scans is read through ocr_images rather than force_ocr."""
    book = mixed_pdf(dirs["INBOX"] / "scan.pdf", scans=8, digital=0)
    seen = {}

    def fake_digital(client, cfg, path, ocr_images=False):
        seen["ocr_images"] = ocr_images
        return "text " * 100, {"texts": []}

    monkeypatch.setattr(ingest, "convert_digital", fake_digital)
    monkeypatch.setattr(ingest, "convert_ocr", lambda *a: pytest.fail("force-OCR route taken with no stale-ocr page"))

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.status == "processed", result.error
    assert result.route == "digital+ocr-images"
    assert seen["ocr_images"] is True


def test_a_stale_ocr_page_sends_the_whole_book_to_force_ocr(dirs, monkeypatch):
    """An old, untrustworthy OCR text layer has to be overwritten, and
    docling-serve takes one option set per file, so the whole book goes
    through force_ocr even though most of it is born-digital."""
    book = mixed_pdf(dirs["INBOX"] / "book.pdf", scans=0, digital=5, stale_ocr=1)
    monkeypatch.setattr(ingest, "convert_digital", lambda *a, **k: pytest.fail("a stale-ocr page took the digital route"))
    monkeypatch.setattr(ingest, "convert_ocr", lambda client, cfg, path: ("text " * 100, {"texts": []}))

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.route == "ocrmac"


def test_a_fully_digital_pdf_is_not_given_an_ocr_engine(dirs, monkeypatch):
    book = mixed_pdf(dirs["INBOX"] / "clean.pdf", scans=0, digital=5)
    seen = {}
    monkeypatch.setattr(ingest, "convert_digital",
                        lambda client, cfg, path, ocr_images=False: (seen.update(ocr_images=ocr_images), "text " * 100, {"texts": []})[1:])

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.route == "digital" and seen["ocr_images"] is False


def test_convert_digital_leaves_pages_with_their_own_text_alone(monkeypatch):
    seen = {}
    monkeypatch.setattr(ingest, "docling_convert",
                        lambda client, cfg, path, options, label: (seen.update(options=options, label=label), ("md", {}))[1])
    cfg = config()

    ingest.convert_digital(None, cfg, Path("x.pdf"))
    assert seen["options"]["do_ocr"] == "false" and "ocr_engine" not in seen["options"]

    ingest.convert_digital(None, cfg, Path("x.pdf"), ocr_images=True)
    # force_ocr off: only the bitmap areas are read, the text layer is kept.
    assert seen["options"]["do_ocr"] == "true" and seen["options"]["force_ocr"] == "false"
    assert seen["options"]["ocr_engine"] == "ocrmac" and seen["label"] == "digital+ocr-images"


# ---------------------------------------------------------------- rotation --

def test_sideways_digital_pages_are_found_from_their_text(tmp_path):
    path = mixed_pdf(tmp_path / "wide.pdf", scans=0, digital=4, sideways=(1, 3))
    assert ingest.text_rotations(path) == {1: 90, 3: 90}


def test_a_sideways_digital_page_is_marked_rotated_without_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WORK", tmp_path / "work")
    monkeypatch.setattr(ingest, "detect_orientation", lambda png: pytest.fail("no OCR is needed to find these"))
    path = mixed_pdf(tmp_path / "wide.pdf", scans=0, digital=3, sideways=(2,))

    out, rotated = ingest.fix_rotation(path, "abc123", ["digital", "digital", "digital"], workers=1)

    assert rotated == [3] and out == tmp_path / "work" / "abc123.pdf"
    pdf = pdfium.PdfDocument(out)
    assert [pdf[i].get_rotation() for i in range(3)] == [0, 0, 90]
    pdf.close()


def test_an_upright_digital_pdf_is_converted_in_place(tmp_path):
    path = mixed_pdf(tmp_path / "flat.pdf", scans=0, digital=2)
    assert ingest.fix_rotation(path, "abc123", ["digital", "digital"], workers=1) == (path, [])


def test_a_scan_page_is_measured_by_osd_and_a_digital_page_beside_it_isnt(tmp_path, monkeypatch):
    """A mixed document: the scan page gets OSD, the digital page next to it
    is measured from its text matrices and never touches tesseract."""
    monkeypatch.setattr(ingest, "WORK", tmp_path / "work")
    ingest.WORK.mkdir()
    path = mixed_pdf(tmp_path / "mixed.pdf", scans=1, digital=1)
    osd_calls = []

    def fake_osd(png):
        osd_calls.append(png)
        return 90, ingest.OSD_MIN_CONFIDENCE + 1

    monkeypatch.setattr(ingest, "detect_orientation", fake_osd)

    out, rotated = ingest.fix_rotation(path, "abc123", ["scan", "digital"], workers=1)

    assert len(osd_calls) == 1  # only the scan page was rendered and OSD'd
    assert rotated == [1]
    pdf = pdfium.PdfDocument(out)
    assert [pdf[i].get_rotation() for i in range(2)] == [90, 0]
    pdf.close()


def test_missing_tesseract_skips_osd_but_keeps_text_rotations(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "WORK", tmp_path / "work")
    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    path = mixed_pdf(tmp_path / "mixed.pdf", scans=1, digital=1, sideways=(1,))

    out, rotated = ingest.fix_rotation(path, "abc123", ["scan", "digital"], workers=1)

    assert rotated == [2]  # the digital page's own angle still applies
    pdf = pdfium.PdfDocument(out)
    assert [pdf[i].get_rotation() for i in range(2)] == [0, 90]
    pdf.close()


def test_rejected_embedded_text_gives_up_its_own_rotation_measurement(dirs, monkeypatch):
    """When the quality gate rejects a page's text, its matrices are no more
    trustworthy than its characters were, so OSD measures those pages too."""
    book = mixed_pdf(dirs["INBOX"] / "broken.pdf", scans=0, digital=3)
    monkeypatch.setattr(ingest, "convert_digital", lambda client, cfg, path, ocr_images=False: ("", {}))
    monkeypatch.setattr(ingest, "convert_ocr", lambda client, cfg, path: ("text " * 100, {"texts": []}))
    osd_pages = []
    monkeypatch.setattr(ingest, "detect_orientation", lambda png: (osd_pages.append(png), (0, 0.0))[1])

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.route == "digital+ocrmac"  # the rejected attempt stays in the route
    assert len(osd_pages) == 3  # every page, though triage called them digital


def test_control_characters_are_stripped_from_the_output():
    """Symbol-font bullets arrive as 0x07/0x08 in born-digital text."""
    doc = {"texts": [{"text": "\x07 Asset pledge", "orig": "\x07 Asset pledge", "self_ref": "#/texts/0"}],
           "tables": [{"data": {"table_cells": [{"text": "Share\x08 pledge", "start_row_offset_idx": 0}]}}]}
    md, clean = ingest.strip_control_chars("- \x07 Asset pledge\n", doc)

    assert md == "-  Asset pledge\n"
    assert clean["texts"][0]["text"] == " Asset pledge"
    assert clean["tables"][0]["data"]["table_cells"][0]["text"] == "Share pledge"
    assert clean["texts"][0]["self_ref"] == "#/texts/0"  # non-text fields untouched
    assert doc["texts"][0]["text"] == "\x07 Asset pledge"  # input left alone


def test_newlines_and_tabs_survive():
    md, _ = ingest.strip_control_chars("a\nb\tc", {})
    assert md == "a\nb\tc"


def test_process_one_writes_text_without_control_characters(dirs, monkeypatch):
    book = mixed_pdf(dirs["INBOX"] / "book.pdf", scans=0, digital=3)
    monkeypatch.setattr(ingest, "convert_digital",
                        lambda client, cfg, path, ocr_images=False: ("ordinary text " * 100 + "\x07", {"texts": []}))

    result = ingest.process_one(None, config(heading_levels=False), book, "abc123", osd_workers=1)

    assert result.status == "processed", result.error
    assert "\x07" not in (dirs["MARKDOWN"] / "abc123.md").read_text()


# ------------------------------------------------------------ quality gate --

@pytest.mark.parametrize("md, pages, reason", [
    ("", 1, "no text"),
    ("   \n ", 1, "no text"),
    ("x" * 90 + "�" * 10, 1, "junk"),
    ("short", 2, "below"),
    ("x" * 500, 2, None),
])
def test_quality_gate(md, pages, reason):
    result = ingest.quality_gate_reason(md, pages, min_chars_per_page=100)
    if reason is None:
        assert result is None
    else:
        assert reason in result


# ------------------------------------------------------------------ config --

@pytest.mark.parametrize("value, expected", [(None, True), ("true", True), ("0", False), ("off", False), ("", True)])
def test_heading_levels_setting(value, expected):
    env = {} if value is None else {"INGEST_HEADING_LEVELS": value}
    assert ingest.Config.from_env(env, force_vlm=False).heading_levels is expected


def test_ocr_lang_accepts_commas_and_spaces():
    assert ingest.Config.from_env({"OCR_LANG": "en-US, de-DE"}, force_vlm=False).ocr_lang == ["en-US", "de-DE"]
    assert ingest.Config.from_env({}, force_vlm=False).ocr_lang == ["en-US"]


# ---------------------------------------------------------- heading levels --

def test_fix_heading_levels_reexports_markdown(make_doc):
    pdf, doc = make_doc([H("Chapter 1 Introduction", size=20), BODY(), H("RISKS", font="Times-Bold"), BODY()])
    md, fixed, source = ingest.fix_heading_levels(pdf, "old markdown", doc)
    assert source == "style"
    assert "## Chapter 1 Introduction" in md and "### RISKS" in md
    assert levels_of(fixed) == {"Chapter 1 Introduction": 1, "RISKS": 2}
    assert levels_of(doc) == {"Chapter 1 Introduction": 1, "RISKS": 1}  # input left untouched


def test_fix_heading_levels_keeps_input_on_failure(make_doc, tmp_path):
    _, doc = make_doc([H("1 Introduction"), BODY()])
    md, fixed, source = ingest.fix_heading_levels(tmp_path / "missing.pdf", "old markdown", doc)
    assert (md, fixed, source) == ("old markdown", doc, None)


def test_process_one_applies_heading_levels(make_doc, dirs, monkeypatch):
    pdf, doc = make_doc([H("Chapter 1 Introduction", size=20), BODY(), H("RISKS", font="Times-Bold"), BODY()])
    inbox_file = dirs["INBOX"] / "book.pdf"
    pdf.rename(inbox_file)
    monkeypatch.setattr(ingest, "convert_digital",
                        lambda client, cfg, path, ocr_images=False: ("# docling markdown " * 20, doc))

    result = ingest.process_one(None, config(), inbox_file, "abc123", osd_workers=1)

    assert result.status == "processed", result.error
    assert result.route == "digital+levels:style"
    saved = json.loads((dirs["DOCLING_JSON"] / "abc123.json").read_text())
    assert levels_of(saved) == {"Chapter 1 Introduction": 1, "RISKS": 2}
    assert "### RISKS" in (dirs["MARKDOWN"] / "abc123.md").read_text()
    assert (dirs["PROCESSED"] / "book.pdf").exists() and not inbox_file.exists()
    row = json.loads(ingest.MANIFEST.read_text().splitlines()[-1])
    assert row["docling_json"] == "docling_json/abc123.json"


def test_process_one_without_heading_levels(make_doc, dirs, monkeypatch):
    pdf, doc = make_doc([H("Chapter 1 Introduction", size=20), BODY(), H("RISKS", font="Times-Bold"), BODY()])
    inbox_file = dirs["INBOX"] / "book.pdf"
    pdf.rename(inbox_file)
    monkeypatch.setattr(ingest, "convert_digital",
                        lambda client, cfg, path, ocr_images=False: ("# docling markdown " * 20, doc))

    result = ingest.process_one(None, config(heading_levels=False), inbox_file, "abc123", osd_workers=1)

    assert result.route == "digital"
    saved = json.loads((dirs["DOCLING_JSON"] / "abc123.json").read_text())
    assert levels_of(saved) == {"Chapter 1 Introduction": 1, "RISKS": 1}


def test_fix_headings_only_rewrites_stored_outputs(make_doc, dirs, capsys):
    pdf, doc = make_doc([H("Chapter 1 Introduction", size=20), BODY(), H("RISKS", font="Times-Bold"), BODY()])
    pdf.rename(dirs["PROCESSED"] / "book.pdf")
    (dirs["DOCLING_JSON"] / "abc123.json").write_text(json.dumps(doc))
    (dirs["MARKDOWN"] / "abc123.md").write_text("old")
    rows = [
        {"filename": "book.pdf", "sha256": "abc123", "status": "processed", "route": "digital",
         "docling_json": "docling_json/abc123.json", "markdown_path": "markdown/abc123.md"},
        {"filename": "gone.pdf", "sha256": "def456", "status": "failed"},
    ]
    ingest.MANIFEST.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    assert ingest.fix_headings_only(config()) == 0

    saved = json.loads((dirs["DOCLING_JSON"] / "abc123.json").read_text())
    assert levels_of(saved) == {"Chapter 1 Introduction": 1, "RISKS": 2}
    assert "### RISKS" in (dirs["MARKDOWN"] / "abc123.md").read_text()
    out = capsys.readouterr().out
    assert "headings from style" in out and "1 updated, 0 unchanged" in out


def test_fix_headings_only_uses_the_rotated_copy(make_doc, dirs, monkeypatch):
    # docling's boxes refer to the rotated copy it converted, not the original.
    _, doc = make_doc([H("1 Introduction"), BODY()])
    work_pdf, _ = make_doc([H("1 Introduction"), BODY()])
    work_pdf.rename(dirs["WORK"] / "abc123.pdf")
    (dirs["PROCESSED"] / "book.pdf").write_bytes(b"not used")
    (dirs["DOCLING_JSON"] / "abc123.json").write_text(json.dumps(doc))
    ingest.MANIFEST.write_text(json.dumps(
        {"filename": "book.pdf", "sha256": "abc123", "status": "processed", "route": "rotated:3+ocrmac",
         "docling_json": "docling_json/abc123.json"}) + "\n")
    used = []
    real = ingest.fix_heading_levels
    monkeypatch.setattr(ingest, "fix_heading_levels", lambda path, *a, **kw: used.append(path) or real(path, *a, **kw))

    ingest.fix_headings_only(config())

    assert used == [dirs["WORK"] / "abc123.pdf"]
