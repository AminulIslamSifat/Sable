"""Tests for engine.library_rag — the retrieval half of Study AI.

Written alongside documentation/26-engine-library-rag.md. These tests exist to
pin down two things the docs got wrong:

1. Chunk<->vector length divergence is NOT silent. ``_vector_scores`` checks
   ``vectors.shape[0] != len(chunks)`` and returns None, so ``retrieve`` falls
   back to lexical scoring. A divergence degrades quality, it does not return
   passages from the wrong pages.
2. ``build()`` deletes an existing ``.npy`` when no embedder is available, so a
   rebuild on a model-less host downgrades a document to lexical mode.
"""
from __future__ import annotations

import json

import pytest

from engine import library_rag as lr


# -- fixtures ---------------------------------------------------------------


class FakeModel:
    """Deterministic stand-in for fastembed.TextEmbedding."""

    def __init__(self, fail_at: int | None = None):
        self.fail_at = fail_at
        self.calls = 0

    def embed(self, texts):
        import numpy as np

        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("fake embedding failure")
        out = []
        for t in texts:
            out.append(np.array([len(t), float(t.count("a") + 1), 1.0], dtype="float32"))
        return out


@pytest.fixture

def embed_dir(tmp_path, monkeypatch):
    """Temp storage dir, in lexical mode by default.

    ``fastembed`` IS installed in this venv, so resetting ``_embedder_probed``
    would re-probe and load the real model. Patching ``_load_embedder`` itself
    is the only reliable way to pin lexical mode.
    """
    target = tmp_path / "embeddings"
    target.mkdir()
    monkeypatch.setattr(lr, "LIBRARY_EMBED_DIR", target)
    monkeypatch.setattr(lr, "_load_embedder", lambda: None)
    return target


@pytest.fixture

def with_model(monkeypatch):
    """Replace the embedder with a deterministic fake (overrides embed_dir)."""
    model = FakeModel()
    monkeypatch.setattr(lr, "_load_embedder", lambda: model)
    return model


# -- chunking ---------------------------------------------------------------


def test_chunk_pages_keeps_pages_separate():
    chunks = lr._chunk_pages(["alpha " * 400, "beta " * 400])
    assert chunks, "expected chunks from long pages"
    pages = {c["page"] for c in chunks}
    assert pages == {1, 2}
    assert all("beta" not in c["text"] for c in chunks if c["page"] == 1)


def test_chunk_pages_skips_empty_pages_but_keeps_numbering():
    chunks = lr._chunk_pages(["", "   ", "real content here", ""])
    assert len(chunks) == 1
    assert chunks[0]["page"] == 3


def test_chunk_pages_short_page_is_one_chunk():
    chunks = lr._chunk_pages(["a short page"])
    assert chunks == [{"page": 1, "text": "a short page"}]


def test_chunk_pages_collapses_whitespace():
    chunks = lr._chunk_pages(["line one\n\n\tline   two"])
    assert chunks[0]["text"] == "line one line two"


def test_chunk_pages_overlaps_by_configured_amount():
    text = "x" * (lr._CHUNK_CHARS * 2)
    chunks = lr._chunk_pages([text])
    assert len(chunks) > 1
    step = lr._CHUNK_CHARS - lr._CHUNK_OVERLAP
    assert chunks[1]["text"][: lr._CHUNK_OVERLAP] == chunks[0]["text"][step: step + lr._CHUNK_OVERLAP]


def test_chunk_pages_drops_sub_80_char_tail():
    # 2401 chars -> starts 0 (1400), 1200 (1201), 2400 (1 char). The 1-char
    # tail is below the 80-char floor and must be dropped.
    chunks = lr._chunk_pages(["y" * 2401])
    assert len(chunks) == 2
    assert all(len(c["text"]) >= 80 for c in chunks)


def test_chunk_pages_tail_break_does_not_kill_later_pages():
    """The ``break`` on a short tail only exits that page's loop."""
    chunks = lr._chunk_pages(["y" * 2401, "SECOND PAGE CONTENT"])
    assert 2 in {c["page"] for c in chunks}


def test_chunk_pages_respects_hard_cap(monkeypatch):
    monkeypatch.setattr(lr, "_MAX_CHUNKS", 3)
    chunks = lr._chunk_pages(["z" * (lr._CHUNK_CHARS * 20)])
    assert len(chunks) == 3


# -- tokenising / lexical scoring -------------------------------------------


