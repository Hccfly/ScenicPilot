from __future__ import annotations

from scenic_pilot.config import Settings
from scenic_pilot.tools.contracts import ToolPolicy
from scenic_pilot.tools.managed import (
    ResilientBudgetTool,
    ResilientDistanceTool,
    ResilientWeatherTool,
    ToolStack,
    ToolSuite,
)
from scenic_pilot.tools.offline import (
    OfflineBudgetTool,
    OfflineDistanceTool,
    OfflineOpeningHoursTool,
    OfflineWeatherTool,
)
from scenic_pilot.tools.providers import AmapDistanceProvider, OpenMeteoWeatherProvider
from scenic_pilot.tools.resilience import ResilientToolRunner


def build_tool_stack(settings: Settings) -> ToolStack:
    mode = settings.tool_mode.lower()
    if mode not in {"offline", "auto", "live"}:
        raise ValueError("SCENICPILOT_TOOL_MODE must be offline, auto, or live")
    runner = ResilientToolRunner(
        ToolPolicy(
            timeout_seconds=settings.tool_timeout_seconds,
            max_attempts=settings.tool_max_attempts,
            retry_backoff_seconds=settings.tool_retry_backoff_seconds,
            cache_ttl_seconds=settings.tool_cache_ttl_seconds,
            stale_ttl_seconds=settings.tool_stale_ttl_seconds,
            circuit_breaker_threshold=settings.tool_circuit_breaker_threshold,
            circuit_reset_seconds=settings.tool_circuit_reset_seconds,
        )
    )
    online = mode in {"auto", "live"}
    weather_provider = OpenMeteoWeatherProvider() if online else None
    distance_provider = (
        AmapDistanceProvider(settings.amap_api_key)
        if online and settings.amap_api_key
        else None
    )
    weather = ResilientWeatherTool(
        runner,
        provider=weather_provider,
        fallback=OfflineWeatherTool(),
    )
    distance = ResilientDistanceTool(
        runner,
        provider=distance_provider,
        fallback=OfflineDistanceTool(),
    )
    budget = ResilientBudgetTool(runner, OfflineBudgetTool())
    opening_hours = OfflineOpeningHoursTool()
    suite = ToolSuite(
        weather=weather,
        distance=distance,
        budget=budget,
        opening_hours=opening_hours,
    )
    return ToolStack(
        runner=runner,
        weather=weather,
        distance=distance,
        budget=budget,
        opening_hours=opening_hours,
        suite=suite,
    )
