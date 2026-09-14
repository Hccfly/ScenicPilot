from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.config import Settings
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
    parser = argparse.ArgumentParser(description="Evaluate the 混合检索阶段 hybrid retriever")
    parser.add_argument(
        "--index",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval",
    )
    defaults = Settings()
    parser.add_argument(
        "--answer-context-limit",
        type=int,
        default=defaults.answer_context_limit,
    )
    parser.add_argument(
        "--context-token-budget",
        type=int,
        default=defaults.answer_context_token_budget,
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "retrieval_cases.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval_eval.json",
    )
    args = parser.parse_args()

    manifest = json.loads((args.index / "manifest.json").read_text(encoding="utf-8"))
    model_id = manifest["embedding_model"]
    revision = manifest.get("embedding_revision")
    embedding = (
        DeterministicHashingEmbedding(int(model_id.rsplit("-", 1)[-1]))
        if model_id.startswith("hashing-v1-")
        else SentenceTransformerEmbedding(
            model_id, local_files_only=True, revision=revision
        )
    )
    retriever = HybridRetriever.load(args.index, embedding=embedding)
    report = evaluate_retriever(
        retriever,
        load_retrieval_cases(args.cases),
        answer_context_k=args.answer_context_limit,
        max_context_tokens=args.context_token_budget,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"report={args.output}")


if __name__ == "__main__":
    main()
