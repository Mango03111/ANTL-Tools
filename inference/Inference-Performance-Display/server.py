#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import math
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from usage_stats import UsageStatsPlugin

# ---------------------------------------------------------------------------
# Easily adjustable defaults. Environment variables override these values.
# ---------------------------------------------------------------------------
DEFAULT_METRICS_URL = "http://127.0.0.1:8000/metrics"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8090
DEFAULT_POLL_INTERVAL_MS = 500.0
DEFAULT_FETCH_TIMEOUT_SECONDS = 5.0
DEFAULT_RATE_WINDOW_SECONDS = 5.0
DEFAULT_LATENCY_WINDOW_SECONDS = 30.0
DEFAULT_ENABLE_RAW_API = False
DEFAULT_ENABLE_USAGE_STATS = True
DEFAULT_USAGE_WRITE_INTERVAL_SECONDS = 60.0
# Resolve the usage data/pricing defaults relative to this script so the
# server works no matter which directory it is started from.
_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_USAGE_DATA_DIR = "usage_data/"
DEFAULT_USAGE_PRICING_FILE = _BASE_DIR / "pricing.yaml"
DEFAULT_SERVICES_FILE = _BASE_DIR / "services.yaml"


_SAMPLE_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

SeriesKey = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True)
class Config:
    metrics_url: str
    host: str
    port: int
    poll_interval_seconds: float
    fetch_timeout_seconds: float
    rate_window_seconds: float
    latency_window_seconds: float
    stale_after_seconds: float
    enable_raw_api: bool
    enable_usage_stats: bool
    usage_write_interval_seconds: float
    usage_data_dir: Path
    usage_pricing_file: Path
    services_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        poll_ms = _env_float("POLL_INTERVAL_MS", DEFAULT_POLL_INTERVAL_MS, minimum=100.0, maximum=60_000.0)
        poll_seconds = poll_ms / 1000.0
        return cls(
            metrics_url=os.getenv(
                "INFERENCE_METRICS_URL",
                os.getenv("VLLM_METRICS_URL", DEFAULT_METRICS_URL),  # legacy alias
            ),
            host=os.getenv("SERVER_HOST", DEFAULT_SERVER_HOST),
            port=_env_int("SERVER_PORT", DEFAULT_SERVER_PORT, minimum=1, maximum=65_535),
            poll_interval_seconds=poll_seconds,
            fetch_timeout_seconds=_env_float(
                "FETCH_TIMEOUT_SECONDS", DEFAULT_FETCH_TIMEOUT_SECONDS, minimum=0.1, maximum=120.0
            ),
            rate_window_seconds=_env_float(
                "RATE_WINDOW_SECONDS", DEFAULT_RATE_WINDOW_SECONDS, minimum=poll_seconds, maximum=300.0
            ),
            latency_window_seconds=_env_float(
                "LATENCY_WINDOW_SECONDS", DEFAULT_LATENCY_WINDOW_SECONDS, minimum=poll_seconds, maximum=900.0
            ),
            stale_after_seconds=_env_float(
                "STALE_AFTER_SECONDS",
                max(3.0, poll_seconds * 4.0),
                minimum=poll_seconds,
                maximum=900.0,
            ),
            enable_raw_api=_env_bool("ENABLE_RAW_API", DEFAULT_ENABLE_RAW_API),
            enable_usage_stats=_env_bool("ENABLE_USAGE_STATS", DEFAULT_ENABLE_USAGE_STATS),
            usage_write_interval_seconds=_env_float("USAGE_WRITE_INTERVAL_SECONDS", DEFAULT_USAGE_WRITE_INTERVAL_SECONDS, minimum=1.0, maximum=86400.0),
            usage_data_dir=(_BASE_DIR / Path(os.getenv("USAGE_DATA_DIR", DEFAULT_USAGE_DATA_DIR))).resolve(),
            usage_pricing_file=Path(os.getenv("USAGE_PRICING_FILE", DEFAULT_USAGE_PRICING_FILE)),
            services_file=Path(os.getenv("SERVICES_FILE", DEFAULT_SERVICES_FILE)),
        )


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    name: str
    metrics_url: str


_SERVICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def load_services(path: Path) -> list[ServiceSpec]:
    """Parse the minimal services.yaml format: a `services:` list whose
    entries carry `id`, optional `name`, and `metrics_url` fields.
    """
    if not path.is_file():
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.rstrip(":") == "services":
            continue
        if line == "-" or line.startswith("- "):
            current = {}
            entries.append(current)
            line = line[1:].strip()
            if not line:
                continue
        if current is None:
            raise ValueError(f"unexpected line outside services entries: {line!r}")
        if ":" not in line:
            raise ValueError(f"cannot parse services entry line: {line!r}")
        key, value = (part.strip().strip("'\"") for part in line.split(":", 1))
        if not key:
            raise ValueError(f"cannot parse services entry line: {raw_line!r}")
        current[key] = value
    specs: list[ServiceSpec] = []
    seen_ids: set[str] = set()
    for entry in entries:
        service_id = entry.get("id", "")
        if not _SERVICE_ID_RE.fullmatch(service_id):
            raise ValueError(f"missing or invalid 'id' in services entry: {entry!r}")
        if service_id in seen_ids:
            raise ValueError(f"duplicate service id: {service_id!r}")
        seen_ids.add(service_id)
        metrics_url = entry.get("metrics_url", "")
        if not metrics_url.startswith(("http://", "https://")):
            raise ValueError(f"service {service_id!r}: invalid metrics_url: {metrics_url!r}")
        specs.append(
            ServiceSpec(
                id=service_id,
                name=entry.get("name") or service_id,
                metrics_url=metrics_url,
            )
        )
    return specs


