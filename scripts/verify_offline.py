from __future__ import annotations

import json

from scenic_pilot.runtime import ScenicPilotRuntime


def main() -> None:
    runtime = ScenicPilotRuntime()
    cases = [
        "珠海长隆有哪些主题内容？",
        "一家三口玩一天，预算 1000 元，怎么安排？",
        "明天准确票价是多少？",
    ]
    for case in cases:
        result = runtime.invoke(case)
        print(json.dumps({"query": case, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
