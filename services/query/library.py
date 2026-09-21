"""
What the knowledge base holds, for the chat model's list_documents tool.

Search answers "what do the documents say about X". This answers "which
documents are there at all", which no amount of searching can: a passage
about bond issues doesn't tell the model whether the library also holds a
book on water systems.

Documents come from data/manifest.jsonl (filename, pages), their passage
counts from Qdrant -- which, as everywhere else here, is the record of
what's actually indexed -- and their sections from the markdown ingest
wrote, whose headings carry the levels the heading step worked out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MANIFEST = DATA_DIR / "manifest.jsonl"
MARKDOWN = DATA_DIR / "markdown"

# Keep the tool's answer to a size a model can actually use.
MAX_DOCS = 40
MAX_SECTIONS = 10        # per document, when listing them all
MAX_SECTIONS_ONE = 60    # for a single document's outline
MAX_FILENAME = 90
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class Document:
    sha256: str
    filename: str
    pages: int = 0
    chunks: int = 0
    sections: list[str] = field(default_factory=list)

    @property
    def short_name(self) -> str:
        name = self.filename.removesuffix(".pdf")
        return name if len(name) <= MAX_FILENAME else f"{name[:MAX_FILENAME]}…"


def manifest_documents() -> dict[str, dict]:
    """Latest processed manifest row per sha256, leaving out documents
    deleted since (a "deleted" row -- see purge.py)."""
    latest: dict[str, dict] = {}
    if not MANIFEST.exists():
        return latest
    with MANIFEST.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "processed":
                latest[rec["sha256"]] = rec
            elif rec.get("status") == "deleted":
                latest.pop(rec["sha256"], None)
    return latest


def outline(markdown_path: Path, limit: int) -> list[str]:
    """A document's main sections: its shallowest heading level, in order."""
    try:
        text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return []
    headings = [(len(m.group(1)), m.group(2)) for line in text.splitlines() if (m := HEADING_RE.match(line))]
    if not headings:
        return []
    top = min(level for level, _ in headings)
    seen, out = set(), []
    for level, title in headings:
        if level == top and title not in seen:
            seen.add(title)
            out.append(title)
    return out[:limit]


class Library:
    """The indexed documents. Reads Qdrant through the retriever's client."""

    def __init__(self, retriever):
        self.retriever = retriever

    def chunk_counts(self, doc_shas: set[str] | None = None) -> dict[str, int]:
        """Passages per document, straight from the index. `doc_shas`
        restricts this to a library scope (see Retriever.search); an empty
        set short-circuits to nothing, since faceting with an impossible
        filter would still be a wasted round trip."""
        if doc_shas is not None and not doc_shas:
            return {}
        cfg = self.retriever.cfg
        body = {"key": "doc_sha", "limit": MAX_DOCS, "exact": True}
        if doc_shas is not None:
            body["filter"] = {"must": [{"key": "doc_sha", "match": {"any": sorted(doc_shas)}}]}
        try:
            result = self.retriever._qdrant("POST", f"/collections/{cfg.collection}/facet", body)
        except (RuntimeError, httpx.HTTPError):
            return {}
        # A document whose points were replaced can linger in the facet with
        # a count of 0; it isn't in the index any more.
        return {hit["value"]: hit["count"] for hit in result.get("hits", []) if hit["count"] > 0}

    def documents(self, sections: int = MAX_SECTIONS, doc_shas: set[str] | None = None) -> list[Document]:
        """Indexed documents, largest first. `doc_shas` is a library scope --
        see chunk_counts."""
        rows = manifest_documents()
        counts = self.chunk_counts(doc_shas)
        docs = []
        for sha, count in counts.items():
            rec = rows.get(sha, {})
            markdown = REPO_ROOT / rec["markdown_path"] if rec.get("markdown_path") else MARKDOWN / f"{sha}.md"
            docs.append(Document(sha256=sha, filename=rec.get("filename") or f"{sha[:12]}.pdf",
                                 pages=rec.get("pages") or 0, chunks=count,
                                 sections=outline(markdown, sections)))
        docs.sort(key=lambda d: -d.chunks)
        return docs[:MAX_DOCS]

    def describe(self, name: str | None = None, doc_shas: set[str] | None = None) -> str:
        """The document list as the model sees it; with `name`, one
        document's sections in full. `doc_shas` is a library scope -- see
        chunk_counts; without it, callers must not pass a non-admin user's
        request through unscoped, or they'd learn about documents outside
        every library they can read."""
        if name:
            wanted = name.strip().lower()
            matches = [d for d in self.documents(MAX_SECTIONS_ONE, doc_shas)
                       if wanted in d.filename.lower() or wanted in d.sha256]
            if not matches:
                return (f"No document in the knowledge base matches {name!r}. "
                        "Call list_documents without arguments to see what is there.")
            lines = []
            for doc in matches[:3]:
                lines.append(f"{doc.short_name} — {doc.pages} pages, {doc.chunks} passages indexed")
                lines.append("Sections: " + ("; ".join(doc.sections) if doc.sections else "(none detected)"))
            return "\n".join(lines)

        docs = self.documents(doc_shas=doc_shas)
        if not docs:
            return "The knowledge base is empty: no documents have been indexed yet."
        total = sum(d.chunks for d in docs)
        lines = [f"The knowledge base holds {len(docs)} document(s), {total} passages in total."]
        for doc in docs:
            lines.append(f"\n- {doc.short_name} — {doc.pages} pages, {doc.chunks} passages")
            if doc.sections:
                lines.append(f"  Main sections: {'; '.join(doc.sections)}")
        lines.append("\nTo read any of this, search for it with search_documents; "
                     "this list has no passage text and nothing here can be cited.")
        return "\n".join(lines)
