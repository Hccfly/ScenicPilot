from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from scenic_pilot.models import Chunk, SearchHit

from .context import ParentContextAssembler
from .contracts import (
    RETRIEVAL_SCHEMA_VERSION,
    EmbeddingProvider,
    RetrievalConfig,
    RetrievalFilters,
    RetrievalResponse,
)
from .fusion import ReciprocalRankFusion
from .keyword import BM25KeywordRetriever, ChineseDomainTokenizer
from .query import DomainQueryNormalizer
from .vector import NumpyVectorRetriever


class HybridRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        *,
        embedding: EmbeddingProvider,
        config: RetrievalConfig | None = None,
        vectors: Any | None = None,
    ) -> None:
        self.config = config or RetrievalConfig()
        self.config.validate()
        self.chunks = list(chunks)
        parents = {
            chunk.chunk_id: chunk for chunk in self.chunks if chunk.level == "parent"
        }
        child_chunks = [chunk for chunk in self.chunks if chunk.level != "parent"]
        self.indexed_chunks = [
            chunk
            for chunk in child_chunks
            if self._is_indexable_child(chunk, parents.get(chunk.parent_chunk_id or ""))
        ]
        self.excluded_non_substantive_chunks = len(child_chunks) - len(
            self.indexed_chunks
        )
        self.embedding = embedding
        self.normalizer = DomainQueryNormalizer()
        self.keyword = BM25KeywordRetriever(
            self.indexed_chunks, tokenizer=ChineseDomainTokenizer()
        )
        self.vector = NumpyVectorRetriever(
            self.indexed_chunks, embedding=embedding, vectors=vectors
        )
        self.fusion = ReciprocalRankFusion(self.config)
        self.context = ParentContextAssembler(self.chunks)

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: dict[str, object] | RetrievalFilters | None = None,
    ) -> list[SearchHit]:
        return list(self.search_with_trace(query, top_k=top_k, filters=filters).hits)

    def search_with_trace(
        self,
        query: str,
        *,
        top_k: int | None = None,
        filters: dict[str, object] | RetrievalFilters | None = None,
    ) -> RetrievalResponse:
        selected_top_k = self.config.final_top_k if top_k is None else top_k
        if selected_top_k <= 0 or not query.strip():
            return RetrievalResponse(hits=(), trace={"reason": "empty_query_or_top_k"})

        default_scenic = self.indexed_chunks[0].scenic_id if self.indexed_chunks else ""
        selected_filters = (
            filters
            if isinstance(filters, RetrievalFilters)
            else RetrievalFilters.from_mapping(filters, scenic_id=default_scenic)
        )
        allowed = {
            chunk.chunk_id for chunk in self.indexed_chunks if selected_filters.matches(chunk)
        }
        normalized = self.normalizer.normalize(query)
        keyword = self.keyword.search(
            normalized,
            allowed_chunk_ids=allowed,
            top_k=self.config.keyword_candidate_k,
        )
        vector = self.vector.search(
            normalized,
            allowed_chunk_ids=allowed,
            top_k=self.config.vector_candidate_k,
        )
        hits, drops = self.fusion.fuse(
            keyword,
            vector,
            category_hints=normalized.category_hints,
            applied_filters=selected_filters.labels(),
            top_k=selected_top_k,
        )
        hydrated: list[SearchHit] = []
        for hit in hits:
            context, expanded = self.context.context_for(
                hit.chunk, include_parent=self.config.include_parent
            )
            hydrated.append(
                replace(hit, context=context, context_expanded=expanded)
            )

        trace = {
            "schema_version": RETRIEVAL_SCHEMA_VERSION,
            "query": normalized.to_dict(),
            "filters": selected_filters.to_dict(),
            "applied_filters": list(selected_filters.labels()),
            "corpus_chunk_count": len(self.indexed_chunks),
            "excluded_non_substantive_chunk_count": self.excluded_non_substantive_chunks,
            "allowed_chunk_count": len(allowed),
            "metadata_filtered_count": len(self.indexed_chunks) - len(allowed),
            "keyword_candidate_count": len(keyword),
            "vector_candidate_count": len(vector),
            "fused_candidate_count": len({item.chunk.chunk_id for item in (*keyword, *vector)}),
            "drop_counts": drops,
            "config": self.config.to_dict(),
            "embedding_model": self.embedding.model_id,
            "hits": [
                {
                    "chunk_id": hit.chunk.chunk_id,
                    "source_id": hit.chunk.source_id,
                    "parent_chunk_id": hit.parent_chunk_id,
                    "keyword_rank": hit.keyword_rank,
                    "vector_rank": hit.vector_rank,
                    "keyword_score": hit.keyword_score,
                    "vector_score": hit.vector_score,
                    "fusion_score": hit.fusion_score,
                    "score": hit.score,
                    "matched_terms": list(hit.matched_terms),
                    "category_boost_applied": hit.category_boost_applied,
                    "context_expanded": hit.context_expanded,
                }
                for hit in hydrated
            ],
        }
        return RetrievalResponse(hits=tuple(hydrated), trace=trace)

    def dump(self, directory: Path, *, corpus_fingerprint: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        chunks_path = directory / "chunks.jsonl"
        chunks_payload = "".join(
            json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for chunk in self.chunks
        )
        chunks_temporary = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
        chunks_temporary.write_text(
            chunks_payload,
            encoding="utf-8",
            newline="\n",
        )
        chunks_temporary.replace(chunks_path)
        vectors_path = directory / "vectors.npy"
        self.vector.save(vectors_path)
        fingerprint = corpus_fingerprint or self._fingerprint()
        manifest = {
            "schema_version": RETRIEVAL_SCHEMA_VERSION,
            "corpus_fingerprint": fingerprint,
            "embedding_model": self.embedding.model_id,
            "embedding_revision": getattr(self.embedding, "revision", None),
            "query_instruction": getattr(self.embedding, "query_instruction", None),
            "embedding_dimensions": self.vector.dimensions,
            "chunk_count": len(self.chunks),
            "indexed_child_count": len(self.indexed_chunks),
            "config": self.config.to_dict(),
            "files": {"chunks": chunks_path.name, "vectors": vectors_path.name},
            "checksums": {
                "chunks_sha256": hashlib.sha256(chunks_payload.encode("utf-8")).hexdigest(),
                "vectors_sha256": hashlib.sha256(vectors_path.read_bytes()).hexdigest(),
            },
        }
        manifest_path = directory / "manifest.json"
        manifest_temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        manifest_temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest_temporary.replace(manifest_path)
        return manifest_path

    @classmethod
    def load(cls, directory: Path, *, embedding: EmbeddingProvider) -> "HybridRetriever":
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != RETRIEVAL_SCHEMA_VERSION:
            raise ValueError("unsupported retrieval index schema")
        if manifest.get("embedding_model") != embedding.model_id:
            raise ValueError(
                "embedding model does not match persisted retrieval index: "
                f"{manifest.get('embedding_model')} != {embedding.model_id}"
            )
        expected_revision = manifest.get("embedding_revision")
        if expected_revision and expected_revision != getattr(embedding, "revision", None):
            raise ValueError("embedding revision does not match persisted retrieval index")
        chunks_path = directory / manifest["files"]["chunks"]
        vectors_path = directory / manifest["files"]["vectors"]
        checksums = manifest.get("checksums", {})
        if checksums.get("chunks_sha256") != hashlib.sha256(
            chunks_path.read_bytes()
        ).hexdigest():
            raise ValueError("persisted retrieval chunks checksum mismatch")
        if checksums.get("vectors_sha256") != hashlib.sha256(
            vectors_path.read_bytes()
        ).hexdigest():
            raise ValueError("persisted retrieval vectors checksum mismatch")
        chunks = [
            Chunk(**json.loads(line))
            for line in chunks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        config = RetrievalConfig(**manifest["config"])
        vectors = NumpyVectorRetriever.load_vectors(
            vectors_path
        )
        return cls(chunks, embedding=embedding, config=config, vectors=vectors)

    def _fingerprint(self) -> str:
        payload = [
            (chunk.chunk_id, chunk.content_version or chunk.content_sha256)
            for chunk in self.chunks
        ]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _has_substantive_body(text: str) -> bool:
        without_headings = re.sub(r"(?m)^#{1,6}\s+.*?$", "", text)
        return bool(re.search(r"[0-9A-Za-z\u3400-\u9fff]", without_headings))

    @classmethod
    def _is_indexable_child(cls, child: Chunk, parent: Chunk | None) -> bool:
        if cls._has_substantive_body(child.content):
            return True
        return bool(parent is not None and cls._has_substantive_body(parent.content))
