from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from scenic_pilot.models import Chunk

from .contracts import ChannelCandidate, NormalizedQuery, TextTokenizer


DOMAIN_TERMS = (
    "珠海长隆",
    "海洋王国",
    "飞船乐园",
    "横琴湾酒店",
    "企鹅酒店",
    "马戏酒店",
    "飞船酒店",
    "迎海酒店公寓",
    "鲸鲨馆",
    "鲨鱼馆",
    "海牛馆",
    "中华白海豚",
    "海豚剧场",
    "虎鲸科普剧场",
    "长江江豚馆",
    "鹦鹉过山车",
    "海底互动船",
    "穿梭巴",
)


class ChineseDomainTokenizer:
    """Jieba tokenizer with a deterministic fallback for minimal installations."""

    _stopwords = frozenset(
        "的 了 是 在 和 与 或 等 为 把 被 及 于 有 无 能否 是否 什么 怎么 哪些 一个 "
        "一下 请问 想要 可以 需要 里面 哪里 有没有".split()
    )

    def __init__(self, domain_terms: Iterable[str] = DOMAIN_TERMS) -> None:
        self.domain_terms = tuple(dict.fromkeys(domain_terms))
        try:
            import jieba
        except ImportError:
            self._jieba = None
        else:
            tokenizer = jieba.Tokenizer()
            for term in self.domain_terms:
                tokenizer.add_word(term, freq=1_000_000)
            self._jieba = tokenizer

    def tokenize(self, text: str) -> list[str]:
        normalized = text.lower()
        if self._jieba is None:
            return self._fallback(normalized)
        raw = self._jieba.lcut(normalized, cut_all=False, HMM=False)
        return self._clean(raw)

    def _fallback(self, text: str) -> list[str]:
        terms: list[str] = re.findall(r"[a-z0-9]+", text)
        chinese_runs = re.findall(r"[\u4e00-\u9fff]+", text)
        for run in chinese_runs:
            matched = [term for term in self.domain_terms if term in run]
            terms.extend(matched)
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))
        return self._clean(terms)

    def _clean(self, values: Iterable[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            token = value.strip().lower()
            if not token or token in self._stopwords:
                continue
            if not re.search(r"[a-z0-9\u4e00-\u9fff]", token):
                continue
            cleaned.append(token)
        return cleaned


CATEGORY_LABELS = {
    "announcement": "公告 新闻",
    "attraction": "景点 展馆 游乐项目",
    "dining": "餐饮 餐厅 美食",
    "faq": "常见问题 服务咨询",
    "hotel": "酒店 住宿",
    "overview": "度假区 乐园 总览",
    "show": "表演 演出 场次",
    "transport": "交通 公交 接驳 穿梭巴",
}


def searchable_text(chunk: Chunk) -> str:
    headings = " ".join(chunk.heading_path)
    category = CATEGORY_LABELS.get(chunk.category, chunk.category)
    return f"{chunk.title} {chunk.title} {headings} {category} {chunk.content}"


class BM25KeywordRetriever:
    def __init__(
        self,
        chunks: Iterable[Chunk],
        *,
        tokenizer: TextTokenizer | None = None,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 requires k1 > 0 and 0 <= b <= 1")
        self.chunks = list(chunks)
        self.tokenizer = tokenizer or ChineseDomainTokenizer()
        self.k1 = k1
        self.b = b
        self._tokens = [self.tokenizer.tokenize(searchable_text(item)) for item in self.chunks]
        self._counts = [Counter(tokens) for tokens in self._tokens]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._average_length = (
            sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        )
        document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            document_frequency.update(set(tokens))
        total = len(self.chunks)
        self._idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def search(
        self,
        query: NormalizedQuery,
        *,
        allowed_chunk_ids: set[str],
        top_k: int,
    ) -> list[ChannelCandidate]:
        original_terms = self.tokenizer.tokenize(query.normalized)
        expansion_terms = self.tokenizer.tokenize(" ".join(query.expanded_terms))
        query_terms = [*original_terms, *original_terms, *expansion_terms]
        unique_query_terms = tuple(dict.fromkeys(query_terms))
        ranked: list[tuple[Chunk, float, tuple[str, ...]]] = []
        for chunk, counts, length in zip(
            self.chunks, self._counts, self._lengths, strict=True
        ):
            if chunk.chunk_id not in allowed_chunk_ids:
                continue
            matched = tuple(term for term in unique_query_terms if counts.get(term, 0) > 0)
            score = 0.0
            for term in query_terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b
                    + self.b * length / max(self._average_length, 1.0)
                )
                score += self._idf.get(term, 0.0) * frequency * (self.k1 + 1) / denominator
            if score > 0:
                ranked.append((chunk, score, matched))
        ranked.sort(key=lambda item: (-item[1], -item[0].authority_level, item[0].chunk_id))
        return [
            ChannelCandidate(
                chunk=chunk,
                channel="keyword",
                rank=rank,
                score=round(score, 6),
                matched_terms=matched,
            )
            for rank, (chunk, score, matched) in enumerate(ranked[:top_k], 1)
        ]
