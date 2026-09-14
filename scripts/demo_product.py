from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import jieba

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.runtime import ScenicPilotRuntime


SCENARIO_PATH = PROJECT_ROOT / "data" / "demo" / "product_scenarios.json"
KNOWLEDGE_PATH = PROJECT_ROOT / "data" / "demo" / "zhuhai_chimelong.jsonl"


def _settings() -> Settings:
    """Build an explicit offline profile; .env and private keys are not read."""
    return Settings(
        scenic_id="zhuhai-chimelong",
        data_path=KNOWLEDGE_PATH,
        retrieval_index_path=None,
        tool_mode="offline",
        llm_provider="offline",
        approval_mode="on_request",
    )


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    citations = result.get("citations") or []
    plan = result.get("plan") or {}
    return {
        "intent": result.get("intent"),
        "answer_status": result.get("answer_status"),
        "answer": result.get("answer"),
        "approval_status": result.get("approval_status"),
        "constraint_status": result.get("constraint_status"),
        "citations": [
            {
                "source_id": item.get("source_id"),
                "title": item.get("title"),
                "url": item.get("url"),
            }
            for item in citations
        ],
        "plan": plan or None,
        "violations": result.get("violations") or [],
        "tool_call_count": result.get("tool_call_count", 0),
        "revision_count": result.get("revision_count", 0),
        "trace": result.get("trace") or [],
        "governance_trace": result.get("governance_trace") or {},
    }


def _evaluate_invoke(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    with TemporaryDirectory(prefix=f"scenicpilot-{case['case_id']}-") as temporary:
        runtime = ScenicPilotRuntime(
            default_dependencies(_settings()),
            checkpoint_path=Path(temporary) / "runtime.sqlite3",
        )
        try:
            result = runtime.invoke(
                case["query"],
                thread_id=f"{case['case_id']}-thread",
                idempotency_key=f"{case['case_id']}-start",
            )
        finally:
            runtime.close()

    citations = result.get("citations") or []
    trace = result.get("trace") or []
    checks = {
        "intent_matches": result.get("intent") == expected.get("intent"),
    }
    if "minimum_citations" in expected:
        checks["minimum_citations"] = len(citations) >= expected["minimum_citations"]
    if "maximum_citations" in expected:
        checks["maximum_citations"] = len(citations) <= expected["maximum_citations"]
    if "answer_status" in expected:
        checks["answer_status"] = result.get("answer_status") == expected["answer_status"]
    if "terminal_trace" in expected:
        checks["terminal_trace"] = bool(trace) and trace[-1] == expected["terminal_trace"]
    if "tool_names" in expected:
        checks["tool_names"] = [
            item.get("tool") for item in result.get("tool_results") or []
        ] == expected["tool_names"]
    return {"checks": checks, "result": _summary(result)}


def _evaluate_hitl(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    with TemporaryDirectory(prefix=f"scenicpilot-{case['case_id']}-") as temporary:
        checkpoint_path = Path(temporary) / "runtime.sqlite3"
        dependencies = default_dependencies(_settings())
        first = ScenicPilotRuntime(dependencies, checkpoint_path=checkpoint_path)
        paused = first.start(
            case["query"],
            thread_id=f"{case['case_id']}-thread",
            idempotency_key=f"{case['case_id']}-start",
            require_approval=True,
        )
        tools_before_approval = (paused.get("result") or {}).get("tool_call_count", 0)
        first.close()

        restored = ScenicPilotRuntime(dependencies, checkpoint_path=checkpoint_path)
        try:
            recovered = restored.get_session(f"{case['case_id']}-thread")
            completed = restored.resume(
                thread_id=f"{case['case_id']}-thread",
                approved=True,
                reason="offline product demonstration",
                idempotency_key=f"{case['case_id']}-resume",
            )
            replayed = restored.resume(
                thread_id=f"{case['case_id']}-thread",
                approved=True,
                reason="offline product demonstration",
                idempotency_key=f"{case['case_id']}-resume",
            )
        finally:
            restored.close()

    result = completed.get("result") or {}
    stops = (result.get("plan") or {}).get("stops") or []
    checks = {
        "paused_before_tools": paused.get("status") == expected["paused_status"]
        and tools_before_approval == 0,
        "restart_recovers_interrupt": recovered.get("status")
        == expected["paused_status"],
        "approval_completes": completed.get("status") == "completed"
        and result.get("approval_status") == expected["approval_status"],
        "intent_matches": result.get("intent") == expected["intent"],
        "minimum_stops": len(stops) >= expected["minimum_stops"],
        "idempotency_replays": replayed.get("idempotency_replayed") is True
        and replayed.get("result") == completed.get("result"),
    }
    return {
        "checks": checks,
        "lifecycle": {
            "paused_status": paused.get("status"),
            "tools_before_approval": tools_before_approval,
            "recovered_status": recovered.get("status"),
            "completed_status": completed.get("status"),
            "replay_detected": replayed.get("idempotency_replayed"),
        },
        "result": _summary(result),
    }


def run_demo(selected_case: str | None = None) -> dict[str, Any]:
    catalog = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    scenarios = [
        item
        for item in catalog["scenarios"]
        if selected_case is None or item["case_id"] == selected_case
    ]
    if not scenarios:
        raise ValueError(f"unknown demo case: {selected_case}")

    cases: list[dict[str, Any]] = []
    for scenario in scenarios:
        try:
            evaluated = (
                _evaluate_hitl(scenario)
                if scenario["flow"] == "approve_after_restart"
                else _evaluate_invoke(scenario)
            )
            checks = evaluated["checks"]
            cases.append(
                {
                    "case_id": scenario["case_id"],
                    "title": scenario["title"],
                    "query": scenario["query"],
                    "passed": all(checks.values()),
                    **evaluated,
                }
            )
        except Exception as error:  # keep the report actionable per scenario
            cases.append(
                {
                    "case_id": scenario["case_id"],
                    "title": scenario["title"],
                    "query": scenario["query"],
                    "passed": False,
                    "checks": {"execution_completed": False},
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                }
            )

    passed = sum(bool(item["passed"]) for item in cases)
    return {
        "schema_version": "scenicpilot-product-demo-report-v1",
        "mode": "offline",
        "uses_private_keys": False,
        "scenic_id": catalog["scenic_id"],
        "notice": catalog["notice"],
        "case_count": len(cases),
        "passed_count": passed,
        "all_passed": passed == len(cases),
        "cases": cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline ScenicPilot product demo.")
    parser.add_argument("--case-id", help="Run one product demo scenario.")
    parser.add_argument("--output", type=Path, help="Optionally save the JSON report.")
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("jieba").setLevel(logging.ERROR)
    jieba.setLogLevel(logging.ERROR)
    args = parse_args()
    try:
        report = run_demo(args.case_id)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        destination = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output + "\n", encoding="utf-8")
    print(output)
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
