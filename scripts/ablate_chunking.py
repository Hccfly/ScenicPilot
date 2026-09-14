from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.retrieval import (
    CachingEmbeddingProvider,
    DeterministicHashingEmbedding,
    SentenceTransformerEmbedding,
)
from scenic_pilot.knowledge.retrieval.ablation import (
    reselect_chunk_ablation,
    run_chunk_ablation,
)
from scenic_pilot.knowledge.retrieval.evaluation import load_retrieval_cases
from scenic_pilot.models import SourceDocument


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_REVISION = "7999e1d3359715c523056ef9478215996d62a620"


def load_documents(path: Path) -> list[SourceDocument]:
    return [
        SourceDocument.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def progress(stage: str, run: dict[str, Any]) -> None:
    metrics = run["metrics"]
    print(
        f"[{stage:6}] {run['candidate_id']} "
        f"R@5={metrics['recall@5']:.3f} "
        f"MRR={metrics['mrr@10']:.3f} "
        f"child-evidence={metrics['child_evidence_recall@5']:.3f} "
        f"parent-evidence={metrics['evidence_completeness']:.3f} "
        f"tokens={metrics['average_estimated_context_tokens']:.1f} "
        f"duplicates={metrics['raw_parent_duplicate_rate']:.3f}",
        flush=True,
    )


def write_summary_csv(path: Path, report: dict[str, Any]) -> None:
    rows: dict[str, dict[str, Any]] = {}
    for stage_name in ("stage1", "stage2"):
        for run in report[stage_name]["runs"]:
            row = rows.setdefault(
                run["candidate_id"],
                {
                    "candidate_id": run["candidate_id"],
                    "stages": [],
                    **run["parameters"],
                    **run["corpus"],
                    **run["metrics"],
                },
            )
            row["stages"].append(stage_name)
    materialized = [
        {**row, "stages": ",".join(row["stages"])} for row in rows.values()
    ]
    fieldnames = list(materialized[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Two-stage child/parent chunk parameter ablation"
    )
    parser.add_argument(
        "--documents",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "documents.jsonl",
    )
    parser.add_argument(
        "--reuse-existing-results",
        action="store_true",
        help="reapply current decision gates to output-dir/report.json without embedding",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "retrieval_cases.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "chunk_ablation",
    )
    parser.add_argument(
        "--embedding",
        choices=("bge", "hashing"),
        default="bge",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument(
        "--strategy",
        choices=("heading-parent-child-v1", "heading-parent-child-v2"),
        default="heading-parent-child-v2",
    )
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="allow sentence-transformers to download a missing model",
    )
    args = parser.parse_args()

    report_path = args.output_dir / "report.json"
    if args.reuse_existing_results:
        report = reselect_chunk_ablation(
            json.loads(report_path.read_text(encoding="utf-8"))
        )
        report["reselected_at"] = datetime.now(UTC).isoformat()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        write_summary_csv(args.output_dir / "summary.csv", report)
        recommendation_path = args.output_dir / "recommended_config.json"
        recommendation_path.write_text(
            json.dumps(report["recommendation"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps(report["recommendation"], ensure_ascii=False, indent=2))
        print(f"report={report_path}")
        return

    base_embedding = (
        DeterministicHashingEmbedding()
        if args.embedding == "hashing"
        else SentenceTransformerEmbedding(
            args.model,
            local_files_only=not args.allow_download,
            revision=args.revision,
        )
    )
    embedding = CachingEmbeddingProvider(base_embedding)
    report = run_chunk_ablation(
        load_documents(args.documents),
        load_retrieval_cases(args.cases),
        embedding=embedding,
        strategy_version=args.strategy,
        on_result=progress,
    )
    report["generated_at"] = datetime.now(UTC).isoformat()
    report["embedding"] = {
        "model_id": embedding.model_id,
        "revision": embedding.revision,
        "query_instruction": embedding.query_instruction,
        "cache": embedding.cache_stats,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    write_summary_csv(args.output_dir / "summary.csv", report)
    recommendation_path = args.output_dir / "recommended_config.json"
    recommendation_path.write_text(
        json.dumps(report["recommendation"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report["recommendation"], ensure_ascii=False, indent=2))
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
