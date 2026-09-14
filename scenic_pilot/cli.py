from __future__ import annotations

import argparse
import json

from scenic_pilot.runtime import get_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="ScenicPilot CLI")
    parser.add_argument("message", nargs="?", help="问题或行程需求")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--thread-id", help="持久化会话 ID")
    parser.add_argument("--idempotency-key", help="本次操作的幂等键")
    parser.add_argument(
        "--require-approval",
        action="store_true",
        help="在执行行程工具前暂停并等待批准",
    )
    parser.add_argument(
        "--resume",
        choices=("approve", "reject"),
        help="恢复被暂停的会话",
    )
    parser.add_argument("--reason", default="", help="批准或拒绝原因")
    args = parser.parse_args()

    runtime = get_runtime()
    if args.resume:
        if not args.thread_id:
            parser.error("--resume requires --thread-id")
        envelope = runtime.resume(
            thread_id=args.thread_id,
            approved=args.resume == "approve",
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
    else:
        if not args.message:
            parser.error("message is required unless --resume is used")
        envelope = runtime.start(
            args.message,
            thread_id=args.thread_id,
            idempotency_key=args.idempotency_key,
            require_approval=args.require_approval,
        )
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return
    if envelope["status"] == "awaiting_approval":
        print(f"会话 {envelope['thread_id']} 已暂停，等待工具执行批准。")
        if envelope.get("interrupts"):
            print(envelope["interrupts"][0].get("question", "请确认是否继续。"))
        return
    if envelope["status"] != "completed" or envelope.get("result") is None:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return
    result = envelope["result"]
    print(result["answer"])
    if result.get("citations"):
        print("\n来源：")
        for index, citation in enumerate(result["citations"], 1):
            print(f"[{index}] {citation['title']} — {citation['url']}")
    print("\n轨迹：" + " -> ".join(result["trace"]))


if __name__ == "__main__":
    main()
