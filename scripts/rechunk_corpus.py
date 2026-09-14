from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.chunking import chunk_documents
from scenic_pilot.knowledge.pipeline import PipelineConfig, _fingerprint
from scenic_pilot.models import SourceDocument


def load_documents(path: Path) -> list[SourceDocument]:
    return [
        SourceDocument.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild chunk artifacts from an existing reviewed documents.jsonl"
    )
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong",
    )
    parser.add_argument("--chunk-size", type=int, default=640)
    parser.add_argument("--overlap", type=int, default=128)
    parser.add_argument("--parent-chunk-size", type=int, default=960)
    parser.add_argument("--strategy", default="heading-parent-child-v2")
    args = parser.parse_args()

    documents_path = args.index_dir / "documents.jsonl"
    manifest_path = args.index_dir / "build_manifest.json"
    documents = load_documents(documents_path)
    previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = chunk_documents(
        documents,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        strategy_version=args.strategy,
        parent_size=args.parent_chunk_size,
    )
    config = PipelineConfig(
        source_path=Path(previous_manifest.get("source_path", documents_path.name)),
        output_dir=args.index_dir,
        scenic_id=str(previous_manifest.get("scenic_id", "zhuhai-chimelong")),
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        parent_chunk_size=args.parent_chunk_size,
        chunking_strategy=args.strategy,
    )
    config.validate()
    corpus_fingerprint = _fingerprint(documents, config)
    chunks_payload = "".join(
        json.dumps(chunk.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        for chunk in chunks
    )
    write_text_atomic(args.index_dir / "chunks.jsonl", chunks_payload)
    parent_count = sum(chunk.level == "parent" for chunk in chunks)
    child_count = sum(chunk.level != "parent" for chunk in chunks)
    manifest = {
        **previous_manifest,
        "chunk_count": len(chunks),
        "parent_chunk_count": parent_count,
        "child_chunk_count": child_count,
        "indexed_chunk_count": child_count,
        "chunking": {
            "chunk_size": args.chunk_size,
            "overlap": args.overlap,
            "parent_chunk_size": args.parent_chunk_size,
            "strategy": args.strategy,
        },
        "corpus_fingerprint": corpus_fingerprint,
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    print(
        f"rechunked documents={len(documents)} parents={parent_count} "
        f"children={child_count} fingerprint={corpus_fingerprint}"
    )


if __name__ == "__main__":
    main()
