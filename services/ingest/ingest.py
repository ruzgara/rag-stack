#!/usr/bin/env python3
"""
Batch document ingestion for rag-stack.

Pipeline per file in data/inbox (up to DOCLING_LOC_NUM_WORKERS files at once):
  1. PDF triage, per page and without rendering: "digital" (visible text, not
     covered by an image), "figure" (visible text plus an image big enough to
     be a chart or a table picture, whose content is only in its pixels),
     "scan" (a page-sized image with no text layer of its own, or a few real
     lines stamped over it), "stale-ocr" (a page-sized image carrying someone
     else's invisible OCR text) or "empty".
  2. Any stale-ocr page routes the whole file to step 3 (force_ocr has to
     replace that text, and docling-serve takes one option set per file).
     Otherwise: docling-serve converts the embedded text, no OCR -- it is
     exactly what the typesetter wrote, which no OCR engine can match. Any
     scan pages (a cover, an inserted plate) only get their own images
     OCR'd; the good text elsewhere is kept. A digital page's sideways angle
     comes from its text matrices; a scan page's comes from tesseract OSD,
     run only on that page. If the embedded text fails the quality gate, the
     file drops to step 3.
  3. Force-OCR PDFs: OSD measures rotation on scan/stale-ocr pages (digital
     pages still use their text matrices), lossless rotation is applied, then
     docling-serve OCRs every page with ocrmac (Apple Vision), replacing any
     old OCR layer. Images go straight to ocrmac.
  4. Quality gate: too little text or too many junk characters -> VLM
     fallback (docling-serve vlm pipeline with the configured Ollama model).
  5. PDFs: heading levels (headings.py). docling marks every heading level 1;
     levels are rebuilt from bookmarks, numbering or measured heading styles,
     so chunks get their full heading path. Mislabelled running headers,
     captions and split headings are cleaned up. The markdown is re-exported
     from the corrected document.
  6. Write the result to data/markdown/<sha>.md (human-readable) and
     data/docling/<sha>.json (DoclingDocument, input for embed.py).
  7. Move the original file to data/processed or data/failed and record
     the outcome in data/manifest.jsonl.

--fix-headings reruns only step 5 on documents already processed, from their
stored docling JSON: no OCR, a few seconds per document.

Run with: uv run ingest.py [--force-vlm] [--file PATH] [--reprocess] [--fix-headings] [-v/--verbose]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from dotenv import dotenv_values

import headings

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
INBOX = DATA_DIR / "inbox"
WORK = DATA_DIR / "work"
PROCESSED = DATA_DIR / "processed"
FAILED = DATA_DIR / "failed"
MARKDOWN = DATA_DIR / "markdown"
DOCLING_JSON = DATA_DIR / "docling"
MANIFEST = DATA_DIR / "manifest.jsonl"

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES | {
    ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".asciidoc",
}

JUNK_CHAR_RE = re.compile(r"[�\x00-\x08\x0b\x0c\x0e-\x1f]")
# The same characters minus the replacement char, stripped from the output.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Document fields that hold text; everything else in the JSON is left alone.
TEXT_KEYS = {"text", "orig", "caption"}

# A page counts as scanned when images cover more than this share of it.
# Born-digital pages with figures sit well below; scans are ~1.0.
SCAN_IMAGE_COVER = 0.5
# A digital page counts as carrying a figure when images cover more than this
# share of it. A chart or a table pasted in as a picture runs ~0.2-0.4; a
# header logo or an inline icon is ~0.005, well under.
FIGURE_IMAGE_COVER = 0.05
# Page labels whose text is the typesetter's own, so their rotation is read
# from the text matrices rather than measured by OSD.
DIGITAL_LABELS = ("digital", "figure")
# Share of a page's text objects that must agree before it counts as sideways.
ROTATION_MIN_SHARE = 0.6
# Resolution pages are rendered at for orientation detection only.
OSD_DPI = 150
# Minimum tesseract orientation confidence to rotate a page. Sideways pages
# measured 3.4-10; near-blank/show-through pages produce spurious guesses <1.
OSD_MIN_CONFIDENCE = 2.0

# docling-serve is a separate process kept alive by launchd, so a crash (it
# segfaults if two conversions run at once -- see DOCLING_LOC_NUM_WORKERS in
# .env) or a `ragctl reload` lands here as a dropped connection, and the
# server is usually back within ~15s. Without these retries the whole batch
# dies with it: every remaining file fails against a closed port in
# milliseconds, so one crash costs the entire run.
DOCLING_ATTEMPTS = 3
# Long enough to cover launchd's ThrottleInterval (10s) plus docling's model
# loading, with room for a machine under load.
DOCLING_RESTART_WAIT = 180.0
# At startup we only need to cover a server still booting -- the ingest
# LaunchAgent fires on files landing in data/inbox, which can happen while
# docling is restarting. A shorter wait so a genuinely dead server still
# fails the run promptly.
DOCLING_START_WAIT = 60.0
DOCLING_HEALTH_POLL = 2.0
# Errors meaning "the server went away", as opposed to "the server is slow".
# Read/write timeouts are deliberately excluded: the conversion may still be
# running server-side, and retrying would queue the same work twice.
DOCLING_GONE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)

VERBOSE = False

# pdfium is not thread-safe, and two files are processed concurrently.
PDFIUM_LOCK = threading.Lock()
MANIFEST_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
_log_ctx = threading.local()


def log(msg: str) -> None:
    """Print a line, prefixed with the current file's [i/N] tag when set."""
    prefix = getattr(_log_ctx, "prefix", "")
    with PRINT_LOCK:
        print(f"{prefix}{msg}", flush=True)


