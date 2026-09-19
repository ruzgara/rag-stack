import json

import httpx
import pytest
from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker
from docling_core.types.doc import DocItemLabel, DoclingDocument

import embed
from embed import Chunk, merge_siblings


class WordTokenizer:
    """Counts words: enough to test token budgets without a model download."""

    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FakeChunker:
    tokenizer = WordTokenizer()


def chunk(headings: list[str], words: int, pages: list[int] | None = None, name: str = "w") -> Chunk:
    text = " ".join([name] * words)
    return Chunk(index=0, text=text, context_text="\n".join([*headings, text]), headings=headings,
                 pages=pages or [1])


def merge(chunks: list[Chunk], max_tokens: int = 100) -> list[Chunk]:
    return merge_siblings(FakeChunker(), chunks, max_tokens)


PARENT = ["Chapter 7", "BOND ISSUES", "The parties"]


# ---------------------------------------------------------- sibling merge --

def test_short_siblings_merge_under_their_parent():
    out = merge([
        chunk(PARENT + ["Investors"], 10, [120], "inv"),
        chunk(PARENT + ["Issuers"], 10, [120], "iss"),
        chunk(PARENT + ["Lead manager"], 10, [121], "lead"),
    ])
    assert len(out) == 1
    merged = out[0]
    assert merged.headings == PARENT
    assert merged.pages == [120, 121]
    # Each subsection keeps its heading as a line where it starts.
    assert merged.text.split("\n\n") == [
        "Investors\n" + " ".join(["inv"] * 10),
        "Issuers\n" + " ".join(["iss"] * 10),
        "Lead manager\n" + " ".join(["lead"] * 10),
    ]
    assert merged.context_text == "\n".join([*PARENT, merged.text])


def test_intro_text_merges_with_the_subsections_that_follow():
    out = merge([chunk(PARENT, 10, name="intro"), chunk(PARENT + ["Investors"], 10, name="inv")])
    assert len(out) == 1
    assert out[0].headings == PARENT
    assert out[0].text.startswith(" ".join(["intro"] * 10) + "\n\nInvestors\n")


def test_merge_stops_at_the_token_limit():
    # Parent path = 6 words, each sibling = heading + 30 words: three fit in
    # 100 (6 + 3 * 31 = 99), a fourth wouldn't.
    siblings = [chunk(PARENT + [f"S{i}"], 30, name=f"s{i}") for i in range(5)]
    out = merge(siblings, max_tokens=100)
    assert [len(c.context_text.split()) for c in out] == [99, 68]
    assert [c.text.count("\n\n") + 1 for c in out] == [3, 2]
    assert merge(siblings, max_tokens=98)[0].text.count("\n\n") + 1 == 2


def test_chunks_under_different_parents_stay_apart():
    out = merge([chunk(["A", "x"], 5), chunk(["B", "y"], 5)])
    assert [c.headings for c in out] == [["A", "x"], ["B", "y"]]


def test_top_level_sections_are_not_merged():
    # Siblings with no parent heading would be different chapters.
    out = merge([chunk(["Chapter 1"], 5), chunk(["Chapter 2"], 5)])
    assert len(out) == 2


def test_continuation_chunks_dont_repeat_the_heading():
    out = merge([chunk(PARENT + ["Investors"], 10, name="a"), chunk(PARENT + ["Investors"], 10, name="b"),
                 chunk(PARENT + ["Issuers"], 10, name="c")])
    assert len(out) == 1
    assert out[0].text.count("Investors") == 1


def test_indices_are_renumbered():
    out = merge([chunk(["A"], 5), chunk(PARENT + ["x"], 5), chunk(PARENT + ["y"], 5), chunk(["B"], 5)])
    assert [c.index for c in out] == [0, 1, 2]


# --------------------------------------- heading levels reach the chunker --

def test_levels_give_full_heading_paths_even_for_empty_parents():
    # What ingest's heading step relies on: with levels set, a heading with no
    # text of its own ("The parties") still heads its subsections' paths.
    doc = DoclingDocument(name="t")
    doc.add_heading("Chapter 7", level=1)
    doc.add_heading("BOND ISSUES", level=2)
    doc.add_text(DocItemLabel.TEXT, "Bonds are negotiable instruments.")
    doc.add_heading("The parties", level=3)
    doc.add_heading("Investors", level=4)
    doc.add_text(DocItemLabel.TEXT, "Institutions and individuals.")
    doc.add_heading("Issuers", level=4)
    doc.add_text(DocItemLabel.TEXT, "Banks and SPVs.")
    doc.add_text(DocItemLabel.TEXT, "A page header", content_layer="furniture")

    paths = {c.text: c.meta.headings for c in HierarchicalChunker().chunk(doc)}
    assert paths == {
        "Bonds are negotiable instruments.": ["Chapter 7", "BOND ISSUES"],
        "Institutions and individuals.": ["Chapter 7", "BOND ISSUES", "The parties", "Investors"],
        "Banks and SPVs.": ["Chapter 7", "BOND ISSUES", "The parties", "Issuers"],
    }


# ------------------------------------------------------------------ config --

ENV = {"EMBED_MODEL": "bge-m3", "EMBED_TOKENIZER": "BAAI/bge-m3"}


