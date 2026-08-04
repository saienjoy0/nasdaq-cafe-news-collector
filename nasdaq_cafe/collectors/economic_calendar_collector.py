from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from nasdaq_cafe.cache import load_or_fetch, read_json
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.normalize import clamp_text, strip_html, utc_now_iso


FED_RSS_FEEDS = [
    ("Federal Reserve Monetary Policy RSS", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve Speeches RSS", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("Federal Reserve Speeches and Testimony RSS", "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"),
]

BLS_RSS_FEEDS = [
    ("BLS CPI RSS", "https://www.bls.gov/feed/cpi.rss"),
    ("BLS PPI RSS", "https://www.bls.gov/feed/ppi.rss"),
    ("BLS Employment Situation RSS", "https://www.bls.gov/feed/empsit.rss"),
]

BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

EVENT_KEYWORDS = (
    "fomc",
    "federal open market",
    "fed chair",
    "powell",
    "cpi",
    "consumer price index",
    "pce",
    "personal income and outlays",
    "ppi",
    "producer price index",
    "nonfarm",
    "employment situation",
    "unemployment",
    "jolts",
    "job openings",
    "ism manufacturing",
    "ism services",
    "retail sales",
    "gross domestic product",
    " gdp",
    "international trade",
    "jobless claims",
    "treasury auction",
    "consumer sentiment",
    "speech",
    "testimony",
    "monetary policy",
    "transmission of monetary policy",
)

UPCOMING_WATCH_WINDOW_DAYS = 7
UPCOMING_MAJOR_EVENT_WINDOW_DAYS = 45


def collect_economic_calendar(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "economic_calendar.json"

    def fetcher() -> dict[str, Any]:
        target_date = _parse_date(config.target_date) or date.today()
        market_session_date = _previous_us_market_session(target_date)
        prior_business_date = _previous_business_day(market_session_date)

        raw_events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        feed_status: list[dict[str, Any]] = []

        for source, url in FED_RSS_FEEDS + BLS_RSS_FEEDS:
            try:
                source_events = _fetch_rss_events(source, url)
                raw_events.extend(source_events)
                feed_status.append({"source": source, "url": url, "status": "ok", "count": len(source_events)})
            except Exception as exc:
                error = _source_error(source, url, exc)
                errors.append(error)
                feed_status.append({"source": source, "url": url, "status": "error", "count": 0, "error": error["error"]})

        try:
            bea_events = _fetch_bea_schedule_events(target_date)
            raw_events.extend(bea_events)
            feed_status.append(
                {"source": "BEA Release Schedule", "url": BEA_SCHEDULE_URL, "status": "ok", "count": len(bea_events)}
            )
        except Exception as exc:
            error = _source_error("BEA Release Schedule", BEA_SCHEDULE_URL, exc)
            errors.append(error)
            feed_status.append(
                {"source": "BEA Release Schedule", "url": BEA_SCHEDULE_URL, "status": "error", "count": 0, "error": error["error"]}
            )

        try:
            rss_events = _extract_existing_rss_events(config.raw_dir)
            raw_events.extend(rss_events)
            feed_status.append({"source": "Existing RSS News", "url": "raw/rss_news.json", "status": "ok", "count": len(rss_events)})
        except Exception as exc:
            error = _source_error("Existing RSS News", "raw/rss_news.json", exc)
            errors.append(error)
            feed_status.append({"source": "Existing RSS News", "url": "raw/rss_news.json", "status": "error", "count": 0, "error": error["error"]})

        items = _dedupe_events(
            _normalize_event(event, target_date, market_session_date, prior_business_date) for event in raw_events
        )
        economic_events = _categorize_events(items)

        return {
            "source": "Economic Calendar",
            "status": _status(items, errors),
            "generated_at": utc_now_iso(),
            "target_date_jst": target_date.isoformat(),
            "market_session_date_us": market_session_date.isoformat(),
            "previous_business_date_us": prior_business_date.isoformat(),
            "items": items,
            "economic_events": economic_events,
            "errors": errors,
            "feed_status": feed_status,
            "notes": [
                "Uses official/public feeds and schedules only.",
                "No article body, login-only page, paywall bypass, CAPTCHA bypass, or trading/account endpoint is used.",
                "Upcoming events are watch points only and must not be treated as causes of the previous US session.",
                f"Upcoming includes the next {UPCOMING_WATCH_WINDOW_DAYS} days plus major official macro events within {UPCOMING_MAJOR_EVENT_WINDOW_DAYS} days.",
            ],
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    economic_events = payload.get("economic_events") or _categorize_events(payload.get("items", []))
    missing_data = _missing_data(payload, economic_events)
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "economic_events": economic_events,
        "raw": payload,
        "missing_data": missing_data,
    }


def _fetch_rss_events(source: str, url: str) -> list[dict[str, Any]]:
    import requests

    response = requests.get(url, headers={"User-Agent": "nasdaq-cafe-economic-calendar/0.1"}, timeout=10)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    output: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:25]:
        title = strip_html(item.findtext("title") or "")
        snippet = clamp_text(item.findtext("description") or title)
        if not _looks_like_target_event(title, snippet, source):
            continue
        output.append(
            {
                "event_name": title,
                "event_date": _date_from_rss(item.findtext("pubDate") or ""),
                "event_time": _time_from_rss(item.findtext("pubDate") or ""),
                "source": source,
                "url": strip_html(item.findtext("link") or ""),
                "snippet": snippet,
            }
        )
    return output


def _fetch_bea_schedule_events(target_date: date) -> list[dict[str, Any]]:
    import requests

    response = requests.get(BEA_SCHEDULE_URL, headers={"User-Agent": "nasdaq-cafe-economic-calendar/0.1"}, timeout=15)
    response.raise_for_status()
    parser = _VisibleTextParser()
    parser.feed(response.text)
    lines = parser.lines
    output: list[dict[str, Any]] = []
    for index in range(0, max(0, len(lines) - 2)):
        title = lines[index]
        date_line = lines[index + 1]
        time_line = lines[index + 2]
        if not _is_bea_release_title(title):
            continue
        if not _is_month_day(date_line) or not _is_time(time_line):
            continue
        event_date = _month_day_to_date(date_line, target_date)
        output.append(
            {
                "event_name": title,
                "event_date": event_date.isoformat(),
                "event_time": f"{time_line} ET",
                "source": "BEA Release Schedule",
                "url": BEA_SCHEDULE_URL,
                "snippet": f"Official BEA scheduled release: {title}",
            }
        )
    return output


def _extract_existing_rss_events(raw_dir) -> list[dict[str, Any]]:
    payload = read_json(raw_dir / "rss_news.json") or {}
    output: list[dict[str, Any]] = []
    for item in payload.get("items", []):
        title = strip_html(item.get("title") or "")
        snippet = clamp_text(item.get("snippet") or title)
        source = strip_html(item.get("source") or "RSS")
        if source in {"SerpAPI", "Tavily"}:
            continue
        if not _looks_like_target_event(title, snippet, source):
            continue
        output.append(
            {
                "event_name": title,
                "event_date": _date_from_any(item.get("published_at")),
                "event_time": "",
                "source": f"RSS News ({source})",
                "url": strip_html(item.get("url") or ""),
                "snippet": snippet,
            }
        )
    return output[:20]


def _normalize_event(raw: dict[str, Any], target_date: date, market_session_date: date, prior_business_date: date) -> dict[str, Any]:
    event_name = strip_html(raw.get("event_name") or raw.get("title") or "")
    event_date = _normalize_date_text(raw.get("event_date"))
    event_time = strip_html(raw.get("event_time") or "")
    source = strip_html(raw.get("source") or "Economic Calendar")
    url = strip_html(raw.get("url") or "")
    snippet = clamp_text(raw.get("snippet") or event_name, 220)
    event_window = _event_window(event_date, target_date, market_session_date, prior_business_date, event_name, source)
    event_phase = _event_phase_from_window(event_window)
    importance = _importance(event_name, source)
    score = _impact_score(event_name, source, importance)
    actual = raw.get("actual")
    forecast = raw.get("forecast")
    previous = raw.get("previous")
    driver_type = _driver_type(event_phase, importance, actual, forecast, previous, event_name, source, event_window)
    filter_reason = _filter_reason(event_phase, driver_type, event_name, source, event_window)
    causal_bridge = _causal_bridge(event_name, source, event_phase, driver_type, event_window)
    return {
        "event_name": event_name,
        "country": "US",
        "event_date": event_date,
        "event_time": event_time,
        "event_phase": event_phase,
        "event_window": event_window,
        "importance": importance,
        "event_impact_score": score,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "market_expectation": _market_expectation(event_name, source),
        "why_viewer_should_care": _why_viewer_should_care(event_name, source),
        "source": source,
        "url": url,
        "snippet": snippet,
        "driver_type": driver_type,
        "causal_bridge": causal_bridge,
        "confidence": _confidence(source, event_date, url),
        "filter_reason": filter_reason,
    }


def _categorize_events(items: list[dict[str, Any]]) -> dict[str, Any]:
    past_events = [
        item for item in items if item.get("event_phase") == "past_event" and item.get("driver_type") != "low_value_macro"
    ]
    upcoming_events = [
        item for item in items if item.get("event_phase") == "upcoming_event" and item.get("driver_type") != "low_value_macro"
    ]
    past_events.sort(key=lambda item: (_as_int(item.get("event_impact_score")), item.get("event_date", "")), reverse=True)
    upcoming_events.sort(key=lambda item: (item.get("event_date", ""), -_as_int(item.get("event_impact_score"))))
    return {
        "metadata": {
            "past_event_window": "previous US market session and prior business day",
            "upcoming_watch_window_days": UPCOMING_WATCH_WINDOW_DAYS,
            "upcoming_major_event_window_days": UPCOMING_MAJOR_EVENT_WINDOW_DAYS,
            "upcoming_policy": "Upcoming includes this-week watch points plus next major official macro events; upcoming events are not prior-session causes.",
        },
        "past_events": past_events,
        "upcoming_events": upcoming_events,
        "low_value_macro_summary": _low_value_summary(items),
    }


def _low_value_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], int] = {}
    for item in items:
        if item.get("driver_type") != "low_value_macro" and item.get("event_phase") != "low_value_macro":
            continue
        reason = item.get("filter_reason") or "Weak or out-of-window macro connection."
        source = item.get("source") or "Economic Calendar"
        key = (source, reason)
        buckets[key] = buckets.get(key, 0) + 1
    return [
        {"source": source, "reason": reason, "count": count}
        for (source, reason), count in sorted(buckets.items(), key=lambda row: (row[0][0], row[0][1]))
    ]


