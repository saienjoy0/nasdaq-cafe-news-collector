from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from nasdaq_cafe.cache import load_or_fetch
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.normalize import clamp_text, strip_html, utc_now_iso


GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS_PER_QUERY = 10
MAX_RAW_RESULTS = 80
MAX_DEDUPED_CANDIDATES = 40
MAX_SOURCE_PACK_CANDIDATES = 20
MAX_FULLTEXT_CANDIDATES = 10
MAX_DAILY_LEAD_THEME_CANDIDATES = 5
MAX_STATEMENT_RADAR_CANDIDATES = 20
MAX_STATEMENT_FULLTEXT_CANDIDATES = 5
UNVERIFIED_CANDIDATE_FLAGS = {
    "body_verified": False,
    "use_as_script_evidence": False,
    "needs_fulltext_before_script": True,
    "market_causality_confirmed": False,
}

GDELT_CATEGORIES: dict[str, dict[str, Any]] = {
    "ai_model_watch": {
        "query": '(OpenAI OR Anthropic OR "AI model" OR "large language model" OR LLM OR DeepSeek) (AI OR artificial intelligence)',
        "terms": ["openai", "anthropic", "ai model", "large language model", "llm", "deepseek"],
    },
    "china_ai_watch": {
        "query": '(China OR Chinese OR DeepSeek OR Alibaba OR Baidu OR Tencent) (AI OR artificial intelligence OR chips)',
        "terms": ["china", "chinese", "deepseek", "alibaba", "baidu", "tencent", "ai"],
    },
    "ai_chip_compute": {
        "query": '(Nvidia OR AMD OR Broadcom OR TSMC OR "AI chip" OR semiconductor OR GPU) (AI OR datacenter OR compute)',
        "terms": ["nvidia", "amd", "broadcom", "tsmc", "ai chip", "semiconductor", "gpu", "datacenter", "compute"],
    },
    "platform_ai": {
        "query": '(Microsoft OR Google OR Alphabet OR Meta OR Amazon OR Apple) (AI OR artificial intelligence OR cloud)',
        "terms": ["microsoft", "google", "alphabet", "meta", "amazon", "apple", "cloud", "ai"],
    },
    "elon_space_xai": {
        "query": '(Elon Musk OR SpaceX OR xAI OR Tesla robotaxi) (AI OR space OR autonomy)',
        "terms": ["elon musk", "spacex", "xai", "tesla", "robotaxi", "autonomy"],
    },
    "robotics_autonomy": {
        "query": '(robotics OR robot OR autonomous driving OR humanoid robot) (AI OR chip OR Tesla OR Nvidia)',
        "terms": ["robotics", "robot", "autonomous driving", "humanoid", "tesla", "nvidia"],
    },
    "regulation_geopolitics": {
        "query": '("export controls" OR sanctions OR regulation OR antitrust OR "US China") (AI OR chips OR semiconductor)',
        "terms": ["export controls", "sanctions", "regulation", "antitrust", "us china", "chips", "semiconductor"],
    },
    "money_flow_deals": {
        "query": '(funding OR investment OR acquisition OR partnership OR deal) (AI OR semiconductor OR datacenter OR cloud)',
        "terms": ["funding", "investment", "acquisition", "partnership", "deal", "datacenter", "cloud"],
    },
}
GLOBAL_RELEVANCE_TERMS = sorted(
    {
        term
        for category in GDELT_CATEGORIES.values()
        for term in category.get("terms", [])
    }
    | {
        "ai",
        "artificial intelligence",
        "chip",
        "chips",
        "semiconductor",
        "gpu",
        "datacenter",
        "data center",
        "cloud",
        "export controls",
        "funding",
        "investment",
    },
    key=len,
    reverse=True,
)

TICKER_TERMS = {
    "NVDA": ["nvidia", "nvda"],
    "AMD": ["advanced micro", "amd"],
    "AVGO": ["broadcom", "avgo"],
    "TSM": ["tsmc", "taiwan semiconductor", "tsm"],
    "AAPL": ["apple", "aapl"],
    "MSFT": ["microsoft", "msft"],
    "GOOGL": ["google", "alphabet", "googl"],
    "META": ["meta", "facebook"],
    "AMZN": ["amazon", "aws", "amzn"],
    "TSLA": ["tesla", "robotaxi", "tsla"],
    "MU": ["micron", "mu"],
    "BABA": ["alibaba", "baba"],
    "BIDU": ["baidu", "bidu"],
    "TCEHY": ["tencent", "tcehy"],
    "private_spacex": ["spacex"],
    "private_xai": ["xai"],
    "private_openai": ["openai"],
    "private_anthropic": ["anthropic"],
    "private_deepseek": ["deepseek"],
}

