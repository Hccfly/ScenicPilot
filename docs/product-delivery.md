# 产品交付与演示

交付层先在现有 `ScenicPilotRuntime` 之上增加可演示、可探活、可部署的交付层；页面验收
随后暴露了纯天气问题误入 FAQ 的路由缺口，因此补充独立 `weather_answer` 分支，但不改变
原有 FAQ 与行程规则。默认交付配置固定为离线工具和离线模型，无需任何私有 Key。

## 交付内容

- `web/index.html`、`web/app.css`、`web/app.js`：无前端构建依赖的响应式页面；
- `POST /v1/sessions/stream`：会话级 SSE，支持 `started`、`approval_required`、
  `completed`、`failed` 四类事件；
- `POST /v1/sessions/resume`：人工批准或拒绝暂停中的行程；
- `GET /health/live`：仅证明进程可响应；
- `GET /health/ready`：检查知识文件、页面资源和可选持久化检索索引；
- `data/demo/product_scenarios.json`：FAQ、HITL 行程、独立天气降级、知识边界拒答四条产品场景；
- `scripts/demo_product.py`：离线走通引用回答、进程重启恢复、人工批准和幂等重放；
- `Dockerfile`：以非 root 用户运行，包含 readiness healthcheck 和 SQLite 持久化目录。

## 页面交互链路

```text
Browser
  | POST /v1/sessions/stream (unique thread_id + idempotency_key)
  v
started -> LangGraph
              |
              +-- weather -> weather_answer ----------------> completed
              |
              +-- FAQ ------------------------------> completed
              |
              +-- itinerary -> approval_gate -> approval_required
                                             |
Browser -- POST /v1/sessions/resume ----------+
                  | approve / reject
                  v
              completed
```

浏览器采用 `fetch` 读取 POST 响应流，不使用仅支持 GET 的 `EventSource`。服务端把同步图执行
放入工作线程，避免阻塞 FastAPI 事件循环。页面只通过 DOM `textContent` 渲染 Agent 输出，
不把模型文本作为 HTML 注入。

## SSE 契约

每个事件都是标准 SSE 块，`data` 为 JSON：

```text
event: approval_required
data: {"status":"awaiting_approval","thread_id":"...","interrupts":[...]}
```

| event | 含义 | 页面动作 |
| --- | --- | --- |
| `started` | 请求已接受 | 显示检索与规划进度 |
| `approval_required` | 图在首个工具节点前暂停 | 显示批准/拒绝面板 |
| `completed` | FAQ、天气或行程执行完成 | 分区渲染答案、计划、约束、引用、轨迹 |
| `failed` | 执行或幂等校验失败 | 显示结构化错误，不伪装成正常答案 |

响应设置 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`，反向代理仍需显式关闭
SSE 路径的响应缓冲。

## 离线演示

```powershell
python scripts/demo_product.py
python scripts/demo_product.py --case-id demo-itinerary-hitl
```

脚本直接构造显式 `Settings(tool_mode="offline", llm_provider="offline")`，不会读取 `.env`，
并为每条场景创建独立临时 SQLite。HITL 场景会关闭第一个 runtime，再从同一 checkpoint
恢复、批准，并使用相同幂等键重放一次。报告中的 `uses_private_keys=false`、逐项 `checks`
和进程退出码共同形成验收结果。

## 验收结果

- 四条产品场景全部通过；
- 纯天气问题跳过检索、规划和 HITL，只产生一条 weather 工具记录；离线时不编造天气字段；
- HITL 在工具调用前暂停，暂停时 `tool_call_count=0`；
- 关闭并重建 runtime 后仍恢复为 `awaiting_approval`；
- 批准后得到结构化行程，相同 resume 幂等键返回同一结果且标记重放；
- 超出知识范围的实时客流问题返回 `answer_status=refused` 且无引用；
- 页面、静态资源、readiness 和 SSE 事件均有 API 合同测试；
- 完整离线测试和 115-case 离线门禁保持通过。

部署方式、SQLite 边界和反向代理要求见 [deployment.md](deployment.md)。
