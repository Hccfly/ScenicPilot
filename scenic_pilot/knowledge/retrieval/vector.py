from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from scenic_pilot.models import Chunk

from .contracts import ChannelCandidate, EmbeddingProvider, NormalizedQuery
from .keyword import searchable_text


class DeterministicHashingEmbedding:
    """Offline test baseline. Production should use SentenceTransformerEmbedding."""

    def __init__(self, dimensions: int = 96) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"hashing-v1-{self.dimensions}"

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        latin = re.findall(r"[a-z0-9]+", text.lower())
        chinese = re.findall(r"[\u4e00-\u9fff]", text)
        runs = re.findall(r"[\u4e00-\u9fff]+", text)
        bigrams = [run[index : index + 2] for run in runs for index in range(len(run) - 1)]
        for token in (*latin, *chinese, *bigrams):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(item * item for item in vector))
        return [item / norm for item in vector] if norm else vector

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        return self._embed(text)


class SentenceTransformerEmbedding:
    DEFAULT_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        *,
        device: str = "cpu",
        query_instruction: str = DEFAULT_QUERY_INSTRUCTION,
        local_files_only: bool = False,
        revision: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.query_instruction = query_instruction
        self.local_files_only = local_files_only
        self.revision = revision
        self._model: Any | None = None

    @property
    def model_id(self) -> str:
        return self.model_name

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise RuntimeError(
                    'sentence-transformers is required; install ".[retrieval]"'
                ) from error
            self._model = SentenceTransformer(
                self.model_name,
                device=self.device,
                local_files_only=self.local_files_only,
                revision=self.revision,
            )
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        return self._load().encode(
            list(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    def embed_query(self, text: str) -> Sequence[float]:
        return self._load().encode(
            [f"{self.query_instruction}{text}"],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]


class CachingEmbeddingProvider:
    """Reuse exact document and query embeddings across ablation configurations."""

    def __init__(self, base: EmbeddingProvider) -> None:
        self.base = base
        self._document_cache: dict[str, Sequence[float]] = {}
        self._query_cache: dict[str, Sequence[float]] = {}

    @property
    def model_id(self) -> str:
        return self.base.model_id

    @property
    def revision(self) -> str | None:
        return getattr(self.base, "revision", None)

    @property
    def query_instruction(self) -> str | None:
        return getattr(self.base, "query_instruction", None)

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        missing = list(dict.fromkeys(text for text in texts if text not in self._document_cache))
        if missing:
            embedded = self.base.embed_documents(missing)
            self._document_cache.update(zip(missing, embedded))
        return [self._document_cache[text] for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        if text not in self._query_cache:
            self._query_cache[text] = self.base.embed_query(text)
        return self._query_cache[text]

    @property
    def cache_stats(self) -> dict[str, int]:
        return {
            "document_embeddings": len(self._document_cache),
            "query_embeddings": len(self._query_cache),
        }


class NumpyVectorRetriever:
    def __init__(
        self,
        chunks: Iterable[Chunk],
        *,
        embedding: EmbeddingProvider,
        vectors: Sequence[Sequence[float]] | None = None,
    ) -> None:
        try:
            import numpy as np
        except ImportError as error:
            raise RuntimeError("numpy is required for vector retrieval") from error
        self._np = np
        self.chunks = list(chunks)
        self.embedding = embedding
        if vectors is None:
            vectors = embedding.embed_documents(
                [searchable_text(chunk) for chunk in self.chunks]
            )
        matrix = np.asarray(vectors, dtype=np.float32)
        if not self.chunks:
            matrix = np.empty((0, 0), dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(self.chunks):
            raise ValueError("vector matrix must have one row per chunk")
        if matrix.size:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.where(norms == 0, 1.0, norms)
        self.vectors = matrix

    @property
    def dimensions(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.ndim == 2 else 0

    def search(
        self,
        query: NormalizedQuery,
        *,
        allowed_chunk_ids: set[str],
        top_k: int,
    ) -> list[ChannelCandidate]:
        if not self.chunks or not allowed_chunk_ids:
            return []
        vector = self._np.asarray(
            self.embedding.embed_query(query.normalized), dtype=self._np.float32
        )
        if vector.ndim != 1 or vector.shape[0] != self.dimensions:
            raise ValueError("query vector dimensions do not match index")
        norm = self._np.linalg.norm(vector)
        if norm:
            vector = vector / norm
        scores = self.vectors @ vector
        ranked = sorted(
            (
                (self.chunks[index], float(score))
                for index, score in enumerate(scores)
                if self.chunks[index].chunk_id in allowed_chunk_ids
            ),
            key=lambda item: (-item[1], -item[0].authority_level, item[0].chunk_id),
        )[:top_k]
        return [
            ChannelCandidate(
                chunk=chunk,
                channel="vector",
                rank=rank,
                score=round(score, 6),
            )
            for rank, (chunk, score) in enumerate(ranked, 1)
        ]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as handle:
            self._np.save(handle, self.vectors, allow_pickle=False)
        temporary.replace(path)

    @staticmethod
    def load_vectors(path: Path) -> Any:
        try:
            import numpy as np
        except ImportError as error:
            raise RuntimeError("numpy is required for vector retrieval") from error
        return np.load(path, allow_pickle=False)