THEME_LABELS = {
    "ai_model_watch": "AI model watch",
    "china_ai_watch": "China AI watch",
    "ai_chip_compute": "AI chip and compute",
    "platform_ai": "Platform AI",
    "elon_space_xai": "Elon / SpaceX / xAI",
    "robotics_autonomy": "Robotics and autonomy",
    "regulation_geopolitics": "Regulation and geopolitics",
    "money_flow_deals": "Money flow and deals",
}

SPEAKER_TARGETS = [
    {"name": "Donald Trump", "role": "US political figure", "aliases": ["donald trump", "trump"]},
    {"name": "Xi Jinping", "role": "China President", "aliases": ["xi jinping", "president xi"]},
    {"name": "Jerome Powell", "role": "Federal Reserve Chair", "aliases": ["jerome powell", "powell", "federal reserve", "fed"]},
    {"name": "White House", "role": "US administration", "aliases": ["white house"]},
    {"name": "US Commerce Department / BIS", "role": "US export-control authority", "aliases": ["commerce department", "bis", "bureau of industry and security"]},
    {"name": "Jensen Huang", "role": "Nvidia CEO", "aliases": ["jensen huang", "nvidia ceo"]},
    {"name": "Sam Altman", "role": "OpenAI CEO", "aliases": ["sam altman", "openai ceo"]},
    {"name": "Elon Musk", "role": "Tesla / SpaceX / xAI executive", "aliases": ["elon musk", "musk"]},
    {"name": "Tim Cook", "role": "Apple CEO", "aliases": ["tim cook", "apple ceo"]},
    {"name": "Satya Nadella", "role": "Microsoft CEO", "aliases": ["satya nadella", "microsoft ceo"]},
    {"name": "Sundar Pichai", "role": "Alphabet CEO", "aliases": ["sundar pichai", "google ceo", "alphabet ceo"]},
    {"name": "Mark Zuckerberg", "role": "Meta CEO", "aliases": ["mark zuckerberg", "zuckerberg", "meta ceo"]},
    {"name": "Lisa Su", "role": "AMD CEO", "aliases": ["lisa su", "amd ceo"]},
    {"name": "TSMC executives", "role": "TSMC executives", "aliases": ["tsmc executives", "tsmc ceo", "cc wei", "c.c. wei"]},
    {"name": "Huawei executives", "role": "Huawei executives", "aliases": ["huawei executives", "huawei ceo"]},
    {"name": "Alibaba executives", "role": "Alibaba executives", "aliases": ["alibaba executives", "alibaba ceo"]},
    {"name": "DeepSeek", "role": "AI company", "aliases": ["deepseek"]},
    {"name": "xAI", "role": "AI company", "aliases": ["xai", "x.ai"]},
]

STATEMENT_TOPIC_TERMS = {
    "AI": ["ai", "artificial intelligence", "llm", "large language model"],
    "semiconductor": ["semiconductor", "chip", "chips", "gpu"],
    "China": ["china", "chinese"],
    "export controls": ["export controls", "export control", "bis"],
    "tariff": ["tariff", "tariffs"],
    "data center": ["data center", "datacenter"],
    "Nvidia": ["nvidia", "jensen huang"],
    "Tesla": ["tesla", "robotaxi"],
    "SpaceX": ["spacex"],
    "xAI": ["xai", "x.ai"],
    "robotics": ["robotics", "robot", "humanoid"],
    "autonomous driving": ["autonomous driving", "autonomy", "robotaxi"],
    "interest rates": ["interest rates", "rate cut", "rate hike", "treasury yields"],
    "inflation": ["inflation", "cpi", "ppi", "pce"],
    "oil": ["oil", "crude"],
    "Taiwan": ["taiwan", "tsmc"],
}

OFFICIAL_DOMAIN_MARKERS = (
    ".gov",
    "federalreserve.gov",
    "whitehouse.gov",
    "commerce.gov",
    "bis.doc.gov",
    "nvidia.com",
    "openai.com",
    "apple.com",
    "microsoft.com",
    "abc.xyz",
    "meta.com",
    "amd.com",
    "tsmc.com",
    "tesla.com",
    "spacex.com",
    "x.ai",
)

