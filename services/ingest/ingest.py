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

Run with: uv run ingest.py [--kb NAME] [--force-vlm] [--file PATH]
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


def run_ocrmypdf(src: Path, dst: Path, lang: str) -> Path:
    """Add a text layer to scanned pages only; leaves digital pages alone.
    Returns the path to use for the next step (dst on success, src if
    ocrmypdf declines because the PDF is already fully text, exit code 6)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ocrmypdf", "--skip-text", "--rotate-pages", "-l", lang,
        "--output-type", "pdf", str(src), str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return dst
    if proc.returncode == 6:  # "already has text" (PriorOcrFoundError family)
        return src
    raise RuntimeError(f"ocrmypdf failed ({proc.returncode}): {proc.stderr[-2000:]}")


def docling_convert_standard(client: httpx.Client, cfg: Config, path: Path) -> str:
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
    if body.get("status") not in ("success", "partial_success"):
        raise RuntimeError(f"docling standard pipeline reported: {body.get('status')}")
    return body["document"]["md_content"] or ""


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
    if body.get("status") not in ("success", "partial_success"):
        raise RuntimeError(f"docling vlm pipeline reported: {body.get('status')}")
    return body["document"]["md_content"] or ""


def needs_vlm_fallback(md: str, pages: int, min_chars_per_page: int) -> bool:
    if not md.strip():
        return True
    junk = len(JUNK_CHAR_RE.findall(md))
    if len(md) and junk / len(md) > 0.02:
        return True
    return len(md) < min_chars_per_page * max(pages, 1)


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


def owui_upload_and_attach(client: httpx.Client, cfg: Config, kb_id: str, filename: str, markdown: str) -> str:
    files = {"file": (filename, markdown.encode("utf-8"), "text/markdown")}
    resp = client.post(f"{cfg.owui_url}/api/v1/files/", headers=owui_headers(cfg), files=files, timeout=120)
    resp.raise_for_status()
    file_id = resp.json()["id"]

    deadline = time.time() + 300
    while time.time() < deadline:
        status_resp = client.get(
            f"{cfg.owui_url}/api/v1/files/{file_id}/process/status", headers=owui_headers(cfg)
        )
        status_resp.raise_for_status()
        status = status_resp.json().get("status")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError("Open WebUI failed to process the uploaded file")
        time.sleep(2)
    else:
        raise RuntimeError("Timed out waiting for Open WebUI to process the uploaded file")

    resp = client.post(
        f"{cfg.owui_url}/api/v1/knowledge/{kb_id}/file/add",
        headers=owui_headers(cfg),
        json={"file_id": file_id},
        timeout=120,
    )
    resp.raise_for_status()
    return file_id


def process_one(client: httpx.Client, cfg: Config, path: Path, kb_id: str) -> Result:
    sha = sha256_of(path)
    result = Result(filename=path.name, sha256=sha)

    try:
        suffix = path.suffix.lower()
        pages = pdf_page_count(path) if suffix in PDF_SUFFIXES else 1
        result.pages = pages

        convert_path = path
        route_parts = []
        if suffix in PDF_SUFFIXES:
            WORK.mkdir(parents=True, exist_ok=True)
            ocr_out = WORK / f"{sha}.pdf"
            convert_path = run_ocrmypdf(path, ocr_out, cfg.ocr_lang)
            route_parts.append("ocr" if convert_path == ocr_out else "digital")

        route_parts.append("standard")
        md = docling_convert_standard(client, cfg, convert_path)

        if cfg.force_vlm or needs_vlm_fallback(md, pages, cfg.min_chars_per_page):
            route_parts.append("vlm")
            md = docling_convert_vlm(client, cfg, path)  # VLM works from the original

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
    args = parser.parse_args()

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
        for path in targets:
            sha = sha256_of(path)
            if already_processed(sha):
                print(f"skip (already processed): {path.name}")
                path.unlink(missing_ok=True)
                continue
            print(f"processing: {path.name}")
            result = process_one(client, cfg, path, kb_id)
            if result.status == "processed":
                ok += 1
                print(f"  -> ok, route={result.route}, chars={result.chars}")
            else:
                bad += 1
                print(f"  -> FAILED: {result.error}")

    print(f"\nDone. {ok} processed, {bad} failed. See {MANIFEST.relative_to(REPO_ROOT)} for details.")
    return 1 if bad and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
