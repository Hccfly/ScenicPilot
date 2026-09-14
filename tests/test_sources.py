import gzip
import json
from dataclasses import replace
from pathlib import Path

from scenic_pilot.knowledge.sources import (
    ChimelongSelectorExtractor,
    ExtractedContent,
    FetchedPage,
    GzipPageArchive,
    LayeredPageExtractor,
    LivePageDocumentProvider,
)
from scenic_pilot.models import SourceDocument


def make_source() -> SourceDocument:
    return SourceDocument(
        source_id="ocean-test",
        scenic_id="zhuhai-chimelong",
        title="海洋测试页",
        category="attraction",
        content="待抓取",
        source_url="https://www.chimelong.com/zh/oceankingdom/exhibition/test/",
    )


def make_page(body: bytes, source_url: str | None = None) -> FetchedPage:
    url = source_url or make_source().source_url
    return FetchedPage(
        source_url=url,
        final_url=url,
        body=body,
        content_type="text/html; charset=utf-8",
        status_code=200,
        fetched_at="2026-09-10T10:00:00Z",
        headers={"content-type": "text/html; charset=utf-8"},
        raw_sha256="a" * 64,
        fetch_method="httpx",
    )


def test_layered_extractor_records_images_and_missing_fields() -> None:
    html = b"""
    <html><body><div class='exhibitionDetail-wrap'>
      <h1>Ocean World</h1><p>""" + (b"useful content " * 30) + b"""</p>
      <img data-src='/image/a.jpg' alt='dolphin'>
      <img src='/image/b.jpg'>
      <img alt='missing source'>
    </div></body></html>
    """

    extracted = LayeredPageExtractor().extract(make_page(html), make_source())

    assert len(extracted.images) == 3
    assert extracted.images[0].url == "https://www.chimelong.com/image/a.jpg"
    assert extracted.images[0].lazy_loaded is True
    assert extracted.images[1].missing_alt is True
    assert extracted.images[2].missing_src is True


def test_chimelong_selector_extracts_known_detail_container() -> None:
    content = "珠海长隆海洋王国项目介绍。" * 20
    html = f"""
    <html><body><nav>导航噪音</nav><div class="exhibitionDetail-wrap">
      <h1>白鲸剧场</h1><p>{content}</p>
    </div><footer>页脚噪音</footer></body></html>
    """

    text, headings = ChimelongSelectorExtractor().extract(
        html, "https://www.chimelong.com/zh/oceankingdom/exhibition/beluga/"
    )

    assert "白鲸剧场" in text
    assert "导航噪音" not in text
    assert headings == ("白鲸剧场",)


def test_quality_gate_rejects_navigation_stub_but_keeps_short_body() -> None:
    extractor = LayeredPageExtractor()

    assert extractor.minimum_quality == 0.36
    assert extractor.minimum_content_chars == 80
    assert extractor.minimum_topic_purity == 0.85
    assert extractor.quality("顶部") == 0
    assert extractor.quality("有效的景区正文。" * 20) > extractor.minimum_quality


def test_navigation_cleanup_removes_leading_control_line() -> None:
    cleaned, flags = LayeredPageExtractor.clean_navigation_boundaries(
        "顶部\n\n## 尊享特权\n\n有效正文。"
    )

    assert cleaned == "## 尊享特权\n\n有效正文。"
    assert flags == ("navigation_removed:顶部",)


def test_navigation_cleanup_removes_breadcrumb_boundary_but_keeps_later_body() -> None:
    body = "有效景区正文。" * 20
    cleaned, flags = LayeredPageExtractor.clean_navigation_boundaries(
        f"## 景区介绍\n\n{body}\n\n顶部\n\n长隆度假区\n\n"
        "横琴长隆度假区\n\n长隆飞船乐园\n\n新闻公告\n\n## 有效公告标题"
    )

    assert cleaned == f"## 景区介绍\n\n{body}\n\n新闻公告\n\n## 有效公告标题"
    assert flags == ("navigation_boundary_cleaned:顶部",)


def test_navigation_cleanup_preserves_word_inside_body_and_is_idempotent() -> None:
    text = "## 水母城堡\n\n在耸高的滑道顶部一划而下，享受奇幻乐趣。"

    cleaned, flags = LayeredPageExtractor.clean_navigation_boundaries(text)
    cleaned_again, second_flags = LayeredPageExtractor.clean_navigation_boundaries(cleaned)

    assert cleaned == text
    assert flags == ()
    assert cleaned_again == text
    assert second_flags == ()


