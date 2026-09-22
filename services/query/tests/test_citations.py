import re

import pytest

from citations import (
    ALPHABET,
    CitationNumbers,
    HANDLE_RE,
    StreamingRenderer,
    assign_numbers,
    canonical_handle,
    handle_for,
    parse_bracket,
    parse_citations,
    render_answer,
    resolve,
)

# ------------------------------------------------------------- handle_for --

def test_handle_for_is_stable_and_shaped_right():
    for doc_sha, chunk in [("a" * 64, 0), ("deadbeef", 1), ("", 999999), ("x", 2**32)]:
        h = handle_for(doc_sha, chunk)
        assert h == handle_for(doc_sha, chunk)  # deterministic
        assert HANDLE_RE.fullmatch(h)
        assert len(h) == 6 and h[0] == "S"
        assert all(c in ALPHABET for c in h[1:])


def test_handle_for_differs_by_sha_and_by_chunk():
    a = handle_for("sha-a", 0)
    b = handle_for("sha-b", 0)
    c = handle_for("sha-a", 1)
    assert len({a, b, c}) == 3


def test_handle_for_collision_rate_is_low():
    """A crude check that the base-31 encoding is actually spreading values
    across the ~7.4M handle space, not silently collapsing onto a few --
    that's the kind of bug this test exists to catch."""
    handles = {handle_for(f"doc-{i}", i % 50) for i in range(10_000)}
    assert len(handles) > 9_900


def test_canonical_handle_normalizes_case():
    assert canonical_handle("SK29FP") == "Sk29fp"
    assert canonical_handle("sk29fp") == "Sk29fp"
    assert canonical_handle("Sk29fp") == "Sk29fp"


# --------------------------------------------------------------- grammar --
# This table is duplicated verbatim in ui/src/tests/citations.test.ts (see
# the comment there) -- a mirror divergence should fail a test, not wait for
# review to notice.

HANDLE_GRAMMAR = [
    ("[Sk29fp]", [("handle", "Sk29fp")]),
    ("[Sk29fp][Sm4p2q]", [("handle", "Sk29fp"), ("handle", "Sm4p2q")]),
    ("[Sk29fp, Sm4p2q]", [("handle", "Sk29fp"), ("handle", "Sm4p2q")]),
    ("[Sk29fp; Sm4p2q]", [("handle", "Sk29fp"), ("handle", "Sm4p2q")]),
]

REF_GRAMMAR = [
    ("[1:3]", [("ref", (1, 3))]),
    ("[1:3][2:4]", [("ref", (1, 3)), ("ref", (2, 4))]),
    ("[1:3, 2:4]", [("ref", (1, 3)), ("ref", (2, 4))]),
    ("[1:3; 2:4]", [("ref", (1, 3)), ("ref", (2, 4))]),
    ("[1:3,4]", [("ref", (1, 3)), ("ref", (1, 4))]),
    ("[1:3-5]", [("ref", (1, 3)), ("ref", (1, 4)), ("ref", (1, 5))]),
    ("[2;4]", [("ref", (2, 4))]),
    ("[2:4;3,5]", [("ref", (2, 4)), ("ref", (3, 5))]),
]


def _flatten(text: str) -> list[tuple]:
    """One bracket's worth of (kind, value) pairs, or several brackets'
    concatenated -- enough to express the grammar table without needing a
    Source/known set (parse_citations alone, not resolve)."""
    out = []
    for m in re.finditer(r"\[([^\[\]]*)\]", text):
        items = parse_bracket(m.group(1))
        assert items is not None, f"{text!r} did not parse as a citation"
        for item in items:
            if item.handle is not None:
                out.append(("handle", item.handle))
            elif item.ref is not None:
                out.append(("ref", item.ref))
            else:
                out.append(("raw", item.raw))
    return out


@pytest.mark.parametrize("text, expected", HANDLE_GRAMMAR + REF_GRAMMAR)
def test_grammar_table(text, expected):
    assert _flatten(text) == expected


def test_orphan_bare_number_after_a_ref_segment_is_an_invalid_marker():
    """[1:3; 4] -- the second segment is bare-only, so 4 reads as a document
    with no chunk: kept as a raw invalid marker, not silently dropped."""
    assert _flatten("[1:3; 4]") == [("ref", (1, 3)), ("raw", "4")]


def test_a_doc_side_range_is_invalid_ranges_are_chunk_only():
    assert _flatten("[1-2:3]") == [("raw", "1-2:3")]


def test_range_is_capped_at_fifty_wide():
    items = parse_bracket("1:3-9999")
    assert items is not None
    assert [i.ref for i in items] == [(1, n) for n in range(3, 54)]
    assert len(items) == 51


