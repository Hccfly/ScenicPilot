from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from scenic_pilot.knowledge.attempt_audit import (
    AttemptAuditSink,
    ExtractionAttemptEvent,
)
from scenic_pilot.models import SourceDocument


@dataclass(frozen=True, slots=True)
class FetchedPage:
    source_url: str
    final_url: str
    body: bytes
    content_type: str
    status_code: int
    fetched_at: str
    headers: dict[str, str]
    raw_sha256: str
    fetch_method: str


@dataclass(frozen=True, slots=True)
class ImageRecord:
    image_id: str
    source_id: str
    url: str | None
    alt: str | None
    title: str | None
    missing_src: bool
    missing_alt: bool
    lazy_loaded: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    text: str
    method: str
    quality_score: float
    headings: tuple[str, ...]
    images: tuple[ImageRecord, ...]
    selector_used: str | None = None
    topic_purity_score: float = 1.0
    contamination_flags: tuple[str, ...] = ()
    cleanup_flags: tuple[str, ...] = ()
    schema_complete: bool | None = None
    structure_flags: tuple[str, ...] = ()
    discovered_links: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class TopicPurityAssessment:
    score: float
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    title: str
    url: str
    description: str = ""
    supplemental: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DirectoryExtraction:
    text: str
    headings: tuple[str, ...]
    entries: tuple[DirectoryEntry, ...]
    schema_complete: bool
    selector_used: str
    structure_flags: tuple[str, ...] = ()


class PageFetcher(Protocol):
    """Network boundary. Implementations own timeout, retry and rate limits."""

    def fetch(self, source: SourceDocument) -> FetchedPage: ...


class PageExtractor(Protocol):
    """Extraction boundary. Implementations turn a fetched page into structured text."""

    def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent: ...


class DocumentProvider(Protocol):
    def materialize(self, source: SourceDocument) -> SourceDocument: ...


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _page(
    *,
    source_url: str,
    final_url: str,
    body: bytes,
    content_type: str,
    status_code: int,
    headers: dict[str, str],
    fetch_method: str,
) -> FetchedPage:
    return FetchedPage(
        source_url=source_url,
        final_url=final_url,
        body=body,
        content_type=content_type,
        status_code=status_code,
        fetched_at=_utc_now(),
        headers=headers,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        fetch_method=fetch_method,
    )


class HttpxPageFetcher:
    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch(self, source: SourceDocument) -> FetchedPage:
        import httpx

        response = httpx.get(
            source.source_url,
            follow_redirects=True,
            timeout=self.timeout_seconds,
            headers={
                "User-Agent": "ScenicPilot-ingestion/0.1 (+source-audit)",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        safe_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "etag", "last-modified", "cache-control"}
        }
        return _page(
            source_url=source.source_url,
            final_url=str(response.url),
            body=response.content,
            content_type=response.headers.get("content-type", ""),
            status_code=response.status_code,
            headers=safe_headers,
            fetch_method="httpx",
        )


class PlaywrightPageFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        browser_channel: str | None = None,
    ) -> None:
        self.timeout_milliseconds = int(timeout_seconds * 1000)
        self.browser_channel = browser_channel

    def fetch(self, source: SourceDocument) -> FetchedPage:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = self._launch_browser(playwright)
            try:
                page = browser.new_page(locale="zh-CN")
                response = page.goto(
                    source.source_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_milliseconds,
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                body = page.content().encode("utf-8")
                status_code = response.status if response else 200
                headers = response.headers if response else {}
                return _page(
                    source_url=source.source_url,
                    final_url=page.url,
                    body=body,
                    content_type=headers.get("content-type", "text/html; charset=utf-8"),
                    status_code=status_code,
                    headers={
                        key: value
                        for key, value in headers.items()
                        if key in {"content-type", "etag", "last-modified", "cache-control"}
                    },
                    fetch_method="playwright",
                )
            finally:
                browser.close()

    def _launch_browser(self, playwright: Any) -> Any:
        channels = (
            (self.browser_channel,) if self.browser_channel else (None, "chrome", "msedge")
        )
        errors: list[str] = []
        for channel in channels:
            try:
                options = {"channel": channel} if channel else {}
                return playwright.chromium.launch(headless=True, **options)
            except Exception as error:
                errors.append(f"{channel or 'bundled'}: {error}")
        raise RuntimeError("no Playwright-compatible browser available: " + " | ".join(errors))


class ChimelongSelectorExtractor:
    """Site adapter for the stable content containers currently used by chimelong.com."""

    HOTEL_DIRECTORY_PATH = "/zh/hotel/"
    TRAFFIC_DIRECTORY_PATH = "/zh/traffic/"
    HOTEL_DIRECTORY_TITLES = (
        "长隆横琴湾酒店",
        "长隆企鹅酒店",
        "长隆马戏酒店",
        "长隆飞船酒店",
        "长隆迎海酒店公寓",
    )
    HOTEL_DIRECTORY_SUPPLEMENTS = (
        DirectoryEntry(
            title="长隆飞船酒店",
            url="https://www.chimelong.com/zh/marinesciencehotel/",
            description="独立官方酒店入口",
            supplemental=True,
        ),
    )
    TRAFFIC_DIRECTORY_TITLES = (
        "省外旅客",
        "省内旅客",
        "港澳旅客",
        "广州长隆度假区旅客",
        "珠海市内公交车",
        "度假区内穿梭巴",
    )

    SELECTORS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("/traffic/type_", (".s-guide-bus-detail", ".c-station-line")),
        ("/exhibition/", (".exhibitionDetail-wrap", ".exhibition-wrap", ".s-paraFacilityDetail")),
        ("/perform/", (".perform-wrap",)),
        ("/foodshopping/", (".foodShopping-wrap",)),
        ("/privilege/", (".privilege-wrap", ".c--privileges-list")),
        ("/news/", (".news-wrap", ".news-list-wrap")),
        ("/question/", (".question-wrap", ".content")),
        ("/traffic/", (".traffic-wrap", ".content")),
    )
    DEFAULT_SELECTORS = ("main", ".content", ".container-box")

    @classmethod
    def candidates(cls, url: str) -> tuple[str, ...]:
        path = urlparse(url).path.lower()
        for marker, selectors in cls.SELECTORS:
            if marker in path:
                return selectors + cls.DEFAULT_SELECTORS
        return cls.DEFAULT_SELECTORS

    def extract(self, html: str, url: str) -> tuple[str, tuple[str, ...]]:
        text, headings, _ = self.extract_with_selector(html, url)
        return text, headings

    def extract_with_selector(
        self, html: str, url: str
    ) -> tuple[str, tuple[str, ...], str | None]:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for node in tree.css("script,style,noscript,nav,footer,.c-hotel-header,.c-hotel-footer"):
            node.decompose()
        detail = self._detail_text(tree)
        if detail is not None:
            return (
                detail[0],
                detail[1],
                ".carteDetail > .title + .bannerDesc + .c-paradise-note",
            )
        for selector in self.candidates(url):
            node = tree.css_first(selector)
            if node is None:
                continue
            text, headings = self._structured_text(node)
            if len(re.sub(r"\s+", "", text)) >= 120:
                return text, headings, selector
        return "", (), None

    def extract_directory(self, html: str, url: str) -> DirectoryExtraction | None:
        path = self._normalized_path(url)
        if path == self.HOTEL_DIRECTORY_PATH:
            return self._hotel_directory(html, url)
        if path == self.TRAFFIC_DIRECTORY_PATH:
            return self._traffic_directory(html, url)
        return None

    @staticmethod
    def _normalized_path(url: str) -> str:
        path = urlparse(url).path.lower()
        return path if path.endswith("/") else f"{path}/"

    @staticmethod
    def is_traffic_detail(url: str) -> bool:
        return re.fullmatch(
            r"/zh/traffic/type_\d+\.html/?",
            urlparse(url).path,
            flags=re.IGNORECASE,
        ) is not None

    def _hotel_directory(self, html: str, url: str) -> DirectoryExtraction:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        entries: list[DirectoryEntry] = []
        for card in tree.css(".s-preparedHotel-container a.items"):
            title_node = card.css_first("h5.otherTitle")
            if title_node is None:
                continue
            title = self._clean_text(title_node)
            href = (card.attributes.get("href") or "").strip()
            description_node = card.css_first("p.desc")
            description = self._clean_text(description_node) if description_node else ""
            if title and href:
                entries.append(
                    DirectoryEntry(
                        title=title,
                        url=urljoin(url, href),
                        description=description,
                    )
                )

        present = {entry.title for entry in entries}
        structure_flags: list[str] = []
        for supplement in self.HOTEL_DIRECTORY_SUPPLEMENTS:
            if supplement.title not in present:
                entries.append(supplement)
                present.add(supplement.title)
                structure_flags.append(f"supplemented_entry:{supplement.title}")

        return self._directory_result(
            heading="酒店住宿",
            entries=entries,
            expected_titles=self.HOTEL_DIRECTORY_TITLES,
            selector_used=".s-preparedHotel-container a.items",
            structure_flags=structure_flags,
        )

    def _traffic_directory(self, html: str, url: str) -> DirectoryExtraction:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        entries: list[DirectoryEntry] = []
        for card in tree.css(".s-guideBus-items a.guide-buse-items-box"):
            title_node = card.css_first("p.subtitle")
            href = (card.attributes.get("href") or "").strip()
            if title_node is None or not href:
                continue
            title = self._clean_text(title_node)
            if title:
                entries.append(DirectoryEntry(title=title, url=urljoin(url, href)))

        return self._directory_result(
            heading="交通指南",
            entries=entries,
            expected_titles=self.TRAFFIC_DIRECTORY_TITLES,
            selector_used=".s-guideBus-items a.guide-buse-items-box",
        )

    def _directory_result(
        self,
        *,
        heading: str,
        entries: list[DirectoryEntry],
        expected_titles: tuple[str, ...],
        selector_used: str,
        structure_flags: list[str] | None = None,
    ) -> DirectoryExtraction:
        deduplicated: list[DirectoryEntry] = []
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            key = (entry.title, entry.url)
            if key not in seen:
                seen.add(key)
                deduplicated.append(entry)

        flags = list(structure_flags or [])
        present = {entry.title for entry in deduplicated}
        for title in expected_titles:
            if title not in present:
                flags.append(f"missing_entry:{title}")
        for entry in deduplicated:
            parsed = urlparse(entry.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                flags.append(f"invalid_entry_url:{entry.title}")

        missing_or_invalid = any(
            flag.startswith(("missing_entry:", "invalid_entry_url:")) for flag in flags
        )
        parts = [f"## {heading}"]
        headings = [heading]
        for entry in deduplicated:
            parts.append(f"### {entry.title}")
            headings.append(entry.title)
            if entry.description:
                parts.append(entry.description)
            parts.append(f"官方入口：{entry.url}")
        return DirectoryExtraction(
            text="\n\n".join(parts),
            headings=tuple(headings),
            entries=tuple(deduplicated),
            schema_complete=bool(deduplicated) and not missing_or_invalid,
            selector_used=selector_used,
            structure_flags=tuple(dict.fromkeys(flags)),
        )

    @staticmethod
    def _clean_text(node: Any) -> str:
        return re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()

    @staticmethod
    def _detail_text(tree: Any) -> tuple[str, tuple[str, ...]] | None:
        container = tree.css_first(".carteDetail")
        description = tree.css_first(".carteDetail > .bannerDesc")
        if container is None or description is None:
            return None

        title_node = tree.css_first(".carteDetail > .title")
        title = (
            re.sub(r"\s+", " ", title_node.text(separator=" ", strip=True)).strip()
            if title_node is not None
            else ""
        )
        description_text = re.sub(
            r"\s+", " ", description.text(separator=" ", strip=True)
        ).strip()
        parts = [f"## {title}"] if title else []
        headings = [title] if title else []
        if description_text:
            parts.append(description_text)

        note = tree.css_first(".carteDetail > .c-paradise-note")
        if note is not None:
            note_text = re.sub(
                r"\s+", " ", note.text(separator=" ", strip=True)
            ).strip()
            if note_text:
                parts.append(note_text)
        text = "\n\n".join(parts)
        return (text, tuple(headings)) if len(re.sub(r"\s+", "", text)) >= 60 else None

    @staticmethod
    def detail_title(html: str) -> str | None:
        from selectolax.parser import HTMLParser

        node = HTMLParser(html).css_first(".carteDetail > .title")
        if node is None:
            return None
        return re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip() or None

    @staticmethod
    def related_titles(html: str) -> tuple[str, ...]:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        titles: list[str] = []
        for node in tree.css(
            ".c-paradise-other .otherTitle,.c-paradise-other .title,"
            ".c-paradise-other h3,.c-paradise-other h4"
        ):
            title = re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()
            if title and title not in titles:
                titles.append(title)
        return tuple(titles)

    @staticmethod
    def _structured_text(node: Any) -> tuple[str, tuple[str, ...]]:
        parts: list[str] = []
        headings: list[str] = []
        seen: set[str] = set()
        selected = node.css("h1,h2,h3,h4,p,li,.title,.NoteTitle,.NoteDesc,.desc")
        for item in selected:
            text = re.sub(r"\s+", " ", item.text(separator=" ", strip=True)).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            tag = (item.tag or "").lower()
            classes = set((item.attributes.get("class") or "").split())
            is_heading = tag in {"h1", "h2", "h3", "h4"} or "title" in classes or "NoteTitle" in classes
            if is_heading:
                level = int(tag[1]) if tag.startswith("h") and tag[1:].isdigit() else 2
                parts.append(f"{'#' * level} {text}")
                headings.append(text)
            else:
                parts.append(text)
        if not parts:
            text = re.sub(r"\s+", " ", node.text(separator=" ", strip=True)).strip()
            return text, ()
        return "\n\n".join(parts), tuple(headings)


class LayeredPageExtractor:
    NAVIGATION_BOUNDARY_TERMS = frozenset({"顶部", "返回"})
    SITE_BREADCRUMB_MARKERS = frozenset(
        {
            "长隆度假区",
            "横琴长隆度假区",
            "长隆海洋王国",
            "长隆飞船乐园",
        }
    )

    def __init__(
        self,
        *,
        minimum_quality: float = 0.36,
        minimum_content_chars: int = 80,
        minimum_topic_purity: float = 0.85,
    ) -> None:
        self.minimum_quality = minimum_quality
        self.minimum_content_chars = minimum_content_chars
        self.minimum_topic_purity = minimum_topic_purity
        self.selector_extractor = ChimelongSelectorExtractor()

    def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
        html = page.body.decode("utf-8", errors="replace")
        images = self._images(html, page.final_url, source.source_id)
        directory = self.selector_extractor.extract_directory(html, page.final_url)
        if directory is not None:
            text, cleanup_flags = self.clean_navigation_boundaries(directory.text)
            purity = self.topic_purity(text, html)
            return ExtractedContent(
                text=text,
                method="chimelong_directory_selector",
                quality_score=self.quality(text),
                headings=self._headings(text),
                images=images,
                selector_used=directory.selector_used,
                topic_purity_score=purity.score,
                contamination_flags=purity.flags,
                cleanup_flags=cleanup_flags,
                schema_complete=directory.schema_complete,
                structure_flags=directory.structure_flags,
                discovered_links=tuple(entry.to_dict() for entry in directory.entries),
            )
        if self.selector_extractor.is_traffic_detail(page.final_url):
            text, headings, selector_used = self.selector_extractor.extract_with_selector(
                html, page.final_url
            )
            text, cleanup_flags = self.clean_navigation_boundaries(text)
            purity = self.topic_purity(text, html)
            return ExtractedContent(
                text=text,
                method="chimelong_selector",
                quality_score=self.quality(text),
                headings=self._headings(text) or headings,
                images=images,
                selector_used=selector_used,
                topic_purity_score=purity.score,
                contamination_flags=purity.flags,
                cleanup_flags=cleanup_flags,
            )
        text = self._trafilatura(html, page.final_url)
        text, cleanup_flags = self.clean_navigation_boundaries(text)
        method = "trafilatura"
        headings = self._headings(text)
        quality = self.quality(text)
        purity = self.topic_purity(text, html)
        if quality < self.minimum_quality or purity.score < self.minimum_topic_purity:
            (
                selected_text,
                selected_headings,
                selected_selector,
            ) = self.selector_extractor.extract_with_selector(html, page.final_url)
            selected_text, selected_cleanup_flags = self.clean_navigation_boundaries(
                selected_text
            )
            selected_quality = self.quality(selected_text)
            selected_purity = self.topic_purity(selected_text, html)
            clean_detail = (
                self.selector_extractor.detail_title(html) is not None
                and selected_quality > 0
                and selected_purity.score >= self.minimum_topic_purity
            )
            if selected_quality > quality or (
                (selected_quality >= self.minimum_quality or clean_detail)
                and selected_purity.score > purity.score
            ):
                text = selected_text
                headings = tuple(
                    heading for heading in selected_headings if heading in selected_text
                )
                quality = selected_quality
                purity = selected_purity
                cleanup_flags = selected_cleanup_flags
                selector_used = selected_selector
                method = "chimelong_detail_selector" if clean_detail else "chimelong_selector"
            else:
                selector_used = None
        else:
            selector_used = None
        return ExtractedContent(
            text=text.strip(),
            method=method,
            quality_score=quality,
            headings=headings,
            images=images,
            selector_used=selector_used,
            topic_purity_score=purity.score,
            contamination_flags=purity.flags,
            cleanup_flags=cleanup_flags,
        )

    @classmethod
    def clean_navigation_boundaries(cls, text: str) -> tuple[str, tuple[str, ...]]:
        lines = text.splitlines()
        flags: list[str] = []
        index = 0
        while index < len(lines):
            term = lines[index].strip()
            if term not in cls.NAVIGATION_BOUNDARY_TERMS:
                index += 1
                continue

            before = "\n".join(lines[:index])
            before_chars = len(re.sub(r"\s+|[#*_`>-]", "", before))
            boundary_end = cls._breadcrumb_boundary_end(lines, index + 1)
            if before_chars >= 80 and boundary_end is not None:
                lines = lines[:index] + lines[boundary_end:]
                flags.append(f"navigation_boundary_cleaned:{term}")
                continue

            del lines[index]
            flags.append(f"navigation_removed:{term}")

        return "\n".join(lines).strip(), tuple(dict.fromkeys(flags))

    @classmethod
    def _breadcrumb_boundary_end(cls, lines: list[str], start: int) -> int | None:
        cursor = start
        hits = 0
        while cursor < len(lines):
            line = lines[cursor].strip()
            if not line:
                cursor += 1
                continue
            if line not in cls.SITE_BREADCRUMB_MARKERS:
                break
            hits += 1
            cursor += 1
        return cursor if hits >= 2 else None

    @staticmethod
    def _trafilatura(html: str, url: str) -> str:
        import trafilatura

        return (
            trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_links=False,
                include_images=False,
                include_formatting=True,
                favor_precision=True,
            )
            or ""
        )

    @staticmethod
    def _headings(text: str) -> tuple[str, ...]:
        return tuple(
            match.group(1).strip()
            for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", text)
        )

    def quality(self, text: str) -> float:
        compact = re.sub(r"\s+", "", text)
        if len(compact) < self.minimum_content_chars:
            return 0.0
        length_score = min(len(compact) / 700, 1.0)
        cjk_ratio = len(re.findall(r"[\u4e00-\u9fff]", compact)) / len(compact)
        boilerplate_hits = sum(
            text.count(term)
            for term in ("立即购票", "网站地图", "关注长隆", "您的浏览器版本过低")
        )
        penalty = min(boilerplate_hits * 0.05, 0.3)
        return round(max(0.0, 0.7 * length_score + 0.3 * cjk_ratio - penalty), 6)

    def topic_purity(self, text: str, html: str) -> TopicPurityAssessment:
        flags: list[str] = []
        penalty = 0.0
        for marker in ("其他展区", "其他乐园设施", "其他酒店设施"):
            if marker in text:
                flags.append(f"related_section:{marker}")
                penalty += 0.35

        for title in self.selector_extractor.related_titles(html):
            if title in text:
                flags.append(f"related_title:{title}")
                penalty += 0.18

        detail_title = self.selector_extractor.detail_title(html)
        if detail_title and detail_title not in text:
            flags.append(f"missing_detail_title:{detail_title}")
            penalty += 0.20

        for navigation in ("顶部", "返回"):
            if re.search(rf"(?m)^\s*{re.escape(navigation)}\s*$", text):
                flags.append(f"navigation:{navigation}")
                penalty += 0.02

        return TopicPurityAssessment(
            score=round(max(0.0, 1.0 - min(penalty, 1.0)), 6),
            flags=tuple(dict.fromkeys(flags)),
        )

    @staticmethod
    def _images(html: str, base_url: str, source_id: str) -> tuple[ImageRecord, ...]:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        records: list[ImageRecord] = []
        for index, node in enumerate(tree.css("img")):
            attributes = node.attributes
            raw_src = next(
                (
                    attributes.get(name)
                    for name in ("src", "data-src", "data-original", "data-lazy")
                    if attributes.get(name)
                ),
                None,
            )
            resolved = urljoin(base_url, raw_src) if raw_src else None
            alt = (attributes.get("alt") or "").strip() or None
            title = (attributes.get("title") or "").strip() or None
            image_id = hashlib.sha256(
                f"{source_id}:{index}:{resolved or '<missing>'}".encode("utf-8")
            ).hexdigest()[:20]
            records.append(
                ImageRecord(
                    image_id=image_id,
                    source_id=source_id,
                    url=resolved,
                    alt=alt,
                    title=title,
                    missing_src=resolved is None,
                    missing_alt=alt is None,
                    lazy_loaded=raw_src is not None and not attributes.get("src"),
                )
            )
        return tuple(records)


