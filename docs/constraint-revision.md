# 约束验证与有界修订

实现将一次性的 `validate -> finalize` 改造成可收敛的约束闭环。系统先区分“计划明确
不可执行”和“实时信息尚未确认”：前者进入修订，后者保留 warning 并正常输出
`requires_live_check`，避免离线环境产生无意义循环。

## 闭环结构

```text
generate_candidates
  -> enrich_tools
  -> validate
       |-- no error ------------------------------> finalize
       |-- error + revision budget -> revise_plan
       |                              |-- changed -> enrich_tools -> validate
       |                              \-- no safe change ----------> finalize
       \-- error + limit reached -----------------> finalize
```

默认最多修订 2 次。修订后必须重新经过 `enrich_tools`，因为删除或补入站点会改变路线、
预算和计划时间轴；禁止在旧工具结果上直接宣称修订成功。

## 校验规则

| 规则 | error | warning | 修订策略 |
| --- | --- | --- | --- |
| 游玩时长 | 游玩分钟数超过用户上限 | 无 | 从末端删除非强制停靠点 |
| 路线时长 | 游玩 + 路线超过用户上限 | 路线仅为估算或缺少耗时 | 删除导致超时的非强制停靠点，重新算路线 |
| 预算 | 工具估算超过预算 | 预算工具无法确认 | 先删除可选餐饮；仍超预算则拒绝 |
| 重复站点 | 同一 source/name 重复 | 无 | 稳定保留第一次出现 |
| 强制偏好 | 已声明的 marine/indoor_science 未覆盖 | 普通偏好不命中不拦截 | 只从当前检索 hits 中补入或替换 |
| 营业状态 | `is_open=false` | 无法实时确认 | 删除关闭站点 |
| 营业窗口 | 计划时段超出 opens/closes | 无法取得窗口 | 删除冲突站点 |

error 最终仍存在时，`answer_status=refused`、`constraint_status=rejected`，自然语言答案不会
继续展示该草案为可执行计划；失败草案仍保留在结构化 state 中供审计。只有 warning 时，
`constraint_status=valid`，但 `answer_status=requires_live_check`。

## 时间轴与路线

意图识别支持从问题中抽取 `HH:MM`；没有显式时间时默认 09:30。验证器按以下顺序生成
每个 stop 的 `start_time`、`end_time` 和 `travel_from_previous_minutes`：

1. 用户出发地到首站的路线耗时；
2. 当前站游玩时长；
3. 跨园区到下一站的路线耗时；
4. 下一站游玩时长。

同园区的 attraction 与 dining 不重复增加驾车时间。`validation.metrics` 同时保留
`visit_minutes`、`route_minutes`、`total_duration_minutes` 和用户时长上限，便于定位
“项目本身超时”还是“加入交通后超时”。

默认 `OfflineOpeningHoursTool` 不伪造营业时间，因此真实运行会产生 `hours_unverified`
warning。验证器已经支持注入返回 `is_open/opens_at/closes_at` 的实时或结构化事实适配器；
在没有可靠当日来源时，不会把历史页面时间当作已确认事实。

## 强制偏好

普通“想看海洋动物”仍是软排序信号；问题包含“必须、一定要、务必、必看、只想”时，
`marine` 和 `indoor_science` 才进入 `required_preferences`。亲子与老人当前表示人群和节奏，
不会被错误当作必须出现某个地点的内容约束。

修订器补足偏好时仅检查当前检索 `hits`。如果模型遗漏海洋主题，但检索中有海洋王国，
修订器可以补入；如果检索中没有证据，则不创建地点，最终拒绝。

## 修订动作与终止条件

当前确定性动作包括：

- `remove_duplicate`
- `remove_unavailable_stop`
- `add_required_preference`
- `replace_for_required_preference`
- `remove_optional_dining`
- `remove_for_time_limit`

每轮把 `attempt`、`trigger_codes`、`actions`、修改前后 source IDs 和 `changed` 写入
`revision_history` 及 `planning_trace.revision.rounds`。终止条件有三种：

1. 校验无 error；
2. 达到 `SCENICPILOT_PLANNING_MAX_REVISIONS`；
3. 修订器找不到安全且确定性的修改。

默认值可在 `.env` 调整：

```dotenv
SCENICPILOT_PLANNING_MAX_REVISIONS=2
SCENICPILOT_PLANNING_DEFAULT_START_TIME=09:30
```

两轮是当前低复杂度计划的工程上限：第一轮通常修复地点或偏好，第二轮用于重新补全工具后
处理新暴露的路线/预算冲突。端到端评测依据 100 条 case 中的平均修订次数、拒绝率和误修订率
决定是否调整。

## 验收

运行：

```powershell
python scripts/evaluate_constraints.py
```

当前 5/5 通过：

- 不确定营业时间和离线路线仅 warning，不进入循环；
- 50 元三人预算在两轮内确定不可修复并拒绝；
- 关闭的海洋王国站点被删除，工具重新执行后通过；
- 超长路线触发站点裁剪，并以新路线重新验证；
- 模型故意遗漏强制海洋偏好时，从检索证据补入海洋王国，随后处理新增路线约束并收敛。

单元测试另外覆盖营业窗口、计划时间轴、重复站点和来源受限的偏好修订。评测产物保存在
`data/index/zhuhai-chimelong/constraint_eval.json`，不作为线上事实来源。