def test_detail_selector_replaces_high_quality_related_card_pollution() -> None:
    source = replace(make_source(), title="鲨鱼馆")
    html = """
    <html><body><div class="exhibitionDetail-wrap">
      <div class="s-paraFacilityDetail"><div class="container carteDetail">
        <div class="title">鲨鱼馆</div>
        <p class="bannerDesc">鲨鱼馆介绍鲨鱼族群、繁育习性和捕食方式，提供沉浸式海洋科普体验。游客可以观察不同鲨鱼的活动特点，并通过展陈了解海洋生态保护知识。</p>
        <div class="c-paradise-note">
          <span class="NoteTitle">适合年龄：</span><p class="NoteDesc">所有年龄</p>
          <h5 class="NoteTitle">注意事项：</h5>
          <ul><li class="NoteItems">请听从现场工作人员指引并遵守展馆规则。</li></ul>
        </div>
        <div class="title">其他乐园设施</div>
        <div class="c-paradise-other">
          <div class="otherTitle">虎鲸科普剧场</div>
          <p class="desc">虎鲸科普剧场的无关推荐介绍。</p>
        </div>
      </div></div>
    </div></body></html>
    """

    class PollutedTrafilaturaExtractor(LayeredPageExtractor):
        @staticmethod
        def _trafilatura(html: str, url: str) -> str:
            return (
                "## 鲨鱼馆\n" + "鲨鱼馆正文。" * 80
                + "\n## 其他乐园设施\n## 虎鲸科普剧场\n"
                + "无关推荐正文。" * 80
            )

    extracted = PollutedTrafilaturaExtractor().extract(
        make_page(html.encode("utf-8")), source
    )

    assert extracted.method == "chimelong_detail_selector"
    assert "鲨鱼馆" in extracted.text
    assert "适合年龄" in extracted.text
    assert "虎鲸科普剧场" not in extracted.text
    assert "其他乐园设施" not in extracted.text
    assert extracted.topic_purity_score == 1.0
    assert extracted.contamination_flags == ()


def test_hotel_directory_extracts_five_hotels_with_official_supplement() -> None:
    cards = "".join(
        f"""
        <a class="items" href="{href}"><div class="container-box">
          <h5 class="otherTitle">{title}</h5><p class="desc">{title}介绍</p>
        </div></a>
        """
        for title, href in (
            ("长隆横琴湾酒店", "/zh/hengqinbayhotel/"),
            ("长隆企鹅酒店", "/zh/Penguinhotel/"),
            ("长隆马戏酒店", "/zh/circushotel/"),
            ("长隆迎海酒店公寓", "/zh/yinghaiapartments/"),
        )
    )
    html = f"""
    <html><body><div class="s-preparedHotel-container">
      <h5 class="preparedHotel-title">酒店住宿</h5>{cards}
    </div><footer>页脚噪音</footer></body></html>
    """
    url = "https://www.chimelong.com/zh/hotel/"
    source = replace(make_source(), source_id="hotel-overview", source_url=url)

    extracted = LayeredPageExtractor().extract(
        make_page(html.encode("utf-8"), url), source
    )

    assert extracted.method == "chimelong_directory_selector"
    assert extracted.schema_complete is True
    assert len(extracted.discovered_links) == 5
    assert {item["title"] for item in extracted.discovered_links} == {
        "长隆横琴湾酒店",
        "长隆企鹅酒店",
        "长隆马戏酒店",
        "长隆飞船酒店",
        "长隆迎海酒店公寓",
    }
    spaceship = next(
        item for item in extracted.discovered_links if item["title"] == "长隆飞船酒店"
    )
    assert spaceship["url"] == "https://www.chimelong.com/zh/marinesciencehotel/"
    assert spaceship["supplemental"] is True
    assert extracted.structure_flags == ("supplemented_entry:长隆飞船酒店",)
    assert "页脚噪音" not in extracted.text


