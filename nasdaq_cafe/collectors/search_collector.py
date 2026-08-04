from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from nasdaq_cafe.cache import load_or_fetch, write_json
from nasdaq_cafe.config import RunConfig, SERPAPI_SEARCH_QUERIES, TAVILY_SEARCH_QUERIES, missing
from nasdaq_cafe.processing.normalize import normalize_news_item, utc_now_iso
from nasdaq_cafe.processing.relevance import explain_relevance, is_relevant_news


INVALID_KEY_VALUES = {
    "",
    "your_serpapi_api_key",
    "your_tavily_api_key",
    "todo",
    "dummy",
    "test",
    "none",
    "null",
}


def collect_search_news(config: RunConfig) -> dict[str, Any]:
    serpapi = _collect_serpapi(config)
    tavily = _collect_tavily(config)

    news_items: list[dict[str, Any]] = []
    for payload in [serpapi["raw"], tavily["raw"]]:
        for item in payload.get("items", []):
            news = normalize_news_item(item, item.get("source", "Search"))
            news["why_relevant"] = news["why_relevant"] or explain_relevance(news)
            if _is_valid_search_news(news, config.target_date) and is_relevant_news(news):
                news_items.append(news)

    return {
        "status": {
            "SerpAPI": serpapi["status"],
            "Tavily": tavily["status"],
        },
        "cache_used": serpapi["cache_used"] or tavily["cache_used"],
        "news_items": news_items,
        "search_supplements": news_items,
        "raw": {"serpapi": serpapi["raw"], "tavily": tavily["raw"]},
        "collector_metadata": {
            "api_key_status": {
                "SERPAPI_API_KEY": _api_key_status(config.env.get("SERPAPI_API_KEY", ""), "serpapi"),
                "TAVILY_API_KEY": _api_key_status(config.env.get("TAVILY_API_KEY", ""), "tavily"),
            },
            "query_count": {
                "SerpAPI": len(SERPAPI_SEARCH_QUERIES),
                "Tavily": len(TAVILY_SEARCH_QUERIES),
            },
            "raw_item_count": {
                "SerpAPI": len(serpapi["raw"].get("items", [])),
                "Tavily": len(tavily["raw"].get("items", [])),
            },
            "adopted_supplement_count": {
                "SerpAPI": sum(1 for item in news_items if item.get("source") == "SerpAPI"),
                "Tavily": sum(1 for item in news_items if item.get("source") == "Tavily"),
            },
            "policy": "Search supplements are secondary to RSS and only normalized items with source, published_at, url, and snippet may be adopted.",
        },
        "missing_data": serpapi["missing_data"] + tavily["missing_data"],
    }


