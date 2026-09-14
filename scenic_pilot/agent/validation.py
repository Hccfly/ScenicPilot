from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from scenic_pilot.models import (
    ConstraintViolation,
    ItineraryPlan,
    ItineraryStop,
    SearchHit,
)
from scenic_pilot.tools.contracts import OpeningHoursTool


PREFERENCE_TERMS: dict[str, tuple[str, ...]] = {
    "family": ("亲子", "儿童", "科普", "飞船", "海洋"),
    "senior_friendly": ("休息", "餐饮", "酒店", "轻松"),
    "indoor_science": ("飞船", "室内", "科普", "spaceship"),
    "marine": ("海洋", "鲸鲨", "动物", "ocean"),
}


def _parse_clock(value: object) -> int | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = (int(part) for part in value.strip().split(":", 1))
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _format_clock(minutes: int) -> str:
    hour, minute = divmod(minutes % (24 * 60), 60)
    return f"{hour:02d}:{minute:02d}"


def _stop_location(stop: ItineraryStop) -> str:
    text = f"{stop.source_id or ''} {stop.name}".casefold()
    if "spaceship" in text or "飞船" in text:
        return "珠海长隆飞船乐园"
    if "ocean" in text or "海洋王国" in text:
        return "珠海长隆海洋王国"
    return "珠海长隆度假区"


def _stop_identity(stop: ItineraryStop) -> str:
    return (stop.source_id or stop.name).strip().casefold()


def _hit_text(hit: SearchHit) -> str:
    return (
        f"{hit.chunk.source_id} {hit.chunk.title} "
        f"{hit.context or hit.chunk.content}"
    ).casefold()


def _matches_preference(text: str, preference: str) -> bool:
    return any(term.casefold() in text for term in PREFERENCE_TERMS.get(preference, ()))


@dataclass(frozen=True, slots=True)
class ItineraryValidationResult:
    plan: ItineraryPlan
    violations: list[ConstraintViolation]
    opening_hours_results: list[dict[str, Any]]
    metrics: dict[str, Any]


