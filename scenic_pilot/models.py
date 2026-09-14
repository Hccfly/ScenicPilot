from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    scenic_id: str
    title: str
    category: str
    content: str
    source_url: str
    authority_level: int = 1
    verified_at: str | None = None
    volatile: bool = False
    demo: bool = False
    status: str = "unknown"
    refresh_days: int | None = None
    retrieval_policy: str = "cached_ok"
    source_type: str = "unknown"
    content_sha256: str | None = None
    document_version: str | None = None
    extraction_method: str = "manifest_summary"
    selector_used: str | None = None
    fetched_at: str | None = None
    http_status: int | None = None
    raw_content_sha256: str | None = None
    extraction_quality: float | None = None
    topic_purity_score: float | None = None
    contamination_flags: tuple[str, ...] = ()
    cleanup_flags: tuple[str, ...] = ()
    schema_complete: bool | None = None
    structure_flags: tuple[str, ...] = ()
    discovered_links: tuple[dict[str, Any], ...] = ()
    discovered_from_source_id: str | None = None
    headings: tuple[str, ...] = ()
    image_records: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceDocument":
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    source_id: str
    scenic_id: str
    title: str
    category: str
    content: str
    source_url: str
    authority_level: int
    verified_at: str | None
    volatile: bool
    demo: bool
    position: int
    status: str = "unknown"
    refresh_days: int | None = None
    retrieval_policy: str = "cached_ok"
    source_type: str = "unknown"
    content_sha256: str | None = None
    char_start: int = 0
    char_end: int = 0
    chunking_strategy: str = "char-v1"
    level: str = "child"
    parent_chunk_id: str | None = None
    heading_path: tuple[str, ...] = ()
    anchor: str | None = None
    content_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk: Chunk
    score: float
    keyword_score: float
    vector_score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None
    fusion_score: float | None = None
    matched_terms: tuple[str, ...] = ()
    applied_filters: tuple[str, ...] = ()
    category_boost_applied: bool = False
    parent_chunk_id: str | None = None
    context: str | None = None
    context_expanded: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchHit":
        fields = {
            name: value[name]
            for name in cls.__dataclass_fields__
            if name in value and name != "chunk"
        }
        return cls(chunk=Chunk(**value["chunk"]), **fields)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["chunk"] = self.chunk.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class Citation:
    source_id: str
    title: str
    url: str
    verified_at: str | None
    volatile: bool
    citation_id: int | None = None
    authority_level: int = 1
    status: str = "unknown"
    retrieval_policy: str = "cached_ok"
    source_type: str = "unknown"
    refresh_days: int | None = None
    freshness: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ItineraryStop:
    name: str
    category: str
    duration_minutes: int
    estimated_cost: float
    reason: str
    source_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    travel_from_previous_minutes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ItineraryPlan:
    duration_minutes: int
    estimated_cost: float
    stops: list[ItineraryStop] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_minutes": self.duration_minutes,
            "estimated_cost": self.estimated_cost,
            "stops": [stop.to_dict() for stop in self.stops],
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    code: str
    message: str
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
