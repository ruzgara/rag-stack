"""
The OCR route end to end, on one real scanned page: rotation detection,
ocrmac through docling-serve, table reconstruction and heading levels.

The fixture (tests/fixtures/basel-p5-rotated.pdf) is page 5 of the Basel III
case study, turned 90 degrees. It carries the RSF factor table, which is
where a sideways page does the most damage: read as-is, docling merges the
"15%" and "50%" rows, and an answer about Level 2B assets comes out with the
wrong factor.

Unlike the other tests, these need docling-serve running (`./ragctl up`) and
take ~20s; they skip themselves when it isn't reachable. Run just these with
`uv run pytest -m integration`, or skip them with `-m "not integration"`.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "basel-p5-rotated.pdf"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cfg() -> ingest.Config:
    config = ingest.Config.from_env(ingest.load_env(), force_vlm=False)
    try:
        httpx.get(f"{config.docling_url}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"docling-serve not reachable at {config.docling_url} ({exc}); run ./ragctl up")
    return config


@pytest.fixture(scope="module")
def converted(cfg, tmp_path_factory) -> dict:
    """The fixture page through the real OCR route, once for all tests."""
    monkeypatch = pytest.MonkeyPatch()  # module-scoped, so not the fixture
    monkeypatch.setattr(ingest, "WORK", tmp_path_factory.mktemp("work"))
    pages, counts, labels = ingest.triage_pdf(FIXTURE)
    work, rotated = ingest.fix_rotation(FIXTURE, "fixture", labels, 2)
    with httpx.Client() as client:
        md, doc = ingest.convert_ocr(client, cfg, work)
    fixed_md, fixed_doc, source = ingest.fix_heading_levels(work, md, doc)
    monkeypatch.undo()
    return {"pages": pages, "counts": counts, "rotated": rotated, "work": work,
            "md": fixed_md, "doc": fixed_doc, "heading_source": source}


def table_rows(md: str) -> dict[str, str]:
    """The markdown table's rows, by their first cell."""
    rows = {}
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) == 2 and not set(cells[0]) <= set("-: "):
            rows[cells[0]] = cells[1]
    return rows


# ------------------------------------------------------------------ route --

def test_the_page_is_recognised_as_a_scan(converted):
    assert (converted["pages"], converted["counts"]) == (1, {"digital": 0, "scan": 1, "stale-ocr": 0, "empty": 0})


def test_the_sideways_page_is_turned_upright(converted):
    assert converted["rotated"] == [1]
    assert converted["work"].name == "fixture.pdf"  # the rotated copy, not the original


# ------------------------------------------------------------------- text --

def test_the_page_text_is_readable(converted):
    md = converted["md"]
    assert "Project Finance Overview" in md
    # Body text, including names OCR tends to mangle.
    for phrase in ("International Project Finance Association", "Cheniere", "Sabine Pass",
                   "liquefied natural gas"):
        assert phrase in md, phrase
    assert "$3.6 billion" in md


def test_almost_no_junk_characters(converted):
    md = converted["md"]
    assert ingest.quality_gate_reason(md, pages=1, min_chars_per_page=2000) is None
    assert len(ingest.JUNK_CHAR_RE.findall(md)) == 0


# ------------------------------------------------------------------ table --

def test_the_table_keeps_its_shape(converted):
    tables = converted["doc"]["tables"]
    assert len(tables) == 1
    data = tables[0]["data"]
    assert (data["num_rows"], data["num_cols"]) == (8, 2)  # header + 7 RSF factors
    first_column = [c["text"] for c in data["table_cells"] if c["start_col_offset_idx"] == 0]
    assert first_column == ["RSF factor", "0%", "5%", "15%", "50%", "65%", "85%", "100%"]


def test_each_factor_keeps_its_own_assets(converted):
    rows = table_rows(converted["md"])
    assert "Level 2B" in rows["50%"]            # the row that a sideways page merges away
    assert "Level 2A" in rows["15%"] and "2B" not in rows["15%"]
    assert "Level 1 assets" in rows["5%"]
    assert "Coins and banknotes" in rows["0%"]
    assert re.search(r"residential mortgages", rows["65%"])
    assert "commodities" in rows["85%"]


def test_the_table_caption_is_kept(converted):
    assert "Figure 3: Summary of Asset Categories" in converted["md"]


# --------------------------------------------------------------- headings --

def test_the_section_heading_is_found(converted):
    headings = [t["text"] for t in converted["doc"]["texts"]
                if t["label"] == "section_header" and t.get("content_layer", "body") == "body"]
    assert headings == ["3. Project Finance Overview"]
    assert "## 3. Project Finance Overview" in converted["md"]


def test_the_running_header_and_footer_stay_out_of_the_text(converted):
    # docling labels these as page furniture; the markdown export drops them.
    labels = {t["label"] for t in converted["doc"]["texts"]}
    assert {"page_header", "page_footer"} <= labels
    assert "Yale SCHOOL OF MANAGEMENT" not in converted["md"]


# ------------------------------------------- why the rotation step exists --

def test_without_the_rotation_fix_the_table_is_mangled(cfg):
    with httpx.Client() as client:
        md, doc = ingest.convert_ocr(client, cfg, FIXTURE)
    data = doc["tables"][0]["data"]
    assert (data["num_rows"], data["num_cols"]) != (8, 2)
    rows = table_rows(md)
    assert "50%" not in rows  # the factors run together instead of one row each
