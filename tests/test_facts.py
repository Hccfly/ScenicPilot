from scenic_pilot.knowledge.facts import RuleBasedFactExtractor
from scenic_pilot.models import SourceDocument


def test_rule_based_extractor_emits_four_dynamic_fact_types() -> None:
    document = SourceDocument(
        source_id="official-page",
        scenic_id="zhuhai-chimelong",
        title="当天公告",
        category="operations",
        content=(
            "标准票价格：￥399\n"
            "园区营业时间：10:00-20:00\n"
            "海洋巡游演出场次：13:30、17:00\n"
            "接驳巴士班次：09:00-21:00，每隔15分钟一班"
        ),
        source_url="https://example.test/official",
        verified_at="2026-09-10",
        volatile=True,
        refresh_days=1,
        retrieval_policy="live_check_required",
        document_version="v-example",
    )

    facts = RuleBasedFactExtractor().extract(document)

    assert {item.fact_type for item in facts} == {
        "ticket_price",
        "opening_hours",
        "performance_time",
        "transit_schedule",
    }
    assert all(item.requires_live_check for item in facts)
    assert all(item.source_url == document.source_url for item in facts)
    assert all(item.observed_at == "2026-09-10" for item in facts)


def test_fact_id_is_stable_but_document_version_tracks_change() -> None:
    extractor = RuleBasedFactExtractor()
    base = SourceDocument(
        source_id="official-page",
        scenic_id="zhuhai-chimelong",
        title="票价",
        category="ticket",
        content="标准票价格：399元",
        source_url="https://example.test/official",
        volatile=True,
        retrieval_policy="live_check_required",
        document_version="v-1",
    )
    changed = SourceDocument.from_dict({**base.to_dict(), "document_version": "v-2"})

    first = extractor.extract(base)[0]
    second = extractor.extract(changed)[0]

    assert first.fact_id == second.fact_id
    assert first.document_version != second.document_version
