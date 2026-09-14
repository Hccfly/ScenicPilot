from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from scenic_pilot.answering.contracts import AnswerConflict, ConflictVariant
from scenic_pilot.models import SearchHit


TIME_RANGE = re.compile(
    r"(?<!\d)([0-2]?\d:[0-5]\d)\s*(?:-|–|—|至|~)\s*([0-2]?\d:[0-5]\d)"
)
CLOCK = re.compile(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)")
PRICE = re.compile(r"(?:¥|￥)\s*(\d+(?:\.\d{1,2})?)|(?<!\d)(\d+(?:\.\d{1,2})?)\s*元")
INTERVAL = re.compile(r"(?:每隔|约|大约)?\s*(\d{1,3}(?:\s*-\s*\d{1,3})?)\s*分钟(?:\s*/\s*班|一班)?")
MARKDOWN_PREFIX = re.compile(r"^\s*(?:#{1,6}|[-*+]|\d+[.)、])\s*")


@dataclass(frozen=True, slots=True)
class _FactVariant:
    fact_type: str
    subject: str
    normalized_subject: str
    value: str
    evidence_text: str
    hit: SearchHit
    scope_score: int


class RuleBasedConflictDetector:
    """Detect only high-precision comparable facts; silence beats false conflicts."""

    _query_terms = {
        "ticket_price": ("票价", "门票", "价格", "费用", "多少钱", "售价"),
        "opening_hours": ("营业时间", "开放时间", "开园", "闭园", "几点开", "几点关"),
        "performance_time": ("演出时间", "表演时间", "演出场次", "表演场次", "巡游时间"),
        "transit_schedule": ("班车", "巴士", "公交", "接驳", "穿梭", "发车", "班次"),
        "operational_status": ("是否开放", "开放吗", "是否营业", "营业吗", "停运", "关闭"),
    }
    _generic_subjects = {
        "备注",
        "温馨提示",
        "营业时间",
        "开放时间",
        "运营时间",
        "演出时间",
        "表演时间",
        "演出场次",
        "表演场次",
        "发车间隔",
        "交通班次",
        "行驶线路",
    }

    def detect(
        self, query: str, hits: Sequence[SearchHit]
    ) -> tuple[AnswerConflict, ...]:
        requested_types = {
            fact_type
            for fact_type, terms in self._query_terms.items()
            if any(term in query for term in terms)
        }
        if not requested_types:
            return ()

        grouped: dict[tuple[str, str], list[_FactVariant]] = defaultdict(list)
        for hit in hits:
            for variant in self._extract(query, hit, requested_types):
                grouped[(variant.fact_type, variant.normalized_subject)].append(variant)

        conflicts: list[AnswerConflict] = []
        for (fact_type, _), candidates in sorted(grouped.items()):
            by_source: dict[str, list[_FactVariant]] = defaultdict(list)
            for candidate in candidates:
                by_source[candidate.hit.chunk.source_id].append(candidate)
            if len(by_source) < 2:
                continue

            variants: list[ConflictVariant] = []
            for source_id, source_candidates in sorted(by_source.items()):
                values = sorted({item.value for item in source_candidates})
                evidence = max(
                    source_candidates,
                    key=lambda item: (item.scope_score, len(item.evidence_text)),
                )
                variants.append(
                    ConflictVariant(
                        source_id=source_id,
                        value="、".join(values),
                        evidence_text=evidence.evidence_text,
                        authority_level=evidence.hit.chunk.authority_level,
                        verified_at=evidence.hit.chunk.verified_at,
                        scope_score=evidence.scope_score,
                        volatile=evidence.hit.chunk.volatile,
                    )
                )
            if len({item.value for item in variants}) < 2:
                continue

            resolved, chosen_source_id, reason = self._resolve(variants)
            subject = candidates[0].subject
            digest = hashlib.sha256(
                f"{fact_type}:{candidates[0].normalized_subject}".encode("utf-8")
            ).hexdigest()[:12]
            conflicts.append(
                AnswerConflict(
                    conflict_id=f"conflict-{digest}",
                    fact_type=fact_type,
                    subject=subject,
                    variants=tuple(variants),
                    resolved=resolved,
                    chosen_source_id=chosen_source_id,
                    resolution_reason=reason,
                )
            )
        return tuple(conflicts)

    def _extract(
        self, query: str, hit: SearchHit, requested_types: set[str]
    ) -> Iterable[_FactVariant]:
        text = hit.context or hit.chunk.content
        for raw_line in text.splitlines():
            line = MARKDOWN_PREFIX.sub("", raw_line).strip()
            if len(line) < 4:
                continue
            subject = self._subject(line, hit.chunk.title)
            normalized_subject = self._normalize_subject(subject)
            if subject in self._generic_subjects or len(normalized_subject) <= 3:
                normalized_subject = self._normalize_subject(
                    f"{hit.chunk.title}{subject}"
                )
            if not normalized_subject:
                continue
            scope_score = self._scope_score(query, hit, line)
            for fact_type in requested_types:
                values = self._values(fact_type, line)
                if not values:
                    continue
                yield _FactVariant(
                    fact_type=fact_type,
                    subject=subject,
                    normalized_subject=normalized_subject,
                    value="、".join(values),
                    evidence_text=line,
                    hit=hit,
                    scope_score=scope_score,
                )

    @staticmethod
    def _subject(line: str, fallback: str) -> str:
        if "：" in line or ":" in line:
            return re.split(r"[：:]", line, maxsplit=1)[0].strip()[:100]
        return fallback.strip()[:100]

    @staticmethod
    def _normalize_subject(subject: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", subject).lower()

    @staticmethod
    def _scope_score(query: str, hit: SearchHit, evidence: str) -> int:
        generic = (
            "票价|门票|价格|费用|多少钱|售价|营业时间|开放时间|开园|闭园|几点开|"
            "几点关|演出时间|表演时间|演出场次|表演场次|巡游时间|班车|巴士|公交|"
            "接驳|穿梭|发车|班次|是否开放|开放吗|是否营业|营业吗|停运|关闭"
        )
        entity = re.sub(generic, "", query)
        entity = re.sub(r"\s+", "", entity)
        score = min(len(hit.matched_terms), 3)
        if len(entity) >= 2 and entity in re.sub(r"\s+", "", hit.chunk.title):
            score += 4
        if len(entity) >= 2 and entity in re.sub(r"\s+", "", evidence):
            score += 2
        return score

    @staticmethod
    def _values(fact_type: str, line: str) -> tuple[str, ...]:
        if fact_type == "ticket_price":
            if not any(term in line for term in ("票", "价格", "售价", "费用", "门市价")):
                return ()
            return tuple(
                sorted({f"{match.group(1) or match.group(2)} CNY" for match in PRICE.finditer(line)})
            )
        if fact_type == "opening_hours":
            if not any(term in line for term in ("营业", "开放", "开园", "闭园", "入园")):
                return ()
            return tuple(
                sorted({f"{match.group(1)}-{match.group(2)}" for match in TIME_RANGE.finditer(line)})
            )
        if fact_type == "performance_time":
            if not any(term in line for term in ("演出", "表演", "剧场", "巡游", "场次")):
                return ()
            return tuple(sorted(set(CLOCK.findall(line))))
        if fact_type == "transit_schedule":
            if not any(term in line for term in ("班车", "巴士", "公交", "接驳", "穿梭", "发车", "班次")):
                return ()
            intervals = {f"{value.replace(' ', '')} minute_interval" for value in INTERVAL.findall(line)}
            windows = {
                f"{match.group(1)}-{match.group(2)} service_window"
                for match in TIME_RANGE.finditer(line)
            }
            return tuple(sorted(intervals | windows))
        if fact_type == "operational_status":
            if any(term in line for term in ("暂停开放", "暂停营业", "临时关闭", "停止运营", "停运", "关闭")):
                return ("closed",)
            if any(term in line for term in ("正常开放", "正常营业", "开放", "营业")):
                return ("open",)
        return ()

    @staticmethod
    def _resolve(
        variants: Sequence[ConflictVariant],
    ) -> tuple[bool, str | None, str]:
        authority_max = max(item.authority_level for item in variants)
        authority_top = [item for item in variants if item.authority_level == authority_max]
        if len(authority_top) == 1:
            return True, authority_top[0].source_id, "higher_authority"

        def parsed_date(value: str | None) -> date:
            if not value:
                return date.min
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return date.min

        newest = max(parsed_date(item.verified_at) for item in authority_top)
        date_top = [item for item in authority_top if parsed_date(item.verified_at) == newest]
        if len(date_top) == 1:
            return True, date_top[0].source_id, "newer_verified_at"

        scope_max = max(item.scope_score for item in date_top)
        scope_top = [item for item in date_top if item.scope_score == scope_max]
        if len({item.value for item in scope_top}) == 1:
            return True, sorted(item.source_id for item in scope_top)[0], "same_top_value"
        if len(scope_top) == 1:
            return True, scope_top[0].source_id, "narrower_scope"
        return False, None, "equal_priority_conflict"
