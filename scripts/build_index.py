from __future__ import annotations

import argparse
from pathlib import Path

from scenic_pilot.config import Settings
from scenic_pilot.knowledge.ingestion import build_index


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Build a deterministic ScenicPilot index")
    parser.add_argument("--input", type=Path, default=settings.data_path)
    parser.add_argument("--output", type=Path, default=Path("data/index/index.json"))
    args = parser.parse_args()
    index = build_index(args.input)
    index.dump(args.output)
    print(f"built {len(index.chunks)} chunks -> {args.output}")


if __name__ == "__main__":
    main()