def _missing_data(payload: dict[str, Any], economic_events: dict[str, Any]) -> list[dict[str, str]]:
    items = payload.get("items", [])
    errors = payload.get("errors", [])
    output = [
        missing("Economic Calendar", f"{error.get('source')}: {error.get('error')}", "low")
        for error in errors
    ]
    if not items:
        if errors:
            output.append(missing("Economic Calendar", "stable public source unavailable.", "medium"))
        output.append(missing("Economic Calendar", "source unavailable or no events found.", "medium"))
    target_events = economic_events.get("past_events", []) + economic_events.get("upcoming_events", [])
    if not any(item.get("importance") in {"high", "medium"} for item in target_events):
        output.append(missing("Economic Calendar", "no high or medium importance events found for target period.", "low"))
    return output


def _looks_like_target_event(title: str, snippet: str, source: str) -> bool:
    text = f"{title} {snippet} {source}".lower()
    if "federal reserve speeches" in source.lower():
        return True
    return any(keyword in text for keyword in EVENT_KEYWORDS)


def _is_bea_release_title(title: str) -> bool:
    lowered = title.lower()
    return any(
        keyword in lowered
        for keyword in (
            "gross domestic product",
            "gdp",
            "personal income and outlays",
            "international trade in goods and services",
        )
    )