def parse_prometheus_text(text: str) -> dict[str, list[Sample]]:
    """Parse the Prometheus text exposition format used by vLLM.

    The parser intentionally implements the subset required by ordinary sample
    lines while correctly handling escaped label values, scientific notation,
    infinities, and optional timestamps.
    """

    series_by_name: dict[str, dict[SeriesKey, Sample]] = defaultdict(dict)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, raw_labels, value = _parse_sample_line(line)
            labels = parse_labels(raw_labels)
        except ValueError:
            # One malformed or unrelated exporter line must not make the entire
            # vLLM scrape unavailable.
            continue
        sample = Sample(name=name, labels=labels, value=value)
        # Prometheus requires a series to occur at most once, but exporters can
        # occasionally violate that rule. Treat the last occurrence as freshest.
        series_by_name[name][_series_key(labels)] = sample
    return {name: list(series.values()) for name, series in series_by_name.items()}


def _parse_sample_line(line: str) -> tuple[str, str, float]:
    name_match = _SAMPLE_NAME_RE.match(line)
    if name_match is None:
        raise ValueError("invalid metric name")
    name = name_match.group(0)
    position = name_match.end()
    raw_labels = ""

    if position < len(line) and line[position] == "{":
        label_start = position + 1
        position = _find_label_set_end(line, position)
        raw_labels = line[label_start:position]
        position += 1

    if position >= len(line) or not line[position].isspace():
        raise ValueError("missing whitespace before sample value")
    while position < len(line) and line[position].isspace():
        position += 1
    value_start = position
    while position < len(line) and not line[position].isspace():
        position += 1
    if value_start == position:
        raise ValueError("missing sample value")
    value = float(line[value_start:position])

    remainder = line[position:].strip()
    if remainder and not remainder.startswith("#"):
        # Text exposition permits one optional timestamp before an OpenMetrics
        # exemplar. Exemplars themselves are intentionally ignored.
        timestamp, separator, tail = remainder.partition(" ")
        parsed_timestamp = float(timestamp)
        if not math.isfinite(parsed_timestamp):
            raise ValueError("invalid sample timestamp")
        remainder = tail.strip() if separator else ""
        if remainder and not remainder.startswith("#"):
            raise ValueError("unexpected sample suffix")
    return name, raw_labels, value


def _find_label_set_end(line: str, opening_position: int) -> int:
    in_quotes = False
    escaped = False
    for position in range(opening_position + 1, len(line)):
        char = line[position]
        if escaped:
            escaped = False
            continue
        if in_quotes and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == "}" and not in_quotes:
            return position
    raise ValueError("unterminated label set")


def _series_key(
    labels: Mapping[str, str], *, exclude: Iterable[str] = ()
) -> SeriesKey:
    excluded = frozenset(exclude)
    return tuple(
        sorted(
            (name, value)
            for name, value in labels.items()
            if name not in excluded
        )
    )


