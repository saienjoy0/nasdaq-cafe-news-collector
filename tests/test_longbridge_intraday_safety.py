from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.collectors import longbridge_cli_quotes as lb


class LongbridgeIntradaySafetyTests(unittest.TestCase):
    def test_intraday_is_read_only_allowed_surface(self) -> None:
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "[]", "stderr": ""},
        )()
        with patch("subprocess.run", return_value=completed) as run:
            result = lb._run_longbridge(
                [
                    "/usr/bin/longbridge",
                    "intraday",
                    "AMD.US",
                    "--date",
                    "20260805",
                    "--session",
                    "all",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, result["returncode"])
        self.assertEqual("intraday", run.call_args.args[0][1])

    def test_trade_command_stays_blocked(self) -> None:
        with self.assertRaises(ValueError):
            lb._run_longbridge(["/usr/bin/longbridge", "order", "submit"])

    def test_positions_command_stays_blocked(self) -> None:
        with self.assertRaises(ValueError):
            lb._run_longbridge(["/usr/bin/longbridge", "positions", "--format", "json"])

    def test_intraday_rows_are_normalized_as_utc_verified_series(self) -> None:
        auth = {
            "returncode": 0,
            "stdout": json.dumps({"token": {"status": "valid"}}),
            "stderr": "",
        }
        rows = [
            {
                "time": "2026-04-09 13:30:00",
                "price": "343.150",
                "avg_price": "343.150",
                "volume": "123",
                "turnover": "42123.45",
            }
        ]
        data = {"returncode": 0, "stdout": json.dumps(rows), "stderr": ""}
        with (
            patch.object(lb, "_find_longbridge_executable", return_value=Path("/usr/bin/longbridge")),
            patch.object(lb, "_run_longbridge", side_effect=[auth, data]) as runner,
        ):
            result = lb.fetch_longbridge_intraday(
                symbol="AMD.US",
                target_date="2026-04-09",
                session="all",
            )
        self.assertEqual("fetched", result["status"])
        series = result["series"]
        self.assertEqual("verified-intraday-series", series["precision"])
        self.assertEqual("UTC", series["timezone"])
        self.assertEqual("2026-04-09T13:30:00Z", series["points"][0]["timestamp"])
        command = runner.call_args_list[1].args[0]
        self.assertEqual("intraday", command[1])
        self.assertIn("20260409", command)
        self.assertIn("--session", command)

    def test_historical_timeshares_object_is_normalized(self) -> None:
        auth = {
            "returncode": 0,
            "stdout": json.dumps({"token": {"status": "valid"}}),
            "stderr": "",
        }
        historical = {
            "timeshares": [
                {
                    "minutes": [
                        {
                            "timestamp": "1775731800",
                            "price": "343.150",
                            "avg_price": "343.100",
                            "amount": "123",
                            "balance": "42123.45",
                        }
                    ]
                }
            ]
        }
        data = {"returncode": 0, "stdout": json.dumps(historical), "stderr": ""}
        with (
            patch.object(lb, "_find_longbridge_executable", return_value=Path("/usr/bin/longbridge")),
            patch.object(lb, "_run_longbridge", side_effect=[auth, data]),
        ):
            result = lb.fetch_longbridge_intraday(
                symbol="QQQ.US",
                target_date="2026-04-09",
                session="all",
            )
        self.assertEqual("fetched", result["status"])
        self.assertEqual(historical, result["rawRows"])
        self.assertEqual(123, result["series"]["points"][0]["volume"])
        self.assertEqual(42123.45, result["series"]["points"][0]["turnover"])
        self.assertTrue(result["series"]["points"][0]["timestamp"].endswith("Z"))

    def test_unknown_historical_object_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            lb._extract_intraday_rows({"lines": []})

    def test_rfc3339_timestamp_is_normalized_to_utc(self) -> None:
        row = {
            "time": "2026-04-09T09:30:00-04:00",
            "price": "343.150",
            "avg_price": "343.150",
            "volume": "123",
            "turnover": "42123.45",
        }
        normalized = lb._normalize_intraday_row(row)
        self.assertEqual("2026-04-09T13:30:00Z", normalized["timestamp"])

    def test_zulu_timestamp_is_preserved_as_utc(self) -> None:
        row = {
            "time": "2026-04-09T13:30:00Z",
            "price": "343.150",
            "avg_price": "343.150",
            "volume": "123",
            "turnover": "42123.45",
        }
        normalized = lb._normalize_intraday_row(row)
        self.assertEqual("2026-04-09T13:30:00Z", normalized["timestamp"])

    def test_unix_timestamp_is_normalized_to_utc(self) -> None:
        normalized = lb._parse_intraday_time("1775731800")
        self.assertEqual("UTC", normalized.tzname())


if __name__ == "__main__":
    unittest.main()
