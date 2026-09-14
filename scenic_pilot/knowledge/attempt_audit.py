from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


ATTEMPT_SCHEMA_VERSION = "scenicpilot-extraction-attempt-v1"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def classify_page_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if "/exhibition/" in path:
        return "exhibition"
    if "/perform/" in path:
        return "perform"
    if "/foodshopping/" in path or path.endswith("/food/"):
        return "food"
    if "/privilege/" in path:
        return "privilege"
    if "/traffic/" in path:
        return "traffic"
    if "/news/" in path:
        return "news"
    if "/question/" in path:
        return "faq"
    if path == "/zh/hotel/":
        return "hotel_directory"
    if "hotel" in path or "apartments" in path:
        return "hotel"
    return "overview"


@dataclass(frozen=True, slots=True)
class ExtractionAttemptEvent:
    schema_version: str
    run_id: str
    event_id: str
    attempted_at: str
    source_id: str
    source_url: str
    final_url: str | None
    page_type: str
    attempt_number: int
    fetch_method: str
    browser_fallback: bool
    http_status: int | None
    raw_sha256: str | None
    extraction_method: str | None
    selector_used: str | None
    content_chars: int
    quality_score: float | None
    topic_purity_score: float | None
    schema_complete: bool | None
    accepted: bool
    failure_reasons: tuple[str, ...]
    contamination_flags: tuple[str, ...] = ()
    cleanup_flags: tuple[str, ...] = ()
    structure_flags: tuple[str, ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        source_id: str,
        source_url: str,
        attempt_number: int,
        fetch_method: str,
        browser_fallback: bool,
        accepted: bool,
        failure_reasons: tuple[str, ...],
        final_url: str | None = None,
        attempted_at: str | None = None,
        http_status: int | None = None,
        raw_sha256: str | None = None,
        extraction_method: str | None = None,
        selector_used: str | None = None,
        content_chars: int = 0,
        quality_score: float | None = None,
        topic_purity_score: float | None = None,
        schema_complete: bool | None = None,
        contamination_flags: tuple[str, ...] = (),
        cleanup_flags: tuple[str, ...] = (),
        structure_flags: tuple[str, ...] = (),
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> "ExtractionAttemptEvent":
        timestamp = attempted_at or _utc_now()
        fingerprint = ":".join(
            (
                run_id,
                source_id,
                str(attempt_number),
                fetch_method,
                raw_sha256 or "no-body",
                timestamp,
            )
        )
        return cls(
            schema_version=ATTEMPT_SCHEMA_VERSION,
            run_id=run_id,
            event_id=hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24],
            attempted_at=timestamp,
            source_id=source_id,
            source_url=source_url,
            final_url=final_url,
            page_type=classify_page_type(final_url or source_url),
            attempt_number=attempt_number,
            fetch_method=fetch_method,
            browser_fallback=browser_fallback,
            http_status=http_status,
            raw_sha256=raw_sha256,
            extraction_method=extraction_method,
            selector_used=selector_used,
            content_chars=content_chars,
            quality_score=quality_score,
            topic_purity_score=topic_purity_score,
            schema_complete=schema_complete,
            accepted=accepted,
            failure_reasons=failure_reasons,
            contamination_flags=contamination_flags,
            cleanup_flags=cleanup_flags,
            structure_flags=structure_flags,
            error_type=error_type,
            error_message=error_message,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExtractionAttemptEvent":
        tuple_fields = {
            "failure_reasons",
            "contamination_flags",
            "cleanup_flags",
            "structure_flags",
        }
        normalized = {
            key: tuple(item) if key in tuple_fields else item
            for key, item in value.items()
        }
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttemptAuditSink(Protocol):
    run_id: str

    def record(self, event: ExtractionAttemptEvent) -> None: ...


class JsonlAttemptAuditSink:
    def __init__(self, path: Path, *, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex
        self.events: list[ExtractionAttemptEvent] = []

    def record(self, event: ExtractionAttemptEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        self.events.append(event)

    def write_summary(self, path: Path) -> dict[str, Any]:
        summary = summarize_attempt_events(self.events)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
        return summary


def load_attempt_events(
    path: Path, *, run_id: str | None = None
) -> list[ExtractionAttemptEvent]:
    events = [
        ExtractionAttemptEvent.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if run_id is not None:
        events = [event for event in events if event.run_id == run_id]
    return events


def summarize_attempt_events(
    events: list[ExtractionAttemptEvent],
) -> dict[str, Any]:
    grouped: dict[str, list[ExtractionAttemptEvent]] = defaultdict(list)
    for event in events:
        grouped[event.page_type].append(event)

    by_page_type: dict[str, Any] = {}
    for page_type, items in sorted(grouped.items()):
        method_counts = Counter(item.extraction_method or "fetch_error" for item in items)
        selector_counts = Counter(
            item.selector_used or "<none>" for item in items
        )
        failure_counts = Counter(
            reason for item in items for reason in item.failure_reasons
        )
        selector_performance: dict[str, Any] = {}
        for selector in sorted(
            {item.selector_used for item in items if item.selector_used}
        ):
            selected = [item for item in items if item.selector_used == selector]
            selector_performance[selector] = {
                "attempts": len(selected),
                "accepted": sum(item.accepted for item in selected),
                "failed": sum(not item.accepted for item in selected),
                "acceptance_rate": round(
                    sum(item.accepted for item in selected) / len(selected), 6
                ),
                "average_quality": _average(
                    item.quality_score for item in selected
                ),
                "average_topic_purity": _average(
                    item.topic_purity_score for item in selected
                ),
            }
        method_performance: dict[str, Any] = {}
        for method in sorted({item.extraction_method or "fetch_error" for item in items}):
            selected = [
                item
                for item in items
                if (item.extraction_method or "fetch_error") == method
            ]
            method_performance[method] = {
                "attempts": len(selected),
                "accepted": sum(item.accepted for item in selected),
                "failed": sum(not item.accepted for item in selected),
                "acceptance_rate": round(
                    sum(item.accepted for item in selected) / len(selected), 6
                ),
                "average_quality": _average(
                    item.quality_score for item in selected
                ),
                "average_topic_purity": _average(
                    item.topic_purity_score for item in selected
                ),
            }
        best_selector = _best_candidate(selector_performance)
        by_page_type[page_type] = {
            "attempts": len(items),
            "accepted": sum(item.accepted for item in items),
            "failed": sum(not item.accepted for item in items),
            "failure_rate": round(
                sum(not item.accepted for item in items) / len(items), 6
            ),
            "browser_attempts": sum(item.browser_fallback for item in items),
            "method_distribution": dict(sorted(method_counts.items())),
            "selector_distribution": dict(sorted(selector_counts.items())),
            "failure_reason_distribution": dict(sorted(failure_counts.items())),
            "method_performance": method_performance,
            "selector_performance": selector_performance,
            "best_observed_method": _best_candidate(method_performance),
            "best_observed_selector": best_selector,
        }

    run_ids = list(dict.fromkeys(event.run_id for event in events))
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "run_ids": run_ids,
        "attempt_count": len(events),
        "accepted_count": sum(event.accepted for event in events),
        "failed_count": sum(not event.accepted for event in events),
        "browser_attempt_count": sum(event.browser_fallback for event in events),
        "page_type_count": len(by_page_type),
        "by_page_type": by_page_type,
    }


def _average(values: Any) -> float | None:
    available = [float(value) for value in values if value is not None]
    return round(sum(available) / len(available), 6) if available else None


def _best_candidate(performance: dict[str, Any]) -> str | None:
    if not performance:
        return None
    return max(
        performance,
        key=lambda selector: (
            performance[selector]["acceptance_rate"],
            performance[selector]["accepted"],
            performance[selector]["attempts"],
            performance[selector]["average_quality"] or 0.0,
            selector,
        ),
    )