def test_tokens_drops_stopwords_and_single_chars():
    tokens = lr._tokens("The a I quick brown fox")
    assert "the" not in tokens
    assert "a" not in tokens
    assert tokens == ["quick", "brown", "fox"]


def test_tokens_keeps_unicode():
    assert "বাংলা" in lr._tokens("বাংলা text")


def test_lexical_scores_prefers_rare_term():
    chunks = [
        {"page": 1, "text": "common word appears here"},
        {"page": 2, "text": "rare unusual distinctive"},
    ]
    scored = lr._lexical_scores("distinctive", chunks)
    by_index = dict(scored)
    assert by_index[1] > by_index[0]


def test_lexical_scores_empty_for_stopword_only_query():
    chunks = [{"page": 1, "text": "anything at all"}]
    assert lr._lexical_scores("the and of", chunks) == []


def test_lexical_scores_length_normalised():
    short = {"page": 1, "text": "target"}
    long = {"page": 2, "text": "target " + "filler " * 500}
    scored = dict(lr._lexical_scores("target", [short, long]))
    assert scored[0] > scored[1]


# -- build / load / remove --------------------------------------------------


def test_build_without_model_writes_chunks_and_no_vectors(embed_dir):
    result = lr.build("doc1", ["hello world"])
    assert result == {"doc_id": "doc1", "chunks": 1, "embedded": False}
    assert (embed_dir / "doc1.json").exists()
    assert not (embed_dir / "doc1.npy").exists()


def test_build_with_model_writes_both_files(embed_dir, with_model):
    result = lr.build("doc2", ["hello world"])
    assert result["embedded"] is True
    assert (embed_dir / "doc2.json").exists()
    assert (embed_dir / "doc2.npy").exists()


def test_build_deletes_stale_vectors_when_model_missing(embed_dir):
    """A rebuild on a model-less host downgrades the doc to lexical mode."""
    import numpy as np

    (embed_dir / "doc3.npy").write_bytes(b"stale")
    np.save(embed_dir / "doc3.npy", np.zeros((1, 3), dtype="float32"))
    assert (embed_dir / "doc3.npy").exists()

    lr.build("doc3", ["hello world"])

    assert not (embed_dir / "doc3.npy").exists(), "build() must not leave a stale .npy behind"


def test_build_is_idempotent(embed_dir, with_model):
    first = lr.build("doc4", ["hello world"])
    second = lr.build("doc4", ["hello world"])
    assert first["chunks"] == second["chunks"]
    assert json.loads((embed_dir / "doc4.json").read_text())["doc_id"] == "doc4"


def test_build_noop_on_empty_input(embed_dir):
    assert lr.build("", ["x"])["chunks"] == 0
    assert lr.build("doc5", [])["chunks"] == 0
    assert lr.build("doc5", ["   "])["chunks"] == 0
    assert not lr.has_index("doc5")


def test_load_rejects_malformed(embed_dir):
    (embed_dir / "bad.json").write_text("{not json")
    assert lr.load("bad") is None
    (embed_dir / "bad2.json").write_text(json.dumps({"chunks": "nope"}))
    assert lr.load("bad2") is None
    assert lr.load("missing") is None


def test_has_index_and_remove(embed_dir, with_model):
    lr.build("doc6", ["hello world"])
    assert lr.has_index("doc6")
    lr.remove("doc6")
    assert not lr.has_index("doc6")
    assert not (embed_dir / "doc6.npy").exists()
    lr.remove("doc6")  # idempotent


# -- the doc correction: divergence is detected, not silent -----------------


def test_vector_scores_returns_none_on_shape_mismatch(embed_dir, with_model):
    import numpy as np

    chunks = [{"page": 1, "text": "a"}, {"page": 2, "text": "b"}, {"page": 3, "text": "c"}]
    (embed_dir / "doc7.json").write_text(json.dumps({"doc_id": "doc7", "chunks": chunks}))
    np.save(embed_dir / "doc7.npy", np.ones((2, 3), dtype="float32"))  # 2 rows, 3 chunks

    assert lr._vector_scores("doc7", "query", chunks) is None


def test_retrieve_falls_back_to_lexical_on_misalignment(embed_dir, with_model):
    """The doc claimed silent wrong passages; the guard prevents that."""
    import numpy as np

    chunks = [
        {"page": 1, "text": "irrelevant filler material"},
        {"page": 2, "text": "photosynthesis chlorophyll chloroplast"},
        {"page": 3, "text": "more unrelated padding"},
    ]
    (embed_dir / "doc8.json").write_text(json.dumps({"doc_id": "doc8", "chunks": chunks}))
    np.save(embed_dir / "doc8.npy", np.ones((2, 3), dtype="float32"))

    hits = lr.retrieve("doc8", "photosynthesis", k=1)

    assert hits, "lexical fallback must still return a passage"
    assert hits[0]["page"] == 2, "must not cite a page that lacks the term"


