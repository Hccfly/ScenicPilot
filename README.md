# ScenicPilot

一个面向真实景区信息场景的可信旅游 Agent 实验场。

ScenicPilot 源于我对一个具体问题的持续探索：当景区资料分散、事实频繁变化、行程还要满足时间与预算约束时，Agent 怎样做到既能回答，也能说明依据、识别未知并在失败后恢复？首个落地场景选择珠海长隆度假区，围绕资料治理、混合检索、可信回答、工具调用和状态图编排构建了完整闭环。

项目不是套壳聊天页面。核心链路可以在无模型 Key、无外部网络的环境中运行和评测；接入真实服务后，外部工具和模型仍被限制在可审计、可降级的边界内。

> `data/demo` 是用于开发与回归测试的合成资料，不代表珠海长隆当前票价、开放时间或服务承诺。易变事实需要保留来源和核验时间，并在使用时重新校验。本项目与长隆集团无隶属关系。

## 我在探索什么

- **检索命中不等于可以回答**：BM25 与向量召回负责找候选，可信回答层继续验证证据覆盖、来源冲突和事实时效；证据不足时明确拒答。
- **模型不应绕过证据边界**：模型只允许在检索命中的 `source_id` 白名单中重排候选，非法输出或调用失败自动回退到确定性策略。
- **行程生成需要闭环验证**：计划经过天气、距离、预算和营业状态补全，再验证时间轴、预算、路线、重复站点与强制偏好；错误触发有界修订，warning 不制造循环。
- **Agent 的可靠性来自执行治理**：SQLite checkpoint、HITL、幂等账本、工具调用预算和循环上限共同保证会话可暂停、可恢复、可解释。
- **自然语言结果之外还要验证状态**：离线评测同时断言引用、claims、工具状态、约束结果、轨迹、调用次数和终止原因。

## 已实现能力

- 50 条经过页面级核验的珠海长隆官网来源清单；
- `trafilatura → 站点专用 selector → Playwright` 分层正文提取，以及压缩 HTML、抓取状态、内容哈希和审计事件；
- 标题感知的 parent-child 切块、稳定 chunk ID、结构化事实表和增量索引；
- BM25 + 本地 BGE + RRF 混合检索，支持 metadata 过滤、category 软加权、child 去重和 parent 回填；
- 来源级编号引用、低证据拒答、易变信息提示和冲突来源裁决；
- 基于 LangGraph 的 FAQ、独立天气、行程规划三分支状态图；
- 天气、距离、预算、营业时间工具，以及超时、重试、缓存、熔断、降级和逐次审计；
- 约束验证与最多两轮的确定性行程修订；
- SQLite 会话恢复、人工审批、请求幂等、工具预算和治理轨迹；
- CLI、FastAPI、JSON SSE、响应式页面、健康检查和 Docker 单实例部署；
- 完全离线的单元测试与 60-case 评测门禁。

## 核心架构

```text
Browser / CLI / HTTP
          |
       FastAPI ------------- SSE session events
          |
   ScenicPilotRuntime ------ SQLite checkpoint + idempotency ledger
          |
     LangGraph workflow ---- RuntimeDependencies
       |       |                    |      |       |
    retrieve  answer              index  tools  model/validator
       |
HybridRetriever (BM25 + BGE/Hashing + RRF + metadata policy)
```

```text
START -> classify --weather--> weather_answer -> END
             |
             +-> retrieve -> grounded_answer -> END
                           +-> generate_candidates
                               -> approval_gate --reject--> approval_rejected -> END
                                      |
                                   approve
                                      v
                                 enrich_tools
                               -> validate --------> finalize -> END
                                    |
                                    +-> revise_plan -> enrich_tools
```

`RuntimeDependencies` 统一注入索引、候选生成器、模型客户端、工具补全器、验证器和修订器。测试可以替换任意外部能力，不请求真实 API；state、trace 和 API 输出均保持 JSON 可序列化。

## 快速运行

要求 Python 3.11 或更高版本。以下流程默认使用离线模型和离线工具，不需要 API Key。

```powershell
cd ScenicPilot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

python scripts/demo_product.py
python -m scenic_pilot.cli "带孩子去珠海长隆玩一天，预算 1000 元，怎么安排？"
uvicorn scenic_pilot.api:app --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000`。也可以调用 JSON API：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat `
  -ContentType application/json `
  -Body '{"message":"珠海长隆有哪些主题内容？"}'
```

Docker 单实例运行：

```powershell
docker build -t scenicpilot:local .
docker run --rm -p 8000:8000 `
  -v scenicpilot-runtime:/app/data/runtime `
  scenicpilot:local
