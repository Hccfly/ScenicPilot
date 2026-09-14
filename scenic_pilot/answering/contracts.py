from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from scenic_pilot.models import Citation


AnswerStatus = Literal["answered", "requires_live_check", "refused", "conflict"]


@dataclass(frozen=True, slots=True)
class AnswerPolicy:
    min_keyword_score: float = 0.08
    min_specific_term_matches: int = 1
    min_specific_term_coverage: float = 0.5
    minimum_authority_level: int = 1
    max_claim_chars: int = 360
    blocked_statuses: tuple[str, ...] = (
        "blocked",
        "failed",
        "invalid",
        "removed",
        "unavailable",
    )

    def validate(self) -> None:
        if self.min_keyword_score < 0:
            raise ValueError("minimum keyword score cannot be negative")
        if self.min_specific_term_matches <= 0:
            raise ValueError("minimum specific term matches must be positive")
        if not 0 < self.min_specific_term_coverage <= 1:
            raise ValueError("specific term coverage must be within (0, 1]")
        if self.minimum_authority_level < 0:
            raise ValueError("minimum authority level cannot be negative")
        if self.max_claim_chars <= 0:
            raise ValueError("maximum claim characters must be positive")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["blocked_statuses"] = list(self.blocked_statuses)
        return value


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    claim_id: str
    text: str
    citation_ids: tuple[int, ...]
    source_ids: tuple[str, ...]
    volatile: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "citation_ids": list(self.citation_ids),
            "source_ids": list(self.source_ids),
            "volatile": self.volatile,
        }


@dataclass(frozen=True, slots=True)
class ConflictVariant:
    source_id: str
    value: str
    evidence_text: str
    authority_level: int
    verified_at: str | None
    scope_score: int
    volatile: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AnswerConflict:
    conflict_id: str
    fact_type: str
    subject: str
    variants: tuple[ConflictVariant, ...]
    resolved: bool
    chosen_source_id: str | None
    resolution_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "fact_type": self.fact_type,
            "subject": self.subject,
            "variants": [item.to_dict() for item in self.variants],
            "resolved": self.resolved,
            "chosen_source_id": self.chosen_source_id,
            "resolution_reason": self.resolution_reason,
        }


@dataclass(frozen=True, slots=True)
class AnswerResult:
    answer: str
    status: AnswerStatus
    citations: tuple[Citation, ...] = ()
    claims: tuple[AnswerClaim, ...] = ()
    conflicts: tuple[AnswerConflict, ...] = ()
    trace: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "answer_status": self.status,
            "citations": [item.to_dict() for item in self.citations],
            "claims": [item.to_dict() for item in self.claims],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "answer_trace": self.trace or {},
        }
