# 文档采集、两级切块与索引流水线

## 数据流

```text
source manifest
  ├─ manifest mode：使用已核验摘要，完全离线
  └─ live mode
       HTTPX → trafilatura → 长隆 selector → Playwright（低质量时）
                    ├─ 图片清单与缺失检测
                    └─ data/raw/*.html.gz + 抓取元数据（Git ignored）
                              ↓
normalize + validate + document_version
                              ↓
标题感知 parent chunk → child chunk
                              ├─ facts.jsonl（动态事实）
                              ├─ chunks.jsonl（父子全集）
                              └─ hybrid_index.json（仅 child）
```

## 分层正文提取

`LayeredPageExtractor` 先使用 `trafilatura` 提取 Markdown 正文；质量不足时使用
`ChimelongSelectorExtractor`。后者按长隆页面路径适配项目详情、演出、餐饮购物、
权益、新闻、问答和交通容器。两者仍不足时，`LivePageDocumentProvider` 用
Playwright 渲染页面后重走提取流程。浏览器启动依次尝试 Playwright 自带 Chromium、
系统 Chrome 和系统 Edge，也可在代码中显式指定 channel。

每个 `<img>` 记录绝对 URL、`alt`、`title`、是否懒加载，以及 `src`/`alt` 缺失状态，
最终写入 `images.jsonl`。selector 和质量阈值属于站点适配配置，页面改版后可独立更新。

50 条 live 页面审计后，默认质量阈值由 `0.42` 调整为 `0.36`，并增加 80 个有效
字符的硬门槛。样本中无效正文最高分为 `0.302`，有效正文最低分为 `0.421199`，
阈值取二者中间区域。低于 80 字的页面不依赖分数，直接进入下一层。导航残留和
“其他设施”关联卡片污染单独标记，因为单一长度分数无法识别这些问题。

详情页额外执行主题纯度检测：从 `.c-paradise-other` 读取推荐卡片标题，检查
trafilatura 正文是否包含关联区标题或推荐项目。纯度低于 `0.85` 时，改用专用详情
selector，只组合 `.carteDetail > .title`、`.bannerDesc` 和 `.c-paradise-note`。
50 页归档回放中，21 个受污染详情页全部被替换，关联区分界词残留从 21 页降为 0。
短而纯净的详情正文只要超过 80 个有效字符即可接受，不要求长度质量分达到 `0.36`。

`/zh/hotel/` 和 `/zh/traffic/` 属于目录聚合页，不套用详情页长度门槛。酒店目录按
`.s-preparedHotel-container a.items` 提取名称、描述和官方入口；当前总览 DOM 展示
4 家酒店，项目已有的长隆飞船酒店独立官方入口作为带 `supplemented_entry` 标记的
第 5 项补齐。交通目录按 `.s-guideBus-items a.guide-buse-items-box` 提取 6 类入口，
live 构建会继续物化对应的 `type_1/2/4/5/6/7.html` 子页面。交通详情统一从
`.s-guide-bus-detail` 提取，避免页头和页脚污染。

目录页使用独立结构契约：`schema_complete` 判断预期条目及 URL 是否齐全，
`structure_flags` 记录缺失、非法 URL 和补充项，`discovered_links` 保留发现结果。
结构完整且主题纯净时，即使正文较短也不会误触发 Playwright；结构不完整时才进入
浏览器回退。基础人工清单仍为 50 条，live 模式可在此基础上增加 6 条交通派生文档。

页面控件残留在质量评分前清理。独立行 `顶部`/`返回` 位于正文开头时只删除该行；
位于有效正文之后且紧邻站点面包屑时，删除控件及连续的面包屑边界，但保留其后的
公告标题、设施标签等业务内容。清理使用整行匹配，因此“在滑道顶部一划而下”不会
被误删。归档元数据通过 `cleanup_flags` 区分 `navigation_removed` 与
`navigation_boundary_cleaned`，保证清理可审计且重复执行保持幂等。

## 标题感知 parent-child

- Markdown 标题生成层级路径与稳定 anchor；无标题文档使用文档标题。
- parent 默认最大 1600 字符，保存完整局部上下文。
- child 默认 320 字符、重叠 48 字符，作为实际召回单元。
- `chunks.jsonl` 保存父子块；`hybrid_index.json` 只索引 child，命中后可按
  `parent_chunk_id` 扩展上下文。

这些大小是可运行基线，不是检索效果结论；混合检索阶段 用离线召回集调参。

> 后续的证据片段消融已将运行默认值更新为 V2、child 640、overlap 128、
> parent 960。这里的 320/48/1600 仅作为文档流水线的历史基线保留。

## 动态事实表

`RuleBasedFactExtractor` 先提供高精度规则基线，从正文抽取：

- `ticket_price`
- `opening_hours`
- `performance_time`
- `transit_schedule`

每条事实都包含来源、证据原文、观察时间、刷新周期和文档版本，并强制
`requires_live_check=true`。它表示“某次抓取观察到的事实”，不能当作永久常量。
规则抽取器通过 `FactExtractor` 注入，后续可替换为模型抽取或规则与模型组合。

