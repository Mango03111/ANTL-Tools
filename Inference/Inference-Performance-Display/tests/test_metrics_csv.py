import csv
import io
import unittest

from server import history_csv


class HistoryCsvTests(unittest.TestCase):
    def test_exports_history_with_units_and_utf8_bom(self) -> None:
        payload = {
            "history": [
                {
                    "timestamp": 1_700_000_000,
                    "generation": 12.5,
                    "prompt": 3.25,
                    "running": 2,
                    "waiting": 1,
                    "kv": 0.5,
                    "e2e_p50": 0.1,
                    "e2e_p90": 0.2,
                    "e2e_p99": 0.3,
                }
            ]
        }

        body = history_csv(payload, "benchmark")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))

        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "benchmark")
        self.assertEqual(rows[0]["timestamp_unix"], "1700000000.0")
        self.assertEqual(rows[0]["timestamp_iso"], "2023-11-14T22:13:20Z")
        self.assertEqual(rows[0]["kv_cache_usage_pct"], "50.0")
        self.assertEqual(rows[0]["e2e_p50_ms"], "100.0")
        self.assertEqual(rows[0]["e2e_p90_ms"], "200.0")
        self.assertEqual(rows[0]["e2e_p99_ms"], "300.0")

    def test_invalid_history_still_returns_header(self) -> None:
        rows = list(
            csv.reader(io.StringIO(history_csv({"history": "invalid"}, None).decode("utf-8-sig")))
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "service")


if __name__ == "__main__":
    unittest.main()
