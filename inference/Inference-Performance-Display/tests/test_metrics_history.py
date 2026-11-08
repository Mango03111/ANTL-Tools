#!/usr/bin/env python3
"""Standard-library tests for the persistent metric history store and API."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from metrics_history import (  # noqa: E402
    HistoryUnavailableError,
    MetricsHistoryStore,
)
from server import (  # noqa: E402
    Config,
    DashboardHTTPServer,
    ServiceRegistry,
    ServiceSpec,
    make_handler,
)


class FakeClock:
    """Deterministic wall-clock and monotonic clock for store tests."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_snapshot(
    timestamp: float,
    *,
    status: str = "online",
    model: str = "qwen3-0.6b",
    running: object = 2,
    generation: object = 10.0,
    kv: object = 0.25,
    e2e: object = 0.2,
    ok: bool = True,
) -> dict[str, object]:
    return {
        "ok": ok,
        "timestamp": timestamp,
        "model": model,
        "health": {"status": status},
        "requests": {
            "running": running,
            "waiting": 1,
            "waiting_capacity": None,
            "waiting_deferred": None,
            "requests_per_sec": 1.5,
            "completed_total": 10,
            "preemptions_total": 0,
        },
        "tokens": {
            "prompt_tokens_per_sec": 5.0,
            "generation_tokens_per_sec": generation,
            "prompt_tokens_total": 100,
            "generation_tokens_total": 200,
        },
        "cache": {"kv_cache_usage": kv, "prefix_cache_hit_rate": 0.5},
        "latency": {
            "e2e_p50": e2e,
            "e2e_p90": e2e,
            "e2e_p99": e2e,
            "ttft_p50": 0.01,
            "ttft_p90": 0.01,
            "ttft_p99": 0.01,
            "itl_p50": 0.02,
            "itl_p90": 0.02,
            "itl_p99": 0.02,
            "tpot_p50": 0.02,
            "tpot_p90": 0.02,
            "tpot_p99": 0.02,
            "queue_p50": 0.0,
            "queue_p90": 0.0,
            "queue_p99": 0.0,
        },
    }


class MetricsHistoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "metrics.db"
        self.clock = FakeClock()
        self.store = MetricsHistoryStore(
            self.db_path,
            interval_seconds=10.0,
            retention_days=7,
            max_points=5000,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
        )
        self.store.start()

    def tearDown(self) -> None:
        self.store.stop()
        self.temp_dir.cleanup()

    def _count_rows(self) -> int:
        with sqlite3.connect(self.db_path) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
            )

    def test_start_creates_database_and_schema(self) -> None:
        self.assertTrue(self.db_path.is_file())
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertIn("metric_samples", tables)
        self.assertIn("idx_metric_samples_service_ts", indexes)
        self.assertEqual(mode.lower(), "wal")

    def test_records_and_queries_one_sample(self) -> None:
        sample_ts = self.clock.now - 30
        self.assertTrue(self.store.maybe_record("svc", make_snapshot(sample_ts)))

        result = self.store.query("svc", minutes=60, step=10)

        self.assertTrue(result["ok"])
        self.assertEqual(result["service"], "svc")
        self.assertEqual(result["step_seconds"], 10)
        self.assertEqual(result["count"], 1)
        sample = result["samples"][0]
        self.assertEqual(sample["model"], "qwen3-0.6b")
        self.assertEqual(sample["status"], "online")
        self.assertEqual(sample["requests"]["running"], 2)
        self.assertEqual(sample["tokens"]["generation_tokens_per_sec"], 10.0)
        self.assertEqual(sample["cache"]["kv_cache_usage"], 0.25)
        self.assertTrue(sample["time"])

    def test_service_isolation(self) -> None:
        self.assertTrue(
            self.store.maybe_record("svc-a", make_snapshot(self.clock.now - 30))
        )
        self.clock.advance(10)
        self.assertTrue(
            self.store.maybe_record("svc-b", make_snapshot(self.clock.now - 20))
        )

        result_a = self.store.query("svc-a", minutes=60, step=10)
        result_b = self.store.query("svc-b", minutes=60, step=10)

        self.assertEqual(result_a["count"], 1)
        self.assertEqual(result_b["count"], 1)
        self.assertNotEqual(result_a["samples"][0]["ts"], result_b["samples"][0]["ts"])

    def test_time_range_filtering(self) -> None:
        old_ts = self.clock.now - 2 * 60 * 60
        self.assertTrue(self.store.maybe_record("svc", make_snapshot(old_ts)))
        self.clock.advance(10)
        recent_ts = self.clock.now - 30
        self.assertTrue(self.store.maybe_record("svc", make_snapshot(recent_ts)))

        last_hour = self.store.query("svc", minutes=60, step=10)
        last_three_hours = self.store.query("svc", minutes=180, step=10)

        self.assertEqual(last_hour["count"], 1)
        self.assertEqual(last_three_hours["count"], 2)

    def test_step_aggregation(self) -> None:
        bucket_ts = self.clock.now
        self.assertTrue(
            self.store.maybe_record(
                "svc", make_snapshot(bucket_ts + 1, running=10, generation=10.0)
            )
        )
        self.clock.advance(10)
        self.assertTrue(
            self.store.maybe_record(
                "svc",
                make_snapshot(
                    bucket_ts + 2,
                    running=20,
                    generation=30.0,
                    status="degraded",
                    model="other-model",
                ),
            )
        )
        self.clock.advance(48)

        result = self.store.query("svc", minutes=1, step=10)

        self.assertEqual(result["count"], 1)
        sample = result["samples"][0]
        self.assertEqual(sample["ts"], bucket_ts)
        self.assertEqual(sample["requests"]["running"], 15.0)
        self.assertEqual(sample["tokens"]["generation_tokens_per_sec"], 20.0)
        self.assertEqual(sample["status"], "degraded")
        self.assertEqual(sample["model"], "other-model")

    def test_unknown_service_is_empty(self) -> None:
        result = self.store.query("missing", minutes=60, step=10)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["samples"], [])

    def test_empty_history(self) -> None:
        result = self.store.query("svc", minutes=60, step=10)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["samples"], [])

    def test_retention_prune(self) -> None:
        old_ts = self.clock.now - 8 * 24 * 60 * 60
        self.assertTrue(self.store.maybe_record("svc", make_snapshot(old_ts)))
        self.clock.advance(10)
        self.assertTrue(self.store.maybe_record("svc", make_snapshot(self.clock.now)))

        self.clock.now += 24 * 60 * 60
        removed = self.store.prune()

        self.assertEqual(removed, 1)
        self.assertEqual(self._count_rows(), 1)

    def test_close_and_reopen_preserves_data(self) -> None:
        self.assertTrue(
            self.store.maybe_record("svc", make_snapshot(self.clock.now - 30))
        )
        self.store.stop()

        reopened = MetricsHistoryStore(
            self.db_path,
            interval_seconds=10.0,
            retention_days=7,
            max_points=5000,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
        )
        reopened.start()
        try:
            result = reopened.query("svc", minutes=60, step=10)
        finally:
            reopened.stop()

        self.assertEqual(result["count"], 1)

    def test_invalid_minutes_and_step(self) -> None:
        for minutes, step in ((0, 10), (1441, 10), (60, 0), (60, 301)):
            with self.subTest(minutes=minutes, step=step):
                with self.assertRaises(ValueError):
                    self.store.query("svc", minutes=minutes, step=step)

    def test_max_points_limit(self) -> None:
        limited = MetricsHistoryStore(
            self.db_path,
            interval_seconds=10.0,
            retention_days=7,
            max_points=5,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
        )
        limited.start()
        try:
            with self.assertRaises(ValueError) as raised:
                limited.query("svc", minutes=60, step=10)
        finally:
            limited.stop()
        self.assertIn("increase step", str(raised.exception))

    def test_unhealthy_snapshot_is_not_recorded(self) -> None:
        self.assertFalse(
            self.store.maybe_record(
                "svc", make_snapshot(self.clock.now, ok=False)
            )
        )
        self.assertEqual(self._count_rows(), 0)

    def test_interval_throttles_writes(self) -> None:
        self.assertTrue(
            self.store.maybe_record("svc", make_snapshot(self.clock.now - 30))
        )
        self.assertFalse(
            self.store.maybe_record("svc", make_snapshot(self.clock.now - 20))
        )
        self.clock.advance(10)
        self.assertTrue(
            self.store.maybe_record("svc", make_snapshot(self.clock.now - 10))
        )

    def test_non_finite_values_are_dropped(self) -> None:
        snapshot = make_snapshot(
            self.clock.now - 30,
            running=math.nan,
            generation=math.inf,
            kv=-math.inf,
            e2e=None,
        )
        self.assertTrue(self.store.maybe_record("svc", snapshot))

        sample = self.store.query("svc", minutes=60, step=10)["samples"][0]

        self.assertIsNone(sample["requests"]["running"])
        self.assertIsNone(sample["tokens"]["generation_tokens_per_sec"])
        self.assertIsNone(sample["cache"]["kv_cache_usage"])
        self.assertIsNone(sample["latency"]["e2e_p50"])

    def test_query_before_start_is_unavailable(self) -> None:
        closed = MetricsHistoryStore(
            Path(self.temp_dir.name) / "closed.db",
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
        )
        self.assertFalse(
            closed.maybe_record("svc", make_snapshot(self.clock.now))
        )
        with self.assertRaises(HistoryUnavailableError):
            closed.query("svc", minutes=60, step=10)

    def test_write_after_close_is_safe(self) -> None:
        self.store.close()
        self.assertFalse(
            self.store.maybe_record("svc", make_snapshot(self.clock.now))
        )


