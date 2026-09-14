from __future__ import annotations

from collections import Counter

from scenic_pilot.models import SearchHit

from .contracts import ChannelCandidate, RetrievalConfig


class ReciprocalRankFusion:
    def __init__(self, config: RetrievalConfig) -> None:
        config.validate()
        self.config = config

    def fuse(
        self,
        keyword: list[ChannelCandidate],
        vector: list[ChannelCandidate],
        *,
        category_hints: tuple[str, ...],
        applied_filters: tuple[str, ...],
        top_k: int,
    ) -> tuple[list[SearchHit], dict[str, int | float]]:
        merged: dict[str, dict[str, ChannelCandidate]] = {}
        for candidate in (*keyword, *vector):
            merged.setdefault(candidate.chunk.chunk_id, {})[candidate.channel] = candidate

        maximum_rrf = 2 / (self.config.rrf_k + 1)
        ranked: list[SearchHit] = []
        for channels in merged.values():
            keyword_candidate = channels.get("keyword")
            vector_candidate = channels.get("vector")
            chunk = (keyword_candidate or vector_candidate).chunk  # type: ignore[union-attr]
            raw_rrf = sum(
                1 / (self.config.rrf_k + candidate.rank)
                for candidate in channels.values()
            )
            category_match = bool(category_hints and chunk.category in category_hints)
            adjusted = raw_rrf * (1 + self.config.category_boost if category_match else 1)
            score = min(1.0, adjusted / maximum_rrf)
            matched_terms = tuple(
                dict.fromkeys(
                    keyword_candidate.matched_terms if keyword_candidate else ()
                )
            )
            ranked.append(
                SearchHit(
                    chunk=chunk,
                    score=round(score, 6),
                    keyword_score=(keyword_candidate.score if keyword_candidate else 0.0),
                    vector_score=(vector_candidate.score if vector_candidate else 0.0),
                    keyword_rank=(keyword_candidate.rank if keyword_candidate else None),
                    vector_rank=(vector_candidate.rank if vector_candidate else None),
                    fusion_score=round(raw_rrf, 8),
                    matched_terms=matched_terms,
                    applied_filters=applied_filters,
                    category_boost_applied=category_match,
                    parent_chunk_id=chunk.parent_chunk_id,
                )
            )

        ranked.sort(
            key=lambda hit: (
                -hit.score,
                -int(hit.category_boost_applied),
                -hit.chunk.authority_level,
                min(hit.keyword_rank or 10**9, hit.vector_rank or 10**9),
                hit.chunk.chunk_id,
            )
        )

        raw_parent_keys = [
            hit.parent_chunk_id or hit.chunk.chunk_id for hit in ranked
        ]
        raw_parent_duplicate_count = len(raw_parent_keys) - len(set(raw_parent_keys))
        selected: list[SearchHit] = []
        seen_parents: set[str] = set()
        source_counts: Counter[str] = Counter()
        drops: dict[str, int | float] = {
            "duplicate_parent": 0,
            "source_limit": 0,
            "raw_parent_duplicate_count": raw_parent_duplicate_count,
            "raw_parent_duplicate_rate": round(
                raw_parent_duplicate_count / len(raw_parent_keys), 6
            ) if raw_parent_keys else 0.0,
        }
        for hit in ranked:
            parent_key = hit.parent_chunk_id or hit.chunk.chunk_id
            if parent_key in seen_parents:
                drops["duplicate_parent"] += 1
                continue
            if source_counts[hit.chunk.source_id] >= self.config.max_parents_per_source:
                drops["source_limit"] += 1
                continue
            seen_parents.add(parent_key)
            source_counts[hit.chunk.source_id] += 1
            selected.append(hit)
            if len(selected) >= top_k:
                break
        return selected, drops