GOOD_DOMAINS = {
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "cnbc.com",
    "marketwatch.com",
    "finance.yahoo.com",
    "nasdaq.com",
    "investors.com",
    "barrons.com",
    "wsj.com",
    "ft.com",
    "theverge.com",
    "technologyreview.com",
    "semianalysis.com",
    "tomshardware.com",
    "techcrunch.com",
    "theinformation.com",
    "scmp.com",
    "nikkei.com",
}

REJECT_DOMAIN_MARKERS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "reddit.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
)

LOW_QUALITY_MARKERS = (
    "press release",
    "sponsored",
    "coupon",
    "giveaway",
    "casino",
    "betting",
    "crypto price prediction",
)


def collect_gdelt_radar(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "gdelt_radar.json"

    def fetcher() -> dict[str, Any]:
        return _fetch_gdelt_radar(config.target_date)

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    missing_data = []
    for item in payload.get("missing_data", []):
        missing_data.append(
            missing(
                item.get("source", "GDELT Radar"),
                item.get("reason", "GDELT Radar did not return usable data."),
                item.get("severity", "low"),
            )
        )
    if not summary.get("accepted_count"):
        missing_data.append(missing("GDELT Radar", "No accepted GDELT radar candidates for this run.", "low"))

    return {
        "status": _status_from_payload(payload),
        "cache_used": cache_used,
        "raw": payload,
        "summary": summary,
        "missing_data": missing_data,
    }


def gdelt_radar_status(result: dict[str, Any], config: RunConfig) -> dict[str, Any]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return {
        "raw_path": f"output/{config.target_date}/raw/gdelt_radar.json",
        "cache_used": result.get("cache_used", False),
        "status": result.get("status", "unknown"),
        "query_count": summary.get("query_count", 0),
        "raw_result_count": summary.get("raw_result_count", 0),
        "candidate_count": summary.get("candidate_count", 0),
        "accepted_count": summary.get("accepted_count", 0),
        "rejected_count": summary.get("rejected_count", 0),
        "fulltext_candidate_count": summary.get("fulltext_candidate_count", 0),
        "categories": payload.get("categories", {}),
    }


def gdelt_radar_candidates(result: dict[str, Any], limit: int = MAX_SOURCE_PACK_CANDIDATES) -> list[dict[str, Any]]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    accepted = payload.get("accepted", []) if isinstance(payload, dict) else []
    return [_source_pack_candidate(item) for item in accepted[:limit]]


def gdelt_fulltext_candidates(result: dict[str, Any], limit: int = MAX_FULLTEXT_CANDIDATES) -> list[dict[str, Any]]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    accepted = payload.get("accepted", []) if isinstance(payload, dict) else []
    candidates = [item for item in accepted if item.get("fulltext_candidate")]
    return [
        {
            "category": item.get("category", ""),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "published_at": item.get("published_at", ""),
            "seen_at": item.get("seen_at", ""),
            "related_tickers": item.get("related_tickers", []),
            "radar_score": item.get("radar_score", 0),
            **UNVERIFIED_CANDIDATE_FLAGS,
        }
        for item in candidates[:limit]
    ]


def gdelt_daily_lead_theme_candidates(result: dict[str, Any], limit: int = MAX_DAILY_LEAD_THEME_CANDIDATES) -> list[dict[str, Any]]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    candidates = payload.get("daily_lead_theme_candidates", []) if isinstance(payload, dict) else []
    return candidates[:limit]


def statement_radar_status(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    status = payload.get("statement_radar_status", {}) if isinstance(payload, dict) else {}
    return status if isinstance(status, dict) else {}


def statement_radar_candidates(result: dict[str, Any], limit: int = MAX_STATEMENT_RADAR_CANDIDATES) -> list[dict[str, Any]]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    candidates = payload.get("statement_radar_candidates", []) if isinstance(payload, dict) else []
    return candidates[:limit]


def statement_fulltext_candidates(result: dict[str, Any], limit: int = MAX_STATEMENT_FULLTEXT_CANDIDATES) -> list[dict[str, Any]]:
    payload = result.get("raw", {}) if isinstance(result, dict) else {}
    candidates = payload.get("statement_fulltext_candidates", []) if isinstance(payload, dict) else []
    return candidates[:limit]


def _fetch_gdelt_radar(target_date: str) -> dict[str, Any]:
    query_records: list[dict[str, Any]] = []
    raw_articles: list[dict[str, Any]] = []
    missing_data: list[dict[str, str]] = []
    implementation_notes = [
        "GDELT Radar collects URL candidates only. It does not save full article text or select a Daily Lead Theme.",
        "Scoring is mechanical and based on category terms, freshness, source quality, URL quality, and ticker mentions.",
    ]

    for category, spec in list(GDELT_CATEGORIES.items())[:8]:
        query = str(spec["query"])
        try:
            response_payload, status = _fetch_gdelt_query(query)
        except Exception as exc:
            reason = f"{category} query failed: {type(exc).__name__}: {exc}"
            missing_data.append({"source": "GDELT Radar", "reason": reason, "severity": "low"})
            query_records.append(_query_record(category, query, "error", reason, 0))
            continue

        articles = response_payload.get("articles", []) if isinstance(response_payload, dict) else []
        articles = [article for article in articles if isinstance(article, dict)][:MAX_RECORDS_PER_QUERY]
        query_records.append(_query_record(category, query, status, "", len(articles)))
        for article in articles:
            article = dict(article)
            article["_gdelt_category"] = category
            article["_gdelt_query"] = query
            raw_articles.append(article)
            if len(raw_articles) >= MAX_RAW_RESULTS:
                break
        time.sleep(1.0)

    candidates = _dedupe_candidates([_normalize_candidate(article, target_date) for article in raw_articles])
    candidates = candidates[:MAX_DEDUPED_CANDIDATES]
    accepted = [item for item in candidates if item["decision"] == "accepted"]
    rejected = [item for item in candidates if item["decision"] != "accepted"]
    accepted.sort(key=lambda item: item.get("radar_score", 0), reverse=True)
    rejected.sort(key=lambda item: item.get("radar_score", 0), reverse=True)
    for index, item in enumerate(accepted):
        item["fulltext_candidate"] = index < MAX_FULLTEXT_CANDIDATES and item.get("radar_score", 0) >= 55
    accepted = accepted[:MAX_SOURCE_PACK_CANDIDATES]
    statement_candidates = _build_statement_radar_candidates(candidates)
    statement_status = _build_statement_radar_status(statement_candidates)
    statement_fulltext = _build_statement_fulltext_candidates(statement_candidates)
    daily_lead_candidates = _build_daily_lead_theme_candidates(accepted, statement_candidates)

    categories = _category_summary(raw_articles, accepted, rejected)
    summary = {
        "query_count": len(query_records),
        "raw_result_count": len(raw_articles),
        "candidate_count": len(candidates),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "fulltext_candidate_count": sum(1 for item in accepted if item.get("fulltext_candidate")),
        "daily_lead_theme_candidate_count": len(daily_lead_candidates),
        "statement_candidate_count": len(statement_candidates),
        "statement_fulltext_candidate_count": len(statement_fulltext),
        "telop_candidate_count": statement_status.get("telop_candidate_count", 0),
    }
    if not raw_articles:
        missing_data.append({"source": "GDELT Radar", "reason": "GDELT DOC API returned no article records.", "severity": "low"})

    return {
        "date": target_date,
        "generated_at_jst": _jst_now(),
        "generated_at": utc_now_iso(),
        "collector": "gdelt_radar_collector",
        "status": "ok" if accepted else ("empty" if raw_articles else "error"),
        "summary": summary,
        "raw_articles": raw_articles,
        "categories": categories,
        "queries": query_records,
        "candidates": candidates,
        "accepted": accepted,
        "rejected": rejected,
        "daily_lead_theme_candidates": daily_lead_candidates,
        "statement_radar_status": statement_status,
        "statement_radar_candidates": statement_candidates,
        "statement_fulltext_candidates": statement_fulltext,
        "missing_data": missing_data,
        "implementation_notes": implementation_notes,
    }


def _fetch_gdelt_query(query: str) -> tuple[dict[str, Any], str]:
    import requests

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS_PER_QUERY,
        "sort": "DateDesc",
        "timespan": "3d",
    }
    headers = {"User-Agent": "nasdaq-cafe-gdelt-radar/0.1 (+metadata-only)"}
    last_response = None
    for attempt in range(2):
        response = requests.get(GDELT_ENDPOINT, params=params, headers=headers, timeout=15)
        last_response = response
        if response.status_code == 429 and attempt == 0:
            time.sleep(4.0)
            continue
        status = f"http_{response.status_code}"
        response.raise_for_status()
        return response.json(), status
    assert last_response is not None
    last_response.raise_for_status()
    return {}, f"http_{last_response.status_code}"


def _normalize_candidate(article: dict[str, Any], target_date: str) -> dict[str, Any]:
    title = strip_html(article.get("title", ""))
    url = strip_html(article.get("url", ""))
    raw_category = str(article.get("_gdelt_category", ""))
    text_for_category = " ".join([title, strip_html(article.get("snippet") or article.get("description") or ""), url]).lower()
    category = _best_category(raw_category, text_for_category)
    spec = GDELT_CATEGORIES.get(category, {})
    domain = _domain(article.get("domain") or url)
    source = strip_html(article.get("sourceCommonName") or article.get("source") or domain)
    seen_at = _normalize_date(article.get("seendate") or article.get("seen_at") or article.get("datetime"))
    published_at = _normalize_date(
        article.get("published_at")
        or article.get("date")
        or article.get("datetime")
        or article.get("seendate")
    )
    snippet = clamp_text(article.get("snippet") or article.get("description") or "")
    text = " ".join([title, snippet, url]).lower()
    category_terms = _matched_terms(text, spec.get("terms", []))
    global_terms = [term for term in _matched_terms(text, GLOBAL_RELEVANCE_TERMS) if term not in category_terms]
    matched_terms = category_terms or global_terms[:5]
    related_tickers = _related_tickers(text)
    score_components = _score_components(
        title=title,
        url=url,
        domain=domain,
        text=text,
        matched_terms=matched_terms,
        related_tickers=related_tickers,
        seen_at=seen_at or published_at,
        target_date=target_date,
    )
    radar_score = sum(score_components.values())
    decision, reason = _decision(title, url, domain, text, matched_terms, score_components, radar_score)
    return {
        "candidate_id": "gdelt-" + hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:12],
        "category": category,
        "title": title,
        "source": source,
        "url": url,
        "published_at": published_at,
        "seen_at": seen_at,
        "language": strip_html(article.get("language", "")),
        "source_country": strip_html(article.get("sourcecountry") or article.get("sourceCountry") or ""),
        "snippet": snippet,
        "related_tickers": related_tickers,
        "matched_terms": matched_terms,
        "gdelt_query_category": raw_category,
        "gdelt_query": str(article.get("_gdelt_query", "")),
        "gdelt_domain": domain,
        "decision": decision,
        "decision_reason": reason,
        "fulltext_candidate": False,
        "radar_score": radar_score,
        "score_components": score_components,
        **UNVERIFIED_CANDIDATE_FLAGS,
    }


