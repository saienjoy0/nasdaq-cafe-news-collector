from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.config import build_config
from nasdaq_cafe.research_acquisition import (
    ResearchAcquisitionError,
    _execute_request,
    run_followup,
    validate_request_document,
    validate_us_symbol,
)


def request_doc(*requests: dict) -> dict:
    return {
        "contractVersion": "1.0.0",
        "episodeDate": "2026-08-06",
        "wave": 1,
        "baseResearchInputManifestSha256": "a" * 64,
        "researchPurpose": "timeline check",
        "requests": list(requests),
    }


def intraday_request(symbol: str = "AMD.US") -> dict:
    return {
        "requestId": "RA-001",
        "type": "market_intraday",
        "reason": "event reaction timing",
        "requiredness": "material",
        "parameters": {
            "symbol": symbol,
            "date": "2026-08-05",
            "resolution": "1m",
            "session": "all",
        },
    }


def exact_url_request(url: str = "https://example.com/target") -> dict:
    return {
        "requestId": "RA-URL",
        "type": "exact_url_archive",
        "reason": "official release",
        "requiredness": "material",
        "parameters": {"url": url, "title": "Target release"},
    }


def temp_config(temp_dir: str):
    config = build_config("2026-08-06", True)
    test_output = Path(temp_dir) / "output" / "2026-08-06"
    return type(config)(
        target_date=config.target_date,
        refresh=config.refresh,
        output_dir=test_output,
        raw_dir=test_output / "raw",
        env=config.env,
    )


class ResearchAcquisitionValidationTests(unittest.TestCase):
    def test_dynamic_symbol_outside_fixed_watchlist_is_allowed(self) -> None:
        normalized = validate_request_document(request_doc(intraday_request("PLTR.US")))
        self.assertEqual("PLTR.US", normalized["requests"][0]["parameters"]["symbol"])

    def test_index_style_us_symbol_is_allowed(self) -> None:
        self.assertEqual(".IXIC.US", validate_us_symbol(".ixic.us"))

    def test_shell_like_symbol_is_rejected(self) -> None:
        with self.assertRaises(ResearchAcquisitionError):
            validate_us_symbol("AMD.US;rm -rf /")

    def test_wave_three_is_rejected(self) -> None:
        value = request_doc(intraday_request())
        value["wave"] = 3
        with self.assertRaisesRegex(ResearchAcquisitionError, "wave"):
            validate_request_document(value)

    def test_intraday_resolution_is_fixed_to_one_minute(self) -> None:
        value = request_doc(intraday_request())
        value["requests"][0]["parameters"]["resolution"] = "5m"
        with self.assertRaisesRegex(ResearchAcquisitionError, "resolution"):
            validate_request_document(value)

    def test_duplicate_market_request_is_rejected(self) -> None:
        one = intraday_request()
        two = dict(intraday_request())
        two["requestId"] = "RA-002"
        with self.assertRaisesRegex(ResearchAcquisitionError, "duplicate market request"):
            validate_request_document(request_doc(one, two))

    def test_duplicate_exact_url_request_is_rejected(self) -> None:
        one = exact_url_request()
        two = dict(exact_url_request())
        two["requestId"] = "RA-URL-2"
        with self.assertRaisesRegex(ResearchAcquisitionError, "duplicate exact URL"):
            validate_request_document(request_doc(one, two))

    def test_exact_url_must_be_absolute_http(self) -> None:
        bad = exact_url_request("file:///etc/passwd")
        with self.assertRaisesRegex(ResearchAcquisitionError, "absolute http"):
            validate_request_document(request_doc(bad))

    def test_episode_date_must_match_collector_target(self) -> None:
        with self.assertRaisesRegex(ResearchAcquisitionError, "collector target date"):
            validate_request_document(request_doc(intraday_request()), episode_date="2026-08-07")


