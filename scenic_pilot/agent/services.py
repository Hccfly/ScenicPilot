from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from scenic_pilot.answering import GroundedAnswerer
from scenic_pilot.agent.validation import PREFERENCE_TERMS, ItineraryVerifier
from scenic_pilot.llm.contracts import ItineraryModelClient
from scenic_pilot.models import (
    ItineraryPlan,
    ItineraryStop,
    SearchHit,
)
from scenic_pilot.tools.managed import ToolSuite


class IntentClassifier:
    _itinerary_terms = (
        "行程",
        "安排",
        "路线",
        "怎么玩",
        "游玩",
        "一日",
        "一天",
        "半天",
        "先去",
        "带孩子",
    )
    _weather_terms = (
        "天气",
        "气温",
        "温度",
        "降雨",
        "下雨",
        "晴天",
        "台风",
    )

    def __init__(self, *, today=date.today) -> None:
        self.today = today

    def classify(self, query: str) -> str:
        if any(term in query for term in self._itinerary_terms):
            return "itinerary"
        if any(term in query for term in self._weather_terms):
            return "weather"
        return "faq"

    def extract_constraints(self, query: str) -> dict[str, Any]:
        constraints: dict[str, Any] = {
            "duration_minutes": (
                480 if any(term in query for term in ("一天", "一日")) else 240
            ),
            "party_size": 1,
        }
        if "半天" in query:
            constraints["duration_minutes"] = 240
        hours = re.search(r"(\d+(?:\.\d+)?)\s*个?小时", query)
        if hours:
            constraints["duration_minutes"] = int(float(hours.group(1)) * 60)
        budget = re.search(r"预算\s*(\d+(?:\.\d+)?)\s*元?", query)
        if budget:
            constraints["budget"] = float(budget.group(1))
        party = re.search(r"(\d+)\s*(?:人|位)", query)
        if party:
            constraints["party_size"] = max(1, int(party.group(1)))
        elif "一家三口" in query:
            constraints["party_size"] = 3

        visit_date = self._extract_visit_date(query)
        if visit_date:
            constraints["visit_date"] = visit_date
        if any(term in query for term in self._weather_terms):
            constraints["weather_location"] = self._extract_weather_location(query)
        origin = re.search(r"从\s*([^，。,.]{2,24}?)(?:出发|前往|去|到)", query)
        if origin:
            constraints["origin"] = origin.group(1).strip()

        preferences: list[str] = []
        if any(term in query for term in ("孩子", "亲子", "儿童")):
            preferences.append("family")
            constraints["visitor_type"] = "family"
        if any(term in query for term in ("老人", "长辈", "少走路", "轻松")):
            preferences.append("senior_friendly")
            constraints["visitor_type"] = "senior"
        if any(term in query for term in ("室内", "雨天", "避雨", "科普")):
            preferences.append("indoor_science")
        if any(term in query for term in ("海洋", "鲸鲨", "动物")):
            preferences.append("marine")
        if preferences:
            constraints["preferences"] = preferences
            constraints["preference"] = preferences[0]
        if any(term in query for term in ("必须", "一定要", "务必", "必看", "只想")):
            required = [
                preference
                for preference in preferences
                if preference in {"indoor_science", "marine"}
            ]
            if required:
                constraints["required_preferences"] = required
        clock = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)", query)
        if clock:
            constraints["start_time"] = (
                f"{int(clock.group(1)):02d}:{int(clock.group(2)):02d}"
            )
        return constraints

    @staticmethod
    def _extract_weather_location(query: str) -> str:
        if "珠海长隆" in query or "长隆" in query:
            return "珠海长隆度假区"
        if "横琴" in query:
            return "珠海横琴"
        if "珠海" in query:
            return "珠海"
        return "珠海长隆度假区"

    def _extract_visit_date(self, query: str) -> str | None:
        matched = re.search(
            r"(20\d{2})\s*(?:-|/|年)\s*(\d{1,2})\s*(?:-|/|月)\s*(\d{1,2})\s*日?",
            query,
        )
        if matched:
            try:
                return date(*(int(value) for value in matched.groups())).isoformat()
            except ValueError:
                return None
        base = self.today()
        if "后天" in query:
            return (base + timedelta(days=2)).isoformat()
        if "明天" in query:
            return (base + timedelta(days=1)).isoformat()
        if "今天" in query:
            return base.isoformat()
        return None


@dataclass(frozen=True, slots=True)
class CandidateGenerationResult:
    plan: ItineraryPlan
    trace: dict[str, Any]


