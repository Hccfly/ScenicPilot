from __future__ import annotations

from dataclasses import replace

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.agent.validation import ItineraryPlanReviser, ItineraryVerifier
from scenic_pilot.config import Settings
from scenic_pilot.models import (
    Chunk,
    ConstraintViolation,
    ItineraryPlan,
    ItineraryStop,
    SearchHit,
)
from scenic_pilot.runtime import ScenicPilotRuntime


class AlwaysOpenHours:
    def check(self, *, venue: str, date: str | None = None):
        return {
            "status": "ok",
            "provider": "fake-hours",
            "venue": venue,
            "date": date,
            "is_open": True,
            "opens_at": "09:00",
            "closes_at": "18:00",
        }


class OceanAttractionClosedHours(AlwaysOpenHours):
    def check(self, *, venue: str, date: str | None = None):
        result = super().check(venue=venue, date=date)
        if venue == "海洋王国主题资料入口":
            result.update({"status": "closed", "is_open": False})
        return result


def make_hit(source_id: str, title: str, content: str, score: float) -> SearchHit:
    return SearchHit(
        chunk=Chunk(
            chunk_id=f"{source_id}-child",
            source_id=source_id,
            scenic_id="zhuhai-chimelong",
            title=title,
            category="attraction",
            content=content,
            source_url=f"https://example.test/{source_id}",
            authority_level=3,
            verified_at="2026-09-13",
            volatile=False,
            demo=False,
            position=0,
        ),
        score=score,
        keyword_score=score,
        vector_score=score,
        context=content,
    )


def make_plan(*stops: ItineraryStop) -> ItineraryPlan:
    return ItineraryPlan(
        duration_minutes=sum(stop.duration_minutes for stop in stops),
        estimated_cost=0.0,
        stops=list(stops),
    )


def distance_result(
    request_id: str,
    origin: str,
    destination: str,
    duration_minutes: int,
) -> dict:
    return {
        "request_id": request_id,
        "tool": "distance",
        "arguments": {"origin": origin, "destination": destination},
        "output": {
            "status": "ok",
            "provider": "fake-distance",
            "origin": origin,
            "destination": destination,
            "duration_minutes": duration_minutes,
            "distance_meters": 1000,
        },
    }


def test_validator_accounts_for_route_time_and_builds_schedule() -> None:
    verifier = ItineraryVerifier(AlwaysOpenHours())
    plan = make_plan(
        ItineraryStop("海洋王国", "attraction", 120, 0, "evidence", "ocean"),
        ItineraryStop("飞船乐园", "attraction", 120, 0, "evidence", "spaceship"),
    )
    tools = [
        distance_result("r1", "珠海站", "珠海长隆海洋王国", 30),
        distance_result(
            "r2", "珠海长隆海洋王国", "珠海长隆飞船乐园", 30
        ),
    ]

    result = verifier.validate(
        plan,
        {"duration_minutes": 280, "origin": "珠海站", "start_time": "09:30"},
        tool_results=tools,
    )

    assert result.metrics["route_minutes"] == 60
    assert result.metrics["total_duration_minutes"] == 300
    assert any(item.code == "route_time_exceeded" for item in result.violations)
    assert result.plan.stops[0].start_time == "10:00"
    assert result.plan.stops[0].end_time == "12:00"
    assert result.plan.stops[1].start_time == "12:30"


def test_validator_rejects_closed_venue_and_missing_required_preference() -> None:
    class ClosedHours:
        def check(self, *, venue: str, date: str | None = None):
            return {"status": "closed", "is_open": False, "provider": "fake"}

    spaceship = make_hit("spaceship", "飞船乐园", "室内科普", 0.9)
    ocean = make_hit("ocean", "海洋王国", "海洋动物与鲸鲨", 0.8)
    verifier = ItineraryVerifier(ClosedHours())
    plan = make_plan(
        ItineraryStop("飞船乐园", "attraction", 120, 0, "evidence", "spaceship")
    )

    result = verifier.validate(
        plan,
        {"duration_minutes": 240, "required_preferences": ["marine"]},
        hits=[spaceship, ocean],
    )

    codes = {item.code for item in result.violations}
    assert {"closed_venue", "missing_required_preference"} <= codes


