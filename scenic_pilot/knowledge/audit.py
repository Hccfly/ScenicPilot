from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


NAVIGATION_TERMS = (
    "顶部",
    "返回",
    "立即购票",
    "酒店预订",
    "关于长隆",
    "联系我们",
    "网站地图",
    "隐私政策",
    "关注长隆",
)
RELATED_MARKERS = (
    "其他展区",
    "其他乐园设施",
    "其他酒店设施",
    "查看更多",
    "查看详情",
)
BROAD_SOURCE_MARKERS = (
    "overview",
    "food",
    "privilege",
    "news",
    "performances",
    "exhibitions",
    "traffic",
    "faq",
)


@dataclass(frozen=True, slots=True)
class ExtractionAudit:
    source_id: str
    title: str
    source_url: str
    fetch_method: str
    extraction_method: str
    quality_score: float
    topic_purity_score: float | None
    contamination_flags: tuple[str, ...]
    cleanup_flags: tuple[str, ...]
    schema_complete: bool | None
    structure_flags: tuple[str, ...]
    content_chars: int
    completeness: str
    navigation_contamination: str
    navigation_terms: tuple[str, ...]
    related_content_leakage: bool
    leakage_evidence: tuple[str, ...]
    needs_playwright: bool
    playwright_outcome: str
    manual_review_required: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compact_length(text: str) -> int:
    return len(re.sub(r"\s+|[#*_`>-]", "", text))


def _is_detail_source(source_id: str) -> bool:
    return not any(marker in source_id for marker in BROAD_SOURCE_MARKERS)


def _navigation_terms(text: str) -> tuple[str, ...]:
    standalone_lines = {line.strip() for line in text.splitlines()}
    return tuple(
        term
        for term in NAVIGATION_TERMS
        if (
            term in standalone_lines
            if term in {"顶部", "返回"}
            else term in text
        )
    )


def audit_document(
    document: dict[str, Any],
    metadata: dict[str, Any],
    detail_titles: dict[str, str],
    *,
    minimum_content_chars: int = 80,
) -> ExtractionAudit:
    content = document["content"]
    content_chars = _compact_length(content)
    navigation_terms = _navigation_terms(content)
    schema_complete = metadata.get("schema_complete")
    if schema_complete is True:
        completeness = "complete"
        navigation_contamination = "minor" if navigation_terms else "none"
    elif schema_complete is False:
        completeness = "incomplete"
        navigation_contamination = "severe" if navigation_terms else "none"
    elif content_chars < minimum_content_chars:
        completeness = "incomplete"
        navigation_contamination = "severe" if navigation_terms else "none"
    else:
        completeness = "complete"
        navigation_contamination = "minor" if navigation_terms else "none"

    leakage_evidence = [marker for marker in RELATED_MARKERS if marker in content]
    if _is_detail_source(document["source_id"]):
        leakage_evidence.extend(
            title
            for source_id, title in detail_titles.items()
            if source_id != document["source_id"]
            and len(title) >= 3
            and title in content
        )
    leakage_evidence = list(dict.fromkeys(leakage_evidence))

    needs_playwright = metadata["fetch_method"] == "playwright"
    if needs_playwright and completeness == "complete":
        playwright_outcome = "required_and_effective"
    elif needs_playwright:
        playwright_outcome = "required_but_ineffective"
    else:
        playwright_outcome = "not_required_in_this_capture"

    related_content_leakage = bool(leakage_evidence)
    return ExtractionAudit(
        source_id=document["source_id"],
        title=document["title"],
        source_url=document["source_url"],
        fetch_method=metadata["fetch_method"],
        extraction_method=metadata["extraction_method"],
        quality_score=float(metadata["extraction_quality"]),
        topic_purity_score=(
            float(metadata["topic_purity_score"])
            if metadata.get("topic_purity_score") is not None
            else None
        ),
        contamination_flags=tuple(metadata.get("contamination_flags", ())),
        cleanup_flags=tuple(metadata.get("cleanup_flags", ())),
        schema_complete=schema_complete,
        structure_flags=tuple(metadata.get("structure_flags", ())),
        content_chars=content_chars,
        completeness=completeness,
        navigation_contamination=navigation_contamination,
        navigation_terms=navigation_terms,
        related_content_leakage=related_content_leakage,
        leakage_evidence=tuple(leakage_evidence),
        needs_playwright=needs_playwright,
        playwright_outcome=playwright_outcome,
        manual_review_required=(completeness != "complete" or related_content_leakage),
    )


