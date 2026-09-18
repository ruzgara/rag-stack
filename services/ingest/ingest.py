#!/usr/bin/env python3
"""
Batch document ingestion for rag-stack.

Pipeline per file in data/inbox:
  1. PDF -> OCRmyPDF (adds a text layer to scanned pages only; born-digital
     pages are left untouched). Non-PDF files skip this step.
  2. docling-serve, standard pipeline -> markdown.
  3. Quality gate: too little text or too many junk characters -> fallback.
  4. VLM fallback: docling-serve vlm pipeline, calling the configured
     Ollama vision model as an OpenAI-compatible endpoint.
  5. Upload the resulting markdown to Open WebUI and attach it to the
     configured knowledge base.
  6. Move the original file to data/processed or data/failed and record
     the outcome in data/manifest.jsonl.

Run with: uv run ingest.py [--kb NAME] [--force-vlm] [--file PATH] [-v/--verbose]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
INBOX = DATA_DIR / "inbox"
WORK = DATA_DIR / "work"
PROCESSED = DATA_DIR / "processed"
FAILED = DATA_DIR / "failed"
MANIFEST = DATA_DIR / "manifest.jsonl"

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES | {
    ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md", ".txt", ".asciidoc",
}

JUNK_CHAR_RE = re.compile(r"[�\x00-\x08\x0b\x0c\x0e-\x1f]")

VERBOSE = False


def vprint(*args, **kwargs) -> None:
    if VERBOSE:
        kwargs.setdefault("flush", True)  # stdout is block-buffered when piped/backgrounded
        print(*args, **kwargs)


def load_env() -> dict:
    env = {**dotenv_values(REPO_ROOT / ".env"), **os.environ}
    return env


@dataclass
class Config:
    docling_url: str
    owui_url: str
    owui_api_key: str
    kb_name: str
    vlm_model: str
    ocr_lang: str
    min_chars_per_page: int
    force_ocr: bool = False
    force_vlm: bool = False

    @classmethod
    def from_env(cls, env: dict, kb_override: str | None, force_vlm: bool) -> "Config":
        missing = [k for k in ("OWUI_API_KEY",) if not env.get(k)]
        if missing:
            sys.exit(
                f"Missing required .env values: {', '.join(missing)}. "
                "Create an API key in Open WebUI (Admin -> your user -> Settings "
                "-> Account -> API keys) and set OWUI_API_KEY, then re-run."
            )
        docling_port = env.get("DOCLING_PORT", "5001")
        webui_port = env.get("WEBUI_PORT", "3000")
        return cls(
            docling_url=f"http://127.0.0.1:{docling_port}",
            owui_url=f"http://127.0.0.1:{webui_port}",
            owui_api_key=env["OWUI_API_KEY"],
            kb_name=kb_override or env.get("KB_NAME", "Knowledge Base"),
            vlm_model=env.get("VLM_MODEL", ""),
            ocr_lang=env.get("OCR_LANG", "eng"),
            min_chars_per_page=int(env.get("MIN_CHARS_PER_PAGE", "200")),
            force_ocr=env.get("FORCE_OCR", "false").strip().lower() == "true",
            force_vlm=force_vlm,
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
    kb_file_id: str | None = None
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
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a") as f:
        f.write(json.dumps(result.__dict__) + "\n")


def pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return 1


def run_ocrmypdf(src: Path, dst: Path, lang: str, force: bool = False) -> Path:
    """Add a text layer to scanned pages. By default (force=False) skips any
    page that already has text, whatever its quality -- fast and safe for
    born-digital PDFs, but leaves a bad pre-existing OCR layer untouched.
    With force=True (FORCE_OCR=true in .env), every page is rasterized and
    re-OCR'd unconditionally, discarding any existing text layer -- use this
    when your inbox is scans with known-bad prior OCR baked in.
    Returns the path to use for the next step (dst on success, src if
    ocrmypdf declines because the PDF is already fully text, exit code 6 --
    only possible in non-force mode)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    text_strategy = "--force-ocr" if force else "--skip-text"
    cmd = [
        "ocrmypdf", text_strategy, "--rotate-pages", "-l", lang,
        "--output-type", "pdf", str(src), str(dst),
    ]
    vprint(f"  ocrmypdf: {' '.join(cmd)}")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.monotonic() - t0
    if proc.returncode == 0:
        vprint(f"  ocrmypdf: OCR applied in {dt:.1f}s -> {dst.name}")
        if VERBOSE and proc.stderr.strip():
            vprint(f"  ocrmypdf stderr (tail):\n    " + proc.stderr.strip()[-1000:].replace("\n", "\n    "))
        return dst
    if proc.returncode == 6:  # "already has text" (PriorOcrFoundError family)
        vprint(f"  ocrmypdf: already has text, nothing to do ({dt:.1f}s)")
        return src
    raise RuntimeError(f"ocrmypdf failed ({proc.returncode}): {proc.stderr[-2000:]}")


