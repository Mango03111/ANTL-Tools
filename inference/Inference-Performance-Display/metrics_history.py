#!/usr/bin/env python3
"""SQLite-backed persistent metric history for the inference dashboard.

The store keeps one compact JSON snapshot per service at a configurable
interval (``METRICS_HISTORY_INTERVAL_SECONDS``, default 10 s), prunes samples
older than the retention window, and exposes a bucketed range query.  Only the
Python standard library is used.

The writer is the dashboard's single polling thread, so the SQLite connection
is opened with ``check_same_thread=False`` and every operation is guarded by a
re-entrant lock.  Failures are logged and swallowed by the write path: a broken
history database must never take the monitoring server down.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_POINTS = 5000
_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60

_STATUS_RANK = {"online": 0, "starting": 1, "degraded": 2, "stale": 3, "offline": 4}

# Only these normalized fields are persisted.  Keeping the schema small makes
# the JSON predictable across vLLM versions; unknown fields are ignored.
_SECTION_KEYS: Mapping[str, tuple[str, ...]] = {
    "requests": (
        "running",
        "waiting",
        "waiting_capacity",
        "waiting_deferred",
        "requests_per_sec",
        "completed_total",
        "preemptions_total",
    ),
    "tokens": (
        "prompt_tokens_per_sec",
        "generation_tokens_per_sec",
        "prompt_tokens_total",
        "generation_tokens_total",
    ),
    "cache": (
        "kv_cache_usage",
        "prefix_cache_hit_rate",
    ),
    "latency": (
        "e2e_p50",
        "e2e_p90",
        "e2e_p99",
        "ttft_p50",
        "ttft_p90",
        "ttft_p99",
        "itl_p50",
        "itl_p90",
        "itl_p99",
        "tpot_p50",
        "tpot_p90",
        "tpot_p99",
        "queue_p50",
        "queue_p90",
        "queue_p99",
    ),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_samples (
    service_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    status     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (service_id, ts)
)
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_metric_samples_service_ts
    ON metric_samples (service_id, ts)
"""


class HistoryError(Exception):
    """Base class for persistent metric history errors."""


class HistoryUnavailableError(HistoryError):
    """Raised when the history database cannot be opened or queried."""