```

## 可复现评测

运行完整测试：

```powershell
python -m pytest
```

运行 60-case 离线门禁，或定位到分组和单个 case：

```powershell
python scripts/evaluate_offline.py
python scripts/evaluate_offline.py --group fault_injection
python scripts/evaluate_offline.py --case-id eval-recovery-004
```

评测固定使用本地索引、离线模型和离线工具。每个 Agent case 都使用独立 SQLite、`thread_id`、执行账本和工具状态；报告逐 case 保存结构化 `expected/actual`，失败时返回非零退出码。覆盖范围包括：

- 30 条真实问法的检索 Recall@K、MRR、重复率和噪声；
- 带引用回答、冲突处理、知识边界拒答；
- 半日/一日行程、预算不足和强制偏好；
- 超时、异常、空字段、脏类型、负金额、未知来源与空检索；
- 中断恢复、重复 start/resume、幂等冲突；
- 独立的工具调用预算和循环终止边界。

专项评测入口：

```powershell
python scripts/evaluate_retrieval.py
python scripts/evaluate_answering.py
python scripts/evaluate_planning.py --tool-mode offline
python scripts/evaluate_constraints.py
python scripts/evaluate_governance.py
```

## 数据与索引

离线构建使用已核验摘要；只有显式传入 `--mode live` 才会访问官网：

```powershell
python scripts/validate_sources.py
python scripts/build_corpus.py
python scripts/build_corpus.py --mode live --source-id ocean-overview
```

官网采集需要安装 `.[ingest]`，并执行 `python -m playwright install chromium`。原始响应保存在不会提交 Git 的 `data/raw/`。

构建本地 BGE 混合检索索引：

```powershell
python -m pip install -e ".[retrieval]"
python scripts/rechunk_corpus.py
python scripts/build_retrieval_index.py --local-files-only
python scripts/evaluate_retrieval.py
```

正式索引使用 `BAAI/bge-small-zh-v1.5`；Hashing embedding 只用于无需下载模型的离线测试。当前基于证据片段消融得到的默认参数为 `child=640 / overlap=128 / parent=960`，回答上下文最多 3 个 parent，估算 token 预算为 2200。

## 可选真实适配器

复制 `.env.example` 为 `.env`，按需配置：

- `SCENICPILOT_TOOL_MODE=auto` 或 `live`：启用 Open-Meteo 天气；
- `AMAP_API_KEY`：启用高德驾车距离；
- `SCENICPILOT_LLM_PROVIDER=deepseek` 与 `DEEPSEEK_API_KEY`，或 `openai` 与 `OPENAI_API_KEY`：启用候选重排；
- `SCENICPILOT_RETRIEVAL_INDEX_PATH`：加载预先构建的本地 BGE 索引。

系统环境变量优先于 `.env`。`.env` 已被 Git 忽略，真实 Key 不应写入示例配置、镜像或前端。

## 项目结构

```text
scenic_pilot/
  agent/          # LangGraph、依赖注入、行程验证与执行治理
  knowledge/      # 采集、切块、结构化事实、索引与可信回答
  tools/          # 天气、距离、预算、营业时间与韧性执行器
  evaluation/     # 隔离式离线评测器
web/              # 无构建依赖的演示页面
data/
  curated/        # 人工核验的来源目录
  demo/           # 明确标识的合成演示资料
  eval/           # 检索与端到端评测 case
scripts/          # 构建、评测、探针与演示入口
tests/            # 单元和边界测试
docs/             # 设计决策、评测方法与部署说明
```

## 设计记录

- [架构与依赖边界](docs/architecture.md)
- [工程演进记录](docs/engineering-evolution.md)
- [来源范围与数据原则](docs/source-inventory.md)
- [采集、正文提取与两级切块](docs/ingestion-pipeline.md)
- [采集参数的决策证据](docs/ingestion-decisions.md)
- [混合检索设计](docs/hybrid-retrieval.md)
- [chunk 与上下文预算消融](docs/chunk-ablation.md)
- [可信回答与冲突处理](docs/grounded-answering.md)
- [工具韧性边界](docs/external-tools.md)
- [行程 StateGraph](docs/itinerary-stategraph.md)
- [约束验证与有界修订](docs/constraint-revision.md)
- [会话恢复与执行治理](docs/session-governance.md)
- [离线评测与故障注入](docs/offline-evaluation.md)
- [产品交付与演示](docs/product-delivery.md)
- [Windows 与 Docker 部署](docs/deployment.md)

ScenicPilot 当前聚焦单景区、单实例部署和可复现的工程验证。下一步更值得探索的是跨景区知识隔离、增量索引质量门禁，以及共享数据库下的多实例 checkpoint 与幂等一致性。
