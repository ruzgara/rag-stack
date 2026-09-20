import httpx
import pytest
from conftest import SEARCH_ENV, FakeQdrant, FakeReranker, make_hit, point

import retrieval
from retrieval import Retriever, SearchConfig

PARENT = ["Chapter 7", "BOND ISSUES", "The parties"]

# The bond-parties section: an intro chunk, then one chunk per party, in a
# document that has other sections before and after it.
# Token estimates (4 chars/token): "Lead manager" = 10, "The parties" = 27,
# all of "BOND ISSUES" = 43. Tests set RAG_SECTION_MAX_TOKENS against these.
FITS_PARTIES = 30   # "The parties" fits, "BOND ISSUES" doesn't (as in a real book)
BOND_POINTS = [
    point("p09", 9, ["Chapter 7", "BOND ISSUES"], "Bonds are negotiable instruments."),
    point("p10", 10, PARENT, "The parties are:"),
    point("p11", 11, PARENT + ["Investors"], "Institutions and individuals."),
    point("p12", 12, PARENT + ["Issuers"], "Banks and SPVs."),
    point("p13", 13, PARENT + ["Lead manager"], "Arranges the issue."),
    point("p14", 14, PARENT + ["Lead manager"], "Also syndicates it."),
    point("p15", 15, ["Chapter 7", "BOND ISSUES", "Timetable"], "Launch, signing, closing."),
    point("x01", 1, PARENT + ["Investors"], "Same headings, another book.", doc_sha="doc2"),
]


def retriever(cfg: SearchConfig, points: list[dict] = BOND_POINTS) -> tuple[Retriever, FakeQdrant]:
    qdrant = FakeQdrant(points)
    return Retriever(cfg, client=qdrant.client()), qdrant


def hit_for(point_id: str, score: float = 0.9):
    p = next(p for p in BOND_POINTS if p["id"] == point_id)["payload"]
    return make_hit(point_id, p["headings"], score, p["doc_sha"], p["text"], p["chunk_index"], p["pages"])


# ---------------------------------------------------------- whole sections --

def test_hits_sharing_a_heading_become_the_whole_section(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=FITS_PARTIES))
    hits = [hit_for("p11", 0.99), hit_for("p15", 0.8), hit_for("p13", 0.95)]
    out = r.expand_sections(hits)

    section, other = out
    assert section.ids == ["p10", "p11", "p12", "p13", "p14"]  # all of it, in document order
    assert section.headings == PARENT
    assert section.matched == 2
    assert section.score == 0.99 and section.rerank_score == 0.99
    assert section.pages == [10, 11, 12, 13, 14]
    # Subsection headings appear where they start, once each.
    assert section.text == ("The parties are:\n\nInvestors\nInstitutions and individuals.\n\n"
                            "Issuers\nBanks and SPVs.\n\nLead manager\nArranges the issue.\n\nAlso syndicates it.")
    assert section.context_text == "\n".join([*PARENT, section.text])
    assert other.point_id == "p15"


def test_the_deepest_shared_heading_wins(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=20))  # "Lead manager" fits, "The parties" doesn't
    out = r.expand_sections([hit_for("p13"), hit_for("p14"), hit_for("p11")])
    assert out[0].ids == ["p13", "p14"]              # "Lead manager", not all of "The parties"
    assert out[0].headings == PARENT + ["Lead manager"]
    assert [h.point_id for h in out[1:]] == ["p11"]


def test_a_section_takes_the_place_of_its_best_member(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=FITS_PARTIES))
    out = r.expand_sections([hit_for("p15", 0.99), hit_for("p12", 0.9), hit_for("p11", 0.5)])
    assert [len(h.ids) for h in out] == [1, 5]
    assert out[0].point_id == "p15"


def test_sections_too_big_are_left_as_chunks(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=10))
    hits = [hit_for("p11"), hit_for("p13")]
    assert [h.ids for h in r.expand_sections(hits)] == [["p11"], ["p13"]]


def test_sections_with_too_many_chunks_are_not_fetched_whole(search_cfg, monkeypatch):
    monkeypatch.setattr(retrieval, "SECTION_FETCH_LIMIT", 3)
    r, _ = retriever(search_cfg())
    assert [h.ids for h in r.expand_sections([hit_for("p11"), hit_for("p12")])] == [["p11"], ["p12"]]


def test_sections_already_handed_out_in_part_are_not_repeated(search_cfg):
    r, _ = retriever(search_cfg())
    out = r.expand_sections([hit_for("p11"), hit_for("p12")], exclude={"p10"})
    assert [h.ids for h in out] == [["p11"], ["p12"]]