def _score_components(
    *,
    title: str,
    url: str,
    domain: str,
    text: str,
    matched_terms: list[str],
    related_tickers: list[str],
    seen_at: str,
    target_date: str,
) -> dict[str, int]:
    freshness = _freshness_score(seen_at, target_date)
    source_quality = _source_quality_score(domain)
    category_match = 25 if len(matched_terms) >= 2 else (15 if matched_terms else 0)
    ticker_link = 10 if any(ticker != "no_us_ticker" for ticker in related_tickers) else 0
    url_quality = 0 if _is_rejected_url(url, domain) else 10
    novelty = 5 if _has_novelty_signal(text) else 0
    title_quality = 5 if len(title) >= 20 else 0
    return {
        "freshness": freshness,
        "source_quality": source_quality,
        "category_match": category_match,
        "ticker_link": ticker_link,
        "url_quality": url_quality,
        "novelty_signal": novelty,
        "title_quality": title_quality,
    }


def _freshness_score(value: str, target_date: str) -> int:
    published = _parse_datetime(value)
    if published is None:
        return 0
    now = datetime.now(UTC)
    delta = abs(now - published.astimezone(UTC))
    if delta <= timedelta(days=1):
        return 35
    if delta <= timedelta(days=3):
        return 25
    try:
        target = datetime.fromisoformat(target_date).replace(tzinfo=UTC)
        if abs(target - published.astimezone(UTC)) <= timedelta(days=3):
            return 20
    except ValueError:
        pass
    if delta <= timedelta(days=7):
        return 10
    return 0


