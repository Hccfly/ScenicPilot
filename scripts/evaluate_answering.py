from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.answering import AnswerPolicy, GroundedAnswerer
from scenic_pilot.answering.evaluation import evaluate_answering, load_refusal_cases
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.knowledge.retrieval import (
    DeterministicHashingEmbedding,
    HybridRetriever,
    SentenceTransformerEmbedding,
)
from scenic_pilot.knowledge.retrieval.evaluation import load_retrieval_cases


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Evaluate 可信回答阶段 grounded answering")
    parser.add_argument(
        "--index",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval",
    )
    parser.add_argument(
        "--answerable-cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "retrieval_cases.jsonl",
    )
    parser.add_argument(
        "--refusal-cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "answer_refusal_cases.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "answer_eval.json",
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
    answerer = GroundedAnswerer(
        AnswerPolicy(
            min_keyword_score=settings.answer_min_keyword_score,
            min_specific_term_matches=settings.answer_min_specific_term_matches,
            min_specific_term_coverage=settings.answer_min_specific_term_coverage,
            minimum_authority_level=settings.answer_min_authority_level,
            max_claim_chars=settings.answer_max_claim_chars,
        )
    )
    report = evaluate_answering(
        retriever,
        answerer,
        load_retrieval_cases(args.answerable_cases),
        load_refusal_cases(args.refusal_cases),
        top_k=settings.top_k,
        min_score=settings.min_retrieval_score,
        context_limit=settings.answer_context_limit,
        max_context_tokens=settings.answer_context_token_budget,
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
