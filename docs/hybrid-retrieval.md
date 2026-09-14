# 混合检索：设计、决策与验收

已确认决策：本地 `BAAI/bge-small-zh-v1.5`、NumPy 精确余弦搜索、Jieba 与长隆
领域词典、BM25、RRF、同 parent 保留一个 child、同 source 最多两个 parent、
category 默认软加权、确定性查询扩展和30条 retrieval-only 评测集。

## 1. 当前基线

项目现有 `HybridIndex` 已经能跑通最小链路：

```text
query
  ├─ HashingEmbedder（96 维确定性哈希，不具备真正语义能力）
  ├─ token overlap（不是 BM25）
  ├─ equality metadata filter
  └─ 0.55 * vector + 0.45 * keyword + authority boost
                         ↓
                      top-k child chunks
```

该实现适合作为离线测试替身和兼容基线，但不能作为正式检索质量结论：

- 哈希向量存在碰撞，无法稳定处理同义表达。
- 词元重合率没有文档频率和长度归一化。
- 向量分和关键词分量纲不同，直接线性相加缺少校准依据。
- metadata 只支持简单等值过滤。
- 命中 child 后尚未真正回填 parent 上下文。
- 只能看到三个分数，不能解释候选如何被过滤、融合和去重。
- 没有查询集和 Recall/MRR/nDCG 指标，无法用数据调整参数。

## 2. 目标数据流

```text
RetrievalRequest(query, filters, top_k)
                 ↓
        deterministic query normalization
          ├─ 景点别名与领域同义词
          └─ 可选 query expansion
                 ↓
       metadata pre-filtered child corpus
          ├─ KeywordRetriever → keyword candidates
          └─ VectorRetriever  → vector candidates
                 ↓
          RankFusion → fused candidates
                 ↓
       deduplicate + source diversity policy
                 ↓
       parent expansion / context assembly
                 ↓
RetrievalResponse(hits, contexts, trace)
                 ↓
        offline evaluation + failure analysis
```

检索单元继续使用 采集与切块阶段 生成的 child chunk；parent 不参与候选打分，只在 child 命中后
回填上下文。这样既保持小块召回精度，又能让回答节点获得完整章节语境。

## 3. 模块骨架

计划新增 `scenic_pilot/knowledge/retrieval/`：

```text
retrieval/
├─ contracts.py      # Request、Filter、Candidate、Hit、Trace 与 Protocol
├─ query.py          # 规范化、别名词典、可选查询扩展
├─ keyword.py        # 关键词倒排索引与 BM25 通道
├─ vector.py         # Embedder、向量索引及批量持久化
├─ fusion.py         # RRF 或归一化加权融合
├─ policy.py         # metadata 过滤、去重、来源多样性、动态信息策略
├─ context.py        # child 命中后的 parent 回填与上下文预算
├─ service.py        # HybridRetriever 编排入口
├─ persistence.py    # 索引 schema、配置、corpus fingerprint
└─ evaluation.py     # Recall、MRR、nDCG、泄漏率与失败样本
```

配套入口：

```text
scripts/build_retrieval_index.py
scripts/evaluate_retrieval.py
data/eval/retrieval_cases.jsonl
```

现有 `HybridIndex.search()` 暂时保留为兼容入口，内部逐步迁移到 `HybridRetriever`，
避免一次改动破坏 Agent、API 和现有离线测试。

## 4. 固定契约

以下属于工程骨架，不依赖具体算法选择。

### 4.1 请求

```python
@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    scenic_id: str
    top_k: int
    filters: RetrievalFilters
    include_parent: bool = True
```

### 4.2 metadata 过滤

```python
@dataclass(frozen=True)
class RetrievalFilters:
    scenic_id: str
    categories: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    minimum_authority: int | None = None
    volatile: bool | None = None
    demo: bool | None = False
    retrieval_policies: tuple[str, ...] = ()
```

`scenic_id` 和 `demo=False` 默认作为硬过滤，防止其他景区或演示数据串入结果。

### 4.3 可解释轨迹

每个最终 hit 至少保留：

```text
child_chunk_id
parent_chunk_id
keyword_rank / keyword_score
vector_rank / vector_score
fusion_score
matched_terms
applied_filters
authority_level
dedup_or_drop_reason
context_expanded
```

所有对象保持 JSON 可序列化，便于 Agent state、API 调试和 SQLite checkpoint 持久化。

### 4.4 依赖注入

```python
class KeywordRetriever(Protocol): ...
class VectorRetriever(Protocol): ...
class Embedder(Protocol): ...
class RankFusion(Protocol): ...
class RetrievalEvaluator(Protocol): ...
```

真实 embedding、关键词索引和融合算法通过依赖注入替换；测试使用固定向量和内存索引，
不访问网络，也不依赖模型下载。

