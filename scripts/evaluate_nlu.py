from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.agent.services import IntentClassifier
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.llm import build_query_understanding_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure intent/slot accuracy on the NLU paraphrase set."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "nlu_cases.jsonl",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="Let the configured model fill slots; needs SCENICPILOT_NLU_MODEL_ENABLED=1.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "nlu_evaluation.json",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Exit with status 1 when combined accuracy is below this value.",
    )
    return parser.parse_args()


def load_cases(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _value_matches(expected: object, actual: object) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) < 1e-6
    return expected == actual


def evaluate(classifier: IntentClassifier, cases: list[dict[str, object]]) -> dict:
    results: list[dict[str, object]] = []
    for case in cases:
        query = str(case["query"])
        intent, constraints, live, trace = classifier.understand(query)
        expected_constraints = case.get("expected_constraints") or {}
        constraint_misses = {
            key: {"expected": value, "actual": constraints.get(key)}
            for key, value in expected_constraints.items()
            if not _value_matches(value, constraints.get(key))
        }
        intent_ok = intent == case["expected_intent"]
        live_ok = live is bool(case["expected_live_data"])
        constraint_ok = not constraint_misses
        results.append(
            {
                "case_id": case["case_id"],
                "query": query,
                "expected_intent": case["expected_intent"],
                "actual_intent": intent,
                "expected_live_data": case["expected_live_data"],
                "actual_live_data": live,
                "constraint_misses": constraint_misses,
                "intent_ok": intent_ok,
                "live_ok": live_ok,
                "constraint_ok": constraint_ok,
                "combined_ok": intent_ok and live_ok and constraint_ok,
                "nlu_trace": trace,
            }
        )
    total = len(results) or 1
    return {
        "case_count": len(results),
        "intent_accuracy": sum(item["intent_ok"] for item in results) / total,
        "live_accuracy": sum(item["live_ok"] for item in results) / total,
        "constraint_accuracy": sum(item["constraint_ok"] for item in results) / total,
        "combined_accuracy": sum(item["combined_ok"] for item in results) / total,
        "cases": results,
    }


def main() -> None:
    args = parse_args()
    settings = Settings.from_env()
    client = build_query_understanding_client(settings) if args.with_model else None
    classifier = IntentClassifier(understanding_client=client)
    report = evaluate(classifier, load_cases(args.cases))
    mode = "rule+model" if args.with_model else "rule"
    report["mode"] = mode
    if client is not None:
        probe = client.understand(query="探测模型是否可用")
        if probe.get("status") != "ok":
            report["model_degradation_reason"] = probe.get("degradation_reason")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "mode",
                    "case_count",
                    "intent_accuracy",
                    "live_accuracy",
                    "constraint_accuracy",
                    "combined_accuracy",
                )
                if key in report
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    misses = [item for item in report["cases"] if not item["combined_ok"]]
    for item in misses:
        print(
            f"miss {item['case_id']}: {item['query']} "
            f"intent={item['actual_intent']}/{item['expected_intent']} "
            f"live={item['actual_live_data']}/{item['expected_live_data']} "
            f"constraints={item['constraint_misses']}"
        )
    print(f"report={args.output}")
    if args.fail_under is not None and report["combined_accuracy"] < args.fail_under:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
