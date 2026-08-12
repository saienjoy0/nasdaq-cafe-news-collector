from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


MAX_POOL_ITEMS = 40
MAX_HANDOFF_ITEMS = 24
MAX_HANDOFF_PER_PERSPECTIVE = 3

PERSPECTIVES: tuple[dict[str, Any], ...] = (
    {
        "id": "P1",
        "key": "P1_ai_semiconductor",
        "label": "AI / Semiconductor",
        "terms": (
            "artificial intelligence", " ai ", "ai model", "llm", "gpu", "semiconductor", "chip", "chips",
            "foundry", "memory", "hbm", "accelerator", "data center compute", "datacenter compute", "nvidia",
            "amd", "tsmc", "broadcom", "micron", "asml",
        ),
    },
    {
        "id": "P2",
        "key": "P2_large_tech_cloud_software",
        "label": "Large Tech / Cloud / Software",
        "terms": (
            "microsoft", "amazon", "aws", "google", "alphabet", "meta", "apple", "hyperscaler", "cloud",
            "software", "enterprise software", "capex", "ad platform", "device ecosystem", "enterprise ai",
        ),
    },
    {
        "id": "P3",
        "key": "P3_japan",
        "label": "Japan Market / Policy / Tech",
        "terms": (
            "japan", "japanese", "tokyo", "nikkei", "topix", "boj", "bank of japan", "yen", "jpy",
            "japan export", "japan supply chain", "japanese equities",
        ),
    },
    {
        "id": "P4",
        "key": "P4_china_hong_kong",
        "label": "China / Hong Kong Market / Policy / Tech",
        "terms": (
            "china", "chinese", "beijing", "pboc", "people's bank of china", "renminbi", "yuan", "cnh",
            "hong kong", "hang seng", "alibaba", "tencent", "baidu", "china economy", "china property",
        ),
    },
    {
        "id": "P5",
        "key": "P5_macro_rates_fx",
        "label": "Macro / Rates / FX",
        "terms": (
            "federal reserve", " fed ", "powell", "inflation", "cpi", "ppi", "pce", "treasury", "yield",
            "interest rate", "rate cut", "rate hike", "central bank", "dollar", "dxy", "currency", "fx",
            "jobs report", "payroll", "unemployment", "gdp",
        ),
    },
    {
        "id": "P6",
        "key": "P6_energy_power_commodities",
        "label": "Energy / Power / Commodities",
        "terms": (
            "crude", "oil", "natural gas", "lng", "electricity", "power grid", "power demand", "nuclear",
            "uranium", "commodity", "commodities", "copper", "rare earth", "critical mineral", "grain shock",
            "wheat prices", "food inflation",
        ),
    },
    {
        "id": "P7",
        "key": "P7_geopolitics_trade_sanctions",
        "label": "Geopolitics / Trade / Sanctions",
        "terms": (
            "tariff", "tariffs", "sanction", "sanctions", "export control", "export controls", "trade war",
            "trade policy", "geopolit", "war", "security event", "nato", "hormuz", "taiwan strait",
            "strait of malacca", "red sea", "chokepoint",
        ),
    },
    {
        "id": "P8",
        "key": "P8_supply_chain_logistics_global_economy",
        "label": "Supply Chain / Logistics / Global Economy",
        "terms": (
            "supply chain", "shipping", "freight", "port", "ports", "logistics", "factory disruption",
            "factory shutdown", "manufacturing", "global demand", "container", "rare earth", "earthquake",
            "typhoon", "disaster", "strait of malacca", "semiconductor equipment",
        ),
    },
)

PERSPECTIVE_BY_ID = {item["id"]: item for item in PERSPECTIVES}
PERSPECTIVE_BY_KEY = {item["key"]: item for item in PERSPECTIVES}
PERSPECTIVE_ORDER = [item["id"] for item in PERSPECTIVES]

REGION_TERMS: dict[str, tuple[str, ...]] = {
    "us": ("united states", "u.s.", " us ", "america", "nasdaq", "federal reserve", "treasury", "wall street"),
    "japan": ("japan", "japanese", "tokyo", "nikkei", "topix", "bank of japan", "boj", "yen"),
    "china": ("china", "chinese", "beijing", "pboc", "renminbi", "yuan", "shanghai", "shenzhen"),
    "hong_kong": ("hong kong", "hang seng", "hstech"),
    "taiwan": ("taiwan", "tsmc", "taipei"),
    "south_korea": ("south korea", "korea", "samsung", "sk hynix", "seoul"),
    "europe": ("europe", "euro zone", "eurozone", "ecb", "european union", "eu "),
    "middle_east": ("middle east", "iran", "israel", "hormuz", "red sea", "saudi", "qatar", "uae"),
}

TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "ai": ("artificial intelligence", " ai ", "ai model", "llm", "machine learning"),
    "semiconductor": ("semiconductor", "chip", "chips", "gpu", "hbm", "foundry", "memory"),
    "large_tech": ("microsoft", "amazon", "google", "alphabet", "meta", "apple", "hyperscaler"),
    "cloud_software": ("cloud", "software", "enterprise software", "saas", "capex"),
    "rates": ("interest rate", "rate cut", "rate hike", "treasury", "yield", "federal reserve", " fed "),
    "fx": ("currency", "fx", "dollar", "yen", "yuan", "renminbi", "cnh", "dxy"),
    "inflation": ("inflation", "cpi", "ppi", "pce", "food inflation"),
    "energy": ("oil", "crude", "natural gas", "lng", "electricity", "power", "nuclear", "uranium"),
    "commodities": ("commodity", "commodities", "copper", "rare earth", "critical mineral", "grain shock"),
    "trade": ("tariff", "trade policy", "trade war", "export control", "export controls"),
    "sanctions": ("sanction", "sanctions"),
    "geopolitics": ("geopolit", "war", "nato", "hormuz", "taiwan strait", "red sea", "strait of malacca"),
    "supply_chain": ("supply chain", "factory disruption", "factory shutdown", "manufacturing", "rare earth"),
    "logistics": ("shipping", "freight", "port", "ports", "logistics", "container"),
    "disaster": ("earthquake", "typhoon", "disaster", "flood", "wildfire"),
    "global_demand": ("global demand", "global economy", "world economy", "manufacturing demand"),
}

BLOCKED_URL_MARKERS = (
    "facebook.com",
    "twitter.com",
    "x.com/",
    "linkedin.com/posts",
    "instagram.com",
    "tiktok.com",
    "/quote/",
    "finance.yahoo.com/quote",
)
LOW_QUALITY_MARKERS = (
    "sponsored",
    "advertorial",
    "coupon",
    "giveaway",
    "casino",
    "sports betting",
    "affiliate disclosure",
)
TRACKING_QUERY_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}


