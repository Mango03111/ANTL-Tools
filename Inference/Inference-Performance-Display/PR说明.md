# 性能趋势 CSV 导出

## PR 信息

- Issue：Mango03111/ANTL-Tools#2
- 分支：`feature/metrics-history-csv`
- 目标分支：`master`
- 功能范围：Inference Performance Display

## 目标

性能看板已经提供最近 60 秒的趋势图，但实验报告需要把吞吐、并发、
KV Cache 使用率和部分延迟数据带到离线表格中继续处理。原先只能人工
抄写或反复调用 JSON 接口，因此本 PR 增加直接下载当前视图趋势 CSV 的
功能。

## 实现内容

### 后端接口

新增：

```text
GET /api/metrics.csv?service=<id|all>
```

接口读取当前内存中的历史快照，不额外请求推理服务。返回字段：

| 字段 | 含义 |
| --- | --- |
| `service` | 当前服务 ID 或 `all` |
| `timestamp_unix` | Unix 时间戳 |
| `timestamp_iso` | UTC ISO 8601 时间 |
| `generation_tokens_per_sec` | 生成吞吐 |
| `prompt_tokens_per_sec` | 输入吞吐 |
| `running_requests` | 正在执行的请求数 |
| `waiting_requests` | 等待请求数 |
| `kv_cache_usage_pct` | KV Cache 使用率，百分比 |
| `e2e_p50_ms` | E2E P50，毫秒 |
| `e2e_p90_ms` | E2E P90，毫秒 |
| `e2e_p99_ms` | E2E P99，毫秒 |

响应头包含：

```text
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment
```

CSV 使用 UTF-8 BOM，便于电子表格软件识别中文和编码。

未知服务 ID 返回 HTTP 404。

### 前端

性能看板工具栏增加：

```text
导出趋势 CSV
```

下载链接跟随当前服务选择变化：

```text
/api/metrics.csv?service=all
/api/metrics.csv?service=antlserver01
```

### 测试与文档

- 增加标准库 `unittest` 测试。
- README 增加接口、功能和测试说明。
- AGENTS 增加接口验证命令。

## 本地验证

```bash
python3 -m py_compile server.py usage_stats.py
python3 -m unittest discover -s tests -v
```

预期：

```text
Ran 2 tests
OK
```

提取内联 JavaScript 后执行：

```bash
node --check
```

接口验证：

```bash
curl -sS -D - \
  "http://127.0.0.1:8090/api/metrics.csv?service=all"
```

预期：

```text
HTTP/1.0 200 OK
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment
```

空历史时仍返回 CSV 表头；未知服务返回 HTTP 404。

## 兼容性

- 仍然仅使用 Python 标准库。
- 不新增端口或网络请求。
- 不改变已有 JSON API。
- 不影响用量统计和多服务聚合。
- 导出时读取看板已经维护的内存历史，不增加推理服务抓取压力。

## 回滚

回滚本提交后：

- 移除 `/api/metrics.csv` 路由。
- 移除工具栏“导出趋势 CSV”按钮。
- 删除对应测试和文档。

模型、推理服务和已有看板指标不受影响。