def test_config_requires_model_and_tokenizer():
    with pytest.raises(SystemExit):
        embed.Config.from_env({"EMBED_MODEL": "bge-m3"})


def test_untagged_model_name_is_normalized():
    assert embed.Config.from_env(ENV).embed_model == "bge-m3:latest"
    assert embed.Config.from_env({**ENV, "EMBED_MODEL": "bge-m3:567m"}).embed_model == "bge-m3:567m"


@pytest.mark.parametrize("change", [
    {"CHUNK_MAX_TOKENS": "256"},
    {"CHUNK_MERGE_SIBLINGS": "false"},
    {"EMBED_TOKENIZER": "other/tokenizer"},
    {"EMBED_MODEL": "nomic-embed-text"},
])
def test_settings_that_change_chunks_change_the_fingerprint(change):
    assert embed.Config.from_env({**ENV, **change}).fingerprint != embed.Config.from_env(ENV).fingerprint


def test_batch_size_doesnt_change_the_fingerprint():
    assert embed.Config.from_env({**ENV, "EMBED_BATCH_SIZE": "8"}).fingerprint == embed.Config.from_env(ENV).fingerprint


def test_document_fingerprint_follows_its_content(tmp_path):
    cfg = embed.Config.from_env(ENV)
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1}')
    doc = embed.Doc(sha256="abc", filename="f.pdf", docling_json=path)
    before = embed.doc_fingerprint(cfg, doc)
    path.write_text('{"a": 2}')
    assert embed.doc_fingerprint(cfg, doc) != before


# ---------------------------------------------------------------- manifest --

def test_select_docs_takes_the_latest_processed_row(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(embed, "MANIFEST", tmp_path / "manifest.jsonl")
    (tmp_path / "docling").mkdir()
    for name in ("aaa-v2", "bbb"):
        (tmp_path / "docling" / f"{name}.json").write_text("{}")
    rows = [
        {"sha256": "aaa", "filename": "a.pdf", "status": "processed", "docling_json": "docling/aaa-v1.json"},
        {"sha256": "aaa", "filename": "a.pdf", "status": "processed", "docling_json": "docling/aaa-v2.json"},
        {"sha256": "bbb", "filename": "b.pdf", "status": "processed", "docling_json": "docling/bbb.json"},
        {"sha256": "ccc", "filename": "c.pdf", "status": "processed"},  # from before JSON output
        {"sha256": "ddd", "filename": "d.pdf", "status": "failed"},
    ]
    embed.MANIFEST.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")

    docs, no_json = embed.select_docs(None)
    assert [(d.sha256, d.docling_json.name) for d in docs] == [("aaa", "aaa-v2.json"), ("bbb", "bbb.json")]
    assert no_json == ["c.pdf"]
    assert [d.sha256 for d in embed.select_docs("bb")[0]] == ["bbb"]


# ------------------------------------------------------------ ollama embed --

def ollama(responses: list):
    """httpx client whose /api/embed answers come from `responses` in order."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(embed.time, "sleep", waits.append)
    return waits


def test_embed_batch_sends_one_request_per_batch(no_sleep):
    client, calls = ollama([httpx.Response(200, json={"embeddings": [[0.1], [0.2]]})])
    assert embed.embed_batch(client, embed.Config.from_env(ENV), ["a", "b"]) == [[0.1], [0.2]]
    assert calls == [{"model": "bge-m3:latest", "input": ["a", "b"], "truncate": False, "keep_alive": "10m"}]


def test_embed_batch_retries_a_full_queue(no_sleep):
    client, calls = ollama([httpx.Response(503, text="queue full"), httpx.ConnectError("loading"),
                            httpx.Response(200, json={"embeddings": [[1.0]]})])
    assert embed.embed_batch(client, embed.Config.from_env(ENV), ["a"]) == [[1.0]]
    assert len(calls) == 3
    assert no_sleep == list(embed.EMBED_RETRY_BACKOFF_S[:2])


def test_embed_batch_gives_up_after_the_last_retry(no_sleep):
    client, calls = ollama([httpx.Response(503)] * 4)
    with pytest.raises(RuntimeError, match="after 4 attempts"):
        embed.embed_batch(client, embed.Config.from_env(ENV), ["a"])


def test_embed_batch_fails_fast_on_a_client_error(no_sleep):
    client, calls = ollama([httpx.Response(400, text="input too long")])
    with pytest.raises(RuntimeError, match="input too long"):
        embed.embed_batch(client, embed.Config.from_env(ENV), ["a"])
    assert len(calls) == 1 and no_sleep == []


def test_embed_batch_checks_the_vector_count(no_sleep):
    client, _ = ollama([httpx.Response(200, json={"embeddings": [[1.0]]})])
    with pytest.raises(RuntimeError, match="1 embeddings for 2 inputs"):
        embed.embed_batch(client, embed.Config.from_env(ENV), ["a", "b"])


def test_doc_filter():
    assert embed.doc_filter("abc") == {"must": [{"key": "doc_sha", "match": {"value": "abc"}}]}
    assert embed.doc_filter("abc", "fp", exclude_fingerprint=True) == {
        "must": [{"key": "doc_sha", "match": {"value": "abc"}}],
        "must_not": [{"key": "fingerprint", "match": {"value": "fp"}}],
    }
