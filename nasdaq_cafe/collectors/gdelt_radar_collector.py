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
    statement_sß~¼¶‰žËkºwµç@‰™Õ¹‘¥¹œˆ°(€€€€€€€€‰¥¹Ù•ÍÑµ•¹Ðˆ°(€€€€€€€€‰…ÅÕ¥Í¥Ñ¥½¸ˆ°(€€€€€€€€‰‘•…°ˆ°(€€€€€€€€‰Á…ÉÑ¹•ÉÍ¡¥Àˆ°(€€€€€€€€‰É•Õ±…Ñ¥½¸ˆ°(€€€€€€€€‰•áÁ½ÉÐ½¹ÑÉ½°ˆ°(€€€€€€€€‰‰…¸ˆ°(€€€€€€€€‰Í…¹Ñ¥½¸ˆ°(€€€€€€€€‰•…É¹¥¹Ìˆ°(€€€€€€€€‰™½É•…ÍÐˆ°(€€€€€€€€‰Õ¥‘…¹”ˆ°(€€€€¤(€€€É•ÑÕÉ¸…¹ä¡Ñ•É´¥¸Ñ•áÐ™½ÈÑ•É´¥¸Ñ•ÉµÌ¤(()‘•˜}‘•‘ÕÁ•}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€½ÕÑÁÕÐ€ômt(€€€™½È¥Ñ•´¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€ÕÉ°€ôÍÑÈ¡¥Ñ•´¹•Ð ‰ÕÉ°ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€€€€€­•ä€ôÕÉ°½ÈÍÑÈ¡¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€€€€€¥˜¹½Ð­•ä½È­•ä¥¸Í••¸è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Í••¸¹…‘¡­•ä¤(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡¥Ñ•´¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}…Ñ•½Éå}ÍÕµµ…Éä (€€€É…Ý}…ÉÑ¥±•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(€€€…•ÁÑ•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(€€€É•©•Ñ•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(¤€´ø‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¥¹Ñutè(€€€ÍÕµµ…Éä€ôì(€€€€€€€…Ñ•½Éäèì‰É…Ý}½Õ¹Ðˆè€À°€‰…•ÁÑ•‘}½Õ¹Ðˆè€À°€‰É•©•Ñ•‘}½Õ¹Ðˆè€À°€‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ•}½Õ¹Ðˆè€Áô(€€€€€€€™½È…Ñ•½Éä¥¸1Q}Q=I%L(€€€ô(€€€™½È…ÉÑ¥±”¥¸É…Ý}…ÉÑ¥±•Ìè(€€€€€€€…Ñ•½Éä€ôÍÑÈ¡…ÉÑ¥±”¹•Ð ‰}‘•±Ñ}…Ñ•½Éäˆ°€ˆˆ¤¤(€€€€€€€¥˜…Ñ•½Éä¥¸ÍÕµµ…Éäè(€€€€€€€€€€€ÍÕµµ…Éåm…Ñ•½Éåul‰É…Ý}½Õ¹Ð‰t€¬ô€Ä(€€€™½È¥Ñ•´¥¸…•ÁÑ•è(€€€€€€€…Ñ•½Éä€ôÍÑÈ¡¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤¤(€€€€€€€¥˜…Ñ•½Éä¥¸ÍÕµµ…Éäè(€€€€€€€€€€€ÍÕµµ…Éåm…Ñ•½Éåul‰…•ÁÑ•‘}½Õ¹Ð‰t€¬ô€Ä(€€€€€€€€€€€¥˜¥Ñ•´¹•Ð ‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ”ˆ¤è(€€€€€€€€€€€€€€€ÍÕµµ…Éåm…Ñ•½Éåul‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ•}½Õ¹Ð‰t€¬ô€Ä(€€€™½È¥Ñ•´¥¸É•©•Ñ•è(€€€€€€€…Ñ•½Éä€ôÍÑÈ¡¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤¤(€€€€€€€¥˜…Ñ•½Éä¥¸ÍÕµµ…Éäè(€€€€€€€€€€€ÍÕµµ…Éåm…Ñ•½Éåul‰É•©•Ñ•‘}½Õ¹Ð‰t€¬ô€Ä(€€€É•ÑÕÉ¸ÍÕµµ…Éä(()‘•˜}‰Õ¥±‘}‘…¥±å}±•…‘}Ñ¡•µ•}…¹‘¥‘…Ñ•Ì (€€€…•ÁÑ•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(€€€ÍÑ…Ñ•µ•¹Ñ}…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€É½ÕÁ•è‘¥ÑmÍÑÈ°±¥ÍÑm‘¥ÑmÍÑÈ°¹åuut€ôíô(€€€™½È¥Ñ•´¥¸…•ÁÑ•è(€€€€€€€…Ñ•½Éä€ôÍÑÈ¡¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤¤(€€€€€€€¥˜¹½Ð…Ñ•½Éäè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€É½ÕÁ•¹Í•Ñ‘•™…Õ±Ð¡…Ñ•½Éä°mt¤¹…ÁÁ•¹¡¥Ñ•´¤((€€€½ÕÑÁÕÐè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½È…Ñ•½Éä°¥Ñ•µÌ¥¸É½ÕÁ•¹¥Ñ•µÌ ¤è(€€€€€€€É•±…Ñ•‘}Ñ¥­•ÉÌ€ô}Õ¹¥ÅÕ•}™±…ÑÑ•¸¡¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤™½È¥Ñ•´¥¸¥Ñ•µÌ¤(€€€€€€€ÍÕÁÁ½ÉÑ¥¹}…¹‘¥‘…Ñ•}¥‘Ì€ômÍÑÈ¡¥Ñ•´¹•Ð ‰…¹‘¥‘…Ñ•}¥ˆ°€ˆˆ¤¤™½È¥Ñ•´¥¸Í½ÉÑ•¡¥Ñ•µÌ°­•äõ±…µ‰‘„É½ÜèÉ½Ü¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°É•Ù•ÉÍ”õQÉÕ”¥lèÕut(€€€€€€€É•±…Ñ•‘}ÍÑ…Ñ•µ•¹ÑÌ€ôl(€€€€€€€€€€€ÍÑ…Ñ•µ•¹Ð(€€€€€€€€€€€™½ÈÍÑ…Ñ•µ•¹Ð¥¸ÍÑ…Ñ•µ•¹Ñ}…¹‘¥‘…Ñ•Ì(€€€€€€€€€€€¥˜…Ñ•½Éä¥¸ÍÑ…Ñ•µ•¹Ð¹•Ð ‰É•±…Ñ•‘}Ñ¡•µ•Ìˆ°mt¤½ÈÍ•Ð¡É•±…Ñ•‘}Ñ¥­•ÉÌ¤¹¥¹Ñ•ÉÍ•Ñ¥½¸¡ÍÑ…Ñ•µ•¹Ð¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤¤(€€€€€€€t(€€€€€€€Í½É•}½µÁ½¹•¹ÑÌ€ô}‘…¥±å}Ñ¡•µ•}Í½É•}½µÁ½¹•¹ÑÌ¡¥Ñ•µÌ°É•±…Ñ•‘}Ñ¥­•ÉÌ°É•±…Ñ•‘}ÍÑ…Ñ•µ•¹ÑÌ¤(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}Ñ¡•µ”ˆèQ!5}1	1L¹•Ð¡…Ñ•½Éä°…Ñ•½Éä¤°(€€€€€€€€€€€€€€€€‰µ…¥¹}…Ñ•½Éäˆè…Ñ•½Éä°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}…Ñ•½É¥•Ìˆèm…Ñ•½Éåt°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¥­•ÉÌˆèÉ•±…Ñ•‘}Ñ¥­•ÉÌ°(€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}½Õ¹Ðˆè±•¸¡¥Ñ•µÌ¤°(€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}Í¥¹…±}½Õ¹Ðˆè±•¸¡É•±…Ñ•‘}ÍÑ…Ñ•µ•¹ÑÌ¤°(€€€€€€€€€€€€€€€€‰É…‘…É}Í½É”ˆèÍÕ´¡Í½É•}½µÁ½¹•¹ÑÌ¹Ù…±Õ•Ì ¤¤°(€€€€€€€€€€€€€€€€‰Í½É•}½µÁ½¹•¹ÑÌˆèÍ½É•}½µÁ½¹•¹ÑÌ°(€€€€€€€€€€€€€€€€‰ÍÕÁÁ½ÉÑ¥¹}…¹‘¥‘…Ñ•}¥‘ÌˆèÍÕÁÁ½ÉÑ¥¹}…¹‘¥‘…Ñ•}¥‘Ì°(€€€€€€€€€€€€€€€€‰ÍÕÁÁ½ÉÑ¥¹}ÍÑ…Ñ•µ•¹Ñ}¥‘ÌˆèmÍÑÈ¡¥Ñ•´¹•Ð ‰ÍÑ…Ñ•µ•¹Ñ}¥ˆ°€ˆˆ¤¤™½È¥Ñ•´¥¸É•±…Ñ•‘}ÍÑ…Ñ•µ•¹ÑÍlèÕut°(€€€€€€€€€€€€€€€€‰¹••‘Í}™Õ±±Ñ•áÐˆèQÉÕ”°(€€€€€€€€€€€€€€€€¨©U9YI%%}9%Q}1L°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€½ÕÑÁÕÐ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸½ÕÑÁÕÑlé5a}%1e}1}Q!5}9%QMt(()‘•˜}‘…¥±å}Ñ¡•µ•}Í½É•}½µÁ½¹•¹ÑÌ (€€€¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(€€€É•±…Ñ•‘}Ñ¥­•ÉÌè±¥ÍÑmÍÑÉt°(€€€ÍÑ…Ñ•µ•¹Ñ}…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°(¤€´ø‘¥ÑmÍÑÈ°¥¹Ñtè(€€€…Ù•É…•}É…‘…È€ô¥¹Ð¡ÍÕ´¡¥¹Ð¡¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ¤½È€À¤™½È¥Ñ•´¥¸¥Ñ•µÌ¤€¼µ…à¡±•¸¡¥Ñ•µÌ¤°€Ä¤¤(€€€Í½ÕÉ•}½Õ¹Ð€ô±•¸¡íÍÑÈ¡¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ°€ˆˆ¤¤¹±½Ý•È ¤™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¥ô¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰…¹‘¥‘…Ñ•}Ù½±Õµ”ˆèµ¥¸¡±•¸¡¥Ñ•µÌ¤€¨€ÄÀ°€ÌÀ¤°(€€€€€€€€‰…Ù•É…•}É…‘…É}Í½É•}½µÁ½¹•¹Ðˆèµ¥¸¡¥¹Ð¡…Ù•É…•}É…‘…È€¨€À¸ÌÔ¤°€ÐÀ¤°(€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}Í¥¹…±}½µÁ½¹•¹Ðˆèµ¥¸¡±•¸¡ÍÑ…Ñ•µ•¹Ñ}…¹‘¥‘…Ñ•Ì¤€¨€à°€ÈÀ¤°(€€€€€€€€‰Ñ¥­•É}‰É•…‘Ñ¡}½µÁ½¹•¹Ðˆèµ¥¸¡±•¸¡mÑ¥­•È™½ÈÑ¥­•È¥¸É•±…Ñ•‘}Ñ¥­•ÉÌ¥˜Ñ¥­•È€„ô€‰¹½}ÕÍ}Ñ¥­•È‰t¤€¨€Ì°€ÄÔ¤°(€€€€€€€€‰Í½ÕÉ•}‰É•…‘Ñ¡}½µÁ½¹•¹Ðˆèµ¥¸¡Í½ÕÉ•}½Õ¹Ð€¨€È°€ÄÀ¤°(€€€ô(()‘•˜}‰Õ¥±‘}ÍÑ…Ñ•µ•¹Ñ}É…‘…É}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€½ÕÑÁÕÐè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€Í½ÉÑ•‘}…¹‘¥‘…Ñ•Ì€ôÍ½ÉÑ•¡…¹‘¥‘…Ñ•Ì°­•äõ±…µ‰‘„¥Ñ•´è¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°É•Ù•ÉÍ”õQÉÕ”¤(€€€™½È¥Ñ•´¥¸Í½ÉÑ•‘}…¹‘¥‘…Ñ•Ìè(€€€€€€€Ñ•áÐ€ô€ˆ€ˆ¹©½¥¸ (€€€€€€€€€€€ÍÑÈ¡¥Ñ•´¹•Ð¡­•ä°€ˆˆ¤¤(€€€€€€€€€€€™½È­•ä¥¸€ ‰Ñ¥Ñ±”ˆ°€‰Í¹¥ÁÁ•Ðˆ°€‰ÕÉ°ˆ°€‰Í½ÕÉ”ˆ¤(€€€€€€€€¤¹±½Ý•È ¤(€€€€€€€ÍÁ•…­•È€ô}‘•Ñ•Ñ}ÍÁ•…­•È¡Ñ•áÐ¤(€€€€€€€Ñ½Á¥Ì€ô}‘•Ñ•Ñ}ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥Ì¡Ñ•áÐ¤(€€€€€€€¥˜ÍÁ•…­•È¥Ì9½¹”½È¹½ÐÑ½Á¥Ìè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Í½ÕÉ•}ÑåÁ”€ô}ÍÑ…Ñ•µ•¹Ñ}Í½ÕÉ•}ÑåÁ”¡ÍÑÈ¡¥Ñ•´¹•Ð ‰‘•±Ñ}‘½µ…¥¸ˆ°€ˆˆ¤¤°Ñ•áÐ¤(€€€€€€€Ù•É¥™¥…Ñ¥½¸€ô}Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌ¡Í½ÕÉ•}ÑåÁ”°¥¹Ð¡¥Ñ•´¹•Ð ‰Í½É•}½µÁ½¹•¹ÑÌˆ°íô¤¹•Ð ‰Í½ÕÉ•}ÅÕ…±¥Ñäˆ¤½È€À¤¤(€€€€€€€ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ€ô}ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ¡ÍÑÈ¡¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ°€ˆˆ¤¤¤(€€€€€€€ÅÕ½Ñ•}…Ù…¥±…‰±”€ô‰½½°¡ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ¤(€€€€€€€Ñ•±½Á}…¹‘¥‘…Ñ”€ôÙ•É¥™¥…Ñ¥½¸€„ô€‰Õ¹Ù•É¥™¥•ˆ…¹ÅÕ½Ñ•}…Ù…¥±…‰±”(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}¥ˆè€‰ÍÑµÐ´ˆ€¬¡…Í¡±¥ˆ¹Í¡„Ä (€€€€€€€€€€€€€€€€€€€˜‰í¥Ñ•´¹•Ð …¹‘¥‘…Ñ•}¥œ°œœ¥õñíÍÁ•…­•Él¹…µ”uõñíÑ½Á¥ÍlÁuôˆ¹•¹½‘” ‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰¥¹½É”ˆ¤(€€€€€€€€€€€€€€€€¤¹¡•á‘¥•ÍÐ ¥lèÄÉt°(€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…¹‘¥‘…Ñ•}¥ˆè¥Ñ•´¹•Ð ‰…¹‘¥‘…Ñ•}¥ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰¥Í}ÍÑ…Ñ•µ•¹Ñ}…¹‘¥‘…Ñ”ˆèQÉÕ”°(€€€€€€€€€€€€€€€€‰ÍÁ•…­•É}¹…µ”ˆèÍÁ•…­•Él‰¹…µ”‰t°(€€€€€€€€€€€€€€€€‰ÍÁ•…­•É}É½±•}½É}•¹Ñ¥ÑäˆèÍÁ•…­•Él‰É½±”‰t°(€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥ŒˆèÑ½Á¥ÍlÁt°(€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÕÉ°ˆè¥Ñ•´¹•Ð ‰ÕÉ°ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÁÕ‰±¥Í¡•‘}…Ðˆè¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}Í½ÕÉ•}ÑåÁ”ˆèÍ½ÕÉ•}ÑåÁ”°(€€€€€€€€€€€€€€€€‰ÅÕ½Ñ•}…¹‘¥‘…Ñ•}…Ù…¥±…‰±”ˆèÅÕ½Ñ•}…Ù…¥±…‰±”°(€€€€€€€€€€€€€€€€‰ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐˆèÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ°(€€€€€€€€€€€€€€€€‰ÅÕ½Ñ•}±…¹Õ…”ˆè€‰Õ¹­¹½Ý¸ˆ¥˜¹½ÐÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ•±Í”}ÅÕ½Ñ•}±…¹Õ…”¡ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ¤°(€€€€€€€€€€€€€€€€‰Ñ•±½Á}…¹‘¥‘…Ñ”ˆèÑ•±½Á}…¹‘¥‘…Ñ”°(€€€€€€€€€€€€€€€€‰Ñ•±½Á}É•…Í½¸ˆè}Ñ•±½Á}É•…Í½¸¡Ñ•±½Á}…¹‘¥‘…Ñ”°Ù•É¥™¥…Ñ¥½¸°ÅÕ½Ñ•}…Ù…¥±…‰±”¤°(€€€€€€€€€€€€€€€€‰Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌˆèÙ•É¥™¥…Ñ¥½¸°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¥­•ÉÌˆè¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¡•µ•Ìˆè}Õ¹¥ÅÕ•}Ù…±Õ•Ì¡m¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤°€©Ñ½Á¥Ít¤°(€€€€€€€€€€€€€€€€‰É…‘…É}Í½É”ˆè¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°(€€€€€€€€€€€€€€€€¨©U9YI%%}9%Q}1L°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€€€€€¥˜±•¸¡½ÕÑÁÕÐ¤€øô5a}MQQ59Q}II}9%QLè(€€€€€€€€€€€‰É•…¬(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}‰Õ¥±‘}ÍÑ…Ñ•µ•¹Ñ}É…‘…É}ÍÑ…ÑÕÌ¡…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€Ñ½Á}ÍÁ•…­•ÉÌ€ô}½Õ¹Ñ}Ñ½À¡…¹‘¥‘…Ñ•Ì°€‰ÍÁ•…­•É}¹…µ”ˆ¤(€€€Ñ½Á}Ñ½Á¥Ì€ô}½Õ¹Ñ}Ñ½À¡…¹‘¥‘…Ñ•Ì°€‰ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥Œˆ¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰½¬ˆ¥˜…¹‘¥‘…Ñ•Ì•±Í”€‰•µÁÑäˆ°(€€€€€€€€‰…¹‘¥‘…Ñ•}½Õ¹Ðˆè±•¸¡…¹‘¥‘…Ñ•Ì¤°(€€€€€€€€‰Ñ•±½Á}…¹‘¥‘…Ñ•}½Õ¹ÐˆèÍÕ´ Ä™½È¥Ñ•´¥¸…¹‘¥‘…Ñ•Ì¥˜¥Ñ•´¹•Ð ‰Ñ•±½Á}…¹‘¥‘…Ñ”ˆ¤¤°(€€€€€€€€‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ•}½Õ¹Ðˆèµ¥¸¡±•¸¡…¹‘¥‘…Ñ•Ì¤°5a}MQQ59Q}U11QaQ}9%QL¤°(€€€€€€€€‰Ñ½Á}ÍÁ•…­•ÉÌˆèÑ½Á}ÍÁ•…­•ÉÌ°(€€€€€€€€‰Ñ½Á}Ñ½Á¥ÌˆèÑ½Á}Ñ½Á¥Ì°(€€€€€€€€‰¹½Ñ”ˆè€‰MÑ…Ñ•µ•¹ÐI…‘…È¥Ì„µ•Ñ…‘…Ñ„µ½¹±ä±¥ÍÐ½˜…ÉÑ¥±•ÌÑ¡…Ðµ…ä½¹Ñ…¥¸ÍÑ…Ñ•µ•¹ÑÌ¸	½‘äÙ•É¥™¥…Ñ¥½¸¥ÌÉ•ÅÕ¥É•‰•™½É”ÍÉ¥ÁÐÕÍ”¸ˆ°(€€€ô(()‘•˜}‰Õ¥±‘}ÍÑ…Ñ•µ•¹Ñ}™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ•Ì¡…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€Í½ÉÑ•‘}…¹‘¥‘…Ñ•Ì€ôÍ½ÉÑ• (€€€€€€€…¹‘¥‘…Ñ•Ì°(€€€€€€€­•äõ±…µ‰‘„¥Ñ•´è€ (€€€€€€€€€€€¥Ñ•´¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌˆ¤€ôô€‰½™™¥¥…±}Í½ÕÉ”ˆ°(€€€€€€€€€€€¥Ñ•´¹•Ð ‰Ñ•±½Á}…¹‘¥‘…Ñ”ˆ¤¥ÌQÉÕ”°(€€€€€€€€€€€¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°(€€€€€€€€¤°(€€€€€€€É•Ù•ÉÍ”õQÉÕ”°(€€€€¤(€€€½ÕÑÁÕÐ€ômt(€€€™½È¥Ñ•´¥¸Í½ÉÑ•‘}…¹‘¥‘…Ñ•Ílé5a}MQQ59Q}U11QaQ}9%QMtè(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰ÍÁ•…­•É}¹…µ”ˆè¥Ñ•´¹•Ð ‰ÍÁ•…­•É}¹…µ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÍÁ•…­•É}É½±•}½É}•¹Ñ¥Ñäˆè¥Ñ•´¹•Ð ‰ÍÁ•…­•É}É½±•}½É}•¹Ñ¥Ñäˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥Œˆè¥Ñ•´¹•Ð ‰ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥Œˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÕÉ°ˆè¥Ñ•´¹•Ð ‰ÕÉ°ˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰ÁÕ‰±¥Í¡•‘}…Ðˆè¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¥­•ÉÌˆè¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¡•µ•Ìˆè¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¡•µ•Ìˆ°mt¤°(€€€€€€€€€€€€€€€€‰Ñ•±½Á}…¹‘¥‘…Ñ”ˆè¥Ñ•´¹•Ð ‰Ñ•±½Á}…¹‘¥‘…Ñ”ˆ°…±Í”¤°(€€€€€€€€€€€€€€€€‰Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌˆè¥Ñ•´¹•Ð ‰Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌˆ°€ˆˆ¤°(€€€€€€€€€€€€€€€€¨©U9YI%%}9%Q}1L°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}‘•Ñ•Ñ}ÍÁ•…­•È¡Ñ•áÐèÍÑÈ¤€´ø‘¥ÑmÍÑÈ°¹åtð9½¹”è(€€€™½ÈÍÁ•…­•È¥¸MA-I}QIQLè(€€€€€€€¥˜…¹ä¡}Ñ•Éµ}µ…Ñ ¡Ñ•áÐ°…±¥…Ì¤™½È…±¥…Ì¥¸ÍÁ•…­•Él‰…±¥…Í•Ì‰t¤è(€€€€€€€€€€€É•ÑÕÉ¸ÍÁ•…­•È(€€€É•ÑÕÉ¸9½¹”(()‘•˜}‘•Ñ•Ñ}ÍÑ…Ñ•µ•¹Ñ}Ñ½Á¥Ì¡Ñ•áÐèÍÑÈ¤€´ø±¥ÍÑmÍÑÉtè(€€€Ñ½Á¥Ì€ômt(€€€™½ÈÑ½Á¥Œ°Ñ•ÉµÌ¥¸MQQ59Q}Q=A%}QI5L¹¥Ñ•µÌ ¤è(€€€€€€€¥˜…¹ä¡}Ñ•Éµ}µ…Ñ ¡Ñ•áÐ°Ñ•É´¤™½ÈÑ•É´¥¸Ñ•ÉµÌ¤è(€€€€€€€€€€€Ñ½Á¥Ì¹…ÁÁ•¹¡Ñ½Á¥Œ¤(€€€É•ÑÕÉ¸Ñ½Á¥Ì(()‘•˜}ÍÑ…Ñ•µ•¹Ñ}Í½ÕÉ•}ÑåÁ”¡‘½µ…¥¸èÍÑÈ°Ñ•áÐèÍÑÈ¤€´øÍÑÈè(€€€¥˜…¹ä¡µ…É­•È¥¸‘½µ…¥¸™½Èµ…É­•È¥¸=%%1}=5%9}5I-IL¤è(€€€€€€€É•ÑÕÉ¸€‰½™™¥¥…°ˆ(€€€¥˜€‰ÑÉ…¹ÍÉ¥ÁÐˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‰ÑÉ…¹ÍÉ¥ÁÐˆ(€€€¥˜€‰¥¹Ñ•ÉÙ¥•Üˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‰¥¹Ñ•ÉÙ¥•Üˆ(€€€¥˜…¹ä¡µ…É­•È¥¸Ñ•áÐ™½Èµ…É­•È¥¸I)Q}=5%9}5I-IL¤è(€€€€€€€É•ÑÕÉ¸€‰Í½¥…±}Á½ÍÐˆ(€€€¥˜€‰Í…¥ˆ¥¸Ñ•áÐ½È€‰Í…åÌˆ¥¸Ñ•áÐ½È€‰Ñ½±ˆ¥¸Ñ•áÐ½È€‰É•µ…É­Ìˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‰¹•ÝÍ}É•Á½ÉÐˆ(€€€É•ÑÕÉ¸€‰Õ¹­¹½Ý¸ˆ(()‘•˜}Ù•É¥™¥…Ñ¥½¹}ÍÑ…ÑÕÌ¡Í½ÕÉ•}ÑåÁ”èÍÑÈ°Í½ÕÉ•}ÅÕ…±¥Ñäè¥¹Ð¤€´øÍÑÈè(€€€¥˜Í½ÕÉ•}ÑåÁ”€ôô€‰½™™¥¥…°ˆè(€€€€€€€É•ÑÕÉ¸€‰½™™¥¥…±}Í½ÕÉ”ˆ(€€€¥˜Í½ÕÉ•}ÑåÁ”¥¸ì‰¹•ÝÍ}É•Á½ÉÐˆ°€‰ÑÉ…¹ÍÉ¥ÁÐˆ°€‰¥¹Ñ•ÉÙ¥•Ü‰ô…¹Í½ÕÉ•}ÅÕ…±¥Ñä€øô€ÄÔè(€€€€€€€É•ÑÕÉ¸€‰É•Á½ÉÑ•‘}‰å}µ•‘¥„ˆ(€€€É•ÑÕÉ¸€‰Õ¹Ù•É¥™¥•ˆ(()‘•˜}ÅÕ½Ñ•}Ñ•áÑ}Í¡½ÉÐ¡Í¹¥ÁÁ•ÐèÍÑÈ¤€´øÍÑÈè(€€€¥˜¹½ÐÍ¹¥ÁÁ•Ðè(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€Á…ÑÑ•É¹Ì€ôl(€€€€€€€Èœˆ¡mx‰uìÄÀ°ÄàÁô¤ˆœ°(€€€€€€€È‹Šp¡m{ŠuuìÄÀ°ÄàÁô§Štˆ°(€€€€€€€È‹Š`¡m{ŠeuìÄÀ°ÄàÁô§Šdˆ°(€€€t(€€€™½ÈÁ…ÑÑ•É¸¥¸Á…ÑÑ•É¹Ìè(€€€€€€€µ…Ñ €ôÉ”¹Í•…É ¡Á…ÑÑ•É¸°Í¹¥ÁÁ•Ð¤(€€€€€€€¥˜µ…Ñ è(€€€€€€€€€€€É•ÑÕÉ¸±…µÁ}Ñ•áÐ¡µ…Ñ ¹É½ÕÀ Ä¤°€ÄàÀ¤(€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}ÅÕ½Ñ•}±…¹Õ…”¡Ñ•áÐèÍÑÈ¤€´øÍÑÈè(€€€¥˜É”¹Í•…É ¡È‰mqÔÌÀÐÀµqÔÌÁ™™qÔÌÐÀÀµqÔå™™™tˆ°Ñ•áÐ¤è(€€€€€€€É•ÑÕÉ¸€‰¹½¹}•¹±¥Í¡}½É}µ¥á•ˆ(€€€É•ÑÕÉ¸€‰•¹±¥Í ˆ(()‘•˜}Ñ•±½Á}É•…Í½¸¡Ñ•±½Á}…¹‘¥‘…Ñ”è‰½½°°Ù•É¥™¥…Ñ¥½¸èÍÑÈ°ÅÕ½Ñ•}…Ù…¥±…‰±”è‰½½°¤€´øÍÑÈè(€€€¥˜Ñ•±½Á}…¹‘¥‘…Ñ”è(€€€€€€€É•ÑÕÉ¸€‰Í¡½ÉÐÅÕ½Ñ”…¹‘¥‘…Ñ”™½Õ¹ì‰½‘äÙ•É¥™¥…Ñ¥½¸ÍÑ¥±°É•ÅÕ¥É•‰•™½É”Ñ•±½ÀÕÍ”ˆ(€€€¥˜Ù•É¥™¥…Ñ¥½¸€ôô€‰Õ¹Ù•É¥™¥•ˆè(€€€€€€€É•ÑÕÉ¸€‰Õ¹Ù•É¥™¥•Í½ÕÉ”ì‘¼¹½ÐÕÍ”™½ÈÑ•±½À‰•™½É”™Õ±±Ñ•áÐÙ•É¥™¥…Ñ¥½¸ˆ(€€€¥˜¹½ÐÅÕ½Ñ•}…Ù…¥±…‰±”è(€€€€€€€É•ÑÕÉ¸€‰¹¼•áÁ±¥¥ÐÍ¡½ÉÐÅÕ½Ñ”¥¸1PÍ¹¥ÁÁ•Ðˆ(€€€É•ÑÕÉ¸€‰‰½‘äÙ•É¥™¥…Ñ¥½¸É•ÅÕ¥É•ˆ(()‘•˜}½Õ¹Ñ}Ñ½À¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°­•äèÍÑÈ°±¥µ¥Ðè¥¹Ð€ô€Ô¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€½Õ¹ÑÌè‘¥ÑmÍÑÈ°¥¹Ñt€ôíô(€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€Ù…±Õ”€ôÍÑÈ¡¥Ñ•´¹•Ð¡­•ä°€ˆˆ¤¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜Ù…±Õ”è(€€€€€€€€€€€½Õ¹ÑÍmÙ…±Õ•t€ô½Õ¹ÑÌ¹•Ð¡Ù…±Õ”°€À¤€¬€Ä(€€€É½ÝÌ€ômì‰¹…µ”ˆè¹…µ”°€‰½Õ¹Ðˆè½Õ¹Ñô™½È¹…µ”°½Õ¹Ð¥¸½Õ¹ÑÌ¹¥Ñ•µÌ ¥t(€€€É½ÝÌ¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è€ µ¥¹Ð¡¥Ñ•µl‰½Õ¹Ð‰t¤°¥Ñ•µl‰¹…µ”‰t¤¤(€€€É•ÑÕÉ¸É½ÝÍlé±¥µ¥Ñt(()‘•˜}Õ¹¥ÅÕ•}™±…ÑÑ•¸¡É½ÕÁÌè¹ä¤€´ø±¥ÍÑmÍÑÉtè(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€™½ÈÉ½ÕÀ¥¸É½ÕÁÌè(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡É½ÕÀ°±¥ÍÐ¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€™½ÈÙ…±Õ”¥¸É½ÕÀè(€€€€€€€€€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜Ñ•áÐ…¹Ñ•áÐ¹½Ð¥¸Í••¸è(€€€€€€€€€€€€€€€Í••¸¹…‘¡Ñ•áÐ¤(€€€€€€€€€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡Ñ•áÐ¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}Õ¹¥ÅÕ•}Ù…±Õ•Ì¡Ù…±Õ•Ìè±¥ÍÑm¹åt¤€´ø±¥ÍÑmÍÑÉtè(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€™½ÈÙ…±Õ”¥¸Ù…±Õ•Ìè(€€€€€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜Ñ•áÐ…¹Ñ•áÐ¹½Ð¥¸Í••¸è(€€€€€€€€€€€Í••¸¹…‘¡Ñ•áÐ¤(€€€€€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡Ñ•áÐ¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}Í½ÕÉ•}Á…­}…¹‘¥‘…Ñ”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€É•ÑÕÉ¸ì(€€€€€€€€‰…Ñ•½Éäˆè¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ°€ˆˆ¤°(€€€€€€€€‰Ñ¥Ñ±”ˆè¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ°€ˆˆ¤°(€€€€€€€€‰Í½ÕÉ”ˆè¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ°€ˆˆ¤°(€€€€€€€€‰ÕÉ°ˆè¥Ñ•´¹•Ð ‰ÕÉ°ˆ°€ˆˆ¤°(€€€€€€€€‰ÁÕ‰±¥Í¡•‘}…Ðˆè¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ°€ˆˆ¤°(€€€€€€€€‰Í••¹}…Ðˆè¥Ñ•´¹•Ð ‰Í••¹}…Ðˆ°€ˆˆ¤°(€€€€€€€€‰Í¹¥ÁÁ•Ðˆè¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ°€ˆˆ¤°(€€€€€€€€‰É•±…Ñ•‘}Ñ¥­•ÉÌˆè¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤°(€€€€€€€€‰É…‘…É}Í½É”ˆè¥Ñ•´¹•Ð ‰É…‘…É}Í½É”ˆ°€À¤°(€€€€€€€€‰‘•¥Í¥½¸ˆè¥Ñ•´¹•Ð ‰‘•¥Í¥½¸ˆ°€ˆˆ¤°(€€€€€€€€‰‘•¥Í¥½¹}É•…Í½¸ˆè¥Ñ•´¹•Ð ‰‘•¥Í¥½¹}É•…Í½¸ˆ°€ˆˆ¤°(€€€€€€€€‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ”ˆè¥Ñ•´¹•Ð ‰™Õ±±Ñ•áÑ}…¹‘¥‘…Ñ”ˆ°…±Í”¤°(€€€€€€€€¨©U9YI%%}9%Q}1L°(€€€ô(()‘•˜}ÅÕ•Éå}É•½É¡…Ñ•½ÉäèÍÑÈ°ÅÕ•ÉäèÍÑÈ°ÍÑ…ÑÕÌèÍÑÈ°•ÉÉ½ÈèÍÑÈ°É•ÍÕ±Ñ}½Õ¹Ðè¥¹Ð¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€É•ÑÕÉ¸ì(€€€€€€€€‰…Ñ•½Éäˆè…Ñ•½Éä°(€€€€€€€€‰ÅÕ•ÉäˆèÅÕ•Éä°(€€€€€€€€‰ÍÑ…ÑÕÌˆèÍÑ…ÑÕÌ°(€€€€€€€€‰•ÉÉ½Èˆè•ÉÉ½È°(€€€€€€€€‰É•ÍÕ±Ñ}½Õ¹ÐˆèÉ•ÍÕ±Ñ}½Õ¹Ð°(€€€ô(()‘•˜}ÍÑ…ÑÕÍ}™É½µ}Á…å±½…¡Á…å±½…è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€ÍÑ…ÑÕÌ€ôÍÑÈ¡Á…å±½…¹•Ð ‰ÍÑ…ÑÕÌˆ°€ˆˆ¤½È€ˆˆ¤(€€€¥˜ÍÑ…ÑÕÌè(€€€€€€€É•ÑÕÉ¸ÍÑ…ÑÕÌ(€€€ÍÕµµ…Éä€ôÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤(€€€¥˜ÍÕµµ…Éä¹•Ð ‰…•ÁÑ•‘}½Õ¹Ðˆ¤è(€€€€€€€É•ÑÕÉ¸€‰½¬ˆ(€€€¥˜ÍÕµµ…Éä¹•Ð ‰É…Ý}É•ÍÕ±Ñ}½Õ¹Ðˆ¤è(€€€€€€€É•ÑÕÉ¸€‰•µÁÑäˆ(€€€É•ÑÕÉ¸€‰•ÉÉ½Èˆ(()‘•˜}‘½µ…¥¸¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€¥˜Ñ•áÐ¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¡ÑÑÀè¼¼ˆ°€‰¡ÑÑÁÌè¼¼ˆ¤¤è(€€€€€€€Á…ÉÍ•€ôÕÉ±Á…ÉÍ”¡Ñ•áÐ¤(€€€€€€€Ñ•áÐ€ôÁ…ÉÍ•¹¹•Ñ±½Œ(€€€É•ÑÕÉ¸Ñ•áÐ¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(()‘•˜}¹½Éµ…±¥é•}‘…Ñ”¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€Á…ÉÍ•€ô}Á…ÉÍ•}‘…Ñ•Ñ¥µ”¡ÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¤(€€€¥˜Á…ÉÍ•¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸ÍÑÉ¥Á}¡Ñµ°¡Ù…±Õ”¤(€€€É•ÑÕÉ¸Á…ÉÍ•¹…ÍÑ¥µ•é½¹”¡UQ¤¹¥Í½™½Éµ…Ð¡Ñ¥µ•ÍÁ•Œô‰Í•½¹‘Ìˆ¤¹É•Á±…” ˆ¬ÀÀèÀÀˆ°€‰hˆ¤(()‘•˜}Á…ÉÍ•}‘…Ñ•Ñ¥µ”¡Ù…±Õ”è¹ä¤€´ø‘…Ñ•Ñ¥µ”ð9½¹”è(€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸9½¹”(€€€¥˜Ñ•áÐ¹•¹‘ÍÝ¥Ñ  ‰hˆ¤è(€€€€€€€Ñ•áÐ€ôÑ•áÑlè´Åt€¬€ˆ¬ÀÀèÀÀˆ(€€€™½È™µÐ¥¸€ ˆ•d•´•• •4•Lˆ°€ˆ•d•´•‘P• •4•Mhˆ°€ˆ•d´•´´•€• è•4è•Lˆ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡Ñ•áÐ°™µÐ¤¹É•Á±…”¡Ñé¥¹™¼õUQ¤(€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€Á…ÍÌ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹É•Á±…”¡Ñé¥¹™¼õUQ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€Á…ÍÌ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ôÁ…ÉÍ•‘…Ñ•}Ñ½}‘…Ñ•Ñ¥µ”¡Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹É•Á±…”¡Ñé¥¹™¼õUQ¤(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È°%¹‘•áÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€É•ÑÕÉ¸9½¹”(()‘•˜}©ÍÑ}¹½Ü ¤€´øÍÑÈè(€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤¹…ÍÑ¥µ•é½¹”¡Ñ¥µ•é½¹”¡Ñ¥µ•‘•±Ñ„¡¡½ÕÉÌôä¤¤¤¹¥Í½™½Éµ…Ð¡Ñ¥µ•ÍÁ•Œô‰Í•½¹‘Ìˆ¤(