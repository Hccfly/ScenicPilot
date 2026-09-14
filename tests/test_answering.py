from __future__ import annotations

import json
import re
from datetime import date

from scenic_pilot.answering import AnswerPolicy, GroundedAnswerer
from scenic_pilot.answering.evaluation import RefusalCase, evaluate_answering
from scenic_pilot.knowledge.retrieval.evaluation import RetrievalCase
from scenic_pilot.models import Chunk, SearchHit


def hit(
    source_id: str,
    content: str,
    *,
    title: str = "海洋王国",
    score: float = 0.5,
    keyword_score: float = 0.2,
    matched_terms: tuple[str, ...] = ("海洋王国",),
    authority_level: int = 3,
    verified_at: str | None = "2026-09-11",
    volatile: bool = False,
    refresh_days: int | None = 30,
    status: str = "verified",
    parent_chunk_id: str | None = None,
) -> SearchHit:
    chunk = Chunk(
        chunk_id=f"{source_id}-{parent_chunk_id or 'child'}",
        source_id=source_id,
        scenic_id="zhuhai-chimelong",
        title=title,
        category="overview",
        content=content,
        source_url=f"https://example.test/{source_id}",
        authority_level=authority_level,
        verified_at=verified_at,
        volatile=volatile,
        demo=False,
        position=0,
        status=status,
        refresh_days=refresh_days,
        retrieval_policy="live_check_required" if volatile else "cached_ok",
        source_type="official_web" if authority_level == 3 else "reviewed_platform",
        parent_chunk_id=parent_chunk_id,
    )
    return SearchHit(
        chunk=chunk,
        score=score,
        keyword_score=keyword_score,
        vector_score=0.8,
        matched_terms=matched_terms,
        parent_chunk_id=parent_chunk_id,
        context=content,
        context_expanded=parent_chunk_id is not None,
    )


def test_source_level_citation_ids_cover_same_source_multiple_parents() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "鲸鲨馆有什么",
        [
            hit("whale", "鲸鲨馆可以看到鲸鲨。", parent_chunk_id="p1"),
            hit("whale", "鲸鲨馆还能看到大型魔鬼鱼。", parent_chunk_id="p2"),
        ],
        min_score=0.12,
    )

    assert result.status == "answered"
    assert len(result.citations) == 1
    assert result.citations[0].citation_id == 1
    assert "[1]" in result.answer
    assert "[2]" not in result.answer
    assert result.trace["citation_marker_coverage"] == 1.0
    assert all(claim.citation_ids == (1,) for claim in result.claims)


def test_low_score_and_vector_only_collision_are_refused_with_reasons() -> None:
    answerer = GroundedAnswerer()
    result = answerer.answer_with_trace(
        "量子教室编号",
        [
            hit("low", "量子教室是 301。", score=0.01),
            hit(
                "collision",
                "无关文本。",
                keyword_score=0.0,
                matched_terms=(),
            ),
        ],
        min_score=0.12,
    )

    assert result.status == "refused"
    assert result.citations == ()
    assert result.claims == ()
    assert result.trace["rejection_counts"] == {
        "below_retrieval_score": 1,
        "no_lexical_support": 1,
    }


def test_blocked_source_status_is_not_presented_as_evidence() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "海洋王国有什么",
        [hit("removed", "旧页面内容。", status="removed")],
        min_score=0.12,
    )

    assert result.status == "refused"
    assert result.trace["rejection_counts"] == {"source_status_blocked": 1}


def test_volatile_and_stale_evidence_requires_live_check() -> None:
    answerer = GroundedAnswerer(today=lambda: date(2026, 9, 12))
    result = answerer.answer_with_trace(
        "今天班车怎么坐",
        [
            hit(
                "bus",
                "穿梭巴士运营时间：08:30-21:30。",
                volatile=True,
                verified_at="2026-09-01",
                refresh_days=1,
                matched_terms=("班车", "巴士"),
            )
        ],
        min_score=0.12,
    )

    assert result.status == "requires_live_check"
    assert result.citations[0].freshness == "stale"
    assert result.trace["stale_source_ids"] == ["bus"]
    assert "到访日期" in result.answer


