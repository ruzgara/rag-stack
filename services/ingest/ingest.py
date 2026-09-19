#!/usr/bin/env python3
"""
Batch document ingestion for rag-stack.

Pipeline per file in data/inbox (up to DOCLING_LOC_NUM_WORKERS files at once):
  1. PDF triage, per page and without rendering: born-digital (visible text,
     not covered by an image) vs scanned (page-sized image, with or without
     an old invisible OCR layer).
  2. Born-digital PDFs: docling-serve converts the embedded text, no OCR.
     If that text fails the quality gate, the file drops to step 3.
  3. Scanned PDFs: tesseract orientation detection marks sideways/upside-down
     pages rotated (lossless, nothing is rasterized), then docling-serve OCRs
     every page with ocrmac (Apple Vision). Images go straight to ocrmac.
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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

# A page counts as scanned when images cover more than this share of it.
# Born-digital pages with figures sit well below; scans are ~1.0.
SCAN_IMAGE_COVER = 0.5
# Resolution pages are rendered at for orientation detection only.
OSD_DPI = 150
# Minimum tesseract orientation confidence to rotate a page. Sideways pages
# measured 3.4-10; near-blank/show-through pages produce spurious guesses <1.
OSD_MIN_CONFIDENCE = 2.0

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
    if not MANIFEST.exists():
        return False
    with MANIFEST.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("sha256") == sha and rec.get("status") == "processed":
                return True
    return False


def append_manifest(result: Result) -> None:
    with MANIFEST_LOCK:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("a") as f:
            f.write(json.dumps(result.__dict__) + "\n")


# -------------------------------------------------------------------- triage --

def page_needs_ocr(page) -> str:
    """'digital', 'scanned' or 'empty', from the page's objects alone."""
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
    # A page-sized image means a scan, whether its text is missing, an old
    # invisible OCR layer, or a few real lines laid over the scan.
    if image_area / (width * height) > SCAN_IMAGE_COVER:
        return "scanned"
    if visible:
        return "digital"
    return "scanned" if invisible else "empty"


def triage_pdf(path: Path) -> tuple[int, dict[str, int]]:
    """Page count and how many pages are digital / scanned / empty."""
    counts = {"digital": 0, "scanned": 0, "empty": 0}
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            for i in range(len(pdf)):
                counts[page_needs_ocr(pdf[i])] += 1
            return len(pdf), counts
        finally:
            pdf.close()


# ------------------------------------------------------------------ rotation --

def detect_orientation(png: Path) -> tuple[int, float]:
    """Tesseract OSD: degrees to rotate clockwise to make the page upright."""
    proc = subprocess.run(["tesseract", str(png), "-", "--psm", "0"], capture_output=True, text=True)
    rotate = re.search(r"Rotate: (\d+)", proc.stdout)
    conf = re.search(r"Orientation confidence: ([\d.]+)", proc.stdout)
    return (int(rotate.group(1)) if rotate else 0, float(conf.group(1)) if conf else 0.0)


def fix_rotation(path: Path, sha: str, workers: int) -> tuple[Path, list[int]]:
    """Mark sideways or upside-down pages rotated. docling never rotates pages
    itself, and OCRs a sideways table into a transposed one. Only the page's
    /Rotate flag changes -- nothing is rasterized. Returns the path to convert
    (a copy in data/work if any page was rotated) and the rotated page numbers."""
    if not shutil.which("tesseract"):
        log("  ! tesseract not found (brew install tesseract); skipping rotation detection")
        return path, []

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(dir=WORK, prefix=f"{sha[:12]}-osd-") as tmp:
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(path)
            n_pages = len(pdf)
        try:
            pngs = []
            for i in range(n_pages):
                # Lock per page, not per document, so a long book doesn't
                # stall the other file's triage and rendering.
                with PDFIUM_LOCK:
                    bitmap = pdf[i].render(scale=OSD_DPI / 72, grayscale=True)
                    image = bitmap.to_pil()
                png = Path(tmp) / f"{i}.png"
                image.save(png)
                pngs.append(png)
        finally:
            with PDFIUM_LOCK:
                pdf.close()
        with ThreadPoolExecutor(workers) as pool:
            orientations = list(pool.map(detect_orientation, pngs))

    to_rotate = {i: rot for i, (rot, conf) in enumerate(orientations) if rot and conf >= OSD_MIN_CONFIDENCE}
    vprint(f"  rotation: checked {len(orientations)} pages in {time.monotonic() - t0:.1f}s, "
           f"rotating {[i + 1 for i in to_rotate] or 'none'}")
    if not to_rotate:
        return path, []

    out = WORK / f"{sha}.pdf"
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            for i, rot in to_rotate.items():
                pdf[i].set_rotation((pdf[i].get_rotation() + rot) % 360)
            pdf.save(out)
        finally:
            pdf.close()
    return out, [i + 1 for i in to_rotate]


# ------------------------------------------------------------------- docling --

def docling_convert(client: httpx.Client, cfg: Config, path: Path, options: dict, label: str) -> tuple[str, dict]:
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


def convert_digital(client: httpx.Client, cfg: Config, path: Path) -> tuple[str, dict]:
    return docling_convert(client, cfg, path, {
        "do_ocr": "false",
        "table_mode": "accurate",
        "pdf_backend": "dlparse_v4",
    }, "digital")


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
            pages, counts = triage_pdf(path)
            vprint(f"  sha256={sha}, pages={pages}: {counts['digital']} digital, "
                   f"{counts['scanned']} scanned, {counts['empty']} empty")
            needs_ocr = counts["scanned"] > 0
        else:
            pages = 1
            needs_ocr = suffix in IMAGE_SUFFIXES
            vprint(f"  sha256={sha}, {suffix} file")
        result.pages = pages

        md, doc_json = "", {}
        if suffix in PDF_SUFFIXES and not needs_ocr:
            route_parts.append("digital")
            md, doc_json = convert_digital(client, cfg, path)
            reason = quality_gate_reason(md, pages, cfg.min_chars_per_page)
            if reason:
                # Embedded text can be garbage (fonts without a usable
                # character map) even when it's visible; OCR the page images.
                vprint(f"  quality gate: embedded text rejected ({reason}), switching to OCR")
                needs_ocr = True

        if needs_ocr:
            if suffix in PDF_SUFFIXES:
                convert_path, rotated = fix_rotation(path, sha, osd_workers)
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

    try:
        httpx.get(f"{cfg.docling_url}/health", timeout=10).raise_for_status()
    except Exception as exc:
        sys.exit(f"docling-serve is not reachable at {cfg.docling_url}/health ({exc}). Run `./ragctl status` first.")

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
