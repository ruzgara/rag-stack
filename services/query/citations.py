"""
Citation handles: the `[Sk29fp]` a model cites a passage by, and the
`[1:3]` a reader sees for it. See db.py's module docstring and the redesign
notes for why the two are separate. Stdlib only, and knows nothing about
`Source`, SSE or SQL -- `db.py` imports this module, so this module must
never import `db` back.

  handle_for      doc_sha + chunk_index -> the handle, forever
  CitationNumbers the conversation's handle <-> [doc:chunk] map
  parse_citations the grammar: an answer's `[...]` brackets -> Citation
  resolve         citations -> (handles cited, raw invalid strings)
  assign_numbers  handles -> their [doc:chunk] numbers
  render_answer   handles -> `[doc:chunk]`, for surfaces that don't know handles
  StreamingRenderer   render_answer, but token by token
  CitationScanner numbers citations as they stream, for the live UI
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

# 31 symbols: no 0/o, 1/l/i -- nothing in a handle can be misread as another
# character when someone (or a small model) copies it by eye.
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DIGITS = "23456789"

# S + 5 chars, a digit forced at the third position (index 2 in the match
# groups below). That forced digit is what stops an ordinary word like
# "Scoped" or "Sample" -- six letters, starts with S -- from parsing as a
# handle: no English word has a digit third. Without it, prose would render
# struck-through as an invalid citation for no reason.
HANDLE_RE = re.compile(r"[Ss][a-hj-km-np-z2-9][2-9][a-hj-km-np-z2-9]{3}")

# Any [...] up to a sane length; the grammar inside is what decides whether
# it's a citation at all (parse_bracket) -- unlike the flat-number grammar
# this replaces, this has to also catch bracket text that FAILS to be a
# citation, e.g. a garbled handle, or citations.py couldn't tell a mistyped
# one from ordinary prose.
CITATION_RE = re.compile(r"\[([^\[\]\n]{1,200})\]")


def handle_for(doc_sha: str, chunk_index: int) -> str:
    """The one handle this passage will ever have. blake2b, no salt --
    deterministic is the entire point: the same passage gets the same
    handle in every turn, every conversation, forever, so an answer that
    cites it can be understood on its own long after the turn that wrote it.
    ~7.4M values (8 * 31**4) against a few dozen live passages, so a
    garbled handle overwhelmingly resolves to nothing rather than to some
    other real passage -- see the redesign notes for why that's the design,
    not an accident."""
    digest = hashlib.blake2b(f"{doc_sha}:{chunk_index}".encode(), digest_size=8).digest()
    n = int.from_bytes(digest, "big")
    n, digit_i = divmod(n, len(DIGITS))
    chars = []
    for _ in range(4):
        n, r = divmod(n, len(ALPHABET))
        chars.append(ALPHABET[r])
    return f"S{chars[0]}{DIGITS[digit_i]}{chars[1]}{chars[2]}{chars[3]}"


def canonical_handle(token: str) -> str:
    """"SK29FP" -> "Sk29fp": the model is told to copy handles exactly, but
    normalizing case costs nothing and a capitalized-by-accident handle
    (start of a sentence, a model that upcases everything) shouldn't fail
    to resolve over it. Only meaningful for a token already known to be
    handle-shaped -- see _match_handle."""
    return "S" + token[1:].lower()


def _match_handle(token: str) -> str | None:
    if len(token) != 6 or token[0] not in "Ss":
        return None
    canonical = canonical_handle(token)
    return canonical if HANDLE_RE.fullmatch(canonical) else None


def _chunk_range(text: str) -> list[int] | None:
    """"3" -> [3]; "3-5" -> [3,4,5]; en dash accepted; capped at 50 wide so
    a typo'd range can't claim half the conversation's numbering."""
    bounds = re.split(r"\s*[-–]\s*", text)
    if not (1 <= len(bounds) <= 2) or not all(b.isdigit() for b in bounds):
        return None
    lo, hi = int(bounds[0]), int(bounds[-1])
    if hi < lo:
        return None
    return list(range(lo, min(hi, lo + 50) + 1))