def test_same_headings_in_different_documents_dont_group(search_cfg):
    r, _ = retriever(search_cfg())
    x = next(p for p in BOND_POINTS if p["id"] == "x01")["payload"]
    other_book = make_hit("x01", x["headings"], 0.9, "doc2", x["text"])
    out = r.expand_sections([hit_for("p11"), other_book])
    assert [h.ids for h in out] == [["p11"], ["x01"]]


def test_a_single_hit_is_never_expanded(search_cfg):
    r, qdrant = retriever(search_cfg())
    assert r.expand_sections([hit_for("p11")])[0].ids == ["p11"]
    assert qdrant.requests == []


def test_section_lookup_filters_by_document_and_heading(search_cfg):
    r, qdrant = retriever(search_cfg())
    r.expand_sections([hit_for("p13"), hit_for("p14")])
    must = qdrant.requests[0]["body"]["filter"]["must"]
    assert must == [{"key": "doc_sha", "match": {"value": "doc1"}},
                    {"key": "headings", "match": {"value": "Lead manager"}}]


# ---------------------------------------------------------------- retrieve --

def ranked_retriever(cfg, hits, scores):
    r, _ = retriever(cfg)
    r.search = lambda question, mode=None, limit=None, doc_shas=None: [h for h in hits]
    r._reranker = FakeReranker(scores)
    return r


def test_retrieve_keeps_relevant_results_best_first(search_cfg):
    hits = [hit_for("p09"), hit_for("p15"), hit_for("x01")]
    cfg = search_cfg(RAG_EXPAND_SECTIONS="false")  # p09 and p15 share a heading
    r = ranked_retriever(cfg, hits, {"negotiable": 0.3, "Launch": 0.95, "another book": 0.05})
    out = r.retrieve("q")
    assert [(h.point_id, h.rank, h.score) for h in out] == [("p15", 1, 0.95), ("p09", 2, 0.3)]  # 0.05 < 0.1


def test_retrieve_threshold_is_configurable(search_cfg):
    hits = [hit_for("p09"), hit_for("x01")]
    r = ranked_retriever(search_cfg(RAG_RERANK_MIN_SCORE=0), hits, {"negotiable": 0.3})
    assert len(r.retrieve("q")) == 2


def test_retrieve_reranks_the_indexed_text_with_headings(search_cfg):
    r = ranked_retriever(search_cfg(), [hit_for("p11")], {})
    r.retrieve("who invests?")
    assert r._reranker.calls == [("who invests?", ["Chapter 7\nBOND ISSUES\nThe parties\nInvestors\n"
                                                    "Institutions and individuals."])]


def test_retrieve_expands_sections_then_applies_the_limit(search_cfg):
    hits = [hit_for("p11"), hit_for("p12"), hit_for("p13"), hit_for("x01")]
    r = ranked_retriever(search_cfg(), hits, {"Institutions": 0.99, "Banks": 0.98, "Arranges": 0.97,
                                              "another book": 0.5})
    out = r.retrieve("q", limit=1)
    assert [len(h.ids) for h in out] == [5]  # the section counts as one result
    assert [len(h.ids) for h in r.retrieve("q", limit=2)] == [5, 1]


def test_an_enclosing_section_that_fits_absorbs_an_inner_one(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=50))
    out = r.expand_sections([hit_for("p11", 0.9), hit_for("p12", 0.8), hit_for("p09", 0.7), hit_for("p15", 0.6)])
    assert len(out) == 1
    assert out[0].ids == ["p09", "p10", "p11", "p12", "p13", "p14", "p15"]
    assert out[0].headings == ["Chapter 7", "BOND ISSUES"]
    assert out[0].matched == 4


def test_no_chunk_is_returned_twice_when_the_enclosing_section_is_too_big(search_cfg):
    r, _ = retriever(search_cfg(RAG_SECTION_MAX_TOKENS=FITS_PARTIES))
    out = r.expand_sections([hit_for("p11", 0.9), hit_for("p12", 0.8), hit_for("p09", 0.7), hit_for("p15", 0.6)])
    assert [h.ids for h in out] == [["p10", "p11", "p12", "p13", "p14"], ["p09"], ["p15"]]
    all_ids = [i for h in out for i in h.ids]
    assert len(all_ids) == len(set(all_ids))


def test_retrieve_with_an_empty_doc_shas_set_finds_nothing(search_cfg):
    r, qdrant = retriever(search_cfg())
    assert r.retrieve("bond parties", doc_shas=set()) == []
    assert qdrant.requests == []


def test_retrieve_skips_excluded_chunks(search_cfg):
    hits = [hit_for("p09"), hit_for("p15")]
    r = ranked_retriever(search_cfg(), hits, {"negotiable": 0.9, "Launch": 0.8})
    assert [h.point_id for h in r.retrieve("q", exclude={"p09"})] == ["p15"]


