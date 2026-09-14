from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

from scenic_pilot.models import Chunk, SearchHit


RETRIEVAL_SCHEMA_VERSION = "scenicpilot-retrieval-v1"


def _tuple_value(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return (str(value),)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    scenic_id: str
    categories: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    minimum_authority: int | None = None
    volatile: bool | None = None
    demo: bool | None = False
    retrieval_policies: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls, value: dict[str, object] | None, *, scenic_id: str
    ) -> "RetrievalFilters":
        raw = value or {}
        return cls(
            scenic_id=str(raw.get("scenic_id", scenic_id)),
            categories=_tuple_value(raw.get("categories", raw.get("category"))),
            source_types=_tuple_value(raw.get("source_types", raw.get("source_type"))),
            source_ids=_tuple_value(raw.get("source_ids", raw.get("source_id"))),
            statuses=_tuple_value(raw.get("statuses", raw.get("status"))),
            minimum_authority=(
                int(raw["minimum_authority"])
                if raw.get("minimum_authority") is not None
                else None
            ),
            volatile=(bool(raw["volatile"]) if raw.get("volatile") is not None else None),
            demo=(bool(raw["demo"]) if raw.get("demo") is not None else False),
            retrieval_policies=_tuple_value(
                raw.get("retrieval_policies", raw.get("retrieval_policy"))
            ),
        )

    def matches(self, chunk: Chunk) -> bool:
        return (
            chunk.scenic_id == self.scenic_id
            and (not self.categories or chunk.category in self.categories)
            and (not self.source_types or chunk.source_type in self.source_types)
            and (not self.source_ids or chunk.source_id in self.source_ids)
            and (not self.statuses or chunk.status in self.statuses)
            and (
                self.minimum_authority is None
                or chunk.authority_level >= self.minimum_authority
            )
            and (self.volatile is None or chunk.volatile is self.volatile)
            and (self.demo is None or chunk.demo is self.demo)
            and (
                not self.retrieval_policies
                or chunk.retrieval_policy in self.retrieval_policies
            )
        )

    def labels(self) -> tuple[str, ...]:
        labels = [f"scenic_id={self.scenic_id}"]
        if self.demo is not None:
            labels.append(f"demo={str(self.demo).lower()}")
        for name in (
            "categories",
            "source_types",
            "source_ids",
            "statuses",
            "retrieval_policies",
        ):
            selected = getattr(self, name)
            if selected:
                labels.append(f"{name}={','.join(selected)}")
        if self.minimum_authority is not None:
            labels.append(f"minimum_authority={self.minimum_authority}")
        if self.volatile is not None:
            labels.append(f"volatile={str(self.volatile).lower()}")
        return tuple(labels)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    keyword_candidate_k: int = 20
    vector_candidate_k: int = 20
    final_top_k: int = 8
    rrf_k: int = 60
    max_parents_per_source: int = 2
    category_boost: float = 0.12
    include_parent: bool = True

    def validate(self) -> None:
        if min(
            self.keyword_candidate_k,
            self.vector_candidate_k,
            self.final_top_k,
            self.rrf_k,
            self.max_parents_per_source,
        ) <= 0:
            raise ValueError("retrieval counts and rrf_k must be positive")
        if self.category_boost < 0:
            raise ValueError("category_boost cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    original: str
    normalized: str
    expanded_query: str
    expanded_terms: tuple[str, ...] = ()
    category_hints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChannelCandidate:
    chunk: Chunk
    channel: str
    rank: int
    score: float
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    hits: tuple[SearchHit, ...]
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [item.to_dict() for item in self.hits],
            "trace": self.trace,
        }


class TextTokenizer(Protocol):
    def tokenize(self, text: str) -> list[str]: ...


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...
