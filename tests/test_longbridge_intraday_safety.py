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


if __name__ == "__main__":
    unittest.main()