def test_validator_rejects_schedule_outside_opening_window() -> None:
    class NarrowHours:
        def check(self, *, venue: str, date: str | None = None):
            return {
                "status": "ok",
                "provider": "fake-hours",
                "is_open": True,
                "opens_at": "10:00",
                "closes_at": "11:00",
            }

    plan = make_plan(
        ItineraryStop("海洋王国", "attraction", 120, 0, "evidence", "ocean")
    )

    result = ItineraryVerifier(NarrowHours()).validate(
        plan,
        {"duration_minutes": 240, "start_time": "09:30"},
    )

    violation = next(
        item for item in result.violations if item.code == "outside_opening_hours"
    )
    assert violation.details["scheduled_start"] == "09:30"
    assert violation.details["scheduled_end"] == "11:30"
    assert violation.details["opens_at"] == "10:00"
    assert violation.details["closes_at"] == "11:00"


def test_reviser_deduplicates_and_adds_evidence_backed_required_stop() -> None:
    spaceship = make_hit("spaceship", "飞船乐园", "室内科普", 0.9)
    ocean = make_hit("ocean", "海洋王国", "海洋动物与鲸鲨", 0.8)
    duplicated = ItineraryStop(
        "飞船乐园", "attraction", 90, 0, "evidence", "spaceship"
    )
    plan = make_plan(duplicated, duplicated)
    violations = [
        ConstraintViolation("duplicate_stop", "duplicate"),
        ConstraintViolation(
            "missing_required_preference",
            "missing marine",
            details={"preferences": ["marine"]},
        ),
    ]

    result = ItineraryPlanReviser().revise(
        plan,
        {
            "duration_minutes": 240,
            "required_preferences": ["marine"],
            "preferences": ["senior_friendly", "marine"],
        },
        violations,
        [spaceship, ocean],
        attempt=1,
    )

    assert result.changed is True
    assert [stop.source_id for stop in result.plan.stops] == ["spaceship", "ocean"]
    assert result.plan.duration_minutes == 180
    assert [item["action"] for item in result.trace["actions"]] == [
        "remove_duplicate",
        "add_required_preference",
    ]


def test_graph_repairs_closed_stop_then_revalidates() -> None:
    settings = Settings(
        tool_mode="offline", llm_provider="offline", planning_max_revisions=2
    )
    dependencies = default_dependencies(settings)
    dependencies = replace(
        dependencies,
        verifier=ItineraryVerifier(OceanAttractionClosedHours()),
    )

    result = ScenicPilotRuntime(dependencies).invoke(
        "一家三口玩一天，预算1000元，怎么安排？"
    )

    assert result["constraint_status"] == "valid"
    assert result["revision_count"] == 1
    assert "revise_plan" in result["trace"]
    assert result["trace"].count("validate") == 2
    assert all(
        stop["name"] != "海洋王国主题资料入口"
        for stop in result["plan"]["stops"]
    )
    assert result["revision_history"][0]["actions"][0]["action"] == (
        "remove_unavailable_stop"
    )


def test_graph_rejects_unfixable_budget_with_bounded_revisions() -> None:
    settings = Settings(
        tool_mode="offline", llm_provider="offline", planning_max_revisions=2
    )

    result = ScenicPilotRuntime(default_dependencies(settings)).invoke(
        "一家三口玩一天，预算50元，怎么安排？"
    )

    assert result["answer_status"] == "refused"
    assert result["constraint_status"] == "rejected"
    assert result["revision_count"] == 2
    assert result["trace"].count("revise_plan") == 2
    assert result["revision_history"][-1]["changed"] is False
    assert "系统已拦截该计划" in result["answer"]
