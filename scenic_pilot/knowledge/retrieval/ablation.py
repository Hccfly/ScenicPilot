from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any

from scenic_pilot.knowledge.chunking import chunk_documents
from scenic_pilot.models import SourceDocument

from .contracts import EmbeddingProvider, RetrievalConfig
from .evaluation import RetrievalCase, evaluate_retriever
from .service import HybridRetriever


@dataclass(frozen=True, slots=True)
class ChunkingCandidate:
    child_size: int
    child_overlap: int
    parent_size: int

    def validate(self) -> None:
        if self.child_size <= 0 or self.parent_size <= 0:
            raise ValueError("chunk sizes must be positive")
        if self.child_overlap < 0 or self.child_overlap >= self.child_size:
            raise ValueError("child_overlap must be between zero and child_size")
        if self.parent_size < self.child_size:
            raise ValueError("parent_size must be greater than or equal to child_size")

    @property
    def candidate_id(self) -> str:
        return f"c{self.child_size}-o{self.child_overlap}-p{self.parent_size}"


DEFAULT_CHILD_GRID = (
    (160, 0),
    (160, 24),
    (160, 32),
    (240, 24),
    (240, 32),
    (240, 48),
    (320, 32),
    (320, 48),
    (320, 64),
    (480, 48),
    (480, 64),
    (480, 96),
    (640, 64),
    (640, 96),
    (640, 128),
)
DEFAULT_PARENT_GRID = (640, 960, 1200, 1600, 2400)
DEFAULT_GATES = {
    "recall@5": 0.95,
    "mrr@10": 0.80,
    "evidence_completeness": 1.0,
    "cross_scenic_leakage": 0,
    "demo_leakage": 0,
    "forbidden_hit_count": 0,
    "max_context_budget_exhausted_case_count": 1,
}


def _metric(metrics: dict[str, Any], name: str, default: float = 0.0) -> float:
    value = metrics.get(name)
    return float(value) if value is not None else default


def _compact_cases(report: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "case_id",
        "query",
        "returned_source_ids",
        "relevant_ranks",
        "recall@5",
        "reciprocal_rank@10",
        "child_evidence_recall@5",
        "evidence_completeness",
        "leading_noise_rate@5",
        "parent_noise_upper_bound",
        "estimated_context_tokens",
        "child_span_coverage",
        "parent_span_coverage",
        "raw_parent_duplicate_rate",
    )
    return [{key: case.get(key) for key in keys} for case in report["cases"]]


def evaluate_chunking_candidate(
    documents: Iterable[SourceDocument],
    cases: list[RetrievalCase],
    *,
    embedding: EmbeddingProvider,
    candidate: ChunkingCandidate,
    retrieval_config: RetrievalConfig | None = None,
    strategy_version: str = "heading-parent-child-v2",
) -> dict[str, Any]:
    candidate.validate()
    chunks = chunk_documents(
        documents,
        chunk_size=candidate.child_size,
        overlap=candidate.child_overlap,
        strategy_version=strategy_version,
        parent_size=candidate.parent_size,
    )
    retriever = HybridRetriever(
        chunks,
        embedding=embedding,
        config=retrieval_config,
    )
    report = evaluate_retriever(retriever, cases)
    child_chunks = [chunk for chunk in chunks if chunk.level != "parent"]
    indexed_chunks = retriever.indexed_chunks
    parent_chunks = [chunk for chunk in chunks if chunk.level == "parent"]
    return {
        "candidate_id": candidate.candidate_id,
        "strategy_version": strategy_version,
        "parameters": asdict(candidate),
        "corpus": {
            "chunk_count": len(chunks),
            "child_chunk_count": len(child_chunks),
            "indexed_child_count": len(indexed_chunks),
            "excluded_non_substantive_child_count": retriever.excluded_non_substantive_chunks,
            "parent_chunk_count": len(parent_chunks),
            "indexed_child_characters": sum(len(chunk.content) for chunk in indexed_chunks),
        },
        "metrics": report["metrics"],
        "cases": _compact_cases(report),
    }


def _passes_child_gates(run: dict[str, Any], gates: dict[str, float]) -> bool:
    metrics = run["metrics"]
    return (
        _metric(metrics, "recall@5") >= gates["recall@5"]
        and _metric(metrics, "mrr@10") >= gates["mrr@10"]
        and _metric(metrics, "cross_scenic_leakage") == 0
        and _metric(metrics, "demo_leakage") == 0
        and _metric(metrics, "forbidden_hit_count") == 0
    )


def _passes_final_gates(run: dict[str, Any], gates: dict[str, float]) -> bool:
    return _passes_child_gates(run, gates) and (
        _metric(run["metrics"], "evidence_completeness")
        >= gates["evidence_completeness"]
        and _metric(run["metrics"], "context_budget_exhausted_case_count")
        <= gates["max_context_budget_exhausted_case_count"]
    )