class ItineraryCandidateGenerator:
    """Build a plan only from retrieved evidence, optionally model-ranked."""

    _category_priority = {"attraction": 0, "dining": 1}
    _preference_terms = PREFERENCE_TERMS

    def __init__(self, model_client: ItineraryModelClient) -> None:
        self.model_client = model_client

    def generate(
        self,
        query: str,
        hits: Sequence[SearchHit],
        constraints: dict[str, Any],
    ) -> CandidateGenerationResult:
        eligible = self._ranked_candidates(hits, constraints)
        candidate_view = [
            {
                "source_id": hit.chunk.source_id,
                "title": hit.chunk.title,
                "category": hit.chunk.category,
                "score": hit.score,
            }
            for hit in eligible[:8]
        ]
        model_result = self.model_client.select_candidates(
            query=query,
            constraints=constraints,
            candidates=candidate_view,
        )
        known = {hit.chunk.source_id: hit for hit in eligible}
        requested_ids = [
            str(source_id)
            for source_id in model_result.get("selected_source_ids", [])
        ]
        valid_ids = list(
            dict.fromkeys(
                source_id for source_id in requested_ids if source_id in known
            )
        )
        rejected_ids = list(
            dict.fromkeys(
                source_id for source_id in requested_ids if source_id not in known
            )
        )
        if model_result.get("status") == "ok" and valid_ids:
            selected = [known[source_id] for source_id in valid_ids]
            selection_mode = "model_ranked"
        else:
            selected = eligible
            selection_mode = "rule_fallback"

        plan = self._build_plan(selected, constraints, selection_mode)
        trace = {
            "schema_version": "scenicpilot-planning-trace-v1",
            "candidate_count": len(candidate_view),
            "selection_mode": selection_mode,
            "selected_source_ids": [
                stop.source_id for stop in plan.stops if stop.source_id
            ],
            "rejected_model_source_ids": rejected_ids,
            "model": {
                key: model_result[key]
                for key in (
                    "status",
                    "provider",
                    "model",
                    "degradation_reason",
                    "rationale",
                    "usage",
                )
                if key in model_result
            },
        }
        return CandidateGenerationResult(plan=plan, trace=trace)

    def plan(
        self,
        query: str,
        hits: Sequence[SearchHit],
        constraints: dict[str, Any],
    ) -> ItineraryPlan:
        return self.generate(query, hits, constraints).plan

    def _ranked_candidates(
        self, hits: Sequence[SearchHit], constraints: dict[str, Any]
    ) -> list[SearchHit]:
        preferences = tuple(constraints.get("preferences", ()))

        def preference_score(hit: SearchHit) -> int:
            text = f"{hit.chunk.title} {hit.context or hit.chunk.content}"
            return sum(
                1
                for preference in preferences
                for term in self._preference_terms.get(str(preference), ())
                if term in text
            )

        unique: list[SearchHit] = []
        seen: set[str] = set()
        for hit in hits:
            if (
                hit.chunk.source_id in seen
                or hit.chunk.category not in self._category_priority
            ):
                continue
            seen.add(hit.chunk.source_id)
            unique.append(hit)
        return sorted(
            unique,
            key=lambda hit: (
                self._category_priority[hit.chunk.category],
                -preference_score(hit),
                -hit.score,
            ),
        )

    @staticmethod
    def _build_plan(
        candidates: Sequence[SearchHit],
        constraints: dict[str, Any],
        selection_mode: str,
    ) -> ItineraryPlan:
        total_limit = int(constraints.get("duration_minutes", 240))
        senior_friendly = "senior_friendly" in constraints.get("preferences", ())
        max_stops = 2 if total_limit <= 240 else 3
        attractions = [
            candidate for candidate in candidates if candidate.chunk.category == "attraction"
        ]
        dining = [
            candidate for candidate in candidates if candidate.chunk.category == "dining"
        ]
        ordered: list[SearchHit] = []
        if attractions:
            ordered.append(attractions[0])
            if dining:
                first_title = attractions[0].chunk.title
                park_terms = [term for term in ("海洋王国", "飞船") if term in first_title]
                matching_dining = next(
                    (
                        item
                        for item in dining
                        if any(term in item.chunk.title for term in park_terms)
                    ),
                    dining[0],
                )
                ordered.append(matching_dining)
            ordered.extend(attractions[1:])
        else:
            ordered.extend(dining[:1])
        stops: list[ItineraryStop] = []
        used = 0
        for hit in ordered:
            duration = {
                "attraction": 90 if senior_friendly else 120,
                "dining": 60,
            }[hit.chunk.category]
            if used + duration > total_limit:
                continue
            stops.append(
                ItineraryStop(
                    name=hit.chunk.title,
                    category=hit.chunk.category,
                    duration_minutes=duration,
                    estimated_cost=0.0,
                    reason=f"仅使用检索证据，混合检索分数 {hit.score:.2f}",
                    source_id=hit.chunk.source_id,
                )
            )
            used += duration
            if len(stops) >= max_stops:
                break
        assumptions = [
            "候选地点只来自当前检索命中的资料，未命中的地点不会进入行程。",
            (
                "模型仅参与候选排序，最终地点已通过 source_id 白名单校验。"
                if selection_mode == "model_ranked"
                else "模型未启用或输出不可用，本次采用确定性规则排序。"
            ),
        ]
        if not stops:
            assumptions.append("没有足够资料生成可执行行程。")
        return ItineraryPlan(
            duration_minutes=used,
            estimated_cost=0.0,
            stops=stops,
            assumptions=assumptions,
        )


