from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from nasdaq_cafe.cache import load_or_fetch, write_json
from nasdaq_cafe.config import RunConfig, WATCHLIST, missing
from nasdaq_cafe.processing.normalize import normalize_mover, utc_now_iso


LARGE_CAPS = set(WATCHLIST)
YAHOO_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
NASDAQ_100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

REQUIRED_CATEGORIES = [
    "Top Gainers",
    "Top Losers",
    "Most Active",
    "Premarket Movers",
    "After Hours Movers",
    "Nasdaq 100 movers",
    "SOX component movers",
]

YAHOO_CATEGORIES = {
    "Top Gainers": "day_gainers",
    "Top Losers": "day_losers",
    "Most Active": "most_actives",
}

SOX_COMPONENTS = [
    "ADI.US",
    "AMAT.US",
    "ASML.US",
    "ENTG.US",
    "GFS.US",
    "INTC.US",
    "KLAC.US",
    "LRCX.US",
    "MCHP.US",
    "MPWR.US",
    "MRVL.US",
    "MU.US",
    "NXPI.US",
    "ON.US",
    "QCOM.US",
    "SWKS.US",
    "TER.US",
    "TXN.US",
]

THEME_KEYWORDS = [
    "AI",
    "artificial intelligence",
    "semiconductor",
    "chip",
    "cloud",
    "EV",
    "electric vehicle",
    "advertising",
    "software",
]

MIN_PRICE = 5.0
MIN_MARKET_CAP = 2_000_000_000
LARGE_CAP_MARKET_CAP = 50_000_000_000


