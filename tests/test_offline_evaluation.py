from __future__ import annotations

from collections import Counter

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.evaluation import EVALUATION_GROUP_COUNTS, OfflineEvaluator, load_case_catalog
from scenic_pilot.evaluation.offline import (
    AssertionResult,
    EvaluationCaseResult,
)


def test_offline_catalog_has_unique_grouped_cases() -> None:
    cases = load_case_catalog(PROJECT_ROOT / "data" / "eval" / "offline_cases.jsonl")

    assert len(cases) == 115
    assert len({case.case_id for case in cases}) == 115
    assert dict(Counter(case.group for case in cases)) == EVALUATION_GROUP_COUNTS


def test_offline_report_localizes_failed_assertion_to_case() -> None:
    result = EvaluationCaseResult(
        case_id="example-case",
        group="fault_injection",
        description="example",
        assertions=(
            AssertionResult("healthy_boundary", True, True, True),
            AssertionResult("broken_boundary", False, "expected", "actual"),
        ),
        observation={"field": "value"},
    ).to_dict()

    assert result["passed"] is False
    assert result["failure_reasons"] == ["broken_boundary"]
    assert result["assertions"][1]["actual"] == "actual"


def test_offline_agent_boundaries_are_offline_isolated_and_structured() -> None:
    report = OfflineEvaluator().evaluate(
        case_ids=(
            "eval-fault-004",
            "eval-recovery-003",
            "eval-budget-001",
            "eval-loop-002",
        )
    )

    assert report["summary"]["all_passed"] is True
    assert report["summary"]["executed_case_count"] == 4
    assert report["execution"]["network_calls_allowed"] is False
    assert report["execution"]["tool_mode"] == "offline"
    assert len({case["observation"]["isolation_key"] for case in report["cases"]}) == 4
    assert all(case["assertions"] for case in report["cases"])
