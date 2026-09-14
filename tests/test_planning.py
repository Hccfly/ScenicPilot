from __future__ import annotations

from datetime import date

from scenic_pilot.agent.services import (
    IntentClassifier,
    ItineraryCandidateGenerator,
    ItineraryToolEnricher,
)
from scenic_pilot.llm import DisabledItineraryModelClient
from scenic_pilot.models import Chunk, ItineraryPlan, ItineraryStop, SearchHit
from scenic_pilot.tools.managed import ToolSuite
from scenic_pilot.tools.offline import OfflineOpeningHoursTool


def hit(
    source_id: str,
    title: str,
    category: str,
    content: str,
    score: float,
) -> SearchHit:
    return SearchHit(
        chunk=Chunk(
            chunk_id=f"{source_id}-child",
            source_id=source_id,
            scenic_id="zhuhai-chimelong",
            title=title,
            category=category,
            content=content,
            source_url=f"https://example.test/{source_id}",
            authority_level=3,
            verified_at="2026-09-12",
            volatile=False,
            demo=False,
            position=0,
        ),
        score=score,
        keyword_score=score,
        vector_score=score,
        context=content,
    )


class FakeSelectionClient:
    def select_candidates(self, **kwargs):
        return {
            "status": "ok",
            "provider": "fake",
            "model": "fake-model",
            "selected_source_ids": ["invented-place", "spaceship"],
            "rationale": "test",
        }


class FakeWeatherTool:
    def forecast(self, *, location: str, date: str | None = None):
        return {
            "status": "ok",
            "provider": "fake-weather",
            "location": location,
            "date": date,
            "weather": "晴",
            "temperature_min_c": 24,
            "temperature_max_c": 30,
            "precipitation_probability_max": 10,
        }


class FakeDistanceTool:
    def estimate(self, *, origin: str, destination: str):
        return {
            "status": "ok",
            "provider": "fake-distance",
            "origin": origin,
            "destination": destination,
            "distance_meters": 1000,
            "duration_minutes": 10,
        }


class FakeBudgetTool:
    def estimate(self, *, party_size: int, duration_minutes: int):
        return {
            "status": "estimated",
            "provider": "fake-budget",
            "estimated_cost": party_size * 100.0,
            "duration_minutes": duration_minutes,
        }


def candidates() -> list[SearchHit]:
    return [
        hit("ocean", "海洋王国", "attraction", "海洋动物与室外项目", 0.9),
        hit("spaceship", "飞船乐园", "attraction", "室内科普体验", 0.6),
        hit("food", "园区餐饮", "dining", "用餐与休息", 0.5),
    ]


def test_classifier_extracts_date_origin_and_multiple_preferences() -> None:
    classifier = IntentClassifier(today=lambda: date(2026, 9, 13))

    constraints = classifier.extract_constraints(
        "明天从珠海站出发，带老人和孩子雨天轻松玩半天，预算800元"
    )

    assert constraints["visit_date"] == "2026-09-14"
    assert constraints["origin"] == "珠海站"
    assert constraints["duration_minutes"] == 240
    assert constraints["party_size"] == 1
    assert constraints["budget"] == 800.0
    assert constraints["preferences"] == [
        "family",
        "senior_friendly",
        "indoor_science",
    ]


def test_classifier_extracts_hard_content_preferences_and_start_time() -> None:
    constraints = IntentClassifier().extract_constraints(
        "明天 10:15 开始，必须看海洋动物，也一定要有室内科普，玩一天"
    )

    assert constraints["start_time"] == "10:15"
    assert constraints["required_preferences"] == ["indoor_science", "marine"]


def test_half_day_plan_respects_time_and_prefers_indoor_science() -> None:
    generator = ItineraryCandidateGenerator(DisabledItineraryModelClient())

    result = generator.generate(
        "雨天室内科普半天",
        candidates(),
        {"duration_minutes": 240, "preferences": ["indoor_science"]},
    )

    assert result.plan.duration_minutes <= 240
    assert len(result.plan.stops) <= 2
    assert result.plan.stops[0].source_id == "spaceship"
    assert result.trace["selection_mode"] == "rule_fallback"


def test_model_selection_cannot_inject_unknown_source_id() -> None:
    generator = ItineraryCandidateGenerator(FakeSelectionClient())

    result = generator.generate(
        "半天怎么玩", candidates(), {"duration_minutes": 240}
    )

    assert [stop.source_id for stop in result.plan.stops] == ["spaceship"]
    assert result.trace["selection_mode"] == "model_ranked"
    assert result.trace["rejected_model_source_ids"] == ["invented-place"]


def test_senior_plan_uses_shorter_attraction_blocks() -> None:
    generator = ItineraryCandidateGenerator(DisabledItineraryModelClient())

    result = generator.generate(
        "带老人轻松玩一天",
        candidates(),
        {"duration_minutes": 480, "preferences": ["senior_friendly"]},
    )

    attraction_durations = [
        stop.duration_minutes
        for stop in result.plan.stops
        if stop.category == "attraction"
    ]
    assert attraction_durations
    assert set(attraction_durations) == {90}


def test_tool_enrichment_adds_weather_budget_and_route_legs() -> None:
    suite = ToolSuite(
        weather=FakeWeatherTool(),
        distance=FakeDistanceTool(),
        budget=FakeBudgetTool(),
        opening_hours=OfflineOpeningHoursTool(),
    )
    enricher = ItineraryToolEnricher(suite)
    plan = ItineraryPlan(
        duration_minutes=240,
        estimated_cost=0,
        stops=[
            ItineraryStop("海洋王国", "attraction", 120, 0, "evidence", "ocean"),
            ItineraryStop(
                "飞船乐园", "attraction", 120, 0, "evidence", "spaceship"
            ),
        ],
    )

    result = enricher.enrich(
        plan,
        {
            "party_size": 3,
            "visit_date": "2026-09-14",
            "origin": "珠海站",
        },
    )

    assert result.plan.estimated_cost == 300.0
    assert [item["tool"] for item in result.tool_results] == [
        "weather",
        "budget",
        "distance",
        "distance",
    ]
    assert result.trace["route_leg_count"] == 2
    assert result.trace["failed_count"] == 0