def collect_market_movers(
    config: RunConfig,
    longbridge_movers: list[dict[str, Any]],
    longbridge_quote_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = config.raw_dir / "market_movers.json"
    quote_inputs = longbridge_quote_inputs or []

    def fetcher() -> dict[str, Any]:
        categories: dict[str, list[dict[str, Any]]] = {category: [] for category in REQUIRED_CATEGORIES}
        missing_categories: list[dict[str, str]] = []
        source_errors: list[dict[str, str]] = []

        for category, scr_id in YAHOO_CATEGORIES.items():
            try:
                categories[category] = _fetch_yahoo_screener(category, scr_id)
            except Exception as exc:
                source_errors.append(_source_error(category, "Yahoo Finance", exc))

        try:
            longbridge_top_movers = _fetch_longbridge_top_movers()
        except Exception as exc:
            longbridge_top_movers = []
            source_errors.append(_source_error("Longbridge Top Movers", "Longbridge top-movers", exc))
        for item in longbridge_top_movers:
            change = _as_float(item.get("change_percent"))
            category = "Top Losers" if change is not None and change < 0 else "Top Gainers"
            categories.setdefault(category, []).append(item)

        for unavailable in ["Premarket Movers", "After Hours Movers"]:
            missing_categories.append(
                {
                    "category": unavailable,
                    "reason": "No stable free RSS/JSON source is configured for this category in Phase 1.9.",
                    "severity": "low",
                }
            )

        try:
            categories["Nasdaq 100 movers"] = _fetch_nasdaq_100_movers()
        except Exception as exc:
            source_errors.append(_source_error("Nasdaq 100 movers", "Nasdaq Market Activity", exc))

        try:
            categories["SOX component movers"] = _fetch_sox_component_movers()
        except Exception as exc:
            source_errors.append(_source_error("SOX component movers", "Longbridge quote", exc))

        longbridge_items = [_normalize_longbridge_top_mover(item) for item in longbridge_movers]
        for item in longbridge_items:
            category = "Top Gainers" if _as_float(item.get("change_percent")) and _as_float(item.get("change_percent")) > 0 else "Top Losers"
            categories.setdefault(category, []).append(item)

        candidates = _select_candidates(categories)
        for category in REQUIRED_CATEGORIES:
            if category not in categories or not categories[category]:
                if not any(item.get("category") == category for item in missing_categories):
                    missing_categories.append(
                        {
                            "category": category,
                            "reason": "No usable rows were returned for this category.",
                            "severity": "low",
                        }
                    )

        return {
            "source": "Market Movers",
            "schema_version": 2,
            "status": "ok" if candidates else "empty",
            "generated_at": utc_now_iso(),
            "categories": categories,
            "longbridge_top_movers": longbridge_top_movers,
            "longbridge_quote_inputs": quote_inputs,
            "missing_categories": missing_categories,
            "source_errors": source_errors,
            "items": candidates,
            "notes": "Phase 1.9 uses read-only free market mover sources and does not fetch full articles.",
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    # Session inputs are refreshed from the normalized Longbridge quote cache on
    # every run, even when the other market-mover providers use same-day cache.
    if payload.get("schema_version") != 2 or payload.get("longbridge_quote_inputs") != quote_inputs:
        payload["schema_version"] = 2
        payload["longbridge_quote_inputs"] = quote_inputs
        write_json(path, payload)
        cache_used = False
    items = [normalize_mover(item, item.get("source", "Market Movers")) for item in payload.get("items", [])]
    missing_data = []
    for category in payload.get("missing_categories", []):
        missing_data.append(
            missing(
                "Market Movers",
                f"{category.get('category')}: {category.get('reason')}",
                category.get("severity", "low"),
            )
        )
    for error in payload.get("source_errors", []):
        missing_data.append(
            missing(
                "Market Movers",
                f"{error.get('category')}: {error.get('source')} failed: {error.get('error')}",
                "low",
            )
        )
    if not items:
        missing_data.append(
            missing(
                "Market Movers",
                "No market movers were available from Longbridge raw JSON or cached Phase 1 data.",
                "low",
            )
        )
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "market_movers": items,
        "longbridge_quote_inputs": payload.get("longbridge_quote_inputs", []),
        "raw": payload,
        "missing_data": missing_data,
    }


def _fetch_yahoo_screener(category: str, scr_id: str) -> list[dict[str, Any]]:
    import requests

    response = requests.get(
        YAHOO_SCREENER_URL,
        params={"scrIds": scr_id, "count": 50, "formatted": "false"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    quotes = (((payload.get("finance") or {}).get("result") or [{}])[0]).get("quotes") or []
    return [_normalize_yahoo_quote(item, category) for item in quotes]


def _normalize_yahoo_quote(item: dict[str, Any], category: str) -> dict[str, Any]:
    ticker = str(item.get("symbol", "")).upper()
    change_percent = item.get("regularMarketChangePercent")
    market_cap = _as_float(item.get("marketCap"))
    volume = _as_float(item.get("regularMarketVolume"))
    avg_volume = _as_float(item.get("averageDailyVolume3Month"))
    name = item.get("shortName") or item.get("longName") or item.get("displayName") or ""
    reason = _reason_for_mover(category, change_percent, market_cap, volume, avg_volume, name)
    return {
        "ticker": ticker,
        "name": name,
        "price": item.get("regularMarketPrice"),
        "change": item.get("regularMarketChange"),
        "change_percent": change_percent,
        "market_cap": market_cap,
        "volume": volume,
        "average_volume": avg_volume,
        "session": "regular",
        "category": category,
        "reason": reason,
        "source": "Yahoo Finance",
        "confidence": "medium" if reason else "unknown",
    }


def _fetch_nasdaq_100_movers() -> list[dict[str, Any]]:
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
    last_rows: list[dict[str, Any]] = []
    for attempt in range(3):
        response = requests.get(NASDAQ_100_URL, headers=headers, timeout=20)
        response.raise_for_status()
        rows = ((((response.json().get("data") or {}).get("data") or {}).get("rows")) or [])
        if rows:
            return [_normalize_nasdaq_row(row) for row in rows]
        last_rows = rows
        if attempt < 2:
            time.sleep(1)
    return [_normalize_nasdaq_row(row) for row in last_rows]


def _normalize_nasdaq_row(row: dict[str, Any]) -> dict[str, Any]:
    change_percent = _parse_percent(row.get("percentageChange"))
    net_change = _parse_money(row.get("netChange"))
    if change_percent is not None and net_change is not None:
        net_change = abs(net_change) * (-1 if change_percent < 0 else 1)
    ticker = str(row.get("symbol", "")).upper()
    name = row.get("companyName", "")
    return {
        "ticker": ticker,
        "name": name,
        "price": _parse_money(row.get("lastSalePrice")),
        "change": net_change,
        "change_percent": change_percent,
        "market_cap": _parse_number(row.get("marketCap")),
        "volume": None,
        "average_volume": None,
        "session": "regular",
        "category": "Nasdaq 100 movers",
        "reason": _reason_for_mover("Nasdaq 100 movers", change_percent, _parse_number(row.get("marketCap")), None, None, name),
        "source": "Nasdaq Market Activity",
        "confidence": "medium",
    }


def _fetch_sox_component_movers() -> list[dict[str, Any]]:
    executable = _find_longbridge_executable()
    if executable is None:
        raise RuntimeError("Longbridge CLI executable was not found.")
    completed = subprocess.run(
        [str(executable), "quote", *SOX_COMPONENTS, "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Longbridge quote failed.").strip())
    payload = json.loads(completed.stdout or "[]")
    output = []
    for item in payload:
        ticker = str(item.get("symbol", "")).replace(".US", "").upper()
        change_percent = _as_float(item.get("change_percentage"))
        output.append(
            {
                "ticker": ticker,
                "name": item.get("symbol", ""),
                "price": item.get("last"),
                "change": item.get("change_value"),
                "change_percent": change_percent,
                "market_cap": None,
                "volume": item.get("volume"),
                "average_volume": None,
                "session": "regular",
                "category": "SOX component movers",
                "reason": _reason_for_mover("SOX component movers", change_percent, None, item.get("volume"), None, "semiconductor"),
                "source": "Longbridge quote",
                "confidence": "medium",
            }
        )
    return output


def _fetch_longbridge_top_movers() -> list[dict[str, Any]]:
    executable = _find_longbridge_executable()
    if executable is None:
        raise RuntimeError("Longbridge CLI executable was not found.")
    completed = subprocess.run(
        [str(executable), "top-movers", "--market", "US", "--sort", "change", "--count", "30", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Longbridge top-movers failed.").strip())
    payload = json.loads(completed.stdout or "{}")
    return [_normalize_longbridge_top_mover(item) for item in payload.get("events", [])]


def _normalize_longbridge_top_mover(item: dict[str, Any]) -> dict[str, Any]:
    stock = item.get("stock") or item
    ticker = str(stock.get("symbol") or stock.get("code") or "").replace(".US", "").upper()
    labels = stock.get("labels") or []
    label_text = ", ".join(str(label) for label in labels)
    change_percent = _as_float(stock.get("change"))
    if change_percent is not None:
        change_percent *= 100
    return {
        "ticker": ticker,
        "name": stock.get("name") or stock.get("full_name") or "",
        "price": stock.get("last_done"),
        "change": stock.get("change"),
        "change_percent": change_percent,
        "market_cap": None,
        "volume": None,
        "average_volume": None,
        "session": "regular",
        "category": "Longbridge Top Movers",
        "reason": f"{item.get('alert_reason', '')}; labels: {label_text}".strip("; "),
        "source": "Longbridge top-movers",
        "confidence": "medium",
    }


def _select_candidates(categories: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category in REQUIRED_CATEGORIES:
        rows = sorted(categories.get(category, []), key=lambda item: abs(_as_float(item.get("change_percent")) or 0), reverse=True)
        for item in rows:
            ticker = str(item.get("ticker", "")).upper()
            if not ticker or ticker in seen or ticker in LARGE_CAPS:
                continue
            if not _passes_filter(item):
                continue
            scored = _score_mover(item)
            scored["category"] = category
            seen.add(ticker)
            candidates.append(scored)
            if len([candidate for candidate in candidates if candidate.get("category") == category]) >= 5:
                break
    return candidates[:30]


def _passes_filter(item: dict[str, Any]) -> bool:
    ticker = str(item.get("ticker", "")).upper()
    change_percent = _as_float(item.get("change_percent"))
    reason = str(item.get("reason", ""))
    price = _as_float(item.get("price"))
    market_cap = _as_float(item.get("market_cap"))
    category = str(item.get("category", ""))
    if price is not None and price < MIN_PRICE:
        return False
    if market_cap is not None and market_cap < MIN_MARKET_CAP:
        return False
    if change_percent is None:
        return bool(reason and ticker)
    threshold = 3.0 if ticker in LARGE_CAPS or (market_cap and market_cap >= LARGE_CAP_MARKET_CAP) else 5.0
    if category in {"Premarket Movers", "After Hours Movers"}:
        threshold = 3.0
    has_volume_spike = _volume_spike(item)
    is_large_or_index = bool(market_cap and market_cap >= LARGE_CAP_MARKET_CAP) or category in {"Nasdaq 100 movers", "SOX component movers"}
    if abs(change_percent) >= threshold and (is_large_or_index or _has_theme(reason) or has_volume_spike):
        return True
    return _has_theme(reason) and abs(change_percent) >= 2.0


def _score_mover(item: dict[str, Any]) -> dict[str, Any]:
    scored = dict(item)
    change_percent = _as_float(item.get("change_percent"))
    scored["score"] = abs(change_percent or 0.0)
    if not scored.get("confidence"):
        scored["confidence"] = "medium" if scored.get("reason") else "unknown"
    return scored


def _reason_for_mover(category: str, change_percent: Any, market_cap: Any, volume: Any, avg_volume: Any, name: str) -> str:
    reasons = [category]
    pct = _as_float(change_percent)
    cap = _as_float(market_cap)
    vol = _as_float(volume)
    avg = _as_float(avg_volume)
    if pct is not None:
        reasons.append(f"price move {pct:.2f}%")
    if cap is not None and cap >= LARGE_CAP_MARKET_CAP:
        reasons.append("large-cap candidate")
    if vol is not None and avg is not None and avg > 0 and vol >= avg * 1.5:
        reasons.append("volume above usual level")
    theme = _theme_from_text(name)
    if theme:
        reasons.append(f"theme: {theme}")
    return "; ".join(reasons)


def _volume_spike(item: dict[str, Any]) -> bool:
    vol = _as_float(item.get("volume"))
    avg = _as_float(item.get("average_volume"))
    return bool(vol is not None and avg is not None and avg > 0 and vol >= avg * 1.5)


def _theme_from_text(text: str) -> str:
    lower = str(text).lower()
    matches = []
    for keyword in THEME_KEYWORDS:
        if keyword.lower() in lower:
            matches.append(keyword)
    return ", ".join(matches)


def _has_theme(text: str) -> bool:
    return bool(_theme_from_text(text))


def _source_error(category: str, source: str, exc: Exception) -> dict[str, str]:
    return {"category": category, "source": source, "error": f"{type(exc).__name__}: {exc}"}


def _find_longbridge_executable() -> str | None:
    discovered = shutil.which("longbridge")
    if discovered:
        return discovered
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidate = os.path.join(local_app_data, "Programs", "longbridge", "longbridge.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def _parse_money(value: Any) -> float | None:
    if value is None:
        return None
    return _as_float(str(value).replace("$", "").replace(",", ""))


def _parse_percent(value: Any) -> float | None:
    return _as_float(str(value).replace("%", "").replace("+", ""))


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    return _as_float(str(value).replace(",", ""))


def _as_float(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().replace("%", "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