@dataclass(frozen=True, slots=True)
class ToolEnrichmentResult:
    plan: ItineraryPlan
    tool_results: list[dict[str, object]]
    trace: dict[str, Any]


class ItineraryToolEnricher:
    def __init__(self, tool_suite: ToolSuite) -> None:
        self.tool_suite = tool_suite

    def enrich(
        self,
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        *,
        max_tool_calls: int | None = None,
    ) -> ToolEnrichmentResult:
        if not plan.stops:
            return ToolEnrichmentResult(
                plan=plan,
                tool_results=[],
                trace={
                    "call_count": 0,
                    "route_leg_count": 0,
                    "requested_call_count": 0,
                    "executed_call_count": 0,
                    "skipped_call_count": 0,
                    "call_limit": max_tool_calls,
                    "budget_exhausted": False,
                },
            )
        calls: list[dict[str, Any]] = [
            {
                "request_id": "weather-1",
                "tool": "weather",
                "arguments": {
                    "location": "珠海长隆度假区",
                    "date": constraints.get("visit_date"),
                },
            },
            {
                "request_id": "budget-1",
                "tool": "budget",
                "arguments": {
                    "party_size": int(constraints.get("party_size", 1)),
                    "duration_minutes": max(plan.duration_minutes, 1),
                },
            },
        ]
        route_points: list[str] = []
        origin = str(constraints.get("origin", "")).strip()
        if origin:
            route_points.append(origin)
        for stop in plan.stops:
            location = self._location_for_stop(stop)
            if not route_points or route_points[-1] != location:
                route_points.append(location)
        for index, (origin_name, destination_name) in enumerate(
            zip(route_points, route_points[1:]), 1
        ):
            calls.append(
                {
                    "request_id": f"route-{index}",
                    "tool": "distance",
                    "arguments": {
                        "origin": origin_name,
                        "destination": destination_name,
                    },
                }
            )

        batch = self.tool_suite.run(calls, max_calls=max_tool_calls)
        tool_results = list(batch["results"])
        assumptions = list(plan.assumptions)
        estimated_cost = plan.estimated_cost
        degradation_reasons: list[str] = []
        for item in tool_results:
            output = item["output"]
            tool_name = str(item["tool"])
            status = str(output.get("status", "unavailable"))
            if tool_name == "budget" and status in {"ok", "estimated"}:
                raw_cost = output.get("estimated_cost")
                if (
                    isinstance(raw_cost, (int, float))
                    and not isinstance(raw_cost, bool)
                    and raw_cost >= 0
                ):
                    estimated_cost = float(raw_cost)
                    assumption = "金额仅为餐饮预留估算，不含门票、住宿和交通。"
                    if assumption not in assumptions:
                        assumptions.append(assumption)
                else:
                    output.update(
                        {
                            "status": "unavailable",
                            "degradation_reason": "invalid_tool_result",
                            "invalid_fields": ["estimated_cost"],
                        }
                    )
                    status = "unavailable"
            if status not in {"ok", "estimated"}:
                reason = str(
                    output.get("degradation_reason", f"{tool_name}_unavailable")
                )
                degradation_reasons.append(f"{tool_name}:{reason}")
        if degradation_reasons:
            assumption = "工具补全部分降级（" + "，".join(degradation_reasons) + "）。"
            if assumption not in assumptions:
                assumptions.append(assumption)
        enriched = ItineraryPlan(
            duration_minutes=plan.duration_minutes,
            estimated_cost=estimated_cost,
            stops=list(plan.stops),
            assumptions=assumptions,
        )
        summary = dict(batch["summary"])
        governance = dict(batch.get("governance", {}))
        return ToolEnrichmentResult(
            plan=enriched,
            tool_results=tool_results,
            trace={
                **summary,
                **governance,
                "route_leg_count": max(0, len(route_points) - 1),
                "degradation_reasons": degradation_reasons,
            },
        )

    @staticmethod
    def _location_for_stop(stop: ItineraryStop) -> str:
        text = f"{stop.source_id or ''} {stop.name}"
        if "spaceship" in text or "飞船" in text:
            return "珠海长隆飞船乐园"
        if "ocean" in text or "海洋王国" in text:
            return "珠海长隆海洋王国"
        return "珠海长隆度假区"