def _finite_number(value: object) -> int | float | None:
    """Return *value* when it is a finite number, otherwise ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _snapshot_status(snapshot: Mapping[str, object]) -> str:
    health = snapshot.get("health")
    if isinstance(health, Mapping):
        status = health.get("status")
        if isinstance(status, str) and status:
            return status
    return "online"


def _normalize_payload(snapshot: Mapping[str, object]) -> dict[str, object] | None:
    """Build the compact, JSON-safe payload persisted for one sample."""
    if not snapshot.get("ok"):
        return None
    payload: dict[str, object] = {
        "model": str(snapshot.get("model") or "unknown"),
    }
    for section, keys in _SECTION_KEYS.items():
        raw = snapshot.get(section)
        values: dict[str, object] = {}
        for key in keys:
            value = raw.get(key) if isinstance(raw, Mapping) else None
            values[key] = _finite_number(value)
        payload[section] = values
    return payload


def _average(values: Iterable[object]) -> float | None:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def _iso_timestamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _validate_int(name: str, value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


class MetricsHistoryStore:
    """Persist normalized snapshots and answer bucketed range queries."""

    def __init__(
        self,
        db_path: Path | str,
        interval_seconds: float = 10.0,
        retention_days: int = 7,
        max_points: int = DEFAULT_MAX_POINTS,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db_path = Path(db_path)
        self.interval_seconds = float(interval_seconds)
        self.retention_days = int(retention_days)
        self.max_points = int(max_points)
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._started = False
        self._last_error: str | None = None
        self._last_record_monotonic: float | None = None
        self._stop_event = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def available(self) -> bool:
        with self._lock:
            return self._started and self._conn is not None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def start(self) -> None:
        """Open the database, create the schema, and prune once.

        A failure is recorded in ``last_error`` and leaves the store
        unavailable; it does not raise, so the dashboard can keep running.
        """
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            self._last_record_monotonic = None
            connection: sqlite3.Connection | None = None
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    str(self.db_path),
                    timeout=5.0,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute(_SCHEMA)
                connection.execute(_INDEX)
                connection.commit()
            except Exception as exc:  # A broken DB must not stop monitoring.
                self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning(
                    "cannot open metrics history database %s: %s",
                    self.db_path,
                    self._last_error,
                )
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                return
            self._conn = connection
            self._started = True
            self._last_error = None

        self._safe_prune()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="metrics-history-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def stop(self) -> None:
        """Stop the maintenance thread and close the SQLite connection."""
        with self._lock:
            self._stop_event.set()
            thread = self._maintenance_thread
            self._maintenance_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self.close()

    def close(self) -> None:
        """Close the SQLite connection; safe to call more than once."""
        with self._lock:
            connection = self._conn
            self._conn = None
            self._started = False
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    LOGGER.warning("cannot close metrics history database: %s", exc)

    # -- writing ----------------------------------------------------------

    def maybe_record(
        self, service_id: str, snapshot: Mapping[str, object]
    ) -> bool:
        """Persist one snapshot if it is healthy and the interval has elapsed.

        Returns ``True`` only when a row was written.  All failures are logged
        and reported as ``False``; they never propagate to the poll loop.
        """
        if not isinstance(snapshot, Mapping) or not snapshot.get("ok"):
            return False
        payload = _normalize_payload(snapshot)
        if payload is None:
            return False

        raw_ts = _finite_number(snapshot.get("timestamp"))
        sampled_at = float(raw_ts) if raw_ts is not None else self._clock()
        status = _snapshot_status(snapshot)
        now_monotonic = self._monotonic()

        with self._lock:
            if not self._started or self._conn is None:
                return False
            if (
                self._last_record_monotonic is not None
                and now_monotonic - self._last_record_monotonic
                < self.interval_seconds
            ):
                return False
            # Throttle failed writes too: retry on the next interval instead of
            # hammering a locked database every poll.
            self._last_record_monotonic = now_monotonic
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                with self._conn:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO metric_samples "
                        "(service_id, ts, status, payload) VALUES (?, ?, ?, ?)",
                        (service_id, sampled_at, status, body),
                    )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning(
                    "cannot write metrics history for %s: %s", service_id, exc
                )
                return False
            self._last_error = None
            return True

    def prune(self) -> int:
        """Delete samples older than the retention window.

        Returns the number of removed rows.  Raises ``HistoryUnavailableError``
        when the store is not open or SQLite fails.
        """
        cutoff = self._clock() - self.retention_days * 24 * 60 * 60
        with self._lock:
            if not self._started or self._conn is None:
                raise HistoryUnavailableError("metrics history is not available")
            try:
                with self._conn:
                    cursor = self._conn.execute(
                        "DELETE FROM metric_samples WHERE ts < ?", (cutoff,)
                    )
                removed = int(cursor.rowcount) if cursor.rowcount > 0 else 0
            except sqlite3.Error as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise HistoryUnavailableError(self._last_error) from exc
            return removed

    def _safe_prune(self) -> None:
        try:
            removed = self.prune()
            if removed:
                LOGGER.info("pruned %d expired metric history rows", removed)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("cannot prune metrics history: %s", exc)

    def _maintenance_loop(self) -> None:
        while not self._stop_event.wait(_PRUNE_INTERVAL_SECONDS):
            self._safe_prune()

    # -- reading ----------------------------------------------------------

    def query(
        self, service_id: str, minutes: int = 60, step: int = 10
    ) -> dict[str, object]:
        """Return bucketed samples for one service and a recent time range.

        Buckets are aligned to the Unix epoch.  Numeric fields are averaged,
        ``model`` keeps the last non-empty value, and ``status`` keeps the
        worst state seen in the bucket.
        """
        minutes = _validate_int("minutes", minutes, 1, 1440)
        step = _validate_int("step", step, 1, 300)
        estimated_points = math.ceil(minutes * 60 / step)
        if estimated_points > self.max_points:
            raise ValueError(
                f"query would return about {estimated_points} points, exceeding "
                f"METRICS_HISTORY_MAX_POINTS={self.max_points}; increase step or "
                "decrease minutes"
            )

        with self._lock:
            if not self._started or self._conn is None:
                raise HistoryUnavailableError(
                    self._last_error or "metrics history is not available"
                )
            to_ts = float(self._clock())
            from_ts = to_ts - minutes * 60
            try:
                rows = self._conn.execute(
                    "SELECT ts, status, payload FROM metric_samples "
                    "WHERE service_id = ? AND ts >= ? AND ts <= ? "
                    "ORDER BY ts",
                    (service_id, from_ts, to_ts),
                ).fetchall()
            except sqlite3.Error as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                raise HistoryUnavailableError(self._last_error) from exc

        samples = _aggregate_rows(rows, step)
        return {
            "ok": True,
            "service": service_id,
            "from": from_ts,
            "to": to_ts,
            "step_seconds": step,
            "count": len(samples),
            "samples": samples,
        }


def _aggregate_rows(
    rows: Sequence[tuple[float, str, str]], step: int
) -> list[dict[str, object]]:
    """Group raw rows into epoch-aligned buckets and average their fields."""
    buckets: dict[int, list[tuple[float, str, Mapping[str, object]]]] = {}
    for ts, status, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            LOGGER.warning("skipping malformed metric history payload at %s", ts)
            continue
        if not isinstance(payload, Mapping):
            continue
        bucket = int(float(ts) // step) * step
        buckets.setdefault(bucket, []).append((float(ts), str(status), payload))

    samples: list[dict[str, object]] = []
    for bucket in sorted(buckets):
        entries = buckets[bucket]
        payloads = [payload for _, _, payload in entries]
        model = "unknown"
        for _, _, payload in entries:
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate:
                model = candidate
        status = max(
            (entry_status for _, entry_status, _ in entries),
            key=lambda name: _STATUS_RANK.get(name, 3),
        )

        sample: dict[str, object] = {
            "ts": float(bucket),
            "time": _iso_timestamp(float(bucket)),
            "model": model,
            "status": status,
        }
        for section, keys in _SECTION_KEYS.items():
            sample[section] = {
                key: _average(
                    payload.get(section, {}).get(key)
                    if isinstance(payload.get(section), Mapping)
                    else None
                    for payload in payloads
                )
                for key in keys
            }
        samples.append(sample)
    return samples
