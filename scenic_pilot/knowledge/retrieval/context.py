from __future__ import annotations

import math
import re
from collections.abc import Iterable

from scenic_pilot.models import Chunk, SearchHit


class ParentContextAssembler:
    def __init__(self, chunks: Iterable[Chunk]) -> None:
        self.parents = {
            chunk.chunk_id: chunk for chunk in chunks if chunk.level == "parent"
        }

    def context_for(self, child: Chunk, *, include_parent: bool) -> tuple[str, bool]:
        if not include_parent or not child.parent_chunk_id:
            return child.content, False
        parent = self.parents.get(child.parent_chunk_id)
        if parent is None:
            return child.content, False
        return parent.content, True


def estimate_context_tokens(text: str) -> int:
    """Return a stable cross-configuration estimate, not billing tokens."""

    pieces = re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s\w]", text)
    return sum(
        max(1, math.ceil(len(piece) / 4))
        if re.fullmatch(r"[A-Za-z0-9_]+", piece)
        else 1
        for piece in pieces
    )


def select_context_hits(
    hits: Iterable[SearchHit],
    *,
    limit: int,
    max_tokens: int | None,
    max_per_source: int = 2,
) -> tuple[list[SearchHit], dict[str, int | bool | None]]:
    """Select distinct parent contexts under count, source, and token budgets."""

    if limit <= 0:
        raise ValueError("context limit must be positive")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("context token budget must be positive when provided")
    if max_per_source <= 0:
        raise ValueError("max_per_source must be positive")

    selected: list[SearchHit] = []
    seen_parents: set[str] = set()
    source_counts: dict[str, int] = {}
    used_tokens = 0
    skipped_by_budget = 0
    for hit in hits:
        parent_key = hit.parent_chunk_id or hit.chunk.chunk_id
        if parent_key in seen_parents:
            continue
        source_count = source_counts.get(hit.chunk.source_id, 0)
        if source_count >= max_per_source:
            continue
        context_tokens = estimate_context_tokens(hit.context or hit.chunk.content)
        if max_tokens is not None and used_tokens + context_tokens > max_tokens:
            skipped_by_budget += 1
            continue
        seen_parents.add(parent_key)
        source_counts[hit.chunk.source_id] = source_count + 1
        selected.append(hit)
        used_tokens += context_tokens
        if len(selected) >= limit:
            break
    return selected, {
        "limit": limit,
        "max_tokens": max_tokens,
        "max_per_source": max_per_source,
        "selected_count": len(selected),
        "used_tokens": used_tokens,
        "skipped_by_budget": skipped_by_budget,
        "budget_exhausted": bool(skipped_by_budget),
    }
