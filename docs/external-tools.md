# 外部工具与韧性边界

工具层将天气、距离和预算从“只有协议的离线占位实现”升级为可配置、可观测、可降级的工具栈。这里先稳定工具执行边界；天气和距离在 行程编排阶段 作为 `tool_enrich` 节点接入行程 StateGraph，避免提前把网络副作用耦合进状态图。

## 供应商与职责

| 工具 | live provider | 无配置或故障时 | 当前输出 |
| --- | --- | --- | --- |
| 天气 | Open-Meteo Geocoding + Forecast | 离线提示，不伪造天气 | 地点、日期、天气码、最高/最低温、最大降水概率、来源与观测时间 |
| 距离 | 高德地理编码 + 驾车路径规划 2.0 | 离线启发式，不伪造距离 | 起终点坐标、驾车距离、预计时长、出租车费用、来源与观测时间 |
| 预算 | 本地确定性规则 | 本地规则本身可用 | 餐饮预留估算、币种、明确排除项 |

天气和路线都是易变事实。成功响应保留 `provider`、`source_url`、`observed_at`；降级响应保留 `degradation_reason`，调用方不需要从自然语言猜测工具是否可信。

天气适配器遵循 [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api) 与 [Forecast API](https://open-meteo.com/en/docs) 的字段契约。由于“珠海”中文查询存在同名地点，适配器使用 `Zhuhai` 拉取候选，并强制校验 `admin1=广东` 和城市名；候选未通过时宁可降级，也不返回错误地点的天气。距离适配器遵循高德 [地理编码](https://lbs.amap.com/api/webservice/guide/georegeo) 与 [路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute) 契约。

## 执行策略

三个工具共用 `ResilientToolRunner`，但按工具名维护独立熔断状态：

1. 使用参数归一化后的 key 查询新鲜缓存。
2. provider 未配置时直接进入显式 fallback，不消耗重试次数，也不污染熔断计数。
3. provider 可用时传入超时预算；可重试错误最多尝试 2 次，第二次前线性退避。
4. 调用成功后写入缓存并清空该工具的连续失败计数。
5. 最终失败时优先返回仍处于 stale 窗口的旧值，否则调用离线 fallback。
6. 连续失败达到阈值后短暂熔断；熔断期间不访问 provider。
7. 每次结果都附加 `scenicpilot-tool-trace-v1`，每次尝试的聚合结果进入有界内存审计队列。

`ToolSuite.run()` 在每个工具调用外再设一层隔离边界。某个适配器即使抛出未预期异常，后续工具仍会执行，最终 summary 分别统计成功、降级和失败数量。

## 当前参数

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `timeout_seconds` | 3 s | 每次 provider HTTP 请求的超时预算 |
| `max_attempts` | 2 | 首次调用加一次重试 |
| `retry_backoff_seconds` | 0.2 s | 线性重试退避基数 |
| `cache_ttl_seconds` | 600 s | 新鲜缓存窗口 |
| `stale_ttl_seconds` | 3600 s | provider 故障时允许返回旧值的最长窗口 |
| `circuit_breaker_threshold` | 3 | 连续失败后熔断 |
| `circuit_reset_seconds` | 30 s | 熔断冷却时间 |

这些是低流量开发阶段的初始工程参数，不是假定的最终生产参数。等积累真实延迟与错误审计后，应按 provider 的 p95/p99 延迟、失败率、限流响应和缓存命中率重新决定。

## 运行模式

- `offline`：默认值。完全不访问天气和地图网络；适合测试、演示和无密钥环境。
- `auto`：启用 Open-Meteo；存在 `AMAP_API_KEY` 时启用高德，否则距离工具显式降级。
- `live`：与 `auto` 使用相同 provider 选择，但用于明确表达本次运行希望访问实时服务。缺少高德 Key 时仍保持可用并返回降级原因，不会在启动阶段使完整会话失败。

项目使用 `python-dotenv` 在 `Settings.from_env()` 中解析根目录 `.env`。合并顺序为
“代码默认值 < `.env` < 进程环境变量”，所以本地命令可直接使用 `.env`，部署环境
仍可通过容器或主机变量覆盖配置。解析只发生在配置边界，不会把文件内容输出到轨迹；
`Settings` 的调试表示也隐藏 `amap_api_key`。

运行探针：

```powershell
python scripts/probe_tools.py --mode offline
python scripts/probe_tools.py --mode live
# 或使用进程变量临时覆盖 .env
$env:AMAP_API_KEY="your-key"
python scripts/probe_tools.py --mode live --origin "珠海站" --destination "珠海长隆度假区"
```

探针输出包含三个工具结果、批次统计、当前策略和本次审计事件。API Key 只进入请求参数，不会出现在结果或审计事件中。

## 验收证据

`tests/test_tools.py` 覆盖以下路径：

- 成功结果缓存与缓存命中；
- 可重试错误后恢复；
- 不可重试错误立即降级；
- provider 故障时返回 stale cache；
- 连续故障触发熔断并跳过 provider；
- Open-Meteo 和高德响应字段映射；
- 高德 Key 不进入输出；
- 单工具抛异常后，预算工具仍正常执行；
- 预算工具超时后，完整行程会话仍结束并在回答中说明 `timeout` 降级；
- 默认离线模式不需要任何 provider 配置。

工具层的完成边界是“工具可独立调用、故障可隔离、轨迹可解释”。行程状态图已在
`enrich_tools` 节点中根据行程调用这些工具并把结果写回计划；路线与营业时间的完整
约束校验和自动修订已在 约束修订闭环接入。