def docling_convert_standard(client: httpx.Client, cfg: Config, path: Path) -> str:
    vprint(f"  docling standard: converting {path.name}")
    t0 = time.monotonic()
    with path.open("rb") as f:
        files = {"files": (path.name, f, "application/octet-stream")}
        data = {
            "to_formats": "md",
            "do_ocr": "false",
            "table_mode": "accurate",
            "pdf_backend": "dlparse_v4",
        }
        resp = client.post(f"{cfg.docling_url}/v1/convert/file", files=files, data=data, timeout=600)
    resp.raise_for_status()
    body = resp.json()
    dt = time.monotonic() - t0
    if body.get("status") not in ("success", "partial_success"):
        raise RuntimeError(f"docling standard pipeline reported: {body.get('status')}")
    md = body["document"]["md_content"] or ""
    vprint(
        f"  docling standard: {body.get('status')} in {dt:.1f}s "
        f"(server processing_time={body.get('processing_time', '?')}s), {len(md)} chars"
    )
    if VERBOSE and body.get("errors"):
        vprint(f"  docling standard: reported non-fatal errors: {body['errors']}")
    return md


def docling_convert_vlm(client: httpx.Client, cfg: Config, path: Path) -> str:
    if not cfg.vlm_model:
        raise RuntimeError("VLM_MODEL is not set in .env; cannot run VLM fallback")
    vlm_api = {
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "params": {"model": cfg.vlm_model},
        "prompt": (
            "Transcribe this page to clean, well-structured markdown. "
            "Preserve headings, lists and tables. Do not add commentary."
        ),
        "timeout": 300,
    }
    vprint(f"  docling vlm: converting {path.name} with model {cfg.vlm_model} (this is slow, be patient)")
    t0 = time.monotonic()
    with path.open("rb") as f:
        files = {"files": (path.name, f, "application/octet-stream")}
        data = {
            "to_formats": "md",
            "pipeline": "vlm",
            "vlm_pipeline_model_api": json.dumps(vlm_api),
        }
        resp = client.post(f"{cfg.docling_url}/v1/convert/file", files=files, data=data, timeout=900)
    resp.raise_for_status()
    body = resp.json()
    dt = time.monotonic() - t0
    if body.get("status") not in ("success", "partial_success"):
        raise RuntimeError(f"docling vlm pipeline reported: {body.get('status')}")
    md = body["document"]["md_content"] or ""
    vprint(
        f"  docling vlm: {body.get('status')} in {dt:.1f}s "
        f"(server processing_time={body.get('processing_time', '?')}s), {len(md)} chars"
    )
    return md


def quality_gate_reason(md: str, pages: int, min_chars_per_page: int) -> str | None:
    """Returns why the VLM fallback should run, or None if the text passes."""
    if not md.strip():
        return "conversion produced no text at all"
    junk = len(JUNK_CHAR_RE.findall(md))
    if len(md) and junk / len(md) > 0.02:
        return f"{junk}/{len(md)} chars ({junk / len(md):.1%}) are junk/replacement characters"
    threshold = min_chars_per_page * max(pages, 1)
    if len(md) < threshold:
        return f"only {len(md)} chars for {pages} page(s), below the {threshold} char threshold"
    return None


def owui_headers(cfg: Config) -> dict:
    return {"Authorization": f"Bearer {cfg.owui_api_key}"}


