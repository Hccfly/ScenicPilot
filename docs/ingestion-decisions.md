# 长隆文档采集与索引决策记录

本文记录 ScenicPilot 文档流水线从初始方案、真实数据审计到参数定稿的决策过程。
它回答的不只是“最终用了哪些参数”，还包括每个参数解决了什么问题、为什么没有采用
更简单的统一规则，以及后续应该在什么证据下重新评估。

## 1. 初始范围

采集与切块阶段 首先确定五项工程目标：

1. 抓取链路采用 `HTTPX → trafilatura → 长隆专用 selector → Playwright`。
2. 切块采用标题感知的 parent-child 两级结构。
3. 将票价、营业时间、演出场次和交通班次抽取为结构化事实。
4. 保存压缩 HTML、抓取时间、HTTP 状态和内容哈希，原始归档不提交 Git。
5. 标题锚点不变时尽量保持 chunk ID，通过版本字段记录内容变化。

这一阶段只确定可运行框架，不提前凭经验固定质量阈值和 selector。具体参数必须由
真实页面样本和失败分布驱动。

## 2. 用 50 条真实来源确定质量门禁

完成 50 条珠海长隆官网来源的 live 抓取后，对正文完整性、导航混入和浏览器回退
需求进行逐页审计。当时样本分布为：

- 无效正文最高质量分：`0.302`
- 有效正文最低质量分：`0.421199`
- 两类样本之间存在可分离区间

因此将原质量阈值从 `0.42` 调整为 `0.36`，并增加 80 个有效字符的硬门槛：

```python
minimum_quality = 0.36
minimum_content_chars = 80
```

质量分负责识别正文过短和模板噪声，但不负责判断正文是否属于当前页面主题。
低于 80 个有效字符的普通正文不依赖质量分，直接进入下一层抽取或浏览器回退。

## 3. 从质量评分扩展到主题纯度

审计发现 21 个详情页虽然质量分可达到 `0.96`，仍混入“其他展区/其他设施”推荐卡片。
这证明高质量分不等于高主题纯度，继续提高或降低统一质量阈值都不能解决串页问题。

因此增加独立的主题纯度门禁：

```python
minimum_topic_purity = 0.85
```

检测过程：

1. 从 `.c-paradise-other` 获取关联卡片标题。
2. 检查正文是否存在“其他展区/其他设施”等关联区分界词。
3. 检查正文是否包含其他项目标题。
4. 纯度不足时，使用详情页专用字段重新提取。

详情页最终只组合以下字段：

```css
.carteDetail > .title
.carteDetail > .bannerDesc
.carteDetail > .c-paradise-note
```

50 页归档回放中，21 个受污染详情页全部被替换，关联内容残留降为 0，详情页主题
纯度达到 `1.0`。

## 4. 将目录页与详情页分开处理

`resort-traffic` 和 `hotel-overview` 是负责发现入口的目录聚合页，不是普通详情正文。
如果继续套用正文长度门禁，短目录可能被拒绝，交通子页面和酒店入口也可能丢失。

因此为目录页定义独立结构契约，用 `schema_complete` 检查预期条目及 URL，而不是
只比较正文质量分。

### 4.1 酒店目录

酒店目录 selector：

```css
.s-preparedHotel-container a.items
```

预期形成五家酒店：

- 长隆横琴湾酒店
- 长隆企鹅酒店
- 长隆马戏酒店
- 长隆迎海酒店公寓
- 长隆飞船酒店

当前酒店总览 DOM 只展示四家。长隆飞船酒店已有独立官方入口，因此作为显式补充项
加入，并记录 `supplemented_entry:长隆飞船酒店`，而不是把补充数据伪装成页面原文。

### 4.2 交通目录

交通目录 selector：

```css
.s-guideBus-items a.guide-buse-items-box
```

目录发现并物化六个交通详情页：`type_1`、`type_2`、`type_4`、`type_5`、`type_6`
和 `type_7`。交通详情统一使用：

```css
.s-guide-bus-detail
```

这样可以保留具体交通路线，同时避免页头、页脚和目录卡片污染正文。

## 5. 清理“返回/顶部”导航残留

审计发现约 20 个页面存在“返回”或“顶部”级别的轻微导航残留。不能全局删除这些
词语，因为“在滑道顶部一划而下”等业务正文也可能合法包含“顶部”。

最终采用上下文感知规则：

- 只匹配独立成行的 `返回` 或 `顶部`。
- 位于正文开头时，只删除该控制行。
- 位于有效正文之后且紧邻长隆站点面包屑时，删除控制行及连续面包屑边界。
- 保留边界之后的公告、设施标签和业务正文。
- 重复执行清理得到相同结果，保证幂等。

清理动作分别记录为 `navigation_removed` 和 `navigation_boundary_cleaned`。最终结果：

- 19 个文档执行导航清理。
- 其中 6 个属于正文中间的面包屑边界。
- 导航残留文档数为 0。
- 正文中的“顶部”等词语没有被误删。

## 6. 从最终结果追踪到每次抓取尝试

只记录最终成功文档会隐藏前序失败，因此进一步将每一次 HTTPX 或 Playwright 尝试
记录为一条追加写入、不可覆盖的审计事件。事件包括：

```text
run_id
source_id
page_type
attempt_number
fetch_method
http_status
extraction_method
selector_used
quality_score
topic_purity_score
schema_complete
accepted
failure_reasons
cleanup_flags
error_type
raw_sha256
```

