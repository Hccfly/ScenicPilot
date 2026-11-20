from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scenic_pilot.models import SearchHit

from .context import estimate_context_tokens, select_context_hits
from .service import HybridRetriever


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    source_id: str
    text: str
    label: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceSpan":
        return cls(
            source_id=str(value["source_id"]),
            text=str(value["text"]),
            label=str(value.get("label", "")),
        )


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    case_id: str
    query: str
    scenic_id: str
    expected_source_ids: tuple[str, ...]
    expected_categories: tuple[str, ...] = ()
    must_not_return_source_ids: tuple[str, ...] = ()
    gold_evidence_spans: tuple[EvidenceSpan, ...] = ()
    filters: dict[str, object] | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RetrievalCase":
        return cls(
            case_id=value["case_id"],
            query=value["query"],
            scenic_id=value["scenic_id"],
            expected_source_ids=tuple(value["expected_source_ids"]),
            expected_categories=tuple(value.get("expected_categories", ())),
            must_not_return_source_ids=tuple(
                value.get("must_not_return_source_ids", ())
            ),
            gold_evidence_spans=tuple(
                EvidenceSpan.from_dict(item)
                for item in value.get("gold_evidence_spans", ())
            ),
            filters=value.get("filters"),
            notes=value.get("notes", ""),
        )


