from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from scenic_pilot.models import Chunk, SourceDocument


HEADING_PATTERN = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_structured_text(text: str) -> str:
    """Normalize whitespace without destroying Markdown heading boundaries."""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = [re.sub(r"[ \t]+", " ", line).strip() for line in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(normalized)).strip()


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _content_version(content: str) -> str:
    return f"v-{_digest(content, 12)}"


def _anchor(text: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", text.lower()).strip("-")
    return normalized[:80] or "untitled"


def _split_spans(text: str, start: int, end: int, size: int, overlap: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        boundary = min(cursor + size, end)
        if boundary < end:
            sentence_end = max(text.rfind(mark, cursor, boundary) for mark in "。！？；\n")
            if sentence_end > cursor + size // 2:
                boundary = sentence_end + 1
        piece_start = cursor
        piece_end = boundary
        while piece_start < piece_end and text[piece_start].isspace():
            piece_start += 1
        while piece_end > piece_start and text[piece_end - 1].isspace():
            piece_end -= 1
        if piece_start < piece_end:
            spans.append((piece_start, piece_end))
        if boundary >= end:
            break
        cursor = max(cursor + 1, boundary - overlap)
    return spans


def _new_chunk(
    document: SourceDocument,
    *,
    chunk_id: str,
    content: str,
    position: int,
    char_start: int,
    char_end: int,
    strategy_version: str,
    level: str,
    parent_chunk_id: str | None,
    heading_path: tuple[str, ...],
    anchor: str,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_id=document.source_id,
        scenic_id=document.scenic_id,
        title=document.title,
        category=document.category,
        content=content,
        source_url=document.source_url,
        authority_level=document.authority_level,
        verified_at=document.verified_at,
        volatile=document.volatile,
        demo=document.demo,
        position=position,
        status=document.status,
        refresh_days=document.refresh_days,
        retrieval_policy=document.retrieval_policy,
        source_type=document.source_type,
        content_sha256=document.content_sha256,
        char_start=char_start,
        char_end=char_end,
        chunking_strategy=strategy_version,
        level=level,
        parent_chunk_id=parent_chunk_id,
        heading_path=heading_path,
        anchor=anchor,
        content_version=_content_version(content),
    )


def chunk_document(
    document: SourceDocument,
    *,
    chunk_size: int = 240,
    overlap: int = 40,
    strategy_version: str = "char-v1",
) -> list[Chunk]:
    if strategy_version == "heading-parent-child-v1":
        return chunk_document_hierarchy(
            document,
            child_size=chunk_size,
            child_overlap=overlap,
        )
    if strategy_version == "heading-parent-child-v2":
        return chunk_document_hierarchy_v2(
            document,
            child_size=chunk_size,
            child_overlap=overlap,
        )
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between 0 and chunk_size")

    content = normalize_text(document.content)
    if not content:
        return []

    chunks: list[Chunk] = []
    for position, (piece_start, piece_end) in enumerate(
        _split_spans(content, 0, len(content), chunk_size, overlap)
    ):
        piece = content[piece_start:piece_end]
        digest = _digest(f"{strategy_version}:{document.source_id}:{position}:{piece}")
        chunks.append(
            _new_chunk(
                document,
                chunk_id=f"{document.source_id}-{digest}",
                content=piece,
                position=position,
                char_start=piece_start,
                char_end=piece_end,
                strategy_version=strategy_version,
                level="child",
                parent_chunk_id=None,
                heading_path=(document.title,),
                anchor=_anchor(document.title),
            )
        )
    return chunks


@dataclass(frozen=True, slots=True)
class _Section:
    start: int
    end: int
    heading_path: tuple[str, ...]
    anchor: str


@dataclass(frozen=True, slots=True)
class _ParentScope:
    start: int
    end: int
    heading_path: tuple[str, ...]
    anchor: str


def _sections(content: str, document_title: str) -> list[_Section]:
    matches = list(HEADING_PATTERN.finditer(content))
    if not matches:
        return [_Section(0, len(content), (document_title,), _anchor(document_title))]

    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    anchor_counts: dict[str, int] = {}
    if matches[0].start() > 0 and content[: matches[0].start()].strip():
        sections.append(
            _Section(0, matches[0].start(), (document_title,), _anchor(document_title))
        )
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        stack = [(item_level, item) for item_level, item in stack if item_level < level]
        stack.append((level, heading))
        base_anchor = _anchor("/".join(item for _, item in stack))
        occurrence = anchor_counts.get(base_anchor, 0)
        anchor_counts[base_anchor] = occurrence + 1
        stable_anchor = base_anchor if occurrence == 0 else f"{base_anchor}-{occurrence + 1}"
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections.append(
            _Section(match.start(), end, tuple(item for _, item in stack), stable_anchor)
        )
    return sections


def _parent_scopes(content: str, document_title: str) -> list[_ParentScope]:
    """Use H1/H2 subtrees as parent scopes; fall back to the document root."""

    matches = list(HEADING_PATTERN.finditer(content))
    structural = [match for match in matches if len(match.group(1)) <= 2]
    if not structural:
        return [
            _ParentScope(
                start=0,
                end=len(content),
                heading_path=(document_title,),
                anchor=_anchor(document_title),
            )
        ]

    root_level = min(len(match.group(1)) for match in structural)
    roots = [match for match in structural if len(match.group(1)) == root_level]
    scopes: list[_ParentScope] = []
    anchor_counts: dict[str, int] = {}
    if roots[0].start() > 0 and content[: roots[0].start()].strip():
        scopes.append(
            _ParentScope(
                start=0,
                end=roots[0].start(),
                heading_path=(document_title,),
                anchor=_anchor(document_title),
            )
        )
    for index, match in enumerate(roots):
        heading = match.group(2).strip()
        base_anchor = _anchor(heading)
        occurrence = anchor_counts.get(base_anchor, 0)
        anchor_counts[base_anchor] = occurrence + 1
        stable_anchor = base_anchor if occurrence == 0 else f"{base_anchor}-{occurrence + 1}"
        scopes.append(
            _ParentScope(
                start=match.start(),
                end=roots[index + 1].start() if index + 1 < len(roots) else len(content),
                heading_path=(heading,),
                anchor=stable_anchor,
            )
        )
    return scopes


def chunk_document_hierarchy(
    document: SourceDocument,
    *,
    child_size: int = 320,
    child_overlap: int = 48,
    parent_size: int = 1600,
) -> list[Chunk]:
    if child_size <= 0 or parent_size <= 0:
        raise ValueError("child_size and parent_size must be positive")
    if child_overlap < 0 or child_overlap >= child_size:
        raise ValueError("child_overlap must be between 0 and child_size")
    if parent_size < child_size:
        raise ValueError("parent_size must be greater than or equal to child_size")

    content = normalize_structured_text(document.content)
    if not content:
        return []

    strategy = "heading-parent-child-v1"
    chunks: list[Chunk] = []
    position = 0
    for section in _sections(content, document.title):
        parent_spans = _split_spans(content, section.start, section.end, parent_size, 0)
        for parent_index, (parent_start, parent_end) in enumerate(parent_spans):
            parent_key = f"{strategy}:{document.source_id}:{section.anchor}:p{parent_index}"
            parent_id = f"{document.source_id}-parent-{_digest(parent_key)}"
            parent_content = content[parent_start:parent_end]
            chunks.append(
                _new_chunk(
                    document,
                    chunk_id=parent_id,
                    content=parent_content,
                    position=position,
                    char_start=parent_start,
                    char_end=parent_end,
                    strategy_version=strategy,
                    level="parent",
                    parent_chunk_id=None,
                    heading_path=section.heading_path,
                    anchor=section.anchor,
                )
            )
            position += 1
            child_spans = _split_spans(
                content, parent_start, parent_end, child_size, child_overlap
            )
            for child_index, (child_start, child_end) in enumerate(child_spans):
                child_id = f"{document.source_id}-child-{_digest(f'{parent_id}:c{child_index}')}"
                chunks.append(
                    _new_chunk(
                        document,
                        chunk_id=child_id,
                        content=content[child_start:child_end],
                        position=position,
                        char_start=child_start,
                        char_end=child_end,
                        strategy_version=strategy,
                        level="child",
                        parent_chunk_id=parent_id,
                        heading_path=section.heading_path,
                        anchor=section.anchor,
                    )
                )
                position += 1
    return chunks


def chunk_document_hierarchy_v2(
    document: SourceDocument,
    *,
    child_size: int = 640,
    child_overlap: int = 128,
    parent_size: int = 960,
) -> list[Chunk]:
    """Build child chunks inside H1/H2 subtree parents instead of leaf headings.

    V1 made every heading a parent boundary. On list-style official pages that
    produced title-only parents such as ``### 飞船餐厅`` and discarded the
    descriptive sibling text at answer time. V2 keeps leaf headings for child
    anchors while packing them into a document/H1/H2 parent context budget.
    """

    if child_size <= 0 or parent_size <= 0:
        raise ValueError("child_size and parent_size must be positive")
    if child_overlap < 0 or child_overlap >= child_size:
        raise ValueError("child_overlap must be between 0 and child_size")
    if parent_size < child_size:
        raise ValueError("parent_size must be greater than or equal to child_size")

    content = normalize_structured_text(document.content)
    if not content:
        return []

    strategy = "heading-parent-child-v2"
    sections = _sections(content, document.title)
    chunks: list[Chunk] = []
    position = 0
    for scope in _parent_scopes(content, document.title):
        parent_spans = _split_spans(content, scope.start, scope.end, parent_size, 0)
        for parent_index, (parent_start, parent_end) in enumerate(parent_spans):
            parent_key = f"{strategy}:{document.source_id}:{scope.anchor}:p{parent_index}"
            parent_id = f"{document.source_id}-parent-{_digest(parent_key)}"
            chunks.append(
                _new_chunk(
                    document,
                    chunk_id=parent_id,
                    content=content[parent_start:parent_end],
                    position=position,
                    char_start=parent_start,
                    char_end=parent_end,
                    strategy_version=strategy,
                    level="parent",
                    parent_chunk_id=None,
                    heading_path=scope.heading_path,
                    anchor=scope.anchor,
                )
            )
            position += 1
            child_index = 0
            for section in sections:
                child_region_start = max(parent_start, section.start)
                child_region_end = min(parent_end, section.end)
                if child_region_start >= child_region_end:
                    continue
                for child_start, child_end in _split_spans(
                    content,
                    child_region_start,
                    child_region_end,
                    child_size,
                    child_overlap,
                ):
                    child_key = (
                        f"{strategy}:{document.source_id}:{scope.anchor}:"
                        f"p{parent_index}:{section.anchor}:c{child_index}"
                    )
                    child_id = f"{document.source_id}-child-{_digest(child_key)}"
                    chunks.append(
                        _new_chunk(
                            document,
                            chunk_id=child_id,
                            content=content[child_start:child_end],
                            position=position,
                            char_start=child_start,
                            char_end=child_end,
                            strategy_version=strategy,
                            level="child",
                            parent_chunk_id=parent_id,
                            heading_path=section.heading_path,
                            anchor=section.anchor,
                        )
                    )
                    position += 1
                    child_index += 1
    return chunks


def chunk_documents(
    documents: Iterable[SourceDocument],
    *,
    chunk_size: int = 240,
    overlap: int = 40,
    strategy_version: str = "char-v1",
    parent_size: int = 1600,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        if strategy_version == "heading-parent-child-v1":
            chunks.extend(
                chunk_document_hierarchy(
                    document,
                    child_size=chunk_size,
                    child_overlap=overlap,
                    parent_size=parent_size,
                )
            )
        elif strategy_version == "heading-parent-child-v2":
            chunks.extend(
                chunk_document_hierarchy_v2(
                    document,
                    child_size=chunk_size,
                    child_overlap=overlap,
                    parent_size=parent_size,
                )
            )
        else:
            chunks.extend(
                chunk_document(
                    document,
                    chunk_size=chunk_size,
                    overlap=overlap,
                    strategy_version=strategy_version,
                )
            )
    return chunks
