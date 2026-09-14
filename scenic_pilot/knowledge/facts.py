from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from scenic_pilot.models import SourceDocument


@dataclass(frozen=True, slots=True)
class FactRecord:
    fact_id: str
    source_id: str
    scenic_id: str
    fact_type: str
    subject: str
    value: str
    unit: str | None
    evidence_text: str
    source_url: str
    observed_at: str | None
    refresh_days: int | None
    requires_live_check: bool
    document_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FactExtractor(Protocol):
    def extract(self, document: SourceDocument) -> list[FactRecord]: ...


class RuleBasedFactExtractor:
    """High-precision baseline; extracted facts remain evidence, never static truth."""

    PRICE = re.compile(r"(?:¥|￥)\s*(\d+(?:\.\d{1,2})?)|(?<!\d)(\d+(?:\.\d{1,2})?)\s*元")
    TIME_RANGE = re.compile(r"(?<!\d)([0-2]?\d:[0-5]\d)\s*(?:-|–|—|至|~)\s*([0-2]?\d:[0-5]\d)")
    CLOCK = re.compile(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)")
    INTERVAL = re.compile(r"(?:每隔\s*)?(\d{1,3})\s*分钟(?:一班|/班|一趟|/趟)?")

    def extract(self, document: SourceDocument) -> list[FactRecord]:
        facts: list[FactRecord] = []
        lines = [line.strip(" #-\t") for line in document.content.splitlines() if line.strip()]
        for line in lines:
            facts.extend(self._prices(document, line))
            facts.extend(self._opening_hours(document, line))
            facts.extend(self._performance_times(document, line))
            facts.extend(self._transit_schedules(document, line))
        unique = {fact.fact_id: fact for fact in facts}
        return sorted(unique.values(), key=lambda item: item.fact_id)

    def _prices(self, document: SourceDocument, line: str) -> list[FactRecord]:
        if not any(term in line for term in ("票", "价格", "售价", "费用", "门市价")):
            return []
        return [
            self._fact(document, "ticket_price", self._subject(line, "票价"), match.group(1) or match.group(2), "CNY", line)
            for match in self.PRICE.finditer(line)
        ]

    def _opening_hours(self, document: SourceDocument, line: str) -> list[FactRecord]:
        if not any(term in line for term in ("营业", "开放", "开园", "闭园", "入园")):
            return []
        return [
            self._fact(document, "opening_hours", self._subject(line, "营业时间"), f"{match.group(1)}-{match.group(2)}", None, line)
            for match in self.TIME_RANGE.finditer(line)
        ]

    def _performance_times(self, document: SourceDocument, line: str) -> list[FactRecord]:
        if not any(term in line for term in ("演出", "表演", "剧场", "巡游", "场次")):
            return []
        return [
            self._fact(document, "performance_time", self._subject(line, "演出场次"), match.group(1), None, line)
            for match in self.CLOCK.finditer(line)
        ]

    def _transit_schedules(self, document: SourceDocument, line: str) -> list[FactRecord]:
        if not any(term in line for term in ("班车", "巴士", "公交", "接驳", "穿梭", "发车", "班次")):
            return []
        results = [
            self._fact(document, "transit_schedule", self._subject(line, "交通班次"), match.group(1), "minute_interval", line)
            for match in self.INTERVAL.finditer(line)
        ]
        results.extend(
            self._fact(document, "transit_schedule", self._subject(line, "交通班次"), f"{match.group(1)}-{match.group(2)}", "service_window", line)
            for match in self.TIME_RANGE.finditer(line)
        )
        return results

    @staticmethod
    def _subject(line: str, fallback: str) -> str:
        head = re.split(r"[：:]", line, maxsplit=1)[0].strip()
        return head[:80] if 1 <= len(head) <= 80 else fallback

    @staticmethod
    def _fact(
        document: SourceDocument,
        fact_type: str,
        subject: str,
        value: str,
        unit: str | None,
        evidence: str,
    ) -> FactRecord:
        stable_key = f"{document.source_id}:{fact_type}:{subject}:{value}:{unit or ''}"
        fact_id = f"{document.source_id}-fact-{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()[:16]}"
        return FactRecord(
            fact_id=fact_id,
            source_id=document.source_id,
            scenic_id=document.scenic_id,
            fact_type=fact_type,
            subject=subject,
            value=value,
            unit=unit,
            evidence_text=evidence,
            source_url=document.source_url,
            observed_at=document.fetched_at or document.verified_at,
            refresh_days=document.refresh_days,
            requires_live_check=True,
            document_version=document.document_version,
        )
