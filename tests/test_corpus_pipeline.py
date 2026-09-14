from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.pipeline import (
    CorpusPipeline,
    PipelineConfig,
    PipelineValidationError,
)
from scenic_pilot.models import SourceDocument


SOURCE_PATH = (
    PROJECT_ROOT / "data" / "curated" / "zhuhai_chimelong_sources.jsonl"
)


def build(output_dir: Path, *, provider=None):
    return CorpusPipeline(
        PipelineConfig(source_path=SOURCE_PATH, output_dir=output_dir),
        provider=provider,
    ).run()


def test_pipeline_builds_reproducible_artifacts(tmp_path: Path) -> None:
    first = build(tmp_path / "first")
    second = build(tmp_path / "second")

    assert first.corpus_fingerprint == second.corpus_fingerprint
    assert len(first.documents) == 50
    assert len(first.chunks) >= 50
    for filename in (
        "documents.jsonl",
        "chunks.jsonl",
        "facts.jsonl",
        "images.jsonl",
        "hybrid_index.json",
        "build_manifest.json",
    ):
        assert (first.output_dir / filename).read_bytes() == (
            second.output_dir / filename
        ).read_bytes()


def test_chunk_inherits_source_metadata_and_offsets(tmp_path: Path) -> None:
    result = build(tmp_path / "output")
    chunk = result.chunks[0]
    source = next(item for item in result.documents if item.source_id == chunk.source_id)

    assert chunk.scenic_id == source.scenic_id
    assert chunk.source_url == source.source_url
    assert chunk.verified_at == source.verified_at
    assert chunk.retrieval_policy == source.retrieval_policy
    assert chunk.content_sha256 == source.content_sha256
    assert source.content[chunk.char_start : chunk.char_end] == chunk.content


def test_provider_is_an_injected_boundary(tmp_path: Path) -> None:
    class PrefixProvider:
        def __init__(self) -> None:
            self.calls = 0

        def materialize(self, source: SourceDocument) -> SourceDocument:
            self.calls += 1
            return replace(source, content=f"正文提取结果。{source.content}")

    provider = PrefixProvider()
    result = build(tmp_path / "output", provider=provider)

    assert provider.calls == 50
    assert all(item.content.startswith("正文提取结果。") for item in result.documents)


def test_pipeline_rejects_invalid_volatile_policy(tmp_path: Path) -> None:
    source_path = tmp_path / "invalid.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "source_id": "invalid",
                "scenic_id": "zhuhai-chimelong",
                "title": "invalid",
                "category": "show",
                "content": "content",
                "source_url": "https://example.test/source",
                "authority_level": 3,
                "verified_at": "2026-09-09",
                "volatile": True,
                "retrieval_policy": "cached_ok",
            }
        ),
        encoding="utf-8",
    )
    pipeline = CorpusPipeline(
        PipelineConfig(source_path=source_path, output_dir=tmp_path / "output")
    )

    with pytest.raises(PipelineValidationError, match="live_check_required"):
        pipeline.run()


def test_manifest_contains_no_nondeterministic_timestamp(tmp_path: Path) -> None:
    result = build(tmp_path / "output")
    manifest = json.loads(
        (result.output_dir / "build_manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["schema_version"] == "scenicpilot-corpus-v2"
    assert manifest["document_count"] == 50
    assert manifest["primary_document_count"] == 50
    assert manifest["discovered_document_count"] == 0
    assert manifest["parent_chunk_count"] == 50
    assert manifest["child_chunk_count"] == 50
    assert manifest["indexed_chunk_count"] == 50
    assert "generated_at" not in manifest


def test_source_filter_limits_live_smoke_scope(tmp_path: Path) -> None:
    result = CorpusPipeline(
        PipelineConfig(
            source_path=SOURCE_PATH,
            output_dir=tmp_path / "output",
            source_ids=("ocean-overview",),
        )
    ).run()

    assert [item.source_id for item in result.documents] == ["ocean-overview"]


def test_pipeline_accepts_provider_discovered_documents(tmp_path: Path) -> None:
    class ExpandingProvider:
        def materialize_many(
            self, source: SourceDocument
        ) -> tuple[SourceDocument, ...]:
            primary = replace(source, content=f"实时正文。{source.content}")
            if source.source_id != "resort-traffic":
                return (primary,)
            child = replace(
                source,
                source_id="resort-traffic-type-1",
                title="省外旅客交通指南",
                source_url="https://www.chimelong.com/zh/traffic/type_1.html",
                content="省外旅客交通详情。",
                discovered_from_source_id=source.source_id,
            )
            return primary, child

    result = build(tmp_path / "output", provider=ExpandingProvider())
    manifest = json.loads(
        (result.output_dir / "build_manifest.json").read_text(encoding="utf-8")
    )

    assert len(result.documents) == 51
    assert manifest["primary_document_count"] == 50
    assert manifest["discovered_document_count"] == 1