## 原始页面归档

live 模式保存：

```text
data/raw/{scenic_id}/{source_id}/{raw_sha256}.html.gz
data/raw/{scenic_id}/{source_id}/{raw_sha256}.json
```

JSON 包含抓取时间、HTTP 状态、最终 URL、响应头白名单、原始内容哈希、抓取方式、
提取方式、质量分和图片缺失统计。`data/raw/` 已加入 `.gitignore`。

每次 HTTP 或 Playwright 抓取尝试还会追加一条不可覆盖的审计事件到
`data/raw/zhuhai-chimelong/extraction_attempts.jsonl`。事件记录 `run_id`、页面类型、
抓取方式、HTTP 状态、抽取方式、实际 selector、质量/纯度/结构分、是否通过门禁、
失败原因和清理动作；抓取异常也会形成事件。当前 run 的聚合结果自动写入
`extraction_attempt_summary.json`，按 `exhibition`、`perform`、`food`、`privilege`、
`traffic` 等类型给出失败率、失败原因分布和 selector 表现。

### 当前 selector 决策（2026-09-11 全量 live run）

本轮 50 条种子来源展开为 56 个最终文档，共记录 57 次抓取尝试；56 次通过，
1 次失败后由 Playwright 恢复。失败率按“尝试”统计，不等同于最终文档失败率。

| 页面类型 | 尝试/失败 | 当前抽取策略 | 决策依据 |
| --- | ---: | --- | --- |
| `exhibition` | 24 / 0 | 详情字段组合：`.carteDetail > .title`、`.carteDetail > .bannerDesc`、`.carteDetail > .c-paradise-note`；列表页回退 `.exhibition-wrap` | 21 个详情页均通过，主题纯度均为 1.0，可排除“其他展区/其他设施”卡片 |
| `perform` | 3 / 1 | 默认 `trafilatura`，空正文时 Playwright；暂不强制 `.perform-wrap` | 唯一失败来自静态响应正文为空，浏览器渲染后恢复，当前没有 selector 优于渲染回退的证据 |
| `food` | 6 / 0 | 默认 `trafilatura`，站点回退 `.foodShopping-wrap` | 6 次均通过；其中 2 次由该 selector 稳定抽取 |
| `privilege` | 5 / 0 | 默认 `trafilatura`，保留 `.privilege-wrap`、`.c--privileges-list` 作为回退 | 当前正文抽取已全部通过，不应为了“统一”而强制 selector |
| `traffic` | 7 / 0 | 详情页 `.s-guide-bus-detail`；目录页 `.s-guideBus-items a.guide-buse-items-box` | 6 个详情页和 1 个目录页均通过，目录结构完整 |
| `hotel_directory` | 1 / 0 | `.s-preparedHotel-container a.items` | 能形成五家酒店的结构化目录（含飞船酒店补充入口） |
| `news` / `faq` | 3 / 0 | `.news-wrap` / `.question-wrap` | 当前样本均通过，作为对应页面类型的首选 selector |

selector 的采用原则是“同页面类型有足够通过样本且能提升主题纯度”；单个高质量样本
不会压过覆盖更多页面的稳定策略。后续可用 `--all-runs` 聚合历史事件观察站点改版漂移，
但正式决策仍以最近一次完整 run 为主，避免旧页面结构掩盖当前故障。

## 稳定 ID 与内容版本

- parent ID = `source_id + heading anchor + parent ordinal`
- child ID = `parent ID + child ordinal`
- `content_version` = 当前块内容哈希
- `document_version` = 当前规范化正文哈希

因此标题锚点和块序号不变时，文字更新不会改变 ID，但版本会变化。如果标题重命名、
章节顺序改变或内容跨越分块边界，相关 ID 可能变化，这是“尽量稳定”而非错误匹配。

## 运行

```powershell
# 离线、可重复构建 50 条来源
python scripts/build_corpus.py

# 只抓一个真实页面，用于冒烟验证
python scripts/build_corpus.py --mode live --source-id ocean-overview

# 不启用浏览器回退
python scripts/build_corpus.py --mode live --source-id ocean-overview --no-browser-fallback

# 对最近一次全量 live 产物生成逐页标注和阈值报告
python scripts/audit_extractions.py

# 重新聚合最近一次 run；加 --all-runs 可观察历史漂移
python scripts/summarize_extraction_attempts.py
```

安装采集依赖；CI 或无 Chrome/Edge 的环境再安装 Chromium：

```powershell
pip install -e ".[dev,ingest]"
python -m playwright install chromium
```

## 验收口径

- 相同离线输入和配置得到相同 fingerprint 与字节级一致产物。
- parent-child 关系、标题路径、字符偏移和 metadata 可追踪。
- 标题锚点不变且未跨越分块边界时，内容更新仅改变版本。
- 四类动态事实携带证据与实时核验标记。
- live 抓取可归档原始响应，并统计图片字段缺失。
