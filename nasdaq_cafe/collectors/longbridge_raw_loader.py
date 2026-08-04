from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.config import WATCHLIST, missing
from nasdaq_cafe.processing.normalize import (
    ensure_list,
    normalize_mover,
    normalize_news_item,
    normalize_quote,
    utc_now_iso,
)
from nasdaq_cafe.processing.relevance import explain_relevance


RAW_FILES = {
    "quotes": "longbridge_quotes.json",
    "news": "longbridge_news.json",
    "market_movers": "longbridge_market_movers.json",
}


def load_longbridge_raw(raw_dir: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    missing_files: list[str] = []

    for key, file_name in RAW_FILES.items():
        path = raw_dir / file_name
        payload = read_json(path)
        if payload is None:
            missing_files.append(file_name)
            loaded[key] = None
        else:
            loaded[key] = payload

    missing_data = _missing_entries(missing_files, env)

    quotes = _normalize_quotes(loaded.get("quotes"))
    normalized_quotes_payload = {
        "source": "Longbridge",
        "kind": "normalized_quotes",
        "schema_version": 2,
        "generated_at": utc_now_iso(),
        "items": quotes,
    }
    write_json(raw_dir / "longbridge_quotes_normalized.json", normalized_quotes_payload)
    market_mover_inputs = _quote_market_mover_inputs(quotes)
    news_items = _normalize_news(loaded.get("news"))
    movers = _normalize_movers(loaded.get("market_movers"))

    status = "loaded" if any(loaded.values()) else "missing raw export"
    return {
        "status": status,
        "raw": loaded,
        "files_loaded": [name for name in RAW_FILES.values() if (raw_dir / name).exists()],
        "missing_files": missing_files,
        "market_data": _extract_market_data(quotes),
        "watchlist": _extract_watchlist(quotes),
        "quotes": quotes,
        "market_mover_inputs": market_mover_inputs,
        "normalized_quotes_raw_path": str(raw_dir / "longbridge_quotes_normalized.json"),
        "news_items": news_items,
        "market_movers": movers,
        "missing_data": missing_data,
    }


def _missing_entries(missing_files: list[str], env: dict[str, str] | None = None) -> list[dict[str, str]]:
    if not missing_files:
        return []
    if set(missing_files) == set(RAW_FILES.values()):
        has_sdk_credentials = bool(
            env
            and env.get("LONGBRIDGE_APP_KEY")
            and env.get("LONGBRIDGE_APP_SECRET")
            and env.get("LONGBRIDGE_ACCESS_TOKEN")
        )
        if not has_sdk_credentials:
            return [
                missing(
                    "Longbridge",
                    "Longbridge CLI / plugin unavailable. SDK credentials not provided yet.",
                    "medium",
                )
            ]
        return [
            missing(
                "Longbridge",
                "Longbridge raw JSON not provided yet. SDK credentials are present, but SDK quote fetch is not enabled in Phase 1.6.",
                "medium",
            )
        ]
    labels = {
        "longbridge_quotes.json": "Quote raw JSON is missing.",
        "longbridge_news.json": "Security news raw JSON is missing.",
        "longbridge_market_movers.json": "Market movers raw JSON is missing.",
    }
    return [
        missing("Longbridge", labels.get(file_name, f"{file_name} is missing."), "low")
        for file_name in missing_files
    ]


def _normalize_quotes(payload: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in ensure_list(payload):
        if isinstance(item, dict):
            output.append(normalize_quote(item))
    return output


def _normalize_news(payload: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in ensure_list(payload):
        if not isinstance(item, dict):
            continue
        normalized = normalize_news_item(item, "Longbridge")
        normalized["why_relevant"] = normalized["why_relevant"] or explain_relevance(normalized)
        output.append(normalized)
    return output


def _normalize_movers(payload: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in ensure_list(payload):
        if isinstance(item, dict):
            output.append(normalize_mover(item, "Longbridge"))
    return [item for item in output if item.get("ticker")]


def _quote_market_mover_inputs(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for quote in quotes:
        regular = quote.get("regular") if isinstance(quote.get("regular"), dict) else {}
        regular_data = regular.get("data") if isinstance(regular.get("data"), dict) else {}
        output.append(
            {
                "ticker": quote.get("ticker", ""),
                "source_symbol": quote.get("source_symbol", ""),
                "name": quote.get("source_symbol", ""),
                "price": quote.get("price"),
                "change": quote.get("change"),
                "change_percent": quote.get("change_percent"),
                "volume": regular_data.get("volume") if "volume" in regular_data else None,
                "turnover": regular_data.get("turnover") if "turnover" in regular_data else None,
                "session": quote.get("active_session", "unknown"),
                "active_session": quote.get("active_session", "unknown"),
                "regular": deepcopy(quote.get("regular")),
                "pre_market": deepcopy(quote.get("pre_market")),
                "post_market": deepcopy(quote.get("post_market")),
                "overnight": deepcopy(quote.get("overnight")),
                "category": "Longbridge Quote Input",
                "reason": "",
                "source": "Longbridge quote",
                "confidence": "unknown",
            }
        )
    return output


def _extract_market_data(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    market_data = {"NASDAQ": None, "SOX": None, "USDJPY": None}
    for quote in quotes:
        symbol = str(quote.get("source_symbol") or quote.get("ticker") or "").upper()
        if symbol in {"NASDAQ", "IXIC", ".IXIC", ".IXIC.US", "^IXIC", "COMP"}:
            market_data["NASDAQ"] = quote
        elif symbol in {"SOX", "SOXX", "SOXX.US", ".SOX", "^SOX"}:
            market_data["SOX"] = quote
        elif symbol in {"USDJPY", "USD/JPY", "JPY=X"}:
            market_data["USDJPY"] = quote
    return market_data


def _extract_watchlist(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {quote.get("ticker"): quote for quote in quotes if quote.get("ticker") in WATCHLIST}
    return [
        by_ticker.get(ticker)
        or {
            "ticker": ticker,
            "source_symbol": f"{ticker}.US",
            "price": None,
            "change": None,
            "change_percent": None,
            "session": "unknown",
            "raw_source": "Longbridge",
        }
        for ticker in WATCHLIST
    ]
