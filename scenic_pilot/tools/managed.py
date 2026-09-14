from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from scenic_pilot.tools.contracts import (
    BudgetTool,
    DistanceProvider,
    DistanceTool,
    OpeningHoursTool,
    WeatherProvider,
    WeatherTool,
)
from scenic_pilot.tools.resilience import ResilientToolRunner


def _cache_key(tool_name: str, arguments: dict[str, object]) -> str:
    return f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


class ResilientWeatherTool:
    def __init__(
        self,
        runner: ResilientToolRunner,
        *,
        provider: WeatherProvider | None,
        fallback: WeatherTool,
    ) -> None:
        self.runner = runner
        self.provider = provider
        self.fallback = fallback

    def forecast(self, *, location: str, date: str | None = None) -> dict[str, object]:
        arguments = {"location": location, "date": date}
        return self.runner.execute(
            tool_name="weather",
            cache_key=_cache_key("weather", arguments),
            operation=(
                None
                if self.provider is None
                else lambda timeout: self.provider.fetch(
                    location=location,
                    date=date,
                    timeout_seconds=timeout,
                )
            ),
            fallback=lambda: self.fallback.forecast(location=location, date=date),
        )


class ResilientDistanceTool:
    def __init__(
        self,
        runner: ResilientToolRunner,
        *,
        provider: DistanceProvider | None,
        fallback: DistanceTool,
    ) -> None:
        self.runner = runner
        self.provider = provider
        self.fallback = fallback

    def estimate(self, *, origin: str, destination: str) -> dict[str, object]:
        arguments = {"origin": origin, "destination": destination}
        return self.runner.execute(
            tool_name="distance",
            cache_key=_cache_key("distance", arguments),
            operation=(
                None
                if self.provider is None
                else lambda timeout: self.provider.fetch(
                    origin=origin,
                    destination=destination,
                    timeout_seconds=timeout,
                )
            ),
            fallback=lambda: self.fallback.estimate(
                origin=origin, destination=destination
            ),
        )


class ResilientBudgetTool:
    def __init__(self, runner: ResilientToolRunner, calculator: BudgetTool) -> None:
        self.runner = runner
        self.calculator = calculator

    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]:
        arguments = {
            "party_size": party_size,
            "duration_minutes": duration_minutes,
        }
        return self.runner.execute(
            tool_name="budget",
            cache_key=_cache_key("budget", arguments),
            operation=lambda timeout: self.calculator.estimate(
                party_size=party_size,
                duration_minutes=duration_minutes,
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolStack:
    runner: ResilientToolRunner
    weather: WeatherTool
    distance: DistanceTool
    budget: BudgetTool
    opening_hours: OpeningHoursTool
    suite: "ToolSuite"


class ToolSuite:
    """Execute independent tool calls without one failure cancelling the batch."""

    def __init__(
        self,
        *,
        weather: WeatherTool,
        distance: DistanceTool,
        budget: BudgetTool,
        opening_hours: OpeningHoursTool,
    ) -> None:
        self.weather = weather
        self.distance = distance
        self.budget = budget
        self.opening_hours = opening_hours

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, object]:
        if tool_name == "weather":
            return self.weather.forecast(**arguments)
        if tool_name == "distance":
            return self.distance.estimate(**arguments)
        if tool_name == "budget":
            return self.budget.estimate(**arguments)
        if tool_name == "opening_hours":
            return self.opening_hours.check(**arguments)
        return {
            "status": "unavailable",
            "provider": "none",
            "message": f"未知工具：{tool_name}",
            "degradation_reason": "unknown_tool",
        }

    def run(
        self,
        calls: Sequence[dict[str, Any]],
        *,
        max_calls: int | None = None,
    ) -> dict[str, object]:
        results: list[dict[str, object]] = []
        success_count = 0
        degraded_count = 0
        failed_count = 0
        executed_count = 0
        allowed_calls = len(calls) if max_calls is None else max(0, max_calls)
        for index, call in enumerate(calls, 1):
            tool_name = str(call.get("tool", ""))
            arguments = call.get("arguments") or {}
            if executed_count >= allowed_calls:
                output = {
                    "status": "unavailable",
                    "provider": "none",
                    "message": "本轮工具调用预算已耗尽，未执行该调用。",
                    "degradation_reason": "call_budget_exceeded",
                }
            else:
                executed_count += 1
                try:
                    output = self.call(tool_name, dict(arguments))
                except Exception as error:  # isolate programming/provider adapters per call
                    output = {
                        "status": "unavailable",
                        "provider": "none",
                        "message": "工具调用失败，但其他工具仍可继续。",
                        "degradation_reason": "uncaught_tool_exception",
                        "error_type": type(error).__name__,
                    }
            status = str(output.get("status", "unavailable"))
            if status in {"ok", "estimated"}:
                success_count += 1
            elif status in {"degraded", "requires_live_check", "unavailable_offline"}:
                degraded_count += 1
            else:
                failed_count += 1
            results.append(
                {
                    "request_id": str(call.get("request_id", f"tool-{index}")),
                    "tool": tool_name,
                    "arguments": dict(arguments),
                    "output": output,
                }
            )
        return {
            "results": results,
            "summary": {
                "call_count": len(results),
                "success_count": success_count,
                "degraded_count": degraded_count,
                "failed_count": failed_count,
            },
            "governance": {
                "requested_call_count": len(calls),
                "executed_call_count": executed_count,
                "skipped_call_count": len(calls) - executed_count,
                "call_limit": max_calls,
                "budget_exhausted": executed_count < len(calls),
            },
        }