class ItineraryVerifier:
    """Validate execution constraints without inventing missing live facts."""

    def __init__(
        self,
        opening_hours_tool: OpeningHoursTool,
        *,
        default_start_time: str = "09:30",
    ) -> None:
        self.opening_hours_tool = opening_hours_tool
        self.default_start_time = default_start_time

    def verify(
        self,
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        *,
        tool_results: Sequence[dict[str, Any]] = (),
        hits: Sequence[SearchHit] = (),
        max_tool_calls: int | None = None,
    ) -> list[ConstraintViolation]:
        """Backward-compatible violation-only interface."""
        return self.validate(
            plan,
            constraints,
            tool_results=tool_results,
            hits=hits,
            max_tool_calls=max_tool_calls,
        ).violations

    def validate(
        self,
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        *,
        tool_results: Sequence[dict[str, Any]] = (),
        hits: Sequence[SearchHit] = (),
        max_tool_calls: int | None = None,
    ) -> ItineraryValidationResult:
        violations: list[ConstraintViolation] = []
        if not plan.stops:
            violation = ConstraintViolation(
                "empty_plan", "没有足够证据生成行程。"
            )
            return ItineraryValidationResult(
                plan=plan,
                violations=[violation],
                opening_hours_results=[],
                metrics={
                    "visit_minutes": 0,
                    "route_minutes": 0,
                    "total_duration_minutes": 0,
                    "duration_limit_minutes": int(
                        constraints.get("duration_minutes", 0)
                    ),
                    "budget_limit": constraints.get("budget"),
                    "estimated_cost": plan.estimated_cost,
                    "opening_hours_calls_used": 0,
                    "opening_hours_calls_skipped": 0,
                    "tool_call_budget_exhausted": False,
                },
            )

        visit_minutes = sum(max(0, stop.duration_minutes) for stop in plan.stops)
        duration_limit = int(constraints.get("duration_minutes", visit_minutes))
        route_items = [
            item for item in tool_results if item.get("tool") == "distance"
        ]
        route_minutes = 0
        route_unverified = 0
        route_estimated = 0
        for item in route_items:
            output = item.get("output") or {}
            value = output.get("duration_minutes")
            if isinstance(value, (int, float)) and value >= 0:
                route_minutes += int(value)
            else:
                route_unverified += 1
            if str(output.get("status")) != "ok":
                route_estimated += 1

        total_duration = visit_minutes + route_minutes
        if visit_minutes > duration_limit:
            violations.append(
                ConstraintViolation(
                    "duration_exceeded",
                    "游玩项目总时长超过用户可用时间。",
                    details={
                        "visit_minutes": visit_minutes,
                        "duration_limit_minutes": duration_limit,
                        "excess_minutes": visit_minutes - duration_limit,
                    },
                )
            )
        elif total_duration > duration_limit:
            violations.append(
                ConstraintViolation(
                    "route_time_exceeded",
                    "加入路线耗时后，行程超过用户可用时间。",
                    details={
                        "visit_minutes": visit_minutes,
                        "route_minutes": route_minutes,
                        "total_duration_minutes": total_duration,
                        "duration_limit_minutes": duration_limit,
                        "excess_minutes": total_duration - duration_limit,
                    },
                )
            )
        if route_unverified:
            violations.append(
                ConstraintViolation(
                    "route_unverified",
                    f"有 {route_unverified} 段路线缺少可用耗时。",
                    severity="warning",
                    details={"leg_count": route_unverified},
                )
            )
        elif route_estimated:
            violations.append(
                ConstraintViolation(
                    "route_estimated",
                    f"有 {route_estimated} 段路线使用估算耗时，出发前需复核。",
                    severity="warning",
                    details={"leg_count": route_estimated},
                )
            )

        budget = constraints.get("budget")
        budget_items = [
            item for item in tool_results if item.get("tool") == "budget"
        ]
        budget_verified = any(
            str((item.get("output") or {}).get("status")) in {"ok", "estimated"}
            for item in budget_items
        )
        if budget is not None and plan.estimated_cost > float(budget):
            violations.append(
                ConstraintViolation(
                    "budget_exceeded",
                    "当前预算估算已超过用户上限。",
                    details={
                        "estimated_cost": plan.estimated_cost,
                        "budget_limit": float(budget),
                        "excess_amount": plan.estimated_cost - float(budget),
                    },
                )
            )
        elif budget is not None and not budget_verified:
            violations.append(
                ConstraintViolation(
                    "budget_unverified",
                    "预算工具未完成，当前金额不能用于确认预算约束。",
                    severity="warning",
                    details={"budget_limit": float(budget)},
                )
            )

        identities = [_stop_identity(stop) for stop in plan.stops]
        duplicate_ids = sorted(
            {identity for identity in identities if identities.count(identity) > 1}
        )
        if duplicate_ids:
            violations.append(
                ConstraintViolation(
                    "duplicate_stop",
                    "行程包含重复站点。",
                    details={"stop_ids": duplicate_ids},
                )
            )

        missing_preferences = self._missing_preferences(plan, constraints, hits)
        if missing_preferences:
            violations.append(
                ConstraintViolation(
                    "missing_required_preference",
                    "行程未覆盖用户声明的强制偏好："
                    + "、".join(missing_preferences),
                    details={"preferences": missing_preferences},
                )
            )

        (
            scheduled_plan,
            hours_results,
            hours_violations,
            hours_executed,
            hours_skipped,
        ) = self._schedule_and_hours(
            plan,
            constraints,
            route_items,
            max_tool_calls=max_tool_calls,
        )
        violations.extend(hours_violations)
        metrics = {
            "visit_minutes": visit_minutes,
            "route_minutes": route_minutes,
            "total_duration_minutes": total_duration,
            "duration_limit_minutes": duration_limit,
            "route_leg_count": len(route_items),
            "route_unverified_count": route_unverified,
            "route_estimated_count": route_estimated,
            "budget_limit": float(budget) if budget is not None else None,
            "estimated_cost": plan.estimated_cost,
            "budget_verified": budget_verified,
            "required_preferences": list(
                constraints.get("required_preferences", ())
            ),
            "missing_required_preferences": missing_preferences,
            "schedule_start_time": constraints.get(
                "start_time", self.default_start_time
            ),
            "opening_hours_calls_used": hours_executed,
            "opening_hours_calls_skipped": hours_skipped,
            "tool_call_budget_exhausted": hours_skipped > 0,
        }
        return ItineraryValidationResult(
            plan=scheduled_plan,
            violations=violations,
            opening_hours_results=hours_results,
            metrics=metrics,
        )

    @staticmethod
    def _missing_preferences(
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        hits: Sequence[SearchHit],
    ) -> list[str]:
        required = [str(item) for item in constraints.get("required_preferences", ())]
        if not required:
            return []
        hit_by_source = {hit.chunk.source_id: _hit_text(hit) for hit in hits}
        selected_texts = [
            hit_by_source.get(
                stop.source_id or "",
                f"{stop.source_id or ''} {stop.name} {stop.reason}".casefold(),
            )
            for stop in plan.stops
        ]
        return [
            preference
            for preference in required
            if not any(
                _matches_preference(text, preference) for text in selected_texts
            )
        ]

    def _schedule_and_hours(
        self,
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        route_items: Sequence[dict[str, Any]],
        *,
        max_tool_calls: int | None,
    ) -> tuple[
        ItineraryPlan,
        list[dict[str, Any]],
        list[ConstraintViolation],
        int,
        int,
    ]:
        start_value = str(constraints.get("start_time", self.default_start_time))
        cursor = _parse_clock(start_value)
        if cursor is None:
            cursor = _parse_clock(self.default_start_time) or 570
        previous_location = str(constraints.get("origin", "")).strip() or None
        unused_route_indexes = set(range(len(route_items)))
        scheduled: list[ItineraryStop] = []
        hours_results: list[dict[str, Any]] = []
        violations: list[ConstraintViolation] = []
        hours_executed = 0
        allowed_calls = len(plan.stops) if max_tool_calls is None else max(0, max_tool_calls)

        for index, stop in enumerate(plan.stops, 1):
            location = _stop_location(stop)
            travel_minutes = 0
            if previous_location and previous_location != location:
                for route_index in sorted(unused_route_indexes):
                    route = route_items[route_index]
                    arguments = route.get("arguments") or {}
                    output = route.get("output") or {}
                    origin = str(output.get("origin") or arguments.get("origin") or "")
                    destination = str(
                        output.get("destination")
                        or arguments.get("destination")
                        or ""
                    )
                    if origin == previous_location and destination == location:
                        raw_duration = output.get("duration_minutes")
                        if isinstance(raw_duration, (int, float)):
                            travel_minutes = max(0, int(raw_duration))
                        unused_route_indexes.remove(route_index)
                        break
            cursor += travel_minutes
            stop_start = cursor
            stop_end = stop_start + max(0, stop.duration_minutes)
            scheduled_stop = replace(
                stop,
                start_time=_format_clock(stop_start),
                end_time=_format_clock(stop_end),
                travel_from_previous_minutes=travel_minutes,
            )
            scheduled.append(scheduled_stop)
            cursor = stop_end
            previous_location = location

            date = constraints.get("visit_date")
            if hours_executed >= allowed_calls:
                output = {
                    "status": "unavailable",
                    "provider": "none",
                    "venue": stop.name,
                    "date": date,
                    "is_open": None,
                    "message": "本轮工具调用预算已耗尽，未执行营业时间查询。",
                    "degradation_reason": "call_budget_exceeded",
                }
            else:
                hours_executed += 1
                try:
                    output = dict(
                        self.opening_hours_tool.check(venue=stop.name, date=date)
                    )
                except Exception as error:
                    output = {
                        "status": "unavailable",
                        "provider": "none",
                        "venue": stop.name,
                        "date": date,
                        "is_open": None,
                        "degradation_reason": "uncaught_tool_exception",
                        "error_type": type(error).__name__,
                    }
            wrapped = {
                "request_id": f"hours-{index}",
                "tool": "opening_hours",
                "arguments": {"venue": stop.name, "date": date},
                "source_id": stop.source_id,
                "output": output,
            }
            hours_results.append(wrapped)
            is_open = output.get("is_open")
            status = str(output.get("status", "unavailable"))
            if is_open is not None and not isinstance(is_open, bool):
                output.update(
                    {
                        "status": "unavailable",
                        "degradation_reason": "invalid_tool_result",
                        "invalid_fields": ["is_open"],
                    }
                )
                is_open = None
                status = "unavailable"
            details = {
                "source_id": stop.source_id,
                "venue": stop.name,
                "scheduled_start": scheduled_stop.start_time,
                "scheduled_end": scheduled_stop.end_time,
                "provider": output.get("provider"),
            }
            if is_open is False or status == "closed":
                violations.append(
                    ConstraintViolation(
                        "closed_venue",
                        f"{stop.name} 在目标日期不开放。",
                        details=details,
                    )
                )
                continue
            opens_at = _parse_clock(
                output.get("opens_at") or output.get("open_time")
            )
            closes_at = _parse_clock(
                output.get("closes_at") or output.get("close_time")
            )
            if is_open is True:
                if opens_at is None or closes_at is None:
                    violations.append(
                        ConstraintViolation(
                            "hours_unverified",
                            f"{stop.name} 的开放时间返回不完整，尚未完成核验。",
                            severity="warning",
                            details=details,
                        )
                    )
                elif stop_start < opens_at or stop_end > closes_at:
                    violations.append(
                        ConstraintViolation(
                            "outside_opening_hours",
                            f"{stop.name} 的计划时段不在营业窗口内。",
                            details={
                                **details,
                                "opens_at": _format_clock(opens_at),
                                "closes_at": _format_clock(closes_at),
                            },
                        )
                    )
            elif is_open is None:
                violations.append(
                    ConstraintViolation(
                        "hours_unverified",
                        f"{stop.name} 的开放时间尚未实时核验。",
                        severity="warning",
                        details=details,
                    )
                )

        return (
            ItineraryPlan(
                duration_minutes=sum(stop.duration_minutes for stop in scheduled),
                estimated_cost=plan.estimated_cost,
                stops=scheduled,
                assumptions=list(plan.assumptions),
            ),
            hours_results,
            violations,
            hours_executed,
            len(plan.stops) - hours_executed,
        )