def _importance(event_name: str, source: str) -> str:
    text = f"{event_name} {source}".lower()
    if any(
        keyword in text
        for keyword in (
            "fomc",
            "federal open market",
            "fed chair",
            "powell",
            "cpi",
            "consumer price index",
            "pce",
            "personal income and outlays",
            "ppi",
            "producer price index",
            "nonfarm",
            "employment situation",
            "unemployment",
            "jolts",
            "job openings",
            "ism manufacturing",
            "ism services",
        )
    ):
        return "high"
    if any(
        keyword in text
        for keyword in (
            "retail sales",
            "gross domestic product",
            "gdp",
            "international trade",
            "jobless claims",
            "treasury auction",
            "consumer sentiment",
            "speech",
            "testimony",
            "federal reserve",
        )
    ):
        return "medium"
    return "low"


def _impact_score(event_name: str, source: str, importance: str) -> int:
    text = f"{event_name} {source}".lower()
    if any(keyword in text for keyword in ("fomc", "federal open market", "fed chair", "powell")):
        return 10
    if any(keyword in text for keyword in ("cpi", "consumer price index", "nonfarm", "employment situation", "unemployment")):
        return 10
    if any(keyword in text for keyword in ("jolts", "job openings")):
        return 9
    if any(keyword in text for keyword in ("pce", "personal income and outlays", "ppi", "producer price index", "ism ")):
        return 8
    if any(keyword in text for keyword in ("retail sales", "monetary policy", "transmission of monetary policy")):
        return 8
    if any(keyword in text for keyword in ("international trade", "goods and services")):
        return 5
    if any(keyword in text for keyword in ("gdp", "gross domestic product", "jobless claims", "consumer sentiment", "treasury auction", "speech", "testimony")):
        return 5
    if importance == "medium":
        return 5
    if importance == "high":
        return 8
    return 1