def test_vector_scores_returns_none_without_vectors(embed_dir, with_model):
    chunks = [{"page": 1, "text": "a"}]
    assert lr._vector_scores("nofile", "q", chunks) is None


def test_retrieve_prefers_vector_when_shapes_agree(embed_dir, with_model):
    import numpy as np

    chunks = [{"page": 1, "text": "bbb"}, {"page": 2, "text": "aaaaaaaa"}]
    (embed_dir / "doc9.json").write_text(json.dumps({"doc_id": "doc9", "chunks": chunks}))
    np.save(embed_dir / "doc9.npy", np.asarray([[1.0, 0.0, 0.0], [0.9, 0.0, 0.0]], dtype="float32"))

    hits = lr.retrieve("doc9", "ignored query", k=2)
    assert {h["page"] for h in hits} == {1, 2}


# -- retrieve contract ------------------------------------------------------


def test_retrieve_unindexed_returns_empty(embed_dir):
    assert lr.retrieve("nope", "query") == []


def test_retrieve_empty_query_returns_empty(embed_dir):
    lr.build("doc10", ["hello world"])
    assert lr.retrieve("doc10", "   ") == []


def test_retrieve_clamps_k(embed_dir):
    lr.build("doc11", ["word " * 500])
    assert len(lr.retrieve("doc11", "word", k=999)) <= 40
    assert len(lr.retrieve("doc11", "word", k=0)) >= 1


def test_retrieve_filters_zero_scores(embed_dir):
    lr.build("doc12", ["completely unrelated content here"])
    assert lr.retrieve("doc12", "zzzzz") == []


# -- context assembly -------------------------------------------------------


def test_context_for_unindexed_returns_empty(embed_dir):
    assert lr.context_for("nope", "q") == ("", [])


def test_context_for_builds_page_blocks(embed_dir):
    lr.build("doc13", ["the mitochondria is the powerhouse", "unrelated appendix"])
    text, pages = lr.context_for("doc13", "mitochondria")
    assert "[page 1]" in text
    assert pages == [1]


def test_context_for_respects_budget(embed_dir):
    lr.build("doc14", ["needle " * 1000])
    text, _ = lr.context_for("doc14", "needle", max_chars=2000)
    assert len(text) < 2100


def test_context_for_never_exceeds_minimum_budget(embed_dir):
    lr.build("doc15", ["needle " * 1000])
    text, pages = lr.context_for("doc15", "needle", max_chars=1)
    assert text and pages


def test_context_for_docs_empty_inputs(embed_dir):
    assert lr.context_for_docs([], "q") == ("", [])
    assert lr.context_for_docs(["a"], "") == ("", [])


def test_context_for_docs_tags_current_and_others(embed_dir):
    lr.build("docA", ["mitochondria in book A"])
    lr.build("docB", ["mitochondria in book B"])

    text, hits = lr.context_for_docs(
        ["docA", "docB"],
        "mitochondria",
        labels={"docA": "Book A", "docB": "Book B"},
        current_doc="docA",
    )

    assert "CURRENT DOCUMENT: Book A" in text
    assert "OTHER DOCUMENT: Book B" in text
    assert {h["doc_id"] for h in hits} == {"docA", "docB"}
    assert next(h for h in hits if h["doc_id"] == "docA")["current"] is True
    assert next(h for h in hits if h["doc_id"] == "docB")["current"] is False


def test_context_for_docs_falls_back_to_doc_id_as_label(embed_dir):
    lr.build("docC", ["mitochondria here"])
    text, hits = lr.context_for_docs(["docC"], "mitochondria", current_doc="docC")
    assert "docC" in text
    assert hits[0]["current"] is True


def test_context_for_docs_dedupes_ids(embed_dir):
    lr.build("docD", ["mitochondria here"])
    _, hits = lr.context_for_docs(["docD", "docD"], "mitochondria", current_doc="docD")
    assert len(hits) == 1


def test_context_for_docs_skips_unindexed(embed_dir):
    lr.build("docE", ["mitochondria here"])
    text, hits = lr.context_for_docs(["docE", "docF"], "mitochondria", current_doc="docE")
    assert text and all(h["doc_id"] == "docE" for h in hits)