def _child_sort_key(run: dict[str, Any], gates: dict[str, float]) -> tuple[Any, ...]:
    metrics = run["metrics"]
    return (
        -int(_passes_child_gates(run, gates)),
        -_metric(metrics, "recall@5"),
        -_metric(metrics, "child_evidence_recall@5"),
        -_metric(metrics, "mrr@10"),
        _metric(metrics, "leading_noise_rate@5", 1.0),
        _metric(metrics, "raw_parent_duplicate_rate", 1.0),
        run["corpus"]["indexed_child_characters"],
        run["corpus"]["child_chunk_count"],
    )


def _final_sort_key(run: dict[str, Any], gates: dict[str, float]) -> tuple[Any, ...]:
    metrics = run["metrics"]
    passes = _passes_final_gates(run, gates)
    if passes:
        return (
            0,
            _metric(metrics, "average_estimated_context_tokens", float("inf")),
            _metric(metrics, "parent_noise_upper_bound", 1.0),
            _metric(metrics, "raw_parent_duplicate_rate", 1.0),
            -_metric(metrics, "mrr@10"),
        )
    return (
        1,
        -_metric(metrics, "evidence_completeness"),
        -_metric(metrics, "recall@5"),
        -_metric(metrics, "child_evidence_recall@5"),
        -_metric(metrics, "mrr@10"),
        _metric(metrics, "average_estimated_context_tokens", float("inf")),
    )


def _select_stage1_finalists(
    runs: list[dict[str, Any]],
    gates: dict[str, float],
    limit: int,
) -> list[dict[str, Any]]:
    """Keep quality, direct-evidence, and efficiency representatives.

    Picking the top N with a single scalar would overfit one end of the child
    size range. This shortlist deliberately carries distinct child sizes into
    the parent sweep, while every representative still has to meet source-level
    quality and leakage gates when possible.
    """

    ordered = sorted(runs, key=lambda item: _child_sort_key(item, gates))
    eligible = [item for item in ordered if _passes_child_gates(item, gates)] or ordered
    best_by_child_size: dict[int, dict[str, Any]] = {}
    for item in eligible:
        best_by_child_size.setdefault(int(item["parameters"]["child_size"]), item)
    representatives = list(best_by_child_size.values())
    selectors = (
        lambda item: (
            -_metric(item["metrics"], "mrr@10"),
            -_metric(item["metrics"], "child_evidence_recall@5"),
            _metric(item["metrics"], "raw_parent_duplicate_rate", 1.0),
        ),
        lambda item: (
            -_metric(item["metrics"], "child_evidence_recall@5"),
            -_metric(item["metrics"], "mrr@10"),
            _metric(item["metrics"], "raw_parent_duplicate_rate", 1.0),
        ),
        lambda item: (
            _metric(item["metrics"], "raw_parent_duplicate_rate", 1.0),
            item["corpus"]["indexed_child_characters"],
            -_metric(item["metrics"], "mrr@10"),
        ),
    )
    selected: list[dict[str, Any]] = []
    selected_sizes: set[int] = set()
    for selector in selectors:
        for item in sorted(representatives, key=selector):
            child_size = int(item["parameters"]["child_size"])
            if child_size not in selected_sizes:
                selected.append(item)
                selected_sizes.add(child_size)
                break
        if len(selected) >= limit:
            return selected
    for item in ordered:
        child_size = int(item["parameters"]["child_size"])
        if child_size not in selected_sizes:
            selected.append(item)
            selected_sizes.add(child_size)
        if len(selected) >= limit:
            break
    return selected