def parse_labels(text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    position = 0
    length = len(text)

    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break

        name_match = _LABEL_NAME_RE.match(text, position)
        if name_match is None:
            raise ValueError(f"invalid label name near {text[position:position + 20]!r}")
        name = name_match.group(0)
        position = name_match.end()

        while position < length and text[position].isspace():
            position += 1
        if position >= length or text[position] != "=":
            raise ValueError(f"missing '=' after label {name!r}")
        position += 1
        while position < length and text[position].isspace():
            position += 1
        if position >= length or text[position] != '"':
            raise ValueError(f"missing quoted value for label {name!r}")
        position += 1

        value_chars: list[str] = []
        while position < length:
            char = text[position]
            position += 1
            if char == '"':
                break
            if char == "\\":
                if position >= length:
                    raise ValueError(f"unterminated escape in label {name!r}")
                escaped = text[position]
                position += 1
                value_chars.append({"n": "\n", "\\": "\\", '"': '"'}.get(escaped, escaped))
            else:
                value_chars.append(char)
        else:
            raise ValueError(f"unterminated quoted value for label {name!r}")

        if name in labels:
            raise ValueError(f"duplicate label {name!r}")
        labels[name] = "".join(value_chars)
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        if text[position] != ",":
            raise ValueError(f"expected ',' after label {name!r}")
        position += 1

    return labels


def metric_sum(
    metrics: Mapping[str, Sequence[Sample]],
    names: Iterable[str],
    predicate: Callable[[Sample], bool] | None = None,
) -> float | None:
    for name in names:
        samples = metrics.get(name)
        if not samples:
            continue
        values = [
            sample.value
            for sample in samples
            if math.isfinite(sample.value) and (predicate is None or predicate(sample))
        ]
        if values:
            return sum(values)
    return None


def metric_series(
    metrics: Mapping[str, Sequence[Sample]], names: Iterable[str]
) -> dict[SeriesKey, float] | None:
    """Return the first available metric alias keyed by its complete label set."""

    for name in names:
        samples = metrics.get(name)
        if not samples:
            continue
        series = {
            _series_key(sample.labels): sample.value
            for sample in samples
            if math.isfinite(sample.value)
        }
        if series:
            return series
    return None


def metric_max(
    metrics: Mapping[str, Sequence[Sample]], names: Iterable[str]
) -> float | None:
    for name in names:
        samples = metrics.get(name)
        if not samples:
            continue
        values = [sample.value for sample in samples if math.isfinite(sample.value)]
        if values:
            return max(values)
    return None


def histogram_buckets(
    metrics: Mapping[str, Sequence[Sample]], base_names: Iterable[str]
) -> dict[float, float] | None:
    series = histogram_series(metrics, base_names)
    if series is None:
        return None
    valid_buckets = [
        buckets for buckets in series.values() if _valid_histogram_buckets(buckets)
    ]
    return _aggregate_histograms(valid_buckets)


def histogram_series(
    metrics: Mapping[str, Sequence[Sample]], base_names: Iterable[str]
) -> dict[SeriesKey, dict[float, float]] | None:
    """Group histogram buckets by every label except the le label."""

    for base_name in base_names:
        samples = metrics.get(f"{base_name}_bucket")
        if not samples:
            continue
        grouped: dict[SeriesKey, dict[float, float]] = defaultdict(dict)
        for sample in samples:
            raw_bound = sample.labels.get("le")
            if raw_bound is None or not math.isfinite(sample.value):
                continue
            try:
                bound = float(raw_bound)
            except ValueError:
                continue
            if math.isnan(bound) or bound == -math.inf:
                continue
            grouped[_series_key(sample.labels, exclude=("le",))][bound] = sample.value
        if grouped:
            return {series_key: dict(buckets) for series_key, buckets in grouped.items()}
    return None


def histogram_quantile(quantile: float, cumulative_buckets: Mapping[float, float]) -> float | None:
    if (
        not 0.0 <= quantile <= 1.0
        or not _valid_histogram_buckets(cumulative_buckets)
    ):
        return None

    ordered = sorted(cumulative_buckets.items(), key=lambda item: item[0])
    total = cumulative_buckets[math.inf]
    if total <= 0:
        return None

    rank = quantile * total
    previous_bound = 0.0
    previous_count = 0.0
    for upper_bound, raw_count in ordered:
        count = max(previous_count, raw_count)
        if count >= rank:
            if math.isinf(upper_bound):
                return previous_bound
            bucket_count = count - previous_count
            if bucket_count <= 0:
                return upper_bound
            fraction = max(0.0, min(1.0, (rank - previous_count) / bucket_count))
            return previous_bound + (upper_bound - previous_bound) * fraction
        previous_bound = upper_bound
        previous_count = count
    return None


class MetricsCollector:
    COUNTER_NAMES: Mapping[str, tuple[str, ...]] = {
        "prompt_tokens": ("vllm:prompt_tokens_total", "vllm:prompt_tokens"),
        "generation_tokens": (
            "vllm:generation_tokens_total",
            "vllm:generation_tokens",
        ),
        "requests": ("vllm:request_success_total", "vllm:request_success"),
        "prefix_queries": (
            "vllm:prefix_cache_queries_total",
            "vllm:prefix_cache_queries",
        ),
        "prefix_hits": (
            "vllm:prefix_cache_hits_total",
            "vllm:prefix_cache_hits",
        ),
        "preemptions": ("vllm:num_preemptions_total", "vllm:num_preemptions"),
    }

    HISTOGRAM_NAMES: Mapping[str, tuple[str, ...]] = {
        "e2e": ("vllm:e2e_request_latency_seconds",),
        "ttft": ("vllm:time_to_first_token_seconds",),
        "itl": ("vllm:inter_token_latency_seconds",),
        "tpot": (
            "vllm:request_time_per_output_token_seconds",
            "vllm:time_per_output_token_seconds",
        ),
        "queue": ("vllm:request_queue_time_seconds",),
    }

    def __init__(self, config: Config):
        self.config = config
        self._state_lock = threading.RLock()
        self._collect_lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._counter_history: deque[
            tuple[float, dict[str, dict[SeriesKey, float]]]
        ] = deque()
        self._histogram_history: deque[
            tuple[
                float,
                dict[str, dict[SeriesKey, dict[float, float]]],
            ]
        ] = deque()
        self._payload = self._empty_payload()
        self._raw_metrics = ""
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._display_history: deque[dict[str, object]] = deque(maxlen=720)
        self._last_latency_delta_histograms: dict[str, dict[float, float] | None] = {}
        self.usage = UsageStatsPlugin(config.usage_data_dir, config.usage_pricing_file, config.usage_write_interval_seconds) if config.enable_usage_stats else None

    def start(self) -> None:
        if self.usage:
            self.usage.start()

    def stop(self) -> None:
        if self.usage:
            self.usage.stop()

    def collect_once(self) -> dict[str, object]:
        with self._collect_lock:
            attempted_at = time.time()
            with self._state_lock:
                self._last_attempt_at = attempted_at
            try:
                raw_metrics = self._fetch_metrics()
                parsed = parse_prometheus_text(raw_metrics)
                if not any(name.startswith("vllm:") for name in parsed):
                    raise ValueError("metrics response contains no vllm:* samples")
                payload, delta_histograms = self._build_payload(parsed, attempted_at)
            except Exception as exc:  # A failed scrape must not stop the server.
                self._record_error(exc)
                return self.snapshot()

            with self._state_lock:
                self._payload = payload
                self._last_latency_delta_histograms = delta_histograms
                self._raw_metrics = raw_metrics
                self._last_success_at = attempted_at
                self._last_error = None
                self._consecutive_failures = 0
                self._display_history.append({
                    "timestamp": attempted_at,
                    "generation": payload["tokens"]["generation_tokens_per_sec"],
                    "prompt": payload["tokens"]["prompt_tokens_per_sec"],
                    "running": payload["requests"]["running"],
                    "waiting": payload["requests"]["waiting"],
                    "kv": payload["cache"]["kv_cache_usage"],
                    "e2e_p50": payload["latency"]["e2e_p50"],
                    "e2e_p90": payload["latency"]["e2e_p90"],
                    "e2e_p99": payload["latency"]["e2e_p99"],
                })
            if self.usage:
                self.usage.update(self.snapshot())
            return self.snapshot()

    def _fetch_metrics(self) -> str:
        request = urllib.request.Request(
            self.config.metrics_url,
            headers={
                "Accept": "text/plain; version=0.0.4, text/plain;q=0.9",
                "User-Agent": "vllm-performance-display/1.0",
            },
        )
        try:
            with self._opener.open(
                request, timeout=self.config.fetch_timeout_seconds
            ) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"metrics endpoint returned HTTP {status}")
                raw = response.read(10 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"metrics endpoint returned HTTP {exc.code}") from exc
        if len(raw) > 10 * 1024 * 1024:
            raise RuntimeError("metrics response exceeds 10 MiB safety limit")
        return raw.decode("utf-8", errors="replace")

    def _record_error(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        # Never bridge a rate or latency window across a failed scrape: series
        # may have reset, disappeared, or changed schema while observations were
        # unavailable.
        self._counter_history.clear()
        self._histogram_history.clear()
        with self._state_lock:
            self._last_latency_delta_histograms = {}
            self._last_error = message[:500]
            self._consecutive_failures += 1

    def _build_payload(
        self, metrics: Mapping[str, Sequence[Sample]], sampled_at: float
    ) -> tuple[dict[str, object], dict[str, dict[float, float] | None]]:
        monotonic_now = time.monotonic()
        counter_series = {
            key: series
            for key, aliases in self.COUNTER_NAMES.items()
            if (series := metric_series(metrics, aliases)) is not None
        }
        counters = {
            key: sum(series.values()) for key, series in counter_series.items()
        }
        self._counter_history.append((monotonic_now, counter_series))
        self._prune_history(self._counter_history, self.config.rate_window_seconds)
        rates, deltas = self._counter_rates()

        histograms = {
            key: series
            for key, aliases in self.HISTOGRAM_NAMES.items()
            if (series := histogram_series(metrics, aliases)) is not None
        }
        self._histogram_history.append((monotonic_now, histograms))
        self._prune_history(
            self._histogram_history, self.config.latency_window_seconds
        )
        delta_histograms = self._latency_delta_histograms()
        latency = self._latency_quantiles(delta_histograms)

        model_names = sorted(
            {
                sample.labels["model_name"]
                for samples in metrics.values()
                for sample in samples
                if sample.labels.get("model_name")
            }
        )
        model = model_names[0] if len(model_names) == 1 else (
            ", ".join(model_names) if model_names else "unknown"
        )

        prefix_queries_delta = deltas.get("prefix_queries")
        prefix_hits_delta = deltas.get("prefix_hits")
        prefix_rate = None
        if (
            prefix_queries_delta is not None
            and prefix_hits_delta is not None
            and prefix_queries_delta > 0
            and 0 <= prefix_hits_delta <= prefix_queries_delta
        ):
            prefix_rate = prefix_hits_delta / prefix_queries_delta

        running = metric_sum(metrics, ("vllm:num_requests_running",))
        waiting = metric_sum(metrics, ("vllm:num_requests_waiting",))
        waiting_capacity = metric_sum(
            metrics,
            ("vllm:num_requests_waiting_by_reason",),
            lambda sample: sample.labels.get("reason") == "capacity",
        )
        waiting_deferred = metric_sum(
            metrics,
            ("vllm:num_requests_waiting_by_reason",),
            lambda sample: sample.labels.get("reason") == "deferred",
        )

        return {
            "api_version": 1,
            "ok": True,
            "timestamp": sampled_at,
            "source": self.config.metrics_url,
            "poll_interval_ms": round(self.config.poll_interval_seconds * 1000),
            "rate_window_seconds": self.config.rate_window_seconds,
            "latency_window_seconds": self.config.latency_window_seconds,
            "model": model,
            "models": model_names,
            "requests": {
                "running": running,
                "waiting": waiting,
                "waiting_capacity": waiting_capacity,
                "waiting_deferred": waiting_deferred,
                # Current vLLM v0.22.1 omits this metric; absence remains null.
                "swapped": metric_sum(metrics, ("vllm:num_requests_swapped",)),
                "requests_per_sec": rates.get("requests"),
                "completed_total": counters.get("requests"),
                "preemptions_total": counters.get("preemptions"),
            },
            "tokens": {
                "prompt_tokens_per_sec": rates.get("prompt_tokens"),
                "generation_tokens_per_sec": rates.get("generation_tokens"),
                "prompt_tokens_total": counters.get("prompt_tokens"),
                "generation_tokens_total": counters.get("generation_tokens"),
            },
            "cache": {
                "kv_cache_usage": metric_max(
                    metrics,
                    ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
                ),
                "prefix_cache_hit_rate": prefix_rate,
                "prefix_queries_delta": prefix_queries_delta,
                "prefix_hits_delta": prefix_hits_delta,
                "prefix_cache_queries_total": counters.get("prefix_queries"),
                "prefix_cache_hits_total": counters.get("prefix_hits"),
            },
            "latency": latency,
            "health": {},
            "diagnostics": {
                "metric_families": len(metrics),
                "samples": sum(len(samples) for samples in metrics.values()),
            },
        }, delta_histograms

    @staticmethod
    def _prune_history(history: deque, window_seconds: float) -> None:
        if len(history) < 3:
            return
        cutoff = history[-1][0] - window_seconds
        # Retain one sample immediately before the cutoff as the baseline.
        while len(history) > 2 and history[1][0] <= cutoff:
            history.popleft()

    def _counter_rates(self) -> tuple[dict[str, float], dict[str, float]]:
        rates: dict[str, float] = {}
        deltas: dict[str, float] = {}
        if len(self._counter_history) < 2:
            return rates, deltas

        current_time, current = self._counter_history[-1]
        previous_entries = list(self._counter_history)[:-1]
        for key, current_series in current.items():
            total_delta = 0.0
            total_rate = 0.0
            matched_series = False
            for series_key, current_value in current_series.items():
                baseline: tuple[float, float] | None = None
                later_value = current_value
                # Walk backward only through a contiguous monotonic run. This
                # prevents a reset that later surpassed its old value from being
                # mistaken for a valid long-window increase.
                for timestamp, values in reversed(previous_entries):
                    old_series = values.get(key)
                    if old_series is None or series_key not in old_series:
                        break
                    old_value = old_series[series_key]
                    if old_value > later_value:
                        break
                    baseline = (timestamp, old_value)
                    later_value = old_value
                if baseline is None:
                    continue
                elapsed = current_time - baseline[0]
                if elapsed <= 0:
                    continue
                delta = current_value - baseline[1]
                total_delta += delta
                total_rate += delta / elapsed
                matched_series = True
            if matched_series:
                deltas[key] = total_delta
                rates[key] = total_rate
        return rates, deltas

    def _latency_delta_histograms(self) -> dict[str, dict[float, float] | None]:
        """Windowed new-sample bucket deltas per histogram metric.

        One entry per metric present in the latest scrape; the value is None
        when no usable windowed samples exist for that metric.
        """
        deltas: dict[str, dict[float, float] | None] = {}
        if len(self._histogram_history) < 2:
            return deltas

        _, current = self._histogram_history[-1]
        previous_entries = list(self._histogram_history)[:-1]
        for name, current_series in current.items():
            series_deltas: list[dict[float, float]] = []
            for series_key, current_buckets in current_series.items():
                if not _valid_histogram_buckets(current_buckets):
                    continue
                baseline_delta: dict[float, float] | None = None
                later_buckets = current_buckets
                # As with counters, only bridge a contiguous, compatible,
                # non-reset sequence for this exact non-le label set.
                for _, previous in reversed(previous_entries):
                    old_series = previous.get(name)
                    if old_series is None or series_key not in old_series:
                        break
                    old_buckets = old_series[series_key]
                    if _histogram_delta(later_buckets, old_buckets) is None:
                        break
                    candidate = _histogram_delta(current_buckets, old_buckets)
                    if candidate is None:
                        break
                    baseline_delta = candidate
                    later_buckets = old_buckets
                if baseline_delta is not None:
                    series_deltas.append(baseline_delta)
            deltas[name] = _aggregate_histograms(series_deltas)
        return deltas

    @staticmethod
    def _latency_quantiles(
        delta_histograms: Mapping[str, dict[float, float] | None]
    ) -> dict[str, float | int | None]:
        result: dict[str, float | int | None] = {
            "e2e_p50": None,
            "e2e_p90": None,
            "e2e_p99": None,
            "e2e_samples": 0,
            "ttft_p50": None,
            "ttft_p90": None,
            "ttft_p99": None,
            "ttft_samples": 0,
            "itl_p50": None,
            "itl_p90": None,
            "itl_p99": None,
            "itl_samples": 0,
            "tpot_p50": None,
            "tpot_p90": None,
            "tpot_p99": None,
            "tpot_samples": 0,
            "queue_p50": None,
            "queue_p90": None,
            "queue_p99": None,
            "queue_samples": 0,
        }
        for name, delta_buckets in delta_histograms.items():
            if delta_buckets is None:
                continue
            sample_count = delta_buckets[math.inf]
            result[f"{name}_samples"] = int(max(0.0, sample_count))
            if sample_count <= 0:
                continue
            for label, quantile in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
                result[f"{name}_{label}"] = histogram_quantile(
                    quantile, delta_buckets
                )
        return result

    def _latency_values(self) -> dict[str, float | int | None]:
        return self._latency_quantiles(self._latency_delta_histograms())

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            payload = copy.deepcopy(self._payload)
            last_attempt_at = self._last_attempt_at
            last_success_at = self._last_success_at
            last_error = self._last_error
            failures = self._consecutive_failures

        now = time.time()
        age = None if last_success_at is None else max(0.0, now - last_success_at)
        stale = age is None or age > self.config.stale_after_seconds
        ok = last_success_at is not None and last_error is None and not stale
        if last_success_at is None:
            status = "starting" if last_error is None else "offline"
        elif stale:
            status = "stale"
        elif last_error is not None:
            status = "degraded"
        else:
            status = "online"

        payload["ok"] = ok
        payload["health"] = {
            "status": status,
            "last_error": last_error,
            "last_attempt_at": last_attempt_at,
            "last_success_at": last_success_at,
            "sample_age_seconds": age,
            "stale": stale,
            "stale_after_seconds": self.config.stale_after_seconds,
            "consecutive_failures": failures,
        }
        payload["history"] = list(self._display_history)
        return payload

    def raw_metrics(self) -> str:
        with self._state_lock:
            return self._raw_metrics

    def latency_delta_histograms(self) -> dict[str, dict[float, float] | None]:
        with self._state_lock:
            return {
                name: dict(buckets) if buckets is not None else None
                for name, buckets in self._last_latency_delta_histograms.items()
            }

    def _empty_payload(self) -> dict[str, object]:
        return {
            "api_version": 1,
            "ok": False,
            "timestamp": None,
            "source": self.config.metrics_url,
            "poll_interval_ms": round(self.config.poll_interval_seconds * 1000),
            "rate_window_seconds": self.config.rate_window_seconds,
            "latency_window_seconds": self.config.latency_window_seconds,
            "model": "unknown",
            "models": [],
            "requests": {
                "running": None,
                "waiting": None,
                "waiting_capacity": None,
                "waiting_deferred": None,
                "swapped": None,
                "requests_per_sec": None,
                "completed_total": None,
                "preemptions_total": None,
            },
            "tokens": {
                "prompt_tokens_per_sec": None,
                "generation_tokens_per_sec": None,
                "prompt_tokens_total": None,
                "generation_tokens_total": None,
            },
            "cache": {
                "kv_cache_usage": None,
                "prefix_cache_hit_rate": None,
                "prefix_queries_delta": None,
                "prefix_hits_delta": None,
                "prefix_cache_queries_total": None,
                "prefix_cache_hits_total": None,
            },
            "latency": self._latency_values(),
            "health": {},
            "diagnostics": {"metric_families": 0, "samples": 0},
        }


_STATUS_RANK = {"online": 0, "degraded": 1, "starting": 1, "stale": 2, "offline": 3}
_LATENCY_METRIC_NAMES = ("e2e", "ttft", "itl", "tpot", "queue")
_USAGE_TOTAL_KEYS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "input_cost",
    "output_cost",
    "total_cost",
)