@dataclass(frozen=True)
class Item:
    """Exactly one of the three is set: a handle (`[Sk29fp]`), a resolved
    [doc:chunk] ref, or a raw marker for a segment that read as an attempted
    citation but didn't parse -- `[1-2:3]`, the orphan `4` in `[1:3; 4]`."""
    handle: str | None = None
    ref: tuple[int, int] | None = None
    raw: str | None = None


@dataclass(frozen=True)
class Citation:
    start: int
    end: int
    text: str
    items: list[Item]


def _parse_segment(segment: str, *, flat: bool) -> list[Item]:
    """One `;`-delimited segment (or, in the no-colon case, the whole
    bracket -- see parse_bracket). Left to right, carrying `current_doc`:
    a handle is a handle; `d:c` or `d:c-e` sets current_doc and emits that
    chunk range; a bare number emits against current_doc, or -- if it's the
    segment's first item and current_doc is unset -- becomes current_doc
    instead of emitting anything yet. If nothing ever claims that number as
    a chunk, it's an orphan: real citation syntax, just incomplete, so it
    becomes a raw invalid marker rather than silently vanishing."""
    sep = r"\s*[,;]\s*" if flat else r"\s*,\s*"
    tokens = [t.strip() for t in re.split(sep, segment) if t.strip()]
    items: list[Item] = []
    current_doc: int | None = None
    pending_doc: int | None = None

    def flush_pending() -> None:
        nonlocal pending_doc
        if pending_doc is not None:
            items.append(Item(raw=str(pending_doc)))
            pending_doc = None

    for i, token in enumerate(tokens):
        handle = _match_handle(token)
        if handle is not None:
            flush_pending()
            items.append(Item(handle=handle))
            continue
        if ":" in token:
            flush_pending()
            doc_s, _, chunk_s = token.partition(":")
            doc_s, chunk_s = doc_s.strip(), chunk_s.strip()
            # The document side is never a range -- only chunks are.
            chunks = _chunk_range(chunk_s) if doc_s.isdigit() else None
            if chunks is None:
                items.append(Item(raw=token))
                continue
            current_doc = int(doc_s)
            items.extend(Item(ref=(current_doc, c)) for c in chunks)
            continue
        if not token.isdigit():
            # Not a handle, no colon, not even a number -- ordinary prose
            # ("Scoped", "see") that happens to sit inside brackets. Silently
            # ignored, not flagged invalid: only something that actually
            # looks like an attempted citation earns a struck-through marker.
            continue
        n = int(token)
        if current_doc is not None:
            items.append(Item(ref=(current_doc, n)))
        elif i == 0:
            pending_doc = n
        elif pending_doc is not None:
            current_doc = pending_doc
            items.append(Item(ref=(current_doc, n)))
            pending_doc = None
        else:
            items.append(Item(raw=token))
    flush_pending()
    return items


def parse_bracket(inner: str) -> list[Item] | None:
    """The grammar inside one `[...]`. None means "not a citation at all" --
    the caller leaves the brackets as literal text. Everything else is a
    citation, even if every item in it turns out invalid (that still
    surfaces as a struck-through marker rather than disappearing)."""
    inner = inner.strip()
    if not inner:
        return None
    if inner.isdigit():
        # A bracket that's nothing but a number -- [2024], a stray legacy
        # [3] -- is a filename year or an old citation far more often than
        # a real one, so it stays plain text rather than becoming an
        # "invalid citation" marker on every such passage.
        return None
    has_colon = ":" in inner
    # No colon anywhere: `;` and `,` are equivalent separators over one flat
    # list (this is what lets [2;4] and [2,4] both mean "chunk 4 of doc 2").
    # With a colon somewhere, `;` genuinely separates distinct citations, so
    # it's split first and `current_doc` resets at each one.
    segments = re.split(r"\s*;\s*", inner) if has_colon else [inner]
    items: list[Item] = []
    for segment in segments:
        items.extend(_parse_segment(segment, flat=not has_colon))
    if not items:
        return None
    deduped: list[Item] = []
    seen: set[tuple] = set()
    for item in items:
        key = (item.handle, item.ref, item.raw)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def parse_citations(text: str) -> list[Citation]:
    out = []
    for m in CITATION_RE.finditer(text):
        items = parse_bracket(m.group(1))
        if items is not None:
            out.append(Citation(start=m.start(), end=m.end(), text=m.group(0), items=items))
    return out