def build_research_candidate_pool(
    streams: Iterable[tuple[str, Iterable[dict[str, Any]]]],
    *,
    target_date: str,
    coverage_inputs: Iterable[dict[str, Any]] = (),
    max_pool_items: int = MAX_POOL_ITEMS,
    max_handoff_items: int = MAX_HANDOFF_ITEMS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the broad observation pool without deciding NASDAQ causality."""
    normalized: list[dict[str, Any]] = []
    for provider, items in streams:
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            candidate = _candidate_from_item(provider, raw, target_date)
            if candidate is not None:
                normalized.append(candidate)

    deduped = _dedupe_candidates(normalized)
    deduped.sort(key=_pool_sort_key, reverse=True)
    pool_items = deduped[: max(0, int(max_pool_items))]
    handoff_items = _select_handoff_items(pool_items, max_handoff_items)

    by_perspective: dict[str, list[dict[str, Any]]] = {}
    for perspective in PERSPECTIVES:
        pid = perspective["id"]
        by_perspective[pid] = [
            item for item in handoff_items if pid in item.get("perspective_ids", [])
        ][:MAX_HANDOFF_PER_PERSPECTIVE]

    pool = {
        "contract_version": "market-transmission-v1.1",
        "max_pool_items": max_pool_items,
        "max_handoff_items": max_handoff_items,
        "max_handoff_per_perspective": MAX_HANDOFF_PER_PERSPECTIVE,
        "pool_count": len(pool_items),
        "handoff_count": len(handoff_items),
        "items": pool_items,
        "handoff_items": handoff_items,
        "by_perspective": by_perspective,
        "note": (
            "Research Candidate Pool is an observation queue. Candidate counts are not confidence and "
            "the collector does not decide NASDAQ causality."
        ),
    }
    coverage = build_discovery_coverage(pool_items, coverage_inputs)
    return pool, coverage


def build_discovery_coverage(
    pool_items: Iterable[dict[str, Any]],
    coverage_inputs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        perspective["key"]: {
            "perspective_id": perspective["id"],
            "label": perspective["label"],
            "attempted": False,
            "provider_attempts": {},
            "provider_status": {},
            "raw_count": 0,
            "candidate_count": 0,
        }
        for perspective in PERSPECTIVES
    }

    for report in coverage_inputs or []:
        if not isinstance(report, dict):
            continue
        for key, row in report.items():
            perspective = PERSPECTIVE_BY_KEY.get(str(key)) or PERSPECTIVE_BY_ID.get(str(key))
            if perspective is None or not isinstance(row, dict):
                continue
            target = coverage[perspective["key"]]
            attempted = bool(row.get("attempted"))
            target["attempted"] = bool(target["attempted"] or attempted)
            provider_attempts = row.get("provider_attempts", {})
            if isinstance(provider_attempts, dict):
                for provider, value in provider_attempts.items():
                    target["provider_attempts"][str(provider)] = bool(value)
            provider_status = row.get("provider_status", {})
            if isinstance(provider_status, dict):
                for provider, value in provider_status.items():
                    target["provider_status"][str(provider)] = str(value or "unknown")
            try:
                target["raw_count"] += int(row.get("raw_count") or 0)
            except (TypeError, ValueError):
                pass

    counts = {perspective["id"]: 0 for perspective in PERSPECTIVES}
    for item in pool_items:
        for pid in item.get("perspective_ids", []):
            if pid in counts:
                counts[pid] += 1
    for perspective in PERSPECTIVES:
        coverage[perspective["key"]]["candidate_count"] = counts[perspective["id"]]

    attempted_count = sum(1 for row in coverage.values() if row["attempted"])
    return {
        "contract_version": "coverage-before-relevance-v1.1",
        "perspectives": coverage,
        "attempted_perspective_count": attempted_count,
        "all_perspectives_attempted": attempted_count == len(PERSPECTIVES),
        "note": (
            "attempted=true with raw_count=0 and candidate_count=0 is a valid covered zero-result. "
            "Provider errors remain visible in provider_status."
        ),
    }


def _candidate_from_item(provider: str, raw: dict[str, Any], target_date: str) -> dict[str, Any] | None:
    title = _clean(raw.get("title") or raw.get("headline") or raw.get("name"))
    source = _clean(raw.get("source") or raw.get("publisher") or raw.get("site") or raw.get("provider") or provider)
    url = _clean(raw.get("url") or raw.get("link") or raw.get("href"))
    published_at = _clean(raw.get("published_at") or raw.get("published") or raw.get("pubDate") or raw.get("seen_at") or raw.get("datetime"))
    snippet = _clean(raw.get("snippet") or raw.get("summary") or raw.get("description") or raw.get("abstract") or raw.get("content"))
    text = _search_text(title, snippet, url, raw)

    if not _quality_usable(title, source, url, published_at, text, target_date):
        return None

    hinted = _perspective_hints(raw)
    perspective_ids = _perspective_ids(text, hinted)
    if not perspective_ids:
        return None

    region_tags = _matched_labels(text, REGION_TERMS)
    topic_tags = _matched_labels(text, TOPIC_TERMS)
    normalized_url = _normalize_url(url)
    provenance = [{"provider": provider, "source": source, "url": url}]

    output = {
        "provider": provider,
        "source": source,
        "url": url,
        "normalized_url": normalized_url,
        "title": title,
        "published_at": published_at,
        "snippet": snippet,
        "region_tags": region_tags,
        "topic_tags": topic_tags,
        "perspective_ids": perspective_ids,
        "quality_status": "usable",
        "provenance": provenance,
    }
    for key in (
        "body_verified",
        "needs_fulltext_before_script",
        "use_as_script_evidence",
        "decision",
        "decision_reason",
        "radar_score",
        "fulltext_candidate",
        "related_tickers",
        "category",
    ):
        if key in raw:
            output[key] = raw.get(key)
    if "body_verified" not in output:
        output["body_verified"] = None
    if "needs_fulltext_before_script" not in output:
        output["needs_fulltext_before_script"] = output["body_verified"] is not True
    return output


def _quality_usable(title: str, source: str, url: str, published_at: str, text: str, target_date: str) -> bool:
    if not title or not source or not url or not published_at:
        return False
    lowered_url = url.lower()
    if not lowered_url.startswith(("http://", "https://")):
        return False
    if any(marker in lowered_url for marker in BLOCKED_URL_MARKERS):
        return False
    if any(marker in text for marker in LOW_QUALITY_MARKERS):
        return False
    return _is_recent(published_at, target_date)


def _is_recent(value: str, target_date: str) -> bool:
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
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _perspective_hints(raw: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("perspective_id", "perspective_ids", "query_perspective", "category"):
        value = raw.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value:
            values.append(value)
    output: list[str] = []
    for value in values:
        text = str(value)
        match = re.search(r"\bP([1-8])\b", text, flags=re.IGNORECASE)
        if match:
            pid = "P" + match.group(1)
            if pid not in output:
                output.append(pid)
    return output


def _perspective_ids(text: str, hints: Iterable[str]) -> list[str]:
    found: list[str] = []
    for pid in hints:
        if pid in PERSPECTIVE_BY_ID and pid not in found:
            found.append(pid)
    for perspective in PERSPECTIVES:
        if any(_term_match(text, term) for term in perspective["terms"]):
            pid = perspective["id"]
            if pid not in found:
                found.append(pid)
    return sorted(found, key=PERSPECTIVE_ORDER.index)


def _matched_labels(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    return [label for label, terms in mapping.items() if any(_term_match(text, term) for term in terms)]


def _term_match(text: str, term: str) -> bool:
    term = term.lower()
    if term.startswith(" ") or term.endswith(" "):
        return term in f" {text} "
    if term.endswith("polit"):
        return term in text
    if " " in term or any(ch in term for ch in ".'/-"):
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _search_text(title: str, snippet: str, url: str, raw: dict[str, Any]) -> str:
    extras = " ".join(
        str(raw.get(key) or "")
        for key in ("query", "category", "why_relevant")
    )
    return re.sub(r"\s+", " ", f" {title} {snippet} {url} {extras} ".lower()).strip()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_QUERY_KEYS]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower().removeprefix("www."), parsed.path.rstrip("/"), "", urlencode(query), ""))


def _dedupe_candidates(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        url_key = item.get("normalized_url") or ""
        title_key = re.sub(r"[^a-z0-9]+", " ", str(item.get("title", "")).lower()).strip()
        key = str(url_key or f"title:{title_key}")
        if not key:
            continue
        if key not in by_key:
            by_key[key] = dict(item)
            order.append(key)
            continue
        existing = by_key[key]
        existing["perspective_ids"] = _ordered_union(existing.get("perspective_ids", []), item.get("perspective_ids", []), PERSPECTIVE_ORDER)
        existing["region_tags"] = _ordered_union(existing.get("region_tags", []), item.get("region_tags", []))
        existing["topic_tags"] = _ordered_union(existing.get("topic_tags", []), item.get("topic_tags", []))
        existing["provenance"] = _merge_provenance(existing.get("provenance", []), item.get("provenance", []))
        if len(str(item.get("snippet", ""))) > len(str(existing.get("snippet", ""))):
            existing["snippet"] = item.get("snippet", "")
        if existing.get("body_verified") is not True and item.get("body_verified") is True:
            existing["body_verified"] = True
            existing["needs_fulltext_before_script"] = False
    return [by_key[key] for key in order]


def _ordered_union(left: Iterable[Any], right: Iterable[Any], preferred_order: list[str] | None = None) -> list[str]:
    values: list[str] = []
    for value in [*left, *right]:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    if preferred_order:
        values.sort(key=lambda value: preferred_order.index(value) if value in preferred_order else len(preferred_order))
    return values


def _merge_provenance(left: Iterable[dict[str, Any]], right: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*left, *right]:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("provider", "")), str(item.get("source", "")), str(item.get("url", "")))
        if key in seen:
            continue
        seen.add(key)
        output.append({"provider": key[0], "source": key[1], "url": key[2]})
    return output


def _pool_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    parsed = _parse_datetime(item.get("published_at"))
    timestamp = parsed.timestamp() if parsed else 0.0
    verified = 1 if item.get("body_verified") is True else 0
    perspective_breadth = min(len(item.get("perspective_ids", [])), 3)
    return (verified, timestamp, perspective_breadth, str(item.get("title", "")))


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        parsed_date = _parse_date(text)
        if parsed_date is None:
            return None
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)


def _select_handoff_items(items: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    if max_items <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    per_perspective = {pid: 0 for pid in PERSPECTIVE_ORDER}

    for pid in PERSPECTIVE_ORDER:
        for item in items:
            key = str(item.get("normalized_url") or item.get("title") or "")
            if key in selected_keys or pid not in item.get("perspective_ids", []):
                continue
            selected.append(item)
            selected_keys.add(key)
            per_perspective[pid] += 1
            if per_perspective[pid] >= MAX_HANDOFF_PER_PERSPECTIVE or len(selected) >= max_items:
                break
        if len(selected) >= max_items:
            break

    for item in items:
        if len(selected) >= max_items:
            break
        key = str(item.get("normalized_url") or item.get("title") or "")
        if key in selected_keys:
            continue
        pids = [pid for pid in item.get("perspective_ids", []) if pid in per_perspective]
        if pids and all(per_perspective[pid] >= MAX_HANDOFF_PER_PERSPECTIVE for pid in pids):
            continue
        selected.append(item)
        selected_keys.add(key)
        for pid in pids:
            if per_perspective[pid] < MAX_HANDOFF_PER_PERSPECTIVE:
                per_perspective[pid] += 1
    return selected


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
