# 会话恢复、HITL 与执行治理

会话治理将已有的有界规划闭环升级为可中断、可恢复、可去重的执行会话。实现使用
LangGraph 原生 checkpointer 和 `interrupt()`，不在业务层模拟暂停；另外用执行账本处理
HTTP/CLI 重试的幂等语义。

## 1. 最终执行流

```text
START -> classify -> retrieve -> grounded_answer -> END
                              \-> generate_candidates
                                  -> approval_gate --reject--> approval_rejected -> END
                                         |
                                      approve
                                         v
                                    enrich_tools
                                         v
                                      validate --------> finalize -> END
                                         |
                                         \-> revise_plan -> enrich_tools
```

`approval_gate` 位于首个工具节点之前。需要审批时，它只构造可序列化的审批载荷并调用
`interrupt()`；此节点在恢复时会从头执行，因此中断前不能放日志外发、扣费或工具调用等
副作用。`Command(resume={"approved": ...})` 使用相同 `thread_id` 恢复。

## 2. SQLite 的两类状态

同一个 SQLite 文件承载两类职责，但使用不同的数据结构：

- LangGraph `SqliteSaver` 保存每个 super-step 的 checkpoint，用 `thread_id` 找回图状态；
- `scenicpilot_execution_requests` 表保存 `(thread_id, idempotency_key)` 执行账本，记录请求
  哈希、操作类型、状态与响应。

checkpoint 解决“执行到哪里”，幂等账本解决“这次 start/resume 是否已经做过”。二者不能
互相替代。默认文件是 `data/runtime/scenicpilot.sqlite3`，已被 Git 忽略。

当前 SQLite 方案面向本地开发、单实例演示和故障恢复。若以后部署多进程或多机器服务，
应替换为支持共享锁和租约的持久化后端，`ExecutionStore` 协议保留了这个替换边界。

## 3. HITL 决策

审批模式由 `SCENICPILOT_APPROVAL_MODE` 控制：

- `on_request`：默认值；仅 API/CLI 明确要求时暂停；
- `always`：所有行程工具调用都需要批准；
- `disabled`：不进入人工审批。

现有天气、距离、预算和营业时间都是只读查询，默认采用 `on_request`，避免每个普通问答
都等待人工操作。未来增加下单、预约、支付或消息发送等有副作用工具时，应把该环境设为
`always`，或为敏感动作增加独立审批节点。

拒绝审批后直接进入 `approval_rejected`，返回 `answer_status=refused`，工具调用数保持 0。

## 4. 幂等语义

客户端可以为每个 `start` 或 `resume` 提供 `idempotency_key`：

- 键的作用域是 `thread_id`；
- 相同键和相同规范化输入返回第一次响应，并设置 `idempotency_replayed=true`；
- 相同键配不同操作或输入会返回冲突，不允许静默复用；
- 进程在账本写入后异常退出时，重试会先读取 LangGraph checkpoint，尽量恢复已有状态，
  不重新执行已完成的工具节点。

幂等键去重的是一次命令，不是整个会话。一次 `start` 和一次人工 `resume` 应分别使用不同
的键。

## 5. 三层终止与预算

默认治理参数：

| 边界 | 默认值 | 作用 |
|---|---:|---|
| `SCENICPILOT_EXECUTION_TOOL_CALL_LIMIT` | 24 | 限制一次图执行中的工具调用总数 |
| `SCENICPILOT_PLANNING_MAX_REVISIONS` | 2 | 限制验证失败后的确定性修订次数 |
| `SCENICPILOT_EXECUTION_RECURSION_LIMIT` | 40 | 防止状态图因错误路由无限运行 |

工具预算在 `enrich_tools` 与 `validate` 之间共享。超过预算的调用不会触达 provider，而是写入
`degradation_reason=call_budget_exceeded`。路线、预算或营业时间因此未验证时，验证器产生
warning，最终状态为 `requires_live_check`，不会把缺失结果误报为已满足约束。

`governance_trace` 记录审批事件、每批请求数/执行数/跳过数、累计调用数和剩余额度；
`planning_trace` 继续记录候选、工具、验证、修订和输出内容，两种轨迹职责分离。

## 6. 可运行入口

CLI 发起并恢复审批会话：

```powershell
python -m scenic_pilot.cli "一家三口玩一天，预算 1000 元" `
  --thread-id demo-1 --idempotency-key start-1 --require-approval --json

python -m scenic_pilot.cli --thread-id demo-1 `
  --idempotency-key resume-1 --resume approve --reason "允许只读查询" --json
```

FastAPI：

```text
POST /v1/sessions
POST /v1/sessions/resume
GET  /v1/sessions/{thread_id}
```

`POST /v1/sessions` 接收 `message/scenic_id/thread_id/idempotency_key/require_approval`；恢复接口
接收 `thread_id/idempotency_key/approved/reason`。幂等键冲突返回 HTTP 409。

## 7. 离线验收

```powershell
python scripts/evaluate_governance.py
python -m pytest tests/test_persistence.py tests/test_api.py -q
```

验收覆盖：

1. 中断前没有工具副作用；
2. 关闭并重新创建 Runtime 后仍能找回中断并完成；
3. 拒绝审批后立即终止；
4. 相同幂等键只返回已保存结果，不重复工具；
5. 相同键不同输入发生冲突；
6. 工具额度耗尽后 provider 不再被调用；
7. 新请求复用 `thread_id` 时，本轮 trace 和输出字段会重置。
