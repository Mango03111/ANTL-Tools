<p align="center">
  <img src="images/display-logo.png" alt="Inference Performance Display" width="480">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/HTML5-E34F26?logo=html5&logoColor=white" alt="HTML5">
  <img src="https://img.shields.io/badge/CSS3-1572B6?logo=css3&logoColor=white" alt="CSS3">
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?logo=javascript&logoColor=black" alt="JavaScript">
  <img src="https://img.shields.io/badge/Prometheus-E6522C?logo=prometheus&logoColor=white" alt="Prometheus">
  <img src="https://img.shields.io/badge/OpenAI-000000?logo=openai&logoColor=white" alt="OpenAI-compatible">
  <img src="https://img.shields.io/badge/No%20Dependencies-Standard%20Library%20Only-2E7D32" alt="No Dependencies">
</p>

<h1 align="center">Inference Performance Display</h1>

一个无第三方依赖、可拓展的推理性能监测看板。后端使用 Python 标准库定时
读取推理服务的 Prometheus 指标，前端使用单文件 HTML/CSS/JavaScript 展示
请求、吞吐、KV Cache 和延迟趋势，并附带用量统计页面（token 消耗、费用与
分时段趋势）。

本工程只连接已经运行的推理服务，不负责创建、启动、停止或修改其容器。

## 功能

- 展示 running、waiting、capacity/deferred waiting 和累计完成请求数。
- 计算 prompt tokens/s、generation tokens/s 和 requests/s。
- 展示 KV Cache 使用率、prefix cache 命中率和累计 token 数。
- 根据最近窗口内新增的 Histogram 样本估算 E2E、TTFT、ITL、TPOT 和
  queue latency 的 p50/p90/p99。
- Token 吞吐、请求并发、KV Cache 压力、端到端延迟四个趋势图，浏览器
  保留最近 60 秒趋势，支持暂停、清空以及 200ms/500ms/1s/2s 刷新间隔。
- 推理服务不可达或数据过期时提供 在线 / 数据已陈旧 / 离线 状态。
- 用量统计页面（`/usage.html`）：累计请求与 token 数、按模型价格计算的
  累计费用、当前模型（超长自动滚动），以及输入/输出 token 双纵轴趋势图，
  支持近一小时 / 近12小时 / 近24小时 时间范围（长范围自动做 15/30 分钟
  移动平均平滑）。
- 多服务聚合：`services.yaml` 配置多个推理服务，性能与用量页面均可在
  "全部聚合"与单个服务之间切换（详见"多服务监测"）。
- 全部实现仅依赖 Python 标准库；不需要 Prometheus、Grafana、Node.js 或
  前端构建步骤。

## 前置条件

- Python 3.10 或更高版本。
- 一个或多个已经运行、且可从宿主机访问的 OpenAI-compatible 推理服务（需
  暴露 Prometheus 指标端点）。
- 默认地址为 `http://127.0.0.1:8000`，指标地址为
  `http://127.0.0.1:8000/metrics`；多个服务在 `services.yaml` 中配置（见
  "多服务监测"）。

