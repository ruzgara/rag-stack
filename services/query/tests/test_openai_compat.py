from chat import Source, TurnResult
from openai_compat import sources_footer, text_of


def test_message_content_as_string_or_parts():
    assert text_of("hello") == "hello"
    assert text_of([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}},
                    {"type": "text", "text": "b"}]) == "a\nb"
    assert text_of(None) == ""


def test_sources_footer_lists_only_cited_sources():
    sources = [Source(number=n, found_by="auto", query="q", point_id=f"p{n}", doc_sha="d", filename=f"doc{n}.pdf",
                      chunk_index=0, pages=[n], headings=[], text="t", rerank_score=None) for n in (1, 2, 3)]
    result = TurnResult(sources=sources, cited=[3, 1])
    assert sources_footer(result) == "\n\nSources:\n[1] doc1.pdf, p. 1\n[3] doc3.pdf, p. 3"
    assert sources_footer(TurnResult(sources=sources, cited=[])) == ""
