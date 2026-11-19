from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.evaluation import EVALUATION_GROUP_COUNTS, OfflineEvaluator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated, offline-only evaluation matrix."
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=PROJECT_ROOT / "data" / "eval" / "offline_cases.jsonl",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong" / "retrieval",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "offline_evaluation.json",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run one case ID; repeat to select multiple cases.",
    )
    parser.add_argument(
        "--group",
        action="append",
        choices=tuple(EVALUATION_GROUP_COUNTS),
        default=[],
        help="Run one group; repeat to select multiple groups.",
    )
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--list", action="store_true", help="List the 100 cases.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    evaluator = OfflineEvaluator(catalog_path=args.catalog, index_path=args.index)
    if args.list:
        for case in evaluator.cases:
            print(f"{case.case_id}\t{case.group}\t{case.description}")
        return
    report = evaluator.evaluate(
        case_ids=args.case_id,
        groups=args.group,
        fail_fast=args.fail_fast,
    )
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for group, summary in report["groups"].items():
        print(
            f"{group}: {summary['passed_count']}/{summary['case_count']} "
            f"failed={summary['failed_case_ids']}"
        )
    print(f"report={output}")
    if not report["summary"]["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
