from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from nasdaq_cafe.cache import load_or_fetch
from nasdaq_cafe.collectors import search_collector as legacy_search
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.normalize import normalize_news_item, utc_now_iso
from nasdaq_cafe.processing.research_candidate_pool import PERSPECTIVE_BY_ID


SERPAPI_DISCOVERY_QUERIES: tuple[tuple[str, str], ...] = (
    ("P1", "Reuters AI semiconductor chips GPU data center markets today"),
    ("P3", "Reuters Japan markets BOJ yen policy stocks today"),
    ("P5", "Reuters global markets Treasury yields dollar central banks inflation today"),
    ("P7", "Reuters tariffs sanctions export controls geopolitics trade markets today"),
)

TAVILY_DISCOVERY_QUERIES: tuple[tuple[str, str], ...] = (
    ("P2", "large tech cloud software capex markets today"),
    ("P4", "China Hong Kong markets PBoC yuan policy tech today"),
    ("P6", "oil power commodities energy market shock today"),
    ("P8", "shipping supply chain logistics manufacturing global demand markets today"),
)

BLOCKED_MARKERS = (
    "facebook.com",
    "twitter.com",
    "x.com/",
    "linkedin.com/posts",
    "instagram.com",
    "tiktok.com",
    "reuters.com/plus",
    "/quote/",
    "stock-price",
    "finance.yahoo.com/quote",
)
LOW_QUALITY_MARKERS = ("sponsored", "advertorial", "coupon", "giveaway", "casino", "sports betting")


def collect_search_news(config: RunConfig) -> dict[str, Any]:
    """Preserve the legacy Search result while adding a quality-only discovery stream."""
    legacy = legacy_search.collect_search_news(config)
    serp_discovery = _collect_serpapi_discovery(config)
    tavily_discovery = _collect_tavily_discovery(config)

    discovery_items: list[dict[str, Any]] = []
    for provider_result in (serp_discovery, tavily_discovery):
        for raw in provider_result.get("raw", {}).get("items", []):
            if not isinstance(raw, dict):
                continue
            news = normalize_news_item(raw, str(raw.get("source") or provider_result.get("provider") or "Search"))
            news["query"] = str(raw.get("query") or "")
            news["query_scope"] = "discovery"
            news["perspective_id"] = str(raw.get("perspective_id") or "")
            if _is_valid_search_document(news, config.target_date):
                discovery_items.append(news)

    combined_raw = {
        "serpapi": _merge_raw_payloads(legacy.get("raw", {}).get("serpapi", {}), serp_discovery.get("raw", {})),
        "tavily": _merge_raw_payloads(legacy.get("raw", {}).get("tavily", {}), tavily_discovery.get("raw", {})),
    }

    metadata = dict(legacy.get("collector_metadata", {}))
    metadata["discovery_query_count"] = {
        "SerpAPI": len(SERPAPI_DISCOVERY_QUERIES),
        "Tavily": len(TAVILY_DISCOVERY_QUERIES),
    }
    metadata["discovery_raw_item_count"] = {
        "SerpAPI": len(serp_discovery.get("raw", {}).get("items", [])),
        "Tavily": len(tavily_discovery.get("raw", {}).get("items", [])),
    }
    metadata["discovery_candidate_count"] = len(discovery_items)
    metadata["discovery_policy"] = (
        "Discovery uses quality/freshness/URL checks only; absence of NASDAQ, AI or semiconductor terms is not a rejection reason."
    )

    return {
        **legacy,
        "raw": combined_raw,
        "legacy_raw": legacy.get("raw", {}),
        "discovery_raw": {
            "serpapi": serp_discovery.get("raw", {}),
            "tavily": tavily_discovery.get("raw", {}),
        },
        "discovery_items": discovery_items,
        "discovery_coverage": _coverage(serp_discovery, tavily_discovery, discovery_items),
        "collector_metadata": metadata,
        "missing_data": [
            *legacy.get("missing_data", []),
            *serp_discovery.get("missing_data", []),
            *tavily_discovery.get("missing_data", []),
        ],
    }