def vprint(msg: str) -> None:
    if VERBOSE:
        log(msg)


def load_env() -> dict:
    env = {**dotenv_values(REPO_ROOT / ".env"), **os.environ}
    return env


@dataclass
class Config:
    docling_url: str
    ollama_url: str
    vlm_model: str
    ocr_lang: list[str]
    min_chars_per_page: int
    workers: int
    heading_levels: bool = True
    force_vlm: bool = False
    reprocess: bool = False

    @classmethod
    def from_env(cls, env: dict, force_vlm: bool, reprocess: bool = False) -> "Config":
        return cls(
            docling_url=f"http://127.0.0.1:{env.get('DOCLING_PORT', '5001')}",
            ollama_url=f"http://127.0.0.1:{env.get('OLLAMA_PORT', '11434')}",
            vlm_model=env.get("VLM_MODEL", ""),
            ocr_lang=(env.get("OCR_LANG") or "en-US").replace(",", " ").split(),
            min_chars_per_page=int(env.get("MIN_CHARS_PER_PAGE", "200")),
            # docling-serve runs this many conversions in parallel; matching it
            # keeps both slots busy without queueing work behind them.
            workers=max(1, int(env.get("DOCLING_LOC_NUM_WORKERS", "2"))),
            heading_levels=(env.get("INGEST_HEADING_LEVELS") or "true").strip().lower() in ("1", "true", "yes", "on"),
            force_vlm=force_vlm,
            reprocess=reprocess,
        )


@dataclass
class Result:
    filename: str
    sha256: str
    route: str = "none"
    pages: int = 0
    chars: int = 0
    status: str = "failed"
    error: str | None = None
    markdown_path: str | None = None
    docling_json: str | None = None
    timestamp: float = field(default_factory=time.time)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def already_processed(sha: str) -> bool:
    """Whether the latest processed-or-deleted row for this document says
    processed. A "deleted" row (written when its uploader deletes it, by the
    query service's purge.py) cancels the ones before it, so the same file
    uploaded again is converted again rather than skipped -- and removed."""
    if not MANIFEST.exists():
        return False
    processed = False
    with MANIFEST.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("sha256") == sha and rec.get("status") in ("processed", "deleted"):
                processed = rec["status"] == "processed"
    return processed


def append_manifest(result: Result) -> None:
    with MANIFEST_LOCK:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("a") as f:
            f.write(json.dumps(result.__dict__) + "\n")


# -------------------------------------------------------------------- triage --