先确认现有实例可用：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/version
curl -fsS http://127.0.0.1:8000/v1/models
curl -fsS http://127.0.0.1:8000/metrics | head
```

`/health` 成功时可能返回空响应体，这是正常现象。

## 启动

在工程目录中执行：

```bash
python3 server.py
```

浏览器打开：

```text
http://127.0.0.1:8090/           # 性能看板
http://127.0.0.1:8090/usage.html # 用量统计
```

也可以在启动前覆盖配置，例如：

```bash
INFERENCE_METRICS_URL=http://127.0.0.1:8000/metrics \
POLL_INTERVAL_MS=500 \
SERVER_PORT=8090 \
python3 server.py
```

### 后台运行

使用 `service.sh` 将服务持久化运行在后台（`setsid + nohup` 完全脱离终端，
关闭终端后继续运行；PID 写入 `server.pid`，日志追加到 `server.log`）：

```bash
./service.sh start     # 后台启动（已在运行时直接提示，不会重复启动）
./service.sh status    # 查看进程与接口状态
./service.sh logs      # 跟踪日志（Ctrl+C 退出）
./service.sh restart   # 重启
./service.sh stop      # 停止（SIGTERM 优雅关闭，会 flush 用量 CSV）
```

环境变量覆盖同样适用，例如 `SERVER_PORT=9090 ./service.sh start`。
脚本从任意目录调用均可（路径相对脚本自身解析）。注意：脚本方式不随系统
重启自启，也不在进程崩溃后自动拉起；如需开机自启与崩溃自愈，请改用
systemd 服务（`systemctl` 管理，`Restart=always`）。

服务启动后提供以下接口（`/api/*` 均支持可选参数 `?service=<id|all>`，
缺省或 `all` 为全部服务聚合视图，未知 id 返回 404）：

- `GET /` 和 `GET /index.html`：性能看板页面。
- `GET /usage.html`：用量统计页面。
- `GET /api/metrics`：当前规范化指标及健康状态，始终返回 JSON。
- `GET /api/services`：已配置服务列表（id、显示名、指标地址、健康状态）。
- `GET /api/health`：看板采集健康状态；上游不可用时返回 HTTP 503。
- `GET /api/raw?service=<id>`：最近一次原始 Prometheus 文本，默认禁用，
  仅支持具体服务（聚合无对应单一文本）。
- `GET /api/usage`：用量统计完整数据（累计值 + 历史分段）。
- `GET /api/usage/history`：仅历史分段序列。
- `GET /api/usage/health`：用量写入健康状态（`last_error` / `last_write_at`）。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `INFERENCE_METRICS_URL` | `http://127.0.0.1:8000/metrics` | 推理服务实例的 Prometheus 指标地址 |
| `SERVER_HOST` | `127.0.0.1` | 看板监听地址 |
| `SERVER_PORT` | `8090` | 看板监听端口 |
| `POLL_INTERVAL_MS` | `500` | 后端抓取间隔，允许 100–60000ms |
| `FETCH_TIMEOUT_SECONDS` | `5` | 单次抓取超时 |
| `RATE_WINDOW_SECONDS` | `5` | Counter 速率计算窗口 |
| `LATENCY_WINDOW_SECONDS` | `30` | Histogram 分位数计算窗口 |
| `STALE_AFTER_SECONDS` | `max(3, 4 × 抓取间隔)` | 超过此时长未成功抓取即标记 stale |
| `ENABLE_RAW_API` | `false` | 设为 `true`/`1` 后开放 `/api/raw` |
| `ENABLE_USAGE_STATS` | `true` | 用量统计插件开关，设为 `false`/`0` 关闭 |
| `USAGE_WRITE_INTERVAL_SECONDS` | `60` | 用量 CSV 写入间隔，允许 1–86400s |
| `USAGE_DATA_DIR` | `usage_data/` | 用量 CSV 输出基础目录（实际按服务写入 `<目录>/<id>/`） |
| `USAGE_PRICING_FILE` | `pricing.yaml` | 模型价格文件 |
| `SERVICES_FILE` | `services.yaml` | 多服务配置文件（脚本同目录） |

前端读取的是后端内存快照，不会因多个浏览器页面而成倍抓取推理服务。通常无需
把 `POLL_INTERVAL_MS` 调到 200ms 以下。

## 多服务监测

默认读取脚本同目录的 `services.yaml`（可用 `SERVICES_FILE` 覆盖）。每个服务
由内部 id 标识，id 同时用作用量数据目录名（`usage_data/<id>/`）和 API 的
`?service=` 取值：

```yaml
services:
  - id: antlserver01        # 必填，唯一，仅允许 [A-Za-z0-9._-]
    name: antlserver01      # 可选，页面显示名，缺省同 id
    metrics_url: http://127.0.0.1:8000/metrics
  - id: siton02
    name: siton02
    metrics_url: http://10.171.22.152:8000/metrics
```

- 文件不存在或无条目时回退为单服务模式：仅监听 `INFERENCE_METRICS_URL`，
  id 为 `default`，行为与旧版本一致。
- 性能页面与用量页面顶部均可在"全部聚合"与单个服务之间切换，选择保存在
  浏览器本地；切换后趋势图从该视图的服务端历史重新填充。
- 聚合口径：请求数、tokens、速率与累计值求和；KV Cache 使用率取最大；
  前缀缓存命中率按各服务窗口内增量求和后重算；E2E/TTFT/ITL/TPOT/queue
  分位数用窗口内新增 Histogram 分桶跨服务合并后重算（要求分桶边界一致，
  不同 vLLM 版本无法合并时显示 `--`）。某服务离线时其计数按 0 计，健康
  状态按最差服务显示，错误信息带服务 id 前缀。
- 用量统计按服务分目录写入 `usage_data/<id>/usage-YYYY-MM-DD.csv`；聚合
  视图的累计值为各服务求和，趋势图按 60 秒时间桶对齐求和。

## 用量统计

用量统计插件默认开启。它按 `USAGE_WRITE_INTERVAL_SECONDS`（默认 60 秒）
把推理服务累计 Counter 的增量写入 `usage_data/<id>/usage-YYYY-MM-DD.csv`
（每个服务一个子目录，见"多服务监测"），每行代表上一写入周期内的新增
用量，并附带按 `pricing.yaml` 计算的 input/output/total 费用。

- 网页顶部卡片显示累计值（由全部历史 CSV 求和，重启后继续累计）：
  请求数、输入/输出/总 tokens、累计费用、当前模型（超长自动滚动）。
- 趋势图为输入/输出 token 双纵轴折线，横轴为时间并带"现在"标记；可选
  近一小时（原始 1 分钟分段）、近12小时（15 分钟移动平均）、近24小时
  （30 分钟移动平均）。
- 历史分段保存在后端内存中（最多 3600 个写入周期，60 秒间隔下约 60
  小时）；启动时自动从 CSV 回读最近 3600 行填充趋势窗口，累计值由全部
  历史 CSV 求和后继续累计。仅服务停机期间（无 CSV 行）在趋势中缺失。
- 价格文件按模型名匹配（支持前缀匹配），未命中时使用 `default` 条目；
  当前 FP8 容器使用 `qwen38-27b-fp8`（输入 0.424 / 输出 1.696 USD 每百万
  tokens）。

## 指标说明

Counter 速率至少需要两个成功抓取点，因此刚启动时 tokens/s 和 requests/s
可能短暂显示 `--`。Counter 回退（例如推理服务被外部重启）不会产生负速率。

延迟分位数仅使用窗口内新增的 Histogram 样本。实例空闲时显示 `--` 属于预期
行为；旧请求的累计 Histogram 不会被误报为当前延迟。

不同推理引擎版本可能缺少个别指标。后端对已知的新旧指标名称提供别名并以 `null`
表示缺失值，页面对应显示 `--`。

## 常见问题

### 页面显示 offline 或 stale

先从运行 dashboard 的同一网络环境执行：

```bash
curl -v http://127.0.0.1:8000/metrics
```

如果推理服务容器没有把端口发布到宿主机，请修正现有容器的网络配置或将
`INFERENCE_METRICS_URL` 指向实际可达地址。dashboard 不会替你修改容器。

### 页面可打开但部分值为 `--`

空闲期间速率和窗口延迟可能没有新样本；某些推理引擎版本也不提供
`num_requests_swapped` 等指标。可查看 `/api/metrics` 中的 `health` 与
`diagnostics` 判断采集是否正常。

### 局域网访问

可以设置 `SERVER_HOST=0.0.0.0`，但本服务没有认证机制。只应在可信网络中暴露，
并谨慎开启 `/api/raw`，因为原始指标可能包含模型名和运行配置标签。

### 用量趋势的近12/24小时范围有缺失

启动时会自动从 CSV 回读最近 3600 个写入周期（60 秒间隔下约 60 小时），
重启后趋势即恢复。仅服务停机期间（无 CSV 行）在趋势中缺失，属于预期
行为。
