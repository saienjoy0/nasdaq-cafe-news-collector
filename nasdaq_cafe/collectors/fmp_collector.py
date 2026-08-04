from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.config import RunConfig, WATCHLIST, missing
from nasdaq_cafe.processing.normalize import utc_now_iso


FMP_BASE_URL = "https://financialmodelingprep.com/stable"
INVALID_KEY_VALUES = {"", "todo", "dummy", "test", "none", "null", "your_fmp_api_key"}


def collect_fmp(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "fmp_results.json"
    api_key = str(config.env.get("FMP_API_KEY") or "").strip()

    def fetcher() -> dict[str, Any]:
        if not _configured_key(api_key):
            return {
                "source": "FMP",
                "schema_version": 2,
                "status": "skipped",
                "reason": "FMP_API_KEY is not set.",
                "generated_at": utc_now_iso(),
                "endpoint_status": [],
                "stock_news": [],
                "press_releases": [],
                "profiles": {},
                "earnings": {},
                "transcript_availability": [],
                "transcript": {"status": "skipped", "reason": "FMP_API_KEY is not set."},
            }

        news_limit = _positive_int(config.env.get("NASDAQ_CAFE_FMP_NEWS_LIMIT"), 100, 250)
        company_limit = _positive_int(config.env.get("NASDAQ_CAFE_FMP_COMPANY_LIMIT"), len(WATCHLIST), len(WATCHLIST))
        endpoints = {
            "stock_news": ("/news/stock-latest", {"page": 0, "limit": news_limit}),
            "press_releases": ("/news/press-releases-latest", {"page": 0, "limit": news_limit}),
            "transcript_availability": ("/earning-call-transcript-latest", {"page": 0, "limit": news_limit}),
        }
        results: dict[str, Any] = {}
        endpoint_status: list[dict[str, Any]] = []
        for name, (endpoint, params) in endpoints.items():
            result = _fetch_endpoint(api_key, endpoint, params)
            results[name] = result["data"] if result["status"] == "ok" else []
            endpoint_status.append({key: value for key, value in result.items() if key != "data"} | {"name": name})

        profiles: dict[str, Any] = {}
        earnings: dict[str, Any] = {}
        symbols = WATCHLIST[:company_limit]
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {}
            for symbol in symbols:
                future_map[executor.submit(_fetch_endpoint, api_key, "/profile", {"symbol": symbol})] = ("profile", symbol)
                future_map[executor.submit(_fetch_endpoint, api_key, "/earnings", {"symbol": symbol, "limit": 4})] = ("earnings", symbol)
            for future in as_completed(future_map):
                kind, symbol = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "status": "error",
                        "http_status": None,
                        "reason": f"{type(exc).__name__}: {exc}",
                        "data": [],
                    }
                if kind == "profile":
                    profiles[symbol] = result["data"] if result["status"] == "ok" else []
                else:
                    earnings[symbol] = result["data"] if result["status"] == "ok" else []
                endpoint_status.append(
                    {key: value for key, value in result.items() if key != "data"}
                    | {"name": kind, "symbol": symbol}
                )

        ok_count = sum(1 for item in endpoint_status if item.get("status") == "ok")
        restricted_count = sum(1 for item in endpoint_status if item.get("status") == "restricted")
        transcript = _fetch_transcript(api_key, config.target_date)
        transcript_data = transcript.pop("data", [])
        transcript_raw_path = config.raw_dir / "fmp_transcript.json"
        write_json(
            transcript_raw_path,
            {
                **transcript,
                "data": transcript_data,
            },
        )
        transcript["raw_path"] = f"output/{config.target_date}/raw/fmp_transcript.json"
        endpoint_status.append(
            {
                "name": "earning_call_transcript",
                "status": transcript.get("status"),
                "http_status": transcript.get("http_status"),
                "reason": transcript.get("reason", ""),
            }
        )
        ok_count = sum(1 for item in endpoint_status if item.get("status") in {"ok", "success"})
        restricted_count = sum(1 for item in endpoint_status if item.get("status") == "restricted")
        return {
            "source": "FMP",
            "schema_version": 2,
            "status": "ok" if ok_count == len(endpoint_status) else ("partial" if ok_count else "restricted" if restricted_count else "error"),
            "generated_at": utc_now_iso(),
            "authentication": "apikey request header",
            "stock_news": results.get("stock_news", []),
            "press_releases": results.get("press_releases", []),
            "profiles": profiles,
            "earnings": earnings,
            "transcript_availability": results.get("transcript_availability", []),
            "transcript": transcript,
            "endpoint_status": endpoint_status,
            "restricted_endpoint_count": restricted_count,
            "implementation_note": "Restricted endpoints are recorded as a normal result and never stop the daily run; transcript text is not guessed.",
        }

    cached = read_json(path)
    should_fetch = config.refresh or not isinstance(cached, dict)
    if _configured_key(api_key) and isinstance(cached, dict):
        should_fetch = should_fetch or cached.get("status") == "skipped" or cached.get("schema_version") != 2
    if should_fetch:
        payload = fetcher()
        write_json(path, payload)
        cache_used = False
    else:
        payload = cached
        cache_used = True
    missing_data = []
    if payload.get("status") != "ok":
        missing_data.append(
            missing("FMP", payload.get("reason", f"FMP status: {payload.get('status', 'unknown')}"), "low")
        )
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "raw": payload,
        "news_items": _news_items(payload),
        "missing_data": missing_data,
    }


