from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    query: str
    scenic_id: str
    intent: Literal["faq", "weather", "itinerary"]
    constraints: dict[str, Any]
    hits: list[dict[str, Any]]
    retrieval_trace: dict[str, Any]
    answer: str
    answer_status: Literal[
        "pending", "answered", "requires_live_check", "refused", "conflict"
    ]
    citations: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    answer_trace: dict[str, Any]
    candidate_plan: dict[str, Any] | None
    plan: dict[str, Any] | None
    tool_results: list[dict[str, Any]]
    planning_trace: dict[str, Any]
    violations: list[dict[str, Any]]
    constraint_status: Literal[
        "not_applicable", "valid", "needs_revision", "rejected"
    ]
    revision_count: int
    max_revisions: int
    revision_changed: bool
    revision_history: list[dict[str, Any]]
    approval_required: bool
    approval_status: Literal["not_required", "pending", "approved", "rejected"]
    approval_decision: dict[str, Any] | None
    tool_call_count: int
    tool_call_limit: int
    governance_trace: dict[str, Any]
    trace: list[str]