def _event_window(
    event_date: str,
    target_date: date,
    market_session_date: date,
    prior_business_date: date,
    event_name: str,
    source: str,
) -> str:
    parsed = _parse_date(event_date)
    if parsed is None:
        return "missing_date"
    if parsed in {market_session_date, prior_business_date}:
        return "recent_session"
    if target_date <= parsed <= target_date + timedelta(days=UPCOMING_WATCH_WINDOW_DAYS):
        return "upcoming_this_week"
    if (
        target_date <= parsed <= target_date + timedelta(days=UPCOMING_MAJOR_EVENT_WINDOW_DAYS)
        and _is_next_major_event(event_name, source)
    ):
        return "next_major_event"
    return "out_of_window"


def _event_phase_from_window(event_window: str) -> str:
    if event_window == "recent_session":
        return "past_event"
    if event_window in {"upcoming_this_week", "next_major_event"}:
        return "upcoming_event"
    return "low_value_macro"


def _is_next_major_event(event_name: str, source: str) -> bool:
    if not source.startswith(("Federal Reserve", "BLS", "BEA")):
        return False
    text = f"{event_name} {source}".lower()
    return any(
        keyword in text
        for keyword in (
            "fomc",
            "federal open market",
            "fed chair",
            "powell",
            "cpi",
            "consumer price index",
            "pce",
            "personal income and outlays",
            "ppi",
            "producer price index",
            "nonfarm",
            "employment situation",
            "unemployment",
            "jolts",
            "job openings",
            "retail sales",
            "gross domestic product",
            "gdp",
            "ism manufacturing",
            "ism services",
            "international trade in goods and services",
        )
    )


def _driver_type(
    event_phase: str,
    importance: str,
    actual: Any,
    forecast: Any,
    previous: Any,
    event_name: str,
    source: str,
    event_window: str,
) -> str:
    if event_phase == "low_value_macro" or importance == "low":
        return "low_value_macro"
    has_actual_context = any(value not in (None, "") for value in (actual, forecast, previous))
    if event_phase == "past_event" and has_actual_context:
        return "direct_macro_driver"
    if _causal_bridge(event_name, source, event_phase, "context_macro_candidate", event_window):
        return "context_macro_candidate"
    return "low_value_macro"


