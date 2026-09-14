from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.retrieval import (
    DeterministicHashingEmbedding,
    HybridRetriever,
    RetrievalConfig,
    SentenceTransformerEmbedding,
)
from scenic_pilot.models import Chunk


DEFAULT_BGE_REVISION = "7999e1d3359715c523056ef9478215996d62a620"


def load_chunks(path: Path) -> list[Chunk]:
    return [
        Chunk(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the 混合检索阶段 hybrid retrieval index")
    parser.add_argument(
        "--chunks",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "chunks.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval",
    )
    parser.add_argument(
        "--embedding",
        choices=("sentence-transformer", "hashing"),
        default="sentence-transformer",
    )
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--revision", default=DEFAULT_BGE_REVISION)
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args()

    embedding = (
        SentenceTransformerEmbedding(
            args.model,
            revision=args.revision,
            local_files_only=args.local_files_only,
        )
        if args.embedding == "sentence-transformer"
        else DeterministicHashingEmbedding()
    )
    chunks = load_chunks(args.chunks)
    retriever = HybridRetriever(chunks, embedding=embedding, config=RetrievalConfig())
    corpus_manifest = args.chunks.parent / "build_manifest.json"
    fingerprint = None
    if corpus_manifest.exists():
        fingerprint = json.loads(corpus_manifest.read_text(encoding="utf-8")).get(
            "corpus_fingerprint"
        )
    manifest = retriever.dump(args.output, corpus_fingerprint=fingerprint)
    print(
        f"built child_vectors={len(retriever.indexed_chunks)} "
        f"model={embedding.model_id} manifest={manifest}"
    )


if __name__ == "__main__":
    main()
