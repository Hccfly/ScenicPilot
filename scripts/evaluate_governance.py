from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scenic_pilot.agent.dependencies import default_dependencies
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.runtime import ScenicPilotRuntime


QUERY = "一家三口玩一天，预算 1000 元，怎么安排？"


def evaluate() -> dict[str, object]:
    settings = Settings(tool_mode="offline", llm_provider="offline")
    dependencies = default_dependencies(settings)
    with TemporaryDirectory(prefix="scenicpilot-governance-") as temporary:
        checkpoint_path = Path(temporary) / "runtime.sqlite3"
        first = ScenicPilotRuntime(dependencies, checkpoint_path=checkpoint_path)
        paused = first.start(
            QUERY,
            thread_id="restart-case",
            idempotency_key="start-1",
            require_approval=True,
        )
        before_tools = (paused.get("result") or {}).get("tool_call_count", 0)
        first.close()

        restored = ScenicPilotRuntime(
            dependencies,
            checkpoint_path=checkpoint_path,
        )
        recovered = restored.get_session("restart-case")
        approved = restored.resume(
            thread_id="restart-case",
            approved=True,
            idempotency_key="resume-1",
        )
        replayed = restored.resume(
            thread_id="restart-case",
            approved=True,
            idempotency_key="resume-1",
        )

        rejected_start = restored.start(
            QUERY,
            thread_id="reject-case",
            idempotency_key="start-2",
            require_approval=True,
        )
        rejected = restored.resume(
            thread_id="reject-case",
            approved=False,
            reason="offline governance evaluation",
            idempotency_key="resume-2",
        )
        restored.close()

    limited = ScenicPilotRuntime(
        default_dependencies(
            Settings(
                tool_mode="offline",
                llm_provider="offline",
                execution_tool_call_limit=1,
            )
        )
    ).invoke(QUERY)
    limited_results = limited.get("tool_results", [])

    checks = {
        "interrupt_before_tools": paused["status"] == "awaiting_approval"
        and before_tools == 0,
        "restart_recovers_interrupt": recovered["status"] == "awaiting_approval",
        "approval_completes": approved["status"] == "completed"
        and approved["result"]["approval_status"] == "approved",
        "idempotency_replays": replayed["idempotency_replayed"] is True
        and replayed["result"] == approved["result"],
        "rejection_has_no_tools": rejected_start["status"]
        == "awaiting_approval"
        and rejected["result"]["approval_status"] == "rejected"
        and rejected["result"]["tool_call_count"] == 0,
        "tool_budget_is_enforced": limited["tool_call_count"] == 1
        and any(
            (item.get("output") or {}).get("degradation_reason")
            == "call_budget_exceeded"
            for item in limited_results
        ),
        "bounded_revision_and_recursion": limited["revision_count"]
        <= settings.planning_max_revisions,
    }
    return {
        "schema_version": "scenicpilot-governance-eval-v1",
        "case_count": len(checks),
        "passed_count": sum(checks.values()),
        "pass_rate": sum(checks.values()) / len(checks),
        "checks": checks,
        "defaults": {
            "approval_mode": settings.approval_mode,
            "tool_call_limit": settings.execution_tool_call_limit,
            "max_revisions": settings.planning_max_revisions,
            "recursion_limit": settings.execution_recursion_limit,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate 会话治理阶段 governance.")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "index"
        / "zhuhai-chimelong"
        / "governance_eval.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = evaluate()
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
