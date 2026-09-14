from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.runtime import ScenicPilotRuntime


@dataclass(frozen=True, slots=True)
class PlanningCase:
    case_id: str
    query: str
    expected_source_id: str
    maximum_duration_minutes: int
    requires_route: bool = False


CASES = (
    PlanningCase(
        "family-day",
        "一家三口玩一天，预算1000元，怎么安排？",
        "demo-ocean",
        480,
    ),
    PlanningCase(
        "indoor-half-day",
        "雨天想看室内科普内容，半天怎么玩？",
        "demo-spaceship",
        240,
    ),
    PlanningCase(
        "marine-half-day",
        "想看海洋动物，半天怎么玩？",
        "demo-ocean",
        240,
    ),
    PlanningCase(
        "senior-day-with-origin",
        "带老人轻松玩一天，从珠海站出发",
        "demo-ocean",
        480,
        requires_route=True,
    ),
)


def evaluate_case(runtime: ScenicPilotRuntime, case: PlanningCase) -> dict[str, Any]:
    result = runtime.invoke(case.query)
    plan = result.get("plan") or {}
    stops = plan.get("stops") or []
    selected_ids = [stop.get("source_id") for stop in stops]
    retrieved_ids = {
        hit.get("chunk", {}).get("source_id") for hit in result.get("hits", [])
    }
    tools = [item.get("tool") for item in result.get("tool_results", [])]
    checks = {
        "structured_plan": bool(stops)
        and all(
            {"name", "category", "duration_minutes", "reason", "source_id"}
            <= set(stop)
            for stop in stops
        ),
        "duration_respected": int(plan.get("duration_minutes", 0))
        <= case.maximum_duration_minutes,
        "source_grounded": all(source_id in retrieved_ids for source_id in selected_ids),
        "preference_hit": case.expected_source_id in selected_ids,
        "tools_enriched": {"weather", "budget"} <= set(tools),
        "route_enriched": ("distance" in tools) if case.requires_route else True,
        "graph_complete": result.get("trace")
        == [
            "classify",
            "retrieve",
            "generate_candidates",
            "approval_gate",
            "enrich_tools",
            "validate",
            "finalize",
        ],
        "planning_trace_complete": {
            "candidate_generation",
            "tool_enrichment",
            "validation",
            "output",
        }
        <= set(result.get("planning_trace", {})),
    }
    return {
        "case": asdict(case),
        "passed": all(checks.values()),
        "checks": checks,
        "selected_source_ids": selected_ids,
        "duration_minutes": plan.get("duration_minutes"),
        "answer_status": result.get("answer_status"),
        "tool_summary": result.get("planning_trace", {}).get(
            "tool_enrichment", {}
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate 行程编排阶段 itinerary graph.")
    parser.add_argument(
        "--tool-mode", choices=("offline", "auto", "live"), default="offline"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "planning_eval.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = replace(
        Settings.from_env(),
        tool_mode=args.tool_mode,
        llm_provider="offline",
        llm_model=None,
    )
    runtime = ScenicPilotRuntime(default_dependencies(settings))
    cases = [evaluate_case(runtime, case) for case in CASES]
    report = {
        "schema_version": "scenicpilot-planning-eval-v1",
        "tool_mode": args.tool_mode,
        "case_count": len(cases),
        "passed_count": sum(case["passed"] for case in cases),
        "pass_rate": sum(case["passed"] for case in cases) / len(cases),
        "cases": cases,
    }
    output = args.output
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
