# 工程演进记录

ScenicPilot 不是按框架示例堆叠功能，而是从珠海长隆这一真实信息场景中逐步暴露问题、建立约束，再用离线证据收敛设计。本文记录能力为什么出现，以及关键参数依据什么确定。

## 从来源清单到可追溯语料

起点是 50 条经过页面级核验的官网来源，覆盖园区、酒店、餐饮、交通、表演和服务设施。资料目录记录 `source_id`、URL、权威等级、核验时间和时效属性，演示资料与正式来源严格隔离。

真实页面审计暴露出两个问题：通用正文提取会混入“其他展区/其他设施”推荐卡片，部分页面还残留“返回/顶部”等导航文本。因此采集链路演进为 `trafilatura → 长隆专用 selector → Playwright`，并按页面类型记录每次尝试的正文长度、主题纯度、导航残留和失败原因。详情见 [来源清单](source-inventory.md)、[采集流水线](ingestion-pipeline.md)和[采集决策记录](ingestion-decisions.md)。

## 从固定切块到证据驱动的上下文

文档采用标题感知的 parent-child 两级切块：child 参与召回，parent 负责补齐回答证据。标题锚点未变化时尽量保持 chunk ID，内容变化则通过版本字段追踪。

参数没有凭经验固定。项目用真实问题标注必要证据片段，对 child 大小、overlap、parent 大小和上下文预算做两阶段消融，同时观察 Recall@K、MRR、重复率、噪声、证据完整性和 token 成本。当前基线为 `child=640 / overlap=128 / parent=960`，最多回填 3 个 parent，估算上下文预算 2200 token。详情见 [chunk 消融](chunk-ablation.md)。

## 从单路召回到混合检索

中文景区问法同时包含专名、口语别称和类别约束，单一检索通道很难兼顾。因此检索层组合 BM25、本地 BGE 与 RRF，并加入 Jieba 领域词典、确定性查询扩展和 metadata 过滤。

结果集采用“同 parent 只保留一个 child、同 source 最多两个 parent、category 默认软加权”的策略，既保留跨来源证据，又控制同页重复和主题噪声。45 条 retrieval-only case 用于持续验证召回质量。详情见 [混合检索设计](hybrid-retrieval.md)。

## 从检索命中到可信回答

检索器的目标是尽量找回候选，不代表候选足以证明答案。回答层因此独立执行证据门禁、claim-citation 绑定、来源冲突裁决和易变事实检查。证据不足、只命中相关主题但未覆盖问题，或问题要求缺失的实时数据时，系统返回结构化拒答，而不是补造内容。详情见 [可信回答设计](grounded-answering.md)。

## 从工具调用到可降级执行

天气、距离、预算和营业时间被抽象为可注入协议，外层统一处理超时、重试、缓存、熔断、离线降级和审计。单个工具失败不会取消同批其他调用；成功但字段为空、类型错误或数值越界也会被识别为无效结果。详情见 [外部工具边界](external-tools.md)。

## 从一次生成到有界规划闭环

LangGraph 将 FAQ、纯天气和行程规划拆成三条明确路径。行程分支依次完成检索、候选生成、人工审批、工具补全、约束验证和结果输出；模型只能在检索命中的 `source_id` 白名单中重排候选。

验证器统一检查时间轴、路线、预算、营业状态、重复站点和强制偏好。明确错误最多触发两轮确定性修订，实时信息未确认只产生 warning，避免无意义循环。详情见 [行程状态图](itinerary-stategraph.md)和[约束修订](constraint-revision.md)。

## 从可运行到可恢复、可评测

SQLite checkpoint 保存 LangGraph super-step，`interrupt()` 与 `Command(resume=...)` 实现人工审批后的跨进程恢复；执行账本通过 `thread_id + idempotency_key + input_hash` 区分重放与冲突。工具调用预算、修订次数和 recursion limit 构成三层终止边界。

离线门禁包含 60 个可定位 case。每个 Agent case 隔离 SQLite、会话 ID、幂等账本和工具状态；除了自然语言答案，还断言引用、claims、工具状态、约束结果、轨迹、调用次数与终止原因。故障注入覆盖异常、超时、空值、脏类型、非法数值和未知来源，并重点验证恢复和幂等。详情见 [会话治理](session-governance.md)、[离线评测](offline-evaluation.md)和[产品交付](product-delivery.md)。

## 当前边界

当前实现聚焦单景区知识域、单 FastAPI worker 和 SQLite 持久化，适合本地运行、工程验证与单实例部署。扩展到多景区或多实例前，需要继续解决知识命名空间隔离、增量索引质量门禁，以及共享 checkpoint 与执行账本的一致性问题。