def _lowest_free(used: set[int]) -> int:
    n = 1
    while n in used:
        n += 1
    return n


class CitationNumbers:
    """A conversation's handle <-> [doc:chunk] map. Pure in-memory, built
    once by db.citation_state from the messages that still exist, then
    copied and mutated per turn (chat.py.run_turn) so a turn that fails
    part-way leaves nothing half-assigned -- see copy()."""

    def __init__(self) -> None:
        self._handle_to_ref: dict[str, tuple[int, int]] = {}
        self._ref_to_handle: dict[tuple[int, int], str] = {}
        self._sha_to_doc: dict[str, int] = {}
        self._doc_chunks: dict[int, set[int]] = {}
        self._used_docs: set[int] = set()

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping]) -> "CitationNumbers":
        """Reconstructed state, not validated input: total and never
        raises. Rows with no number yet (retrieved, never cited) are
        skipped; on any contradiction between rows (there shouldn't be one)
        the first one wins rather than either raising or guessing which is
        "right"."""
        self = cls()
        for row in rows:
            doc_n, chunk_n = row.get("doc_number"), row.get("chunk_number")
            if doc_n is None or chunk_n is None:
                continue
            handle = row.get("handle") or handle_for(row["doc_sha"], row["chunk_index"])
            self._bind(handle, row["doc_sha"], doc_n, chunk_n)
        return self

    def _bind(self, handle: str, doc_sha: str, doc_n: int, chunk_n: int) -> None:
        if handle in self._handle_to_ref or (doc_n, chunk_n) in self._ref_to_handle:
            return  # first row wins
        self._handle_to_ref[handle] = (doc_n, chunk_n)
        self._ref_to_handle[(doc_n, chunk_n)] = handle
        self._used_docs.add(doc_n)
        self._doc_chunks.setdefault(doc_n, set()).add(chunk_n)
        self._sha_to_doc.setdefault(doc_sha, doc_n)

    def copy(self) -> "CitationNumbers":
        new = CitationNumbers()
        new._handle_to_ref = dict(self._handle_to_ref)
        new._ref_to_handle = dict(self._ref_to_handle)
        new._sha_to_doc = dict(self._sha_to_doc)
        new._doc_chunks = {doc: set(chunks) for doc, chunks in self._doc_chunks.items()}
        new._used_docs = set(self._used_docs)
        return new

    def ref(self, handle: str) -> tuple[int, int] | None:
        return self._handle_to_ref.get(handle)

    def handle_at(self, doc: int, chunk: int) -> str | None:
        return self._ref_to_handle.get((doc, chunk))

    def __contains__(self, handle: object) -> bool:
        return handle in self._handle_to_ref

    def assign(self, handle: str, doc_sha: str) -> tuple[int, int]:
        """Idempotent: citing the same handle twice in one turn (or one
        already numbered from an earlier turn) returns the same pair. A new
        handle gets the sha's existing document number if it has one --
        several passages of the same document share a [doc:*] -- else the
        lowest free document number; within that document, the lowest free
        chunk number."""
        existing = self._handle_to_ref.get(handle)
        if existing is not None:
            return existing
        doc_n = self._sha_to_doc.get(doc_sha)
        if doc_n is None:
            doc_n = _lowest_free(self._used_docs)
            self._used_docs.add(doc_n)
            self._sha_to_doc[doc_sha] = doc_n
        chunks = self._doc_chunks.setdefault(doc_n, set())
        chunk_n = _lowest_free(chunks)
        chunks.add(chunk_n)
        self._handle_to_ref[handle] = (doc_n, chunk_n)
        self._ref_to_handle[(doc_n, chunk_n)] = handle
        return (doc_n, chunk_n)


