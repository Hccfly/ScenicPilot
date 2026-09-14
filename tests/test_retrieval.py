from __future__ import annotations

from pathlib import Path

from scenic_pilot.knowledge.retrieval import (
    CachingEmbeddingProvider,
    DomainQueryNormalizer,
    HybridRetriever,
    RetrievalConfig,
    RetrievalFilters,
)
from scenic_pilot.knowledge.retrieval.evaluation import (
    EvidenceSpan,
    RetrievalCase,
    evaluate_retriever,
)
from scenic_pilot.agent.services import GroundedAnswerer
from scenic_pilot.models import Chunk


class FakeEmbedding:
    model_id = "fake-semantic-v1"

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            float(any(term in text for term in ("鲸鲨", "海洋", "展馆"))),
            float(any(term in text for term in ("吃饭", "餐饮", "餐厅", "美食"))),
            float(any(term in text for term in ("交通", "公交", "接驳"))),
            0.1,
        ]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


def chunk(
    chunk_id: str,
    *,
    source_id: str,
    content: str,
    category: str = "attraction",
    scenic_id: str = "zhuhai-chimelong",
    level: str = "child",
    parent_chunk_id: str | None = None,
    demo: bool = False,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_id=source_id,
        scenic_id=scenic_id,
        title=source_id,
        category=category,
        content=content,
        source_url=f"https://example.test/{source_id}",
        authority_level=3,
        verified_at="2026-09-11",
        volatile=False,
        demo=demo,
        position=0,
        level=level,
        parent_chunk_id=parent_chunk_id,
        heading_path=(source_id,),
    )


def test_query_normalizer_expands_aliases_and_infers_soft_category() -> None:
    normalized = DomainQueryNormalizer().normalize("飞船乐园里面去哪吃饭")

    assert {"餐饮", "餐厅", "美食"}.issubset(set(normalized.expanded_terms))
    assert normalized.category_hints == ("dining",)


def test_metadata_filter_is_hard_for_scenic_and_demo() -> None:
    filters = RetrievalFilters(scenic_id="zhuhai-chimelong")

    assert filters.matches(chunk("ok", source_id="ok", content="鲸鲨"))
    assert not filters.matches(
        chunk("other", source_id="other", content="鲸鲨", scenic_id="other-scenic")
    )
    assert not filters.matches(
        chunk("demo", source_id="demo", content="鲸鲨", demo=True)
    )


def test_hybrid_retrieval_deduplicates_parent_limits_source_and_expands_context() -> None:
    chunks = [
        chunk("p1", source_id="source-a", content="鲸鲨馆完整父级介绍", level="parent"),
        chunk("a1", source_id="source-a", content="鲸鲨", parent_chunk_id="p1"),
        chunk("a2", source_id="source-a", content="鲸鲨展馆", parent_chunk_id="p1"),
        chunk("p2", source_id="source-a", content="海洋展馆父级二", level="parent"),
        chunk("a3", source_id="source-a", content="鲸鲨海洋", parent_chunk_id="p2"),
        chunk("p3", source_id="source-a", content="海洋展馆父级三", level="parent"),
        chunk("a4", source_id="source-a", content="鲸鲨海洋", parent_chunk_id="p3"),
        chunk("p4", source_id="source-b", content="另一个鲸鲨父级", level="parent"),
        chunk("b1", source_id="source-b", content="鲸鲨海洋", parent_chunk_id="p4"),
    ]
    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())

    response = retriever.search_with_trace(
        "鲸鲨馆在哪里", filters={"scenic_id": "zhuhai-chimelong"}, top_k=8
    )

    parent_ids = [hit.parent_chunk_id for hit in response.hits]
    assert len(parent_ids) == len(set(parent_ids))
    assert sum(hit.chunk.source_id == "source-a" for hit in response.hits) == 2
    assert any(hit.chunk.source_id == "source-b" for hit in response.hits)
    assert all(hit.context_expanded for hit in response.hits)
    assert all(hit.context != hit.chunk.content for hit in response.hits)
    assert response.trace["drop_counts"]["source_limit"] >= 1
    assert response.trace["drop_counts"]["raw_parent_duplicate_count"] >= 1
    assert response.trace["drop_counts"]["raw_parent_duplicate_rate"] > 0


def test_category_hint_is_a_soft_boost_not_a_hard_filter() -> None:
    chunks = [
        chunk("food", source_id="food", content="园区餐厅", category="dining"),
        chunk("ride", source_id="ride", content="园区游乐项目", category="attraction"),
    ]
    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())

    hits = retriever.search(
        "园区里面去哪里吃饭", filters={"scenic_id": "zhuhai-chimelong"}, top_k=2
    )

    assert hits[0].chunk.source_id == "food"
    assert hits[0].category_boost_applied is True
    assert {hit.chunk.source_id for hit in hits} == {"food", "ride"}


def test_zero_top_k_returns_no_candidates() -> None:
    retriever = HybridRetriever(
        [chunk("whale", source_id="whale", content="鲸鲨馆")],
        embedding=FakeEmbedding(),
    )

    response = retriever.search_with_trace(
        "鲸鲨馆", filters={"scenic_id": "zhuhai-chimelong"}, top_k=0
    )

    assert response.hits == ()
    assert response.trace["reason"] == "empty_query_or_top_k"


def test_persisted_vector_index_round_trip(tmp_path: Path) -> None:
    chunks = [chunk("whale", source_id="whale", content="鲸鲨馆海洋展馆")]
    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())
    target = tmp_path / "retrieval"

    retriever.dump(target, corpus_fingerprint="corpus-test")
    restored = HybridRetriever.load(target, embedding=FakeEmbedding())

    assert restored.search("鲸鲨", filters={"scenic_id": "zhuhai-chimelong"})[
        0
    ].chunk.source_id == "whale"