事件按 `exhibition`、`perform`、`food`、`privilege`、`traffic` 等页面类型聚合，
同时统计抽取方法和 selector 的覆盖量、成功率、平均质量和平均主题纯度。

本轮 50 条种子来源展开为 56 个最终文档，共产生 57 次抓取尝试。唯一失败来自
`spaceship-performances`：HTTP 状态为 200，但静态响应抽取结果为空；Playwright
渲染后恢复成功。因此将其归因为动态渲染问题，而不是 selector 选择错误。

## 7. 最终参数

### 7.1 抽取门禁

```python
minimum_quality = 0.36
minimum_content_chars = 80
minimum_topic_purity = 0.85
generic_selector_candidate_chars = 120
detail_selector_candidate_chars = 60
```

`detail_selector_candidate_chars=60` 只表示详情字段可以形成候选；当前质量函数在少于
80 个有效字符时返回 0，因此最终接受门禁仍以 80 个有效字符为下限。

### 7.2 两级切块

```python
parent_chunk_size = 1600
child_chunk_size = 320
child_overlap = 48
```

这些值是 文档流水线的稳定工程基线。只有在离线召回评测提供新证据时才调整，
后续检索调优不改变文档流水线的职责边界。

后续混合检索评测提供了新证据并完成参数调整：运行默认值改为
`heading-parent-child-v2 / child=640 / overlap=128 / parent=960`。本节原值保留用于
追溯当时决策，不再代表当前默认值。

### 7.3 页面类型策略

| 页面类型 | 最终策略 |
| --- | --- |
| `exhibition` 详情 | `.carteDetail` 下的 title、bannerDesc、note 三字段组合 |
| `exhibition` 列表 | `.exhibition-wrap` |
| `perform` | `trafilatura` 优先，空正文时 Playwright；不强制 `.perform-wrap` |
| `food` | `trafilatura` 优先，`.foodShopping-wrap` 回退 |
| `privilege` | `trafilatura` 优先，`.privilege-wrap`、`.c--privileges-list` 回退 |
| `traffic` 详情 | `.s-guide-bus-detail` |
| `traffic` 目录 | `.s-guideBus-items a.guide-buse-items-box` |
| `hotel_directory` | `.s-preparedHotel-container a.items` |
| `news` | `.news-wrap` |
| `faq` | `.question-wrap` |
| 未知页面 | `main`、`.content`、`.container-box` 通用回退 |

selector 的采用原则是：同页面类型有足够成功样本，并且 selector 能提高主题纯度或
满足结构契约。单个高质量样本不会压过覆盖更多页面的稳定策略。

## 8. 稳定 ID 与内容版本

两级块采用以下标识规则：

```text
parent ID = source_id + heading anchor + parent ordinal
child ID = parent ID + child ordinal
content_version = 当前块内容哈希
document_version = 当前规范化正文哈希
```

标题锚点和块序号不变时，正文更新只改变版本，不改变 ID。标题重命名、章节顺序变化
或内容跨越分块边界时，相关 ID 可以变化，避免为了表面稳定而错误匹配旧内容。

## 9. 动态事实与原始证据

规则抽取器当前提取：

- `ticket_price`
- `opening_hours`
- `performance_time`
- `transit_schedule`

每条事实包含来源、证据原文、观察时间、刷新周期和文档版本，并设置
`requires_live_check=true`。这表示事实是某次抓取观察值，而不是永久常量。

live 模式保存以下原始证据：

```text
data/raw/{scenic_id}/{source_id}/{raw_sha256}.html.gz
data/raw/{scenic_id}/{source_id}/{raw_sha256}.json
data/raw/zhuhai-chimelong/extraction_attempts.jsonl
data/raw/zhuhai-chimelong/extraction_attempt_summary.json
```

`data/raw/` 已加入 `.gitignore`。归档可以本地回放和追责，但不会把大量网页快照提交
到代码仓库。

## 10. 最终验收

2026-09-11 全量 live 构建结果：

| 指标 | 结果 |
| --- | ---: |
| 种子来源 | 50 |
| 派生交通文档 | 6 |
| 最终文档 | 56 |
| 抓取尝试 | 57 |
| 成功尝试 | 56 |
| 失败后恢复 | 1 |
| parent chunk | 374 |
| child chunk | 435 |
| chunk 总数 | 809 |
| 结构化事实 | 54 |
| 图片记录 | 2923 |
| 低质量文档 | 0 |
| 低主题纯度文档 | 0 |
| 关联卡片污染 | 0 |
| 导航残留 | 0 |
| 结构不完整目录 | 0 |

语料 fingerprint：

```text
774b09ff22a8c3f7562b1b44f9d3db5c61b75742cad2ecc8d8eabef21b94bb57
```

测试结果为 `48 passed`，`pip check` 未发现依赖冲突。

## 11. 何时重新评估

以下情况出现时才重新评估 采集与切块阶段 参数：

- 长隆页面 DOM 改版导致 selector 成功率下降。
- `--all-runs` 显示某页面类型的失败率持续上升。
- 出现低于当前门禁但人工确认完整的新页面类型。
- 混合检索阶段 离线召回评测证明 parent/child 大小影响关键问题召回。
- 图片、营业时间或演出信息迁移到新的动态接口。

重新评估时应保留旧 run，比较页面类型失败分布和 selector 表现，不直接覆盖历史
审计证据。