def page_needs_ocr(page) -> str:
    """'digital', 'figure', 'scan', 'stale-ocr' or 'empty', from the page's
    objects alone. The distinction that matters isn't how much of the page is
    a scan, it's whether an old OCR text layer needs overwriting: that's the
    only case that requires force_ocr, since it's the only case where docling
    would otherwise trust text it shouldn't. 'figure' is a second, weaker
    signal: the page's own text is fine, but a chart or a table pasted in as
    a picture carries content no embedded text holds, so the image wants
    reading even though the page around it doesn't."""
    width, height = page.get_size()
    image_area = 0.0
    visible = invisible = 0
    for obj in page.get_objects(max_depth=3):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            left, bottom, right, top = obj.get_bounds()
            image_area += max(0.0, min(right, width) - max(left, 0.0)) * max(0.0, min(top, height) - max(bottom, 0.0))
        elif obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            if pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE:
                invisible += 1
            else:
                visible += 1
    if image_area / (width * height) > SCAN_IMAGE_COVER:
        # A page-sized image with an invisible text layer is someone else's
        # OCR result sitting under the scan; visible text here is a stamp,
        # not a substitute for reading the image.
        return "stale-ocr" if invisible else "scan"
    if visible:
        return "figure" if image_area / (width * height) > FIGURE_IMAGE_COVER else "digital"
    return "stale-ocr" if invisible else "empty"


def triage_pdf(path: Path) -> tuple[int, dict[str, int], list[str]]:
    """Page count, how many pages are digital / figure / scan / stale-ocr /
    empty, and the per-page labels in page order (the rotation step needs to
    know which pages are which)."""
    counts = {"digital": 0, "figure": 0, "scan": 0, "stale-ocr": 0, "empty": 0}
    labels: list[str] = []
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            for i in range(len(pdf)):
                label = page_needs_ocr(pdf[i])
                counts[label] += 1
                labels.append(label)
            return len(pdf), counts, labels
        finally:
            pdf.close()


# ------------------------------------------------------------------ rotation --

def detect_orientation(png: Path) -> tuple[int, float]:
    """Tesseract OSD: degrees to rotate clockwise to make the page upright."""
    proc = subprocess.run(["tesseract", str(png), "-", "--psm", "0"], capture_output=True, text=True)
    rotate = re.search(r"Rotate: (\d+)", proc.stdout)
    conf = re.search(r"Orientation confidence: ([\d.]+)", proc.stdout)
    return (int(rotate.group(1)) if rotate else 0, float(conf.group(1)) if conf else 0.0)


def fix_rotation(path: Path, sha: str, labels: list[str], workers: int) -> tuple[Path, list[int]]:
    """Mark sideways or upside-down pages rotated, measuring each page the
    cheap way when it's available. A "digital" or "figure" page states its
    angle exactly in its text objects' matrices (free, no rendering). A "scan"
    or "stale-ocr" page has no reliable text of its own, so it's measured by
    tesseract OSD on a rendered page instead -- run only on those pages, so a
    129-page book with one scanned cover renders 1 page, not 129, while a
    150-page scan with no text layer still gets OSD everywhere. docling never
    rotates pages itself, and OCRs a sideways table into a transposed one.
    Only the page's /Rotate flag changes -- nothing is rasterized. Returns the
    path to convert (a copy in data/work if any page was rotated) and the
    rotated page numbers."""
    osd_indices = [i for i, label in enumerate(labels) if label in ("scan", "stale-ocr")]

    to_rotate: dict[int, int] = {i: a for i, a in text_rotations(path).items() if labels[i] in DIGITAL_LABELS}
    if digital := sum(1 for label in labels if label in DIGITAL_LABELS):
        vprint(f"  rotation: {digital} digital page(s) measured from text matrices, "
               f"sideways: {[i + 1 for i in to_rotate] or 'none'}")

    if osd_indices:
        if not shutil.which("tesseract"):
            log("  ! tesseract not found (brew install tesseract); skipping OSD for scanned pages")
        else:
            t0 = time.monotonic()
            with tempfile.TemporaryDirectory(dir=WORK, prefix=f"{sha[:12]}-osd-") as tmp:
                pngs = []
                with PDFIUM_LOCK:
                    pdf = pdfium.PdfDocument(path)
                try:
                    for i in osd_indices:
                        # Lock per page, not per document, so a long book
                        # doesn't stall the other file's triage and rendering.
                        with PDFIUM_LOCK:
                            image = pdf[i].render(scale=OSD_DPI / 72, grayscale=True).to_pil()
                        png = Path(tmp) / f"{i}.png"
                        image.save(png)
                        pngs.append((i, png))
                finally:
                    with PDFIUM_LOCK:
                        pdf.close()
                with ThreadPoolExecutor(workers) as pool:
                    orientations = list(pool.map(detect_orientation, [png for _, png in pngs]))
            osd_rotate = {i: rot for (i, _), (rot, conf) in zip(pngs, orientations)
                          if rot and conf >= OSD_MIN_CONFIDENCE}
            vprint(f"  rotation: OSD checked {len(osd_indices)} scanned page(s) in {time.monotonic() - t0:.1f}s, "
                   f"sideways: {[i + 1 for i in osd_rotate] or 'none'}")
            to_rotate |= osd_rotate

    return apply_rotations(path, sha, to_rotate)