def _fetch_endpoint(api_key: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(
            FMP_BASE_URL + endpoint,
            headers={"apikey": api_key, "Accept": "application/json"},
            params=params,
            timeout=20,
        )
        status_code = int(response.status_code)
        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": response.text[:1000]}
    except requests.RequestException as exc:
        return {
            "status": "error",
            "http_status": None,
            "reason": f"{type(exc).__name__}: {exc}",
            "data": [],
        }
    text = str(data).lower()
    if status_code in {401, 402, 403} or any(marker in text for marker in ("restricted", "subscription", "premium plan", "not available under your current")):
        return {
            "status": "restricted",
            "http_status": status_code,
            "reason": "Restricted Endpoint or plan limitation.",
            "data": [],
        }
    if status_code < 200 or status_code >= 300:
        return {
            "status": "error",
            "http_status": status_code,
            "reason": f"HTTP {status_code}",
            "data": [],
        }
    return {
        "status": "ok" if data not in (None, [], {}) else "empty",
        "http_status": status_code,
        "reason": "" if data not in (None, [], {}) else "FMP returned an empty response.",
        "data": data,
    }


def _fetch_transcript(api_key: str, target_date: str) -> dict[str, Any]:
    dates = _fetch_endpoint(api_key, "/earning-call-transcript-dates", {"symbol": "AAPL"})
    if dates.get("status") == "restricted":
        return {
            "status": "restricted",
            "http_status": dates.get("http_status"),
            "reason": dates.get("reason", "Restricted Endpoint or plan limitation."),
            "symbol": "AAPL",
            "data": [],
        }
    year, quarter = _latest_transcript_period(dates.get("data"), target_date)
    result = _fetch_endpoint(
        api_key,
        "/earning-call-transcript",
        {"symbol": "AAPL", "year": year, "quarter": quarter},
    )
    if result.get("status") == "restricted":
        status = "restricted"
    elif result.get("status") == "ok" and result.get("data") not in (None, [], {}):
        status = "success"
    else:
        status = "failed"
    return {
        "status": status,
        "http_status": result.get("http_status"),
        "reason": result.get("reason", ""),
        "symbol": "AAPL",
        "year": year,
        "quarter": quarter,
        "data": result.get("data", []),
    }


def _latest_transcript_period(value: Any, target_date: str) -> tuple[int, int]:
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                return int(item.get("year")), int(item.get("quarter"))
            except (TypeError, ValueError):
                continue
    try:
        year, month, _ = (int(part) for part in target_date.split("-", 2))
    except (TypeError, ValueError):
        return 2026, 1
    quarter = max(1, min(4, (month - 1) // 3 + 1))
    return year, quarter


def _news_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group in ("stock_news", "press_releases"):
        records = payload.get(group, [])
        if not isinstance(records, list):
            continue
        for item in records:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "title": item.get("title", ""),
                    "source": item.get("site") or item.get("publisher") or "FMP",
                    "published_at": item.get("publishedDate") or item.get("date") or "",
                    "url": item.get("url") or item.get("link") or "",
                    "snippet": item.get("text") or item.get("snippet") or "",
                    "related_tickers": [item.get("symbol")] if item.get("symbol") in WATCHLIST else [],
                }
            )
    return [item for item in output if item.get("title") and item.get("url")]


def _configured_key(value: str) -> bool:
    return bool(value and value.lower() not in INVALID_KEY_VALUES)


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(str(value or default))
    except (TypeError, ValueError):
        parsed = default
    return min(max(1, parsed), maximum)
