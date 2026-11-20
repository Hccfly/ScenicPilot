# 离线评测与故障注入

这里建立了一个严格的 115-case 离线门禁。评测入口不会读取 `.env` 中的 live 配置：
真实语料检索只加载本地持久化 BGE 索引，模型固定为 `offline`，工具固定为 `offline`。

## 1. Case 分布

| 分组 | 数量 | 核心验证 |
|---|---:|---|
| `retrieval` | 50 | Hit@5、多答案 Recall@5、禁止来源、景区隔离、证据完整度、上下文 token |
| `trusted_answer` | 31 | 引用绑定、claim 绑定、拒答无引用、口语化实时信息拒答、决策轨迹 |
| `planning` | 10 | 结构化 plan、来源白名单、时长、偏好、路线、预算拒绝 |
| `fault_injection` | 11 | 超时、异常、空结果、缺字段、脏类型、负数金额、模型未知来源 |
| `recovery_idempotency` | 7 | 重启恢复、人工拒绝、重复 start、重复 resume 与冲突 |
| `call_budget` | 3 | 0 次和 1 次工具额度，provider 实际调用数与 state 对齐 |
| `loop_limit` | 3 | 0 次和 1 次修订上限，拒绝并终止 |

总数严格为 115，`case_id` 必须唯一。目录定义在
`data/eval/offline_cases.jsonl`；加载时会验证分组数量和必填字段，避免漏测后仍显示通过。
其中 15 条检索 case 有 2–5 个可接受来源，通过标准是 Hit@5（命中任一来源），
Recall@5 只作为覆盖度诊断，避免把合理的替代来源误判成噪声。

## 2. 隔离策略

每个 Agent case 都使用：

- 独立 `thread_id=case_id`；
- 独立 SQLite 文件；
- 独立执行账本、工具缓存、熔断器和 Fake 计数器；
- case 完成后显式关闭 SQLite，再由 `TemporaryDirectory` 清理。

只有不可变的 demo knowledge index 和正式 BGE 检索索引在 case 间共享，以降低离线评测耗时。
检索与可信回答 case 本身无会话状态，每次重新构造 answer policy 和结果对象。

Windows 下不使用项目内共享临时目录，因此不会再次触发 `.pytest-tmp` 的跨账户 ACL 问题；
正式报告使用 UTF-8 JSON，SQLite 临时产物不会进入仓库。

## 3. 结构化断言

评测不会只检查自然语言中是否出现某个词。每条 case 都输出：

```json
{
  "case_id": "eval-recovery-004",
  "passed": true,
  "failure_reasons": [],
  "assertions": [
    {
      "name": "same_key_different_input_conflicts",
      "passed": true,
      "expected": true,
      "actual": true
    }
  ],
  "observation": {
    "replayed": true,
    "conflict": true,
    "tool_calls": 6
  }
}
```

结构化字段覆盖 `answer_status`、`constraint_status`、`citations`、`claims`、`plan`、
`source_id`、`violations`、`revision_count`、`tool_results`、`trace` 和
`governance_trace`。失败报告同时保留 assertion 的 expected/actual，可直接定位到具体 case
和字段。

## 4. 故障注入边界

| Case | 注入 | 必须观察到的边界行为 |
|---|---|---|
| `eval-fault-001` | 天气 `TimeoutError` | 天气失败被分类，预算工具继续 |
| `eval-fault-002` | 预算工具异常 | 会话完成，产生 `budget_unverified` |
| `eval-fault-003` | 距离返回成功但耗时为空 | 产生 `route_unverified` |
| `eval-fault-004` | `is_open="yes"` | 脏类型转为 `invalid_tool_result` |
| `eval-fault-005` | `estimated_cost=-100` | 负金额不写入 plan，预算未核验 |
| `eval-fault-006` | 模型返回未知 `source_id` | source 被拒绝，回退规则排序 |
| `eval-fault-007` | 检索空结果 | 可信拒答，citations/claims 为空 |

这轮故障注入发现并修复了三个真实边界问题：

1. 非布尔 `is_open` 原先可能绕过营业时间 warning；
2. 负数预算估算原先可能被当作有效金额；
3. SQLite 首次响应中的 tuple 在幂等重放后会变成 JSON list，导致首响与重放结构不一致。

Runtime 现在在保存和返回前统一做 JSON 归一化，工具脏返回则标记
`degradation_reason=invalid_tool_result`。

## 5. 恢复、幂等与上限

恢复 case 会真正关闭第一个 Runtime、重新打开同一 SQLite 文件，再通过相同 `thread_id`
恢复 interrupt。Fake 工具计数器验证：

- 中断和拒绝之前 provider 调用数为 0；
- 重复 `start` 或 `resume` 不增加调用数；
- 同幂等键、不同输入触发 `IdempotencyConflict`；
- 首次响应和 SQLite 重放响应具有相同 JSON 结构。

调用预算与循环上限不混在普通故障 case 里：`eval-budget-*` 分别固定额度为 0/1，
`eval-loop-*` 分别固定最大修订次数为 0/1，并同时断言 provider 调用数、state 计数、
`revise_plan` 节点次数、revision history 和最终拒绝状态。

## 6. 执行与定位

完整离线门禁：

```powershell
python scripts/evaluate_offline.py
```

按分组运行：

```powershell
python scripts/evaluate_offline.py --group recovery_idempotency
python scripts/evaluate_offline.py --group fault_injection --group call_budget
```

只复现失败 case：

```powershell
python scripts/evaluate_offline.py --case-id eval-recovery-004
```

默认报告位于
`data/index/zhuhai-chimelong/offline_evaluation.json`。报告包含 catalog SHA-256、全局汇总、
分组汇总、`failed_case_ids`、完整 `failed_cases` 和全部 case 明细；脚本只要存在失败 case
就返回非零退出码，可直接作为 CI 门禁。

## 7. 当前基线

2026-09-13 的离线基线：

| 分组 | 结果 |
|---|---:|
| retrieval | 30/30 |
| trusted_answer | 10/10 |
| planning | 5/5 |
| fault_injection | 7/7 |
| recovery_idempotency | 4/4 |
| call_budget | 2/2 |
| loop_limit | 2/2 |
| 总计 | 60/60 |
