from __future__ import annotations

import argparse
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.attempt_audit import JsonlAttemptAuditSink
from scenic_pilot.knowledge.pipeline import CorpusPipeline, PipelineConfig
from scenic_pilot.knowledge.sources import (
    GzipPageArchive,
    HttpxPageFetcher,
    LayeredPageExtractor,
    LivePageDocumentProvider,
    PlaywrightPageFetcher,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build ScenicPilot corpus artifacts from reviewed summaries or live pages"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "data" / "curated" / "zhuhai_chimelong_sources.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong",
    )
    parser.add_argument("--chunk-size", type=int, default=640)
    parser.add_argument("--overlap", type=int, default=128)
    parser.add_argument("--parent-chunk-size", type=int, default=960)
    parser.add_argument("--strategy", default="heading-parent-child-v2")
    parser.add_argument("--mode", choices=("manifest", "live"), default="manifest")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument(
        "--browser-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use Playwright when HTTP extraction quality is insufficient in live mode",
    )
    parser.add_argument(
        "--raw-archive",
        type=Path,
        default=PROJECT_ROOT / "data" / "raw",
    )
    args = parser.parse_args()

    provider = None
    attempt_audit = None
    if args.mode == "live":
        audit_dir = args.raw_archive / "zhuhai-chimelong"
        attempt_audit = JsonlAttemptAuditSink(
            audit_dir / "extraction_attempts.jsonl"
        )
        provider = LivePageDocumentProvider(
            HttpxPageFetcher(),
            LayeredPageExtractor(),
            browser_fetcher=PlaywrightPageFetcher() if args.browser_fallback else None,
            archive=GzipPageArchive(args.raw_archive),
            attempt_audit=attempt_audit,
        )

    result = CorpusPipeline(
        PipelineConfig(
            source_path=args.source,
            output_dir=args.output,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            parent_chunk_size=args.parent_chunk_size,
            chunking_strategy=args.strategy,
            source_ids=tuple(args.source_id),
        ),
        provider=provider,
    ).run()
    print(
        f"built documents={len(result.documents)} chunks={len(result.chunks)} "
        f"facts={len(result.facts)} images={len(result.images)} "
        f"fingerprint={result.corpus_fingerprint} output={result.output_dir}"
    )
    if attempt_audit is not None:
        summary_path = (
            args.raw_archive
            / "zhuhai-chimelong"
            / "extraction_attempt_summary.json"
        )
        summary = attempt_audit.write_summary(summary_path)
        print(
            f"attempts={summary['attempt_count']} failed={summary['failed_count']} "
            f"run_id={attempt_audit.run_id} summary={summary_path}"
        )


if __name__ == "__main__":
    main()