## 5. 已确认的关键决策

### 语义向量来源

采用方案 A：本地中文 embedding，生产使用真实模型，测试使用 `FakeEmbedder`。

- 优点：离线可运行、成本固定、结果可复现，适合当前 435 个 child 的小语料。
- 代价：首次需要下载模型，仓库不能提交模型文件。

方案 B：云端 embedding API。

- 优点：接入简单，通常具有较好的泛化能力。
- 代价：需要密钥、网络、调用成本和缓存治理，离线测试仍需 Fake。

方案 C：继续只用 HashingEmbedder。

- 优点：零新增依赖。
- 代价：只能当测试基线，不能证明语义召回能力，不建议作为最终选择。

### 向量索引存储

采用方案 A：NumPy 矩阵 + 精确余弦搜索。

- 435 个 child 的规模下性能足够，文件结构透明，容易测试和解释。
- 数据增长到数万块后再迁移 FAISS 或向量数据库。

方案 B：直接使用 FAISS。

- 更接近大规模部署，但当前收益有限，增加二进制依赖和跨平台成本。

方案 C：使用 Chroma 等向量数据库。

- metadata 和持久化能力更完整，但引入额外运行组件，当前项目规模可能过重。

### 关键词检索与中文分词

采用方案 A：BM25 + 中文分词 + 长隆领域词典。

- 能保护“鲸鲨馆”“海牛馆”“飞船酒店”等专有名词。
- 领域词典只保存稳定别名，不保存票价、场次等动态事实。

方案 B：字符 unigram/bigram BM25。

- 不需要分词库，对新词鲁棒，但索引更大、噪声更高。

方案 C：保留当前 token overlap。

- 实现最简单，但缺少 IDF 和文档长度归一化，不建议作为最终方案。

### 两路分数融合

采用方案 A：Reciprocal Rank Fusion（RRF）。

- 只依赖两路排名，不要求关键词分和向量分处于相同量纲。
- 初始可使用 `rrf_k=60`，再通过离线评测调整。

方案 B：min-max 归一化后加权。

- 可以表达语义/关键词偏好，但对候选集合和异常值敏感。

方案 C：学习排序或 reranker。

- 上限更高，但当前缺少标注数据；可作为后续升级，不适合 混合检索阶段 第一版。

### 候选数、去重和 parent 回填

采用参数：

```python
keyword_candidate_k = 20
vector_candidate_k = 20
final_top_k = 8
max_hits_per_source = 2
include_parent = True
```

采用流程：两路各取 20 → 融合 → 同 child 去重 → 同 parent 只保留最高 child →
每个 source 最多保留两个 parent → 取前 8 → 回填 parent。

### 查询规范化范围

采用方案 A：只做确定性别名和同义词扩展。

示例：

```text
鲸鲨馆 ↔ 鲸鲨
飞船乐园 ↔ 宇宙飞船
吃饭 ↔ 餐饮/餐厅
怎么去 ↔ 交通/公交/接驳/穿梭巴
表演 ↔ 演出/剧场/场次
```

方案 B：接入 LLM multi-query。

- 召回覆盖可能更高，但增加延迟、费用和不可复现性。
- 可以保留扩展接口，待离线基线证明不足后再启用。

### metadata 策略

建议固定：

- `scenic_id` 必须硬过滤。
- 默认排除 `demo=True`。
- 用户明确问交通、酒店、餐饮时，可加 category 过滤；意图不明确时不强制 category。
- `authority_level` 先作为排序 tie-breaker，不直接排除低级来源。
- `volatile` 和 `live_check_required` 不从召回中删除，只在结果中保留标记，交由可信回答层处理。

采用 category 软加权；只有调用方明确传入 category filter 时才执行硬过滤，避免意图
分类错误造成零召回。

### 离线评测集与通过标准

检索评测包含 30 条 retrieval-only 查询；端到端评测再扩展到完整的 60 条端到端评测。
每条样本保存：

```text
query
scenic_id
filters
expected_source_ids
expected_categories
must_not_return_source_ids
notes
```

建议指标和初始验收线：

```text
Recall@5 >= 0.90
MRR@10 >= 0.75
cross_scenic_leakage = 0
demo_leakage = 0
```

同时输出失败样本，不允许只有平均分而无法定位具体查询。

## 6. 建议实施顺序

关键决策确认后按以下顺序实现：

1. 定义请求、过滤器、候选、命中和 trace 契约。
2. 实现关键词和向量两个独立通道及持久化格式。
3. 实现融合、去重、来源多样性和 parent 回填。
4. 保留旧 `HybridIndex` 兼容层，接入 Agent `retrieve` 节点。
5. 编写 30 条查询评测集和指标脚本。
6. 根据失败样本调参，而不是只看平均指标。
7. 固化最终配置、索引 schema、corpus fingerprint 和决策记录。