def test_traffic_directory_extracts_six_child_sources() -> None:
    cards = "".join(
        f'<a class="guide-buse-items-box" href="/zh/traffic/type_{number}.html?tab=1">'
        f'<p class="subtitle">{title}</p></a>'
        for number, title in (
            (1, "省外旅客"),
            (2, "省内旅客"),
            (4, "港澳旅客"),
            (6, "广州长隆度假区旅客"),
            (5, "珠海市内公交车"),
            (7, "度假区内穿梭巴"),
        )
    )
    html = f'<div class="s-guideBus-items">{cards}</div>'
    url = "https://www.chimelong.com/zh/traffic/"
    source = replace(make_source(), source_id="resort-traffic", source_url=url)

    extracted = LayeredPageExtractor().extract(
        make_page(html.encode("utf-8"), url), source
    )

    assert extracted.method == "chimelong_directory_selector"
    assert extracted.schema_complete is True
    assert extracted.structure_flags == ()
    assert len(extracted.discovered_links) == 6
    assert extracted.discovered_links[0]["url"].endswith("type_1.html?tab=1")


def test_traffic_detail_uses_specific_content_container() -> None:
    url = "https://www.chimelong.com/zh/traffic/type_1.html"
    content = "省外旅客可按航空、高铁和落地换乘说明规划路线。" * 20
    html = f"""
    <html><body><nav>导航噪音</nav>
      <div class="s-guide-bus-detail"><h1>其他出行方式</h1><p>{content}</p></div>
      <footer>页脚噪音</footer>
    </body></html>
    """
    source = replace(make_source(), source_id="resort-traffic-type-1", source_url=url)

    extracted = LayeredPageExtractor().extract(
        make_page(html.encode("utf-8"), url), source
    )

    assert extracted.method == "chimelong_selector"
    assert "省外旅客" in extracted.text
    assert "导航噪音" not in extracted.text
    assert "页脚噪音" not in extracted.text


def test_archive_saves_gzip_body_and_fetch_metadata(tmp_path: Path) -> None:
    page = make_page(b"<html><body>archive me</body></html>")
    extracted = LayeredPageExtractor(minimum_quality=0).extract(page, make_source())

    html_path, metadata_path = GzipPageArchive(tmp_path).store(
        make_source(), page, extracted
    )

    with gzip.open(html_path, "rb") as handle:
        assert handle.read() == page.body
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["fetched_at"] == page.fetched_at
    assert metadata["http_status"] == 200
    assert metadata["raw_sha256"] == "a" * 64
    assert metadata["topic_purity_score"] == extracted.topic_purity_score
    assert metadata["contamination_flags"] == list(extracted.contamination_flags)
    assert metadata["cleanup_flags"] == list(extracted.cleanup_flags)
    assert metadata["schema_complete"] == extracted.schema_complete
    assert metadata["structure_flags"] == list(extracted.structure_flags)
    assert metadata["discovered_links"] == list(extracted.discovered_links)


def test_clean_short_detail_does_not_trigger_browser_fallback() -> None:
    class StubFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, source: SourceDocument) -> FetchedPage:
            self.calls += 1
            return make_page(b"<html></html>")

    class CleanDetailExtractor:
        def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
            return ExtractedContent(
                text="## 鲨鱼馆\n\n短而完整的详情正文。",
                method="chimelong_detail_selector",
                quality_score=0.34,
                headings=("鲨鱼馆",),
                images=(),
                topic_purity_score=1.0,
                contamination_flags=(),
            )

    primary = StubFetcher()
    browser = StubFetcher()
    document = LivePageDocumentProvider(
        primary,
        CleanDetailExtractor(),
        browser_fetcher=browser,
    ).materialize(make_source())

    assert primary.calls == 1
    assert browser.calls == 0
    assert document.extraction_quality == 0.34
    assert document.topic_purity_score == 1.0


def test_complete_directory_does_not_trigger_browser_fallback() -> None:
    class StubFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, source: SourceDocument) -> FetchedPage:
            self.calls += 1
            return make_page(b"<html></html>", source.source_url)

    class CompleteDirectoryExtractor:
        def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
            return ExtractedContent(
                text="## 交通指南",
                method="chimelong_directory_selector",
                quality_score=0.0,
                headings=("交通指南",),
                images=(),
                schema_complete=True,
            )

    source = replace(
        make_source(),
        source_id="resort-traffic",
        source_url="https://www.chimelong.com/zh/traffic/",
    )
    primary = StubFetcher()
    browser = StubFetcher()
    document = LivePageDocumentProvider(
        primary,
        CompleteDirectoryExtractor(),
        browser_fetcher=browser,
    ).materialize(source)

    assert primary.calls == 1
    assert browser.calls == 0
    assert document.schema_complete is True