def test_newer_source_resolves_same_authority_conflict_and_discloses_both() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "海洋王国营业时间",
        [
            hit("old", "海洋王国营业时间：10:00-19:00。", verified_at="2026-09-01"),
            hit("new", "海洋王国营业时间：10:00-20:00。", verified_at="2026-09-11"),
        ],
        min_score=0.12,
    )

    assert result.status == "answered"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].resolved is True
    assert result.conflicts[0].chosen_source_id == "new"
    assert result.conflicts[0].resolution_reason == "newer_verified_at"
    assert "优先采用" in result.answer
    assert "另一版本" in result.answer
    assert {item.source_id for item in result.citations} == {"old", "new"}


def test_authority_precedes_recency_when_resolving_conflict() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "海洋王国营业时间",
        [
            hit(
                "official",
                "海洋王国营业时间：10:00-20:00。",
                authority_level=3,
                verified_at="2026-09-01",
            ),
            hit(
                "platform",
                "海洋王国营业时间：10:00-19:00。",
                authority_level=2,
                verified_at="2026-09-11",
            ),
        ],
        min_score=0.12,
    )

    assert result.conflicts[0].chosen_source_id == "official"
    assert result.conflicts[0].resolution_reason == "higher_authority"


def test_equal_priority_conflict_refuses_definitive_conclusion() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "海洋王国营业时间",
        [
            hit("source-a", "海洋王国营业时间：10:00-19:00。"),
            hit("source-b", "海洋王国营业时间：10:00-20:00。"),
        ],
        min_score=0.12,
    )

    assert result.status == "conflict"
    assert result.conflicts[0].resolved is False
    assert result.conflicts[0].resolution_reason == "equal_priority_conflict"
    assert "不能给出确定结论" in result.answer
    assert len(result.citations) == 2
    assert result.trace["unresolved_conflict_count"] == 1


def test_equal_values_do_not_create_false_conflict() -> None:
    result = GroundedAnswerer().answer_with_trace(
        "海洋王国营业时间",
        [
            hit("source-a", "海洋王国营业时间：10:00-20:00。"),
            hit("source-b", "海洋王国营业时间：10:00-20:00。"),
        ],
        min_score=0.12,
    )

    assert result.status == "answered"
    assert result.conflicts == ()


def test_answer_result_contract_is_json_serializable() -> None:
    result = GroundedAnswerer(AnswerPolicy(max_claim_chars=80)).answer_with_trace(
        "鲸鲨馆有什么",
        [hit("whale", "鲸鲨馆可以看到鲸鲨和大型魔鬼鱼。")],
        min_score=0.12,
    )

    payload = result.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["answer_status"] == "answered"
    assert set(map(int, re.findall(r"\[(\d+)]", result.answer))) == {1}


def test_answer_evaluator_checks_grounding_and_refusal_together() -> None:
    class FakeRetriever:
        def search(self, query: str, **kwargs):
            if "鲸鲨" in query:
                return [hit("whale", "鲸鲨馆可以看到鲸鲨。", title="鲸鲨馆")]
            return []

    report = evaluate_answering(
        FakeRetriever(),
        GroundedAnswerer(),
        [
            RetrievalCase(
                case_id="answerable",
                query="鲸鲨馆有什么",
                scenic_id="zhuhai-chimelong",
                expected_source_ids=("whale",),
            )
        ],
        [RefusalCase(case_id="refusal", query="火星基地氧气浓度")],
        top_k=8,
        min_score=0.12,
        context_limit=3,
        max_context_tokens=2200,
    )

    assert report["metrics"]["passed_case_count"] == 2
    assert report["metrics"]["failed_case_count"] == 0