## 7. 混合检索阶段 完成口径

- 关键词、向量、融合、过滤和 parent 回填均可单独测试。
- 真实 embedding 可替换，测试完全离线。
- 每个命中能解释来自哪一路、为何进入最终结果、应用了哪些过滤。
- `scenic_id` 和 `demo` 泄漏为 0。
- 评测结果达到确认后的 Recall/MRR 指标。
- 相同语料、配置和 embedding 版本可以重建相同索引。
- Agent、API 和 CLI 继续通过现有兼容入口运行。

## 8. 最终实现参数

```python
embedding_model = "BAAI/bge-small-zh-v1.5"
embedding_revision = "7999e1d3359715c523056ef9478215996d62a620"
embedding_dimensions = 512

bm25_k1 = 1.5
bm25_b = 0.75
original_query_weight = 2
expansion_term_weight = 1

keyword_candidate_k = 20
vector_candidate_k = 20
rrf_k = 60
category_boost = 0.12
max_parents_per_source = 2
final_top_k = 8
include_parent = True
```

BGE 查询使用短查询检索长文档的中文 instruction；文档向量不添加 instruction。
向量在建索引阶段批量生成并保存为 `vectors.npy`，查询与评测强制从本地缓存加载模型，
不在运行时发起网络请求。

持久化 manifest 同时保存：

- corpus fingerprint
- embedding 模型与固定 revision
- embedding 维度与查询 instruction
- BM25/RRF/去重参数
- chunk 和向量文件 SHA-256

## 9. 查询扩展调优

首轮真实模型评测中，“飞船乐园鲨鱼馆介绍”的目标页面只排在第6。轨迹显示，原因是
将已经规范的“飞船乐园”继续扩展为多个宽泛词，稀释了“鲨鱼馆”这一精确实体。

最终规则调整为：

- 原始查询词在 BM25 中权重为 2。
- 扩展词权重为 1，只进入 BM25 通道。
- BGE 向量只编码原始查询，不编码扩展后的宽泛文本。
- 将“鲨鱼馆”加入长隆领域词典。
- 只把非规范表达映射到规范名称，不对规范名称做反向泛化。

调整后该用例进入 Top 5，30条查询全部通过。

## 10. 最终评测

最终语料共 571 个块：174 个 parent 只用于上下文回填；397 个 child 中排除 88 个
“child 与 parent 均无实质正文”的块，309 个进入关键词和向量索引。

| 指标 | BGE 正式索引 | 目标 |
| --- | ---: | ---: |
| Recall@5 | 1.000000 | ≥ 0.90 |
| MRR@10 | 0.816667 | ≥ 0.75 |
| nDCG@10 | 0.864674 | 观察指标 |
| parent 证据完整度 | 1.000000 | 1.000000 |
| 平均 / P95 上下文估算 tokens | 1388.90 / 2057 | ≤ 2200 预算 |
| 上下文预算耗尽用例 | 1/30 | ≤ 1/30 |
| 跨景区 / demo / 禁返来源泄漏 | 0 / 0 / 0 | 0 / 0 / 0 |
| 通过用例 | 30/30 | 30/30 |

96 维 Hashing 只保留为可重复的离线测试替身，不与最终 BGE 质量结论混用；正式索引
使用本地 `BAAI/bge-small-zh-v1.5`。

## 11. 运行入口

```powershell
# 首次安装真实本地 embedding 依赖
pip install -e ".[dev,ingest,retrieval]"

# 首次下载模型并构建索引
python scripts/build_retrieval_index.py

# 模型已有本地缓存时，完全离线重建
python scripts/build_retrieval_index.py --local-files-only

# 复验上下文预算并运行30条检索评测
python scripts/ablate_context_budget.py
python scripts/evaluate_retrieval.py
```

Agent 使用正式索引时设置：

```text
SCENICPILOT_RETRIEVAL_INDEX_PATH=data/index/zhuhai-chimelong/retrieval
```

未设置时继续使用可重复的 Hashing 索引，保证基础开发和测试不依赖模型下载。

## 12. chunk 参数消融补充

在源级评测之后增加了 30 条人工核验的最小决定性证据片段，并对 child、overlap、
parent 做两阶段消融。最终运行参数更新为：

```python
chunking_strategy = "heading-parent-child-v2"
child_size = 640
child_overlap = 128
parent_size = 960
answer_context_limit = 3
answer_context_token_budget = 2200
```

索引准入会排除“child 和 parent 均只有标题”的无正文块，但保留可路由到有正文 parent
的标题 child。完整方法、上下文预算消融、V1 结构问题和逐项指标见
`docs/chunk-ablation.md`。