def _source_quality_score(domain: str) -> int:
    if any(domain.endswith(good) for good in GOOD_DOMAINS):
        return 25
    if not domain:
        return 0
    if any(marker in domain for marker in REJECT_DOMAIN_MARKERS):
        return 0
    return 15


def _decision(
    title: str,
    url: str,
    domain: str,
    text: str,
    matched_terms: list[str],
    score_components: dict[str, int],
    radar_score: int,
) -> tuple[str, str]:
    if not title:
        return "rejected", "missing title"
    if not url:
        return "rejected", "missing url"
    if _is_rejected_url(url, domain):
        return "rejected", "blocked or low-quality URL domain"
    if any(marker in text for marker in LOW_QUALITY_MARKERS):
        return "rejected", "low-quality or sponsored marker"
    if not matched_terms:
        return "rejected", "no category terms matched"
    if score_components.get("freshness", 0) == 0:
        return "rejected", "not recent enough for 3-day radar"
    if radar_score < 45:
        return "rejected", f"mechanical radar_score below threshold ({radar_score})"
    return "accepted", "mechanical category, freshness, source, and URL checks passed"


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if _term_match(text, term)]


def _best_category(original_category: str, text: str) -> str:
    original_terms = GDELT_CATEGORIES.get(original_category, {}).get("terms", [])
    original_count = len(_matched_terms(text, original_terms))
    best_category = original_category if original_category in GDELT_CATEGORIES else ""
    best_count = original_count
    for category, spec in GDELT_CATEGORIES.items():
        count = len(_matched_terms(text, spec.get("terms", [])))
        if count > best_count:
            best_category = category
            best_count = count
    return best_category or original_category