def _collect_serpapi_discovery(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "serpapi_discovery_results.json"
    key = str(config.env.get("SERPAPI_API_KEY") or "").strip()
    if not legacy_search._is_configured_api_key(key, "serpapi"):
        return _skipped("SerpAPI", "SERPAPI_API_KEY is not set. Broad discovery Search skipped.")

    def fetcher() -> dict[str, Any]:
        import requests

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for perspective_id, query in SERPAPI_DISCOVERY_QUERIES:
            try:
                response = requests.get(
                    "https://serpapi.com/search.json",
                    params={"engine": "google", "q": query, "api_key": key, "num": 5},
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
                            "query_scope": "discovery",
                            "perspective_id": perspective_id,
                        }
                    )
            except Exception as exc:
                errors.append(f"{perspective_id}: {type(exc).__name__}: {exc}")
        return {
            "source": "SerpAPI",
            "status": "ok" if items else ("error" if errors else "empty"),
            "generated_at": utc_now_iso(),
            "query_count": len(SERPAPI_DISCOVERY_QUERIES),
            "items": items,
            "errors": errors,
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    return _result("SerpAPI", payload, cache_used)


def _collect_tavily_discovery(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "tavily_discovery_results.json"
    key = str(config.env.get("TAVILY_API_KEY") or "").strip()
    if not legacy_search._is_configured_api_key(key, "tavily"):
        return _skipped("Tavily", "TAVILY_API_KEY is not set. Broad discovery Search skipped.")

    def fetcher() -> dict[str, Any]:
        import requests

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for perspective_id, query in TAVILY_DISCOVERY_QUERIES:
            try:
                response = requests.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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
                            "query_scope": "discovery",
                            "perspective_id": perspective_id,
                        }
                    )
            except Exception as exc:
                errors.append(f"{perspective_id}: {type(exc).__name__}: {exc}")
        return {
            "source": "Tavily",
            "status": "ok" if items else ("error" if errors else "empty"),
            "generated_at": utc_now_iso(),
            "query_count": len(TAVILY_DISCOVERY_QUERIES),
            "items": items,
            "errors": errors,
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    return _result("Tavily", payload, cache_used)


def _result(provider: str, payload: dict[str, Any], cache_used: bool) -> dict[str, Any]:
    status = str(payload.get("status") or "unknown")
    missing_data = []
    if status not in {"ok", "empty"}:
        reason = "; ".join(str(value) for value in payload.get("errors", []) if value) or f"{provider} broad discovery failed."
        missing_data.append(missing(f"{provider} Discovery", reason, "low"))
    return {
        "provider": provider,
        "status": status,
        "cache_used": cache_used,
        "raw": payload,
        "missing_data": missing_data,
    }


def _skipped(provider: str, reason: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": "skipped",
        "cache_used": False,
        "raw": {
            "source": provider,
            "status": "skipped",
            "reason": reason,
            "generated_at": utc_now_iso(),
            "query_count": 0,
            "items": [],
        },
        "missing_data": [missing(f"{provider} Discovery", reason, "low")],
    }


def _is_valid_search_document(news: dict[str, Any], target_date: str) -> bool:
    if news.get("source") not in {"SerpAPI", "Tavily"}:
        return False
    if not news.get("title") or not news.get("published_at") or not news.get("url") or not news.get("snippet"):
        return False
    if not _is_recent(news.get("published_at"), target_date):
        return False
    text = " ".join(str(news.get(key, "")).lower() for key in ("title", "url", "snippet"))
    if any(marker in text for marker in BLOCKED_MARKERS):
        return False
    if any(marker in text for marker in LOW_QUALITY_MARKERS):
        return False
    if text.rstrip("/").endswith(("reuters.com", "cnbc.com", "marketwatch.com", "nasdaq.com", "finance.yahoo.com")):
        return False
    return True


def _is_recent(value: Any, target_date: str) -> bool:
    published = _parse_date(value)
    target = _parse_date(target_date)
    if published is None or target is None:
        return False
    return target - timedelta(days=7) <= published <= target + timedelta(days=1)


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone(UTC).date() if parsed.tzinfo else parsed.date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _merge_raw_payloads(legacy: Any, discovery: Any) -> dict[str, Any]:
    legacy_payload = legacy if isinstance(legacy, dict) else {}
    discovery_payload = discovery if isinstance(discovery, dict) else {}
    items = [
        *[dict(item, query_scope=item.get("query_scope", "legacy")) for item in legacy_payload.get("items", []) if isinstance(item, dict)],
        *[dict(item) for item in discovery_payload.get("items", []) if isinstance(item, dict)],
    ]
    status_values = {str(legacy_payload.get("status") or ""), str(discovery_payload.get("status") or "")}
    status = "ok" if "ok" in status_values else ("error" if "error" in status_values else ("skipped" if status_values == {"skipped"} else "empty"))
    return {
        "source": legacy_payload.get("source") or discovery_payload.get("source") or "Search",
        "status": status,
        "generated_at": utc_now_iso(),
        "legacy_query_count": legacy_payload.get("query_count", 0),
        "discovery_query_count": discovery_payload.get("query_count", 0),
        "items": items,
    }


def _coverage(
    serp: dict[str, Any],
    tavily: dict[str, Any],
    discovery_items: list[dict[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    provider_queries = {
        "SerpAPI": SERPAPI_DISCOVERY_QUERIES,
        "Tavily": TAVILY_DISCOVERY_QUERIES,
    }
    provider_results = {"SerpAPI": serp, "Tavily": tavily}
    for pid in [f"P{index}" for index in range(1, 9)]:
        perspective = PERSPECTIVE_BY_ID[pid]
        row = {
            "attempted": False,
            "provider_attempts": {},
            "provider_status": {},
            "raw_count": 0,
            "candidate_count": sum(1 for item in discovery_items if item.get("perspective_id") == pid),
        }
        for provider, queries in provider_queries.items():
            configured_for_pid = any(query_pid == pid for query_pid, _ in queries)
            result = provider_results[provider]
            status = str(result.get("status") or "unknown")
            attempted = configured_for_pid and status != "skipped"
            row["provider_attempts"][provider.lower()] = attempted
            if configured_for_pid:
                row["provider_status"][provider.lower()] = status
            row["attempted"] = bool(row["attempted"] or attempted)
            if configured_for_pid:
                raw_items = result.get("raw", {}).get("items", [])
                row["raw_count"] += sum(1 for item in raw_items if isinstance(item, dict) and item.get("perspective_id") == pid)
        output[perspective["key"]] = row
    return output
