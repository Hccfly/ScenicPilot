# 行程规划 StateGraph

该状态图把检索、可选模型排序、天气/路线/预算工具与基础验证组成一条可运行的行程分支。
核心原则是：模型可以帮助排序，但不能成为景点、票价、时间或路线事实的来源；进入计划
的每个停靠点都必须能回指当前检索结果中的 `source_id`。

## 状态流

```text
START
  -> classify
  -> retrieve
       |-- faq ------> grounded_answer --------------------> END
       \-- itinerary -> generate_candidates
                       -> enrich_tools
                       -> validate
                       -> finalize ------------------------> END
```

行程分支的六个节点职责如下：

1. `classify`：识别 FAQ 或 itinerary，并抽取半日/一日、人数、预算、到访日期、出发地和游客偏好。
2. `retrieve`：在当前景区范围内执行 混合检索阶段 混合检索，把候选与完整检索轨迹写入 state。
3. `generate_candidates`：仅保留 attraction/dining；可选模型对候选 `source_id` 排序，随后执行来源白名单过滤和确定性组合。
4. `enrich_tools`：查询天气、餐饮预算以及“出发地→首站→后续园区”的驾车路线；每个调用独立失败、独立降级。
5. `validate`：执行 行程编排阶段 基础时长、预算、重复站点和开放状态检查。
6. `finalize`：生成结构化计划、自然语言摘要、引用、工具结果、违规项和规划轨迹。

## 候选生成边界

`ItineraryModelClient` 是依赖注入协议，默认实现为禁用模型的离线客户端。启用模型时，
发送给 provider 的只是 query、constraints 以及候选的 `source_id/title/category/score`，
不发送整页正文。输出使用严格 JSON Schema：

```json
{
  "selected_source_ids": ["demo-spaceship", "demo-ocean"],
  "rationale": "先满足室内科普偏好，再补充海洋主题"
}
```

模型返回的 ID 会与本次检索候选取交集；未知 ID 进入
`planning_trace.candidate_generation.rejected_model_source_ids`，不会进入计划。模型未配置、
超时、网络错误、HTTP 错误或结构非法时，系统记录 `degradation_reason` 并使用规则排序。
请求设置 `store=false` 和 300 个输出 token 上限，控制不必要的数据保留与成本。

确定性组合不是简单截取 top-k：

- 半日最多 2 个停靠点，一日最多 3 个；
- 优先选择满足 `indoor_science`、`marine` 等显式偏好的 attraction；
- 一日计划优先组成“首个园区 → 同园区用餐 → 第二园区”；
- 老人慢游把 attraction 时段从 120 分钟缩短为 90 分钟；
- overview、hotel、transport 等资料可以参与检索，但不会被误当作可游玩停靠点。

## 工具补全与可解释降级

`ItineraryToolEnricher` 使用工具层的 `ToolSuite`，根据结构化约束生成请求：

- weather：景区 + 到访日期；
- budget：人数 + 实际计划时长；
- distance：用户出发地到首站，以及跨园区停靠点之间的路线。

预算结果更新 `plan.estimated_cost`，天气与路线保留在 `tool_results`。任何 provider 故障
都不会终止图；输出改为 `requires_live_check`，并在回答、工具 trace 和
`planning_trace.tool_enrichment` 三处保留原因。离线模式同样执行工具节点，只返回显式
`provider_not_configured`，所以线上和测试走的是同一张图。

## 状态与依赖注入

Agent state 只保存 dict、list、str、number、bool 与 null，包括：

- `constraints`：意图抽取后的结构化约束；
- `hits` / `retrieval_trace`：检索证据与调试信息；
- `candidate_plan`：工具补全前的候选计划；
- `plan` / `tool_results` / `violations`：补全后的计划和验证结果；
- `planning_trace`：候选生成、工具补全、验证、输出四段审计信息；
- `trace`：实际经过的节点名。

`RuntimeDependencies` 注入 classifier、retriever、answerer、candidate generator、model client、
tool enricher 和 verifier。测试可替换为 Fake model/tool，不需要真实网络；JSON 可序列化
这套 JSON 可序列化状态已经在 会话治理阶段 接入 SQLite checkpoint、HITL 与执行治理。

## 模型配置

模型是可选增强，不影响无 Key 运行。DeepSeek 示例：

```dotenv
SCENICPILOT_LLM_PROVIDER=deepseek
SCENICPILOT_LLM_MODEL=deepseek-flash
SCENICPILOT_LLM_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=your-key
```

OpenAI 示例：

```dotenv
SCENICPILOT_LLM_PROVIDER=openai
SCENICPILOT_LLM_MODEL=your-responses-compatible-model
SCENICPILOT_LLM_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-key
```

真实 Key 只写入被 Git 忽略的 `.env`。本实现使用 Responses API 的结构化文本输出；
DeepSeek 的 Responses API 为兼容实现，因此 provider 差异被隔离在 adapter 内。候选排序属于
短结构化任务，DeepSeek 请求会显式设置 `reasoning.effort=none`，避免默认思考 token 挤占
`max_output_tokens` 后截断 JSON。

## 验收

运行：

```powershell
python scripts/evaluate_planning.py --tool-mode offline
python scripts/evaluate_planning.py --tool-mode live
```

离线验收覆盖亲子一日、雨天室内半日、海洋偏好半日、老人从珠海站出发一日共 4 类需求，
检查结构化计划、时长、来源闭包、偏好命中、工具补全、路线调用、六节点 trace 和四段
planning trace。当前 4/4 通过。

live 验收已验证 Open-Meteo 天气、高德首段驾车路线和预算返回真实结果；后续路线遇到
高德配额限制时按预期降级，完整图仍正常结束并标为 `requires_live_check`。

行程状态图不宣称初次生成的计划已经完全可执行；营业时间、路线总耗时、强制偏好和失败后的
有界自动修订见 [约束验证与有界修订](constraint-revision.md)。
