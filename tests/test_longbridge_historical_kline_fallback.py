from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.collectors import longbridge_cli_quotes as lb


class LongbridgeHistoricalKlineFallbackTests(unittest.TestCase):
    def test_zero_history_timeshares_falls_back_to_one_minute_kline(self) -> None:
        auth = {
            "returncode": 0,
            "stdout": json.dumps({"token": {"status": "valid"}}),
            "stderr": "",
        }
        empty_intraday = {
            "returncode": 0,
            "stdout": json.dumps({"timeshares": []}),
            "stderr": "",
        }
        kline_rows = [
            {
                "time": "2026-08-07T13:30:00Z",
                "session": "Intraday",
                "open": "580.10",
                "high": "580.20",
                "low": "579.90",
                "close": "580.15",
                "volume": "12345",
                "turnover": "7161017.50",
            }
        ]
        kline = {
            "returncode": 0,
            "stdout": json.dumps(kline_rows),
            "stderr": "",
        }
        with (
            patch.object(lb, "_find_longbridge_executable", return_value=Path("/usr/bin/longbridge")),
            patch.object(lb, "_run_longbridge", side_effect=[auth, empty_intraday, kline]) as runner,
        ):
            result = lb.fetch_longbridge_intraday(
                symbol="QQQ.US",
                target_date="2026-08-07",
                session="all",
            )

        self.assertEqual("fetched", result["status"])
        self.assertEqual("kline-history-fallback", result["series"]["providerSurface"])
        self.assertEqual("minute-close", result["series"]["priceBasis"])
        self.assertEqual(580.15, result["series"]["points"][0]["price"])
        self.assertEqual(580.10, result["series"]["points"][0]["open"])
        self.assertEqual("Intraday", result["series"]["points"][0]["session"])
        self.assertEqual({"timeshares": []}, result["rawRows"]["historicalIntraday"])
        self.assertEqual(kline_rows, result["rawRows"]["fallbackKlineHistory"])

        fallback_command = runner.call_args_list[2].args[0]
        self.assertEqual(["kline", "history"], fallback_command[1:3])
        self.assertIn("1m", fallback_command)
        self.assertIn("2026-08-07", fallback_command)
        self.assertIn("--session", fallback_command)
        self.assertIn("all", fallback_command)

    def test_existing_historical_intraday_rows_do_not_call_fallback(self) -> None:
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
                            "timestamp": "1786119000",
                            "price": "580.15",
                            "avg_price": "580.00",
                            "amount": "12345",
                            "balance": "7161017.50",
                        }
                    ]
                }
            ]
        }
        primary = {"returncode": 0, "stdout": json.dumps(historical), "stderr": ""}
        with (
            patch.object(lb, "_find_longbridge_executable", return_value=Path("/usr/bin/longbridge")),
            patch.object(lb, "_run_longbridge", side_effect=[auth, primary]) as runner,
        ):
            result = lb.fetch_longbridge_intraday(
                symbol="QQQ.US",
                target_date="2026-08-07",
                session="all",
            )

        self.assertEqual("fetched", result["status"])
        self.assertEqual("history-timeshares", result["series"]["providerSurface"])
        self.assertEqual("intraday-line-price", result["series"]["priceBasis"])
        self.assertEqual(2, runner.call_count)

    def test_kline_history_is_read_only_allowed_surface(self) -> None:
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "[]", "stderr": ""},
        )()
        with patch("subprocess.run", return_value=completed) as run:
            result = lb._run_longbridge(
                [
                    "/usr/bin/longbridge",
                    "kline",
                    "history",
                    "QQQ.US",
                    "--period",
                    "1m",
                    "--start",
                    "2026-08-07",
                    "--end",
                    "2026-08-07",
                    "--session",
                    "all",
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, result["returncode"])
        self.assertEqual(["kline", "history"], run.call_args.args[0][1:3])

    def test_non_history_kline_stays_blocked(self) -> None:
        with self.assertRaises(ValueError):
            lb._run_longbridge(
                ["/usr/bin/longbridge", "kline", "QQQ.US", "--period", "1m"]
            )


if __name__ == "__main__":
    unittest.main()
