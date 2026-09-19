import json

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

    scan_page()                               # bare scan
    scan_page(with_text=True, invisible=True)  # scan with an old OCR layer
    scan_page(with_text=True)                  # visible text laid over a scan
    pdf.new_page(PAGE_W, PAGE_H)               # empty
    invisible_only = pdf.new_page(PAGE_W, PAGE_H)
    add_text(pdf, invisible_only, "hidden", 60, 700, "Times-Roman", 11, invisible=True)
    invisible_only.gen_content()
    path = tmp_path / "mixed.pdf"
    pdf.save(path)

    pages, counts = ingest.triage_pdf(path)
    assert pages == 6
    assert counts == {"digital": 1, "scanned": 4, "empty": 1}


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
    monkeypatch.setattr(ingest, "convert_digital", lambda client, cfg, path: ("# docling markdown " * 20, doc))

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
    monkeypatch.setattr(ingest, "convert_digital", lambda client, cfg, path: ("# docling markdown " * 20, doc))

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
