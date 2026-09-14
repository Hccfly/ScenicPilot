from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol

from scenic_pilot.knowledge.retrieval import HybridRetriever, RetrievalConfig
from scenic_pilot.models import Chunk, SearchHit


def tokenize(text: str) -> list[str]:
    normalized = text.lower()
    latin = re.findall(r"[a-z0-9]+", normalized)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", normalized)
    stop_chars = set("的了是在和与或等为把被及于有无能否是否什么怎么哪些一个")
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.extend(character for character in run if character not in stop_chars)
        chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
    return latin + chinese


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """A deterministic offline baseline, not a semantic production embedding."""

    def __init__(self, dimensions: int = 96) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vector dimensions must match")
    return max(0.0, sum(a * b for a, b in zip(left, right, strict=True)))


class _LegacyEmbeddingAdapter:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    @property
    def model_id(self) -> str:
        dimensions = getattr(self.embedder, "dimensions", "unknown")
        return f"legacy-{type(self.embedder).__name__}-{dimensions}"

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self.embedder.embed(text) for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        return self.embedder.embed(text)


class HybridIndex:
    """Backward-compatible facade over the 混合检索阶段 BM25 + vector + RRF retriever."""

    def __init__(
        self,
        chunks: Iterable[Chunk] = (),
        *,
        embedder: Embedder | None = None,
        vector_weight: float = 0.55,
        retrieval_config: RetrievalConfig | None = None,
    ) -> None:
        if not 0 <= vector_weight <= 1:
            raise ValueError("vector_weight must be between 0 and 1")
        self.embedder = embedder or HashingEmbedder()
        self.vector_weight = vector_weight
        self.chunks = list(chunks)
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self._retriever = HybridRetriever(
            self.chunks,
            embedding=_LegacyEmbeddingAdapter(self.embedder),
            config=self.retrieval_config,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, object] | None = None,
    ) -> list[SearchHit]:
        return self._retriever.search(
            query, top_k=top_k, filters=self._compatible_filters(filters)
        )

    def search_with_trace(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, object] | None = None,
    ):
        return self._retriever.search_with_trace(
            query, top_k=top_k, filters=self._compatible_filters(filters)
        )

    def _compatible_filters(
        self, filters: dict[str, object] | None
    ) -> dict[str, object] | None:
        selected = dict(filters or {})
        if (
            "demo" not in selected
            and self.chunks
            and all(chunk.demo for chunk in self.chunks)
        ):
            selected["demo"] = True
        return selected or None

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "scenicpilot-hybrid-index-v2",
            "vector_weight": self.vector_weight,
            "retrieval_config": self.retrieval_config.to_dict(),
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path, *, embedder: Embedder | None = None) -> "HybridIndex":
        payload = json.loads(path.read_text(encoding="utf-8"))
        chunks = [Chunk(**item) for item in payload["chunks"]]
        config_value = payload.get("retrieval_config")
        config = RetrievalConfig(**config_value) if config_value else RetrievalConfig()
        return cls(
            chunks,
            embedder=embedder,
            vector_weight=payload.get("vector_weight", 0.55),
            retrieval_config=config,
        )
