from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from nasdaq_cafe.trading_calendar import resolve_research_trading_session

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "output"

WATCHLIST = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSM", "AMD", "TSLA"]
LONGBRIDGE_SYMBOLS = {ticker: f"{ticker}.US" for ticker in WATCHLIST}
LONGBRIDGE_FIXED_SYMBOLS = [".IXIC.US", "QQQ.US", "SMH.US", "SOXX.US", *LONGBRIDGE_SYMBOLS.values()]

MARKET_TARGETS = ["NASDAQ", "SOX", "DGS10", "USDJPY"]

RSS_SOURCES = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "NASDAQ": "https://www.nasdaq.com/feed/rssoutbound?category=Stocks",
    "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
}

SERPAPI_SEARCH_QUERIES = [
    "Reuters Nasdaq tech stocks today",
    "Reuters semiconductor stocks today",
]

TAVILY_SEARCH_QUERIES = [
    "Nasdaq tech stocks why moved today",
    "semiconductor stocks Treasury yields tech stocks",
]

SEARCH_QUERIES = SERPAPI_SEARCH_QUERIES + TAVILY_SEARCH_QUERIES


@dataclass(frozen=True)
class RunConfig:
    target_date: str
    refresh: bool
    output_dir: Path
    raw_dir: Path
    env: dict[str, str]
    research_trading_date: str = ""
    research_trading_calendar: str = "NYSE"
    research_trading_market_open: str = ""
    research_trading_market_close: str = ""
    research_trading_is_half_day: bool = False


def load_environment() -> dict[str, str]:
    env_path = ROOT_DIR / ".env"
    file_env = _read_env_file(env_path)
    if load_dotenv is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    elif env_path.exists():
        _load_env_file(env_path)
    keys = [
        "FRED_API_KEY",
        "SERPAPI_API_KEY",
        "TAVILY_API_KEY",
        "LONGBRIDGE_APP_KEY",
        "LONGBRIDGE_APP_SECRET",
        "LONGBRIDGE_ACCESS_TOKEN",
        "FMP_API_KEY",
        "SEC_USER_AGENT",
        "NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS",
        "NASDAQ_CAFE_FULLTEXT_RUNTIME_BUDGET_SECONDS",
        "NASDAQ_CAFE_FULLTEXT_WORKERS",
        "NASDAQ_CAFE_REQUEST_TIMEOUT_SECONDS",
        "NASDAQ_CAFE_MAX_ARTICLE_BYTES",
        "NASDAQ_CAFE_TAVILY_EXTRACT_BASIC_LIMIT",
        "NASDAQ_CAFE_TAVILY_EXTRACT_ADVANCED_LIMIT",
        "NASDAQ_CAFE_FULLTEXT_DISALLOWED_DOMAINS",
        "NASDAQ_CAFE_FMP_NEWS_LIMIT",
        "NASDAQ_CAFE_FMP_COMPANY_LIMIT",
        "NASDAQ_CAFE_SEC_LOOKBACK_DAYS",
        "NASDAQ_CAFE_SEC_MAX_FILINGS_PER_COMPANY",
    ]
    # OS values always win. The project .env only fills missing process values.
    return {key: os.getenv(key, file_env.get(key, "")) for key in keys}


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_target_date(value: str | None) -> str:
    if not value or value.lower() == "today":
        return date.today().isoformat()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("--date must be 'today' or YYYY-MM-DD") from exc


def build_config(target_date: str | None, refresh: bool) -> RunConfig:
    normalized_date = parse_target_date(target_date)
    research_session = resolve_research_trading_session(normalized_date)
    output_dir = OUTPUT_DIR / normalized_date
    raw_dir = output_dir / "raw"
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return RunConfig(
        target_date=normalized_date,
        refresh=refresh,
        output_dir=output_dir,
        raw_dir=raw_dir,
        env=load_environment(),
        research_trading_date=research_session.session_date,
        research_trading_calendar=research_session.calendar,
        research_trading_market_open=research_session.market_open_utc,
        research_trading_market_close=research_session.market_close_utc,
        research_trading_is_half_day=research_session.is_half_day,
    )


def missing(source: str, reason: str, severity: str = "medium") -> dict[str, str]:
    return {"source": source, "reason": reason, "severity": severity}


def unique_missing(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in items:
        key = (item.get("source", ""), item.get("reason", ""), item.get("severity", ""))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