class _ApiFixture:
    def __init__(self, *, enable_history: bool = True) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "metrics.db"
        self.clock = FakeClock()
        self.store = MetricsHistoryStore(
            self.db_path,
            interval_seconds=0.0,
            retention_days=7,
            max_points=5000,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
        )
        if enable_history:
            self.store.start()

        env = {
            "ENABLE_USAGE_STATS": "false",
            "ENABLE_METRICS_HISTORY": "true" if enable_history else "false",
            "METRICS_HISTORY_DB": str(self.db_path),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = Config.from_env()
        self.registry = ServiceRegistry(
            config,
            [ServiceSpec(id="svc", name="svc", metrics_url="http://127.0.0.1:1/metrics")],
        )
        if enable_history:
            self.registry.history = self.store

        self.httpd = DashboardHTTPServer(
            ("127.0.0.1", 0), make_handler(self.registry, PROJECT_DIR / "index.html")
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.store.stop()
        self.temp_dir.cleanup()

    def get(self, path: str) -> tuple[int, dict[str, object]]:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class MetricsHistoryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _ApiFixture()
        self.assertTrue(
            self.fixture.store.maybe_record(
                "svc", make_snapshot(self.fixture.clock.now - 30)
            )
        )

    def tearDown(self) -> None:
        self.fixture.close()

    def test_history_returns_samples(self) -> None:
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=60&step=10"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "svc")
        self.assertEqual(payload["step_seconds"], 10)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["samples"][0]["requests"]["running"], 2)

    def test_history_defaults(self) -> None:
        status, payload = self.fixture.get("/api/metrics/history?service=svc")
        self.assertEqual(status, 200)
        self.assertEqual(payload["step_seconds"], 10)

    def test_24h_query_works(self) -> None:
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=1440&step=60"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["step_seconds"], 60)
        self.assertEqual(payload["count"], 1)

    def test_empty_history_is_200(self) -> None:
        self.fixture.close()
        self.fixture = _ApiFixture()
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=60&step=10"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["samples"], [])

    def test_service_all_is_rejected(self) -> None:
        status, payload = self.fixture.get(
            "/api/metrics/history?service=all&minutes=60&step=10"
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("all", payload["error"])

    def test_missing_service_is_rejected(self) -> None:
        status, payload = self.fixture.get("/api/metrics/history?minutes=60")
        self.assertEqual(status, 400)
        self.assertIn("service", payload["error"])

    def test_unknown_service_is_404(self) -> None:
        status, payload = self.fixture.get(
            "/api/metrics/history?service=missing&minutes=60&step=10"
        )
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_invalid_minutes_and_step_are_400(self) -> None:
        for query in ("minutes=0", "minutes=1441", "step=0", "step=301", "minutes=x"):
            with self.subTest(query=query):
                status, payload = self.fixture.get(
                    f"/api/metrics/history?service=svc&{query}"
                )
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])

    def test_max_points_is_400(self) -> None:
        self.fixture.store.max_points = 5
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=60&step=10"
        )
        self.assertEqual(status, 400)
        self.assertIn("increase step", payload["error"])

    def test_disabled_history_is_404(self) -> None:
        self.fixture.close()
        self.fixture = _ApiFixture(enable_history=False)
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=60&step=10"
        )
        self.assertEqual(status, 404)
        self.assertIn("disabled", payload["error"])

    def test_unavailable_store_is_503(self) -> None:
        self.fixture.store.stop()
        status, payload = self.fixture.get(
            "/api/metrics/history?service=svc&minutes=60&step=10"
        )
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

    def test_existing_metrics_and_services_apis_are_unchanged(self) -> None:
        status, payload = self.fixture.get("/api/metrics?service=svc")
        self.assertEqual(status, 200)
        self.assertIn("requests", payload)
        self.assertIn("history", payload)

        status, payload = self.fixture.get("/api/metrics?service=all")
        self.assertEqual(status, 200)
        self.assertIn("requests", payload)

        status, payload = self.fixture.get("/api/services")
        self.assertEqual(status, 200)
        self.assertEqual(payload["services"][0]["id"], "svc")


if __name__ == "__main__":
    unittest.main()
