from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenic_pilot.config import PROJECT_ROOT
from scenic_pilot.knowledge.audit import audit_summary, load_audits


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a completed live extraction build")
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "index" / "zhuhai-chimelong",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "raw" / "zhuhai-chimelong",
    )
    parser.add_argument("--minimum-content-chars", type=int, default=80)
    args = parser.parse_args()

    audits = load_audits(
        args.index_dir,
        args.raw_dir,
        minimum_content_chars=args.minimum_content_chars,
    )
    audit_path = args.raw_dir / "extraction_audit.jsonl"
    summary_path = args.raw_dir / "extraction_audit_summary.json"
    audit_path.write_text(
        "\n".join(
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
            for item in audits
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = audit_summary(
        audits, minimum_content_chars=args.minimum_content_chars
    )
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"audit={audit_path}")
    print(f"summary={summary_path}")


if __name__ == "__main__":
    main()
