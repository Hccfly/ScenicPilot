from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scenic_pilot.knowledge.chunking import chunk_documents, normalize_structured_text
from scenic_pilot.knowledge.facts import FactExtractor, FactRecord, RuleBasedFactExtractor
from scenic_pilot.knowledge.index import HybridIndex
from scenic_pilot.knowledge.ingestion import load_documents
from scenic_pilot.knowledge.sources import DocumentProvider, ManifestDocumentProvider
from scenic_pilot.models import Chunk, SourceDocument


SCHEMA_VERSION = "scenicpilot-corpus-v2"


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    source_path: Path
    output_dir: Path
    scenic_id: str = "zhuhai-chimelong"
    chunk_size: int = 640
    overlap: int = 128
    parent_chunk_size: int = 960
    chunking_strategy: str = "heading-parent-child-v2"
    source_ids: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self.overlap < 0 or self.overlap >= self.chunk_size:
            raise ValueError("overlap must be between 0 and chunk_size")
        if self.parent_chunk_size < self.chunk_size:
            raise ValueError("parent_chunk_size must be >= chunk_size")
        if not self.chunking_strategy:
            raise ValueError("chunking_strategy cannot be empty")


@dataclass(frozen=True, slots=True)
class BuildResult:
    documents: tuple[SourceDocument, ...]
    chunks: tuple[Chunk, ...]
    facts: tuple[FactRecord, ...]
    images: tuple[dict[str, Any], ...]
    corpus_fingerprint: str
    output_dir: Path


class PipelineValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_document(document: SourceDocument) -> list[str]:
    errors: list[str] = []
    prefix = document.source_id or "<missing-source-id>"
    if not document.source_id.strip():
        errors.append("source_id cannot be empty")
    if not document.title.strip():
        errors.append(f"{prefix}: title cannot be empty")
    if not document.content.strip():
        errors.append(f"{prefix}: content cannot be empty")
    if not document.scenic_id.strip():
        errors.append(f"{prefix}: scenic_id cannot be empty")
    parsed = urlparse(document.source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors.append(f"{prefix}: source_url must be an absolute HTTP URL")
    if not 0 <= document.authority_level <= 3:
        errors.append(f"{prefix}: authority_level must be between 0 and 3")
    if document.verified_at:
        try:
            date.fromisoformat(document.verified_at)
        except ValueError:
            errors.append(f"{prefix}: verified_at must use YYYY-MM-DD")
    if document.volatile and document.retrieval_policy != "live_check_required":
        errors.append(f"{prefix}: volatile content requires live_check_required")
    return errors


def normalize_document(document: SourceDocument) -> SourceDocument:
    content = normalize_structured_text(document.content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return replace(
        document,
        content=content,
        content_sha256=digest,
        document_version=f"v-{digest[:12]}",
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(documents: list[SourceDocument], config: PipelineConfig) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "scenic_id": config.scenic_id,
        "chunk_size": config.chunk_size,
        "overlap": config.overlap,
        "parent_chunk_size": config.parent_chunk_size,
        "chunking_strategy": config.chunking_strategy,
        "source_ids": list(config.source_ids),
        "documents": [
            {
                "source_id": document.source_id,
                "content_sha256": document.content_sha256,
                "source_url": document.source_url,
                "category": document.category,
                "authority_level": document.authority_level,
                "volatile": document.volatile,
                "retrieval_policy": document.retrieval_policy,
            }
            for document in documents
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    lines = [_canonical_json(value) for value in values]
    _write_text_atomic(path, "\n".join(lines) + "\n")


class CorpusPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        provider: DocumentProvider | None = None,
        fact_extractor: FactExtractor | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.provider = provider or ManifestDocumentProvider()
        self.fact_extractor = fact_extractor or RuleBasedFactExtractor()

    def run(self) -> BuildResult:
        source_documents = load_documents(self.config.source_path)
        if self.config.source_ids:
            requested = set(self.config.source_ids)
            source_documents = [
                document for document in source_documents if document.source_id in requested
            ]
            missing = requested - {document.source_id for document in source_documents}
            if missing:
                raise PipelineValidationError(
                    [f"unknown source_id: {source_id}" for source_id in sorted(missing)]
                )
        documents: list[SourceDocument] = []
        materialize_many = getattr(self.provider, "materialize_many", None)
        for source_document in source_documents:
            materialized = (
                materialize_many(source_document)
                if callable(materialize_many)
                else (self.provider.materialize(source_document),)
            )
            documents.extend(normalize_document(document) for document in materialized)
        documents.sort(key=lambda item: item.source_id)

        errors: list[str] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        for document in documents:
            errors.extend(validate_document(document))
            if document.scenic_id != self.config.scenic_id:
                errors.append(
                    f"{document.source_id}: expected scenic_id={self.config.scenic_id}"
                )
            if document.source_id in seen_ids:
                errors.append(f"duplicate source_id: {document.source_id}")
            seen_ids.add(document.source_id)
            if document.source_url in seen_urls:
                errors.append(f"duplicate source_url: {document.source_url}")
            seen_urls.add(document.source_url)
        if errors:
            raise PipelineValidationError(errors)

        chunks = chunk_documents(
            documents,
            chunk_size=self.config.chunk_size,
            overlap=self.config.overlap,
            strategy_version=self.config.chunking_strategy,
            parent_size=self.config.parent_chunk_size,
        )
        facts = [
            fact
            for document in documents
            for fact in self.fact_extractor.extract(document)
        ]
        images = [
            image
            for document in documents
            for image in document.image_records
        ]
        corpus_fingerprint = _fingerprint(documents, self.config)
        self._write_artifacts(documents, chunks, facts, images, corpus_fingerprint)
        return BuildResult(
            documents=tuple(documents),
            chunks=tuple(chunks),
            facts=tuple(facts),
            images=tuple(images),
            corpus_fingerprint=corpus_fingerprint,
            output_dir=self.config.output_dir,
        )

    def _write_artifacts(
        self,
        documents: list[SourceDocument],
        chunks: list[Chunk],
        facts: list[FactRecord],
        images: list[dict[str, Any]],
        corpus_fingerprint: str,
    ) -> None:
        output_dir = self.config.output_dir
        _write_jsonl(output_dir / "documents.jsonl", [item.to_dict() for item in documents])
        _write_jsonl(output_dir / "chunks.jsonl", [item.to_dict() for item in chunks])
        _write_jsonl(output_dir / "facts.jsonl", [item.to_dict() for item in facts])
        _write_jsonl(output_dir / "images.jsonl", images)
        indexed_chunks = [item for item in chunks if item.level == "child"]
        HybridIndex(chunks).dump(output_dir / "hybrid_index.json")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "scenic_id": self.config.scenic_id,
            "source_path": self.config.source_path.name,
            "document_count": len(documents),
            "primary_document_count": sum(
                item.discovered_from_source_id is None for item in documents
            ),
            "discovered_document_count": sum(
                item.discovered_from_source_id is not None for item in documents
            ),
            "chunk_count": len(chunks),
            "parent_chunk_count": sum(item.level == "parent" for item in chunks),
            "child_chunk_count": sum(item.level == "child" for item in chunks),
            "indexed_chunk_count": len(indexed_chunks),
            "fact_count": len(facts),
            "image_count": len(images),
            "low_extraction_quality_document_count": sum(
                item.extraction_quality is not None
                and item.extraction_quality < 0.36
                and not (
                    (
                        (
                            item.extraction_method.startswith("chimelong_detail_selector")
                            and item.extraction_quality > 0
                        )
                        or (
                            item.extraction_method.startswith("chimelong_directory_selector")
                            and item.schema_complete is True
                        )
                    )
                    and (item.topic_purity_score or 0) >= 0.85
                )
                for item in documents
            ),
            "low_topic_purity_document_count": sum(
                item.topic_purity_score is not None and item.topic_purity_score < 0.85
                for item in documents
            ),
            "purity_flagged_document_count": sum(
                bool(item.contamination_flags) for item in documents
            ),
            "related_content_document_count": sum(
                any(flag.startswith("related_") for flag in item.contamination_flags)
                for item in documents
            ),
            "navigation_residue_document_count": sum(
                any(flag.startswith("navigation:") for flag in item.contamination_flags)
                for item in documents
            ),
            "navigation_cleanup_document_count": sum(
                bool(item.cleanup_flags) for item in documents
            ),
            "navigation_boundary_cleaned_document_count": sum(
                any(
                    flag.startswith("navigation_boundary_cleaned:")
                    for flag in item.cleanup_flags
                )
                for item in documents
            ),
            "schema_incomplete_document_count": sum(
                item.schema_complete is False for item in documents
            ),
            "structure_flagged_document_count": sum(
                bool(item.structure_flags) for item in documents
            ),
            "supplemented_directory_document_count": sum(
                any(flag.startswith("supplemented_entry:") for flag in item.structure_flags)
                for item in documents
            ),
            "discovered_link_count": sum(
                len(item.discovered_links) for item in documents
            ),
            "missing_image_src_count": sum(bool(item.get("missing_src")) for item in images),
            "missing_image_alt_count": sum(bool(item.get("missing_alt")) for item in images),
            "chunking": {
                "strategy": self.config.chunking_strategy,
                "chunk_size": self.config.chunk_size,
                "overlap": self.config.overlap,
                "parent_chunk_size": self.config.parent_chunk_size,
            },
            "corpus_fingerprint": corpus_fingerprint,
            "source_fingerprints": {
                item.source_id: item.content_sha256 for item in documents
            },
        }
        _write_text_atomic(
            output_dir / "build_manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