def test_live_provider_records_each_primary_and_browser_attempt() -> None:
    class MemoryAudit:
        run_id = "run-test"

        def __init__(self) -> None:
            self.events = []

        def record(self, event) -> None:
            self.events.append(event)

    class StubFetcher:
        def __init__(self, method: str) -> None:
            self.method = method

        def fetch(self, source: SourceDocument) -> FetchedPage:
            return replace(make_page(b"<html></html>", source.source_url), fetch_method=self.method)

    class AttemptExtractor:
        def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
            accepted = page.fetch_method == "playwright"
            return ExtractedContent(
                text="完整正文。" * 20,
                method="chimelong_selector" if accepted else "trafilatura",
                quality_score=0.8 if accepted else 0.2,
                headings=(),
                images=(),
                selector_used=".perform-wrap" if accepted else None,
            )

    audit = MemoryAudit()
    document = LivePageDocumentProvider(
        StubFetcher("httpx"),
        AttemptExtractor(),
        browser_fetcher=StubFetcher("playwright"),
        attempt_audit=audit,
    ).materialize(
        replace(
            make_source(),
            source_id="show-test",
            source_url="https://www.chimelong.com/zh/test/perform/",
        )
    )

    assert document.extraction_method == "chimelong_selector+playwright"
    assert len(audit.events) == 2
    assert audit.events[0].accepted is False
    assert audit.events[0].failure_reasons == ("quality_below_threshold",)
    assert audit.events[1].accepted is True
    assert audit.events[1].browser_fallback is True
    assert audit.events[1].selector_used == ".perform-wrap"


def test_fetch_error_is_recorded_before_browser_recovery() -> None:
    class MemoryAudit:
        run_id = "run-fetch-error"

        def __init__(self) -> None:
            self.events = []

        def record(self, event) -> None:
            self.events.append(event)

    class FailingFetcher:
        def fetch(self, source: SourceDocument) -> FetchedPage:
            raise RuntimeError("network unavailable")

    class BrowserFetcher:
        def fetch(self, source: SourceDocument) -> FetchedPage:
            return replace(
                make_page(b"<html></html>", source.source_url),
                fetch_method="playwright",
            )

    class SuccessfulExtractor:
        def extract(self, page: FetchedPage, source: SourceDocument) -> ExtractedContent:
            return ExtractedContent(
                text="浏览器恢复后的完整正文。" * 20,
                method="trafilatura",
                quality_score=0.8,
                headings=(),
                images=(),
            )

    audit = MemoryAudit()
    document = LivePageDocumentProvider(
        FailingFetcher(),
        SuccessfulExtractor(),
        browser_fetcher=BrowserFetcher(),
        attempt_audit=audit,
    ).materialize(make_source())

    assert document.extraction_method == "trafilatura+playwright"
    assert len(audit.events) == 2
    assert audit.events[0].failure_reasons == ("fetch_error",)
    assert audit.events[0].error_type == "RuntimeError"
    assert audit.events[1].accepted is True


def test_live_provider_materializes_discovered_traffic_children() -> None:
    links = tuple(
        {
            "title": title,
            "url": f"https://www.chimelong.com/zh/traffic/type_{number}.html",
            "description": "",
            "supplemental": False,
        }
        for number, title in (
            (1, "省外旅客"),
            (2, "省内旅客"),
            (4, "港澳旅客"),
            (6, "广州长隆度假区旅客"),
            (5, "珠海市内公交车"),
            (7, "度假区内穿梭巴"),
        )
    )

    class DiscoveryProvider(LivePageDocumentProvider):
        def __init__(self) -> None:
            pass

        def materialize(self, source: SourceDocument) -> SourceDocument:
            if source.source_url.endswith("/zh/traffic/"):
                return replace(source, content="交通目录", discovered_links=links)
            return replace(source, content=f"{source.title}实时正文")

    source = replace(
        make_source(),
        source_id="resort-traffic",
        source_url="https://www.chimelong.com/zh/traffic/",
        category="transport",
        refresh_days=7,
    )

    documents = DiscoveryProvider().materialize_many(source)

    assert len(documents) == 7
    assert documents[0].source_id == "resort-traffic"
    assert {item.source_id for item in documents[1:]} == {
        "resort-traffic-type-1",
        "resort-traffic-type-2",
        "resort-traffic-type-4",
        "resort-traffic-type-5",
        "resort-traffic-type-6",
        "resort-traffic-type-7",
    }
    assert all(
        item.discovered_from_source_id == "resort-traffic" for item in documents[1:]
    )
    shuttle = next(item for item in documents if item.source_id.endswith("type-7"))
    assert shuttle.refresh_days == 1
