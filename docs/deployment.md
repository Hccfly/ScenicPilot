# 部署与运行

ScenicPilot 默认以单进程、SQLite、离线工具和离线模型启动。这个配置适合本地演示、
功能验收和单实例部署；不会请求 OpenAI、DeepSeek、Open-Meteo 或高德。

## Windows 本地启动

要求 Python 3.11 或更高版本。首次安装：

```powershell
cd ScenicPilot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

先执行离线产品验收，再启动服务：

```powershell
$env:SCENICPILOT_TOOL_MODE = "offline"
$env:SCENICPILOT_LLM_PROVIDER = "offline"
python scripts/demo_product.py
uvicorn scenic_pilot.api:app --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000`。探活命令：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

`/health/live` 只检查进程；`/health/ready` 还检查知识文件、Web 页面和已配置的检索索引。
readiness 非 200 时不应接收新流量。

## Docker

```powershell
docker build -t scenicpilot:1.0.0 .
docker run --rm -p 8000:8000 `
  -v scenicpilot-runtime:/app/data/runtime `
  scenicpilot:1.0.0
```

镜像使用非 root 用户，默认单 worker，并把 checkpoint 和幂等账本放到
`/app/data/runtime/scenicpilot.sqlite3`。命名卷使容器重启后仍可恢复暂停会话。

## 启用真实适配器

复制 `.env.example` 为 `.env`，按需设置以下配置：

- `SCENICPILOT_TOOL_MODE=auto` 或 `live`：启用 Open-Meteo；
- `AMAP_API_KEY`：启用高德驾车距离；
- `SCENICPILOT_LLM_PROVIDER=deepseek` 与 `DEEPSEEK_API_KEY`，或 `openai` 与
  `OPENAI_API_KEY`：只用于对已检索候选进行排序；
- `SCENICPILOT_RETRIEVAL_INDEX_PATH`：加载预先构建的本地 BGE 索引。

Key 只能通过运行环境或不提交 Git 的 `.env` 注入，不能写入镜像、演示数据或前端资源。
正式索引需要先安装 `.[retrieval]` 并离线构建；采集环境另安装 `.[ingest]`。

## 生产边界

- SQLite 方案保持 `--workers 1`，适用于单实例。进程内会串行访问共享 checkpointer；
  多 worker 或多副本部署前，需要把 LangGraph checkpointer 和执行账本替换为共享数据库实现；
- `/v1/chat/stream` 与 `/v1/sessions/stream` 经反向代理时应关闭 buffering，并配置高于
  最长图执行时间的读超时；
- `data/demo` 是演示资料。生产环境必须通过采集流水线更新事实，保留抓取时间、内容哈希、
  来源等级和 `volatile` 标记；
- readiness 只验证启动依赖是否存在，不替代外部天气、距离和模型服务的运行时降级监控；
- 对外暴露前应在网关补充 TLS、身份认证、速率限制和请求体大小限制。
