# AGENTS.md

供 AI 编码代理在本工程中工作时参考。

## 工程概览

无第三方依赖的多推理（vLLM）服务性能监测看板。后端用 Python 标准库定时抓取
`services.yaml` 配置的各推理服务 Prometheus 指标，支持"全部聚合 + 分服务"
两种视图；前端为单文件 HTML/CSS/JS（无构建步骤），提供性能看板与用量统计
两个页面，顶部均可切换聚合/单服务。

本工程只读取已运行的推理服务，不负责创建/启动/停止容器。

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `server.py` | 后端全部逻辑（约 1400 行）：Prometheus 文本解析、`load_services`、`MetricsCollector`（单服务采集）、`ServiceRegistry`（多服务轮询+聚合）、HTTP 服务、路由 |
| `usage_stats.py` | `UsageStatsPlugin` 用量统计插件：按分钟把 Counter 增量与费用写入 CSV，启动时回读恢复 |
| `services.yaml` | 多服务配置：`id`（内部 id，数据目录/URL 参数用）+ 可选 `name`（显示名）+ `metrics_url`；手写极简 YAML 解析，勿换 PyYAML |
| `index.html` | 性能看板页面（单文件，内联 CSS/JS，约 2450 行） |
| `usage.html` | 用量统计页面（单文件，内联 CSS/JS） |
| `pricing.yaml` | 模型价格（USD/百万 tokens），极简 YAML，按模型名前缀匹配，未命中用 `default` |
| `service.sh` | 后台服务管理：start/stop/restart/status/logs（setsid+nohup，PID 写 `server.pid`，日志追加 `server.log`） |
| `images/` | 页面 logo，经 `/images/*` 路由提供 |
| `usage_data/<id>/` | 每服务一个子目录，内含 `usage-YYYY-MM-DD.csv`（gitignore，含真实历史数据，勿删） |
| `server.log` / `server.pid` | 运行时产物（gitignore） |

## 构建与运行

无构建、无测试框架、无 lint 配置。

```bash
python3 server.py                 # 前台运行
./service.sh start|stop|status    # 后台运行（推荐，当前服务通常已在后台跑）
```

- 看板地址：`http://127.0.0.1:8090/`（性能）与 `http://127.0.0.1:8090/usage.html`（用量）
- 监控的服务列表在 `services.yaml`（`id`/`name`/`metrics_url`）；文件缺失时
  回退单服务模式（id=`default`，URL 取 `INFERENCE_METRICS_URL`）
- 配置通过环境变量覆盖（`SERVER_PORT`、`INFERENCE_METRICS_URL`、`POLL_INTERVAL_MS`、
  `SERVICES_FILE` 等），见 `README.md` 配置表与 `server.py` 中 `Config.from_env()`
- 服务可能正在后台运行（`server.pid`），修改代码后需 `./service.sh restart` 才生效

## 验证方式

无单元测试。改动后手动验证：

```bash
./service.sh status
curl -fsS http://127.0.0.1:8090/api/services                   # 服务列表与健康
curl -fsS http://127.0.0.1:8090/api/health                     # 聚合健康（503 = 有上游不可达）
curl -fsS "http://127.0.0.1:8090/api/metrics?service=all" | head -c 500
curl -fsS "http://127.0.0.1:8090/api/metrics?service=antlserver01" | head -c 500
curl -fsS -D - "http://127.0.0.1:8090/api/metrics.csv?service=all" | head -20
curl -fsS http://127.0.0.1:8090/api/usage
python3 -m py_compile server.py usage_stats.py                 # 语法检查
python3 -m unittest discover -s tests -v                       # 标准库单元测试
node --check <提取的页面内联 JS>                                 # 前端语法（可选）
```

上游空闲时 tokens/s、延迟分位数显示 `--`（null）是预期行为，不是 bug。

## 后端关键设计（server.py）

- **指标别名**：`MetricsCollector.COUNTER_NAMES` / `HISTOGRAM_NAMES` 为新旧 vLLM
  指标名提供别名（如 `*_total` 与非 `_total`），缺失值一律为 `null`
- **速率计算**：`_counter_rates()` 在 `RATE_WINDOW_SECONDS`（默认 5s）窗口内，
  沿"连续单调递增"的抓取序列回溯取基线；抓取失败会清空历史，绝不跨断点计算，
  Counter 回退（服务重启）不产生负速率
