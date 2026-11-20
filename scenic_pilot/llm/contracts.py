from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class ItineraryModelClient(Protocol):
    """Model boundary that may rank retrieved candidates, never invent places."""

    def select_candidates(
        self,
        *,
        query: str,
        constraints: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> dict[str, object]: ...


class DisabledItineraryModelClient:
    def __init__(self, reason: str = "provider_disabled") -> None:
        self.reason = reason

    def select_candidates(
        self,
        *,
        query: str,
        constraints: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> dict[str, object]:
        return {
            "status": "disabled",
            "provider": "none",
            "selected_source_ids": [],
            "degradation_reason": self.reason,
        }


class QueryUnderstandingClient(Protocol):
    """Model boundary for slot filling, never for facts or free-form answers."""

    def understand(self, *, query: str) -> dict[str, object]: ...


class DisabledQueryUnderstandingClient:
    def __init__(self, reason: str = "provider_disabled") -> None:
        self.reason = reason

    def understand(self, *, query: str) -> dict[str, object]:
        return {
            "status": "disabled",
            "provider": "none",
            "degradation_reason": self.reason,
        }