class GzipPageArchive:
    def __init__(self, root: Path) -> None:
        self.root = root

    def store(
        self,
        source: SourceDocument,
        page: FetchedPage,
        extracted: ExtractedContent,
    ) -> tuple[Path, Path]:
        directory = self.root / source.scenic_id / source.source_id
        directory.mkdir(parents=True, exist_ok=True)
        html_path = directory / f"{page.raw_sha256}.html.gz"
        metadata_path = directory / f"{page.raw_sha256}.json"
        if not html_path.exists():
            with gzip.open(html_path, "wb", compresslevel=9) as handle:
                handle.write(page.body)
        metadata = {
            "source_id": source.source_id,
            "source_url": page.source_url,
            "final_url": page.final_url,
            "fetched_at": page.fetched_at,
            "http_status": page.status_code,
            "content_type": page.content_type,
            "headers": page.headers,
            "raw_sha256": page.raw_sha256,
            "fetch_method": page.fetch_method,
            "extraction_method": extracted.method,
            "selector_used": extracted.selector_used,
            "extraction_quality": extracted.quality_score,
            "topic_purity_score": extracted.topic_purity_score,
            "contamination_flags": list(extracted.contamination_flags),
            "cleanup_flags": list(extracted.cleanup_flags),
            "schema_complete": extracted.schema_complete,
            "structure_flags": list(extracted.structure_flags),
            "discovered_links": list(extracted.discovered_links),
            "image_count": len(extracted.images),
            "missing_src_count": sum(item.missing_src for item in extracted.images),
            "missing_alt_count": sum(item.missing_alt for item in extracted.images),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return html_path, metadata_path


class ManifestDocumentProvider:
    """Offline baseline: use the reviewed summary already stored in the manifest."""

    def materialize(self, source: SourceDocument) -> SourceDocument:
        return replace(source, extraction_method="manifest_summary")


class LivePageDocumentProvider:
    def __init__(
        self,
        fetcher: PageFetcher,
        extractor: PageExtractor,
        *,
        browser_fetcher: PageFetcher | None = None,
        archive: GzipPageArchive | None = None,
        attempt_audit: AttemptAuditSink | None = None,
        minimum_quality: float = 0.36,
        minimum_topic_purity: float = 0.85,
    ) -> None:
        self.fetcher = fetcher
        self.extractor = extractor
        self.browser_fetcher = browser_fetcher
        self.archive = archive
        self.attempt_audit = attempt_audit
        self.minimum_quality = minimum_quality
        self.minimum_topic_purity = minimum_topic_purity

    def materialize(self, source: SourceDocument) -> SourceDocument:
        page, extracted, error = self._attempt(
            source,
            self.fetcher,
            attempt_number=1,
            browser_fallback=False,
        )
        needs_browser = extracted is None or not self._acceptable(extracted)
        if needs_browser and self.browser_fetcher:
            rendered_page, rendered, rendered_error = self._attempt(
                source,
                self.browser_fetcher,
                attempt_number=2,
                browser_fallback=True,
            )
            if rendered is not None and (
                extracted is None or self._rank(rendered) >= self._rank(extracted)
            ):
                page = rendered_page
                extracted = replace(rendered, method=f"{rendered.method}+playwright")
            elif extracted is None:
                error = rendered_error or error
        if page is None or extracted is None:
            if error is not None:
                raise error
            raise RuntimeError(f"no extraction attempt succeeded for {source.source_id}")
        if not extracted.text.strip():
            raise ValueError(f"extractor returned empty content for {source.source_id}")
        if self.archive:
            self.archive.store(source, page, extracted)
        return replace(
            source,
            content=extracted.text,
            extraction_method=extracted.method,
            selector_used=extracted.selector_used,
            fetched_at=page.fetched_at,
            http_status=page.status_code,
            raw_content_sha256=page.raw_sha256,
            extraction_quality=extracted.quality_score,
            topic_purity_score=extracted.topic_purity_score,
            contamination_flags=extracted.contamination_flags,
            cleanup_flags=extracted.cleanup_flags,
            schema_complete=extracted.schema_complete,
            structure_flags=extracted.structure_flags,
            discovered_links=extracted.discovered_links,
            headings=extracted.headings,
            image_records=tuple(item.to_dict() for item in extracted.images),
        )

    def materialize_many(self, source: SourceDocument) -> tuple[SourceDocument, ...]:
        document = self.materialize(source)
        if ChimelongSelectorExtractor._normalized_path(source.source_url) != "/zh/traffic/":
            return (document,)

        discovered: list[SourceDocument] = []
        for entry in document.discovered_links:
            url = str(entry.get("url") or "")
            match = re.fullmatch(
                r"https://(?:www\.)?chimelong\.com/zh/traffic/type_(\d+)\.html(?:\?.*)?",
                url,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            child = replace(
                source,
                source_id=f"{source.source_id}-type-{match.group(1)}",
                title=f"{title}交通指南",
                category="transport",
                content=f"{title}官方交通详情页，线路和班次以实时页面为准。",
                source_url=url,
                refresh_days=1 if match.group(1) == "7" else source.refresh_days,
                discovered_from_source_id=source.source_id,
            )
            discovered.append(self.materialize(child))
        return (document, *discovered)

    def _attempt(
        self,
        source: SourceDocument,
        fetcher: PageFetcher,
        *,
        attempt_number: int,
        browser_fallback: bool,
    ) -> tuple[FetchedPage | None, ExtractedContent | None, Exception | None]:
        try:
            page = fetcher.fetch(source)
        except Exception as error:
            self._record_attempt(
                ExtractionAttemptEvent.create(
                    run_id=self._audit_run_id(),
                    source_id=source.source_id,
                    source_url=source.source_url,
                    attempt_number=attempt_number,
                    fetch_method=type(fetcher).__name__,
                    browser_fallback=browser_fallback,
                    accepted=False,
                    failure_reasons=("fetch_error",),
                    error_type=type(error).__name__,
                    error_message=str(error)[:500],
                )
            )
            return None, None, error

        try:
            extracted = self._extract(page, source)
        except Exception as error:
            reason = (
                f"http_status_{page.status_code}"
                if page.status_code >= 400
                else "extraction_error"
            )
            self._record_attempt(
                ExtractionAttemptEvent.create(
                    run_id=self._audit_run_id(),
                    source_id=source.source_id,
                    source_url=source.source_url,
                    final_url=page.final_url,
                    attempt_number=attempt_number,
                    fetch_method=page.fetch_method,
                    browser_fallback=browser_fallback,
                    attempted_at=page.fetched_at,
                    http_status=page.status_code,
                    raw_sha256=page.raw_sha256,
                    accepted=False,
                    failure_reasons=(reason,),
                    error_type=type(error).__name__,
                    error_message=str(error)[:500],
                )
            )
            return page, None, error

        accepted = self._acceptable(extracted)
        self._record_attempt(
            ExtractionAttemptEvent.create(
                run_id=self._audit_run_id(),
                source_id=source.source_id,
                source_url=source.source_url,
                final_url=page.final_url,
                attempt_number=attempt_number,
                fetch_method=page.fetch_method,
                browser_fallback=browser_fallback,
                attempted_at=page.fetched_at,
                http_status=page.status_code,
                raw_sha256=page.raw_sha256,
                extraction_method=extracted.method,
                selector_used=extracted.selector_used,
                content_chars=len(re.sub(r"\s+|[#*_`>-]", "", extracted.text)),
                quality_score=extracted.quality_score,
                topic_purity_score=extracted.topic_purity_score,
                schema_complete=extracted.schema_complete,
                accepted=accepted,
                failure_reasons=self._failure_reasons(extracted),
                contamination_flags=extracted.contamination_flags,
                cleanup_flags=extracted.cleanup_flags,
                structure_flags=extracted.structure_flags,
            )
        )
        return page, extracted, None

    def _audit_run_id(self) -> str:
        return self.attempt_audit.run_id if self.attempt_audit is not None else "unrecorded"

    def _record_attempt(self, event: ExtractionAttemptEvent) -> None:
        if self.attempt_audit is not None:
            self.attempt_audit.record(event)

    def _failure_reasons(self, extracted: ExtractedContent) -> tuple[str, ...]:
        reasons: list[str] = []
        if not extracted.text.strip():
            reasons.append("empty_content")
        detail_quality_ok = (
            extracted.method.startswith("chimelong_detail_selector")
            and extracted.quality_score > 0
        )
        directory_schema_ok = (
            extracted.method.startswith("chimelong_directory_selector")
            and extracted.schema_complete is True
        )
        if (
            extracted.quality_score < self.minimum_quality
            and not detail_quality_ok
            and not directory_schema_ok
        ):
            reasons.append("quality_below_threshold")
        if extracted.topic_purity_score < self.minimum_topic_purity:
            reasons.append("topic_purity_below_threshold")
        if extracted.schema_complete is False:
            reasons.append("schema_incomplete")
        return tuple(reasons)

    def _extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
        if page.status_code >= 400:
            raise RuntimeError(f"fetch failed for {source.source_id}: HTTP {page.status_code}")
        return self.extractor.extract(page, source)

    def _rank(self, extracted: ExtractedContent) -> tuple[bool, bool, float]:
        acceptable = self._acceptable(extracted)
        combined = 0.45 * extracted.quality_score + 0.55 * extracted.topic_purity_score
        return acceptable, extracted.schema_complete is True, round(combined, 6)

    def _acceptable(self, extracted: ExtractedContent) -> bool:
        detail_quality_ok = (
            extracted.method.startswith("chimelong_detail_selector")
            and extracted.quality_score > 0
        )
        directory_schema_ok = (
            extracted.method.startswith("chimelong_directory_selector")
            and extracted.schema_complete is True
        )
        return (
            (
                extracted.quality_score >= self.minimum_quality
                or detail_quality_ok
                or directory_schema_ok
            )
            and extracted.topic_purity_score >= self.minimum_topic_purity
        )
