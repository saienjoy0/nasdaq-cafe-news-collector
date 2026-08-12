from __future__ import annotations

import unittest
from datetime import date

from nasdaq_cafe.collectors.cross_market_snapshot_collector import _entry_from_rows
from nasdaq_cafe.collectors.gdelt_perspective_collector import PERSPECTIVE_GDELT_CATEGORIES
from nasdaq_cafe.processing.research_candidate_pool import build_research_candidate_pool


TARGET = "2026-08-12"


def item(title: str, url: str, *, source: str = "Reuters", snippet: str = "", perspective_id: str = ""):
    value = {
        "title": title,
        "source": source,
        "url": url,
        "published_at": "2026-08-11T12:00:00Z",
        "snippet": snippet or title,
    }
    if perspective_id:
        value["perspective_id"] = perspective_id
    return value


class BroadCandidatePoolTests(unittest.TestCase):
    def build(self, *items):
        pool, coverage = build_research_candidate_pool(
            [("TestProvider", list(items))],
            target_date=TARGET,
            coverage_inputs=[],
        )
        return pool, coverage

    def test_t1_nvidia_earnings_stays_in_ai_and_large_tech_when_cloud_capex_link_present(self):
        pool, _ = self.build(
            item(
                "Nvidia earnings lift AI chips as hyperscaler cloud capex accelerates",
                "https://example.com/nvda",
            )
        )
        self.assertEqual(1, pool["pool_count"])
        self.assertIn("P1", pool["items"][0]["perspective_ids"])
        self.assertIn("P2", pool["items"][0]["perspective_ids"])

    def test_t2_china_ai_regulation_is_p4_and_p7_without_causal_claim(self):
        pool, _ = self.build(
            item("China tightens AI chip export controls", "https://example.com/china-ai")
        )
        candidate = pool["items"][0]
        self.assertIn("P4", candidate["perspective_ids"])
        self.assertIn("P7", candidate["perspective_ids"])
        self.assertNotIn("main_cause", candidate)
        self.assertNotIn("market_causality_confirmed", candidate)

    def test_t3_boj_surprise_survives_without_nasdaq_or_ai_terms(self):
        pool, _ = self.build(
            item("BOJ surprise rate hike sends yen sharply higher", "https://example.com/boj")
        )
        candidate = pool["items"][0]
        self.assertIn("P3", candidate["perspective_ids"])
        self.assertIn("P5", candidate["perspective_ids"])

    def test_t4_hormuz_oil_spike_is_p6_p7(self):
        pool, _ = self.build(
            item("Hormuz disruption sends crude oil prices higher", "https://example.com/hormuz")
        )
        candidate = pool["items"][0]
        self.assertIn("P6", candidate["perspective_ids"])
        self.assertIn("P7", candidate["perspective_ids"])

    def test_t5_taiwan_earthquake_is_supply_chain_candidate(self):
        pool, _ = self.build(
            item("Taiwan earthquake disrupts TSMC semiconductor factories", "https://example.com/taiwan")
        )
        candidate = pool["items"][0]
        self.assertIn("P1", candidate["perspective_ids"])
        self.assertIn("P8", candidate["perspective_ids"])

    def test_t6_malacca_is_not_theme_hard_excluded(self):
        pool, _ = self.build(
            item("Strait of Malacca disruption delays global shipping", "https://example.com/malacca")
        )
        candidate = pool["items"][0]
        self.assertIn("P7", candidate["perspective_ids"])
        self.assertIn("P8", candidate["perspective_ids"])

    def test_t7_nato_can_remain_observation_without_market_causality(self):
        pool, _ = self.build(
            item("NATO leaders hold security meeting", "https://example.com/nato")
        )
        self.assertEqual(1, pool["pool_count"])
        self.assertIn("P7", pool["items"][0]["perspective_ids"])
        self.assertNotIn("caused_nasdaq_decline", pool["items"][0])

    def test_t8_grain_inflation_shock_is_macro_commodity_candidate(self):
        pool, _ = self.build(
            item("Grain shock drives food inflation higher", "https://example.com/grain")
        )
        candidate = pool["items"][0]
        self.assertIn("P5", candidate["perspective_ids"])
        self.assertIn("P6", candidate["perspective_ids"])

    def test_t9_duplicate_keeps_provenance_without_turning_count_into_confidence(self):
        article = item("BOJ surprise rate hike", "https://example.com/boj?utm_source=a")
        duplicate = item("BOJ surprise rate hike", "https://example.com/boj?utm_source=b", source="AP")
        pool, _ = build_research_candidate_pool(
            [("RSS", [article]), ("Search", [duplicate])],
            target_date=TARGET,
        )
        self.assertEqual(1, pool["pool_count"])
        self.assertEqual(2, len(pool["items"][0]["provenance"]))
        self.assertNotIn("confidence", pool["items"][0])

    def test_t10_zero_result_perspective_can_be_attempted_and_pass(self):
        coverage_input = {
            "P3_japan": {
                "attempted": True,
                "provider_attempts": {"gdelt": True},
                "provider_status": {"gdelt": "http_200"},
                "raw_count": 0,
                "candidate_count": 0,
            }
        }
        _, coverage = build_research_candidate_pool(
            [],
            target_date=TARGET,
            coverage_inputs=[coverage_input],
        )
        row = coverage["perspectives"]["P3_japan"]
        self.assertTrue(row["attempted"])
        self.assertEqual(0, row["raw_count"])
        self.assertEqual(0, row["candidate_count"])

    def test_pool_and_handoff_are_bounded(self):
        rows = [
            item(
                f"BOJ yen rates story {idx}",
                f"https://example.com/story-{idx}",
            )
            for idx in range(60)
        ]
        pool, _ = self.build(*rows)
        self.assertLessEqual(pool["pool_count"], 40)
        self.assertLessEqual(pool["handoff_count"], 24)
        for candidates in pool["by_perspective"].values():
            self.assertLessEqual(len(candidates), 3)


