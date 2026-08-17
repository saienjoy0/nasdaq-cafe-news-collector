from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


CALENDAR_NAME = "NYSE"
EPISODE_TIMEZONE = ZoneInfo("Asia/Tokyo")
MARKET_TIMEZONE = ZoneInfo("America/New_York")
EPISODE_COLLECTION_TIME = time(7, 17)
_STANDARD_CLOSE = time(16, 0)


class TradingCalendarError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchTradingSession:
    episode_date: str
    session_date: str
    market_open_utc: str
    market_close_utc: str
    is_half_day: bool
    calendar: str = CALENDAR_NAME

    def metadata(self) -> dict[str, object]:
        return {
            "calendar": self.calendar,
            "marketOpen": self.market_open_utc,
            "marketClose": self.market_close_utc,
            "isHalfDay": self.is_half_day,
            "resolution": "latest-completed-regular-session-before-episode-collection-cutoff",
        }


def resolve_research_trading_session(episode_date: str) -> ResearchTradingSession:
    """Resolve the latest completed US equity session for a NASDAQ Cafe episode.

    The episode date is a Japan-local production date. The production collector runs at
    07:17 Asia/Tokyo, so the canonical cutoff is deterministic even for backfills.
    Exchange holidays, weekends, early closes and DST are delegated to the installed
    NYSE calendar; no downstream component should infer a previous session by calendar
    subtraction.
    """

    try:
        episode_day = datetime.strptime(episode_date, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise TradingCalendarError("episode_date must be YYYY-MM-DD") from exc

    cutoff_local = datetime.combine(
        episode_day,
        EPISODE_COLLECTION_TIME,
        tzinfo=EPISODE_TIMEZONE,
    )
    cutoff_utc = pd.Timestamp(cutoff_local).tz_convert("UTC")

    calendar = mcal.get_calendar(CALENDAR_NAME)
    # Fourteen calendar days safely covers normal US holiday/weekend runs. Keep a
    # wider deterministic fallback for exceptional exchange closures.
    schedule = calendar.schedule(
        start_date=episode_day - timedelta(days=14),
        end_date=episode_day,
    )
    completed = schedule.loc[schedule["market_close"] <= cutoff_utc]
    if completed.empty:
        schedule = calendar.schedule(
            start_date=episode_day - timedelta(days=45),
            end_date=episode_day,
        )
        completed = schedule.loc[schedule["market_close"] <= cutoff_utc]
    if completed.empty:
        raise TradingCalendarError(
            f"no completed {CALENDAR_NAME} session found before episode cutoff {cutoff_local.isoformat()}"
        )

    session_label = completed.index[-1]
    row = completed.iloc[-1]
    market_open = pd.Timestamp(row["market_open"])
    market_close = pd.Timestamp(row["market_close"])
    close_new_york = market_close.tz_convert(MARKET_TIMEZONE)

    return ResearchTradingSession(
        episode_date=episode_day.isoformat(),
        session_date=pd.Timestamp(session_label).date().isoformat(),
        market_open_utc=market_open.tz_convert("UTC").isoformat(),
        market_close_utc=market_close.tz_convert("UTC").isoformat(),
        is_half_day=close_new_york.time().replace(tzinfo=None) < _STANDARD_CLOSE,
    )