def text_rotations(path: Path) -> dict[int, int]:
    """Pages whose embedded text runs sideways, and the clockwise rotation that
    puts it upright. Books typeset wide tables rotated on a portrait page, and
    docling's layout model reads the rendered image, so it sees those pages
    sideways unless the page is marked rotated. A born-digital page states its
    angle exactly in each text object's matrix, so unlike tesseract OSD this
    costs no rendering and no OCR."""
    out: dict[int, int] = {}
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            for i in range(len(pdf)):
                angles: Counter[int] = Counter()
                for obj in pdf[i].get_objects(max_depth=3):
                    if obj.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
                        matrix = obj.get_matrix()
                        angles[round(math.degrees(math.atan2(matrix.b, matrix.a)) / 90) % 4 * 90] += 1
                if not angles:
                    continue
                angle, count = angles.most_common(1)[0]
                if angle and count >= ROTATION_MIN_SHARE * sum(angles.values()):
                    out[i] = angle
            return out
        finally:
            pdf.close()


def apply_rotations(path: Path, sha: str, to_rotate: dict[int, int]) -> tuple[Path, list[int]]:
    """Mark the given pages rotated in a copy under data/work. Only the /Rotate
    flag changes; nothing is rasterized. Returns the path to convert and the
    rotated page numbers."""
    if not to_rotate:
        return path, []
    out = WORK / f"{sha}.pdf"
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            for i, rot in to_rotate.items():
                pdf[i].set_rotation((pdf[i].get_rotation() + rot) % 360)
            WORK.mkdir(parents=True, exist_ok=True)
            pdf.save(out)
        finally:
            pdf.close()
    return out, [i + 1 for i in to_rotate]


# ------------------------------------------------------------------- docling --

