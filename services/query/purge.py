"""
Removing one document from the machine, for an uploader deleting their
upload (api.delete_upload).

A document is everything ingest and embed made from one file, keyed by its
sha256: its points in Qdrant, data/markdown/<sha>.md, data/docling/<sha>.json,
the rotated working copy in data/work, and the original -- still waiting in
data/inbox, or moved to data/processed or data/failed. All of it goes. Old
chat answers that cited it keep their copy of the passages
(message_sources), same as they do across a re-ingest: they're a record of
what the machine answered at the time.

The manifest is append-only, so the document's history isn't rewritten:
a "deleted" row is appended instead, and every reader (ingest, embed and the
query service) treats it as cancelling the "processed" rows before it. That
is what lets the same file be uploaded again later -- ingest would otherwise
skip it as already processed -- and keeps embed from re-indexing it.

The index goes first: if Qdrant can't be reached nothing else is touched,
and the caller can try again. Files are removed only when their content
still hashes to this document, so a name that has since been reused by a
different file is left alone.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_if_same(path: Path, sha: str) -> bool:
    try:
        if path.is_file() and _sha256(path) == sha:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def manifest_filenames(manifest: Path, sha: str) -> list[str]:
    """Every name this document has been ingested under."""
    names: list[str] = []
    if manifest.exists():
        with manifest.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("sha256") == sha and rec.get("filename") and rec["filename"] not in names:
                    names.append(rec["filename"])
    return names


def purge_document(sha: str, *, retriever, data_dir: Path, inbox: Path, manifest: Path,
                   inbox_names: list[str]) -> None:
    """Removes the document everywhere on disk and from the index, and
    records the deletion in the manifest. `inbox_names` are the names it was
    uploaded under, to find a copy still waiting to be ingested. Raises (and
    touches nothing) if Qdrant can't be reached."""
    retriever.delete_document(sha)

    names = manifest_filenames(manifest, sha)
    for name in {*inbox_names, *names}:
        _remove_if_same(inbox / Path(name).name, sha)
    for folder in ("processed", "failed"):
        for name in names:
            _remove_if_same(data_dir / folder / Path(name).name, sha)
    for derived in (data_dir / "markdown" / f"{sha}.md", data_dir / "docling" / f"{sha}.json",
                    data_dir / "work" / f"{sha}.pdf"):
        derived.unlink(missing_ok=True)

    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a") as f:
        f.write(json.dumps({"sha256": sha, "filename": names[-1] if names else (inbox_names or [sha])[0],
                            "status": "deleted", "timestamp": time.time()}) + "\n")
