from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date

from scenic_pilot.answering.conflicts import RuleBasedConflictDetector
from scenic_pilot.answering.contracts import (
    AnswerClaim,
    AnswerConflict,
    AnswerPolicy,
    AnswerResult,
)
from scenic_pilot.knowledge.retrieval.context import select_context_hits
from scenic_pilot.knowledge.retrieval.keyword import ChineseDomainTokenizer
from scenic_pilot.knowledge.retrieval.contracts import TextTokenizer
from scenic_pilot.models import Citation, SearchHit


MARKDOWN_PREFIX = re.compile(r"^\s*(?:#{1,6}|[-*+]|\d+[.)、])\s*")
CITATION_MARKER = re.compile(r"\[(\d+)]")
GENERIC_QUERY_TERMS = {
    "什么",
    "哪里",
    "怎么",
    "介绍",
    "可以",
    "适合",
    "是否",
    "有哪些",
    "如何",
    "珠海长隆",
}
QUERY_NOISE = (
    "珠海长隆",
    "是什么",
    "有哪些",
    "多少钱",
    "负责人是谁",
    "什么",
    "哪里",
    "怎么",
    "如何",
    "介绍",
    "可以",
    "适合",
    "是否",
    "多少",
    "请问",
    "一下",
    "园区",
    "里面",
    "的",
    "是",
    "吗",
    "呢",
    "去",
    "看",
    "玩",
    "坐",
)
QUERY_TOKEN_NOISE = set(QUERY_NOISE) | {
    "当前",
    "现在",
    "今天",
    "明天",
    "下周",
    "能",
    "看到",
    "值得",
    "负责人",
    "谁",
    "一共",
    "哪几家",
    "哪几个",
    "几家",
    "几个",
    "选择",
    "园区",
    "度假区",
    "里面",
    "体验",
}


