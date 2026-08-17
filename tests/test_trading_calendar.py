from __future__ import annotations

import unittest
from pathlib import Path

from nasdaq_cafe.config import build_config
from nasdaq_cafe.outputs.write_chatgpt_handoff import render_chatgpt_handoff
from nasdaq_cafe.trading_calendar import resolve_research_trading_session


ROOT = Path(__file__).resolve().parents[1]


class TradingCalendarContractTests(unittest.TestCase):
    def test_monday_uses_previous_friday(self) -> None:
        session = resolve_research_trading_session("2026-08-17")
        self.assertEqual(session.session_date, "2026-08-14")
        self.assertFalse(session.is_half_day)

    def test_independence_day_observed_is_skipped(self) -> None:
        session = resolve_research_trading_session("2026-07-06")
        self.assertEqual(session.session_date, "2026-07-02")

    def test_thanksgiving_is_skipped_and_black_friday_is_half_day(self) -> None:
        thanksgiving_episode = resolve_research_trading_session("2026-11-27")
        self.assertEqual(thanksgiving_episode.session_date, "2026-11-25")
        black_friday_episode = resolve_research_trading_session("2026-11-28")
        self.assertEqual(black_friday_episode.session_date, "2026-11-27")
        self.assertTrue(black_friday_episode.is_half_day)

    def test_new_year_holiday_is_skipped(self) -> None:
        session = resolve_research_trading_session("2027-01-02")
        self.assertEqual(session.session_date, "2026-12-31")

    def test_dst_transition_uses_exchange_timezone_not_manual_offset(self) -> None:
        before = resolve_research_trading_session("2026-03-09")
        after = resolve_research_trading_session("2026-03-10")
        self.assertEqual(before.session_date, "2026-03-06")
        self.assertEqual(after.session_date, "2026-03-09")
        self.assertIn("T21:00:00+00:00", before.market_close_utc)
        self.assertIn("T20:00:00+00:00", after.market_close_utc)

    def test_run_config_binds_one_canonical_research_date(self) -> None:
        config = build_config("2026-08-17", False)
        self.assertEqual(config.target_date, "2026-08-17")
        self.assertEqual(config.research_trading_date, "2026-08-14")
        self.assertEqual(config.research_trading_calendar, "NYSE")

    def test_source_pack_emits_canonical_field(self) -> None:
        source = (ROOT / "nasdaq_cafe" / "run.py").read_text(encoding="utf-8")
        self.assertIn('"researchTradingDate": config.research_trading_date', source)
        self.assertNotIn('researchTradingDate": (', source)

    def test_formal_chatgpt_handoff_consumes_canonical_field_without_recalculation(self) -> None:
        source = (ROOT / "nasdaq_cafe" / "outputs" / "write_chatgpt_handoff.py").read_text(encoding="utf-8")
        self.assertIn('pack.get("researchTradingDate"', source)
        self.assertNotIn("def _market_session_date", source)
        self.assertNotIn("timedelta(days=1)", source)

        rendered = render_chatgpt_handoff(
            {
                "date": "2026-08-17",
                "researchTradingDate": "2026-08-14",
                "researchTradingSession": {
                    "calendar": "NYSE",
                    "marketOpen": "2026-08-14T13:30:00+00:00",
                    "marketClose": "2026-08-14T20:00:00+00:00",
                    "isHalfDay": False,
                },
            }
        )
        self.assertIn("market_session_date_us: 2026-08-14", rendered)


if __name__ == "__main__":
    unittest.main()
