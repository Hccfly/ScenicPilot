from __future__ import annotations

import re
from collections.abc import Mapping

from .contracts import NormalizedQuery


DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "鲸鲨馆": ("鲸鲨", "海洋奇观"),
    "鲨鱼馆": ("鲨鱼",),
    "海牛馆": ("海牛",),
    "宇宙飞船": ("飞船乐园",),
    "飞船酒店": ("长隆飞船酒店", "海洋科学酒店"),
    "吃饭": ("餐饮", "餐厅", "美食"),
    "怎么去": ("交通", "公交", "接驳", "穿梭巴"),
    "表演": ("演出", "剧场", "场次"),
    "住宿": ("酒店", "入住"),
}


CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "attraction": (
        "景点",
        "项目",
        "展馆",
        "动物",
        "过山车",
        "游乐",
        "剧场",
        "鲸鲨",
        "海豚",
        "海牛",
        "江豚",
        "虎鲸",
    ),
    "dining": ("吃饭", "餐饮", "餐厅", "美食", "早餐", "午餐", "晚餐"),
    "transport": ("交通", "公交", "怎么去", "接驳", "穿梭巴", "机场", "口岸"),
    "show": ("表演", "演出", "主题秀", "场次", "巡游", "烟花"),
    "hotel": ("酒店", "住宿", "入住", "客房", "住客", "权益"),
    "announcement": ("公告", "新闻", "通知"),
    "faq": ("常见问题", "退票", "发票", "咨询"),
}


class DomainQueryNormalizer:
    def __init__(
        self,
        aliases: Mapping[str, tuple[str, ...]] | None = None,
        category_terms: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.aliases = dict(aliases or DEFAULT_ALIASES)
        self.category_terms = dict(category_terms or CATEGORY_TERMS)

    def normalize(self, query: str) -> NormalizedQuery:
        normalized = re.sub(r"\s+", " ", query.strip().lower())
        expanded: list[str] = []
        for marker, aliases in self.aliases.items():
            if marker in normalized:
                for alias in aliases:
                    if alias not in normalized and alias not in expanded:
                        expanded.append(alias)

        category_hints = tuple(
            category
            for category, terms in self.category_terms.items()
            if any(term in normalized for term in terms)
        )
        expanded_query = " ".join((normalized, *expanded)).strip()
        return NormalizedQuery(
            original=query,
            normalized=normalized,
            expanded_query=expanded_query,
            expanded_terms=tuple(expanded),
            category_hints=category_hints,
        )