def recommend_threshold(audits: list[ExtractionAudit]) -> float:
    complete_scores = [item.quality_score for item in audits if item.completeness == "complete"]
    incomplete_scores = [
        item.quality_score for item in audits if item.completeness == "incomplete"
    ]
    if not complete_scores or not incomplete_scores:
        raise ValueError("both complete and incomplete samples are required")
    lower_bound = max(incomplete_scores)
    upper_bound = min(complete_scores)
    if lower_bound >= upper_bound:
        raise ValueError("quality scores do not separate complete and incomplete samples")
    return round((lower_bound + upper_bound) / 2, 2)


def load_audits(
    index_dir: Path,
    raw_dir: Path,
    *,
    minimum_content_chars: int = 80,
) -> list[ExtractionAudit]:
    documents = [
        json.loads(line)
        for line in (index_dir / "documents.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    detail_titles = {
        item["source_id"]: item["title"]
        for item in documents
        if _is_detail_source(item["source_id"])
    }
    audits: list[ExtractionAudit] = []
    for document in documents:
        metadata_path = (
            raw_dir
            / document["source_id"]
            / f"{document['raw_content_sha256']}.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        audits.append(
            audit_document(
                document,
                metadata,
                detail_titles,
                minimum_content_chars=minimum_content_chars,
            )
        )
    return sorted(audits, key=lambda item: item.source_id)


def audit_summary(
    audits: list[ExtractionAudit],
    *,
    minimum_content_chars: int = 80,
    current_quality_threshold: float = 0.36,
) -> dict[str, Any]:
    complete_scores = [item.quality_score for item in audits if item.completeness == "complete"]
    incomplete_scores = [
        item.quality_score for item in audits if item.completeness == "incomplete"
    ]
    threshold_reestimated = bool(complete_scores and incomplete_scores)
    threshold = (
        recommend_threshold(audits)
        if threshold_reestimated
        else current_quality_threshold
    )
    return {
        "source_count": len(audits),
        "complete_count": sum(item.completeness == "complete" for item in audits),
        "incomplete_count": sum(item.completeness == "incomplete" for item in audits),
        "navigation_none_count": sum(
            item.navigation_contamination == "none" for item in audits
        ),
        "navigation_minor_count": sum(
            item.navigation_contamination == "minor" for item in audits
        ),
        "navigation_severe_count": sum(
            item.navigation_contamination == "severe" for item in audits
        ),
        "navigation_cleanup_count": sum(bool(item.cleanup_flags) for item in audits),
        "navigation_boundary_cleaned_count": sum(
            any(
                flag.startswith("navigation_boundary_cleaned:")
                for flag in item.cleanup_flags
            )
            for item in audits
        ),
        "related_content_leakage_count": sum(
            item.related_content_leakage for item in audits
        ),
        "playwright_required_count": sum(item.needs_playwright for item in audits),
        "playwright_effective_count": sum(
            item.playwright_outcome == "required_and_effective" for item in audits
        ),
        "playwright_ineffective_count": sum(
            item.playwright_outcome == "required_but_ineffective" for item in audits
        ),
        "max_incomplete_quality": max(incomplete_scores, default=None),
        "min_complete_quality": min(complete_scores, default=None),
        "recommended_quality_threshold": threshold,
        "quality_threshold_reestimated": threshold_reestimated,
        "minimum_content_chars": minimum_content_chars,
        "decision_rule": (
            f"use schema completeness for directory pages; reject other content shorter "
            f"than {minimum_content_chars} compact chars; otherwise use quality threshold "
            f"{threshold:.2f}; audit related-card leakage separately"
        ),
    }