def owui_get_or_create_kb(client: httpx.Client, cfg: Config) -> str:
    resp = client.get(f"{cfg.owui_url}/api/v1/knowledge/", headers=owui_headers(cfg))
    resp.raise_for_status()
    body = resp.json()
    kbs = body["items"] if isinstance(body, dict) else body  # paginated {"items": [...], "total": N}
    for kb in kbs:
        if kb.get("name") == cfg.kb_name:
            return kb["id"]
    resp = client.post(
        f"{cfg.owui_url}/api/v1/knowledge/create",
        headers=owui_headers(cfg),
        json={"name": cfg.kb_name, "description": "Created by rag-stack ingest.py"},
    )
    resp.raise_for_status()
    return resp.json()["id"]


UPLOAD_RETRY_BACKOFF_S = (5, 15, 30)  # retries on a transient "failed" processing status


def _owui_upload_once(client: httpx.Client, cfg: Config, filename: str, markdown: str) -> str:
    """Upload the markdown and poll until Open WebUI finishes processing it
    (chunking + embedding). Returns the new file_id. Raises on a "failed"
    status or timeout -- the caller decides whether to retry."""
    resp = client.post(
        f"{cfg.owui_url}/api/v1/files/",
        headers=owui_headers(cfg),
        files={"file": (filename, markdown.encode("utf-8"), "text/markdown")},
        timeout=120,
    )
    resp.raise_for_status()
    file_id = resp.json()["id"]
    vprint(f"  open webui: uploaded -> file_id={file_id}, waiting for chunking/embedding")

    deadline = time.time() + 300
    last_status = None
    while time.time() < deadline:
        status_resp = client.get(
            f"{cfg.owui_url}/api/v1/files/{file_id}/process/status", headers=owui_headers(cfg)
        )
        status_resp.raise_for_status()
        status = status_resp.json().get("status")
        if status != last_status:
            vprint(f"  open webui: status={status}")
            last_status = status
        if status == "completed":
            return file_id
        if status == "failed":
            # Open WebUI's non-streaming status endpoint doesn't expose the
            # underlying error (that's only on the SSE variant) -- check
            # `docker compose logs open-webui` for the real cause (a common
            # one: "Ollama embed error (503): ... maximum pending requests
            # exceeded" when a large document floods Ollama's embed queue).
            raise RuntimeError(f"Open WebUI failed to process the uploaded file (file_id={file_id})")
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for Open WebUI to process the uploaded file (file_id={file_id})")


def owui_upload_and_attach(client: httpx.Client, cfg: Config, kb_id: str, filename: str, markdown: str) -> str:
    file_id = None
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((0, *UPLOAD_RETRY_BACKOFF_S), start=1):
        if backoff:
            vprint(f"  open webui: retrying upload in {backoff}s (attempt {attempt}/{len(UPLOAD_RETRY_BACKOFF_S) + 1})")
            time.sleep(backoff)
        try:
            file_id = _owui_upload_once(client, cfg, filename, markdown)
            break
        except Exception as exc:  # noqa: BLE001 - processing failures are often transient (e.g. Ollama's embed queue was briefly overloaded)
            last_exc = exc
            vprint(f"  open webui: attempt {attempt} failed: {exc}")
    if file_id is None:
        raise last_exc  # type: ignore[misc]

    resp = client.post(
        f"{cfg.owui_url}/api/v1/knowledge/{kb_id}/file/add",
        headers=owui_headers(cfg),
        json={"file_id": file_id},
        timeout=120,
    )
    resp.raise_for_status()
    vprint(f"  open webui: attached file_id={file_id} to knowledge base {kb_id}")
    return file_id