def test_handles_are_case_insensitive():
    assert _flatten("[SK29FP]") == [("handle", "Sk29fp")]


def test_scoped_and_sample_are_not_handles():
    """Six letters, starts with S -- but no digit in position 3, so these
    stay literal text rather than becoming struck-through invalid markers."""
    assert parse_bracket("Scoped") is None
    assert parse_bracket("Sample") is None


def test_a_lone_number_is_literal_text_not_a_citation():
    assert parse_bracket("2024") is None
    assert parse_bracket("3") is None
    assert parse_bracket(" 2 ") is None


def test_ordinary_prose_in_brackets_is_not_a_citation():
    assert parse_bracket("a") is None
    assert parse_bracket("see") is None
    assert parse_bracket("") is None


def test_items_are_deduped_by_first_appearance():
    items = parse_bracket("1:3, 1:3, 1:4")
    assert [i.ref for i in items] == [(1, 3), (1, 4)]


def test_parse_citations_finds_spans_and_skips_literal_brackets():
    text = "See [Sk29fp] and also [2024].pdf, plus [1:3]."
    cites = parse_citations(text)
    assert len(cites) == 2
    assert cites[0].text == "[Sk29fp]" and cites[0].items[0].handle == "Sk29fp"
    assert cites[1].text == "[1:3]" and cites[1].items[0].ref == (1, 3)


# --------------------------------------------------------------- resolve --

def test_resolve_splits_known_from_invalid_in_first_citation_order():
    # A garbled-but-handle-shaped token (passes HANDLE_RE, matches nothing) is
    # exactly the failure mode the redesign wants visible -- see resolve()'s
    # docstring and the sparse-handle-space rationale in the plan.
    garbled = handle_for("no-such-sha", 0)
    numbers = CitationNumbers()
    known = {"Sk29fp": object(), "Sm4p2q": object()}
    cites = parse_citations(f"First [Sm4p2q] then [Sk29fp] then [{garbled}] and [9:9].")
    cited, invalid = resolve(cites, numbers, known)
    assert cited == ["Sm4p2q", "Sk29fp"]
    assert invalid == [garbled, "9:9"]


def test_resolve_finds_a_handle_cited_earlier_via_numbers_even_if_evicted():
    handle = handle_for("sha-old", 0)
    numbers = CitationNumbers()
    numbers.assign(handle, "sha-old")
    cites = parse_citations(f"[{handle}]")
    cited, invalid = resolve(cites, numbers, {})  # not in this turn's sources any more
    assert cited == [handle] and invalid == []


def test_resolve_by_ref_only_works_once_numbered():
    numbers = CitationNumbers()
    cites = parse_citations("[1:1]")
    cited, invalid = resolve(cites, numbers, {})
    assert cited == [] and invalid == ["1:1"]
    numbers.assign("Sabc23", "sha")
    cited, invalid = resolve(cites, numbers, {"Sabc23": object()})
    assert cited == ["Sabc23"]


# ---------------------------------------------------------- CitationNumbers --

def test_assign_is_idempotent():
    numbers = CitationNumbers()
    first = numbers.assign("Sabc23", "sha-a")
    assert numbers.assign("Sabc23", "sha-a") == first


def test_assign_allocates_lowest_free_document_after_a_gap():
    numbers = CitationNumbers()
    numbers.assign("Sh1", "sha-a")  # doc 1
    numbers.assign("Sh2", "sha-b")  # doc 2
    numbers._handle_to_ref.pop("Sh1")  # simulate doc 1's only citation vanishing
    numbers._ref_to_handle.pop((1, 1))
    numbers._used_docs.discard(1)
    numbers._sha_to_doc.pop("sha-a")
    assert numbers.assign("Sh3", "sha-c")[0] == 1  # recycled, not 3


def test_assign_allocates_lowest_free_chunk_within_a_document():
    numbers = CitationNumbers()
    numbers.assign("Sh1", "sha-a")  # (1, 1)
    numbers.assign("Sh2", "sha-a")  # (1, 2)
    numbers._handle_to_ref.pop("Sh1")
    numbers._ref_to_handle.pop((1, 1))
    numbers._doc_chunks[1].discard(1)
    assert numbers.assign("Sh3", "sha-a") == (1, 1)


def test_a_documents_passages_share_its_number():
    numbers = CitationNumbers()
    a, b = numbers.assign("Sh1", "sha-a"), numbers.assign("Sh2", "sha-a")
    assert a[0] == b[0] and a[1] != b[1]


