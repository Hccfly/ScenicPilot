from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.agent.services import ItineraryToolEnricher
from scenic_pilot.config import Settings
from scenic_pilot.runtime import ScenicPilotRuntime
from scenic_pilot.tools.contracts import ToolPolicy, ToolProviderError
from scenic_pilot.tools.factory import build_tool_stack
from scenic_pilot.tools.managed import ResilientBudgetTool, ToolSuite
from scenic_pilot.tools.offline import (
    OfflineBudgetTool,
    OfflineDistanceTool,
    OfflineOpeningHoursTool,
)
from scenic_pilot.tools.providers import (
    AmapDistanceProvider,
    AmapWeatherProvider,
    OpenMeteoWeatherProvider,
)
from scenic_pilot.tools.resilience import ResilientToolRunner


@dataclass
class FakeClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def runner(
    clock: FakeClock | None = None,
    *,
    max_attempts: int = 2,
    cache_ttl: float = 10.0,
    stale_ttl: float = 100.0,
    circuit_threshold: int = 3,
) -> ResilientToolRunner:
    selected_clock = clock or FakeClock()
    return ResilientToolRunner(
        ToolPolicy(
            timeout_seconds=1.5,
            max_attempts=max_attempts,
            retry_backoff_seconds=0,
            cache_ttl_seconds=cache_ttl,
            stale_ttl_seconds=stale_ttl,
            circuit_breaker_threshold=circuit_threshold,
            circuit_reset_seconds=30,
        ),
        clock=selected_clock,
        sleep=lambda _: None,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )


def test_runner_caches_successful_result() -> None:
    calls = 0
    resilient = runner()

    def operation(timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert timeout == 1.5
        return {"status": "ok", "provider": "fake", "value": 42}

    first = resilient.execute(
        tool_name="weather", cache_key="same", operation=operation
    )
    second = resilient.execute(
        tool_name="weather", cache_key="same", operation=operation
    )

    assert calls == 1
    assert first["tool_trace"]["cache_status"] == "miss"
    assert second["tool_trace"]["cache_status"] == "hit"
    assert second["tool_trace"]["attempts"] == 0
    assert len(resilient.audit_events) == 2


def test_runner_retries_retryable_error_then_recovers() -> None:
    calls = 0
    resilient = runner()

    def operation(_: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolProviderError("temporary", "temporary failure")
        return {"status": "ok", "provider": "fake"}

    result = resilient.execute(
        tool_name="distance", cache_key="route", operation=operation
    )

    assert result["status"] == "ok"
    assert result["tool_trace"]["attempts"] == 2
    assert result["tool_trace"]["errors"][0]["code"] == "temporary"


def test_non_retryable_error_uses_explicit_fallback() -> None:
    resilient = runner()

    def operation(_: float) -> dict[str, object]:
        raise ToolProviderError("invalid_input", "bad input", retryable=False)

    result = resilient.execute(
        tool_name="weather",
        cache_key="bad",
        operation=operation,
        fallback=lambda: {"status": "unavailable_offline", "provider": "offline"},
    )

    assert result["status"] == "degraded"
    assert result["fallback_status"] == "unavailable_offline"
    assert result["degradation_reason"] == "invalid_input"
    assert result["tool_trace"]["attempts"] == 1


def test_stale_cache_is_served_when_provider_later_fails() -> None:
    clock = FakeClock()
    resilient = runner(clock, cache_ttl=10, stale_ttl=100, max_attempts=1)
    should_fail = False

    def operation(_: float) -> dict[str, object]:
        if should_fail:
            raise ToolProviderError("network_error", "offline")
        return {"status": "ok", "provider": "fake", "value": "fresh"}

    resilient.execute(tool_name="weather", cache_key="zhuhai", operation=operation)
    clock.advance(11)
    should_fail = True
    result = resilient.execute(
        tool_name="weather", cache_key="zhuhai", operation=operation
    )

    assert result["status"] == "degraded"
    assert result["stale"] is True
    assert result["value"] == "fresh"
    assert result["tool_trace"]["cache_status"] == "stale"


def test_circuit_breaker_skips_provider_after_threshold() -> None:
    calls = 0
    resilient = runner(max_attempts=1, circuit_threshold=2)

    def operation(_: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ToolProviderError("provider_5xx", "down")

    fallback = lambda: {"status": "unavailable_offline", "provider": "offline"}
    resilient.execute(
        tool_name="weather", cache_key="one", operation=operation, fallback=fallback
    )
    second = resilient.execute(
        tool_name="weather", cache_key="two", operation=operation, fallback=fallback
    )
    third = resilient.execute(
        tool_name="weather", cache_key="three", operation=operation, fallback=fallback
    )

    assert calls == 2
    assert second["tool_trace"]["circuit_state"] == "open"
    assert third["degradation_reason"] == "circuit_open"
    assert third["tool_trace"]["attempts"] == 0


def test_open_meteo_provider_maps_geocoding_and_daily_forecast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            params = parse_qs(request.url.query.decode())
            assert params["name"] == ["Zhuhai"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "珠海",
                            "admin1": "山东",
                            "latitude": 35.8712,
                            "longitude": 119.9963,
                        },
                        {
                            "name": "珠海市",
                            "admin1": "广东",
                            "latitude": 22.2769,
                            "longitude": 113.5678,
                        },
                    ]
                },
            )
        assert request.url.host == "api.open-meteo.com"
        return httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-09-12"],
                    "weather_code": [61],
                    "temperature_2m_max": [30.5],
                    "temperature_2m_min": [25.1],
                    "precipitation_probability_max": [65],
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenMeteoWeatherProvider(
        client,
        today=lambda: date(2026, 9, 12),
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    result = provider.fetch(
        location="珠海长隆度假区", date=None, timeout_seconds=2
    )

    assert result["status"] == "ok"
    assert result["resolved_location"] == "珠海市"
    assert result["latitude"] == 22.2769
    assert result["weather"] == "小雨"
    assert result["precipitation_probability_max"] == 65


def test_amap_provider_geocodes_and_maps_first_driving_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/geocode/geo":
            params = parse_qs(request.url.query.decode())
            location = "113.550000,22.220000"
            if params["address"] == ["珠海长隆度假区"]:
                location = "113.540000,22.100000"
            return httpx.Response(
                200,
                json={"status": "1", "geocodes": [{"location": location}]},
            )
        assert request.url.path == "/v5/direction/driving"
        return httpx.Response(
            200,
            json={
                "status": "1",
                "route": {
                    "taxi_cost": "45",
                    "paths": [{"distance": "12000", "cost": {"duration": "1800"}}],
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AmapDistanceProvider(
        "secret-key",
        client,
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    result = provider.fetch(
        origin="珠海站", destination="珠海长隆度假区", timeout_seconds=2
    )

    assert result["status"] == "ok"
    assert result["distance_meters"] == 12000
    assert result["duration_minutes"] == 30
    assert result["taxi_cost_cny"] == "45"
    assert "secret-key" not in str(result)


def test_amap_weather_provider_maps_zhuhai_daily_forecast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "restapi.amap.com"
        assert request.url.path == "/v3/weather/weatherInfo"
        params = parse_qs(request.url.query.decode())
        assert params["city"] == ["440400"]
        assert params["extensions"] == ["all"]
        return httpx.Response(
            200,
            json={
                "status": "1",
                "forecasts": [
                    {
                        "city": "珠海市",
                        "casts": [
                            {
                                "date": "2026-09-12",
                                "dayweather": "阴",
                                "daytemp": "30",
                                "nighttemp": "25",
                            }
                        ],
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AmapWeatherProvider(
        "secret-key",
        client,
        today=lambda: date(2026, 9, 12),
        now=lambda: datetime(2026, 9, 12, tzinfo=UTC),
    )

    result = provider.fetch(
        location="珠海长隆海洋王国", date="2026-09-12", timeout_seconds=2
    )

    assert result["status"] == "ok"
    assert result["provider"] == "amap-weather"
    assert result["location"] == "珠海长隆海洋王国"
    assert result["resolved_location"] == "珠海市"
    assert result["weather"] == "阴"
    assert result["temperature_max_c"] == 30.0
    assert result["temperature_min_c"] == 25.0
    assert result["weather_code"] is None
    assert result["precipitation_probability_max"] is None
    assert "secret-key" not in str(result)


def test_amap_weather_provider_rejects_date_outside_forecast_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "1",
                "forecasts": [
                    {
                        "city": "珠海市",
                        "casts": [
                            {
                                "date": "2026-09-12",
                                "dayweather": "晴",
                                "daytemp": "30",
                                "nighttemp": "25",
                            }
                        ],
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AmapWeatherProvider("secret-key", client)

    with pytest.raises(ToolProviderError) as error:
        provider.fetch(location="珠海", date="2026-10-01", timeout_seconds=2)

    assert error.value.code == "forecast_date_unavailable"
    assert error.value.retryable is False


class ExplodingWeatherTool:
    def forecast(self, *, location: str, date: str | None = None) -> dict[str, object]:
        raise RuntimeError("injected failure")


class ExplodingBudgetCalculator:
    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]:
        raise TimeoutError("injected budget timeout")


def test_tool_suite_isolates_one_failure_and_continues_batch() -> None:
    suite = ToolSuite(
        weather=ExplodingWeatherTool(),
        distance=OfflineDistanceTool(),
        budget=OfflineBudgetTool(),
        opening_hours=OfflineOpeningHoursTool(),
    )

    result = suite.run(
        [
            {"request_id": "w", "tool": "weather", "arguments": {"location": "珠海"}},
            {
                "request_id": "b",
                "tool": "budget",
                "arguments": {"party_size": 3, "duration_minutes": 480},
            },
        ]
    )

    assert result["summary"] == {
        "call_count": 2,
        "success_count": 1,
        "degraded_count": 0,
        "failed_count": 1,
    }
    assert result["results"][0]["output"]["degradation_reason"] == (
        "uncaught_tool_exception"
    )
    assert result["results"][1]["output"]["estimated_cost"] == 300.0


def test_offline_factory_never_requires_provider_configuration() -> None:
    stack = build_tool_stack(Settings(tool_mode="offline"))

    result = stack.suite.run(
        [
            {"tool": "weather", "arguments": {"location": "珠海长隆"}},
            {
                "tool": "distance",
                "arguments": {"origin": "珠海站", "destination": "珠海长隆"},
            },
            {
                "tool": "budget",
                "arguments": {"party_size": 2, "duration_minutes": 480},
            },
        ]
    )

    assert result["summary"]["call_count"] == 3
    assert result["summary"]["degraded_count"] == 2
    assert result["summary"]["success_count"] == 1
    weather = result["results"][0]["output"]
    assert weather["degradation_reason"] == "provider_not_configured"
    assert weather["tool_trace"]["circuit_state"] == "disabled"


def test_itinerary_session_survives_budget_tool_failure_and_explains_it() -> None:
    failing_budget = ResilientBudgetTool(
        runner(max_attempts=1), ExplodingBudgetCalculator()
    )
    dependencies = default_dependencies(Settings(tool_mode="offline"))
    dependencies = replace(
        dependencies,
        budget_tool=failing_budget,
        tool_enricher=ItineraryToolEnricher(
            ToolSuite(
                weather=dependencies.weather_tool,
                distance=dependencies.distance_tool,
                budget=failing_budget,
                opening_hours=dependencies.opening_hours_tool,
            )
        ),
    )

    result = ScenicPilotRuntime(dependencies).invoke(
        "一家三口玩一天，预算 1000 元，怎么安排？"
    )

    assert result["answer_status"] == "requires_live_check"
    assert result["plan"]["stops"]
    assert result["plan"]["estimated_cost"] == 0.0
    assert "budget:timeout" in result["answer"]
