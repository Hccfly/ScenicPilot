from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.attempt_audit import (
    load_attempt_events,
    summarize_attempt_events,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate extraction attempt failures by page type and selector"
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "raw"
            / "zhuhai-chimelong"
            / "extraction_attempts.jsonl"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            PROJECT_ROOT
            / "data"
            / "raw"
            / "zhuhai-chimelong"
            / "extraction_attempt_summary.json"
        ),
    )
    parser.add_argument("--run-id")
    parser.add_argument("--all-runs", action="store_true")
    args = parser.parse_args()

    events = load_attempt_events(args.events)
    if not events:
        raise ValueError(f"no extraction attempt events found in {args.events}")
    run_id = None if args.all_runs else (args.run_id or events[-1].run_id)
    selected = events if run_id is None else [event for event in events if event.run_id == run_id]
    if not selected:
        raise ValueError(f"no extraction attempt events found for run_id={run_id}")

    summary = summarize_attempt_events(selected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary={args.output}")


if __name__ == "__main__":
    main()