def test_from_rows_round_trips_assign():
    numbers = CitationNumbers()
    numbers.assign("Sh1", "sha-a")
    numbers.assign("Sh2", "sha-b")
    rows = [{"handle": "Sh1", "doc_sha": "sha-a", "chunk_index": 0, "doc_number": 1, "chunk_number": 1},
            {"handle": "Sh2", "doc_sha": "sha-b", "chunk_index": 0, "doc_number": 2, "chunk_number": 1}]
    rebuilt = CitationNumbers.from_rows(rows)
    assert rebuilt.ref("Sh1") == (1, 1) and rebuilt.ref("Sh2") == (2, 1)
    assert rebuilt.handle_at(2, 1) == "Sh2"


def test_from_rows_skips_null_numbers_and_never_raises():
    rows = [
        {"handle": "Sh1", "doc_sha": "sha-a", "chunk_index": 0, "doc_number": None, "chunk_number": None},
        {"handle": None, "doc_sha": None, "chunk_index": None, "doc_number": 1, "chunk_number": 1},  # garbage row
        {"handle": "Sh2", "doc_sha": "sha-b", "chunk_index": 0, "doc_number": 1, "chunk_number": 2},
    ]
    numbers = CitationNumbers.from_rows(rows)
    assert "Sh1" not in numbers
    assert numbers.ref("Sh2") == (1, 2)


def test_from_rows_first_row_wins_on_a_contradiction():
    rows = [
        {"handle": "Sh1", "doc_sha": "sha-a", "chunk_index": 0, "doc_number": 1, "chunk_number": 1},
        {"handle": "Sh1", "doc_sha": "sha-a", "chunk_index": 0, "doc_number": 9, "chunk_number": 9},
    ]
    numbers = CitationNumbers.from_rows(rows)
    assert numbers.ref("Sh1") == (1, 1)


def test_copy_is_independent_of_the_original():
    numbers = CitationNumbers()
    numbers.assign("Sh1", "sha-a")
    dup = numbers.copy()
    dup.assign("Sh2", "sha-b")
    assert "Sh2" not in numbers  # mutating the copy left the original untouched
    assert "Sh2" in dup


def test_contains_and_ref_and_handle_at():
    numbers = CitationNumbers()
    numbers.assign("Sh1", "sha-a")
    assert "Sh1" in numbers and "Snone1" not in numbers
    assert numbers.ref("Snone1") is None
    assert numbers.handle_at(99, 1) is None


# -------------------------------------------------------------- rendering --

def test_render_answer_emits_one_bracket_per_passage():
    numbers = CitationNumbers()
    numbers.assign("Sk29fp", "sha-a")
    numbers.assign("Sm4p2q", "sha-b")
    ref_of = numbers.ref
    assert render_answer("Both [Sk29fp, Sm4p2q].", ref_of) == "Both [1:1][2:1]."


def test_render_answer_drops_an_unresolved_handle():
    handle = handle_for("sha-a", 0)
    assert render_answer(f"Cites [{handle}].", lambda h: None) == "Cites ."


def test_render_answer_passes_through_plain_text():
    assert render_answer("No citations here at all.", lambda h: None) == "No citations here at all."


@pytest.mark.parametrize("chunk_size", [1, 3, 7])
def test_streaming_renderer_matches_render_answer(chunk_size):
    text = "Both [Sk29fp, Sm4p2q] agree, but [Sbogus] doesn't and neither does [2024].pdf."
    sha_of = {"Sk29fp": "sha-a", "Sm4p2q": "sha-b"}

    renderer = StreamingRenderer(sha_of)
    streamed = "".join(renderer.feed(text[i:i + chunk_size]) for i in range(0, len(text), chunk_size))
    streamed += renderer.flush()

    numbers = CitationNumbers()
    for h in ("Sk29fp", "Sm4p2q"):
        numbers.assign(h, sha_of[h])
    expected = render_answer(text, numbers.ref)
    assert streamed == expected


def test_streaming_renderer_allocates_in_first_citation_order():
    renderer = StreamingRenderer({"Sk29fp": "sha-a", "Sm4p2q": "sha-b"})
    out = renderer.feed("First [Sm4p2q] then [Sk29fp].") + renderer.flush()
    assert out == "First [1:1] then [2:1]."


def test_streaming_renderer_never_leaks_a_handle_across_chunk_boundaries():
    renderer = StreamingRenderer({"Sk29fp": "sha-a"})
    out = renderer.feed("cite [Sk2") + renderer.feed("9fp] done")
    assert "Sk29fp" not in out
    assert out == "cite [1:1] done"


def test_assign_numbers_is_idempotent_across_calls():
    numbers = CitationNumbers()
    first = assign_numbers(["Sh1", "Sh2"], {"Sh1": "sha-a", "Sh2": "sha-b"}, numbers)
    second = assign_numbers(["Sh2", "Sh1"], {"Sh1": "sha-a", "Sh2": "sha-b"}, numbers)
    assert first == second
