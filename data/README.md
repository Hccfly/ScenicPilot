# 数据目录

- `curated/zhuhai_chimelong_sources.jsonl`：经过人工核验的来源清单，每行对应一个可独立访问的官方页面。
- `source_plan.csv`：便于人工审阅和维护的同源清单。
- `demo/`：为离线功能演示编写的合成资料，不代表实时事实。
- `index/`：采集、切块和检索流水线生成的索引产物，不纳入 Git。
- `eval/retrieval_cases.jsonl`：45 条 retrieval-only 离线评测集。
- `eval/offline_cases.jsonl`：覆盖检索、回答、规划、故障、恢复、幂等和终止边界的 100 条端到端评测集。

`curated` 目前保存页面级摘要和采集 metadata，不保存完整网页副本。票价、营业时间、场次、交通班次、权益和餐饮状态等字段统一要求实时核验。

执行质量检查：

```powershell
python scripts/validate_sources.py
python scripts/validate_sources.py --check-network
```