def _causal_bridge(event_name: str, source: str, event_phase: str, driver_type: str, event_window: str) -> str:
    if driver_type == "low_value_macro":
        return ""
    label = _event_label(event_name, source)
    if event_phase == "upcoming_event":
        if event_window == "next_major_event":
            evidence = "event is the next major official macro watch point; do not treat it as a prior-session cause"
        else:
            evidence = "event is scheduled within the target watch window; do not treat it as a prior-session cause"
    else:
        evidence = "event was released by an official/public source near the target US session"
    if label in {"CPI", "PCE", "PPI"}:
        return f"{label} -> inflation expectations / Treasury yields -> Nasdaq 100 / growth stocks / semiconductors -> {evidence}"
    if label == "FOMC":
        return f"FOMC -> rate expectations / Treasury yields / dollar -> Nasdaq 100 / large tech / semiconductors -> {evidence}"
    if label == "Jobs":
        return f"labor market data -> Fed policy expectations / yields -> growth stocks / semiconductors -> {evidence}"
    if label == "ISM":
        return f"ISM data -> growth and inflation expectations -> Nasdaq 100 / semiconductors -> {evidence}"
    if label == "Treasury auction":
        return f"Treasury auction -> long-term yield pressure -> growth stock valuation sensitivity -> {evidence}"
    if label == "GDP":
        return f"GDP -> growth outlook / rate expectations -> Nasdaq 100 / cyclical tech demand -> {evidence}"
    if label == "Fed speech":
        return f"Fed speech -> rate-cut expectations / yields / dollar -> Nasdaq 100 / AI stocks / semiconductors -> {evidence}"
    if label == "Retail sales":
        return f"retail sales -> demand outlook / inflation expectations -> Nasdaq 100 / consumer tech -> {evidence}"
    if label == "Trade":
        return f"trade data -> global demand / dollar / supply-chain context -> Nasdaq 100 / semiconductors -> {evidence}"
    return f"{label} -> macro expectations -> Nasdaq 100 / SOX context -> {evidence}"


def _filter_reason(event_phase: str, driver_type: str, event_name: str, source: str, event_window: str) -> str:
    if driver_type == "direct_macro_driver":
        return "Potential direct macro driver; use only if market data confirms a rate/yield/risk reaction."
    if driver_type == "context_macro_candidate":
        if event_phase == "upcoming_event":
            if event_window == "next_major_event":
                return "Next major scheduled macro event; use as a watch calendar item, not as a cause of the previous US session."
            return "Watch point only; do not claim it caused the previous US session."
        return "Context only unless actual/forecast/previous and market reaction support causality."
    if event_phase == "low_value_macro":
        if event_window == "missing_date":
            return "Missing a usable event date."
        return "Outside the practical past/upcoming watch window or not a major official macro event."
    return "Weak Nasdaq/SOX/semiconductor causal bridge."


def _market_expectation(event_name: str, source: str) -> str | None:
    label = _event_label(event_name, source)
    expectations = {
        "CPI": "Markets look for confirmation that inflation is cooling enough to keep Fed easing expectations intact.",
        "PCE": "Markets watch whether the Fed's preferred inflation gauge supports rate-cut expectations.",
        "PPI": "Markets watch whether pipeline inflation pressure could affect bond yields.",
        "FOMC": "Markets watch the Fed's timing and tone on future rate cuts.",
        "Jobs": "Markets watch whether labor strength keeps yields high or weakness supports easing expectations.",
        "ISM": "Markets watch whether growth and price components change the soft-landing narrative.",
        "Treasury auction": "Markets watch whether demand is strong enough to avoid upward pressure on long-term yields.",
        "GDP": "Markets watch whether growth is resilient without forcing a more hawkish rate outlook.",
        "Fed speech": "Markets watch whether Fed speakers shift rate-cut expectations, yields, or dollar direction.",
        "Retail sales": "Markets watch whether consumer demand changes growth and inflation expectations.",
        "Trade": "Markets watch whether global demand and trade flows add pressure to the growth or dollar narrative.",
    }
    return expectations.get(label)