@dataclass(frozen=True, slots=True)
class ItineraryRevisionResult:
    plan: ItineraryPlan
    changed: bool
    trace: dict[str, Any]


class ItineraryPlanReviser:
    """Apply deterministic, evidence-bounded repairs to an invalid plan."""

    def revise(
        self,
        plan: ItineraryPlan,
        constraints: dict[str, Any],
        violations: Sequence[ConstraintViolation],
        hits: Sequence[SearchHit],
        *,
        attempt: int,
    ) -> ItineraryRevisionResult:
        before_ids = [stop.source_id or stop.name for stop in plan.stops]
        stops = [
            replace(
                stop,
                start_time=None,
                end_time=None,
                travel_from_previous_minutes=0,
            )
            for stop in plan.stops
        ]
        actions: list[dict[str, Any]] = []
        codes = {violation.code for violation in violations if violation.severity == "error"}

        if "duplicate_stop" in codes:
            unique: list[ItineraryStop] = []
            seen: set[str] = set()
            for stop in stops:
                identity = _stop_identity(stop)
                if identity in seen:
                    actions.append(
                        {"action": "remove_duplicate", "source_id": stop.source_id}
                    )
                    continue
                seen.add(identity)
                unique.append(stop)
            stops = unique

        blocked_ids: set[str] = set()
        blocked_names: set[str] = set()
        for violation in violations:
            if violation.code not in {"closed_venue", "outside_opening_hours"}:
                continue
            source_id = violation.details.get("source_id")
            venue = violation.details.get("venue")
            if source_id:
                blocked_ids.add(str(source_id))
            if venue:
                blocked_names.add(str(venue))
        if blocked_ids or blocked_names:
            retained: list[ItineraryStop] = []
            for stop in stops:
                if (stop.source_id and stop.source_id in blocked_ids) or stop.name in blocked_names:
                    actions.append(
                        {
                            "action": "remove_unavailable_stop",
                            "source_id": stop.source_id,
                            "name": stop.name,
                        }
                    )
                else:
                    retained.append(stop)
            stops = retained

        missing_preferences: list[str] = []
        for violation in violations:
            if violation.code == "missing_required_preference":
                missing_preferences.extend(
                    str(item) for item in violation.details.get("preferences", ())
                )
        for preference in dict.fromkeys(missing_preferences):
            candidate = self._preference_candidate(
                preference,
                hits,
                stops,
                blocked_ids,
            )
            if candidate is None:
                continue
            duration = 60 if candidate.chunk.category == "dining" else (
                90
                if "senior_friendly" in constraints.get("preferences", ())
                else 120
            )
            replacement = ItineraryStop(
                name=candidate.chunk.title,
                category=candidate.chunk.category,
                duration_minutes=duration,
                estimated_cost=0.0,
                reason=f"约束修订：补足强制偏好 {preference}",
                source_id=candidate.chunk.source_id,
            )
            duration_limit = int(constraints.get("duration_minutes", 240))
            if sum(stop.duration_minutes for stop in stops) + duration <= duration_limit:
                stops.append(replacement)
                actions.append(
                    {
                        "action": "add_required_preference",
                        "preference": preference,
                        "source_id": replacement.source_id,
                    }
                )
                continue
            protected = self._sources_covering_required_preferences(
                stops, constraints, hits
            )
            replace_index = next(
                (
                    index
                    for index in range(len(stops) - 1, -1, -1)
                    if (stops[index].source_id or "") not in protected
                ),
                None,
            )
            if replace_index is not None:
                removed = stops[replace_index]
                stops[replace_index] = replacement
                actions.append(
                    {
                        "action": "replace_for_required_preference",
                        "preference": preference,
                        "removed_source_id": removed.source_id,
                        "source_id": replacement.source_id,
                    }
                )

        if "budget_exceeded" in codes:
            dining_index = next(
                (
                    index
                    for index in range(len(stops) - 1, -1, -1)
                    if stops[index].category == "dining"
                ),
                None,
            )
            if dining_index is not None:
                removed = stops.pop(dining_index)
                actions.append(
                    {
                        "action": "remove_optional_dining",
                        "source_id": removed.source_id,
                    }
                )

        if codes & {"duration_exceeded", "route_time_exceeded"}:
            violation = next(
                item
                for item in violations
                if item.code in {"duration_exceeded", "route_time_exceeded"}
            )
            route_minutes = int(violation.details.get("route_minutes", 0))
            duration_limit = int(constraints.get("duration_minutes", 240))
            protected = self._sources_covering_required_preferences(
                stops, constraints, hits
            )
            while (
                len(stops) > 1
                and sum(stop.duration_minutes for stop in stops) + route_minutes
                > duration_limit
            ):
                remove_index = next(
                    (
                        index
                        for index in range(len(stops) - 1, -1, -1)
                        if (stops[index].source_id or "") not in protected
                    ),
                    None,
                )
                if remove_index is None:
                    break
                removed = stops.pop(remove_index)
                actions.append(
                    {
                        "action": "remove_for_time_limit",
                        "source_id": removed.source_id,
                    }
                )

        after_ids = [stop.source_id or stop.name for stop in stops]
        changed = before_ids != after_ids
        assumptions = list(plan.assumptions)
        if changed:
            assumptions.append(f"已执行第 {attempt} 次确定性约束修订。")
        else:
            assumptions.append(f"第 {attempt} 次修订未找到安全的确定性修改。")
        final_stops = stops if changed else list(plan.stops)
        revised = ItineraryPlan(
            duration_minutes=sum(stop.duration_minutes for stop in final_stops),
            estimated_cost=0.0 if changed else plan.estimated_cost,
            stops=final_stops,
            assumptions=assumptions,
        )
        return ItineraryRevisionResult(
            plan=revised,
            changed=changed,
            trace={
                "attempt": attempt,
                "trigger_codes": sorted(codes),
                "actions": actions,
                "changed": changed,
                "before_source_ids": before_ids,
                "after_source_ids": after_ids,
            },
        )

    @staticmethod
    def _preference_candidate(
        preference: str,
        hits: Sequence[SearchHit],
        stops: Sequence[ItineraryStop],
        blocked_ids: set[str],
    ) -> SearchHit | None:
        selected = {_stop_identity(stop) for stop in stops}
        candidates = [
            hit
            for hit in hits
            if hit.chunk.category in {"attraction", "dining"}
            and hit.chunk.source_id.casefold() not in selected
            and hit.chunk.source_id not in blocked_ids
            and _matches_preference(_hit_text(hit), preference)
        ]
        return max(candidates, key=lambda item: item.score, default=None)

    @staticmethod
    def _sources_covering_required_preferences(
        stops: Sequence[ItineraryStop],
        constraints: dict[str, Any],
        hits: Sequence[SearchHit],
    ) -> set[str]:
        required = [str(item) for item in constraints.get("required_preferences", ())]
        if not required:
            return set()
        hit_by_source = {hit.chunk.source_id: _hit_text(hit) for hit in hits}
        protected: set[str] = set()
        for preference in required:
            matches = [
                stop
                for stop in stops
                if _matches_preference(
                    hit_by_source.get(
                        stop.source_id or "",
                        f"{stop.source_id or ''} {stop.name} {stop.reason}".casefold(),
                    ),
                    preference,
                )
            ]
            if len(matches) == 1 and matches[0].source_id:
                protected.add(matches[0].source_id)
        return protected
