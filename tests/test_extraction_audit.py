from scenic_pilot.knowledge.audit import audit_document, audit_summary, recommend_threshold


def make_document(source_id: str, content: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "title": "测试页面",
        "source_url": "https://example.test/page",
        "content": content,
    }


def make_metadata(*, quality: float, fetch_method: str = "httpx") -> dict[str, object]:
    return {
        "fetch_method": fetch_method,
        "extraction_method": "trafilatura",
        "extraction_quality": quality,
    }


def test_audit_marks_stub_and_effective_playwright() -> None:
    stub = audit_document(
        make_document("stub", "顶部"),
        make_metadata(quality=0.302, fetch_method="playwright"),
        {},
    )
    recovered = audit_document(
        make_document("recovered", "完整的景区正文内容。" * 20),
        make_metadata(quality=0.421199, fetch_method="playwright"),
        {},
    )

    assert stub.completeness == "incomplete"
    assert stub.navigation_contamination == "severe"
    assert stub.playwright_outcome == "required_but_ineffective"
    assert recovered.playwright_outcome == "required_and_effective"
    assert recommend_threshold([stub, recovered]) == 0.36


def test_audit_marks_related_card_leakage_separately() -> None:
    audit = audit_document(
        make_document(
            "ocean-item",
            "目标项目正文。" * 20 + "其他展区：无关项目",
        ),
        make_metadata(quality=0.9),
        {"ocean-item": "目标项目", "other-item": "无关项目"},
    )

    assert audit.completeness == "complete"
    assert audit.related_content_leakage is True
    assert audit.manual_review_required is True


def test_audit_does_not_treat_top_inside_sentence_as_navigation() -> None:
    audit = audit_document(
        make_document("slide", "在耸高的滑道顶部一划而下。" * 10),
        make_metadata(quality=0.8),
        {},
    )

    assert audit.navigation_terms == ()
    assert audit.navigation_contamination == "none"


def test_audit_summary_keeps_current_threshold_without_incomplete_samples() -> None:
    complete = audit_document(
        make_document("complete", "完整的景区正文内容。" * 20),
        make_metadata(quality=0.8),
        {},
    )

    summary = audit_summary([complete])

    assert summary["incomplete_count"] == 0
    assert summary["max_incomplete_quality"] is None
    assert summary["recommended_quality_threshold"] == 0.36
    assert summary["quality_threshold_reestimated"] is False