class ResearchAcquisitionExecutionTests(unittest.TestCase):
    def test_valid_followup_writes_request_result_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            request_path.write_text(json.dumps(request_doc(intraday_request())), encoding="utf-8")
            config = temp_config(temp_dir)
            provider = {
                "status": "fetched",
                "cache_used": False,
                "rawRows": [
                    {
                        "time": "2026-08-05 20:00:00",
                        "price": "100.0",
                        "avg_price": "100.0",
                        "volume": "10",
                        "turnover": "1000",
                    }
                ],
                "series": {
                    "source": "Longbridge",
                    "kind": "intraday",
                    "fetched_by": "longbridge-cli",
                    "generated_at": "2026-08-06T00:00:00Z",
                    "symbol": "AMD.US",
                    "marketDate": "2026-08-05",
                    "timezone": "UTC",
                    "session": "all",
                    "resolution": "1m",
                    "precision": "verified-intraday-series",
                    "points": [
                        {
                            "timestamp": "2026-08-05T20:00:00Z",
                            "price": 100.0,
                            "avgPrice": 100.0,
                            "volume": 10,
                            "turnover": 1000.0,
                        }
                    ],
                },
                "missing_data": [],
            }
            with patch("nasdaq_cafe.research_acquisition.fetch_longbridge_intraday", return_value=provider):
                result = run_followup(config, request_path)

            self.assertEqual("success", result["status"])
            self.assertTrue(result["request_path"].is_file())
            self.assertTrue(result["result_path"].is_file())
            self.assertTrue(result["manifest_path"].is_file())
            result_doc = json.loads(result["result_path"].read_text(encoding="utf-8"))
            self.assertEqual("success", result_doc["results"][0]["status"])
            series_path = config.output_dir / result_doc["results"][0]["outputPath"]
            series = json.loads(series_path.read_text(encoding="utf-8"))
            self.assertEqual("verified-intraday-series", series["precision"])
            self.assertRegex(series["rawSha256"], r"^[0-9a-f]{64}$")

    def test_exact_url_success_is_bound_to_requested_document_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = temp_config(temp_dir)
            config.output_dir.mkdir(parents=True, exist_ok=True)
            requested = "https://example.com/target"
            archive = {
                "summary": {"complete_count": 99},
                "payload": {
                    "items": [
                        {"primary_url": "https://example.com/other", "full_text": "other"},
                        {"primary_url": requested, "full_text": "target"},
                    ],
                    "unreadable": [],
                },
            }
            with patch("nasdaq_cafe.research_acquisition.register_and_fetch_url", return_value=archive):
                result = _execute_request(
                    config=config,
                    item=exact_url_request(requested),
                    followup_root=config.output_dir / "followup" / "wave-01",
                    raw_root=config.raw_dir / "followup" / "wave-01",
                )
            self.assertEqual("success", result["status"])
            self.assertEqual(1, result["recordCount"])
            snapshot = json.loads((config.output_dir / result["outputPath"]).read_text(encoding="utf-8"))
            self.assertEqual([requested], [item["primary_url"] for item in snapshot["items"]])

    def test_other_complete_documents_do_not_mask_requested_url_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = temp_config(temp_dir)
            requested = "https://example.com/target"
            archive = {
                "summary": {"complete_count": 99},
                "payload": {
                    "items": [
                        {"primary_url": "https://example.com/other", "full_text": "other"}
                    ],
                    "unreadable": [
                        {"primary_url": requested, "reason": "paywall"}
                    ],
                },
            }
            with patch("nasdaq_cafe.research_acquisition.register_and_fetch_url", return_value=archive):
                result = _execute_request(
                    config=config,
                    item=exact_url_request(requested),
                    followup_root=config.output_dir / "followup" / "wave-01",
                    raw_root=config.raw_dir / "followup" / "wave-01",
                )
            self.assertEqual("unavailable", result["status"])
            self.assertEqual("paywall", result["reason"])
            self.assertIsNone(result["outputPath"])


if __name__ == "__main__":
    unittest.main()
