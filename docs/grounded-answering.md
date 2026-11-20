# 可信回答、引用、拒答与冲突处理

## 1. 最终结论

实现将“检索命中”与“允许回答”拆成两个独立判断。检索器负责尽量找回候选，回答器
只接受能通过证据门禁的内容，并输出 JSON 可序列化的 claim、citation、conflict 和
answer trace。

当前规则基线不需要模型 API，最终配置为：

```python
min_retrieval_score = 0.12
min_keyword_score = 0.08
min_specific_term_matches = 1
min_specific_term_coverage = 0.50
minimum_authority_level = 1
max_claim_chars = 360
answer_context_limit = 3
answer_context_token_budget = 2200
```

真实本地 BGE 索引的离线结果：

| 指标 | 结果 |
| --- | ---: |
| 可回答问题 | 30/30 |
| 无证据与实时能力问题正确拒答 | 5/5 |
| 预期来源引用召回率 | 1.000000 |
| 引用标号有效率 | 1.000000 |
| 无引用 claim | 0 |
| 错误拒答 / 错误回答 | 0 / 0 |

## 2. 回答链路

```text
retrieval hits
  -> score / lexical / entity / authority / status gates
  -> distinct parent + source + token budget
  -> high-precision fact conflict detection
  -> source-level citation id allocation
  -> evidence snippet claims
  -> answer status + answer trace
```

输出状态分为：

- `answered`：静态证据充分，可以基于当前资料回答；
- `requires_live_check`：证据充分但包含易变信息，只作为官方核验入口；
- `refused`：没有足够证据，或问题需要尚未接入的实时排队、库存能力；
- `conflict`：同优先级来源给出不同结论，无法可靠裁决。

## 3. 引用编号为什么按来源分配

旧实现按上下文块生成正文 `[1]`、`[2]`，却按 `source_id` 去重引用列表。同一来源命中
两个 parent 时，正文可能出现 `[2]`，引用列表却只有一个条目。

当前实现先为选中的唯一来源建立稳定编号，再让同一来源的所有 claim 复用编号。例如
同一官网页面提供两个不同 parent，两个 claim 都使用 `[1]`。`answer_trace` 保存
`citation_map` 和 `citation_marker_coverage`，因此可以离线检查：

- 正文标号是否都存在于引用列表；
- 每个 claim 是否至少绑定一个 citation；
- citation 是否能回到 `source_id`、URL、核验日期和时效策略。

Citation 还携带 `authority_level`、`status`、`retrieval_policy`、`source_type`、
`refresh_days` 和 `freshness`，前端无需重新查询内部 chunk 即可解释来源。

## 4. “召回了”为什么仍可能拒答

混合检索的目标是 Recall，向量通道会主动找相近内容；这不代表候选能够证明用户问题。
可信回答阶段 对每个候选继续检查：

1. 融合分数不低于 `0.12`；
2. BM25 分数不低于 `0.08`，或查询扩展后存在真实关键词命中；
3. 去掉“什么、怎么、有哪些”等问句套话后，至少命中一个主题词，且主题词覆盖率
   不低于 `0.50`；
4. 来源权威等级至少为 1；
5. 来源状态不属于 removed、invalid、blocked、failed 或 unavailable；
6. 回填后的上下文必须有实质内容。

第一版只使用分数与宽泛关键词时，35 条评测中出现 3 条假回答，例如“量子纠缠课程的
教室编号”被景区通用词带入。直接要求 50% 字面覆盖又误伤餐饮同义表达和酒店汇总问题。
最终规则允许一个经过检索查询扩展得到的领域同义词补位，并排除“一共、哪几家、选择”
等问题结构词，实现 35/35。

实时排队精确值和现场库存属于当前能力边界。即使检索到了景点或票务页面，也直接以
`live_data_required` 拒答；工具层已提供实时调用边界，状态图再将把工具结果作为新证据接入回答或规划。

## 5. 冲突检测与裁决

规则检测器只处理可稳定比较的事实类型：

- 票价；
- 营业时间；
- 演出场次；
- 交通班次与服务时间；
- 开放、暂停、关闭状态。

自由描述、推荐偏好和无法确定主语的数字不强行判冲突。通用主语如“备注”“运营时间”
会与页面标题组合后再分组，避免把不同路线或不同设施的时间误判为冲突。

同一事实出现不同值时依次比较：

1. `authority_level`；
2. `verified_at`；
3. 与用户问题的适用范围匹配度。

存在唯一高优先级来源时，回答同时披露优先版本和另一版本，并记录
`chosen_source_id`、`resolution_reason`。最高优先级仍并列且值不同，则状态设为
`conflict`，展示双方证据但不输出确定结论。

## 6. 时效策略

所有 `volatile=true` 的证据都会把回答状态设为 `requires_live_check`，回答末尾明确说明
需要按实际到访日期核验。`verified_at + refresh_days` 用来生成 `fresh`、`stale` 或
`unknown`，过期来源写入 `answer_trace.stale_source_ids`。

这意味着静态语料可以回答“去哪里核验、页面记录了什么”，但不会把票价、营业时间、
场次、班次、权益、库存等包装成实时承诺。

## 7. API 与 StateGraph 契约

FAQ 分支现在返回：

```json
{
  "answer": "根据当前可核验资料：... [1]",
  "answer_status": "requires_live_check",
  "citations": [],
  "claims": [],
  "conflicts": [],
  "answer_trace": {}
}
```

其中 state 只包含字符串、数字、布尔、列表和字典，保持 JSON 可序列化。旧的
`GroundedAnswerer.answer() -> (text, citations)` 入口继续保留，图节点使用新的
`answer_with_trace()`。

## 8. 可复现入口

```powershell
# 可信回答阶段 专项规则测试
python -m pytest tests/test_answering.py

# 使用本地 BGE 正式索引跑 50 条可回答 + 21 条拒答评测
python scripts/evaluate_answering.py

# 完整回归
python -m pytest
python -m pip check
```

生成报告位于 `data/index/zhuhai-chimelong/answer_eval.json`，属于本地生成物，不提交
Git。离线评测阶段 再扩展为 115 条端到端样例，并增加模型回答忠实度、原子事实覆盖率和故障注入。