def test_evaluation_reports_ranking_and_zero_leakage() -> None:
    chunks = [
        chunk("whale", source_id="whale", content="鲸鲨馆海洋展馆"),
        chunk("food", source_id="food", content="园区餐厅", category="dining"),
    ]
    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())
    cases = [
        RetrievalCase(
            case_id="case-1",
            query="鲸鲨馆",
            scenic_id="zhuhai-chimelong",
            expected_source_ids=("whale",),
        )
    ]

    report = evaluate_retriever(retriever, cases)

    assert report["metrics"]["recall@5"] == 1.0
    assert report["metrics"]["mrr@10"] == 1.0
    assert report["metrics"]["ndcg@10"] == 1.0
    assert report["metrics"]["cross_scenic_leakage"] == 0
    assert report["metrics"]["demo_leakage"] == 0


def test_evaluation_distinguishes_child_hit_from_parent_evidence_coverage() -> None:
    chunks = [
        chunk(
            "parent",
            source_id="whale",
            content="鲸鲨馆完整介绍：这里还能看到大型魔鬼鱼。",
            level="parent",
        ),
        chunk(
            "child",
            source_id="whale",
            content="鲸鲨馆介绍",
            parent_chunk_id="parent",
        ),
    ]
    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())
    cases = [
        RetrievalCase(
            case_id="case-evidence",
            query="鲸鲨馆",
            scenic_id="zhuhai-chimelong",
            expected_source_ids=("whale",),
            gold_evidence_spans=(
                EvidenceSpan(
                    source_id="whale",
                    text="这里还能看到大型魔鬼鱼",
                    label="展示动物",
                ),
            ),
        )
    ]

    report = evaluate_retriever(retriever, cases)

    assert report["metrics"]["child_evidence_recall@5"] == 0.0
    assert report["metrics"]["evidence_completeness"] == 1.0
    assert report["metrics"]["average_estimated_context_tokens"] > 0
    assert report["cases"][0]["parent_span_coverage"] == [True]


def test_caching_embedding_reuses_document_and_query_vectors() -> None:
    class CountingEmbedding(FakeEmbedding):
        def __init__(self) -> None:
            self.document_batches = 0
            self.query_calls = 0

        def embed_documents(self, texts):
            self.document_batches += 1
            return super().embed_documents(texts)

        def embed_query(self, text):
            self.query_calls += 1
            return super().embed_query(text)

    base = CountingEmbedding()
    cached = CachingEmbeddingProvider(base)

    first = cached.embed_documents(["鲸鲨", "餐厅", "鲸鲨"])
    second = cached.embed_documents(["餐厅", "鲸鲨"])
    cached.embed_query("鲸鲨馆")
    cached.embed_query("鲸鲨馆")

    assert len(first) == 3
    assert len(second) == 2
    assert base.document_batches == 1
    assert base.query_calls == 1
    assert cached.cache_stats == {"document_embeddings": 2, "query_embeddings": 1}


def test_grounded_answer_keeps_two_distinct_parents_but_one_source_citation() -> None:
    hits = HybridRetriever(
        [
            chunk("p1", source_id="whale", content="父级证据一", level="parent"),
            chunk("c1", source_id="whale", content="鲸鲨证据一", parent_chunk_id="p1"),
            chunk("p2", source_id="whale", content="父级证据二", level="parent"),
            chunk("c2", source_id="whale", content="鲸鲨证据二", parent_chunk_id="p2"),
        ],
        embedding=FakeEmbedding(),
    ).search("鲸鲨", filters={"scenic_id": "zhuhai-chimelong"}, top_k=2)

    answer, citations = GroundedAnswerer().answer("鲸鲨", hits, min_score=0.0)

    assert "父级证据一" in answer
    assert "父级证据二" in answer
    assert len(citations) == 1


def test_title_only_child_is_excluded_when_parent_has_no_body() -> None:
    chunks = [
        chunk("empty-parent", source_id="hotel", content="## 评分：", level="parent"),
        chunk(
            "empty-child",
            source_id="hotel",
            content="## 评分：",
            parent_chunk_id="empty-parent",
        ),
        chunk(
            "router-parent",
            source_id="hotel",
            content="### 飞船酒店\n拥有主题客房。",
            level="parent",
        ),
        chunk(
            "router-child",
            source_id="hotel",
            content="### 飞船酒店",
            parent_chunk_id="router-parent",
        ),
    ]

    retriever = HybridRetriever(chunks, embedding=FakeEmbedding())

    assert [item.chunk_id for item in retriever.indexed_chunks] == ["router-child"]
    assert retriever.excluded_non_substantive_chunks == 1


def test_grounded_answer_respects_context_token_budget() -> None:
    hits = HybridRetriever(
        [
            chunk("p1", source_id="a", content="鲸鲨" * 40, level="parent"),
            chunk("c1", source_id="a", content="鲸鲨", parent_chunk_id="p1"),
            chunk("p2", source_id="b", content="鲸鲨" * 40, level="parent"),
            chunk("c2", source_id="b", content="鲸鲨", parent_chunk_id="p2"),
        ],
        embedding=FakeEmbedding(),
    ).search("鲸鲨", filters={"scenic_id": "zhuhai-chimelong"}, top_k=2)

    answer, citations = GroundedAnswerer().answer(
        "鲸鲨",
        hits,
        min_score=0.0,
        context_limit=3,
        max_context_tokens=100,
    )

    assert len(answer) < 120
    assert len(citations) == 1
