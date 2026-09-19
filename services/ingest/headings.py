"""
Heading levels for docling output.

docling gives every PDF section header level 1, so a chunk's heading path is
just its nearest heading and a heading with no text of its own ("The parties
to a typical bond issue", followed straight away by "Investors") is lost to
the chunker. This module rewrites the levels in the DoclingDocument JSON, from
the best evidence the PDF has:

  1. Bookmarks (the PDF outline), when they match the headings.
  2. Numbering ("7", "7.3", "7.3.1"), when most headings are numbered.
  3. Style, measured from the rendered page: letter height, stroke width
     (bold) and slant (italic), plus ALL CAPS from the text. Scans carry no
     font information, so pixels are the only style evidence that works for
     both scanned and born-digital pages. Styles rank as: "Chapter N" > very large >
     caps > bold > upright > italic.

Before that, it cleans up what docling mislabels as headings: running page
headers and logos repeated in the page margin (moved to the furniture layer,
which the chunker skips), figure/table captions and sentence-length
"headings" (relabelled as text), and headings split over two lines (merged).

If the styles don't separate into a few clear groups, the document keeps its
flat levels. No model calls; the cost is rendering the pages that have
headings, a fraction of a second each.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium

# Rendering scale for style measurement (4 = 288 dpi); stroke widths of body
# and bold text differ by well under a point, so lower scales blur them.
RENDER_SCALE = 4.0
# Ink threshold on the grayscale render.
INK = 128
# A heading counts as italic below this slant (x shift per unit of height).
# Measured: upright -0.05..+0.05, italic -0.20..-0.25.
ITALIC_SLANT = -0.12
# Bold: stroke width above this multiple of the body text's.
BOLD_STROKE = 1.2
# "Very large" (chapter titles): letter height above this multiple of the median heading's.
HUGE_SIZE = 1.8
# More distinct styles than this means the measurements are noise; stay flat.
# (The Guide to Financing BOT, a well-structured book, has 7.)
MAX_STYLES = 8
# Running headers: the same text on at least this many pages, always within
# this share of the page height from the top or bottom edge.
RUNNING_MIN_PAGES = 3
RUNNING_MARGIN = 0.12
# Longer "headings" are sentences docling mislabelled.
MAX_HEADING_WORDS = 14
# Use bookmarks when at least this share of headings match one.
BOOKMARK_MIN_MATCH = 0.3
# Use numbering when at least this share of headings are numbered.
NUMBERED_MIN_SHARE = 0.4

NUMBERING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,5})\.?(?:\s+|$)")
CHAPTER_RE = re.compile(r"^\s*(?i:chapter|part|appendix|annex)\s+(\d+|[IVXLC]+|[A-Z])\b")
# A heading that is only a chapter label or number ("Chapter 4", "3"): the
# title that follows it on the page belongs to it.
BARE_LABEL_RE = re.compile(r"^\s*((?i:chapter|part|appendix|annex)\s+(\d+|[IVXLC]+|[A-Z])|\d{1,2})\.?\s*$")
CAPTION_RE = re.compile(r"^\s*(?i:table|figure|fig\.|exhibit|chart|panel|graph|box)\s*(\d|[A-Z]\b|[IVX]+\b)")


@dataclass
class Heading:
    item: dict       # the DoclingDocument text item (mutated in place)
    index: int       # position in doc["texts"]
    page: int        # 1-based
    box: tuple[float, float, float, float]  # left, top, right, bottom; points from the page's top-left
    size: float | None = None     # letter height of the first line, points
    stroke: float | None = None   # mean horizontal stroke width, points
    slant: float | None = None

    @property
    def text(self) -> str:
        return self.item.get("text", "")

    @property
    def caps(self) -> bool:
        return self.text.isupper()

    @property
    def measured(self) -> bool:
        return self.size is not None


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class PageStyles:
    """Renders pages on demand (one at a time) and measures text boxes on them."""

    def __init__(self, pdf: pdfium.PdfDocument, lock):
        self.pdf, self.lock = pdf, lock
        self._page_no: int | None = None
        self._img: np.ndarray | None = None
        self.renders = 0

    def _image(self, page_no: int) -> np.ndarray:
        if page_no != self._page_no:
            with self.lock:
                bitmap = self.pdf[page_no - 1].render(scale=RENDER_SCALE, grayscale=True)
                img = bitmap.to_numpy()
            self._img = img[..., 0] if img.ndim == 3 else img
            self._page_no = page_no
            self.renders += 1
        return self._img

    def measure(self, page_no: int, box: tuple[float, float, float, float]) -> tuple[float, float, float] | None:
        """(letter height, stroke width, slant) of the first text line in box."""
        img = self._image(page_no)
        s = RENDER_SCALE
        l, t, r, b = box
        crop = img[max(0, int(t * s)):int(b * s) + 1, max(0, int(l * s)):int(r * s) + 1] < INK
        rows = np.flatnonzero(crop.any(axis=1))
        if rows.size == 0:
            return None
        crop = crop[rows[0]:rows[-1] + 1]
        gaps = np.flatnonzero(~crop.any(axis=1))
        line = crop[:gaps[0]] if gaps.size else crop
        h = line.shape[0]
        # Stroke width: mean length of horizontal ink runs.
        padded = np.pad(line.astype(np.int8), ((0, 0), (1, 1)))
        d = np.diff(padded, axis=1)
        runs = np.flatnonzero(d.ravel() == -1) - np.flatnonzero(d.ravel() == 1)
        if runs.size == 0:
            return None
        # Slant: the shear that makes vertical strokes line up best, i.e. that
        # maximises the sharpness of the column profile.
        ys, xs = np.nonzero(line)
        best_a, best = 0.0, -1.0
        for a in np.linspace(-0.4, 0.4, 33):
            x2 = np.round(xs - a * (ys - h / 2)).astype(int)
            score = float((np.bincount(x2 - x2.min()).astype(np.float64) ** 2).sum())
            if score > best:
                best_a, best = float(a), score
        return h / s, float(runs.mean()) / s, best_a


def _box(prov: dict, page_height: float) -> tuple[float, float, float, float]:
    b = prov["bbox"]
    if b.get("coord_origin", "BOTTOMLEFT") == "BOTTOMLEFT":
        return b["l"], page_height - b["t"], b["r"], page_height - b["b"]
    return b["l"], b["t"], b["r"], b["b"]


def _to_text(item: dict) -> None:
    item["label"] = "text"
    item.pop("level", None)


def _to_furniture(item: dict) -> None:
    item["content_layer"] = "furniture"


# Style key: (very large, ALL CAPS, bold, upright); sorts most prominent first.
CHAPTER_KEY = (True, True, True, True)


def _style_key(h: Heading, median_size: float, body_stroke: float | None) -> tuple[bool, bool, bool, bool]:
    if CHAPTER_RE.match(h.text):
        # "Chapter 4 ..." is top level whatever it looks like; books often
        # set chapter titles in the same face as minor headings.
        return CHAPTER_KEY
    bold = body_stroke is not None and h.stroke > body_stroke * BOLD_STROKE
    return (h.size >= HUGE_SIZE * median_size, h.caps, bold and not h.caps, h.slant > ITALIC_SLANT)


def assign_levels(pdf_path: Path, doc: dict, lock=None) -> dict:
    """Rewrite section-header levels in doc (a DoclingDocument as a dict) in
    place. Returns a report: {"source", "levels", "furniture", "to_text",
    "merged", "renders", "seconds"}."""
    t0 = time.monotonic()
    lock = lock or nullcontext()
    report = {"source": "none", "levels": {}, "furniture": 0, "to_text": 0, "merged": 0, "renders": 0}
    texts = doc.get("texts", [])

    with lock:
        pdf = pdfium.PdfDocument(pdf_path)
        page_heights = {i + 1: pdf[i].get_height() for i in range(len(pdf))}
        toc = []
        for bm in pdf.get_toc():
            dest = bm.get_dest()
            index = dest.get_index() if dest else None
            if index is not None and bm.get_title().strip():
                toc.append((bm.level + 1, _norm(bm.get_title()), index + 1))
    try:
        styles = PageStyles(pdf, lock)
        heads: list[Heading] = []
        for i, item in enumerate(texts):
            if item.get("label") != "section_header" or item.get("content_layer", "body") != "body":
                continue
            prov = (item.get("prov") or [None])[0]
            if not prov or prov["page_no"] not in page_heights:
                continue
            heads.append(Heading(item, i, prov["page_no"], _box(prov, page_heights[prov["page_no"]])))
        if not heads:
            return report

        # --- cleanup: running headers/logos, captions, sentences -----------
        by_text: dict[str, list[Heading]] = {}
        for h in heads:
            by_text.setdefault(_norm(h.text), []).append(h)
        for group in by_text.values():
            pages = {h.page for h in group}
            if len(pages) >= RUNNING_MIN_PAGES and all(
                    h.box[3] < RUNNING_MARGIN * page_heights[h.page]
                    or h.box[1] > (1 - RUNNING_MARGIN) * page_heights[h.page] for h in group):
                for h in group:
                    _to_furniture(h.item)
                report["furniture"] += len(group)
        kept = []
        for h in heads:
            if h.item.get("content_layer") == "furniture":
                continue
            if CAPTION_RE.match(h.text) or len(h.text.split()) > MAX_HEADING_WORDS:
                _to_text(h.item)
                report["to_text"] += 1
                continue
            kept.append(h)
        heads = kept

        # Style measurement: pages in order, so each is rendered once.
        def measure(subset: list[Heading]) -> None:
            for h in sorted(subset, key=lambda x: x.page):
                if not h.measured and (m := styles.measure(h.page, h.box)):
                    h.size, h.stroke, h.slant = m

        # --- merge headings split over several items ------------------------
        # Consecutive in reading order, same page, stacked with a small gap:
        # a bare label and its title ("Chapter 4" + "Determination of a"), and
        # lines of one heading in the same style ("Determination of a" +
        # "Discount Rate").
        def adjacent(a: Heading, b: Heading, max_gap: float) -> bool:
            return (b.index == a.index + 1 and a.page == b.page
                    and -0.5 <= (b.box[1] - a.box[3]) / (a.box[3] - a.box[1]) < max_gap)

        def same_style(a: Heading, b: Heading) -> bool:
            return (a.measured and b.measured and a.caps == b.caps
                    and (a.slant > ITALIC_SLANT) == (b.slant > ITALIC_SLANT)
                    and abs(a.size - b.size) <= 0.25 * max(a.size, b.size)
                    and abs(a.stroke - b.stroke) <= 0.25 * max(a.stroke, b.stroke))

        measure([h for a, b in zip(heads, heads[1:]) if adjacent(a, b, 1.0) for h in (a, b)])
        kept = []
        for prev, h in zip([None, *heads], heads):
            target = kept[-1] if kept else None
            label = prev is target and prev is not None and BARE_LABEL_RE.match(prev.text) and adjacent(prev, h, 3.0)
            line = (prev is not None and adjacent(prev, h, 1.0) and not NUMBERING_RE.match(h.text)
                    and not CHAPTER_RE.match(h.text) and not BARE_LABEL_RE.match(prev.text) and same_style(prev, h))
            if target is not None and (label or line):
                target.item["text"] = f"{target.text} {h.text}"
                if "orig" in target.item:
                    target.item["orig"] = f"{target.item['orig']} {h.item.get('orig', h.text)}"
                _to_furniture(h.item)
                report["merged"] += 1
            else:
                kept.append(h)
        heads = kept

        # --- body text stroke width, the reference for "bold" ---------------
        def body_stroke(pages: set[int]) -> float | None:
            samples = []
            for item in texts:
                if len(samples) >= 20:
                    break
                prov = (item.get("prov") or [None])[0]
                if (item.get("label") == "text" and prov and prov["page_no"] in pages
                        and len(item.get("text", "")) > 150):
                    if m := styles.measure(prov["page_no"], _box(prov, page_heights[prov["page_no"]])):
                        samples.append(m[1])
            return float(np.median(samples)) if samples else None

        def style_levels(subset: list[Heading]) -> dict[int, int] | None:
            """Level (1-based) per heading index from style rank; None if the
            styles don't form a few clear groups."""
            measure(subset)
            subset = [h for h in subset if h.measured]
            if not subset:
                return None
            median = float(np.median([h.size for h in subset]))
            ref = body_stroke({h.page for h in subset})
            keys = {h.index: _style_key(h, median, ref) for h in subset}
            ranked = sorted(set(keys.values()), reverse=True)
            if len(ranked) > MAX_STYLES:
                return None
            if CHAPTER_KEY in ranked:
                # With chapters, other very large headings (cover, contents,
                # foreword, closing remarks) are top-level parts beside them.
                ranked = [CHAPTER_KEY] + [k for k in ranked if not k[0]]
                keys = {i: CHAPTER_KEY if k[0] else k for i, k in keys.items()}
            rank = {k: i + 1 for i, k in enumerate(ranked)}
            return {i: rank[k] for i, k in keys.items()}

        levels: dict[int, int] = {}

        # --- 1. bookmarks ----------------------------------------------------
        if toc:
            for h in heads:
                t = _norm(h.text)
                for lvl, title, page in toc:
                    if abs(page - h.page) <= 1 and t and (
                            t == title or (min(len(t), len(title)) >= 4 and (t.startswith(title) or title.startswith(t)))):
                        levels[h.index] = lvl
                        break
            if len(levels) >= BOOKMARK_MIN_MATCH * len(heads):
                report["source"] = "bookmarks"
                rest = [h for h in heads if h.index not in levels]
                deepest = max(levels.values())
                for i, lvl in (style_levels(rest) or {h.index: 1 for h in rest}).items():
                    levels[i] = deepest + lvl
            else:
                levels = {}

        # --- 2. numbering ----------------------------------------------------
        if not levels:
            numbered = {h.index: m.group(1).count(".") + 1 for h in heads if (m := NUMBERING_RE.match(h.text))}
            if len(numbered) >= 3 and len(numbered) >= NUMBERED_MIN_SHARE * len(heads):
                report["source"] = "numbering"
                current = 0
                for h in heads:
                    if h.index in numbered:
                        current = levels[h.index] = numbered[h.index]
                    elif CHAPTER_RE.match(h.text):
                        levels[h.index] = current = 1
                    else:
                        # Unnumbered headings inside a numbered section sit
                        # below it; before the first number they're top level.
                        levels[h.index] = current + 1

        # --- 3. style ----------------------------------------------------------
        if not levels:
            if (by_style := style_levels(heads)) and len(set(by_style.values())) > 1:
                report["source"] = "style"
                levels = by_style
            else:
                report["source"] = "flat"

        for h in heads:
            lvl = levels.get(h.index, 1)
            h.item["level"] = lvl
            report["levels"][lvl] = report["levels"].get(lvl, 0) + 1
        report["levels"] = dict(sorted(report["levels"].items()))
        report["renders"] = styles.renders
        return report
    finally:
        report["seconds"] = round(time.monotonic() - t0, 1)
        with lock:
            pdf.close()


def describe(report: dict) -> str:
    levels = " ".join(f"L{k}:{v}" for k, v in report["levels"].items()) or "no headings"
    extra = [f"{report[k]} {k.replace('_', ' ')}" for k in ("furniture", "to_text", "merged") if report[k]]
    return (f"headings from {report['source']}: {levels}"
            + (f" ({', '.join(extra)})" if extra else "")
            + f", {report['renders']} pages rendered, {report['seconds']}s")
