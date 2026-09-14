# 架构说明

ScenicPilot 采用“领域核心与外部能力分离”的结构。知识层负责资料标准化、切块、索引与检索；工具层只定义天气、距离、预算、营业时间等协议；Agent 层通过依赖注入组合这些能力；Web、API 与 CLI 只负责交付和输入输出适配。

```text
Browser UI / CLI / HTTP client
            |
       FastAPI delivery
      /       |        \
 static   JSON API    SSE events
            |             |
            +------ ScenicPilotRuntime
                        |------ SQLite checkpoint + execution ledger
                        |
                 LangGraph workflow -------- RuntimeDependencies
                   |        |                  |      |       |
                 retrieve  answer           index  tools  candidate/model/validator
                   |
                 HybridRetriever (BM25 + BGE/Hashing + RRF + metadata policy)

ToolSuite -> ResilientToolRunner -> provider -> Open-Meteo / AMap
                     |                |
                     |                +-> timeout / retry / circuit breaker
                     +-> fresh cache / stale cache / offline fallback / audit
```

## 交付层

- `/` 与 `/assets/*` 提供无构建依赖的静态页面，浏览器不持有任何服务端 Key；
- `/v1/chat` 与 `/v1/chat/stream` 提供无人工中断的兼容接口；
- `/v1/sessions/stream` 启动可中断会话，`/v1/sessions/resume` 接收批准或拒绝；
- SSE 的 `started / approval_required / completed / failed` 是稳定的页面事件边界，
  `data` 始终是 JSON；
- `/health/live` 区分进程存活，`/health/ready` 检查知识文件、页面资源和可选索引；
- FastAPI 把同步的 LangGraph 执行转移到工作线程，防止阻塞异步事件循环；runtime
  对共享 SQLite checkpointer 的图读写加进程内锁，避免同一连接被并发事务交叉使用。

## 状态流转

1. `classify` 区分 FAQ、纯天气和行程规划；同时包含规划词与天气词时优先保留行程意图。
2. 纯天气分支直接进入 `weather_answer`，跳过知识检索、候选生成和 HITL，只执行一次
   weather 调用；结果仍受调用预算、超时、重试、缓存、熔断和降级策略约束。
3. FAQ 与行程分支进入 `retrieve`，在 `scenic_id=zhuhai-chimelong` 范围内召回证据，并把分数和来源写入 state。
4. FAQ 分支进入 `grounded_answer`：证据不足时拒答，否则生成带 `[n]` 标号的摘要与来源列表。
5. 行程分支进入 `generate_candidates`：只从检索命中的 attraction/dining 来源生成候选；可选模型只负责 source ID 排序，输出必须再次经过白名单过滤。
6. `approval_gate` 按配置在首个行程工具调用前使用 LangGraph `interrupt()` 暂停；批准后继续，拒绝则终止且工具调用数为 0。
7. `enrich_tools` 根据日期、出发地、停靠点和人数调用天气、距离、预算工具，并把每次结果、降级原因和调用额度写回 state。
8. `validate` 计算游玩与路线总时长、时间轴、预算、营业状态、重复站点和强制偏好覆盖。
9. 存在 error 且仍有修订预算时进入 `revise_plan`，执行证据范围内的确定性修改，然后回到 `enrich_tools` 重新计算路线和预算；warning 不触发循环。
10. `finalize` 输出计划、引用、约束结果以及候选、工具、验证、修订、输出和治理轨迹；不可修复计划返回 `refused`。

## 可替换边界

- `EmbeddingProvider`：正式索引使用本地 BGE，离线测试使用确定性 Hashing/Fake。
- `HybridRetriever`：child 参与 BM25 与向量召回，经 RRF、category 软加权、parent
  去重和来源限流后回填 parent 上下文；完整检索轨迹写入 Agent state。
- `WeatherTool` / `DistanceTool`：通过韧性执行器接入 Open-Meteo 与高德，默认离线，
  支持超时、重试、缓存、熔断、降级和逐次审计；`BudgetTool` 使用确定性本地规则。
- `weather_answer`：通过注入的 `ToolSuite` 只发出一个 weather 请求；实时结果生成易变
  claim 与 API 来源，离线或故障结果不补造天气字段。
- `OpeningHoursTool`：默认不伪造易变营业时间；验证器支持注入实时或结构化事实适配器。
- `GroundedAnswerer`：执行引用绑定、证据门槛和冲突裁决。
- `ItineraryModelClient`：可选 OpenAI/DeepSeek Responses API 适配器，只对检索候选排序；无配置、超时、HTTP 错误或非法输出都回退到确定性规则。
- `ItineraryCandidateGenerator`：白名单过滤模型输出，并负责半日/一日、偏好、老人节奏与园区内用餐的确定性组合。
- `ItineraryToolEnricher`：把天气、路线和餐饮预算结果接入计划，同时保留单工具故障的降级状态。
- `ItineraryVerifier`：统一计算游玩与路线总时长、计划时间轴、预算、重复站点、营业窗口和强制偏好覆盖。
- `ItineraryPlanReviser`：只执行删除重复/关闭站点、补入已检索偏好候选、删除可选餐饮和超时裁剪；最多两轮，不能编造新地点。
- `CompiledStateGraph`：使用 SQLite checkpointer 保存 super-step，以 `thread_id` 恢复；`interrupt` 与 `Command(resume=...)` 实现 HITL。
- `ExecutionStore`：按 `thread_id + idempotency_key + input_hash` 去重 start/resume；默认 SQLite，本地测试可注入内存实现。
- `governance_trace`：记录审批、工具批次、跳过原因和累计调用量；最大两轮修订和 LangGraph recursion limit 形成终止边界。

这种边界让离线测试能够覆盖检索、引用、拒答、规划和异常路径，同时保持生产适配器独立演进。

## 部署拓扑与状态边界

默认部署是单 FastAPI worker 加一个 SQLite 文件。checkpoint 和执行账本共享该文件，
因此单实例重启后可以恢复暂停会话。Docker 镜像把 `/app/data/runtime` 作为持久化边界，
并以 `/health/ready` 作为容器健康检查。

SQLite 不作为多实例一致性方案。扩展到多 worker 或多副本前，需要同时替换 checkpointer
和 execution store，不能只迁移其中一个，否则图状态与幂等记录会分裂。SSE 经反向代理时
还需要关闭响应缓冲。具体命令见 [deployment.md](deployment.md)。
