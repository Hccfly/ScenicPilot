from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.agent.services import (
    ItineraryCandidateGenerator,
    ItineraryToolEnricher,
)
from scenic_pilot.agent.validation import ItineraryVerifier
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.runtime import ScenicPilotRuntime
from scenic_pilot.tools.managed import ToolSuite


class AlwaysOpenHours:
    def check(self, *, venue: str, date: str | None = None) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "constraint-eval-hours",
            "venue": venue,
            "date": date,
            "is_open": True,
            "opens_at": "09:00",
            "closes_at": "20:00",
        }


class OceanClosedHours(AlwaysOpenHours):
    def check(self, *, venue: str, date: str | None = None) -> dict[str, object]:
        result = super().check(venue=venue, date=date)
        if venue == "海洋王国主题资料入口":
            result.update({"status": "closed", "is_open": False})
        return result


class LongRouteTool:
    def estimate(self, *, origin: str, destination: str) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "constraint-eval-distance",
            "origin": origin,
            "destination": destination,
            "distance_meters": 60_000,
            "duration_minutes": 180,
        }


class WrongPreferenceModel:
    def select_candidates(self, **kwargs: Any) -> dict[str, object]:
        return {
            "status": "ok",
            "provider": "constraint-eval-model",
            "model": "deterministic-fake",
            "selected_source_ids": ["demo-spaceship"],
            "rationale": "故意遗漏 marine，用于验证修订。",
        }


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    result: dict[str, Any]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": all(self.checks.values()),
            "checks": self.checks,
            "answer_status": self.result.get("answer_status"),
            "constraint_status": self.result.get("constraint_status"),
            "revision_count": self.result.get("revision_count"),
            "trace": self.result.get("trace"),
            "revision_history": self.result.get("revision_history"),
            "violation_codes": [
                item.get("code") for item in self.result.get("violations", [])
            ],
        }


def runtime_with(
    *,
    hours: object | None = None,
    distance: object | None = None,
    model: object | None = None,
) -> ScenicPilotRuntime:
    settings = Settings(
        tool_mode="offline",
        llm_provider="offline",
        planning_max_revisions=2,
    )
    dependencies = default_dependencies(settings)
    selected_hours = hours or dependencies.opening_hours_tool
    updates: dict[str, Any] = {
        "verifier": ItineraryVerifier(selected_hours),
    }
    if model is not None:
        updates["candidate_generator"] = ItineraryCandidateGenerator(model)
        updates["model_client"] = model
    if distance is not None:
        suite = ToolSuite(
            weather=dependencies.weather_tool,
            distance=distance,
            budget=dependencies.budget_tool,
            opening_hours=selected_hours,
        )
        updates.update(
            {
                "distance_tool": distance,
                "tool_suite": suite,
                "tool_enricher": ItineraryToolEnricher(suite),
            }
        )
    return ScenicPilotRuntime(replace(dependencies, **updates))


def evaluate() -> list[EvaluationCase]:
    normal = runtime_with().invoke("一家三口玩一天，预算1000元，怎么安排？")
    budget = runtime_with().invoke("一家三口玩一天，预算50元，怎么安排？")
    closed = runtime_with(hours=OceanClosedHours()).invoke(
        "一家三口玩一天，预算1000元，怎么安排？"
    )
    route = runtime_with(
        hours=AlwaysOpenHours(), distance=LongRouteTool()
    ).invoke("从珠海站出发，一家三口玩一天，预算1000元，怎么安排？")
    preference = runtime_with(
        hours=AlwaysOpenHours(), model=WrongPreferenceModel()
    ).invoke("半天一定要看海洋动物，怎么安排？")

    return [
        EvaluationCase(
            "warnings-do-not-loop",
            normal,
            {
                "valid": normal.get("constraint_status") == "valid",
                "no_revision": normal.get("revision_count") == 0,
                "requires_live_check": normal.get("answer_status")
                == "requires_live_check",
            },
        ),
        EvaluationCase(
            "unfixable-budget-is-rejected",
            budget,
            {
                "rejected": budget.get("constraint_status") == "rejected",
                "bounded": budget.get("revision_count") == 2,
                "refused": budget.get("answer_status") == "refused",
            },
        ),
        EvaluationCase(
            "closed-stop-is-removed",
            closed,
            {
                "revalidated": closed.get("constraint_status") == "valid",
                "revised_once": closed.get("revision_count") == 1,
                "closed_removed": all(
                    stop.get("name") != "海洋王国主题资料入口"
                    for stop in (closed.get("plan") or {}).get("stops", [])
                ),
            },
        ),
        EvaluationCase(
            "route-overrun-is-trimmed",
            route,
            {
                "revalidated": route.get("constraint_status") == "valid",
                "revised": int(route.get("revision_count", 0)) >= 1,
                "time_action": any(
                    action.get("action") == "remove_for_time_limit"
                    for revision in route.get("revision_history", [])
                    for action in revision.get("actions", [])
                ),
            },
        ),
        EvaluationCase(
            "hard-preference-is-restored",
            preference,
            {
                "revalidated": preference.get("constraint_status") == "valid",
                "bounded_revisions": 1
                <= int(preference.get("revision_count", 0))
                <= 2,
                "marine_added": "demo-ocean"
                in {
                    stop.get("source_id")
                    for stop in (preference.get("plan") or {}).get("stops", [])
                },
            },
        ),
    ]


def main() -> None:
    cases = [case.to_dict() for case in evaluate()]
    report = {
        "schema_version": "scenicpilot-constraint-eval-v1",
        "case_count": len(cases),
        "passed_count": sum(case["passed"] for case in cases),
        "pass_rate": sum(case["passed"] for case in cases) / len(cases),
        "cases": cases,
    }
    output = (
        PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "constraint_eval.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