def test_retrieve_without_reranking_or_sections(search_cfg):
    hits = [hit_for("p11"), hit_for("p12"), hit_for("p15")]
    r = ranked_retriever(search_cfg(RAG_RERANK="false", RAG_EXPAND_SECTIONS="false"), hits, {})
    assert [h.point_id for h in r.retrieve("q", limit=2)] == ["p11", "p12"]
    assert r._reranker.calls == []


# ------------------------------------------------------------------ config --

def test_search_defaults():
    cfg = SearchConfig.from_env(SEARCH_ENV)
    assert (cfg.embed_model, cfg.mode, cfg.top_k, cfg.top_k_reranker, cfg.rerank_min_score) == \
        ("bge-m3:latest", "hybrid", 20, 10, 0.1)
    assert cfg.expand_sections and cfg.section_max_tokens == 2000


@pytest.mark.parametrize("env", [
    {"RAG_SEARCH_MODE": "fuzzy"},
    {"RAG_FUSION": "sum"},
    {"RAG_DENSE_WEIGHT": "0", "RAG_BM25_WEIGHT": "0"},
    {"RAG_DENSE_WEIGHT": "-1"},
    {"RAG_RERANK_MIN_SCORE": "1"},
    {},  # no EMBED_MODEL
])
def test_bad_search_settings_are_rejected(env):
    base = {} if env == {} else SEARCH_ENV
    with pytest.raises(ValueError):
        SearchConfig.from_env({**base, **env})


def test_hybrid_query_prefetches_both_retrievers_and_fuses(search_cfg):
    r, _ = retriever(search_cfg(RAG_PREFETCH_K=40, RAG_RRF_K=60, RAG_BM25_WEIGHT=0.5))
    q = r.build_query("bond parties", [0.1, 0.2])
    assert q["prefetch"] == [
        {"query": [0.1, 0.2], "using": "dense", "limit": 40},
        {"query": {"text": "bond parties", "model": "qdrant/bm25"}, "using": "bm25", "limit": 40},
    ]
    assert q["query"] == {"rrf": {"k": 60, "weights": [1.0, 0.5]}}
    assert q["limit"] == 20


def test_single_retriever_and_dbsf_queries(search_cfg):
    r, _ = retriever(search_cfg(RAG_FUSION="dbsf"))
    assert r.build_query("q", [0.1], mode="dense", limit=5) == {"query": [0.1], "using": "dense", "limit": 5,
                                                                  "with_payload": True}
    assert r.build_query("q", None, mode="bm25")["using"] == "bm25"
    assert r.build_query("q", [0.1])["query"] == {"fusion": "dbsf"}


def test_a_library_scope_is_applied_to_each_retriever_not_only_the_fused_query(search_cfg):
    """With prefetch + fusion, a filter placed only at the top level would
    apply after fusion: an unfiltered prefetch would draw its candidates
    from the whole corpus and mostly discard them, so a scoped search would
    silently come back thin. The filter has to sit on each sub-query."""
    r, _ = retriever(search_cfg())
    q = r.build_query("bond parties", [0.1, 0.2], doc_shas={"doc2", "doc1"})
    want = {"must": [{"key": "doc_sha", "match": {"any": ["doc1", "doc2"]}}]}
    assert q["prefetch"][0]["filter"] == want
    assert q["prefetch"][1]["filter"] == want
    assert "filter" not in q  # the outer query has no top-level filter of its own


def test_a_library_scope_on_a_single_retriever_mode(search_cfg):
    r, _ = retriever(search_cfg())
    q = r.build_query("q", [0.1], mode="dense", doc_shas={"doc1"})
    assert q["filter"] == {"must": [{"key": "doc_sha", "match": {"any": ["doc1"]}}]}


def test_no_doc_shas_means_no_filter_at_all(search_cfg):
    r, _ = retriever(search_cfg())
    q = r.build_query("bond parties", [0.1, 0.2])
    assert "filter" not in q["prefetch"][0]
    assert "filter" not in q["prefetch"][1]


def test_an_empty_doc_shas_set_matches_nothing_without_a_request(search_cfg):
    """Distinct from None (unrestricted): a non-admin with zero readable
    libraries must get no results, not an unfiltered search -- and without
    even a wasted round trip to Qdrant."""
    r, qdrant = retriever(search_cfg())
    assert r.search("bond parties", doc_shas=set()) == []
    assert qdrant.requests == []


def test_an_index_built_with_another_model_is_refused(search_cfg):
    def handler(request):
        return httpx.Response(200, json={"result": {"points": [{"id": "a", "payload": {"embed_model": "other:latest"}}]}})

    r = Retriever(search_cfg(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="built with other:latest"):
        r._check_index()