class ServiceRegistry:
    """Owns one MetricsCollector per configured service plus the shared poll loop.

    A ``service`` argument of ``None``, ``""`` or ``"all"`` selects the
    aggregated view across every configured service.
    """

    def __init__(self, config: Config, services: Sequence[ServiceSpec]):
        self.config = config
        self.services: list[ServiceSpec] = list(services)
        self.collectors: dict[str, MetricsCollector] = {}
        for spec in self.services:
            service_config = replace(
                config,
                metrics_url=spec.metrics_url,
                usage_data_dir=config.usage_data_dir / spec.id,
            )
            self.collectors[spec.id] = MetricsCollector(service_config)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._aggregate_history: deque[dict[str, object]] = deque(maxlen=720)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        for collector in self.collectors.values():
            collector.start()
        self._thread = threading.Thread(
            target=self._run, name="vllm-metrics-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(
                timeout=self.config.fetch_timeout_seconds * max(1, len(self.services)) + 1.0
            )
        for collector in self.collectors.values():
            collector.stop()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            for collector in self.collectors.values():
                if self._stop_event.is_set():
                    break
                collector.collect_once()
            self._append_aggregate_point()
            elapsed = time.monotonic() - started
            self._stop_event.wait(max(0.0, self.config.poll_interval_seconds - elapsed))

    # -- lookups ----------------------------------------------------------

    def _resolve(self, service: str) -> MetricsCollector:
        collector = self.collectors.get(service)
        if collector is None:
            raise KeyError(service)
        return collector

    def metrics_payload(self, service: str | None) -> dict[str, object]:
        if service in (None, "", "all"):
            return self._aggregate_metrics_payload()
        return self._resolve(service).snapshot()

    def usage_payload(self, service: str | None) -> dict[str, object]:
        if service in (None, "", "all"):
            return self._aggregate_usage_payload()
        collector = self._resolve(service)
        if collector.usage is None:
            return {"ok": False, "error": "usage statistics are disabled"}
        return collector.usage.api_payload()

    def raw_metrics(self, service: str) -> str:
        return self._resolve(service).raw_metrics()

    def services_info(self) -> list[dict[str, object]]:
        info: list[dict[str, object]] = []
        for spec in self.services:
            snapshot = self.collectors[spec.id].snapshot()
            info.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "url": spec.metrics_url,
                    "status": snapshot["health"]["status"],
                    "ok": bool(snapshot["ok"]),
                }
            )
        return info

    # -- aggregation -------------------------------------------------------

    @staticmethod
    def _sum_values(values: Iterable[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    @staticmethod
    def _max_value(values: Iterable[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return max(present) if present else None

    def _aggregate_latency(self) -> dict[str, float | int | None]:
        per_service = [
            collector.latency_delta_histograms() for collector in self.collectors.values()
        ]
        result: dict[str, float | int | None] = {}
        for name in _LATENCY_METRIC_NAMES:
            merged = _aggregate_histograms(
                [deltas[name] for deltas in per_service if deltas.get(name) is not None]
            )
            if merged is None or merged[math.inf] <= 0:
                result[f"{name}_samples"] = 0
                result[f"{name}_p50"] = None
                result[f"{name}_p90"] = None
                result[f"{name}_p99"] = None
                continue
            sample_count = merged[math.inf]
            result[f"{name}_samples"] = int(max(0.0, sample_count))
            for label, quantile in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
                result[f"{name}_{label}"] = histogram_quantile(quantile, merged)
        return result

    def _aggregate_metrics_payload(self) -> dict[str, object]:
        snapshots = [collector.snapshot() for collector in self.collectors.values()]

        models: list[str] = []
        for snapshot in snapshots:
            for model in snapshot.get("models", []):
                if model not in models:
                    models.append(model)

        def total(section: str, key: str) -> float | None:
            return self._sum_values([snapshot[section][key] for snapshot in snapshots])

        requests_payload = {
            key: total("requests", key)
            for key in (
                "running",
                "waiting",
                "waiting_capacity",
                "waiting_deferred",
                "swapped",
                "requests_per_sec",
                "completed_total",
                "preemptions_total",
            )
        }
        tokens_payload = {
            key: total("tokens", key)
            for key in (
                "prompt_tokens_per_sec",
                "generation_tokens_per_sec",
                "prompt_tokens_total",
                "generation_tokens_total",
            )
        }

        queries_delta = total("cache", "prefix_queries_delta")
        hits_delta = total("cache", "prefix_hits_delta")
        prefix_rate = None
        if (
            queries_delta is not None
            and hits_delta is not None
            and queries_delta > 0
            and 0 <= hits_delta <= queries_delta
        ):
            prefix_rate = hits_delta / queries_delta

        health_sources = [snapshot["health"] for snapshot in snapshots]
        status = max(
            (str(health.get("status", "offline")) for health in health_sources),
            key=lambda name: _STATUS_RANK.get(name, 3),
        )
        errors = [
            f"{spec.id}: {health['last_error']}"
            for spec, health in zip(self.services, health_sources)
            if health.get("last_error")
        ]
        successes = [
            health["last_success_at"]
            for health in health_sources
            if health.get("last_success_at") is not None
        ]
        last_success_at = min(successes) if successes else None
        timestamps = [
            snapshot["timestamp"] for snapshot in snapshots if snapshot.get("timestamp") is not None
        ]
        now = time.time()
        sample_age = None if last_success_at is None else max(0.0, now - last_success_at)
        stale = last_success_at is None or sample_age > self.config.stale_after_seconds
        ok = all(snapshot["ok"] for snapshot in snapshots) and not stale

        return {
            "api_version": snapshots[0]["api_version"],
            "ok": ok,
            "timestamp": min(timestamps) if timestamps else None,
            "source": ", ".join(spec.metrics_url for spec in self.services),
            "poll_interval_ms": snapshots[0]["poll_interval_ms"],
            "rate_window_seconds": snapshots[0]["rate_window_seconds"],
            "latency_window_seconds": snapshots[0]["latency_window_seconds"],
            "model": ", ".join(models) if models else "unknown",
            "models": models,
            "requests": requests_payload,
            "tokens": tokens_payload,
            "cache": {
                "kv_cache_usage": self._max_value(
                    [snapshot["cache"]["kv_cache_usage"] for snapshot in snapshots]
                ),
                "prefix_cache_hit_rate": prefix_rate,
                "prefix_queries_delta": queries_delta,
                "prefix_hits_delta": hits_delta,
                "prefix_cache_queries_total": total("cache", "prefix_cache_queries_total"),
                "prefix_cache_hits_total": total("cache", "prefix_cache_hits_total"),
            },
            "latency": self._aggregate_latency(),
            "health": {
                "status": status,
                "last_error": "; ".join(errors) or None,
                "last_attempt_at": max(
                    (
                        health["last_attempt_at"]
                        for health in health_sources
                        if health.get("last_attempt_at") is not None
                    ),
                    default=None,
                ),
                "last_success_at": last_success_at,
                "sample_age_seconds": sample_age,
                "stale": stale,
                "stale_after_seconds": self.config.stale_after_seconds,
                "consecutive_failures": max(
                    (int(health.get("consecutive_failures") or 0) for health in health_sources),
                    default=0,
                ),
            },
            "diagnostics": {
                "metric_families": sum(
                    int(snapshot.get("diagnostics", {}).get("metric_families") or 0)
                    for snapshot in snapshots
                ),
                "samples": sum(
                    int(snapshot.get("diagnostics", {}).get("samples") or 0)
                    for snapshot in snapshots
                ),
            },
            "history": list(self._aggregate_history),
        }

    def _append_aggregate_point(self) -> None:
        snapshots = [collector.snapshot() for collector in self.collectors.values()]
        if not all(snapshot["ok"] for snapshot in snapshots):
            return
        latency = self._aggregate_latency()
        self._aggregate_history.append(
            {
                "timestamp": time.time(),
                "generation": self._sum_values(
                    [s["tokens"]["generation_tokens_per_sec"] for s in snapshots]
                ),
                "prompt": self._sum_values(
                    [s["tokens"]["prompt_tokens_per_sec"] for s in snapshots]
                ),
                "running": self._sum_values([s["requests"]["running"] for s in snapshots]),
                "waiting": self._sum_values([s["requests"]["waiting"] for s in snapshots]),
                "kv": self._max_value(
                    [s["cache"]["kv_cache_usage"] for s in snapshots]
                ),
                "e2e_p50": latency["e2e_p50"],
                "e2e_p90": latency["e2e_p90"],
                "e2e_p99": latency["e2e_p99"],
            }
        )

    def _aggregate_usage_payload(self) -> dict[str, object]:
        entries = [
            (spec.id, self.collectors[spec.id].usage.api_payload())
            for spec in self.services
            if self.collectors[spec.id].usage is not None
        ]
        if not entries:
            return {"ok": False, "error": "usage statistics are disabled"}

        totals = {key: 0.0 for key in _USAGE_TOTAL_KEYS}
        period_totals = {key: 0.0 for key in _USAGE_TOTAL_KEYS}
        models: list[str] = []
        last_write_at: float | None = None
        max_duration = 0.0
        errors: list[str] = []
        first_usage = entries[0][1]["usage"]
        for service_id, payload in entries:
            usage = payload["usage"]
            period = usage.get("current_period") or {}
            for key in totals:
                totals[key] += float(usage.get(key) or 0.0)
                period_totals[key] += float(period.get(key) or 0.0)
            max_duration = max(max_duration, float(period.get("duration_seconds") or 0.0))
            model = str(usage.get("model") or "")
            if model and model != "unknown" and model not in models:
                models.append(model)
            last_write = usage.get("last_write_at")
            if last_write is not None:
                last_write_at = max(last_write_at or 0.0, float(last_write))
            if usage.get("last_error"):
                errors.append(f"{service_id}: {usage['last_error']}")

        model_display = ", ".join(models) or "unknown"
        price_fields = {
            "input_price_per_1m_tokens": float(
                first_usage.get("input_price_per_1m_tokens") or 0.0
            ),
            "output_price_per_1m_tokens": float(
                first_usage.get("output_price_per_1m_tokens") or 0.0
            ),
            "currency": first_usage.get("currency", "USD"),
        }
        return {
            "ok": all(payload["ok"] for _, payload in entries),
            "usage": {
                **totals,
                "model": model_display,
                **price_fields,
                "current_period": {
                    **period_totals,
                    "duration_seconds": max_duration,
                    "model": model_display,
                    **price_fields,
                },
                "updated_at": time.time(),
                "data_dir": str(self.config.usage_data_dir),
                "last_write_at": last_write_at,
                "last_error": "; ".join(errors) or None,
            },
            "history": self._merge_usage_history(
                [payload["history"] for _, payload in entries]
            ),
        }

    @classmethod
    def _merge_usage_history(
        cls, histories: Sequence[Sequence[Mapping[str, object]]]
    ) -> list[dict[str, object]]:
        """Merge per-service 1-minute rows into one row per 60 s time bucket."""
        merged: dict[int, dict[str, object]] = {}
        for history in histories:
            for row in history:
                timestamp = row.get("timestamp")
                if not isinstance(timestamp, (int, float)):
                    continue
                bucket = int(timestamp // 60)
                entry = merged.get(bucket)
                if entry is None:
                    merged[bucket] = dict(row)
                    continue
                for key in _USAGE_TOTAL_KEYS:
                    entry[key] = float(entry.get(key) or 0.0) + float(row.get(key) or 0.0)
                entry["period_start"] = min(
                    str(entry.get("period_start") or ""), str(row.get("period_start") or "")
                )
                entry["period_end"] = max(
                    str(entry.get("period_end") or ""), str(row.get("period_end") or "")
                )
                entry["duration_seconds"] = max(
                    float(entry.get("duration_seconds") or 0.0),
                    float(row.get("duration_seconds") or 0.0),
                )
                if str(entry.get("model") or "") in ("", "unknown") and str(
                    row.get("model") or ""
                ) not in ("", "unknown"):
                    entry["model"] = row.get("model")
        return [merged[bucket] for bucket in sorted(merged)]


_HISTOGRAM_EPSILON = 1e-9


def _valid_histogram_buckets(buckets: Mapping[float, float]) -> bool:
    if not buckets or math.inf not in buckets:
        return False
    previous_count = 0.0
    for bound, count in sorted(buckets.items()):
        if math.isnan(bound) or bound == -math.inf:
            return False
        if not math.isfinite(count) or count < 0:
            return False
        if count + _HISTOGRAM_EPSILON < previous_count:
            return False
        previous_count = max(previous_count, count)
    return True


def _aggregate_histograms(
    histograms: Iterable[Mapping[float, float]],
) -> dict[float, float] | None:
    items = list(histograms)
    if not items:
        return None
    schema = set(items[0])
    if not _valid_histogram_buckets(items[0]):
        return None
    aggregate = {bound: 0.0 for bound in schema}
    for buckets in items:
        if set(buckets) != schema or not _valid_histogram_buckets(buckets):
            return None
        for bound, count in buckets.items():
            aggregate[bound] += count
    return aggregate if _valid_histogram_buckets(aggregate) else None


def _histogram_delta(
    current: Mapping[float, float], previous: Mapping[float, float]
) -> dict[float, float] | None:
    if (
        set(current) != set(previous)
        or not _valid_histogram_buckets(current)
        or not _valid_histogram_buckets(previous)
    ):
        return None

    delta: dict[float, float] = {}
    last_count = 0.0
    for bound in sorted(current):
        value = current[bound] - previous.get(bound, 0.0)
        if value < -_HISTOGRAM_EPSILON:
            return None
        value = max(0.0, value)
        if value + _HISTOGRAM_EPSILON < last_count:
            return None
        # Only normalize insignificant floating point drift; genuine
        # non-monotonic deltas are rejected above.
        value = max(last_count, value)
        delta[bound] = value
        last_count = value
    return delta


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    registry: ServiceRegistry, index_path: Path
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "InferencePerformanceDisplay/1.0"

        def _service_param(self) -> str | None:
            query = urllib.parse.urlsplit(self.path).query
            value = urllib.parse.parse_qs(query).get("service", [None])[0]
            if value is None or value == "" or value == "all":
                return None
            return value

        def _unknown_service(self, service: str | None) -> None:
            self._send_json(
                {"ok": False, "error": f"unknown service: {service}"}, status=404
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urllib.parse.urlsplit(self.path).path
            service = self._service_param()
            if path == "/api/metrics":
                try:
                    self._send_json(registry.metrics_payload(service))
                except KeyError:
                    self._unknown_service(service)
                return
            if path == "/api/health":
                try:
                    payload = registry.metrics_payload(service)
                except KeyError:
                    self._unknown_service(service)
                    return
                status = 200 if payload["ok"] else 503
                self._send_json(
                    {
                        "api_version": payload["api_version"],
                        "ok": payload["ok"],
                        "timestamp": payload["timestamp"],
                        "health": payload["health"],
                    },
                    status=status,
                )
                return
            if path == "/api/raw":
                if not registry.config.enable_raw_api:
                    self._send_json(
                        {"ok": False, "error": "raw API is disabled"}, status=404
                    )
                    return
                if service is None:
                    self._send_json(
                        {"ok": False, "error": "raw API requires a specific service"},
                        status=400,
                    )
                    return
                try:
                    raw = registry.raw_metrics(service)
                except KeyError:
                    self._unknown_service(service)
                    return
                self._send_bytes(
                    raw.encode("utf-8"),
                    "text/plain; charset=utf-8",
                    cache_control="no-store",
                )
                return
            if path == "/api/services":
                self._send_json({"ok": True, "services": registry.services_info()})
                return
            if path in {"/api/usage", "/api/usage/history", "/api/usage/health"}:
                try:
                    usage_payload = registry.usage_payload(service)
                except KeyError:
                    self._unknown_service(service)
                    return
                if "error" in usage_payload:
                    self._send_json(usage_payload, status=404)
                    return
                self._send_json(usage_payload if path == "/api/usage" else ({"ok": usage_payload["ok"], "history": usage_payload["history"]} if path.endswith("/history") else {"ok": usage_payload["ok"], "usage": {"last_error": usage_payload["usage"]["last_error"], "last_write_at": usage_payload["usage"]["last_write_at"]}}))
                return
            if path in {"/", "/index.html"}:
                try:
                    body = index_path.read_bytes()
                except OSError as exc:
                    self._send_json(
                        {"ok": False, "error": f"cannot read index.html: {exc}"},
                        status=500,
                    )
                    return
                self._send_bytes(
                    body,
                    "text/html; charset=utf-8",
                    cache_control="no-cache",
                )
                return
            if path == "/usage.html":
                usage_path = index_path.with_name("usage.html")
                try:
                    body = usage_path.read_bytes()
                except OSError as exc:
                    self._send_json({"ok": False, "error": f"cannot read usage.html: {exc}"}, status=500)
                    return
                self._send_bytes(body, "text/html; charset=utf-8", cache_control="no-cache")
                return
            if path.startswith("/images/"):
                self._send_image(path)
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self._send_json({"ok": False, "error": "not found"}, status=404)

        def _send_json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            self._send_bytes(
                body,
                "application/json; charset=utf-8",
                status=status,
                cache_control="no-store",
            )

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            *,
            status: int = 200,
            cache_control: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _send_image(self, path: str) -> None:
            images_dir = index_path.with_name("images").resolve()
            target = (images_dir / path[len("/images/"):]).resolve()
            if not target.is_relative_to(images_dir) or not target.is_file():
                self._send_json({"ok": False, "error": "not found"}, status=404)
                return
            content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(target.suffix.lower(), "application/octet-stream")
            self._send_bytes(
                target.read_bytes(),
                content_type,
                cache_control="public, max-age=3600",
            )

        def log_message(self, format_string: str, *args: object) -> None:
            sys.stderr.write(
                f"[{self.log_date_time_string()}] {self.address_string()} "
                f"{format_string % args}\n"
            )

    return DashboardHandler


def main() -> int:
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        services = load_services(config.services_file)
    except ValueError as exc:
        print(f"services configuration error: {exc}", file=sys.stderr)
        return 2
    if not services:
        services = [
            ServiceSpec(id="default", name="default", metrics_url=config.metrics_url)
        ]

    index_path = Path(__file__).resolve().with_name("index.html")
    if not index_path.is_file():
        print(f"missing frontend file: {index_path}", file=sys.stderr)
        return 2

    registry = ServiceRegistry(config, services)
    try:
        server = DashboardHTTPServer(
            (config.host, config.port), make_handler(registry, index_path)
        )
    except OSError as exc:
        print(f"cannot listen on {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 1

    def stop_on_signal(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop_on_signal)
    signal.signal(signal.SIGTERM, stop_on_signal)

    registry.start()
    print(f"Dashboard: http://{config.host}:{config.port}/")
    for spec in services:
        print(f"Service {spec.id}: {spec.metrics_url}")
    print(
        f"Polling every {config.poll_interval_seconds * 1000:.0f} ms; "
        f"rate window {config.rate_window_seconds:g} s; "
        f"latency window {config.latency_window_seconds:g} s"
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping dashboard...")
    finally:
        server.server_close()
        registry.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