class GdeltPerspectiveTests(unittest.TestCase):
    def test_exactly_eight_perspective_queries(self):
        self.assertEqual(8, len(PERSPECTIVE_GDELT_CATEGORIES))
        self.assertEqual(
            {f"P{index}" for index in range(1, 9)},
            {spec["perspective_id"] for spec in PERSPECTIVE_GDELT_CATEGORIES.values()},
        )


class CrossMarketSessionTests(unittest.TestCase):
    def test_t12_holiday_mismatch_uses_latest_completed_local_session(self):
        rows = [
            {"time": "2026-08-07T08:00:00Z", "close": "100"},
            {"time": "2026-08-10T08:00:00Z", "close": "102"},
        ]
        entry = _entry_from_rows(
            market="hong_kong",
            instrument="Hang Seng",
            symbol="HSI.HK",
            rows=rows,
            target_session_date=date(2026, 8, 11),
            timezone_name="Asia/Hong_Kong",
            relation="preceding_us_session",
            error="",
            exact_date=False,
        )
        self.assertEqual("available", entry["status"])
        self.assertEqual("2026-08-10", entry["market_date_local"])
        self.assertEqual("completed", entry["session_status"])

    def test_unavailable_data_is_not_fabricated(self):
        entry = _entry_from_rows(
            market="hong_kong",
            instrument="Hang Seng",
            symbol="HSI.HK",
            rows=[],
            target_session_date=date(2026, 8, 11),
            timezone_name="Asia/Hong_Kong",
            relation="preceding_us_session",
            error="provider unavailable",
            exact_date=False,
        )
        self.assertEqual("unavailable", entry["status"])
        self.assertIsNone(entry["price"])
        self.assertEqual("provider unavailable", entry["reason"])


if __name__ == "__main__":
    unittest.main()
