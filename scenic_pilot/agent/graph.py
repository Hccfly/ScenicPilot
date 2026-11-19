from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from scenic_pilot.agent.dependencies import RuntimeDependencies
from scenic_pilot.agent.state import AgentState
from scenic_pilot.models import ConstraintViolation, ItineraryPlan, ItineraryStop, SearchHit


def _hydrate_hits(values: list[dict[str, Any]]) -> list[SearchHit]:
    return [SearchHit.from_dict(value) for value in values]


def _hydrate_plan(value: dict[str, Any]) -> ItineraryPlan:
    return ItineraryPlan(
        duration_minutes=value["duration_minutes"],
        estimated_cost=value["estimated_cost"],
        stops=[ItineraryStop(**stop) for stop in value.get("stops", [])],
        assumptions=list(value.get("assumptions", [])),
    )


def _planning_trace(
    state: AgentState, stage: str, payload: dict[str, Any]
) -> dict[str, Any]:
    trace = dict(state.get("planning_trace", {}))
    trace.setdefault("schema_version", "scenicpilot-planning-trace-v1")
    trace[stage] = payload
    return trace


def _append_trace_round(
    state: AgentState,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    trace = dict(state.get("planning_trace", {}))
    trace.setdefault("schema_version", "scenicpilot-planning-trace-v1")
    section = dict(trace.get(stage, {}))
    rounds = list(section.get("rounds", []))
    rounds.append(payload)
    section.update(
        {
            key: value
            for key, value in payload.items()
            if key not in {"violations", "opening_hours_results"}
        }
    )
    section["rounds"] = rounds
    trace[stage] = section
    return trace


def _join_messages(messages: list[str]) -> str:
    return "；".join(message.rstrip("。；") for message in messages) + "。"


def _next_trace(state: AgentState, node: str) -> list[str]:
    return [*state.get("trace", []), node]


def _governance_trace(
    state: AgentState,
    event: dict[str, Any],
) -> dict[str, Any]:
    trace = dict(state.get("governance_trace", {}))
    events = list(trace.get("events", []))
    events.append(event)
    trace.update(
        {
            "schema_version": "scenicpilot-governance-v1",
            "tool_call_limit": int(state.get("tool_call_limit", 0)),
            "events": events,
        }
    )
    return trace


def build_graph(dependencies: RuntimeDependencies, *, checkpointer: Any = None):
    def classify(state: AgentState) -> dict[str, Any]:
        approval_required = bool(state.get("approval_required", False))
        return {
            "intent": dependencies.classifier.classify(state["query"]),
            "constraints": dependencies.classifier.extract_constraints(
                state["query"]
            ),
            "constraint_status": "not_applicable",
            "revision_count": 0,
            "max_revisions": dependencies.settings.planning_max_revisions,
            "revision_changed": False,
            "revision_history": [],
            "candidate_plan": None,
            "plan": None,
            "answer": "",
            "answer_status": "pending",
            "tool_results": [],
            "planning_trace": {},
            "violations": [],
            "citations": [],
            "claims": [],
            "conflicts": [],
            "answer_trace": {},
            "approval_status": "pending" if approval_required else "not_required",
            "approval_decision": None,
            "tool_call_count": 0,
            "tool_call_limit": dependencies.settings.execution_tool_call_limit,
            "governance_trace": {
                "schema_version": "scenicpilot-governance-v1",
                "tool_call_limit": dependencies.settings.execution_tool_call_limit,
                "events": [],
            },
            "trace": _next_trace(state, "classify"),
        }

    def retrieve(state: AgentState) -> dict[str, Any]:
        response = dependencies.index.search_with_trace(
            state["query"],
            top_k=dependencies.settings.top_k,
            filters={"scenic_id": state["scenic_id"]},
        )
        return {
            "hits": [hit.to_dict() for hit in response.hits],
            "retrieval_trace": response.trace,
            "trace": _next_trace(state, "retrieve"),
        }

    def route_after_classify(state: AgentState) -> Literal["weather_answer", "retrieve"]:
        return "weather_answer" if state["intent"] == "weather" else "retrieve"

    def route_after_retrieval(
        state: AgentState,
    ) -> Literal["grounded_answer", "generate_candidates"]:
        return (
            "generate_candidates"
            if state["intent"] == "itinerary"
            else "grounded_answer"
        )

    def weather_answer(state: AgentState) -> dict[str, Any]:
        used_before = int(state.get("tool_call_count", 0))
        call_limit = int(state.get("tool_call_limit", 0))
        remaining = max(0, call_limit - used_before)
        constraints = state.get("constraints", {})
        location = str(
            constraints.get("weather_location") or "珠海长隆度假区"
        )
        requested_date = constraints.get("visit_date")
        batch = dependencies.tool_suite.run(
            [
                {
                    "request_id": "weather-1",
                    "tool": "weather",
                    "arguments": {
                        "location": location,
                        "date": requested_date,
                    },
                }
            ],
            max_calls=remaining,
        )
        tool_results = list(batch["results"])
        output = dict(tool_results[0]["output"])
        governance = dict(batch.get("governance", {}))
        used_now = int(governance.get("executed_call_count", 0))
        total_used = used_before + used_now
        tool_status = str(output.get("status", "unavailable"))
        provider = str(output.get("provider", "none"))
        resolved_location = str(
            output.get("resolved_location") or output.get("location") or location
        )
        weather_date = str(output.get("date") or requested_date or "当前日期")

        usable = tool_status == "ok" and any(
            output.get(field) is not None
            for field in (
                "weather",
                "temperature_min_c",
                "temperature_max_c",
                "precipitation_probability_max",
            )
        )
        citations: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        if usable:
            details: list[str] = []
            if output.get("weather") is not None:
                details.append(str(output["weather"]))
            minimum = output.get("temperature_min_c")
            maximum = output.get("temperature_max_c")
            if minimum is not None and maximum is not None:
                details.append(f"{minimum}～{maximum}℃")
            elif minimum is not None:
                details.append(f"最低温 {minimum}℃")
            elif maximum is not None:
                details.append(f"最高温 {maximum}℃")
            rain = output.get("precipitation_probability_max")
            if rain is not None:
                details.append(f"最大降水概率 {rain}%")
            claim = f"{resolved_location}天气预报（{weather_date}）：" + "，".join(details)
            source_url = str(output.get("source_url") or "").strip()
            if source_url:
                citations.append(
                    {
                        "source_id": f"weather-{provider}",
                        "title": f"{provider} 天气数据",
                        "url": source_url,
                        "verified_at": output.get("observed_at"),
                        "volatile": True,
                        "citation_id": 1,
                        "authority_level": 2,
                        "status": "live",
                        "retrieval_policy": "live_required",
                        "source_type": "weather_api",
                        "refresh_days": 0,
                        "freshness": "live",
                    }
                )
                claims.append(
                    {
                        "claim_id": "weather-claim-1",
                        "text": claim,
                        "citation_ids": [1],
                        "source_ids": [f"weather-{provider}"],
                        "volatile": True,
                    }
                )
            answer = claim + (" [1]" if citations else "")
            answer += "\n\n时效提示：天气预报会更新，出发前请再次核验。"
            decision_reasons = ["live_weather_tool_result", "volatile_forecast"]
        else:
            message = str(output.get("message") or "实时天气服务暂不可用。").strip()
            answer = f"当前无法取得 {resolved_location} 在 {weather_date} 的实时天气。{message}"
            decision_reasons = [
                "weather_tool_degraded",
                str(output.get("degradation_reason") or tool_status),
            ]

        governance_trace = _governance_trace(
            state,
            {
                "event": "weather_tool_executed",
                "node": "weather_answer",
                "requested": int(governance.get("requested_call_count", 1)),
                "executed": used_now,
                "skipped": int(governance.get("skipped_call_count", 0)),
                "status": tool_status,
                "used_total": total_used,
                "remaining": max(0, call_limit - total_used),
            },
        )
        governance_trace["tool_calls_used"] = total_used
        return {
            "hits": [],
            "retrieval_trace": {
                "schema_version": "scenicpilot-retrieval-v1",
                "skipped": True,
                "reason": "weather_intent_uses_live_tool",
            },
            "answer": answer,
            "answer_status": "requires_live_check",
            "citations": citations,
            "claims": claims,
            "conflicts": [],
            "answer_trace": {
                "schema_version": "scenicpilot-answer-trace-v1",
                "status": "requires_live_check",
                "decision_reasons": decision_reasons,
                "tool_status": tool_status,
                "provider": provider,
                "requested_location": location,
                "resolved_location": resolved_location,
                "requested_date": requested_date,
                "citation_count": len(citations),
                "claim_count": len(claims),
                "citation_marker_coverage": 1.0 if citations else 0.0,
            },
            "candidate_plan": None,
            "plan": None,
            "tool_results": tool_results,
            "planning_trace": {},
            "violations": [],
            "constraint_status": "not_applicable",
            "approval_status": "not_required",
            "approval_decision": None,
            "tool_call_count": total_used,
            "governance_trace": governance_trace,
            "trace": _next_trace(state, "weather_answer"),
        }

    def grounded_answer(state: AgentState) -> dict[str, Any]:
        result = dependencies.answerer.answer_with_trace(
            state["query"],
            _hydrate_hits(state.get("hits", [])),
            min_score=dependencies.settings.min_retrieval_score,
            context_limit=dependencies.settings.answer_context_limit,
            max_context_tokens=dependencies.settings.answer_context_token_budget,
        )
        return {
            **result.to_dict(),
            "candidate_plan": None,
            "plan": None,
            "tool_results": [],
            "planning_trace": {},
            "violations": [],
            "constraint_status": "not_applicable",
            "trace": _next_trace(state, "grounded_answer"),
        }

    def generate_candidates(state: AgentState) -> dict[str, Any]:
        result = dependencies.candidate_generator.generate(
            state["query"],
            _hydrate_hits(state.get("hits", [])),
            state["constraints"],
        )
        return {
            "candidate_plan": result.plan.to_dict(),
            "planning_trace": _planning_trace(
                state, "candidate_generation", result.trace
            ),
            "trace": _next_trace(state, "generate_candidates"),
        }

    def approval_gate(state: AgentState) -> dict[str, Any]:
        if not state.get("approval_required", False):
            return {
                "approval_status": "not_required",
                "governance_trace": _governance_trace(
                    state,
                    {
                        "event": "approval_not_required",
                        "node": "approval_gate",
                    },
                ),
                "trace": _next_trace(state, "approval_gate"),
            }
        candidate_plan = state.get("candidate_plan") or {}
        decision = interrupt(
            {
                "schema_version": "scenicpilot-approval-v1",
                "action": "execute_itinerary_tools",
                "question": "是否批准执行天气、路线、预算和营业时间查询？",
                "candidate_source_ids": [
                    stop.get("source_id")
                    for stop in candidate_plan.get("stops", [])
                ],
                "tools": ["weather", "distance", "budget", "opening_hours"],
                "tool_call_limit": int(state.get("tool_call_limit", 0)),
            }
        )
        if isinstance(decision, dict):
            approved = bool(decision.get("approved", False))
            reason = str(decision.get("reason", ""))[:500]
        else:
            approved = bool(decision)
            reason = ""
        normalized = {"approved": approved, "reason": reason}
        return {
            "approval_status": "approved" if approved else "rejected",
            "approval_decision": normalized,
            "governance_trace": _governance_trace(
                state,
                {
                    "event": "approval_decided",
                    "node": "approval_gate",
                    **normalized,
                },
            ),
            "trace": _next_trace(state, "approval_gate"),
        }

    def route_after_approval(
        state: AgentState,
    ) -> Literal["enrich_tools", "approval_rejected"]:
        return (
            "approval_rejected"
            if state.get("approval_status") == "rejected"
            else "enrich_tools"
        )

    def approval_rejected(state: AgentState) -> dict[str, Any]:
        candidate = state.get("candidate_plan")
        reason = str((state.get("approval_decision") or {}).get("reason", "")).strip()
        answer = "本次工具执行未获批准，行程生成已停止。"
        if reason:
            answer += f" 原因：{reason}"
        return {
            "answer": answer,
            "answer_status": "refused",
            "constraint_status": "rejected",
            "plan": candidate,
            "tool_results": [],
            "violations": [],
            "citations": [],
            "claims": [],
            "conflicts": [],
            "answer_trace": {
                "schema_version": "scenicpilot-answer-trace-v1",
                "status": "refused",
                "decision_reasons": ["human_approval_rejected"],
            },
            "planning_trace": _planning_trace(
                state,
                "output",
                {
                    "status": "refused",
                    "constraint_status": "rejected",
                    "approval_status": "rejected",
                },
            ),
            "governance_trace": {
                **state.get("governance_trace", {}),
                "tool_calls_used": int(state.get("tool_call_count", 0)),
            },
            "trace": _next_trace(state, "approval_rejected"),
        }

    def enrich_tools(state: AgentState) -> dict[str, Any]:
        used_before = int(state.get("tool_call_count", 0))
        call_limit = int(state.get("tool_call_limit", 0))
        remaining = max(0, call_limit - used_before)
        result = dependencies.tool_enricher.enrich(
            _hydrate_plan(state["candidate_plan"]),
            state["constraints"],
            max_tool_calls=remaining,
        )
        used_now = int(result.trace.get("executed_call_count", 0))
        total_used = used_before + used_now
        return {
            "plan": result.plan.to_dict(),
            "tool_results": result.tool_results,
            "tool_call_count": total_used,
            "governance_trace": _governance_trace(
                state,
                {
                    "event": "tool_batch_executed",
                    "node": "enrich_tools",
                    "requested": int(result.trace.get("requested_call_count", 0)),
                    "executed": used_now,
                    "skipped": int(result.trace.get("skipped_call_count", 0)),
                    "used_total": total_used,
                    "remaining": max(0, call_limit - total_used),
                },
            ),
            "planning_trace": _planning_trace(
                state, "tool_enrichment", result.trace
            ),
            "trace": _next_trace(state, "enrich_tools"),
        }

    def validate(state: AgentState) -> dict[str, Any]:
        used_before = int(state.get("tool_call_count", 0))
        call_limit = int(state.get("tool_call_limit", 0))
        remaining = max(0, call_limit - used_before)
        result = dependencies.verifier.validate(
            _hydrate_plan(state["plan"]),
            state["constraints"],
            tool_results=state.get("tool_results", []),
            hits=_hydrate_hits(state.get("hits", [])),
            max_tool_calls=remaining,
        )
        hours_used = int(result.metrics.get("opening_hours_calls_used", 0))
        total_used = used_before + hours_used
        violations = [violation.to_dict() for violation in result.violations]
        error_count = sum(item["severity"] == "error" for item in violations)
        warning_count = sum(item["severity"] == "warning" for item in violations)
        revision_count = int(state.get("revision_count", 0))
        max_revisions = int(state.get("max_revisions", 0))
        if not error_count:
            constraint_status = "valid"
        elif revision_count < max_revisions:
            constraint_status = "needs_revision"
        else:
            constraint_status = "rejected"
        round_trace = {
            "revision_count": revision_count,
            "violation_count": len(violations),
            "error_count": error_count,
            "warning_count": warning_count,
            "constraint_status": constraint_status,
            "metrics": result.metrics,
            "violations": violations,
            "opening_hours_results": result.opening_hours_results,
        }
        return {
            "plan": result.plan.to_dict(),
            "violations": violations,
            "tool_results": [
                item
                for item in state.get("tool_results", [])
                if item.get("tool") != "opening_hours"
            ]
            + result.opening_hours_results,
            "constraint_status": constraint_status,
            "tool_call_count": total_used,
            "governance_trace": _governance_trace(
                state,
                {
                    "event": "opening_hours_checked",
                    "node": "validate",
                    "requested": len(result.plan.stops),
                    "executed": hours_used,
                    "skipped": int(
                        result.metrics.get("opening_hours_calls_skipped", 0)
                    ),
                    "used_total": total_used,
                    "remaining": max(0, call_limit - total_used),
                },
            ),
            "planning_trace": _append_trace_round(
                state, "validation", round_trace
            ),
            "trace": _next_trace(state, "validate"),
        }

    def route_after_validation(
        state: AgentState,
    ) -> Literal["revise_plan", "finalize"]:
        return (
            "revise_plan"
            if state.get("constraint_status") == "needs_revision"
            else "finalize"
        )

    def revise_plan(state: AgentState) -> dict[str, Any]:
        attempt = int(state.get("revision_count", 0)) + 1
        violations = [
            ConstraintViolation(
                code=item["code"],
                message=item["message"],
                severity=item.get("severity", "error"),
                details=dict(item.get("details", {})),
            )
            for item in state.get("violations", [])
        ]
        result = dependencies.reviser.revise(
            _hydrate_plan(state["plan"]),
            state["constraints"],
            violations,
            _hydrate_hits(state.get("hits", [])),
            attempt=attempt,
        )
        revision_history = list(state.get("revision_history", []))
        revision_history.append(result.trace)
        constraint_status = "needs_revision" if result.changed else "rejected"
        return {
            "candidate_plan": result.plan.to_dict(),
            "plan": result.plan.to_dict(),
            "revision_count": attempt,
            "revision_changed": result.changed,
            "revision_history": revision_history,
            "constraint_status": constraint_status,
            "planning_trace": _append_trace_round(
                state, "revision", result.trace
            ),
            "trace": _next_trace(state, "revise_plan"),
        }

    def route_after_revision(
        state: AgentState,
    ) -> Literal["enrich_tools", "finalize"]:
        return "enrich_tools" if state.get("revision_changed") else "finalize"

    def finalize(state: AgentState) -> dict[str, Any]:
        plan_value = state["plan"]
        stops = plan_value.get("stops", [])
        errors = [
            item["message"]
            for item in state.get("violations", [])
            if item["severity"] == "error"
        ]
        if errors:
            revision_count = int(state.get("revision_count", 0))
            lines = [
                "当前约束下未能生成可执行行程，系统已拦截该计划。",
                "未通过原因：" + _join_messages(errors),
            ]
            if revision_count:
                lines.append(f"已尝试 {revision_count} 次确定性修订。")
            output_trace = {
                "status": "refused",
                "constraint_status": "rejected",
                "revision_count": revision_count,
                "citation_count": 0,
                "tool_degraded": any(
                    str(item.get("output", {}).get("status"))
                    not in {"ok", "estimated"}
                    for item in state.get("tool_results", [])
                    if item.get("tool") != "opening_hours"
                ),
            }
            return {
                "answer": "\n".join(lines),
                "answer_status": "refused",
                "citations": [],
                "claims": [],
                "conflicts": [],
                "constraint_status": "rejected",
                "answer_trace": {
                    "schema_version": "scenicpilot-answer-trace-v1",
                    "status": "refused",
                    "decision_reasons": [
                        "constraint_validation_failed",
                        (
                            "revision_exhausted"
                            if revision_count >= int(state.get("max_revisions", 0))
                            else "no_safe_revision"
                        ),
                    ],
                },
                "planning_trace": _planning_trace(
                    state, "output", output_trace
                ),
                "governance_trace": {
                    **state.get("governance_trace", {}),
                    "tool_calls_used": int(state.get("tool_call_count", 0)),
                },
                "trace": _next_trace(state, "finalize"),
            }
        if not stops:
            return {
                "answer": dependencies.answerer.refusal,
                "answer_status": "refused",
                "citations": [],
                "claims": [],
                "conflicts": [],
                "answer_trace": {
                    "schema_version": "scenicpilot-answer-trace-v1",
                    "status": "refused",
                    "decision_reasons": ["empty_itinerary"],
                },
                "planning_trace": _planning_trace(
                    state, "output", {"status": "refused"}
                ),
                "constraint_status": "rejected",
                "governance_trace": {
                    **state.get("governance_trace", {}),
                    "tool_calls_used": int(state.get("tool_call_count", 0)),
                },
                "trace": _next_trace(state, "finalize"),
            }

        lines = ["珠海长隆行程草案："]
        elapsed = 0
        for index, stop in enumerate(stops, 1):
            elapsed += int(stop["duration_minutes"])
            schedule = ""
            if stop.get("start_time") and stop.get("end_time"):
                schedule = f"，{stop['start_time']}-{stop['end_time']}"
            travel = int(stop.get("travel_from_previous_minutes", 0))
            if travel:
                schedule += f"，前序交通约 {travel} 分钟"
            lines.append(
                f"{index}. {stop['name']}（{stop['duration_minutes']} 分钟{schedule}，"
                f"累计游玩 {elapsed} 分钟）"
            )

        tool_degraded = False
        route_lines: list[str] = []
        for item in state.get("tool_results", []):
            output = item.get("output", {})
            status = str(output.get("status", "unavailable"))
            tool_name = str(item.get("tool", ""))
            if status not in {"ok", "estimated"}:
                tool_degraded = True
                continue
            if tool_name == "weather":
                weather = output.get("weather", "未知")
                weather_date = output.get("date", "目标日期")
                minimum = output.get("temperature_min_c")
                maximum = output.get("temperature_max_c")
                rain = output.get("precipitation_probability_max")
                detail = f"天气参考（{weather_date}）：{weather}"
                if minimum is not None and maximum is not None:
                    detail += f"，{minimum}～{maximum}℃"
                if rain is not None:
                    detail += f"，最大降水概率 {rain}%"
                lines.append(f"{detail}。")
            elif tool_name == "distance":
                meters = output.get("distance_meters")
                duration = output.get("duration_minutes")
                if meters is not None:
                    route_lines.append(
                        f"{output.get('origin')} → {output.get('destination')}："
                        f"{float(meters) / 1000:.1f} 公里，约 {duration} 分钟"
                    )
        if route_lines:
            lines.append("路线参考：" + "；".join(route_lines) + "。")
        lines.append(f"餐饮预留估算：{plan_value['estimated_cost']:.0f} 元。")

        degraded_assumptions = [
            assumption
            for assumption in plan_value.get("assumptions", [])
            if "工具补全部分降级" in assumption
        ]
        if degraded_assumptions:
            lines.append("工具降级：" + "；".join(degraded_assumptions))

        warnings = [
            item["message"]
            for item in state.get("violations", [])
            if item["severity"] == "warning"
        ]
        if warnings:
            lines.append("出发前核验：" + _join_messages(warnings))

        hits = _hydrate_hits(state.get("hits", []))
        seen: set[str] = set()
        citations: list[dict[str, Any]] = []
        for hit in hits:
            if hit.chunk.source_id in seen:
                continue
            if any(
                stop.get("source_id") == hit.chunk.source_id for stop in stops
            ):
                seen.add(hit.chunk.source_id)
                citations.append(
                    {
                        "source_id": hit.chunk.source_id,
                        "title": hit.chunk.title,
                        "url": hit.chunk.source_url,
                        "verified_at": hit.chunk.verified_at,
                        "volatile": hit.chunk.volatile,
                    }
                )

        needs_check = bool(warnings or errors or tool_degraded)
        answer_status = "requires_live_check" if needs_check else "answered"
        decision_reasons = ["itinerary_finalized"]
        if tool_degraded:
            decision_reasons.append("tool_enrichment_degraded")
        if warnings or errors:
            decision_reasons.append("constraint_check_requires_attention")
        output_trace = {
            "status": answer_status,
            "constraint_status": state.get("constraint_status", "valid"),
            "revision_count": int(state.get("revision_count", 0)),
            "citation_count": len(citations),
            "tool_degraded": tool_degraded,
        }
        return {
            "answer": "\n".join(lines),
            "answer_status": answer_status,
            "constraint_status": state.get("constraint_status", "valid"),
            "citations": citations,
            "claims": [],
            "conflicts": [],
            "answer_trace": {
                "schema_version": "scenicpilot-answer-trace-v1",
                "status": answer_status,
                "decision_reasons": decision_reasons,
            },
            "planning_trace": _planning_trace(state, "output", output_trace),
            "governance_trace": {
                **state.get("governance_trace", {}),
                "tool_calls_used": int(state.get("tool_call_count", 0)),
            },
            "trace": _next_trace(state, "finalize"),
        }

    workflow = StateGraph(AgentState)
    workflow.add_node("classify", classify)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("weather_answer", weather_answer)
    workflow.add_node("grounded_answer", grounded_answer)
    workflow.add_node("generate_candidates", generate_candidates)
    workflow.add_node("approval_gate", approval_gate)
    workflow.add_node("approval_rejected", approval_rejected)
    workflow.add_node("enrich_tools", enrich_tools)
    workflow.add_node("validate", validate)
    workflow.add_node("revise_plan", revise_plan)
    workflow.add_node("finalize", finalize)
    workflow.add_edge(START, "classify")
    workflow.add_conditional_edges("classify", route_after_classify)
    workflow.add_conditional_edges("retrieve", route_after_retrieval)
    workflow.add_edge("weather_answer", END)
    workflow.add_edge("grounded_answer", END)
    workflow.add_edge("generate_candidates", "approval_gate")
    workflow.add_conditional_edges("approval_gate", route_after_approval)
    workflow.add_edge("approval_rejected", END)
    workflow.add_edge("enrich_tools", "validate")
    workflow.add_conditional_edges("validate", route_after_validation)
    workflow.add_conditional_edges("revise_plan", route_after_revision)
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=checkpointer)
