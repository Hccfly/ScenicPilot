from __future__ import annotations

import argparse
import json
from dataclasses import replace

from scenic_pilot.config import Settings
from scenic_pilot.tools.factory import build_tool_stack


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe ScenicPilot weather, distance and budget tools."
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "auto", "live"),
        help="Override SCENICPILOT_TOOL_MODE for this run.",
    )
    parser.add_argument("--date", help="Forecast date in YYYY-MM-DD format.")
    parser.add_argument("--location", default="珠海长隆度假区")
    parser.add_argument("--origin", default="珠海站")
    parser.add_argument("--destination", default="珠海长隆度假区")
    parser.add_argument("--party-size", type=int, default=3)
    parser.add_argument("--duration-minutes", type=int, default=480)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings.from_env()
    if args.mode:
        settings = replace(settings, tool_mode=args.mode)
    stack = build_tool_stack(settings)
    result = stack.suite.run(
        [
            {
                "request_id": "weather-1",
                "tool": "weather",
                "arguments": {"location": args.location, "date": args.date},
            },
            {
                "request_id": "distance-1",
                "tool": "distance",
                "arguments": {
                    "origin": args.origin,
                    "destination": args.destination,
                },
            },
            {
                "request_id": "budget-1",
                "tool": "budget",
                "arguments": {
                    "party_size": args.party_size,
                    "duration_minutes": args.duration_minutes,
                },
            },
        ]
    )
    result["mode"] = settings.tool_mode
    result["policy"] = stack.runner.policy.to_dict()
    result["audit_events"] = list(stack.runner.audit_events)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