def wait_for_docling(client: httpx.Client, cfg: Config, deadline: float) -> bool:
    """Block until docling-serve answers /health, or `deadline` seconds pass."""
    t0 = time.monotonic()
    while True:
        try:
            if client.get(f"{cfg.docling_url}/health", timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass  # still down; keep waiting until the deadline
        if time.monotonic() - t0 >= deadline:
            return False
        time.sleep(DOCLING_HEALTH_POLL)


def docling_convert(client: httpx.Client, cfg: Config, path: Path, options: dict, label: str) -> tuple[str, dict]:
    """Convert, retrying from the start if docling-serve dies mid-conversion.

    The retry redoes the whole conversion rather than resuming the poll: the
    task lives in the server's in-memory orchestrator, so a restart takes the
    task_id with it and polling it again would only ever 404."""
    last: Exception | None = None
    for attempt in range(1, DOCLING_ATTEMPTS + 1):
        try:
            return docling_convert_once(client, cfg, path, options, label)
        except DOCLING_GONE as exc:
            last = exc
            if attempt == DOCLING_ATTEMPTS:
                break
            log(f"  docling {label}: server went away ({type(exc).__name__}: {exc or 'connection lost'}); "
                f"waiting for it to come back")
            if not wait_for_docling(client, cfg, DOCLING_RESTART_WAIT):
                raise RuntimeError(
                    f"docling {label}: server did not come back within {DOCLING_RESTART_WAIT:.0f}s "
                    f"of dying ({type(exc).__name__}). Check data/logs/docling-serve.log and `./ragctl status`."
                ) from exc
            log(f"  docling {label}: server is back, retrying from the start "
                f"(attempt {attempt + 1}/{DOCLING_ATTEMPTS})")
    raise RuntimeError(
        f"docling {label}: server died {DOCLING_ATTEMPTS} times converting this file "
        f"(last: {type(last).__name__}: {last}). It may be crashing on this document; "
        f"see data/logs/docling-serve.log."
    ) from last


def docling_convert_once(client: httpx.Client, cfg: Config, path: Path, options: dict, label: str) -> tuple[str, dict]:
    """Convert via docling-serve's async API: long documents would otherwise
    hit its synchronous wait limit (DOCLING_SERVE_MAX_SYNC_WAIT)."""
    vprint(f"  docling {label}: converting {path.name}")
    t0 = time.monotonic()
    with path.open("rb") as f:
        files = {"files": (path.name, f, "application/octet-stream")}
        resp = client.post(f"{cfg.docling_url}/v1/convert/file/async", files=files,
                           data={"to_formats": ["md", "json"], **options}, timeout=120)
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    while True:
        status = client.get(f"{cfg.docling_url}/v1/status/poll/{task_id}", params={"wait": 5}, timeout=60)
        status.raise_for_status()
        task = status.json()
        if task["task_status"] == "failure":
            # A failed task has no result to fetch; the reason is in the status.
            detail = task.get("error_message") or json.dumps(task)[:500]
            raise RuntimeError(f"docling {label} failed: {detail} (full traceback in data/logs/docling-serve.log)")
        if task["task_status"] in ("success", "partial_success"):
            break
    resp = client.get(f"{cfg.docling_url}/v1/result/{task_id}", timeout=300)
    resp.raise_for_status()
    body = resp.json()
    dt = time.monotonic() - t0
    if body.get("status") not in ("success", "partial_success"):
        raise RuntimeError(f"docling {label} reported: {body.get('status')} {body.get('errors') or ''}".strip())
    md = body["document"]["md_content"] or ""
    doc_json = body["document"].get("json_content") or {}
    vprint(
        f"  docling {label}: {body.get('status')} in {dt:.1f}s "
        f"(server processing_time={body.get('processing_time', '?')}s), {len(md)} chars"
    )
    if VERBOSE and body.get("errors"):
        vprint(f"  docling {label}: reported non-fatal errors: {body['errors']}")
    return md, doc_json


def convert_digital(client: httpx.Client, cfg: Config, path: Path, ocr_images: bool = False) -> tuple[str, dict]:
    """The embedded text, unchanged. With ocr_images, docling also OCRs the
    bitmap areas -- the scanned pages of an otherwise digital book -- while
    leaving the pages that have text of their own alone (force_ocr stays off)."""
    options = {
        "do_ocr": "false",
        "table_mode": "accurate",
        "pdf_backend": "dlparse_v4",
    }
    if ocr_images:
        options |= {"do_ocr": "true", "force_ocr": "false",
                    "ocr_engine": "ocrmac", "ocr_lang": cfg.ocr_lang}
    return docling_convert(client, cfg, path, options, "digital+ocr-images" if ocr_images else "digital")


def convert_ocr(client: httpx.Client, cfg: Config, path: Path) -> tuple[str, dict]:
    return docling_convert(client, cfg, path, {
        "do_ocr": "true",
        "force_ocr": "true",  # replace any old OCR layer rather than trust it
        "ocr_engine": "ocrmac",
        "ocr_lang": cfg.ocr_lang,
        "table_mode": "accurate",
        # dlparse_v4 hands OCR an unreadable page image for some scans
        # (JPEG2000 layers with soft masks); pypdfium2 renders them correctly.
        "pdf_backend": "pypdfium2",
    }, "ocrmac")


def convert_vlm(client: httpx.Client, cfg: Config, path: Path) -> tuple[str, dict]:
    if not cfg.vlm_model:
        raise RuntimeError("VLM_MODEL is not set in .env; cannot run VLM fallback")
    vlm_api = {
        "url": f"{cfg.ollama_url}/v1/chat/completions",
        "params": {"model": cfg.vlm_model},
        "response_format": "markdown",
        "prompt": (
            "Transcribe this page to clean, well-structured markdown. "
            "Preserve headings, lists and tables. Do not add commentary."
        ),
        "timeout": 300,
    }
    vprint(f"  docling vlm: using model {cfg.vlm_model} (this is slow, be patient)")
    return docling_convert(client, cfg, path, {
        "pipeline": "vlm",
        "vlm_pipeline_model_api": json.dumps(vlm_api),
    }, "vlm")


def strip_control_chars(md: str, doc_json: dict) -> tuple[str, dict]:
    """Drop control characters from the converted text. Symbol fonts map their
    bullets and dingbats to code points like 0x07, which come through as
    control characters in the embedded text -- readable neither to a person nor
    to the embedding model. Runs after the quality gate, which needs to see
    them: a page that is mostly junk means a broken font map, not a bullet."""
    def clean(value):
        if isinstance(value, str):
            return CONTROL_CHAR_RE.sub("", value)
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {k: (clean(v) if k in TEXT_KEYS or isinstance(v, (list, dict)) else v)
                    for k, v in value.items()}
        return value

    return CONTROL_CHAR_RE.sub("", md), clean(doc_json)


def quality_gate_reason(md: str, pages: int, min_chars_per_page: int) -> str | None:
    """Returns why the text should be redone, or None if it passes."""
    if not md.strip():
        return "conversion produced no text at all"
    junk = len(JUNK_CHAR_RE.findall(md))
    if len(md) and junk / len(md) > 0.02:
        return f"{junk}/{len(md)} chars ({junk / len(md):.1%}) are junk/replacement characters"
    threshold = min_chars_per_page * max(pages, 1)
    if len(md) < threshold:
        return f"only {len(md)} chars for {pages} page(s), below the {threshold} char threshold"
    return None


def fix_heading_levels(path: Path, md: str, doc_json: dict, announce: bool = False) -> tuple[str, dict, str | None]:
    """Heading levels for a converted PDF (see headings.py). Returns the
    re-exported markdown, the corrected JSON and the evidence used; on any
    failure, the input unchanged, since flat headings still chunk fine."""
    try:
        fixed = copy.deepcopy(doc_json)
        report = headings.assign_levels(path, fixed, PDFIUM_LOCK)
        (log if announce else vprint)(f"  {headings.describe(report)}")
        if report["source"] == "none":
            return md, doc_json, None
        from docling_core.types.doc import DoclingDocument

        return DoclingDocument.model_validate(fixed).export_to_markdown(), fixed, report["source"]
    except Exception as exc:  # noqa: BLE001 - optional step; keep docling's output
        log(f"  heading levels skipped: {type(exc).__name__}: {exc}")
        return md, doc_json, None


def write_outputs(sha: str, markdown: str, doc_json: dict) -> tuple[Path, Path]:
    """Outputs are keyed by the source file's sha256, not its name, so two
    different files that share a stem can't overwrite each other."""
    MARKDOWN.mkdir(parents=True, exist_ok=True)
    DOCLING_JSON.mkdir(parents=True, exist_ok=True)
    md_path = MARKDOWN / f"{sha}.md"
    json_path = DOCLING_JSON / f"{sha}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(doc_json), encoding="utf-8")
    vprint(f"  outputs: wrote {len(markdown)} chars -> {md_path}, docling json -> {json_path}")
    return md_path, json_path


# ------------------------------------------------------------------ pipeline --

def process_one(client: httpx.Client, cfg: Config, path: Path, sha: str, osd_workers: int) -> Result:
    result = Result(filename=path.name, sha256=sha)

    try:
        suffix = path.suffix.lower()
        route_parts = []
        convert_path = path

        if suffix in PDF_SUFFIXES:
            pages, counts, labels = triage_pdf(path)
            vprint(f"  sha256={sha}, pages={pages}: {counts['digital']} digital, "
                   f"{counts['figure']} figure, {counts['scan']} scan, "
                   f"{counts['stale-ocr']} stale-ocr, {counts['empty']} empty")
            # Only a stale OCR layer needs force_ocr (it has to be overwritten,
            # not trusted). A bare scan's own image is read by ocr_images on
            # the digital route -- reading the whole book from pixels because
            # of one scanned cover costs accuracy, not just time: OCR mangles
            # what the embedded text has exactly (currency signs, rating
            # codes) and loses the real table cells. A figure page wants the
            # same treatment for the opposite reason: its text is perfect and
            # is kept (force_ocr stays off), but a chart or a table pasted in
            # as a picture is content that only OCR can reach.
            needs_ocr = bool(counts["stale-ocr"])
            ocr_images = bool(counts["scan"] or counts["figure"]) and not needs_ocr
        else:
            pages = 1
            labels = []
            needs_ocr = suffix in IMAGE_SUFFIXES
            ocr_images = False
            vprint(f"  sha256={sha}, {suffix} file")
        result.pages = pages

        md, doc_json = "", {}
        if suffix in PDF_SUFFIXES and not needs_ocr:
            convert_path, rotated = fix_rotation(path, sha, labels, osd_workers)
            if rotated:
                route_parts.append(f"rotated:{len(rotated)}")
            route_parts.append("digital+ocr-images" if ocr_images else "digital")
            md, doc_json = convert_digital(client, cfg, convert_path, ocr_images=ocr_images)
            reason = quality_gate_reason(md, pages, cfg.min_chars_per_page)
            if reason:
                # Embedded text can be garbage (fonts without a usable
                # character map) even when it's visible; OCR the page images.
                vprint(f"  quality gate: embedded text rejected ({reason}), switching to OCR")
                needs_ocr = True
                # That text is being read from pixels now, so its matrices are
                # no more trustworthy than the characters were: measure every
                # page's orientation with OSD instead.
                labels = ["scan" if label in DIGITAL_LABELS else label for label in labels]

        if needs_ocr:
            if suffix in PDF_SUFFIXES:
                # This pass measures the pages the rejected one took from their
                # text matrices, so its rotation tag is superseded.
                route_parts = [p for p in route_parts if not p.startswith("rotated:")]
                convert_path, rotated = fix_rotation(path, sha, labels, osd_workers)
                if rotated:
                    route_parts.append(f"rotated:{len(rotated)}")
            route_parts.append("ocrmac")
            md, doc_json = convert_ocr(client, cfg, convert_path)
        elif suffix not in PDF_SUFFIXES:
            route_parts.append("standard")
            md, doc_json = convert_digital(client, cfg, path)

        vlm_reason = "forced via --force-vlm" if cfg.force_vlm else quality_gate_reason(md, pages, cfg.min_chars_per_page)
        if vlm_reason:
            vprint(f"  quality gate: falling back to VLM ({vlm_reason})")
            route_parts.append("vlm")
            md, doc_json = convert_vlm(client, cfg, convert_path)
        else:
            vprint("  quality gate: passed")

        md, doc_json = strip_control_chars(md, doc_json)

        if suffix in PDF_SUFFIXES and cfg.heading_levels and doc_json:
            md, doc_json, source = fix_heading_levels(convert_path, md, doc_json)
            if source:
                route_parts.append(f"levels:{source}")

        result.route = "+".join(route_parts)
        result.chars = len(md)

        if not md.strip():
            raise RuntimeError("conversion produced no text even after VLM fallback")
        if not doc_json:
            raise RuntimeError("docling returned no json_content; embed.py needs it for chunking")

        md_path, json_path = write_outputs(sha, md, doc_json)
        result.markdown_path = str(md_path.relative_to(REPO_ROOT))
        result.docling_json = str(json_path.relative_to(REPO_ROOT))
        result.status = "processed"

    except Exception as exc:  # noqa: BLE001 - report and continue with the next file
        result.status = "failed"
        result.error = str(exc)

    # --reprocess works on originals already in data/processed; leave them there
    # even if re-conversion fails, since the earlier successful run still stands.
    if not cfg.reprocess:
        dest_dir = PROCESSED if result.status == "processed" else FAILED
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(dest_dir / path.name))
        except Exception as exc:  # noqa: BLE001
            result.error = f"{result.error or ''} | failed to move source file: {exc}".strip(" |")

    append_manifest(result)
    return result


