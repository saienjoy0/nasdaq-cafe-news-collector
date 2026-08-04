from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.collectors.fmp_collector import collect_fmp
from nasdaq_cafe.collectors.longbridge_raw_loader import load_longbridge_raw
from nasdaq_cafe.collectors.market_movers_collector import collect_market_movers
from nasdaq_cafe.collectors.sec_ir_collector import collect_sec_ir
from nasdaq_cafe.config import ROOT_DIR, RunConfig, load_environment
from nasdaq_cafe.processing.normalize import normalize_quote


class FakeSecResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "name": "Apple Inc.",
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-07-10"],
                    "accessionNumber": ["0000320193-26-000001"],
                    "primaryDocument": ["aapl-8k.htm"],
                    "reportDate": ["2026-07-09"],
                }
            },
        }


class EnvironmentAndSessionTests(unittest.TestCase):
    def _config(self, base: Path, env: dict[str, str]) -> RunConfig:
        output_dir = base / "output" / "2026-07-10"
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        return RunConfig("2026-07-10", False, output_dir, raw_dir, env)

    def test_project_env_loads_without_overwriting_os_values(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            root = Path(temp_name)
            (root / ".env").write_text(
                "FMP_API_KEY=file-fmp-value\nSEC_USER_AGENT=file-sec-agent\n",
                encoding="utf-8",
            )
            original_fmp = os.environ.get("FMP_API_KEY")
            original_sec = os.environ.get("SEC_USER_AGENT")
            try:
                os.environ["FMP_API_KEY"] = "os-fmp-value"
                os.environ.pop("SEC_USER_AGENT", None)
                with patch("nasdaq_cafe.config.ROOT_DIR", root):
                    loaded = load_environment()
                self.assertEqual("os-fmp-value", loaded["FMP_API_KEY"])
                self.assertEqual("file-sec-agent", loaded["SEC_USER_AGENT"])
            finally:
                if original_fmp is None:
                    os.environ.pop("FMP_API_KEY", None)
                else:
                    os.environ["FMP_API_KEY"] = original_fmp
                if original_sec is None:
                    os.environ.pop("SEC_USER_AGENT", None)
                else:
                    os.environ["SEC_USER_AGENT"] = original_sec

    def test_session_states_distinguish_null_empty_missing_and_zero(self) -> None:
        normalized = normalize_quote(
            {
                "symbol": "ZERO.US",
                "last": 0,
                "open": 0,
                "high": None,
                "pre_market": None,
                "post_market": {},
            }
        )
        self.assertEqual("unknown", normalized["active_session"])
        self.assertEqual(0, normalized["regular"]["data"]["last"])
        self.assertEqual("value", normalized["regular"]["field_states"]["last"])
        self.assertEqual("null", normalized["regular"]["field_states"]["high"])
        self.assertEqual("null", normalized["pre_market"]["raw_state"])
        self.assertEqual("empty_object", normalized["post_market"]["raw_state"])
        self.assertEqual("missing", normalized["overnight"]["raw_state"])
        self.assertEqual("not_available", normalized["pre_market"]["availability"])
        self.assertEqual("not_available", normalized["post_market"]["availability"])
        self.assertEqual("not_available", normalized["overnight"]["availability"])

    def test_raw_normalized_and_market_mover_inputs_match(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            config = self._config(Path(temp_name), {})
            raw_item = {
                "symbol": "AAPL.US",
                "last": "316.220",
                "open": "310.510",
                "high": "316.530",
                "low": "308.160",
                "prev_close": "313.390",
                "change_value": "2.830",
                "change_percentage": "0.90",
                "volume": 0,
                "turnover": "15133298066.000",
                "pre_market": None,
                "post_market": {},
                "overnight": {
                    "last": "315.500",
                    "high": "315.780",
                    "low": "314.690",
                    "prev_close": "316.220",
                    "timestamp": "2026-07-10T07:24:38Z",
                    "turnover": "7569807.110",
                    "volume": 24023,
                },
            }
            write_json(config.raw_dir / "longbridge_quotes.json", {"items": [raw_item]})
            write_json(
                config.raw_dir / "market_movers.json",
                {"source": "Market Movers", "status": "ok", "schema_version": 1, "items": []},
            )
            loaded = load_longbridge_raw(config.raw_dir, {})
            movers = collect_market_movers(config, [], loaded["market_mover_inputs"])

            normalized = loaded["quotes"][0]
            mover_input = movers["longbridge_quote_inputs"][0]
            regular_fields = {
                key: raw_item[key]
                for key in (
                    "last",
                    "open",
                    "high",
                    "low",
                    "prev_close",
                    "change_value",
                    "change_percentage",
                    "volume",
                    "turnover",
                )
            }
            self.assertEqual(regular_fields, normalized["regular"]["data"])
            for session in ("regular", "pre_market", "post_market", "overnight"):
                self.assertEqual(normalized[session], mover_input[session])
            self.assertEqual("unknown", mover_input["active_session"])
            normalized_file = json.loads(
                (config.raw_dir / "longbridge_quotes_normalized.json").read_text(encoding="utf-8")
            )
            self.assertEqual(normalized, normalized_file["items"][0])

    def test_fmp_skipped_cache_reactivates_and_restricted_transcript_is_saved(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            config = self._config(
                Path(temp_name),
                {"FMP_API_KEY": "secret-fmp-test", "NASDAQ_CAFE_FMP_COMPANY_LIMIT": "1"},
            )
            write_json(config.raw_dir / "fmp_results.json", {"status": "skipped", "schema_version": 2})

            def fake_endpoint(api_key: str, endpoint: str, params: dict):
                self.assertEqual("secret-fmp-test", api_key)
                del params
                if endpoint == "/earning-call-transcript-dates":
                    return {"status": "restricted", "http_status": 403, "reason": "Restricted Endpoint", "data": []}
                return {"status": "ok", "http_status": 200, "reason": "", "data": [{"endpoint": endpoint}]}

            with patch("nasdaq_cafe.collectors.fmp_collector._fetch_endpoint", side_effect=fake_endpoint):
                result = collect_fmp(config)

            self.assertNotEqual("skipped", result["status"])
            self.assertTrue(any(item.get("status") == "ok" for item in result["raw"]["endpoint_status"]))
            self.assertEqual("restricted", result["raw"]["transcript"]["status"])
            saved = (config.raw_dir / "fmp_results.json").read_text(encoding="utf-8")
            transcript_saved = (config.raw_dir / "fmp_transcript.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-fmp-test", saved + transcript_saved)

    def test_sec_skipped_cache_reactivates_and_user_agent_is_not_saved(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            config = self._config(Path(temp_name), {"SEC_USER_AGENT": "secret-sec-agent"})
            write_json(config.raw_dir / "sec_ir.json", {"status": "skipped", "schema_version": 2})
            with (
                patch("nasdaq_cafe.collectors.sec_ir_collector.SEC_CIKS", {"AAPL": "0000320193"}),
                patch("nasdaq_cafe.collectors.sec_ir_collector.requests.get", return_value=FakeSecResponse()),
                patch("nasdaq_cafe.collectors.sec_ir_collector.time.sleep"),
            ):
                result = collect_sec_ir(config)

            self.assertEqual("ok", result["status"])
            self.assertIn("AAPL", result["raw"]["submissions"])
            saved = (config.raw_dir / "sec_ir.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-sec-agent", saved)


if __name__ == "__main__":
    unittest.main()