def _why_viewer_should_care(event_name: str, source: str) -> str:
    label = _event_label(event_name, source)
    if label in {"CPI", "PCE", "PPI", "FOMC", "Fed speech"}:
        return "Rate expectations and Treasury yields can quickly change valuation pressure on Nasdaq, AI, and semiconductor shares."
    if label == "Jobs":
        return "Labor data can move yields and the Fed outlook, which affects growth-stock risk appetite."
    if label == "Treasury auction":
        return "Auction demand can affect long-term yields, an important pressure point for growth stocks."
    if label == "GDP":
        return "Growth data helps separate soft-landing optimism from demand-slowdown risk for large tech."
    if label == "ISM":
        return "ISM can connect macro demand and inflation pressure to cyclical tech and semiconductor sentiment."
    if label == "Retail sales":
        return "Retail sales can move growth, inflation, and rate expectations that affect consumer tech and Nasdaq risk appetite."
    if label == "Trade":
        return "Trade data can add context for global demand, the dollar, and supply-chain-sensitive semiconductor names."
    return "Use only if it reinforces stronger market, rate, or sector evidence."


def _event_label(event_name: str, source: str) -> str:
    text = f"{event_name} {source}".lower()
    if "fomc" in text or "federal open market" in text:
        return "FOMC"
    if "consumer price index" in text or "cpi" in text:
        return "CPI"
    if "personal income and outlays" in text or "pce" in text:
        return "PCE"
    if "producer price index" in text or "ppi" in text:
        return "PPI"
    if "nonfarm" in text or "employment situation" in text or "unemployment" in text or "jolts" in text or "job openings" in text:
        return "Jobs"
    if "ism manufacturing" in text or "ism services" in text or "ism " in text:
        return "ISM"
    if "retail sales" in text:
        return "Retail sales"
    if "treasury auction" in text:
        return "Treasury auction"
    if "gdp" in text or "gross domestic product" in text:
        return "GDP"
    if "international trade" in text or "goods and services" in text:
        return "Trade"
    if "speech" in text or "testimony" in text or "federal reserve" in text:
        return "Fed speech"
    return "Macro event"


def _confidence(source: str, event_date: str, url: str) -> str:
    if source.startswith(("Federal Reserve", "BLS", "BEA")) and event_date and url:
        return "high"
    if source.startswith("RSS News") and event_date and url:
        return "medium"
    return "unknown"


def _status(items: list[dict[str, Any]], errors: list[dict[str, str]]) -> str:
    if items and errors:
        return "partial"
    if items:
        return "ok"
    if errors:
        return "error"
    return "empty"


def _source_error(source: str, url: str, exc: Exception) -> dict[str, str]:
    return {"source": source, "url": url, "error": f"{type(exc).__name__}: {exc}"}


def _dedupe_events(items) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        key = (
            str(item.get("event_name", "")).strip().lower(),
            str(item.get("event_date", "")).strip(),
            str(item.get("url", "")).strip().lower(),
        )
        if not key[0] or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.lines.append(text)


def _date_from_rss(value: str) -> str:
    parsed = _parse_rss_datetime(value)
    return parsed.date().isoformat() if parsed else ""


def _time_from_rss(value: str) -> str:
    parsed = _parse_rss_datetime(value)
    return parsed.astimezone(timezone.utc).strftime("%H:%M UTC") if parsed else ""


def _parse_rss_datetime(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _date_from_any(value: Any) -> str:
    text = strip_html(value or "")
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.date().isoformat()
    except ValueError:
        pass
    parsed_rss = _parse_rss_datetime(strip_html(value or ""))
    if parsed_rss:
        return parsed_rss.date().isoformat()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(strip_html(value or ""), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _normalize_date_text(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return _date_from_any(value) or strip_html(value or "")


def _parse_date(value: Any) -> date | None:
    text = strip_html(value or "")
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _previous_us_market_session(target_date: date) -> date:
    candidate = target_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _previous_business_day(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _is_month_day(value: str) -> bool:
    return re.fullmatch(r"(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}", value) is not None


def _is_time(value: str) -> bool:
    return re.fullmatch(r"\d{1,2}:\d{2} [AP]M", value) is not None


def _month_day_to_date(value: str, target_date: date) -> date:
    month_name, day_text = value.split()
    month = MONTHS[month_name.lower()]
    day = int(day_text)
    candidate = date(target_date.year, month, day)
    if candidate < target_date - timedelta(days=45):
        candidate = date(target_date.year + 1, month, day)
    return candidate


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
