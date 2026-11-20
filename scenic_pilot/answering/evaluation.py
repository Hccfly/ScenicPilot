from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from scenic_pilot.answering.service import GroundedAnswerer
from scenic_pilot.knowledge.retrieval.evaluation import RetrievalCase


CITATION_MARKER = re.compile(r"\[(\d+)]")


class SearchableRetriever(Protocol):
    def search(self, query: str, **kwargs: Any): ...


@dataclass(frozen=True, slots=True)
class RefusalCase:
    case_id: str
    query: str
    scenic_id: str = "zhuhai-chimelong"
    requires_live_data: bool = False


def load_refusal_cases(path: Path) -> list[RefusalCase]:
    return [
        RefusalCase(**json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def evaluate_answering(
    retriever: SearchableRetriever,
    answerer: GroundedAnswerer,
    answerable_cases: Sequence[RetrievalCase],
    refusal_cases: Sequence[RefusalCase],
    *,
    top_k: int,
    min_score: float,
    context_limit: int,
    max_context_tokens: int,
) -> dict[str, Any]:
    case_results: list[dict[str, Any]] = []
    expected_source_hits = 0
    marker_valid_cases = 0
    unsupported_claim_count = 0
    false_refusal_count = 0
    false_answer_count = 0
    passed_answerable = 0
    passed_refusal = 0

    for case in answerable_cases:
        hits = retriever.search(
            case.query,
            top_k=top_k,
            filters={"scenic_id": case.scenic_id},
        )
        result = answerer.answer_with_trace(
            case.query,
            hits,
            min_score=min_score,
            context_limit=context_limit,
            max_context_tokens=max_context_tokens,
        )
        citation_map = {
            item.citation_id: item.source_id
            for item in result.citations
            if item.citation_id is not None
        }
        cited_sources = set(citation_map.values())
        expected_cited = bool(cited_sources & set(case.expected_source_ids))
        expected_source_hits += int(expected_cited)
        marker_ids = {int(value) for value in CITATION_MARKER.findall(result.answer)}
        markers_valid = bool(marker_ids) and marker_ids <= set(citation_map)
        marker_valid_cases += int(markers_valid)
        invalid_claims = sum(
            not claim.citation_ids
            or any(citation_id not in citation_map for citation_id in claim.citation_ids)
            for claim in result.claims
        )
        unsupported_claim_count += invalid_claims
        answered = result.status in {"answered", "requires_live_check"}
        false_refusal_count += int(not answered)
        passed = answered and expected_cited and markers_valid and invalid_claims == 0
        passed_answerable += int(passed)
        case_results.append(
            {
                "case_id": case.case_id,
                "kind": "answerable",
                "query": case.query,
                "status": result.status,
                "expected_source_ids": list(case.expected_source_ids),
                "cited_source_ids": sorted(cited_sources),
                "expected_source_cited": expected_cited,
                "citation_markers_valid": markers_valid,
                "unsupported_claim_count": invalid_claims,
                "decision_reasons": (result.trace or {}).get("decision_reasons", []),
                "rejection_counts": (result.trace or {}).get("rejection_counts", {}),
                "passed": passed,
            }
        )

    for case in refusal_cases:
        hits = retriever.search(
            case.query,
            top_k=top_k,
            filters={"scenic_id": case.scenic_id},
        )
        result = answerer.answer_with_trace(
            case.query,
            hits,
            min_score=min_score,
            context_limit=context_limit,
            max_context_tokens=max_context_tokens,
            requires_live_data=case.requires_live_data,
        )
        passed = (
            result.status == "refused"
            and not result.citations
            and not result.claims
        )
        false_answer_count += int(not passed)
        passed_refusal += int(passed)
        case_results.append(
            {
                "case_id": case.case_id,
                "kind": "refusal",
                "query": case.query,
                "status": result.status,
                "citation_count": len(result.citations),
                "claim_count": len(result.claims),
                "decision_reasons": (result.trace or {}).get("decision_reasons", []),
                "passed": passed,
            }
        )

    answerable_count = len(answerable_cases)
    refusal_count = len(refusal_cases)
    passed_count = passed_answerable + passed_refusal
    total_count = answerable_count + refusal_count
    return {
        "schema_version": "scenicpilot-answer-eval-v1",
        "metrics": {
            "case_count": total_count,
            "answerable_case_count": answerable_count,
            "refusal_case_count": refusal_count,
            "expected_source_citation_recall": round(
                expected_source_hits / answerable_count if answerable_count else 0.0,
                6,
            ),
            "citation_marker_valid_rate": round(
                marker_valid_cases / answerable_count if answerable_count else 0.0,
                6,
            ),
            "refusal_accuracy": round(
                passed_refusal / refusal_count if refusal_count else 0.0,
                6,
            ),
            "unsupported_claim_count": unsupported_claim_count,
            "false_refusal_count": false_refusal_count,
            "false_answer_count": false_answer_count,
            "passed_case_count": passed_count,
            "failed_case_count": total_count - passed_count,
        },
        "cases": case_results,
    }
