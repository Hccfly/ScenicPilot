from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.knowledge.retrieval import (
    DeterministicHashingEmbedding,
    HybridRetriever,
    SentenceTransformerEmbedding,
)
from scenic_pilot.knowledge.retrieval.evaluation import (
    evaluate_retriever,
    load_retrieval_cases,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate answer-context token budgets on one persisted index"
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "retrieval_cases.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "context_budget_ablation.json",
    )
    parser.add_argument(
        "--budgets",
        type=int,
        nargs="+",
        default=[1200, 1600, 2000, 2200, 2400, 2800],
    )
    args = parser.parse_args()

    manifest = json.loads((args.index / "manifest.json").read_text(encoding="utf-8"))
    model_id = manifest["embedding_model"]
    embedding = (
        DeterministicHashingEmbedding(int(model_id.rsplit("-", 1)[-1]))
        if model_id.startswith("hashing-v1-")
        else SentenceTransformerEmbedding(
            model_id,
            local_files_only=True,
            revision=manifest.get("embedding_revision"),
        )
    )
    retriever = HybridRetriever.load(args.index, embedding=embedding)
    cases = load_retrieval_cases(args.cases)
    context_limit = Settings().answer_context_limit
    runs: list[dict[str, Any]] = []
    for budget in sorted(set(args.budgets)):
        evaluation = evaluate_retriever(
            retriever,
            cases,
            answer_context_k=context_limit,
            max_context_tokens=budget,
        )
        metrics = evaluation["metrics"]
        runs.append({"context_limit": context_limit, "token_budget": budget, **metrics})
        print(
            f"budget={budget} evidence={metrics['evidence_completeness']:.3f} "
            f"average_tokens={metrics['average_estimated_context_tokens']:.1f} "
            f"exhausted={metrics['context_budget_exhausted_case_count']}",
            flush=True,
        )

    maximum_exhausted = math.floor(len(cases) * 0.05)
    passing = [
        run
        for run in runs
        if run["evidence_completeness"] == 1.0
        and run["context_budget_exhausted_case_count"] <= maximum_exhausted
        and run["cross_scenic_leakage"] == 0
        and run["demo_leakage"] == 0
        and run["forbidden_hit_count"] == 0
    ]
    recommendation = min(
        passing or runs,
        key=lambda run: (
            run["token_budget"] if passing else -run["evidence_completeness"],
            run["average_estimated_context_tokens"],
        ),
    )
    report = {
        "schema_version": "scenicpilot-context-budget-ablation-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "method": {
            "answer_context_limit": context_limit,
            "required_evidence_completeness": 1.0,
            "maximum_budget_exhausted_rate": 0.05,
            "token_metric": "deterministic Unicode estimate; not model billing tokens",
            "scope": "single-source minimally decisive evidence; multi-source answer budgeting remains an end-to-end evaluation concern",
        },
        "runs": runs,
        "recommendation": recommendation,
        "passes_policy": bool(passing),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(recommendation, ensure_ascii=False, indent=2))
    print(f"report={args.output}")


if __name__ == "__main__":
    main()
