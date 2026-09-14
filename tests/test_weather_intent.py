from __future__ import annotations

from dataclasses import replace

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.agent.services import IntentClassifier
from scenic_pilot.config import Settings
from scenic_pilot.runtime import ScenicPilotRuntime
from scenic_pilot.tools.managed import ToolSuite


class RecordingWeather:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def forecast(self, *, location: str, date: str | None = None):
        self.calls.append({"location": location, "date": date})
        return dict(self.output, location=location, date=date)


class ForbiddenDistance:
    def __init__(self) -> None:
        self.calls = 0

    def estimate(self, **kwargs):
        self.calls += 1
        raise AssertionError("distance must not run for a weather intent")


class ForbiddenBudget:
    def __init__(self) -> None:
        self.calls = 0

    def estimate(self, **kwargs):
        self.calls += 1
        raise AssertionError("budget must not run for a weather intent")


class ForbiddenOpeningHours:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, **kwargs):
        self.calls += 1
        raise AssertionError("opening hours must not run for a weather intent")


def weather_runtime(
    output: dict[str, object], *, tool_call_limit: int = 24
) -> tuple[ScenicPilotRuntime, RecordingWeather, list[object]]:
    settings = Settings(
        tool_mode="offline",
        llm_provider="offline",
        execution_tool_call_limit=tool_call_limit,
    )
    base = default_dependencies(settings)
    weather = RecordingWeather(output)
    forbidden: list[object] = [
        ForbiddenDistance(),
        ForbiddenBudget(),
        ForbiddenOpeningHours(),
    ]
    suite = ToolSuite(
        weather=weather,
        distance=forbidden[0],
        budget=forbidden[1],
        opening_hours=forbidden[2],
    )
    dependencies = replace(
        base,
        weather_tool=weather,
        distance_tool=forbidden[0],
        budget_tool=forbidden[1],
        opening_hours_tool=forbidden[2],
        tool_suite=suite,
    )
    return ScenicPilotRuntime(dependencies), weather, forbidden


def test_classifier_separates_pure_weather_from_mixed_itinerary() -> None:
    classifier = IntentClassifier()

    assert classifier.classify("明天珠海天气怎么样？") == "weather"
    assert classifier.classify("珠海长隆气温和降雨概率是多少？") == "weather"
    assert (
        classifier.classify("帮我安排明天珠海长隆一日游，并根据天气调整")
        == "itinerary"
    )
    assert classifier.classify("酒店住客权益有哪些？") == "faq"


def test_weather_node_calls_only_weather_and_skips_hitl_and_retrieval() -> None:
    runtime, weather, forbidden = weather_runtime(
        {
            "status": "ok",
            "provider": "fake-weather",
            "source_url": "https://weather.example.test/forecast",
            "observed_at": "2026-09-14T06:00:00+00:00",
            "resolved_location": "横琴",
            "weather": "晴",
            "temperature_min_c": 25,
            "temperature_max_c": 31,
            "precipitation_probability_max": 20,
        }
    )
    try:
        envelope = runtime.start(
            "2026年9月15日横琴天气怎么样？",
            require_approval=True,
            thread_id="weather-live",
            idempotency_key="weather-live-start",
        )
    finally:
        runtime.close()

    result = envelope["result"]
    assert envelope["status"] == "completed"
    assert envelope["interrupts"] == []
    assert result["intent"] == "weather"
    assert result["trace"] == ["classify", "weather_answer"]
    assert result["retrieval_trace"]["skipped"] is True
    assert result["approval_status"] == "not_required"
    assert result["plan"] is None
    assert result["tool_call_count"] == 1
    assert [item["tool"] for item in result["tool_results"]] == ["weather"]
    assert weather.calls == [{"location": "珠海横琴", "date": "2026-09-15"}]
    assert all(getattr(tool, "calls") == 0 for tool in forbidden)
    assert "横琴天气预报（2026-09-15）" in result["answer"]
    assert "25～31℃" in result["answer"]
    assert "[1]" in result["answer"]
    assert result["answer_status"] == "requires_live_check"
    assert result["citations"][0]["source_type"] == "weather_api"
    assert result["claims"][0]["citation_ids"] == [1]
    assert result["answer_trace"]["citation_marker_coverage"] == 1.0
    assert result["governance_trace"]["tool_calls_used"] == 1


def test_weather_node_reports_degradation_without_inventing_forecast() -> None:
    runtime, weather, forbidden = weather_runtime(
        {
            "status": "unavailable_offline",
            "provider": "offline",
            "message": "离线模式未查询实时天气，请在出发前核验。",
        }
    )
    try:
        result = runtime.invoke("明天珠海天气怎么样？")
    finally:
        runtime.close()

    assert len(weather.calls) == 1
    assert all(getattr(tool, "calls") == 0 for tool in forbidden)
    assert result["answer_status"] == "requires_live_check"
    assert "无法取得" in result["answer"]
    assert "离线模式" in result["answer"]
    assert "℃" not in result["answer"]
    assert result["citations"] == []
    assert result["claims"] == []
    assert "weather_tool_degraded" in result["answer_trace"]["decision_reasons"]


def test_weather_node_honors_zero_tool_call_budget() -> None:
    runtime, weather, forbidden = weather_runtime(
        {"status": "ok", "provider": "must-not-run", "weather": "晴"},
        tool_call_limit=0,
    )
    try:
        result = runtime.invoke("明天珠海天气怎么样？")
    finally:
        runtime.close()

    assert weather.calls == []
    assert all(getattr(tool, "calls") == 0 for tool in forbidden)
    assert result["tool_call_count"] == 0
    assert result["tool_results"][0]["output"]["degradation_reason"] == (
        "call_budget_exceeded"
    )
    assert result["governance_trace"]["events"][-1]["executed"] == 0
    assert result["governance_trace"]["events"][-1]["skipped"] == 1