class GroundedAnswerer:
    refusal = "现有资料不足以可靠回答这个问题。我不会猜测；请补充问题范围或核验珠海长隆官方最新信息。"
    _live_only_terms = (
        "实时排队",
        "排队精确",
        "现场库存",
        "实时库存",
        "剩余票",
        "还剩几张票",
        "还剩几间",
        "实时房态",
        "实时客流",
        "今天入园人数",
        "现场余票",
        "实时余票",
    )

    def __init__(
        self,
        policy: AnswerPolicy | None = None,
        *,
        conflict_detector: RuleBasedConflictDetector | None = None,
        tokenizer: TextTokenizer | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.policy = policy or AnswerPolicy()
        self.policy.validate()
        self.conflict_detector = conflict_detector or RuleBasedConflictDetector()
        self.tokenizer = tokenizer or ChineseDomainTokenizer()
        self.today = today

    def answer(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        min_score: float,
        context_limit: int = 3,
        max_context_tokens: int | None = 2200,
        requires_live_data: bool = False,
    ) -> tuple[str, list[Citation]]:
        """Compatibility wrapper for callers that only need text and citations."""

        result = self.answer_with_trace(
            query,
            hits,
            min_score=min_score,
            context_limit=context_limit,
            max_context_tokens=max_context_tokens,
            requires_live_data=requires_live_data,
        )
        return result.answer, list(result.citations)

    def answer_with_trace(
        self,
        query: str,
        hits: Sequence[SearchHit],
        *,
        min_score: float,
        context_limit: int = 3,
        max_context_tokens: int | None = 2200,
        requires_live_data: bool = False,
    ) -> AnswerResult:
        if requires_live_data or any(term in query for term in self._live_only_terms):
            return self._refusal_result(
                candidate_count=len(hits),
                rejection_counts={},
                reason="live_data_required",
                min_score=min_score,
            )
        usable, rejection_counts = self._usable_hits(
            query, hits, min_score=min_score
        )
        if not usable:
            return self._refusal_result(
                candidate_count=len(hits),
                rejection_counts=rejection_counts,
                reason="no_usable_evidence",
                min_score=min_score,
            )

        selected, context_trace = select_context_hits(
            usable,
            limit=context_limit,
            max_tokens=max_context_tokens,
            max_per_source=2,
        )
        if not selected:
            return self._refusal_result(
                candidate_count=len(hits),
                rejection_counts=rejection_counts,
                reason="context_budget_rejected_all_evidence",
                min_score=min_score,
                context_trace=context_trace,
            )

        citations = self._citations(selected)
        citation_ids = {item.source_id: item.citation_id for item in citations}
        conflicts = self.conflict_detector.detect(query, selected)
        unresolved = tuple(item for item in conflicts if not item.resolved)
        if unresolved:
            claims = self._conflict_claims(unresolved, citation_ids)
            answer = self._render_unresolved_conflicts(unresolved, citation_ids)
            trace = self._trace(
                answer=answer,
                status="conflict",
                min_score=min_score,
                candidate_count=len(hits),
                usable_count=len(usable),
                rejection_counts=rejection_counts,
                context_trace=context_trace,
                citations=citations,
                claims=claims,
                conflicts=conflicts,
                decision_reasons=["equal_priority_conflict"],
            )
            return AnswerResult(
                answer=answer,
                status="conflict",
                citations=tuple(citations),
                claims=claims,
                conflicts=conflicts,
                trace=trace,
            )

        if conflicts:
            claims = self._conflict_claims(conflicts, citation_ids)
            answer = self._render_resolved_conflicts(conflicts, citation_ids)
        else:
            claims = self._claims(query, selected, citation_ids)
            if not claims:
                return self._refusal_result(
                    candidate_count=len(hits),
                    rejection_counts=rejection_counts,
                    reason="no_renderable_claim",
                    min_score=min_score,
                    context_trace=context_trace,
                )
            answer = self._render_claims(claims)

        volatile_sources = [item.source_id for item in citations if item.volatile]
        stale_sources = [item.source_id for item in citations if item.freshness == "stale"]
        requires_live_check = bool(volatile_sources)
        status = "requires_live_check" if requires_live_check else "answered"
        decision_reasons: list[str] = []
        if conflicts:
            decision_reasons.append("resolved_conflict")
        if requires_live_check:
            decision_reasons.append("volatile_evidence")
            answer += (
                "\n\n时效提示：以上来源包含易变信息，当前回答只作为官方核验入口；"
                "请按实际到访日期重新确认。"
            )
        if stale_sources:
            decision_reasons.append("stale_evidence")

        trace = self._trace(
            answer=answer,
            status=status,
            min_score=min_score,
            candidate_count=len(hits),
            usable_count=len(usable),
            rejection_counts=rejection_counts,
            context_trace=context_trace,
            citations=citations,
            claims=claims,
            conflicts=conflicts,
            decision_reasons=decision_reasons or ["grounded_evidence_available"],
        )
        trace["volatile_source_ids"] = volatile_sources
        trace["stale_source_ids"] = stale_sources
        return AnswerResult(
            answer=answer,
            status=status,
            citations=tuple(citations),
            claims=claims,
            conflicts=conflicts,
            trace=trace,
        )

    def _usable_hits(
        self, query: str, hits: Sequence[SearchHit], *, min_score: float
    ) -> tuple[list[SearchHit], dict[str, int]]:
        usable: list[SearchHit] = []
        rejected: Counter[str] = Counter()
        blocked_statuses = {item.lower() for item in self.policy.blocked_statuses}
        specific_terms = self._specific_query_terms(query)
        required_matches = max(
            self.policy.min_specific_term_matches,
            math.ceil(len(specific_terms) * self.policy.min_specific_term_coverage),
        )
        for hit in hits:
            if hit.score < min_score:
                rejected["below_retrieval_score"] += 1
                continue
            if hit.keyword_score < self.policy.min_keyword_score and not hit.matched_terms:
                rejected["no_lexical_support"] += 1
                continue
            evidence = (
                f"{hit.chunk.title}\n{hit.chunk.content}\n"
                f"{hit.context or hit.chunk.content}"
            )
            expanded_support = {
                term
                for term in hit.matched_terms
                if len(term) >= 2
                and term not in GENERIC_QUERY_TERMS
                and term not in QUERY_TOKEN_NOISE
            }
            direct_matches = sum(term in evidence for term in specific_terms)
            alias_matches = sum(
                term in evidence and term not in specific_terms
                for term in expanded_support
            )
            supported_matches = direct_matches + min(alias_matches, 1)
            if supported_matches < required_matches:
                rejected["no_specific_query_support"] += 1
                continue
            if hit.chunk.authority_level < self.policy.minimum_authority_level:
                rejected["authority_too_low"] += 1
                continue
            if hit.chunk.status.lower() in blocked_statuses:
                rejected["source_status_blocked"] += 1
                continue
            if not (hit.context or hit.chunk.content).strip():
                rejected["empty_context"] += 1
                continue
            usable.append(hit)
        return usable, dict(sorted(rejected.items()))

    def _specific_query_terms(self, query: str) -> set[str]:
        return {
            token
            for token in self.tokenizer.tokenize(query)
            if token not in QUERY_TOKEN_NOISE
            and token not in GENERIC_QUERY_TERMS
            and (len(token) >= 2 or bool(re.search(r"[A-Za-z0-9]", token)))
        }

    def _citations(self, hits: Sequence[SearchHit]) -> list[Citation]:
        citations: list[Citation] = []
        seen: set[str] = set()
        for hit in hits:
            chunk = hit.chunk
            if chunk.source_id in seen:
                continue
            seen.add(chunk.source_id)
            citations.append(
                Citation(
                    source_id=chunk.source_id,
                    title=chunk.title,
                    url=chunk.source_url,
                    verified_at=chunk.verified_at,
                    volatile=chunk.volatile,
                    citation_id=len(citations) + 1,
                    authority_level=chunk.authority_level,
                    status=chunk.status,
                    retrieval_policy=chunk.retrieval_policy,
                    source_type=chunk.source_type,
                    refresh_days=chunk.refresh_days,
                    freshness=self._freshness(chunk.verified_at, chunk.refresh_days),
                )
            )
        return citations

    def _freshness(self, verified_at: str | None, refresh_days: int | None) -> str:
        if verified_at is None or refresh_days is None:
            return "unknown"
        try:
            verified = date.fromisoformat(verified_at[:10])
        except ValueError:
            return "unknown"
        age = (self.today() - verified).days
        return "stale" if age > refresh_days else "fresh"

    def _claims(
        self,
        query: str,
        hits: Sequence[SearchHit],
        citation_ids: dict[str, int | None],
    ) -> tuple[AnswerClaim, ...]:
        claims: list[AnswerClaim] = []
        seen_text: set[str] = set()
        for hit in hits:
            snippet = self._best_snippet(query, hit)
            normalized = re.sub(r"\s+", "", snippet)
            if not snippet or normalized in seen_text:
                continue
            seen_text.add(normalized)
            citation_id = citation_ids[hit.chunk.source_id]
            if citation_id is None:
                continue
            claims.append(
                AnswerClaim(
                    claim_id=f"claim-{len(claims) + 1}",
                    text=snippet,
                    citation_ids=(citation_id,),
                    source_ids=(hit.chunk.source_id,),
                    volatile=hit.chunk.volatile,
                )
            )
        return tuple(claims)

    def _best_snippet(self, query: str, hit: SearchHit) -> str:
        text = hit.context or hit.chunk.content
        segments: list[str] = []
        for line in text.splitlines():
            cleaned = MARKDOWN_PREFIX.sub("", line).strip()
            for segment in re.split(r"(?<=[。！？；])\s*", cleaned):
                segment = CITATION_MARKER.sub("", segment).strip()
                if len(segment) >= 4:
                    segments.append(segment)
        if not segments:
            return ""

        terms = set(hit.matched_terms)
        terms.update(
            term
            for term in re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]{2,}", query)
            if term not in GENERIC_QUERY_TERMS
        )

        def score(item: str) -> tuple[int, int, int]:
            overlap = sum(1 for term in terms if term and term in item)
            query_match = int(query in item)
            return query_match, overlap, -len(item)

        selected = max(segments, key=score).strip()
        if len(selected) > self.policy.max_claim_chars:
            selected = selected[: self.policy.max_claim_chars].rstrip("，,；;：: ") + "…"
        return selected

    @staticmethod
    def _conflict_claims(
        conflicts: Sequence[AnswerConflict], citation_ids: dict[str, int | None]
    ) -> tuple[AnswerClaim, ...]:
        claims: list[AnswerClaim] = []
        for conflict in conflicts:
            for variant in conflict.variants:
                citation_id = citation_ids.get(variant.source_id)
                if citation_id is None:
                    continue
                claims.append(
                    AnswerClaim(
                        claim_id=f"claim-{len(claims) + 1}",
                        text=variant.evidence_text,
                        citation_ids=(citation_id,),
                        source_ids=(variant.source_id,),
                        volatile=variant.volatile,
                    )
                )
        return tuple(claims)

    @staticmethod
    def _render_claims(claims: Sequence[AnswerClaim]) -> str:
        lines = ["根据当前可核验资料："]
        for claim in claims:
            markers = "".join(f"[{item}]" for item in claim.citation_ids)
            lines.append(f"- {claim.text} {markers}")
        return "\n".join(lines)

    @staticmethod
    def _render_unresolved_conflicts(
        conflicts: Sequence[AnswerConflict], citation_ids: dict[str, int | None]
    ) -> str:
        lines = ["检索到同等优先级来源的记录不一致，因此不能给出确定结论："]
        for conflict in conflicts:
            lines.append(f"- 冲突主题：{conflict.subject}")
            for variant in conflict.variants:
                citation_id = citation_ids.get(variant.source_id)
                if citation_id is not None:
                    lines.append(f"  - {variant.evidence_text} [{citation_id}]")
        lines.append("请以到访日期对应的官方实时页面或现场公告为准。")
        return "\n".join(lines)

    @staticmethod
    def _render_resolved_conflicts(
        conflicts: Sequence[AnswerConflict], citation_ids: dict[str, int | None]
    ) -> str:
        lines = ["检索到不同版本的记录；已按来源权威性、核验时间和适用范围排序："]
        for conflict in conflicts:
            chosen = next(
                item
                for item in conflict.variants
                if item.source_id == conflict.chosen_source_id
            )
            chosen_citation = citation_ids.get(chosen.source_id)
            if chosen_citation is not None:
                lines.append(f"- 优先采用：{chosen.evidence_text} [{chosen_citation}]")
            for variant in conflict.variants:
                if variant.source_id == conflict.chosen_source_id:
                    continue
                citation_id = citation_ids.get(variant.source_id)
                if citation_id is not None:
                    lines.append(f"- 另一版本：{variant.evidence_text} [{citation_id}]")
        return "\n".join(lines)

    def _refusal_result(
        self,
        *,
        candidate_count: int,
        rejection_counts: dict[str, int],
        reason: str,
        min_score: float,
        context_trace: dict | None = None,
    ) -> AnswerResult:
        trace = {
            "schema_version": "scenicpilot-answer-trace-v1",
            "status": "refused",
            "decision_reasons": [reason],
            "candidate_count": candidate_count,
            "usable_count": 0,
            "rejection_counts": rejection_counts,
            "policy": {**self.policy.to_dict(), "min_retrieval_score": min_score},
            "context_selection": context_trace or {},
            "citation_count": 0,
            "claim_count": 0,
            "conflict_count": 0,
            "unresolved_conflict_count": 0,
            "citation_marker_coverage": 1.0,
        }
        return AnswerResult(answer=self.refusal, status="refused", trace=trace)

    def _trace(
        self,
        *,
        answer: str,
        status: str,
        min_score: float,
        candidate_count: int,
        usable_count: int,
        rejection_counts: dict[str, int],
        context_trace: dict,
        citations: Sequence[Citation],
        claims: Sequence[AnswerClaim],
        conflicts: Sequence[AnswerConflict],
        decision_reasons: list[str],
    ) -> dict:
        valid_ids = {item.citation_id for item in citations if item.citation_id is not None}
        cited_ids = {item for claim in claims for item in claim.citation_ids}
        marker_ids = {int(item) for item in CITATION_MARKER.findall(answer)}
        coverage = 1.0 if cited_ids == marker_ids and cited_ids <= valid_ids else 0.0
        return {
            "schema_version": "scenicpilot-answer-trace-v1",
            "status": status,
            "decision_reasons": decision_reasons,
            "candidate_count": candidate_count,
            "usable_count": usable_count,
            "rejection_counts": rejection_counts,
            "policy": {**self.policy.to_dict(), "min_retrieval_score": min_score},
            "context_selection": context_trace,
            "citation_count": len(citations),
            "claim_count": len(claims),
            "conflict_count": len(conflicts),
            "unresolved_conflict_count": sum(not item.resolved for item in conflicts),
            "citation_marker_coverage": coverage,
            "citation_map": {
                str(item.citation_id): item.source_id
                for item in citations
                if item.citation_id is not None
            },
        }
