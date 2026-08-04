from __future__ import annotations

import html
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from nasdaq_cafe.config import WATCHLIST


WATCHLIST_SET = set(WATCHLIST)
REGULAR_SESSION_FIELDS = (
    "last",
    "open",
    "high",
    "low",
    "prev_close",
    "change_value",
    "change_percentage",
    "volume",
    "turnover",
    "timestamp",
)
EXTENDED_SESSION_FIELDS = (
    "last",
    "high",
    "low",
    "prev_close",
    "timestamp",
    "turnover",
    "volume",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in (
            "items",
            "data",
            "results",
            "news",
            "quotes",
            "movers",
            "market_movers",
            "securities",
            "list",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = ensure_list(value)
                if nested:
                    return nested
        values = list(payload.values())
        if values and all(isinstance(value, dict) for value in values):
            return values
    return []


def first_value(item: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def strip_html(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clamp_text(value: Any, limit: int = 280) -> str:
    text = strip_html(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_symbol(value: Any) -> str:
    symbol = "" if value is None else str(value).upper().strip()
    symbol = symbol.replace(".US", "")
    symbol = symbol.replace("US.", "")
    symbol = symbol.lstrip("$")
    return symbol


def extract_tickers(*parts: Any) -> list[str]:
    text = " ".join("" if part is None else str(part) for part in parts)
    found: list[str] = []
    for ticker in WATCHLIST:
        if re.search(rf"(?<![A-Z0-9]){re.escape(ticker)}(?:\.US)?(?![A-Z0-9])", text):
            found.append(ticker)
    return found


def normalize_news_item(item: dict[str, Any], fallback_source: str = "") -> dict[str, Any]:
    title = first_value(item, ["title", "headline", "name"], "")
    source = first_value(item, ["source", "publisher", "site", "provider"], fallback_source)
    published_at = first_value(item, ["published_at", "published", "pubDate", "datetime", "time"], "")
    url = first_value(item, ["url", "link", "href"], "")
    snippet = first_value(item, ["snippet", "summary", "description", "abstract", "content"], "")
    tickers = item.get("related_tickers")
    if not isinstance(tickers, list):
        tickers = extract_tickers(title, snippet)
    confidence = item.get("confidence") or ("medium" if source and title else "unknown")
    return {
        "title": strip_html(title),
        "source": strip_html(source),
        "published_at": strip_html(published_at),
        "url": strip_html(url),
        "snippet": clamp_text(snippet),
        "related_tickers": [ticker for ticker in tickers if ticker in WATCHLIST_SET],
        "why_relevant": strip_html(item.get("why_relevant") or ""),
        "confidence": strip_html(confidence),
    }


def normalize_quote(item: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(first_value(item, ["symbol", "ticker", "code", "security_code"], ""))
    price = first_value(item, ["last", "last_price", "price", "close", "latest_price"])
    change_percent = first_value(
        item,
        ["change_percent", "change_percentage", "percent_change", "change_pct", "pct_change"],
    )
    change = first_value(item, ["change", "change_value", "net_change"])
    active_session = first_value(item, ["active_session", "session", "market_session"], "unknown")
    active_session = str(active_session).strip() or "unknown"
    normalized = {
        "ticker": symbol,
        "source_symbol": first_value(item, ["symbol", "ticker", "code", "security_code"], ""),
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "session": active_session,
        "active_session": active_session,
        "raw_source": "Longbridge",
        "regular": _regular_session(item),
        "pre_market": _named_session(item, "pre_market", aliases=("premarket",)),
        "post_market": _named_session(item, "post_market", aliases=("after_hours",)),
        "overnight": _named_session(item, "overnight"),
    }
    for field in ("open", "high", "low", "volume", "turnover", "timestamp", "prev_close"):
        if field in item:
            normalized[field] = deepcopy(item.get(field))
    return normalized


def _regular_session(item: dict[str, Any]) -> dict[str, Any]:
    data = {field: deepcopy(item[field]) for field in REGULAR_SESSION_FIELDS if field in item}
    return {
        "availability": "available" if _has_available_value(data) else "not_available",
        "raw_state": "object" if data else "missing",
        "data": data if data else None,
        "field_states": {field: _field_state(item, field) for field in REGULAR_SESSION_FIELDS},
    }


def _named_session(
    item: dict[str, Any],
    name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    source_key = next((key for key in (name, *aliases) if key in item), None)
    if source_key is None:
        return {
            "availability": "not_available",
            "raw_state": "missing",
            "data": None,
            "field_states": {field: "missing" for field in EXTENDED_SESSION_FIELDS},
        }
    value = deepcopy(item.get(source_key))
    raw_state = _value_state(value)
    if isinstance(value, dict):
        field_states = {field: _field_state(value, field) for field in EXTENDED_SESSION_FIELDS}
    else:
        field_states = {field: "not_applicable" for field in EXTENDED_SESSION_FIELDS}
    return {
        "availability": "not_available" if raw_state in {"null", "empty_object", "empty_string"} else "available",
        "raw_state": raw_state,
        "data": value,
        "field_states": field_states,
    }


def _field_state(container: dict[str, Any], field: str) -> str:
    if field not in container:
        return "missing"
    return _value_state(container.get(field))


def _value_state(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object" if value else "empty_object"
    if isinstance(value, str) and value == "":
        return "empty_string"
    return "value"


def _has_available_value(data: dict[str, Any]) -> bool:
    return any(value is not None and value != "" for value in data.values())


def normalize_mover(item: dict[str, Any], fallback_source: str = "") -> dict[str, Any]:
    ticker = normalize_symbol(first_value(item, ["ticker", "symbol", "code", "security_code"], ""))
    name = first_value(item, ["name", "security_name", "company_name"], "")
    change_percent = first_value(item, ["change_percent", "percent_change", "change_pct", "pct_change"])
    price = first_value(item, ["last", "last_price", "price", "close", "latest_price"])
    reason = first_value(item, ["reason", "why_relevant", "description", "summary"], "")
    source = first_value(item, ["source", "provider"], fallback_source)
    session = first_value(item, ["session", "market_session"], "unknown")
    normalized = {
        "ticker": ticker,
        "name": strip_html(name),
        "price": price,
        "change_percent": change_percent,
        "session": session or "unknown",
        "reason": clamp_text(reason, 220),
        "source": strip_html(source or fallback_source),
        "confidence": "medium" if reason else "unknown",
        "category": strip_html(item.get("category") or ""),
        "market_cap": item.get("market_cap"),
        "volume": item.get("volume"),
        "average_volume": item.get("average_volume"),
    }
    if "active_session" in item:
        normalized["active_session"] = item.get("active_session")
    for session_name in ("regular", "pre_market", "post_market", "overnight"):
        if session_name in item:
            normalized[session_name] = deepcopy(item.get(session_name))
    return normalized


def dedupe_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        title = item.get("title", "").strip().lower()
        url = item.get("url", "").strip().lower()
        key = (title, url)
        if not title or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output