def run_file(cfg: Config, path: Path, sha: str, tag: str, osd_workers: int) -> Result:
    _log_ctx.prefix = f"{tag} "
    log(f"processing: {path.name}")
    t0 = time.monotonic()
    with httpx.Client() as client:  # one per thread
        result = process_one(client, cfg, path, sha, osd_workers)
    dt = time.monotonic() - t0
    if result.status == "processed":
        log(f"-> ok, route={result.route}, chars={result.chars}, {dt:.1f}s total")
    else:
        log(f"-> FAILED ({dt:.1f}s): {result.error}")
    return result


def fix_headings_only(cfg: Config) -> int:
    """--fix-headings: redo heading levels for every processed PDF from its
    stored docling JSON, without converting again."""
    latest: dict[str, dict] = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "processed" and rec.get("docling_json"):
                latest[rec["sha256"]] = rec
            elif rec.get("status") == "deleted":  # see already_processed
                latest.pop(rec["sha256"], None)
    docs = [r for r in latest.values() if Path(r["filename"]).suffix.lower() in PDF_SUFFIXES]
    if not docs:
        print("No processed PDFs with docling JSON.")
        return 0
    failed = 0
    for i, rec in enumerate(docs, start=1):
        sha, name = rec["sha256"], rec["filename"]
        # docling's boxes refer to the file it converted: the rotated copy
        # in data/work when pages were rotated, else the original.
        work = WORK / f"{sha}.pdf"
        pdf = work if "rotated" in (rec.get("route") or "") and work.exists() else PROCESSED / name
        json_path = REPO_ROOT / rec["docling_json"]
        if not pdf.exists() or not json_path.exists():
            print(f"[{i}/{len(docs)}] skip (source PDF or JSON missing): {name}")
            failed += 1
            continue
        doc_json = json.loads(json_path.read_text(encoding="utf-8"))
        md_path = REPO_ROOT / (rec.get("markdown_path") or f"data/markdown/{sha}.md")
        md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        _log_ctx.prefix = f"[{i}/{len(docs)}] "
        log(name)
        md, doc_json, source = fix_heading_levels(pdf, md, doc_json, announce=True)
        if source:
            write_outputs(sha, md, doc_json)
        else:
            failed += 1
    print(f"\nDone. {len(docs) - failed} updated, {failed} unchanged. Run `./ragctl embed` to re-index them.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-vlm", action="store_true", help="Always use the VLM fallback pipeline")
    parser.add_argument("--file", dest="single_file", default=None, help="Process a single file instead of scanning data/inbox")
    parser.add_argument("--reprocess", action="store_true", help="Re-convert originals in data/processed (ignores the already-processed check)")
    parser.add_argument("--fix-headings", action="store_true", help="Only redo heading levels for already processed PDFs, from their stored docling JSON (no OCR)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-stage detail: page triage, rotation, docling timings, quality-gate reasoning, heading levels")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose
    try:
        sys.stdout.reconfigure(line_buffering=True)  # stdout is block-buffered when piped/backgrounded otherwise
    except Exception:  # noqa: BLE001 - cosmetic, never worth failing the run over
        pass

    env = load_env()
    cfg = Config.from_env(env, args.force_vlm, args.reprocess)

    for d in (INBOX, WORK, PROCESSED, FAILED, MARKDOWN, DOCLING_JSON):
        d.mkdir(parents=True, exist_ok=True)

    if args.fix_headings:
        return fix_headings_only(cfg)

    if args.single_file:
        targets = [Path(args.single_file)]
    else:
        source_dir = PROCESSED if args.reprocess else INBOX
        targets = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)

    if not targets:
        print("No files to process.")
        return 0

    with httpx.Client() as probe:
        if not wait_for_docling(probe, cfg, DOCLING_START_WAIT):
            sys.exit(
                f"docling-serve did not answer {cfg.docling_url}/health within "
                f"{DOCLING_START_WAIT:.0f}s. Run `./ragctl status` first."
            )

    total = len(targets)
    todo: list[tuple[Path, str, str]] = []
    seen: set[str] = set()
    for i, path in enumerate(targets, start=1):
        sha = sha256_of(path)
        tag = f"[{i}/{total}]"
        if sha in seen or (not cfg.reprocess and already_processed(sha)):
            why = "duplicate of another file in this batch" if sha in seen else "already processed"
            print(f"{tag} skip ({why}): {path.name}")
            if not cfg.reprocess:
                path.unlink(missing_ok=True)
            continue
        seen.add(sha)
        todo.append((path, sha, tag))

    ok, bad = 0, 0
    if todo:
        workers = min(cfg.workers, len(todo))
        # Orientation detection shells out to tesseract; share the cores
        # between the files being processed at the same time.
        osd_workers = max(1, (os.cpu_count() or 4) // workers)
        if workers > 1:
            print(f"Converting {len(todo)} file(s), {workers} at a time.")
        with ThreadPoolExecutor(workers) as pool:
            futures = [pool.submit(run_file, cfg, path, sha, tag, osd_workers) for path, sha, tag in todo]
            for fut in as_completed(futures):
                if fut.result().status == "processed":
                    ok += 1
                else:
                    bad += 1

    print(f"\nDone. {ok} processed, {bad} failed. See {MANIFEST.relative_to(REPO_ROOT)} for details.")
    return 1 if bad and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
