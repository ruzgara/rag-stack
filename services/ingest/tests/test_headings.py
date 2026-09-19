import copy

import pypdfium2 as pdfium
from conftest import BODY, H, headings_of, levels_of
from docling_core.types.doc import DoclingDocument

import headings
from headings import assign_levels


# ------------------------------------------------------------------- style --

def test_style_ranks_chapter_caps_bold_italic(make_doc):
    pdf, doc = make_doc([
        H("Chapter 1 Introduction", size=20),
        BODY(),
        H("OVERVIEW OF BONDS", font="Times-Bold"),
        BODY(),
        H("The parties to a bond issue"),
        H("Investors", font="Times-Italic"),
        BODY(),
        H("Issuers", font="Times-Italic"),
        BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["source"] == "style"
    assert levels_of(doc) == {
        "Chapter 1 Introduction": 1,
        "OVERVIEW OF BONDS": 2,
        "The parties to a bond issue": 3,
        "Investors": 4,
        "Issuers": 4,
    }


def test_very_large_heading_ranks_first_without_chapters(make_doc):
    pdf, doc = make_doc([
        H("Sources of Finance", size=24),
        BODY(),
        H("EQUITY", font="Times-Bold"),
        BODY(),
        H("Relevant documentation"),
        BODY(),
        H("Shareholder loans", font="Times-Italic"),
        BODY(),
        H("Pension funds", font="Times-Italic"),
        BODY(),
    ])
    assert assign_levels(pdf, doc)["source"] == "style"
    assert levels_of(doc) == {
        "Sources of Finance": 1,
        "EQUITY": 2,
        "Relevant documentation": 3,
        "Shareholder loans": 4,
        "Pension funds": 4,
    }


def test_upright_ranks_above_italic_when_otherwise_alike(make_doc):
    pdf, doc = make_doc([
        H("Undertakings", font="Times-Roman"), BODY(),
        H("Information undertakings", font="Times-Italic"), BODY(),
        H("Events of default", font="Times-Roman"), BODY(),
    ])
    assign_levels(pdf, doc)
    assert levels_of(doc) == {"Undertakings": 1, "Information undertakings": 2, "Events of default": 1}


def test_chapter_is_top_level_even_in_a_minor_style(make_doc):
    # Some books set "Chapter N" in the same face as their subsections.
    pdf, doc = make_doc([
        H("Chapter 4 Discount Rates", font="Times-Italic"),
        BODY(),
        H("SELECTING A DISCOUNT RATE", font="Times-Bold"),
        BODY(),
        H("Time frame of reference", font="Times-Italic"),
        BODY(),
    ])
    assign_levels(pdf, doc)
    levels = levels_of(doc)
    assert levels["Chapter 4 Discount Rates"] == 1
    assert levels["Chapter 4 Discount Rates"] < levels["SELECTING A DISCOUNT RATE"] < levels["Time frame of reference"]


def test_cover_title_sits_beside_chapters_not_above_them(make_doc):
    # A very large cover title ranked above chapters would prefix every
    # heading path in the book.
    # "Very large" is relative to the median heading, so the book needs a
    # realistic share of ordinary headings.
    pdf, doc = make_doc(
        [H("THE GUIDE TO PROJECT FINANCE", size=26), BODY()],
        [H("Chapter 1 Introduction", size=20), BODY(), H("RISKS", font="Times-Bold"), BODY(),
         H("Construction risk"), BODY(), H("Operating risk"), BODY(), H("Market risk"), BODY()],
    )
    assign_levels(pdf, doc)
    levels = levels_of(doc)
    assert levels["THE GUIDE TO PROJECT FINANCE"] == 1
    assert levels["Chapter 1 Introduction"] == 1
    assert levels["RISKS"] == 2


def test_one_style_stays_flat(make_doc):
    pdf, doc = make_doc([H("Alpha"), BODY(), H("Beta"), BODY(), H("Gamma"), BODY()])
    report = assign_levels(pdf, doc)
    assert report["source"] == "flat"
    assert set(levels_of(doc).values()) == {1}


def test_too_many_styles_stays_flat(make_doc, monkeypatch):
    monkeypatch.setattr(headings, "MAX_STYLES", 2)
    pdf, doc = make_doc([
        H("PART ONE", font="Times-Bold"), BODY(),
        H("A section"), BODY(),
        H("A subsection", font="Times-Italic"), BODY(),
    ])
    assert assign_levels(pdf, doc)["source"] == "flat"
    assert set(levels_of(doc).values()) == {1}


def test_no_headings(make_doc):
    pdf, doc = make_doc([BODY(), BODY()])
    before = copy.deepcopy(doc)
    assert assign_levels(pdf, doc)["source"] == "none"
    assert doc == before


# --------------------------------------------------------------- numbering --

def test_numbering_sets_depth_and_places_unnumbered_headings(make_doc):
    pdf, doc = make_doc([
        H("Preface"), BODY(),
        H("1 Introduction"), BODY(),
        H("1.1 Scope"), BODY(),
        H("1.1.1 Exclusions"), BODY(),
        H("Notes"), BODY(),
        H("2 Methods"), BODY(),
        H("Chapter 3 Results"), BODY(),
        H("3.1 Findings"), BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["source"] == "numbering"
    assert levels_of(doc) == {
        "Preface": 1,           # before any number: top level
        "1 Introduction": 1,
        "1.1 Scope": 2,
        "1.1.1 Exclusions": 3,
        "Notes": 4,             # unnumbered inside 1.1.1: below it
        "2 Methods": 1,
        "Chapter 3 Results": 1,
        "3.1 Findings": 2,
    }
    assert report["renders"] == 0  # numbering needs no style measurement


def test_numbering_needs_most_headings_numbered(make_doc):
    pdf, doc = make_doc([
        H("1 Introduction"), BODY(), H("Background"), BODY(), H("Scope"), BODY(),
        H("Method"), BODY(), H("Results"), BODY(), H("2 Outlook"), BODY(),
    ])
    assert assign_levels(pdf, doc)["source"] != "numbering"


# --------------------------------------------------------------- bookmarks --

class FakeDest:
    def __init__(self, index):
        self.index = index

    def get_index(self):
        return self.index


class FakeBookmark:
    def __init__(self, level, title, page_index):
        self.level, self.title, self.page_index = level, title, page_index

    def get_title(self):
        return self.title

    def get_dest(self):
        return FakeDest(self.page_index)


def test_bookmarks_give_levels_and_unmatched_headings_go_below(make_doc, monkeypatch):
    pdf, doc = make_doc([
        H("Introduction"), BODY(),
        H("Background and context"), BODY(),
        H("An unlisted detail", font="Times-Italic"), BODY(),
    ])
    toc = [FakeBookmark(0, "Introduction", 0), FakeBookmark(1, "Background and context", 0),
           FakeBookmark(0, "Cover", 0)]
    monkeypatch.setattr(pdfium.PdfDocument, "get_toc", lambda self, **kw: iter(toc))
    report = assign_levels(pdf, doc)
    assert report["source"] == "bookmarks"
    assert levels_of(doc) == {"Introduction": 1, "Background and context": 2, "An unlisted detail": 3}


def test_bookmarks_on_other_pages_dont_match(make_doc, monkeypatch):
    pdf, doc = make_doc([H("1 Introduction"), BODY(), H("1.1 Scope"), BODY(), H("1.2 Aims"), BODY()])
    toc = [FakeBookmark(0, "1 Introduction", 40)]  # far from page 1
    monkeypatch.setattr(pdfium.PdfDocument, "get_toc", lambda self, **kw: iter(toc))
    assert assign_levels(pdf, doc)["source"] == "numbering"


# ----------------------------------------------------------------- cleanup --

def test_running_headers_in_the_margin_become_furniture(make_doc):
    page = lambda n: [H("A Practical Guide to PPP", font="Times-Italic", size=9, y=810),
                      H(f"{n} Section {n}", y=700), BODY()]
    pdf, doc = make_doc(page(1), page(2), page(3))
    report = assign_levels(pdf, doc)
    assert report["furniture"] == 3
    running = [t for t in doc["texts"] if t["text"] == "A Practical Guide to PPP"]
    assert len(running) == 3 and all(t["content_layer"] == "furniture" for t in running)
    assert levels_of(doc) == {"1 Section 1": 1, "2 Section 2": 1, "3 Section 3": 1}


def test_repeated_headings_in_the_page_body_stay(make_doc):
    page = lambda n: [H(f"{n} Country {n}", y=700), BODY(), H("Investment insurance", y=500), BODY()]
    pdf, doc = make_doc(page(1), page(2), page(3))
    assert assign_levels(pdf, doc)["furniture"] == 0
    kept = [t for t in doc["texts"] if t["text"] == "Investment insurance"]
    assert all(t.get("content_layer", "body") == "body" and t["label"] == "section_header" for t in kept)


def test_captions_and_sentences_become_text(make_doc):
    sentence = "Will the financiers require that the currency and interest rate exposure is completely or partially hedged"
    pdf, doc = make_doc([
        H("1 Introduction"), BODY(),
        H("Table 4-A"), BODY(),
        H("Exhibit 6.1 BUYER CREDIT"), BODY(),
        H(sentence, size=8), BODY(),
        H("Table of contents"), BODY(),
        H("1.1 Scope"), BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["to_text"] == 3
    by_text = {t["text"]: t for t in doc["texts"]}
    for text in ("Table 4-A", "Exhibit 6.1 BUYER CREDIT", sentence):
        assert by_text[text]["label"] == "text"
        assert "level" not in by_text[text]
    assert "Table of contents" in headings_of(doc)  # "Table" alone isn't a caption


def test_heading_split_over_two_lines_is_joined(make_doc):
    pdf, doc = make_doc([
        H("Determination of a", size=14),
        H("Discount Rate", size=14, step=14 * 1.25),
        BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["merged"] == 1
    assert list(headings_of(doc)) == ["Determination of a Discount Rate"]
    assert [t for t in doc["texts"] if t["text"] == "Discount Rate"][0]["content_layer"] == "furniture"


def test_chapter_label_is_joined_with_its_title(make_doc):
    pdf, doc = make_doc([
        H("Chapter 5", size=18),
        H("Comparing Upfront", font="Times-Bold", size=14, step=18 * 1.6),
        H("and Annual Costs", font="Times-Bold", size=14, step=14 * 1.25),
        BODY(),
        H("THE PRESENT VALUE METHOD", font="Times-Bold"),
        BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["merged"] == 2
    assert levels_of(doc)["Chapter 5 Comparing Upfront and Annual Costs"] == 1


def test_stacked_headings_in_different_styles_stay_apart(make_doc):
    # A parent with no text of its own, straight followed by its first child.
    pdf, doc = make_doc([
        H("The parties to a bond issue"),
        H("Investors", font="Times-Italic", step=11 * 1.4),
        BODY(),
        H("Issuers", font="Times-Italic"),
        BODY(),
    ])
    report = assign_levels(pdf, doc)
    assert report["merged"] == 0
    levels = levels_of(doc)
    assert levels["The parties to a bond issue"] < levels["Investors"] == levels["Issuers"]


# ------------------------------------------------------------------ output --

def test_output_is_a_valid_docling_document(make_doc):
    pdf, doc = make_doc([
        H("A Practical Guide", size=9, y=810), H("Chapter 1 Introduction", size=20, y=700), BODY(),
        H("Table 1-A"), BODY(),
        H("Determination of a", size=14), H("Discount Rate", size=14, step=14 * 1.25), BODY(),
    ], [H("A Practical Guide", size=9, y=810), BODY()], [H("A Practical Guide", size=9, y=810), BODY()])
    assign_levels(pdf, doc)
    loaded = DoclingDocument.model_validate(doc)
    md = loaded.export_to_markdown()
    assert "## Chapter 1 Introduction" in md
    assert "A Practical Guide" not in md  # furniture isn't exported
    assert "Table 1-A" in md and "## Table 1-A" not in md


def test_running_twice_changes_nothing(make_doc):
    pdf, doc = make_doc([
        H("Chapter 1 Introduction", size=20), BODY(),
        H("OVERVIEW", font="Times-Bold"), BODY(),
        H("Determination of a", size=14), H("Discount Rate", size=14, step=14 * 1.25), BODY(),
        H("Investors", font="Times-Italic"), BODY(),
        H("Table 2-B"), BODY(),
    ])
    assign_levels(pdf, doc)
    once = copy.deepcopy(doc)
    assign_levels(pdf, doc)
    assert doc == once


def test_describe_summarises_the_report():
    text = headings.describe({"source": "style", "levels": {1: 2, 2: 5}, "furniture": 3, "to_text": 0,
                              "merged": 1, "renders": 4, "seconds": 0.2})
    assert text == "headings from style: L1:2 L2:5 (3 furniture, 1 merged), 4 pages rendered, 0.2s"