def _select_final_recommendation(
    runs: list[dict[str, Any]],
    gates: dict[str, float],
    *,
    token_tolerance: float = 0.05,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    passing = [item for item in runs if _passes_final_gates(item, gates)]
    if not passing:
        return sorted(runs, key=lambda item: _final_sort_key(item, gates))[0], []
    maximum_evidence = max(
        _metric(item["metrics"], "evidence_completeness") for item in passing
    )
    best_evidence = [
        item
        for item in passing
        if _metric(item["metrics"], "evidence_completeness") == maximum_evidence
    ]
    minimum_tokens = min(
        _metric(item["metrics"], "average_estimated_context_tokens", float("inf"))
        for item in best_evidence
    )
    near_minimum = [
        item
        for item in best_evidence
        if _metric(item["metrics"], "average_estimated_context_tokens", float("inf"))
        <= minimum_tokens * (1 + token_tolerance)
    ]
    selected = min(
        near_minimum,
        key=lambda item: (
            _metric(item["metrics"], "raw_parent_duplicate_rate", 1.0),
            item["corpus"]["indexed_child_characters"],
            -_metric(item["metrics"], "mrr@10"),
            int(item["parameters"]["child_overlap"]),
        ),
    )
    return selected, passing


def run_chunk_ablation(
    documents: list[SourceDocument],
    cases: list[RetrievalCase],
    *,
    embedding: EmbeddingProvider,
    retrieval_config: RetrievalConfig | None = None,
    child_grid: tuple[tuple[int, int], ...] = DEFAULT_CHILD_GRID,
    parent_grid: tuple[int, ...] = DEFAULT_PARENT_GRID,
    fixed_parent_size: int = 1600,
    stage1_top_n: int = 4,
    gates: dict[str, float] | None = None,
    strategy_version: str = "heading-parent-child-v2",
    on_result: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    selected_gates = dict(DEFAULT_GATES)
    if gates is not None:
        selected_gates.update(gates)
    evaluated: dict[str, dict[str, Any]] = {}

    def run(stage: str, candidate: ChunkingCandidate) -> dict[str, Any]:
        cached = evaluated.get(candidate.candidate_id)
        if cached is None:
            cached = evaluate_chunking_candidate(
                documents,
                cases,
                embedding=embedding,
                candidate=candidate,
                retrieval_config=retrieval_config,
                strategy_version=strategy_version,
            )
            evaluated[candidate.candidate_id] = cached
        if on_result is not None:
            on_result(stage, cached)
        return cached

    stage1_runs = [
        run("child", ChunkingCandidate(child, overlap, fixed_parent_size))
        for child, overlap in child_grid
    ]
    ranked_stage1 = sorted(
        stage1_runs,
        key=lambda item: _child_sort_key(item, selected_gates),
    )
    finalists = _select_stage1_finalists(
        stage1_runs,
        selected_gates,
        stage1_top_n,
    )

    stage2_runs: list[dict[str, Any]] = []
    for finalist in finalists:
        child = int(finalist["parameters"]["child_size"])
        overlap = int(finalist["parameters"]["child_overlap"])
        for parent in parent_grid:
            if parent >= child:
                stage2_runs.append(
                    run("parent", ChunkingCandidate(child, overlap, parent))
                )

    ranked_stage2 = sorted(
        stage2_runs,
        key=lambda item: _final_sort_key(item, selected_gates),
    )
    recommendation, passing = _select_final_recommendation(
        stage2_runs,
        selected_gates,
    )
    return {
        "schema_version": "scenicpilot-chunk-ablation-v1",
        "strategy_version": strategy_version,
        "method": {
            "stage1": "fix parent; retain distinct quality, direct-evidence, and efficiency representatives that pass source-level gates",
            "stage2": "apply source ranking, parent evidence, and leakage gates; retain the highest achievable evidence completeness, then among configurations within 5% of minimum context tokens minimize pre-dedup parent duplication and index cost",
            "evidence_scope": "one human-audited minimally decisive source passage per query; not exhaustive answer-fact coverage",
            "child_evidence_metric": "diagnostic only: a title child may correctly route to a complete parent, so direct child-span coverage is not a hard gate",
            "token_metric": "deterministic Unicode estimate; not model billing tokens",
            "parent_noise_metric": "conservative upper bound: one minus annotated evidence characters divided by expected-source context characters",
        },
        "gates": selected_gates,
        "stage1": {
            "fixed_parent_size": fixed_parent_size,
            "candidate_count": len(stage1_runs),
            "finalist_ids": [item["candidate_id"] for item in finalists],
            "runs": ranked_stage1,
        },
        "stage2": {
            "parent_grid": list(parent_grid),
            "candidate_count": len(stage2_runs),
            "passing_candidate_count": len(passing),
            "runs": ranked_stage2,
        },
        "recommendation": {
            "candidate_id": recommendation["candidate_id"],
            "parameters": recommendation["parameters"],
            "metrics": recommendation["metrics"],
            "passes_all_gates": _passes_final_gates(recommendation, selected_gates),
            "selection_pool": "passing" if passing else "best available; no candidate passed every gate",
        },
    }


def reselect_chunk_ablation(
    report: dict[str, Any],
    *,
    gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Reapply decision gates to existing measurements without re-embedding."""

    selected_gates = dict(DEFAULT_GATES)
    if gates is not None:
        selected_gates.update(gates)
    runs = list(report["stage2"]["runs"])
    recommendation, passing = _select_final_recommendation(runs, selected_gates)
    report["gates"] = selected_gates
    report["stage2"]["passing_candidate_count"] = len(passing)
    report["recommendation"] = {
        "candidate_id": recommendation["candidate_id"],
        "parameters": recommendation["parameters"],
        "metrics": recommendation["metrics"],
        "passes_all_gates": _passes_final_gates(recommendation, selected_gates),
        "selection_pool": "passing" if passing else "best available; no candidate passed every gate",
    }
    return report