def resolve(citations: list[Citation], numbers: CitationNumbers,
            known: Mapping[str, object]) -> tuple[list[str], list[str]]:
    """(handles cited, raw invalid strings), both in first-citation order.
    A handle resolves if it's in `known` (this turn's sources) or already
    in `numbers` (cited earlier in the conversation, even if evicted from
    this turn's prompt block); a ref resolves through `numbers.handle_at`.
    Order is exactly the contract assign_numbers consumes."""
    cited: list[str] = []
    invalid: list[str] = []
    for citation in citations:
        for item in citation.items:
            if item.handle is not None:
                if item.handle in known or item.handle in numbers:
                    if item.handle not in cited:
                        cited.append(item.handle)
                elif item.handle not in invalid:
                    invalid.append(item.handle)
            elif item.ref is not None:
                handle = numbers.handle_at(*item.ref)
                if handle is not None and (handle in known or handle in numbers):
                    if handle not in cited:
                        cited.append(handle)
                else:
                    raw = f"{item.ref[0]}:{item.ref[1]}"
                    if raw not in invalid:
                        invalid.append(raw)
            elif item.raw is not None and item.raw not in invalid:
                invalid.append(item.raw)
    return cited, invalid


def assign_numbers(handles: list[str], sha_of: Mapping[str, str],
                    numbers: CitationNumbers) -> dict[str, tuple[int, int]]:
    """handles, in first-citation order -> their [doc:chunk]. assign() is
    idempotent, so this also just returns the number for a handle cited
    earlier in the conversation."""
    return {h: numbers.assign(h, sha_of[h]) for h in handles}


def render_answer(text: str, ref_of: Callable[[str], tuple[int, int] | None]) -> str:
    """Handles -> `[doc:chunk]`, for a surface that was never taught the
    handle scheme (openai_compat's non-streamed reply). One bracket per
    passage, same as the UI: `[Sk29fp, Sm4p2q]` becomes `[1:3][2:4]`, and a
    chunk range expands the same way. A handle ref_of can't resolve (never
    got a number -- e.g. it was invalid) is dropped rather than passed
    through raw: a raw handle must never reach a reader of this surface."""
    out = []
    last = 0
    for citation in parse_citations(text):
        out.append(text[last:citation.start])
        out.append("".join(_render_item(item, ref_of) for item in citation.items))
        last = citation.end
    out.append(text[last:])
    return "".join(out)


def _render_item(item: Item, ref_of: Callable[[str], tuple[int, int] | None]) -> str:
    if item.handle is not None:
        ref = ref_of(item.handle)
        return f"[{ref[0]}:{ref[1]}]" if ref else ""
    if item.ref is not None:
        return f"[{item.ref[0]}:{item.ref[1]}]"
    return ""


class _BracketStream:
    """Splits a stream of tokens into plain text and complete `[...]`
    brackets, holding back once it sees `[` until the bracket closes -- so a
    handle split across two tokens is never read half-written. A `[` with no
    `]` within HOLD_LIMIT characters was never a citation, and is released as
    the prose it turned out to be."""

    HOLD_LIMIT = 64

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[tuple[bool, str]]:
        """(is_bracket, piece) in order, for everything `text` completes.
        Whatever is still being held back stays for the next call."""
        self._buf += text
        out: list[tuple[bool, str]] = []
        while True:
            start = self._buf.find("[")
            if start == -1:
                if self._buf:
                    out.append((False, self._buf))
                self._buf = ""
                return out
            if start:
                out.append((False, self._buf[:start]))
            end = self._buf.find("]", start + 1)
            if end == -1:
                if len(self._buf) - start > self.HOLD_LIMIT:
                    out.append((False, self._buf[start:]))
                    self._buf = ""
                else:
                    self._buf = self._buf[start:]
                return out
            out.append((True, self._buf[start:end + 1]))
            self._buf = self._buf[end + 1:]

    def flush(self) -> str:
        # Whatever's left is an unterminated '[' -- the stream ended
        # mid-citation, so there's no closing bracket to wait for; it's
        # plain text now.
        out, self._buf = self._buf, ""
        return out