def load_retrieval_cases(path: Path) -> list[RetrievalCase]:
    return [
        RetrievalCase.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def normalize_for_evidence_match(text: str) -> str:
    """Keep exact wording while ignoring extraction-only whitespace differences."""

    return "".join(text.split()).casefold()


def select_answer_hits(
    hits: list[SearchHit],
    *,
    limit: int,
    max_context_tokens: int | None = None,
) -> list[SearchHit]:
    """Mirror retrieval policy: distinct parents, up to two per source."""

    selected, _ = select_context_hits(
        hits,
        limit=limit,
        max_tokens=max_context_tokens,
        max_per_source=2,
    )
    return selected


def _covered_spans(
    spans: tuple[EvidenceSpan, ...],
    hits: list[SearchHit],
    *,
    use_parent_context: bool,
) -> list[bool]:
    normalized_by_source: dict[str, list[str]] = {}
    for hit in hits:
        text = (hit.context or hit.chunk.content) if use_parent_context else hit.chunk.content
        normalized_by_source.setdefault(hit.chunk.source_id, []).append(
            normalize_for_evidence_match(text)
        )
    return [
        any(
            normalize_for_evidence_match(span.text) in context
            for context in normalized_by_source.get(span.source_id, ())
        )
        for span in spans
    ]


def _percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def evaluate_retriever(
    retriever: HybridRetriever,
    cases: list[RetrievalCase],
    *,
    recall_k: int = 5,
    ranking_k: int = 10,
    answer_context_k: int = 3,
    max_context_tokens: int | None = 2200,
) -> dict[str, Any]:
    if not cases:
        raise ValueError("retrieval evaluation requires at least one case")
    per_case: list[dict[str, Any]] = []
    hits_at_k: list[float] = []
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    off_target_rates: list[float] = []
    leading_noise_rates: list[float] = []
    evidence_completeness_values: list[float] = []
    child_evidence_recall_values: list[float] = []
    parent_noise_upper_bounds: list[float] = []
    context_token_counts: list[int] = []
    raw_parent_duplicate_rates: list[float] = []
    context_budget_exhausted_cases = 0
    cross_scenic_leaks = 0
    demo_leaks = 0
    forbidden_hits = 0

    for case in cases:
        filters = dict(case.filters or {})
        filters["scenic_id"] = case.scenic_id
        response = retriever.search_with_trace(
            case.query, top_k=ranking_k, filters=filters
        )
        hits = list(response.hits)
        source_ids = [hit.chunk.source_id for hit in hits]
        expected = set(case.expected_source_ids)
        relevant_ranks: list[int] = []
        seen_relevant_sources: set[str] = set()
        for index, source_id in enumerate(source_ids, 1):
            if source_id in expected and source_id not in seen_relevant_sources:
                relevant_ranks.append(index)
                seen_relevant_sources.add(source_id)
        recall = len(set(source_ids[:recall_k]) & expected) / max(1, len(expected))
        hit = 1.0 if set(source_ids[:recall_k]) & expected else 0.0
        reciprocal_rank = 1 / relevant_ranks[0] if relevant_ranks else 0.0
        dcg = sum(
            1 / math.log2(index + 1)
            for index in relevant_ranks
            if index <= ranking_k
        )
        ideal_count = min(len(expected), ranking_k)
        ideal_dcg = sum(1 / math.log2(index + 1) for index in range(1, ideal_count + 1))
        ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
        top_recall_hits = hits[:recall_k]
        off_target_rate = (
            sum(hit.chunk.source_id not in expected for hit in top_recall_hits)
            / len(top_recall_hits)
            if top_recall_hits
            else 1.0
        )
        first_relevant_rank = relevant_ranks[0] if relevant_ranks else None
        leading_noise_rate = (
            min(first_relevant_rank - 1, recall_k) / recall_k
            if first_relevant_rank is not None
            else 1.0
        )
        answer_hits, context_selection = select_context_hits(
            hits,
            limit=answer_context_k,
            max_tokens=max_context_tokens,
            max_per_source=2,
        )
        context_budget_exhausted_cases += int(
            bool(context_selection["budget_exhausted"])
        )
        child_span_coverage = _covered_spans(
            case.gold_evidence_spans,
            top_recall_hits,
            use_parent_context=False,
        )
        parent_span_coverage = _covered_spans(
            case.gold_evidence_spans,
            answer_hits,
            use_parent_context=True,
        )
        child_evidence_recall = (
            sum(child_span_coverage) / len(child_span_coverage)
            if child_span_coverage
            else None
        )
        evidence_completeness = (
            sum(parent_span_coverage) / len(parent_span_coverage)
            if parent_span_coverage
            else None
        )
        selected_contexts = [hit.context or hit.chunk.content for hit in answer_hits]
        estimated_context_tokens = sum(
            estimate_context_tokens(context) for context in selected_contexts
        )
        expected_contexts = [
            hit.context or hit.chunk.content
            for hit in answer_hits
            if hit.chunk.source_id in expected
        ]
        expected_context_chars = sum(
            len(normalize_for_evidence_match(context)) for context in expected_contexts
        )
        covered_evidence_chars = sum(
            len(normalize_for_evidence_match(span.text))
            for span, covered in zip(case.gold_evidence_spans, parent_span_coverage)
            if covered
        )
        parent_noise_upper_bound = (
            max(0.0, 1 - min(1.0, covered_evidence_chars / expected_context_chars))
            if expected_context_chars
            else 1.0
        )
        raw_parent_duplicate_rate = float(
            response.trace.get("drop_counts", {}).get("raw_parent_duplicate_rate", 0.0)
        )
        case_cross_leaks = sum(
            hit.chunk.scenic_id != case.scenic_id for hit in hits
        )
        case_demo_leaks = sum(hit.chunk.demo for hit in hits)
        case_forbidden = sorted(
            set(source_ids) & set(case.must_not_return_source_ids)
        )
        cross_scenic_leaks += case_cross_leaks
        demo_leaks += case_demo_leaks
        forbidden_hits += len(case_forbidden)
        recalls.append(recall)
        hits_at_k.append(hit)
        reciprocal_ranks.append(reciprocal_rank)
        ndcgs.append(ndcg)
        off_target_rates.append(off_target_rate)
        leading_noise_rates.append(leading_noise_rate)
        context_token_counts.append(estimated_context_tokens)
        raw_parent_duplicate_rates.append(raw_parent_duplicate_rate)
        if child_evidence_recall is not None:
            child_evidence_recall_values.append(child_evidence_recall)
        if evidence_completeness is not None:
            evidence_completeness_values.append(evidence_completeness)
            parent_noise_upper_bounds.append(parent_noise_upper_bound)
        per_case.append(
            {
                "case_id": case.case_id,
                "query": case.query,
                "expected_source_ids": list(case.expected_source_ids),
                "returned_source_ids": source_ids,
                "relevant_ranks": relevant_ranks,
                f"hit@{recall_k}": hit,
                f"recall@{recall_k}": round(recall, 6),
                f"reciprocal_rank@{ranking_k}": round(reciprocal_rank, 6),
                f"ndcg@{ranking_k}": round(ndcg, 6),
                f"off_target_rate@{recall_k}": round(off_target_rate, 6),
                f"leading_noise_rate@{recall_k}": round(leading_noise_rate, 6),
                f"child_evidence_recall@{recall_k}": (
                    round(child_evidence_recall, 6)
                    if child_evidence_recall is not None
                    else None
                ),
                "evidence_completeness": (
                    round(evidence_completeness, 6)
                    if evidence_completeness is not None
                    else None
                ),
                "parent_noise_upper_bound": round(parent_noise_upper_bound, 6),
                "estimated_context_tokens": estimated_context_tokens,
                "context_selection": context_selection,
                "answer_contexts": [
                    {
                        "source_id": hit.chunk.source_id,
                        "child_chunk_id": hit.chunk.chunk_id,
                        "parent_chunk_id": hit.parent_chunk_id,
                        "character_count": len(hit.context or hit.chunk.content),
                        "estimated_tokens": estimate_context_tokens(
                            hit.context or hit.chunk.content
                        ),
                    }
                    for hit in answer_hits
                ],
                "gold_evidence_spans": [asdict(span) for span in case.gold_evidence_spans],
                "child_span_coverage": child_span_coverage,
                "parent_span_coverage": parent_span_coverage,
                "raw_parent_duplicate_rate": round(raw_parent_duplicate_rate, 6),
                "cross_scenic_leaks": case_cross_leaks,
                "demo_leaks": case_demo_leaks,
                "forbidden_hits": case_forbidden,
                "passed": bool(hit and not case_forbidden),
                "trace": response.trace,
            }
        )

    metrics = {
        "case_count": len(cases),
        "multi_answer_case_count": sum(
            len(case.expected_source_ids) > 1 for case in cases
        ),
        f"hit@{recall_k}": round(sum(hits_at_k) / len(hits_at_k), 6),
        f"recall@{recall_k}": round(sum(recalls) / len(recalls), 6),
        f"mrr@{ranking_k}": round(
            sum(reciprocal_ranks) / len(reciprocal_ranks), 6
        ),
        f"ndcg@{ranking_k}": round(sum(ndcgs) / len(ndcgs), 6),
        f"off_target_rate@{recall_k}": round(
            sum(off_target_rates) / len(off_target_rates), 6
        ),
        f"leading_noise_rate@{recall_k}": round(
            sum(leading_noise_rates) / len(leading_noise_rates), 6
        ),
        f"child_evidence_recall@{recall_k}": round(
            sum(child_evidence_recall_values) / len(child_evidence_recall_values), 6
        ) if child_evidence_recall_values else None,
        "evidence_completeness": round(
            sum(evidence_completeness_values) / len(evidence_completeness_values), 6
        ) if evidence_completeness_values else None,
        "parent_noise_upper_bound": round(
            sum(parent_noise_upper_bounds) / len(parent_noise_upper_bounds), 6
        ) if parent_noise_upper_bounds else None,
        "average_estimated_context_tokens": round(
            sum(context_token_counts) / len(context_token_counts), 2
        ),
        "p95_estimated_context_tokens": _percentile_95(context_token_counts),
        "raw_parent_duplicate_rate": round(
            sum(raw_parent_duplicate_rates) / len(raw_parent_duplicate_rates), 6
        ),
        "context_budget": max_context_tokens,
        "context_budget_exhausted_case_count": context_budget_exhausted_cases,
        "cross_scenic_leakage": cross_scenic_leaks,
        "demo_leakage": demo_leaks,
        "forbidden_hit_count": forbidden_hits,
        "passed_case_count": sum(item["passed"] for item in per_case),
        "failed_case_count": sum(not item["passed"] for item in per_case),
    }
    return {
        "schema_version": "scenicpilot-retrieval-eval-v2",
        "metrics": metrics,
        "failed_cases": [item for item in per_case if not item["passed"]],
        "cases": per_case,
    }


def case_to_dict(case: RetrievalCase) -> dict[str, Any]:
    return asdict(case)
