import json

import httpx
import pytest
from conftest import SEARCH_ENV

import library
from library import Library, outline
from retrieval import Retriever, SearchConfig

GUIDE = "a" * 64
BASEL = "b" * 64
GONE = "c" * 64

GUIDE_MD = """## PROJECT FINANCE

Front matter.

## Chapter 7 Finance Documents

### LOANS

Text about loans.

#### Conditions precedent

More text.

## Chapter 7 Finance Documents

Repeated heading, listed once.
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A Library over a temp data dir and a faked Qdrant facet."""
    monkeypatch.setattr(library, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(library, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(library, "MARKDOWN", tmp_path / "markdown")
    (tmp_path / "markdown").mkdir()
    (tmp_path / "markdown" / f"{GUIDE}.md").write_text(GUIDE_MD)
    (tmp_path / "markdown" / f"{BASEL}.md").write_text("# Basel III\n\n# 1. Introduction\n\nText.\n")
    rows = [
        {"sha256": GUIDE, "filename": "Guide to Financing BOT projects.pdf", "status": "processed", "pages": 150,
         "markdown_path": f"markdown/{GUIDE}.md"},
        {"sha256": BASEL, "filename": "Basel III G.pdf", "status": "processed", "pages": 9,
         "markdown_path": f"markdown/{BASEL}.md"},
        {"sha256": GONE, "filename": "Failed.pdf", "status": "failed"},
    ]
    (tmp_path / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def make(facets: list[dict] | None = None, fail: bool = False) -> Library:
        def handler(request: httpx.Request) -> httpx.Response:
            if fail:
                return httpx.Response(500, text="qdrant down")
            hits = facets if facets is not None else [{"value": GUIDE, "count": 352}, {"value": BASEL, "count": 18}]
            return httpx.Response(200, json={"result": {"hits": hits}})

        retriever = Retriever(SearchConfig.from_env(SEARCH_ENV), client=httpx.Client(transport=httpx.MockTransport(handler)))
        return Library(retriever)

    return make


# ----------------------------------------------------------------- outline --

def test_outline_takes_the_top_heading_level(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text(GUIDE_MD)
    assert outline(path, limit=10) == ["PROJECT FINANCE", "Chapter 7 Finance Documents"]


def test_outline_of_a_document_without_headings_or_markdown(tmp_path):
    (tmp_path / "flat.md").write_text("Just text.\n")
    assert outline(tmp_path / "flat.md", 10) == []
    assert outline(tmp_path / "missing.md", 10) == []


def test_outline_is_capped(tmp_path):
    path = tmp_path / "many.md"
    path.write_text("\n".join(f"## Section {i}" for i in range(50)))
    assert len(outline(path, limit=10)) == 10


# --------------------------------------------------------------- documents --

def test_documents_come_from_the_index_biggest_first(lib):
    docs = lib().documents()
    assert [(d.filename, d.pages, d.chunks) for d in docs] == [
        ("Guide to Financing BOT projects.pdf", 150, 352), ("Basel III G.pdf", 9, 18)]
    assert docs[0].sections == ["PROJECT FINANCE", "Chapter 7 Finance Documents"]


def test_documents_no_longer_in_the_index_are_left_out(lib):
    # Qdrant's facet can still list a document whose points were replaced.
    docs = lib([{"value": GUIDE, "count": 352}, {"value": GONE, "count": 0}]).documents()
    assert [d.sha256 for d in docs] == [GUIDE]


def test_a_document_missing_from_the_manifest_still_appears(lib):
    docs = lib([{"value": "d" * 64, "count": 5}]).documents()
    assert (docs[0].filename, docs[0].pages, docs[0].chunks) == ("dddddddddddd.pdf", 0, 5)


# ---------------------------------------------------------------- describe --

def test_describe_lists_every_document_with_its_sections(lib):
    text = lib().describe()
    assert text.startswith("The knowledge base holds 2 document(s), 370 passages in total.")
    assert "- Guide to Financing BOT projects — 150 pages, 352 passages" in text
    assert "Main sections: PROJECT FINANCE; Chapter 7 Finance Documents" in text
    assert "nothing here can be cited" in text


def test_describe_one_document_by_part_of_its_name(lib):
    text = lib().describe("basel")
    assert text.startswith("Basel III G — 9 pages, 18 passages indexed")
    assert "Sections: Basel III; 1. Introduction" in text
    assert "Guide to Financing" not in text


def test_describe_says_when_nothing_matches(lib):
    assert "No document in the knowledge base matches 'tax law'" in lib().describe("tax law")


def test_describe_an_empty_knowledge_base(lib):
    assert lib([]).describe() == "The knowledge base is empty: no documents have been indexed yet."


def test_describe_survives_qdrant_being_down(lib):
    assert "empty" in lib(fail=True).describe()


def test_long_filenames_are_shortened(lib):
    long_name = "A " + "very " * 40 + "long name.pdf"
    docs = lib([{"value": "e" * 64, "count": 1}])
    docs.retriever.cfg  # noqa: B018 - keep the fixture's retriever alive
    doc = library.Document(sha256="e" * 64, filename=long_name)
    assert len(doc.short_name) == library.MAX_FILENAME + 1 and doc.short_name.endswith("…")