def _collect_serpapi(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "serpapi_results.json"
    api_key = config.env.get("SERPAPI_API_KEY", "").strip()

    if not _is_configured_api_key(api_key, "serpapi"):
        return _skipped_result("SerpAPI", "SERPAPI_API_KEY is not set. Search supplement skipped.", path)

    def fetcher() -> dict[str, Any]:
        try:
            import requests

            items = []
            for query in SERPAPI_SEARCH_QUERIES:
                response = requests.get(
                    "https://serpapi.com/search.json",
                    params={"engine": "google", "q": query, "api_key": api_key, "num": 5},
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                for result in payload.get("organic_results", [])[:5]:
                    items.append(
                        {
                            "title": result.get("title", ""),
                            "source": "SerpAPI",
                            "published_at": result.get("date", ""),
                            "url": result.get("link", ""),
                            "snippet": result.get("snippet", ""),
                            "query": query,
                        }
                    )
            return {
                "source": "SerpAPI",
                "status": "ok" if items else "empty",
                "generated_at": utc_now_iso(),
                "api_key_status": "set",
                "query_count": len(SERPAPI_SEARCH_QUERIES),
                "items": items,
            }
        except Exception as exc:
            return _error_payload("SerpAPI", exc)

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    return _collector_result("SerpAPI", payload, cache_used)


def _collect_tavily(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "tavily_results.json"
    api_key = config.env.get("TAVILY_API_KEY", "").strip()

    if not _is_configured_api_key(api_key, "tavily"):
        return _skipped_result("Tavily", "TAVILY_API_KEY is not set. Search supplement skipped.", path)

    def fetcher() -> dict[str, Any]:
        try:
            items = _fetch_tavily(api_key)
            return {
                "source": "Tavily",
                "status": "ok" if items else "empty",
                "generated_at": utc_now_iso(),
                "api_key_status": "set",
                "query_count": len(TAVILY_SEARCH_QUERIES),
                "items": items,
            }
        except Exception as exc:
            return _error_payload("Tavily", exc)

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    return _collector_result("Tavily", payload, cache_used)


def _fetch_tavily(api_key: str) -> list[dict[str, Any]]:
    # Keep HTTP auth explicit and auditable. The API key is never written to raw output.
    return _fetch_tavily_with_requests(api_key)


def _fetch_tavily_with_requests(api_key: str) -> list[dict[str, Any]]:
    import requests

    items = []
    for query in TAVILY_SEARCH_QUERIES:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "search_depth": "basic", "max_results": 5, "include_raw_content": False},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        for result in payload.get("results", []):
            items.append(
                {
                    "title": result.get("title", ""),
                    "source": "Tavily",
                    "published_at": result.get("published_date", ""),
                    "url": result.get("url", ""),
                    "snippet": result.get("content", ""),
                    "query": query,
                }
            )
    return items


def _skipped_payload(source: str, reason: str) -> dict[str, Any]:
    return {"source": source, "status": "skipped", "reason": reason, "generated_at": utc_now_iso(), "api_key_status": "not_set", "items": []}


def _error_payload(source: str, exc: Exception) -> dict[str, Any]:
    return {
        "source": source,
        "status": "error",
        "reason": f"{type(exc).__name__}: {exc}",
        "generated_at": utc_now_iso(),
        "items": [],
    }


def _collector_result(source: str, payload: dict[str, Any], cache_used: bool) -> dict[str, Any]:
    missing_data = []
    if payload.get("status") != "ok":
        missing_data.append(missing(source, payload.get("reason", f"{source} did not return results."), "medium"))
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "raw": payload,
        "missing_data": missing_data,
    }


def _skipped_result(source: str, reason: str, path) -> dict[str, Any]:
    payload = _skipped_payload(source, reason)
    write_json(path, payload)
    return _collector_result(source, payload, False)


def _is_configured_api_key(value: str, provider: str) -> bool:
    key = value.strip()
    if not key:
        return False
    lowered = key.lower()
    if lowered in INVALID_KEY_VALUES:
        return False
    if lowered == f"your_{provider}_api_key":
        return False
    return True


def _api_key_status(value: str, provider: str) -> str:
    return "set" if _is_configured_api_key(value, provider) else "not_set"


def _is_valid_search_news(news: dict[str, Any], target_date: str) -> bool:
    if news.get("source") not in {"SerpAPI", "Tavily"}:
        return False
    if not news.get("published_at") or not news.get("url") or not news.get("snippet"):
        return False
    if not _is_recent_published_at(news.get("published_at"), target_date):
        return False
    text = " ".join(
        str(news.get(key, "")).lower()
        for key in ("title", "url", "snippet")
    )
    blocked_markers = (
        "facebook.com",
        "twitter.com",
        "x.com/",
        "linkedin.com/posts",
        "reuters.com/plus",
        "/quote/",
        "stock-price",
        "finance.yahoo.com/quote",
    )
    if any(marker in text for marker in blocked_markers):
        return False
    if text.rstrip("/").endswith(("reuters.com", "cnbc.com", "marketwatch.com", "nasdaq.com", "finance.yahoo.com")):
        return False
    if not _has_search_supplement_value(text):
        return False
    return True


def _is_recent_published_at(value: Any, target_date: str) -> bool:
    published = _parse_date(value)
    if published is None:
        return False
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        target = date.today()
    return target - timedelta(days=30) <= published <= target + timedelta(days=1)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
        return parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _has_search_supplement_value(text: str) -> bool:
    terms = (
        "nasdaq",
        "sox",
        "semiconductor",
        "chip",
        "chips",
        "gpu",
        "memory",
        "nvidia",
        "amd",
        "tsmc",
        "broadcom",
        "micron",
        "marvell",
        "microsoft",
        "amazon",
        "google",
        "meta",
        "treasury yield",
        "bond yield",
        "ai infrastructure",
        "data center",
        "cloud capex",
    )
    return any(term in text for term in terms)