- **多服务**：`ServiceRegistry` 持有每服务一个 `MetricsCollector`（config 用
  `dataclasses.replace` 派生：`metrics_url` + `usage_data_dir/<id>`），单线程
  轮询依次 `collect_once()`；`?service=<id|all>` 选视图，缺省 `all`
- **聚合口径**：计数/速率/累计求和（缺失按 0）；KV 取 max；前缀命中率按
  Σ窗口 Δ 重算；延迟分位数 = 各服务 `_latency_delta_histograms()` 差集分桶
  经 `_aggregate_histograms` 跨服务求和后 `histogram_quantile` 重算（分桶
  schema 不一致返回 None → `--`）；健康状态取最差、错误带服务 id 前缀
- **聚合历史**：registry 每轮追加 `_aggregate_history`（schema 同单服务
  `_display_history`），前端 60s 趋势图首次加载靠它填充
- **延迟分位数**：`_latency_delta_histograms()` 只对窗口内"新增"的 Histogram
  样本做分桶差集（结果缓存在 collector，`_record_error` 时清空防陈旧），
  `_latency_quantiles()` 插值（`histogram_quantile`），同一 non-le 标签序列才做差集
- **健康状态机**：`starting / online / degraded / stale / offline`，
  超过 `STALE_AFTER_SECONDS` 未成功抓取即 stale
- **HTTP 路由**（`make_handler`）：`/`、`/index.html`、`/usage.html`、
  `/api/metrics`、`/api/metrics.csv`、`/api/health`、`/api/services`、`/api/raw`（默认禁用且需
  具体服务，`ENABLE_RAW_API=true` 开启）、`/api/usage{,/history,/health}`、
  `/images/*`（有路径穿越防护）、`/favicon.ico`
- **线程模型**：registry 单采集线程（串行轮询所有服务）+ `ThreadingHTTPServer`
  （daemon 线程）；各 collector 状态读写走 `_state_lock`（RLock），抓取走
  `_collect_lock`；前端读后端内存快照（`snapshot()`），多开浏览器不放大抓取压力
- **安全头**：CSP（仅允许 inline script/style）、X-Frame-Options DENY、nosniff；
  无认证机制，`SERVER_HOST=0.0.0.0` 仅限可信网络

## 用量统计（usage_stats.py）

- 每 `USAGE_WRITE_INTERVAL_SECONDS`（默认 60s）把上一周期 Counter 增量 +
  按 `pricing.yaml` 计算的费用追加到 `usage_data/<id>/usage-YYYY-MM-DD.csv`
  （每服务一个子目录；该插件本身单服务无感知，子目录由 registry 传入）
- 累计值 = 全部历史 CSV 求和（重启后继续累计）；内存趋势窗口最多 3600 行，
  启动时从 CSV 回读最近 3600 行填充
- 聚合视图：`ServiceRegistry._aggregate_usage_payload()` 求和累计值、按
  60s 时间桶（`_merge_usage_history`）对齐合并各服务趋势行
- 价格匹配：精确名 → 小写前缀 → `default`（`_price_for`）
- CSV 列定义在 `PERIOD_FIELDS`，表头变化时自动改写 `-v2` 文件
- 停机期间无 CSV 行，趋势图对应缺失属预期

## 前端约定（index.html / usage.html）

- 单文件：HTML + `<style>` + `<script>` 全内联，禁止引入外部 CDN/库
- 纯 Canvas 2D 手绘图表（无图表库），devicePixelRatio 适配
- 服务下拉框（`#service-select`，选项来自 `/api/services`）+ 轮询间隔
  200/500/1000/2000ms 可选，均持久化到 localStorage；API 请求带
  `?service=<id|all>`
- 切换服务时清空本地趋势（`state.history` / `history`），由服务端历史重新填充
- UI 文案为中文；缺失值统一显示 `--`
- 改动前端时保持与现有 CSS 变量/类名风格一致

## 代码风格

- Python：类型注解齐全，docstring/注释用英文；模块级常量 UPPER_SNAKE
- 仅用标准库；不要引入 pip 依赖（`pricing.yaml` 用自写极简解析器，勿换 PyYAML）
- 单文件 HTML 内的 JS 沿用现有命名（camelCase 函数、`const $ = id => ...`）
