from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.collection_policy import collect_manifest_fulltext_with_policy
from nasdaq_cafe.config import ROOT_DIR, RunConfig
from nasdaq_cafe.provider_capability import build_provider_capability_report


class CollectionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT_DIR)
        base = Path(self.temp.name)
        output_dir = base / "output" / "2026-08-12"
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True)
        self.config = RunConfig(
            target_date="2026-08-12",
            refresh=True,
            output_dir=output_dir,
            raw_dir=raw_dir,
            env={
                "NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS": "0",
                "NASDAQ_CAFE_FULLTEXT_RUNTIME_BUDGET_SECONDS": "1200",
                "NASDAQ_CAFE_FULLTEXT_WORKERS": "6",
                "NASDAQ_CAFE_REQUEST_TIMEOUT_SECONDS": "20",
                "FRED_API_KEY": "fred-key",
                "FMP_API_KEY": "",
                "SEC_USER_AGENT": "NasdaqCafe contact@example.com",
                "SERPAPI_API_KEY": "",
                "TAVILY_API_KEY": "tavily-key",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def manifest(count: int) -> dict:
        return {
            "date": "2026-08-12",
            "documents": [
                {
                    "document_id": f"doc-{index:04d}",
                    "url": f"https://example.com/{index}",
                    "fulltext_status": "pending",
                    "access_status": "pending",
                    "attempts": [],
                }
                for index in range(count)
            ],
        }

    @staticmethod
    def fake_raw_collect(config: RunConfig, manifest: dict) -> dict:
        limit = int(config.env.get("NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS") or "0")
        documents = manifest["documents"]
        attempted = documents if limit == 0 else documents[:limit]
        deferred = [] if limit == 0 else documents[limit:]
        for row in attempted:
            row["fulltext_status"] = "complete"
            row["access_status"] = "readable"
            row["attempts"].append({"route": "requests", "status": "200"})
        for row in deferred:
            row["fulltext_status"] = "not_attempted_limit"
            row["access_status"] = "not_attempted"
            row["failure_reason"] = "configured full-text attempt limit reached; URL retained for retry"
            row["attempts"].append(
                {
                    "route": "requests",
                    "status": "not_attempted_limit",
                    "reason": "configured_fulltext_attempt_limit",
                }
            )
        summary = {
            "target_count": len(documents),
            "attempted_count": len(attempted),
            "complete_count": len(attempted),
            "failed_count": 0,
            "excluded_count": 0,
            "not_attempted_limit_count": len(deferred),
            "pending_count": 0,
        }
        payload = {
            "summary": summary,
            "items": [],
            "unreadable": [
                {
                    "document_id": row["document_id"],
                    "fulltext_status": row["fulltext_status"],
                    "reason": row.get("failure_reason"),
                    "notes_for_chatgpt": row.get("failure_reason"),
                }
                for row in deferred
            ],
        }
        manifest["retrieval_policy"] = {"all_manifest_article_urls_attempted": True}
        return {
            "status": "ok" if not deferred else "partial",
            "cache_used": False,
            "payload": payload,
            "summary": summary,
        }

    def test_94_candidates_have_no_count_cap_or_runtime_deferral(self) -> None:
        manifest = self.manifest(94)
        with patch(
            "nasdaq_cafe.collection_policy.collect_manifest_fulltext",
            side_effect=self.fake_raw_collect,
        ) as mocked:
            result = collect_manifest_fulltext_with_policy(self.config, manifest)

        effective_config = mocked.call_args.args[0]
        self.assertEqual("0", effective_config.env["NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"])
        self.assertEqual(94, result["summary"]["attempted_count"])
        self.assertEqual(0, result["acquisition_coverage"]["not_attempted_runtime_budget_unique"])
        self.assertEqual(1.0, result["acquisition_coverage"]["directAttemptCoverage"])
        self.assertTrue(manifest["retrieval_policy"]["all_manifest_article_urls_attempted"])

    def test_large_manifest_is_deferred_only_by_technical_runtime_budget(self) -> None:
        manifest = self.manifest(500)
        with patch(
            "nasdaq_cafe.collection_policy.collect_manifest_fulltext",
            side_effect=self.fake_raw_collect,
        ) as mocked:
            result = collect_manifest_fulltext_with_policy(self.config, manifest)

        effective_config = mocked.call_args.args[0]
        effective_limit = int(effective_config.env["NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"])
        self.assertGreater(effective_limit, 0)
        self.assertLess(effective_limit, 500)
        deferred = 500 - effective_limit
        self.assertEqual(
            deferred,
            result["acquisition_coverage"]["not_attempted_runtime_budget_unique"],
        )
        self.assertEqual(0, result["summary"]["not_attempted_limit_count"])
        self.assertEqual(deferred, result["summary"]["not_attempted_runtime_budget_count"])
        self.assertFalse(manifest["retrieval_policy"]["all_manifest_article_urls_attempted"])
        self.assertEqual(
            deferred,
            manifest["retrieval_policy"]["runtime_budget_deferred_count"],
        )
        self.assertTrue(
            all(
                row["fulltext_status"] == "not_attempted_runtime_budget"
                for row in manifest["documents"][effective_limit:]
            )
        )

    def test_explicit_count_cap_remains_distinct_from_runtime_budget(self) -> None:
        self.config.env["NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"] = "25"
        manifest = self.manifest(94)
        with patch(
            "nasdaq_cafe.collection_policy.collect_manifest_fulltext",
            side_effect=self.fake_raw_collect,
        ):
            result = collect_manifest_fulltext_with_policy(self.config, manifest)

        self.assertEqual(25, result["summary"]["attempted_count"])
        self.assertEqual(69, result["summary"]["not_attempted_limit_count"])
        self.assertEqual(0, result["acquisition_coverage"]["not_attempted_runtime_budget_unique"])
        self.assertFalse(manifest["retrieval_policy"]["all_manifest_article_urls_attempted"])

    def test_provider_report_separates_configuration_from_observed_status(self) -> None:
        pack = {
            "source_status": {
                "Longbridge": "loaded",
                "FRED": "error",
                "FMP": "skipped",
                "SEC / IR": "ok",
                "SerpAPI": "skipped",
                "Tavily": "partial",
                "GDELT Radar": "ok",
                "Economic Calendar": "partial",
                "RSS": "ok",
                "Raw Archive": "ok",
            },
            "missing_data": [
                {"source": "FRED DGS10", "reason": "HTTP 500", "severity": "medium"},
                {"source": "SerpAPI", "reason": "SERPAPI_API_KEY not set", "severity": "medium"},
            ],
            "collector_metadata": {"acquisitionCoverage": {}},
        }
        report = build_provider_capability_report(self.config, pack)
        providers = {row["provider"]: row for row in report["providers"]}
        self.assertTrue(providers["FRED"]["configured"])
        self.assertEqual("failed", providers["FRED"]["observedStatus"])
        self.assertFalse(providers["SerpAPI"]["configured"])
        self.assertEqual("missing", providers["SerpAPI"]["observedStatus"])
        self.assertTrue(report["operationalOnly"])
        self.assertFalse(report["marketEvidence"])
        self.assertEqual("source_pack.json", report["collectorMachineSourceOfTruth"])


if __name__ == "__main__":
    unittest.main()
