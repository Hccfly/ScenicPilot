from __future__ import annotations

from dataclasses import replace

import pytest

from scenic_pilot.agent.dependencies import RuntimeDependencies, default_dependencies
from scenic_pilot.agent.services import ItineraryToolEnricher
from scenic_pilot.agent.validation import ItineraryVerifier
from scenic_pilot.config import Settings
from scenic_pilot.persistence import IdempotencyConflict
from scenic_pilot.runtime import ScenicPilotRuntime
from scenic_pilot.tools.managed import ToolSuite


class CountingWeather:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def forecast(self, **arguments):
        self.calls += 1
        return self.delegate.forecast(**arguments)


class CountingDistance:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def estimate(self, **arguments):
        self.calls += 1
        return self.delegate.estimate(**arguments)


class CountingBudget:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def estimate(self, **arguments):
        self.calls += 1
        return self.delegate.estimate(**arguments)


class CountingOpeningHours:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def check(self, **arguments):
        self.calls += 1
        return self.delegate.check(**arguments)


def governed_dependencies(
    *, tool_call_limit: int = 24
) -> tuple[RuntimeDependencies, dict[str, object]]:
    settings = Settings(
        tool_mode="offline",
        llm_provider="offline",
        execution_tool_call_limit=tool_call_limit,
    )
    base = default_dependencies(settings)
    tools = {
        "weather": CountingWeather(base.weather_tool),
        "distance": CountingDistance(base.distance_tool),
        "budget": CountingBudget(base.budget_tool),
        "opening_hours": CountingOpeningHours(base.opening_hours_tool),
    }
    suite = ToolSuite(
        weather=tools["weather"],
        distance=tools["distance"],
        budget=tools["budget"],
        opening_hours=tools["opening_hours"],
    )
    dependencies = replace(
        base,
        weather_tool=tools["weather"],
        distance_tool=tools["distance"],
        budget_tool=tools["budget"],
        opening_hours_tool=tools["opening_hours"],
        tool_suite=suite,
        tool_enricher=ItineraryToolEnricher(suite),
        verifier=ItineraryVerifier(tools["opening_hours"]),
    )
    return dependencies, tools


def call_count(tools: dict[str, object]) -> int:
    return sum(int(getattr(tool, "calls")) for tool in tools.values())


def test_hitl_pauses_before_tools_and_resumes_after_process_restart(tmp_path) -> None:
    dependencies, tools = governed_dependencies()
    checkpoint_path = tmp_path / "runtime.sqlite3"
    first_runtime = ScenicPilotRuntime(
        dependencies,
        checkpoint_path=checkpoint_path,
    )

    paused = first_runtime.start(
        "一家三口玩一天，预算 1000 元，怎么安排？",
        thread_id="restart-thread",
        idempotency_key="start-1",
        require_approval=True,
    )

    assert paused["status"] == "awaiting_approval"
    assert paused["interrupts"][0]["action"] == "execute_itinerary_tools"
    assert call_count(tools) == 0
    first_runtime.close()

    second_runtime = ScenicPilotRuntime(
        dependencies,
        checkpoint_path=checkpoint_path,
    )
    recovered = second_runtime.get_session("restart-thread")
    assert recovered["status"] == "awaiting_approval"
    replayed_start = second_runtime.start(
        "一家三口玩一天，预算 1000 元，怎么安排？",
        thread_id="restart-thread",
        idempotency_key="start-1",
        require_approval=True,
    )
    assert replayed_start["status"] == "awaiting_approval"
    assert replayed_start["idempotency_replayed"] is True
    assert call_count(tools) == 0

    completed = second_runtime.resume(
        thread_id="restart-thread",
        approved=True,
        reason="继续查询",
        idempotency_key="resume-1",
    )

    assert completed["status"] == "completed"
    assert completed["result"]["approval_status"] == "approved"
    assert completed["result"]["trace"][3] == "approval_gate"
    assert call_count(tools) > 0
    calls_after_resume = call_count(tools)
    replayed_resume = second_runtime.resume(
        thread_id="restart-thread",
        approved=True,
        reason="继续查询",
        idempotency_key="resume-1",
    )
    assert replayed_resume["idempotency_replayed"] is True
    assert replayed_resume["result"] == completed["result"]
    assert call_count(tools) == calls_after_resume
    assert second_runtime.get_session("restart-thread")["status"] == "completed"
    second_runtime.close()


def test_hitl_rejection_terminates_without_tool_side_effects() -> None:
    dependencies, tools = governed_dependencies()
    runtime = ScenicPilotRuntime(dependencies)
    paused = runtime.start(
        "带老人玩一天，从珠海站出发",
        thread_id="reject-thread",
        idempotency_key="start-reject",
        require_approval=True,
    )

    rejected = runtime.resume(
        thread_id=paused["thread_id"],
        approved=False,
        reason="暂不调用外部服务",
        idempotency_key="resume-reject",
    )

    assert rejected["status"] == "completed"
    assert rejected["result"]["answer_status"] == "refused"
    assert rejected["result"]["approval_status"] == "rejected"
    assert rejected["result"]["trace"][-1] == "approval_rejected"
    assert rejected["result"]["governance_trace"]["tool_calls_used"] == 0
    assert call_count(tools) == 0


def test_idempotency_replays_result_without_repeating_tools() -> None:
    dependencies, tools = governed_dependencies()
    runtime = ScenicPilotRuntime(dependencies)
    arguments = {
        "thread_id": "idempotent-thread",
        "idempotency_key": "same-start",
    }

    first = runtime.start("一家三口玩一天，预算 1000 元", **arguments)
    calls_after_first = call_count(tools)
    replay = runtime.start("一家三口玩一天，预算 1000 元", **arguments)

    assert first["status"] == "completed"
    assert replay["idempotency_replayed"] is True
    assert replay["result"] == first["result"]
    assert call_count(tools) == calls_after_first

    with pytest.raises(IdempotencyConflict):
        runtime.start("换成半天行程", **arguments)


def test_tool_call_budget_skips_calls_and_marks_unverified_constraints() -> None:
    dependencies, tools = governed_dependencies(tool_call_limit=1)
    result = ScenicPilotRuntime(dependencies).invoke(
        "一家三口玩一天，预算 1000 元，怎么安排？"
    )

    assert result["tool_call_count"] == 1
    assert call_count(tools) == 1
    assert result["governance_trace"]["tool_calls_used"] == 1
    assert any(
        item["output"].get("degradation_reason") == "call_budget_exceeded"
        for item in result["tool_results"]
    )
    assert any(
        violation["code"] == "budget_unverified"
        for violation in result["violations"]
    )
    assert result["answer_status"] == "requires_live_check"


def test_new_request_on_same_thread_resets_turn_trace() -> None:
    dependencies, _ = governed_dependencies()
    runtime = ScenicPilotRuntime(dependencies)
    runtime.start(
        "珠海长隆有哪些园区主题？",
        thread_id="multi-turn",
        idempotency_key="turn-1",
    )

    second = runtime.start(
        "一家三口玩半天怎么安排？",
        thread_id="multi-turn",
        idempotency_key="turn-2",
    )

    assert second["result"]["trace"][0] == "classify"
    assert second["result"]["trace"].count("classify") == 1
    assert second["result"]["answer_status"] != "pending"
