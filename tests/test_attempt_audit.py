import json
from pathlib import Path

from scenic_pilot.knowledge.attempt_audit import (
    ExtractionAttemptEvent,
    JsonlAttemptAuditSink,
    classify_page_type,
    load_attempt_events,
    summarize_attempt_events,
)


def make_event(
    *,
    page_type_url: str,
    accepted: bool,
    selector: str | None,
    reason: str | None = None,
) -> ExtractionAttemptEvent:
    return ExtractionAttemptEvent.create(
        run_id="run-test",
        source_id="source-test",
        source_url=page_type_url,
        final_url=page_type_url,
        attempt_number=1,
        fetch_method="httpx",
        browser_fallback=False,
        http_status=200,
        raw_sha256="a" * 64,
        extraction_method="chimelong_selector" if selector else "trafilatura",
        selector_used=selector,
        content_chars=320,
        quality_score=0.8 if accepted else 0.2,
        topic_purity_score=1.0,
        accepted=accepted,
        failure_reasons=(reason,) if reason else (),
    )


def test_classify_page_type_uses_chimelong_url_shape() -> None:
    assert classify_page_type("https://www.chimelong.com/zh/a/exhibition/1.html") == "exhibition"
    assert classify_page_type("https://www.chimelong.com/zh/a/perform/") == "perform"
    assert classify_page_type("https://www.chimelong.com/zh/a/food/") == "food"
    assert classify_page_type("https://www.chimelong.com/zh/a/privilege/") == "privilege"
    assert classify_page_type("https://www.chimelong.com/zh/traffic/type_1.html") == "traffic"


def test_attempt_summary_groups_failures_and_selector_performance() -> None:
    events = [
        make_event(
            page_type_url="https://www.chimelong.com/zh/a/exhibition/1.html",
            accepted=True,
            selector=".exhibitionDetail-wrap",
        ),
        make_event(
            page_type_url="https://www.chimelong.com/zh/a/perform/",
            accepted=False,
            selector=None,
            reason="quality_below_threshold",
        ),
    ]

    summary = summarize_attempt_events(events)

    assert summary["attempt_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["by_page_type"]["perform"]["failure_reason_distribution"] == {
        "quality_below_threshold": 1
    }
    exhibition = summary["by_page_type"]["exhibition"]
    assert exhibition["best_observed_selector"] == ".exhibitionDetail-wrap"
    assert exhibition["selector_performance"][".exhibitionDetail-wrap"]["accepted"] == 1


def test_jsonl_attempt_sink_appends_and_can_reload(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    sink = JsonlAttemptAuditSink(path, run_id="run-test")
    event = make_event(
        page_type_url="https://www.chimelong.com/zh/a/privilege/",
        accepted=True,
        selector=".privilege-wrap",
    )

    sink.record(event)
    summary = sink.write_summary(tmp_path / "summary.json")
    loaded = load_attempt_events(path, run_id="run-test")

    assert loaded == [event]
    assert summary["attempt_count"] == 1
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))[
        "by_page_type"
    ]["privilege"]["accepted"] == 1