def _related_tickers(text: str) -> list[str]:
    related = []
    for ticker, terms in TICKER_TERMS.items():
        if any(_term_match(text, term) for term in terms):
            related.append(ticker)
    if "ai" in text and not related:
        related.append("no_us_ticker")
    return related


def _term_match(text: str, term: str) -> bool:
    escaped = re.escape(term.lower())
    if " " in term:
        return re.search(escaped.replace("\\ ", r"\s+"), text) is not None
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None


def _is_rejected_url(url: str, domain: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return True
    if any(marker in lowered or marker in domain for marker in REJECT_DOMAIN_MARKERS):
        return True
    if lowered.rstrip("/").endswith(("reuters.com", "cnbc.com", "marketwatch.com", "nasdaq.com", "finance.yahoo.com")):
        return True
    if "/quote/" in lowered or "finance.yahoo.com/quote" in lowered:
        return True
    return False


def _has_novelty_signal(text: str) -> bool:
    terms = (
        "launch",
        "new",
        "funding",
        "investment",
        "acquisition",
        "deal",
        "partnership",
        "regulation",
        "export control",
        "ban",
        "sanction",
        "earnings",
        "forecast",
        "guidance",
    )
    return any(term in text for term in terms)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output = []
    for item in candidates:
        url = str(item.get("url", "")).strip().lower()
        key = url or str(item.get("title", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _category_summary(
    raw_articles: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    summary = {
        category: {"raw_count": 0, "accepted_count": 0, "rejected_count": 0, "fulltext_candidate_count": 0}
        for category in GDELT_CATEGORIES
    }
    for article in raw_articles:
        category = str(article.get("_gdelt_category", ""))
        if category in summary:
            summary[category]["raw_count"] += 1
    for item in accepted:
        category = str(item.get("category", ""))
        if category in summary:
            summary[category]["accepted_count"] += 1
            if item.get("fulltext_candidate"):
                summary[category]["fulltext_candidate_count"] += 1
    for item in rejected:
        category = str(item.get("category", ""))
        if category in summary:
            summary[category]["rejected_count"] += 1
    return summary


def _build_daily_lead_theme_candidates(
    accepted: list[dict[str, Any]],
    statement_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in accepted:
        category = str(item.get("category", ""))
        if not category:
            continue
        grouped.setdefault(category, []).append(item)

    output: list[dict[str, Any]] = []
    for category, items in grouped.items():
        related_tickers = _unique_flatten(item.get("related_tickers", []) for item in items)
        supporting_candidate_ids = [str(item.get("candidate_id", "")) for item in sorted(items, key=lambda row: row.get("radar_score", 0), reverse=True)[:5]]
        related_statements = [
            statement
            for statement in statement_candidates
            if category in statement.get("related_themes", []) or set(related_tickers).intersection(statement.get("related_tickers", []))
        ]
        score_components = _daily_theme_score_components(items, related_tickers, related_statements)
        output.append(
            {
                "candidate_theme": THEME_LABELS.get(category, category),
                "main_category": category,
                "related_categories": [category],
                "related_tickers": related_tickers,
                "candidate_count": len(items),
                "statement_signal_count": len(related_statements),
                "radar_score": sum(score_components.values()),
                "score_components": score_components,
                "supporting_candidate_ids": supporting_candidate_ids,
                "supporting_statement_ids": [str(item.get("statement_id", "")) for item in related_statements[:5]],
                "needs_fulltext": True,
                **UNVERIFIED_CANDIDATE_FLAGS,
            }
        )
    output.sort(key=lambda item: item.get("radar_score", 0), reverse=True)
    return output[:MAX_DAILY_LEAD_THEME_CANDIDATES]


def _daily_theme_score_components(
    items: list[dict[str, Any]],
    related_tickers: list[str],
    statement_candidates: list[dict[str, Any]],
) -> dict[str, int]:
    average_radar = int(sum(int(item.get("radar_score") or 0) for item in items) / max(len(items), 1))
    source_count = len({str(item.get("source", "")).lower() for item in items if item.get("source")})
    return {
        "candidate_volume": min(len(items) * 10, 30),
        "average_radar_score_component": min(int(average_radar * 0.35), 40),
        "statement_signal_component": min(len(statement_candidates) * 8, 20),
        "ticker_breadth_component": min(len([ticker for ticker in related_tickers if ticker != "no_us_ticker"]) * 3, 15),
        "source_breadth_component": min(source_count * 2, 10),
    }


def _build_statement_radar_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    sorted_candidates = sorted(candidates, key=lambda item: item.get("radar_score", 0), reverse=True)
    for item in sorted_candidates:
        text = " ".join(
            str(item.get(key, ""))
            for key in ("title", "snippet", "url", "source")
        ).lower()
        speaker = _detect_speaker(text)
        topics = _detect_statement_topics(text)
        if speaker is None or not topics:
            continue
        source_type = _statement_source_type(str(item.get("gdelt_domain", "")), text)
        verification = _verification_status(source_type, int(item.get("score_components", {}).get("source_quality") or 0))
        quote_text_short = _quote_text_short(str(item.get("snippet", "")))
        quote_available = bool(quote_text_short)
        telop_candidate = verification != "unverified" and quote_available
        output.append(
            {
                "statement_id": "stmt-" + hashlib.sha1(
                    f"{item.get('candidate_id','')}|{speaker['name']}|{topics[0]}".encode("utf-8", errors="ignore")
                ).hexdigest()[:12],
                "source_candidate_id": item.get("candidate_id", ""),
                "is_statement_candidate": True,
                "speaker_name": speaker["name"],
                "speaker_role_or_entity": speaker["role"],
                "statement_topic": topics[0],
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_at", ""),
                "statement_source_type": source_type,
                "quote_candidate_available": quote_available,
                "quote_text_short": quote_text_short,
                "quote_language": "unknown" if not quote_text_short else _quote_language(quote_text_short),
                "telop_candidate": telop_candidate,
                "telop_reason": _telop_reason(telop_candidate, verification, quote_available),
                "verification_status": verification,
                "related_tickers": item.get("related_tickers", []),
                "related_themes": _unique_values([item.get("category", ""), *topics]),
                "radar_score": item.get("radar_score", 0),
                **UNVERIFIED_CANDIDATE_FLAGS,
            }
        )
        if len(output) >= MAX_STATEMENT_RADAR_CANDIDATES:
            break
    return output


def _build_statement_radar_status(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    top_speakers = _count_top(candidates, "speaker_name")
    top_topics = _count_top(candidates, "statement_topic")
    return {
        "status": "ok" if candidates else "empty",
        "candidate_count": len(candidates),
        "telop_candidate_count": sum(1 for item in candidates if item.get("telop_candidate")),
        "fulltext_candidate_count": min(len(candidates), MAX_STATEMENT_FULLTEXT_CANDIDATES),
        "top_speakers": top_speakers,
        "top_topics": top_topics,
        "note": "Statement Radar is a metadata-only list of articles that may contain statements. Body verification is required before script use.",
    }


def _build_statement_fulltext_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_candidates = sorted(
        candidates,
        key=lambda item: (
            item.get("verification_status") == "official_source",
            item.get("telop_candidate") is True,
            item.get("radar_score", 0),
        ),
        reverse=True,
    )
    output = []
    for item in sorted_candidates[:MAX_STATEMENT_FULLTEXT_CANDIDATES]:
        output.append(
            {
                "speaker_name": item.get("speaker_name", ""),
                "speaker_role_or_entity": item.get("speaker_role_or_entity", ""),
                "statement_topic": item.get("statement_topic", ""),
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "published_at": item.get("published_at", ""),
                "related_tickers": item.get("related_tickers", []),
                "related_themes": item.get("related_themes", []),
                "telop_candidate": item.get("telop_candidate", False),
                "verification_status": item.get("verification_status", ""),
                **UNVERIFIED_CANDIDATE_FLAGS,
            }
        )
    return output


def _detect_speaker(text: str) -> dict[str, Any] | None:
    for speaker in SPEAKER_TARGETS:
        if any(_term_match(text, alias) for alias in speaker["aliases"]):
            return speaker
    return None


def _detect_statement_topics(text: str) -> list[str]:
    topics = []
    for topic, terms in STATEMENT_TOPIC_TERMS.items():
        if any(_term_match(text, term) for term in terms):
            topics.append(topic)
    return topics


def _statement_source_type(domain: str, text: str) -> str:
    if any(marker in domain for marker in OFFICIAL_DOMAIN_MARKERS):
        return "official"
    if "transcript" in text:
        return "transcript"
    if "interview" in text:
        return "interview"
    if any(marker in text for marker in REJECT_DOMAIN_MARKERS):
        return "social_post"
    if "said" in text or "says" in text or "told" in text or "remarks" in text:
        return "news_report"
    return "unknown"


def _verification_status(source_type: str, source_quality: int) -> str:
    if source_type == "official":
        return "official_source"
    if source_type in {"news_report", "transcript", "interview"} and source_quality >= 15:
        return "reported_by_media"
    return "unverified"


def _quote_text_short(snippet: str) -> str:
    if not snippet:
        return ""
    patterns = [
        r'"([^"]{10,180})"',
        r"“([^”]{10,180})”",
        r"‘([^’]{10,180})’",
    ]
    for pattern in patterns:
        match = re.search(pattern, snippet)
        if match:
            return clamp_text(match.group(1), 180)
    return ""


def _quote_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
        return "non_english_or_mixed"
    return "english"


def _telop_reason(telop_candidate: bool, verification: str, quote_available: bool) -> str:
    if telop_candidate:
        return "short quote candidate found; body verification still required before telop use"
    if verification == "unverified":
        return "unverified source; do not use for telop before fulltext verification"
    if not quote_available:
        return "no explicit short quote in GDELT snippet"
    return "body verification required"


def _count_top(items: list[dict[str, Any]], key: str, limit: int = 5) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, "")).strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    rows = [{"name": name, "count": count} for name, count in counts.items()]
    rows.sort(key=lambda item: (-int(item["count"]), item["name"]))
    return rows[:limit]


def _unique_flatten(groups: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for value in group:
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                output.append(text)
    return output


def _unique_values(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _source_pack_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": item.get("category", ""),
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "url": item.get("url", ""),
        "published_at": item.get("published_at", ""),
        "seen_at": item.get("seen_at", ""),
        "snippet": item.get("snippet", ""),
        "related_tickers": item.get("related_tickers", []),
        "radar_score": item.get("radar_score", 0),
        "decision": item.get("decision", ""),
        "decision_reason": item.get("decision_reason", ""),
        "fulltext_candidate": item.get("fulltext_candidate", False),
        **UNVERIFIED_CANDIDATE_FLAGS,
    }


def _query_record(category: str, query: str, status: str, error: str, result_count: int) -> dict[str, Any]:
    return {
        "category": category,
        "query": query,
        "status": status,
        "error": error,
        "result_count": result_count,
    }


def _status_from_payload(payload: dict[str, Any]) -> str:
    status = str(payload.get("status", "") or "")
    if status:
        return status
    summary = payload.get("summary", {})
    if summary.get("accepted_count"):
        return "ok"
    if summary.get("raw_result_count"):
        return "empty"
    return "error"


def _domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        parsed = urlparse(text)
        text = parsed.netloc
    return text.removeprefix("www.")


def _normalize_date(value: Any) -> str:
    parsed = _parse_datetime(str(value or ""))
    if parsed is None:
        return strip_html(value)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _jst_now() -> str:
    return datetime.now(UTC).astimezone(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
