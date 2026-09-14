from __future__ import annotations

from scenic_pilot.knowledge.retrieval.ablation import (
    reselect_chunk_ablation,
    run_chunk_ablation,
)
from scenic_pilot.knowledge.retrieval.evaluation import EvidenceSpan, RetrievalCase
from scenic_pilot.models import SourceDocument

from test_retrieval import FakeEmbedding


def test_two_stage_ablation_returns_an_auditable_recommendation() -> None:
    document = SourceDocument(
        source_id="whale",
        scenic_id="zhuhai-chimelong",
        title="鲸鲨馆",
        category="attraction",
        content="## 鲸鲨馆\n\n鲸鲨馆完整介绍，这里还能看到大型魔鬼鱼。",
        source_url="https://example.test/whale",
        authority_level=3,
    )
    case = RetrievalCase(
        case_id="case-1",
        query="鲸鲨馆有什么",
        scenic_id="zhuhai-chimelong",
        expected_source_ids=("whale",),
        gold_evidence_spans=(
            EvidenceSpan(source_id="whale", text="这里还能看到大型魔鬼鱼"),
        ),
    )

    report = run_chunk_ablation(
        [document],
        [case],
        embedding=FakeEmbedding(),
        child_grid=((24, 0), (32, 4)),
        parent_grid=(32, 64),
        fixed_parent_size=64,
        stage1_top_n=1,
        gates={
            "recall@5": 1.0,
            "mrr@10": 1.0,
            "evidence_completeness": 1.0,
            "cross_scenic_leakage": 0,
            "demo_leakage": 0,
            "forbidden_hit_count": 0,
        },
    )

    assert report["schema_version"] == "scenicpilot-chunk-ablation-v1"
    assert report["stage1"]["candidate_count"] == 2
    assert report["stage2"]["candidate_count"] >= 1
    assert report["recommendation"]["candidate_id"]
    assert "average_estimated_context_tokens" in report["recommendation"]["metrics"]


def test_reselection_rejects_contexts_that_frequently_exhaust_budget() -> None:
    safe = {
        "candidate_id": "safe",
        "parameters": {"child_size": 32, "child_overlap": 4, "parent_size": 64},
        "corpus": {"indexed_child_characters": 100},
        "metrics": {
            "recall@5": 1.0,
            "mrr@10": 1.0,
            "evidence_completeness": 1.0,
            "cross_scenic_leakage": 0,
            "demo_leakage": 0,
            "forbidden_hit_count": 0,
            "context_budget_exhausted_case_count": 0,
            "average_estimated_context_tokens": 100,
            "raw_parent_duplicate_rate": 0.1,
        },
    }
    misleading = {
        **safe,
        "candidate_id": "misleading",
        "metrics": {
            **safe["metrics"],
            "context_budget_exhausted_case_count": 10,
            "average_estimated_context_tokens": 10,
        },
    }
    report = {
        "stage2": {"runs": [misleading, safe]},
        "recommendation": {},
    }

    selected = reselect_chunk_ablation(report)

    assert selected["recommendation"]["candidate_id"] == "safe"