class StreamingRenderer:
    """render_answer, fed one token at a time (openai_compat's streamed
    reply). Owns its own CitationNumbers and allocates in first-citation
    order as brackets close -- provably the same order a post-hoc
    resolve()+assign_numbers() pass over the finished text would use, so
    streamed and non-streamed output can't disagree."""

    HOLD_LIMIT = _BracketStream.HOLD_LIMIT

    def __init__(self, sha_of: Mapping[str, str] | None = None, numbers: CitationNumbers | None = None):
        # `sha_of if sha_of is not None else {}`, not `sha_of or {}` -- a
        # caller (openai_compat) builds this dict incrementally as sources
        # arrive and hands it over empty, relying on the same object being
        # mutated in place; `or {}` would silently swap in a throwaway dict
        # the moment it's empty, which is exactly when that caller needs it.
        self._sha_of = sha_of if sha_of is not None else {}
        self._numbers = numbers if numbers is not None else CitationNumbers()
        self._brackets = _BracketStream()

    def feed(self, text: str) -> str:
        return "".join(self._render_bracket(piece) if is_bracket else piece
                       for is_bracket, piece in self._brackets.feed(text))

    def flush(self) -> str:
        return self._brackets.flush()

    def _render_bracket(self, bracket: str) -> str:
        citations = parse_citations(bracket)
        if not citations:
            return bracket
        out = []
        last = 0
        for citation in citations:
            out.append(bracket[last:citation.start])
            out.append("".join(self._render_item(item) for item in citation.items))
            last = citation.end
        out.append(bracket[last:])
        return "".join(out)

    def _render_item(self, item: Item) -> str:
        if item.handle is not None:
            ref = self._numbers.ref(item.handle)
            if ref is None and item.handle in self._sha_of:
                ref = self._numbers.assign(item.handle, self._sha_of[item.handle])
            return f"[{ref[0]}:{ref[1]}]" if ref else ""
        if item.ref is not None:
            return f"[{item.ref[0]}:{item.ref[1]}]"
        return ""


class CitationScanner:
    """Numbers an answer's citations while it streams, so a reader sees
    `[1:3]` the moment the bracket closes instead of a placeholder that only
    resolves at the end of the turn.

    Allocating mid-stream is safe because stream order IS first-citation
    order: every number handed out here is the one the end-of-turn
    assign_numbers() would have given, and assign() is idempotent, so that
    final pass only confirms what happened here. `sha_of` is a lookup rather
    than a dict because the turn's sources grow as tool rounds add to them."""

    def __init__(self, numbers: CitationNumbers, sha_of: Callable[[str], str | None]):
        self._numbers = numbers
        self._sha_of = sha_of
        self._brackets = _BracketStream()

    def feed(self, text: str) -> list[tuple[str, int, int]]:
        """(handle, doc, chunk) for citations that got a number from this
        text. A handle already numbered -- cited earlier in this answer, or
        in an earlier turn -- yields nothing, since the reader's copy of the
        map already has it."""
        out = []
        for is_bracket, piece in self._brackets.feed(text):
            if not is_bracket:
                continue
            for citation in parse_citations(piece):
                for item in citation.items:
                    if item.handle is None or self._numbers.ref(item.handle) is not None:
                        continue
                    doc_sha = self._sha_of(item.handle)
                    if doc_sha is None:  # not one of this turn's passages; resolve() will judge it
                        continue
                    doc, chunk = self._numbers.assign(item.handle, doc_sha)
                    out.append((item.handle, doc, chunk))
        return out