def process_one(client: httpx.Client, cfg: Config, path: Path, kb_id: str) -> Result:
    sha = sha256_of(path)
    result = Result(filename=path.name, sha256=sha)

    try:
        suffix = path.suffix.lower()
        pages = pdf_page_count(path) if suffix in PDF_SUFFIXES else 1
        result.pages = pages
        vprint(f"  sha256={sha}, pages={pages}")

        convert_path = path
        route_parts = []
        if suffix in PDF_SUFFIXES:
            WORK.mkdir(parents=True, exist_ok=True)
            ocr_out = WORK / f"{sha}.pdf"
            convert_path = run_ocrmypdf(path, ocr_out, cfg.ocr_lang, force=cfg.force_ocr)
            if convert_path == ocr_out:
                route_parts.append("force-ocr" if cfg.force_ocr else "ocr")
            else:
                route_parts.append("digital")

        route_parts.append("standard")
        md = docling_convert_standard(client, cfg, convert_path)

        vlm_reason = "forced via --force-vlm" if cfg.force_vlm else quality_gate_reason(md, pages, cfg.min_chars_per_page)
        if vlm_reason:
            vprint(f"  quality gate: falling back to VLM ({vlm_reason})")
            route_parts.append("vlm")
            md = docling_convert_vlm(client, cfg, path)  # VLM works from the original
        else:
            vprint("  quality gate: passed, no VLM fallback needed")

        result.route = "+".join(route_parts)
        result.chars = len(md)

        if not md.strip():
            raise RuntimeError("conversion produced no text even after VLM fallback")

        md_filename = f"{path.stem}.md"
        file_id = owui_upload_and_attach(client, cfg, kb_id, md_filename, md)
        result.kb_file_id = file_id
        result.status = "processed"

    except Exception as exc:  # noqa: BLE001 - report and continue with the next file
        result.status = "failed"
        result.error = str(exc)

    dest_dir = PROCESSED if result.status == "processed" else FAILED
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), str(dest_dir / path.name))
    except Exception as exc:  # noqa: BLE001
        result.error = f"{result.error or ''} | failed to move source file: {exc}".strip(" |")

    append_manifest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", dest="kb_name", default=None, help="Knowledge base name (default: KB_NAME in .env)")
    parser.add_argument("--force-vlm", action="store_true", help="Always use the VLM fallback pipeline")
    parser.add_argument("--file", dest="single_file", default=None, help="Process a single file instead of scanning data/inbox")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-stage detail: ocrmypdf output, docling timings, quality-gate reasoning, Open WebUI upload/processing progress")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose
    try:
        sys.stdout.reconfigure(line_buffering=True)  # stdout is block-buffered when piped/backgrounded otherwise
    except Exception:  # noqa: BLE001 - cosmetic, never worth failing the run over
        pass

    env = load_env()
    cfg = Config.from_env(env, args.kb_name, args.force_vlm)

    for d in (INBOX, WORK, PROCESSED, FAILED):
        d.mkdir(parents=True, exist_ok=True)

    if args.single_file:
        targets = [Path(args.single_file)]
    else:
        targets = sorted(p for p in INBOX.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)

    if not targets:
        print("No files to process.")
        return 0

    with httpx.Client() as client:
        # Fail fast if the two local services aren't reachable.
        for name, url in (("docling-serve", f"{cfg.docling_url}/health"), ("Open WebUI", f"{cfg.owui_url}/health")):
            try:
                client.get(url, timeout=10).raise_for_status()
            except Exception as exc:
                sys.exit(f"{name} is not reachable at {url} ({exc}). Run `./ragctl status` first.")

        kb_id = owui_get_or_create_kb(client, cfg)

        ok, bad = 0, 0
        total = len(targets)
        for i, path in enumerate(targets, start=1):
            sha = sha256_of(path)
            if already_processed(sha):
                print(f"[{i}/{total}] skip (already processed): {path.name}")
                path.unlink(missing_ok=True)
                continue
            print(f"[{i}/{total}] processing: {path.name}")
            t0 = time.monotonic()
            result = process_one(client, cfg, path, kb_id)
            dt = time.monotonic() - t0
            if result.status == "processed":
                ok += 1
                print(f"  -> ok, route={result.route}, chars={result.chars}, {dt:.1f}s total")
            else:
                bad += 1
                print(f"  -> FAILED ({dt:.1f}s): {result.error}")

    print(f"\nDone. {ok} processed, {bad} failed. See {MANIFEST.relative_to(REPO_ROOT)} for details.")
    return 1 if bad and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
