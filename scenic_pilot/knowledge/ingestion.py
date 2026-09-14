from __future__ import annotations

import json
from pathlib import Path

from scenic_pilot.knowledge.chunking import chunk_documents
from scenic_pilot.knowledge.index import Embedder, HybridIndex
from scenic_pilot.models import SourceDocument


def load_documents(path: Path) -> list[SourceDocument]:
    documents: list[SourceDocument] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            document = SourceDocument.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid document at {path}:{line_number}: {exc}") from exc
        if not document.source_id or not document.content or not document.source_url:
            raise ValueError(f"missing required field at {path}:{line_number}")
        documents.append(document)
    return documents


def build_index(path: Path, *, embedder: Embedder | None = None) -> HybridIndex:
    documents = load_documents(path)
    return HybridIndex(chunk_documents(documents), embedder=embedder)
