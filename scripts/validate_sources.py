from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scenic_pilot.config import PROJECT_ROOT


DEFAULT_JSONL = PROJECT_ROOT / "data" / "curated" / "zhuhai_chimelong_sources.jsonl"
DEFAULT_CSV = PROJECT_ROOT / "data" / "source_plan.csv"
REQUIRED_FIELDS = {
    "source_id",
    "scenic_id",
    "title",
    "category",
    "content",
    "source_url",
    "authority_level",
    "verified_at",
    "volatile",
    "demo",
    "status",
    "refresh_days",
    "retrieval_policy",
}
OFFICIAL_HOSTS = {"www.chimelong.com", "chimelong.com"}
VOLATILE_CATEGORIES = {"announcement", "dining", "faq", "hotel", "show", "transport"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
    return records


def validate_inventory(records: list[dict[str, Any]], csv_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not 30 <= len(records) <= 50:
        errors.append(f"expected 30-50 records, got {len(records)}")

    source_ids: list[str] = []
    urls: list[str] = []
    for index, record in enumerate(records, 1):
        missing = sorted(REQUIRED_FIELDS - record.keys())
        if missing:
            errors.append(f"record {index} missing fields: {', '.join(missing)}")
            continue
        source_id = str(record["source_id"])
        source_ids.append(source_id)
        urls.append(str(record["source_url"]))
        prefix = f"record {index} ({source_id})"
        if record["scenic_id"] != "zhuhai-chimelong":
            errors.append(f"{prefix}: wrong scenic_id")
        if record["demo"] is not False:
            errors.append(f"{prefix}: curated record must set demo=false")
        if record["status"] != "verified":
            errors.append(f"{prefix}: status must be verified")
        if record["authority_level"] != 3:
            errors.append(f"{prefix}: official source must use authority_level=3")
        try:
            date.fromisoformat(str(record["verified_at"]))
        except ValueError:
            errors.append(f"{prefix}: invalid verified_at")
        if not isinstance(record["refresh_days"], int) or record["refresh_days"] <= 0:
            errors.append(f"{prefix}: refresh_days must be a positive integer")
        parsed = urlparse(str(record["source_url"]))
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
            errors.append(f"{prefix}: source URL is not an approved official HTTPS URL")
        if record["category"] in VOLATILE_CATEGORIES:
            if record["volatile"] is not True:
                errors.append(f"{prefix}: volatile category must set volatile=true")
            if record["retrieval_policy"] != "live_check_required":
                errors.append(f"{prefix}: volatile category requires live_check_required")

    duplicates = sorted({item for item in source_ids if source_ids.count(item) > 1})
    if duplicates:
        errors.append("duplicate source_id: " + ", ".join(duplicates))
    duplicate_urls = sorted({item for item in urls if urls.count(item) > 1})
    if duplicate_urls:
        errors.append("duplicate source_url: " + ", ".join(duplicate_urls))

    if csv_path is not None:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            csv_records = list(csv.DictReader(handle))
        csv_ids = [record["source_id"] for record in csv_records]
        if csv_ids != source_ids:
            errors.append("CSV and JSONL source_id order/content differ")
        if len(csv_records) != len(records):
            errors.append("CSV and JSONL record counts differ")
    return errors


def check_network(records: list[dict[str, Any]]) -> list[str]:
    import httpx

    failures: list[str] = []

    def fetch(record: dict[str, Any]) -> tuple[str, int | str]:
        try:
            response = httpx.get(
                record["source_url"],
                follow_redirects=True,
                timeout=15,
                headers={"User-Agent": "ScenicPilot-source-audit/0.1"},
            )
            return record["source_id"], response.status_code
        except httpx.HTTPError as exc:
            return record["source_id"], type(exc).__name__

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch, record) for record in records]
        for future in as_completed(futures):
            source_id, status = future.result()
            if not isinstance(status, int) or status >= 400:
                failures.append(f"{source_id}: {status}")
    return sorted(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the 资料治理阶段 source inventory")
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--check-network", action="store_true")
    args = parser.parse_args()

    records = load_jsonl(args.jsonl)
    errors = validate_inventory(records, args.csv)
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        return 1
    print(f"OK {len(records)} curated sources passed offline validation")

    if args.check_network:
        failures = check_network(records)
        if failures:
            for failure in failures:
                print(f"NETWORK {failure}")
            return 2
        print(f"OK {len(records)} source URLs are reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
