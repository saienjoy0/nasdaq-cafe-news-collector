from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.config import RunConfig

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


MIN_ARTICLE_CHARS = 450
MAX_SUPPLEMENT_TARGETS = 2
MAX_FALLBACK_QUERIES = 4
MAX_FALLBACK_QUERIES_PER_ARTICLE = 2
REQUEST_TIMEOUT_SECONDS = 14

FALLBACK_ELIGIBLE_STATUSES = {"blocked", "extraction_failed", "rate_limited", "failed"}

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BLOCKED_TEXT_MARKERS = (
    "access denied",
    "are you a robot",
    "captcha",
    "verify you are human",
    "enable javascript",
    "please enable cookies",
    "login to continue",
    "log in to continue",
    "sign in to continue",
)

PAYWALL_TEXT_MARKERS = (
    "subscribe to continue reading",
    "subscribe to read",
    "already a subscriber",
    "for subscribers only",
    "subscription required",
)

BOILERPLATE_LINES = (
    "in this article",
    "watch live",
    "subscribe",
    "sign up",
    "all rights reserved",
    "please try using other words",
    "thank you for your patience",
    "oops, something went wrong",
    "read more",
    "click here",
)

THEME_TERMS = (
    "nasdaq",
    "sox",
    "semiconductor",
    "semiconductors",
    "chip",
    "chips",
    "chipmaker",
    "nvidia",
    "nvda",
    "ai",
    "samsung",
    "treasury",
    "yield",
    "tech stocks",
)

QUALITY_DOMAINS = (
    "finance.yahoo.com",
    "nasdaq.com",
    "marketwatch.com",
    "cnbc.com",
    "barchart.com",
    "benzinga.com",
    "businesswire.com",
    "techcrunch.com",
    "reuters.com",
)

REJECT_DOMAINS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "reddit.com",
    "stocktwits.com",
    "youtube.com",
    "instagram.com",
    "tiktok.com",
)


@dataclass(frozen=True)
class FullTextTarget:
    review_priority: str
    source_group: str
    title: str
    source: str
    published_at: str
    primary_url: str
    related_tickers: list[str]
    related_indexes: list[str]
    snippet: str


@dataclass(frozen=True)
class FetchResult:
    access_status: str
    read_url: str
    full_text: str
    note: str


def collect_article_fulltext(
    config: RunConfig,
    article_review_targets: dict[str, Any],
    search_supplements: list[dict[str, Any]],
) -> dict[str, Any]:
    path = config.raw_dir / "article_fulltext.json"
    targets = _build_targets(article_review_targets, search_supplements)

    if path.exists() and not config.refresh:
        payload = read_json(path) or _empty_payload(config.target_date)
        return {
            "status": "ok" if payload.get("items") else "empty",
            "cache_used": True,
            "raw_path": path,
            "payload": payload,
            "summary": _summary_from_payload(payload),
            "fallback_search_supplements": payload.get("fallback_search_supplements", []),
        }

    payload = _fetch_targets(config, targets, search_supplements)
    write_json(path, payload)
    return {
        "status": "ok" if payload.get("items") else "empty",
        "cache_used": False,
        "raw_path": path,
        "payload": payload,
        "summary": _summary_from_payload(payload),
        "fallback_search_supplements": payload.get("fallback_search_supplements", []),
    }


def article_fulltext_status(result: dict[str, Any], config: RunConfig) -> dict[str, Any]:
    summary = result.get("summary", {})
    return {
        "raw_path": f"output/{config.target_date}/raw/article_fulltext.json",
        "cache_used": result.get("cache_used", False),
        "target_count": summary.get("target_count", 0),
        "readable_count": summary.get("readable_count", 0),
        "alternate_readable_count": summary.get("alternate_readable_count", 0),
        "unreadable_count": summary.get("unreadable_count", 0),
        "total_full_text_chars": summary.get("total_full_text_chars", 0),
        "fallback_attempted_count": summary.get("fallback_attempted_count", 0),
        "fallback_success_count": summary.get("fallback_success_count", 0),
        "fallback_failed_count": summary.get("fallback_failed_count", 0),
        "fallback_query_count": summary.get("fallback_query_count", 0),
        "readable_count_before_fallback": summary.get("readable_count_before_fallback", summary.get("readable_count", 0)),
        "unreadable_count_before_fallback": summary.get("unreadable_count_before_fallback", summary.get("unreadable_count", 0)),
    }


def _fetch_targets(config: RunConfig, targets: list[FullTextTarget], search_supplements: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for target in targets:
        item = _fetch_target(target, targets)
        if item.get("full_text"):
            items.append(item)
        else:
            failed_items.append(item)

    readable_before_fallback = len(items)
    unreadable_before_fallback = len(failed_items)
    fallback = _rescue_unreadable_items(config, failed_items, search_supplements=search_supplements, all_targets=targets)
    items.extend(fallback["items"])
    unreadable = fallback["unreadable"]
    summary = {
        "target_count": len(targets),
        "readable_count": sum(1 for item in items if item.get("access_status") == "readable"),
        "alternate_readable_count": sum(1 for item in items if item.get("access_status") == "alternate_readable"),
        "unreadable_count": len(unreadable),
        "total_full_text_chars": sum(int(item.get("full_text_char_count") or 0) for item in items),
        "readable_count_before_fallback": readable_before_fallback,
        "unreadable_count_before_fallback": unreadable_before_fallback,
        "fallback_attempted_count": fallback["attempted_count"],
        "fallback_success_count": fallback["success_count"],
        "fallback_failed_count": fallback["failed_count"],
        "fallback_query_count": fallback["query_count"],
    }
    return {
        "date": config.target_date,
        "generated_at_jst": _now_jst(),
        "collector": "article_fulltext_collector",
        "summary": summary,
        "items": items,
        "unreadable": unreadable,
        "fallback_search_supplements": fallback["search_supplements"],
        "fallback_attempts": fallback["attempts"],
    }


def _rescue_unreadable_items(
    config: RunConfig,
    failed_items: list[dict[str, Any]],
    *,
    search_supplements: list[dict[str, Any]],
    all_targets: list[FullTextTarget],
) -> dict[str, Any]:
    rescued_items: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    fallback_search_supplements: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    attempted_count = 0
    success_count = 0
    query_count = 0

    existing_candidates = _fallback_targets_from_search_supplements(search_supplements)
    existing_candidates.extend(all_targets)

    for failed_item in failed_items:
        if failed_item.get("access_status") not in FALLBACK_ELIGIBLE_STATUSES:
            unreadable.append(_unreadable_from_item(failed_item))
            continue

        target = _target_from_fulltext_item(failed_item)
        if not target or target.source_group not in {"core_driver", "context_candidate", "search_supplement"}:
            unreadable.append(_unreadable_from_item(failed_item))
            continue

        attempted_count += 1
        attempt_log = {
            "article_id": failed_item.get("article_id", ""),
            "title": target.title,
            "primary_url": target.primary_url,
            "initial_status": failed_item.get("access_status", ""),
            "candidate_count": 0,
            "query_count": 0,
            "result": "failed",
            "reason": "",
        }

        rescued = _try_fallback_candidates(
            target,
            failed_item,
            _rank_existing_fallback_candidates(target, existing_candidates),
            source_note="existing search_supplements/all_targets",
        )
        if rescued:
            rescued_items.append(rescued)
            success_count += 1
            attempt_log["candidate_count"] = 1
            attempt_log["result"] = "alternate_readable"
            attempt_log["reason"] = "existing candidate was readable"
            attempts.append(attempt_log)
            continue

        remaining_budget = MAX_FALLBACK_QUERIES - query_count
        if remaining_budget > 0:
            search_result = _fetch_fallback_search_candidates(config, target, remaining_budget)
            query_count += search_result["query_count"]
            attempt_log["query_count"] = search_result["query_count"]
            fallback_search_supplements.extend(search_result["candidates"])
            rescued = _try_fallback_candidates(
                target,
                failed_item,
                _rank_existing_fallback_candidates(target, search_result["targets"]),
                source_note="fallback search",
            )
            if rescued:
                rescued_items.append(rescued)
                success_count += 1
                attempt_log["candidate_count"] = len(search_result["targets"])
                attempt_log["result"] = "alternate_readable"
                attempt_log["reason"] = "fallback search candidate was readable"
                attempts.append(attempt_log)
                continue

        updated_unreadable = _unreadable_from_item(failed_item)
        updated_unreadable["reason"] = _append_rescue_note(
            updated_unreadable.get("reason", ""),
            "fallback attempted; no acceptable readable alternate found.",
        )
        updated_unreadable["notes_for_chatgpt"] = _append_rescue_note(
            updated_unreadable.get("notes_for_chatgpt", ""),
            "fallback attempted; no acceptable readable alternate found.",
        )
        unreadable.append(updated_unreadable)
        attempt_log["reason"] = "no acceptable readable alternate found"
        attempts.append(attempt_log)

    return {
        "items": rescued_items,
        "unreadable": unreadable,
        "search_supplements": _dedupe_candidate_items(fallback_search_supplements),
        "attempts": attempts,
        "attempted_count": attempted_count,
        "success_count": success_count,
        "failed_count": attempted_count - success_count,
        "query_count": query_count,
    }


def _try_fallback_candidates(
    target: FullTextTarget,
    failed_item: dict[str, Any],
    candidates: list[FullTextTarget],
    *,
    source_note: str,
) -> dict[str, Any] | None:
    for candidate in candidates:
        if candidate.primary_url.strip().lower() == target.primary_url.strip().lower():
            continue
        if not _acceptable_fallback_candidate(target, candidate):
            continue
        result = _fetch_url(candidate.primary_url)
        if not result.full_text:
            continue
        return _item_from_result(
            target,
            FetchResult(
                access_status="alternate_readable",
                read_url=result.read_url,
                full_text=result.full_text,
                note=f"primary_url was {failed_item.get('access_status', 'unreadable')}; alternate source was used.",
            ),
            used_alternate=True,
            alternate_sources=[
                {
                    "source": candidate.source,
                    "url": candidate.primary_url,
                    "reason": f"{source_note}; title/theme similarity passed; primary_url was {failed_item.get('access_status', 'unreadable')}.",
                }
            ],
        )
    return None


def _fetch_target(target: FullTextTarget, all_targets: list[FullTextTarget]) -> dict[str, Any]:
    primary = _fetch_url(target.primary_url)
    if primary.full_text:
        return _item_from_result(target, primary, used_alternate=False, alternate_sources=[])

    alternate_sources: list[dict[str, str]] = []
    for alternate in _candidate_alternates(target, all_targets):
        alternate_result = _fetch_url(alternate.primary_url)
        if alternate_result.full_text:
            alternate_sources.append(
                {
                    "source": alternate.source,
                    "url": alternate.primary_url,
                    "reason": f"primary_url was {primary.access_status}; existing candidate with similar title/theme was readable.",
                }
            )
            return _item_from_result(
                target,
                FetchResult(
                    access_status="alternate_readable",
                    read_url=alternate_result.read_url,
                    full_text=alternate_result.full_text,
                    note=f"primary_url was {primary.access_status}; alternate source was used.",
                ),
                used_alternate=True,
                alternate_sources=alternate_sources,
            )

    return _item_from_result(target, primary, used_alternate=False, alternate_sources=[])


def _fetch_url(url: str) -> FetchResult:
    clean_url = _clean(url)
    if not _valid_http_url(clean_url):
        return FetchResult("failed", clean_url, "", "invalid or missing URL; no full text retrieved.")
    try:
        response = requests.get(clean_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
    except requests.RequestException as exc:
        return FetchResult("failed", clean_url, "", f"{type(exc).__name__}: no full text retrieved.")

    read_url = response.url or clean_url
    if response.status_code == 429:
        return FetchResult("rate_limited", read_url, "", "primary_url was rate limited; no full text retrieved.")
    if response.status_code in {401, 403, 451}:
        return FetchResult("blocked", read_url, "", "article was skipped because access was blocked.")
    if response.status_code == 402:
        return FetchResult("paywalled", read_url, "", "article appears to require payment; no bypass attempted.")
    if response.status_code < 200 or response.status_code >= 300:
        return FetchResult("failed", read_url, "", f"HTTP {response.status_code}; no full text retrieved.")

    content_type = response.headers.get("content-typßo=¶‰žËkºwµçpÕ±±Q•áÑQ…É•Ñut€ômt(€€€™½È…¹‘¥‘…Ñ”¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€¥˜…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°¹ÍÑÉ¥À ¤¹±½Ý•È ¤€ôôÑ…É•Ð¹ÁÉ¥µ…Éå}ÕÉ°¹ÍÑÉ¥À ¤¹±½Ý•È ¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜}É•©•Ñ•‘}ÕÉ°¡…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Í½É”€ô}™…±±‰…­}Í¥µ¥±…É¥Ñå}Í½É”¡Ñ…É•Ð°…¹‘¥‘…Ñ”¤(€€€€€€€¥˜Í½É”€ðô€Àè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€É…¹­•¹…ÁÁ•¹ ¡Í½É”°…¹‘¥‘…Ñ”¤¤(€€€É…¹­•¹Í½ÉÐ¡­•äõ±…µ‰‘„É½ÜèÉ½ÝlÁt°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸m…¹‘¥‘…Ñ”™½È|°…¹‘¥‘…Ñ”¥¸É…¹­•‘lèÕut(()‘•˜}…•ÁÑ…‰±•}™…±±‰…­}…¹‘¥‘…Ñ”¡Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð°…¹‘¥‘…Ñ”èÕ±±Q•áÑQ…É•Ð¤€´ø‰½½°è(€€€¥˜}É•©•Ñ•‘}ÕÉ°¡…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½Ð}ÅÕ…±¥Ñå}ÕÉ°¡…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½Ð}ÁÕ‰±¥Í¡•‘}‘…Ñ•Í}±½Í”¡Ñ…É•Ð¹ÁÕ‰±¥Í¡•‘}…Ð°…¹‘¥‘…Ñ”¹ÁÕ‰±¥Í¡•‘}…Ð¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜Ñ…É•Ð¹É•±…Ñ•‘}Ñ¥­•ÉÌè(€€€€€€€…¹‘¥‘…Ñ•}Ñ¥­•ÉÌ€ôÍ•Ð¡…¹‘¥‘…Ñ”¹É•±…Ñ•‘}Ñ¥­•ÉÌ¤(€€€€€€€¥˜…¹‘¥‘…Ñ•}Ñ¥­•ÉÌ…¹¹½ÐÍ•Ð¡Ñ…É•Ð¹É•±…Ñ•‘}Ñ¥­•ÉÌ¤¹¥¹Ñ•ÉÍ•Ñ¥½¸¡…¹‘¥‘…Ñ•}Ñ¥­•ÉÌ¤è(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸}™…±±‰…­}Í¥µ¥±…É¥Ñå}Í½É”¡Ñ…É•Ð°…¹‘¥‘…Ñ”¤€øô€À¸ÐØ(()‘•˜}™…±±‰…­}Í¥µ¥±…É¥Ñå}Í½É”¡Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð°…¹‘¥‘…Ñ”èÕ±±Q•áÑQ…É•Ð¤€´ø™±½…Ðè(€€€Ñ¥Ñ±•}Í½É”€ô}Ý½É‘}½Ù•É±…Á}Í½É”¡Ñ…É•Ð¹Ñ¥Ñ±”°…¹‘¥‘…Ñ”¹Ñ¥Ñ±”¤(€€€½µ‰¥¹•‘}Ñ…É•Ð€ô˜‰íÑ…É•Ð¹Ñ¥Ñ±•ôíÑ…É•Ð¹Í¹¥ÁÁ•Ñôìœ€œ¹©½¥¸¡Ñ…É•Ð¹É•±…Ñ•‘}Ñ¥­•ÉÌ¥ôìœ€œ¹©½¥¸¡Ñ…É•Ð¹É•±…Ñ•‘}¥¹‘•á•Ì¥ôˆ(€€€½µ‰¥¹•‘}…¹‘¥‘…Ñ”€ô˜‰í…¹‘¥‘…Ñ”¹Ñ¥Ñ±•ôí…¹‘¥‘…Ñ”¹Í¹¥ÁÁ•Ñôìœ€œ¹©½¥¸¡…¹‘¥‘…Ñ”¹É•±…Ñ•‘}Ñ¥­•ÉÌ¥ôìœ€œ¹©½¥¸¡…¹‘¥‘…Ñ”¹É•±…Ñ•‘}¥¹‘•á•Ì¥ôˆ(€€€Ñ¡•µ•}‰½¹ÕÌ€ô€À¸Äà¥˜}¡…Í}Ñ¡•µ•}½Ù•É±…À¡½µ‰¥¹•‘}Ñ…É•Ð°½µ‰¥¹•‘}…¹‘¥‘…Ñ”¤•±Í”€À¸À(€€€Ñ¥­•É}‰½¹ÕÌ€ô€À¸ÄÈ¥˜Í•Ð¡Ñ…É•Ð¹É•±…Ñ•‘}Ñ¥­•ÉÌ¤¹¥¹Ñ•ÉÍ•Ñ¥½¸¡…¹‘¥‘…Ñ”¹É•±…Ñ•‘}Ñ¥­•ÉÌ¤•±Í”€À¸À(€€€Í½ÕÉ•}‰½¹ÕÌ€ô€À¸ÀÔ¥˜}Í…µ•}‘½µ…¥¹}™…µ¥±ä¡Ñ…É•Ð¹ÁÉ¥µ…Éå}ÕÉ°°…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°¤•±Í”€À¸À(€€€É•ÑÕÉ¸Ñ¥Ñ±•}Í½É”€¬Ñ¡•µ•}‰½¹ÕÌ€¬Ñ¥­•É}‰½¹ÕÌ€¬Í½ÕÉ•}‰½¹ÕÌ(()‘•˜}™•Ñ¡}™…±±‰…­}Í•…É¡}…¹‘¥‘…Ñ•Ì¡½¹™¥œèIÕ¹½¹™¥œ°Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð°É•µ…¥¹¥¹}‰Õ‘•Ðè¥¹Ð¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€ÅÕ•Éå}½Õ¹Ð€ô€À(€€€¥˜É•µ…¥¹¥¹}‰Õ‘•Ð€ðô€Àè(€€€€€€€É•ÑÕÉ¸ì‰…¹‘¥‘…Ñ•Ìˆèmt°€‰Ñ…É•ÑÌˆèmt°€‰ÅÕ•Éå}½Õ¹Ðˆè€Áô((€€€Í•ÉÁ…Á¥}­•ä€ô½¹™¥œ¹•¹Ø¹•Ð ‰MIAA%}A%}-dˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€Ñ…Ù¥±å}­•ä€ô½¹™¥œ¹•¹Ø¹•Ð ‰QY%1e}A%}-dˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€ÅÕ•É¥•Ì€ô}™…±±‰…­}ÅÕ•É¥•Ì¡Ñ…É•Ð¥lèµ¥¸¡5a}11	-}EUI%M}AI}IQ%1°É•µ…¥¹¥¹}‰Õ‘•Ð¥t(€€€™½ÈÅÕ•Éä¥¸ÅÕ•É¥•Ìè(€€€€€€€¥˜ÅÕ•Éå}½Õ¹Ð€øôÉ•µ…¥¹¥¹}‰Õ‘•Ðè(€€€€€€€€€€€‰É•…¬(€€€€€€€™•Ñ¡•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€€€€€¥˜}¥Í}½¹™¥ÕÉ•‘}…Á¥}­•ä¡Í•ÉÁ…Á¥}­•ä°€‰Í•ÉÁ…Á¤ˆ¤è(€€€€€€€€€€€™•Ñ¡•€ô}™•Ñ¡}Í•ÉÁ…Á¥}™…±±‰…¬¡Í•ÉÁ…Á¥}­•ä°ÅÕ•Éä¤(€€€€€€€€€€€ÅÕ•Éå}½Õ¹Ð€¬ô€Ä(€€€€€€€•±¥˜}¥Í}½¹™¥ÕÉ•‘}…Á¥}­•ä¡Ñ…Ù¥±å}­•ä°€‰Ñ…Ù¥±äˆ¤è(€€€€€€€€€€€™•Ñ¡•€ô}™•Ñ¡}Ñ…Ù¥±å}™…±±‰…¬¡Ñ…Ù¥±å}­•ä°ÅÕ•Éä¤(€€€€€€€€€€€ÅÕ•Éå}½Õ¹Ð€¬ô€Ä(€€€€€€€¥˜¹½Ð™•Ñ¡•è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€…¹‘¥‘…Ñ•Ì¹•áÑ•¹¡™•Ñ¡•¤((€€€¹½Éµ…±¥é•€ô}‘•‘ÕÁ•}…¹‘¥‘…Ñ•}¥Ñ•µÌ¡…¹‘¥‘…Ñ•Ì¤(€€€Ñ…É•ÑÌ€ômÑ…É•Ñ}¥Ñ•´™½È¥Ñ•´¥¸¹½Éµ…±¥é•¥˜€¡Ñ…É•Ñ}¥Ñ•´€èô}Ñ…É•Ñ}™É½µ}Í•…É¡}¥Ñ•´¡¥Ñ•´¤¥t(€€€É•ÑÕÉ¸ì‰…¹‘¥‘…Ñ•Ìˆè¹½Éµ…±¥é•°€‰Ñ…É•ÑÌˆèÑ…É•ÑÌ°€‰ÅÕ•Éå}½Õ¹ÐˆèÅÕ•Éå}½Õ¹Ñô(()‘•˜}™•Ñ¡}Í•ÉÁ…Á¥}™…±±‰…¬¡…Á¥}­•äèÍÑÈ°ÅÕ•ÉäèÍÑÈ¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€ÑÉäè(€€€€€€€É•ÍÁ½¹Í”€ôÉ•ÅÕ•ÍÑÌ¹•Ð (€€€€€€€€€€€€‰¡ÑÑÁÌè¼½Í•ÉÁ…Á¤¹½´½Í•…É ¹©Í½¸ˆ°(€€€€€€€€€€€Á…É…µÌõì‰•¹¥¹”ˆè€‰½½±”ˆ°€‰ÄˆèÅÕ•Éä°€‰…Á¥}­•äˆè…Á¥}­•ä°€‰¹Õ´ˆè€Ñô°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÄÀ°(€€€€€€€€¤(€€€€€€€É•ÍÁ½¹Í”¹É…¥Í•}™½É}ÍÑ…ÑÕÌ ¤(€€€€€€€Á…å±½…€ôÉ•ÍÁ½¹Í”¹©Í½¸ ¤(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€É•ÑÕÉ¸mt((€€€¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½ÈÉ•ÍÕ±Ð¥¸Á…å±½…¹•Ð ‰½É…¹¥}É•ÍÕ±ÑÌˆ°mt¥lèÑtè(€€€€€€€¥Ñ•µÌ¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰Ñ¥Ñ±”ˆ¤¤°(€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè€‰M•ÉÁA$ˆ°(€€€€€€€€€€€€€€€€‰ÁÕ‰±¥Í¡•‘}…Ðˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰‘…Ñ”ˆ¤¤°(€€€€€€€€€€€€€€€€‰ÕÉ°ˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰±¥¹¬ˆ¤¤°(€€€€€€€€€€€€€€€€‰Í¹¥ÁÁ•Ðˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰Í¹¥ÁÁ•Ðˆ¤¤°(€€€€€€€€€€€€€€€€‰ÅÕ•ÉäˆèÅÕ•Éä°(€€€€€€€€€€€€€€€€‰Í•…É¡}É½±”ˆè€‰…ÉÑ¥±•}™Õ±±Ñ•áÑ}™…±±‰…¬ˆ°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€É•ÑÕÉ¸m¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤…¹¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤…¹¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ¥t(()‘•˜}™•Ñ¡}Ñ…Ù¥±å}™…±±‰…¬¡…Á¥}­•äèÍÑÈ°ÅÕ•ÉäèÍÑÈ¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€ÑÉäè(€€€€€€€É•ÍÁ½¹Í”€ôÉ•ÅÕ•ÍÑÌ¹Á½ÍÐ (€€€€€€€€€€€€‰¡ÑÑÁÌè¼½…Á¤¹Ñ…Ù¥±ä¹½´½Í•…É ˆ°(€€€€€€€€€€€©Í½¸õì‰…Á¥}­•äˆè…Á¥}­•ä°€‰ÅÕ•ÉäˆèÅÕ•Éä°€‰Í•…É¡}‘•ÁÑ ˆè€‰‰…Í¥Œˆ°€‰µ…á}É•ÍÕ±ÑÌˆè€Ð°€‰¥¹±Õ‘•}É…Ý}½¹Ñ•¹Ðˆè…±Í•ô°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÄÀ°(€€€€€€€€¤(€€€€€€€É•ÍÁ½¹Í”¹É…¥Í•}™½É}ÍÑ…ÑÕÌ ¤(€€€€€€€Á…å±½…€ôÉ•ÍÁ½¹Í”¹©Í½¸ ¤(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€É•ÑÕÉ¸mt((€€€¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½ÈÉ•ÍÕ±Ð¥¸Á…å±½…¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¥lèÑtè(€€€€€€€¥Ñ•µÌ¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰Ñ¥Ñ±”ˆ¤¤°(€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè€‰Q…Ù¥±äˆ°(€€€€€€€€€€€€€€€€‰ÁÕ‰±¥Í¡•‘}…Ðˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰ÁÕ‰±¥Í¡•‘}‘…Ñ”ˆ¤¤°(€€€€€€€€€€€€€€€€‰ÕÉ°ˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰ÕÉ°ˆ¤¤°(€€€€€€€€€€€€€€€€‰Í¹¥ÁÁ•Ðˆè}±•…¸¡É•ÍÕ±Ð¹•Ð ‰½¹Ñ•¹Ðˆ¤¤°(€€€€€€€€€€€€€€€€‰ÅÕ•ÉäˆèÅÕ•Éä°(€€€€€€€€€€€€€€€€‰Í•…É¡}É½±”ˆè€‰…ÉÑ¥±•}™Õ±±Ñ•áÑ}™…±±‰…¬ˆ°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€É•ÑÕÉ¸m¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤…¹¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤…¹¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ¥t(()‘•˜}™…±±‰…­}ÅÕ•É¥•Ì¡Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð¤€´ø±¥ÍÑmÍÑÉtè(€€€Ñ¥Ñ±”€ôÑ…É•Ð¹Ñ¥Ñ±”¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÑ¥Ñ±”è(€€€€€€€É•ÑÕÉ¸mt(€€€ÅÕ•É¥•Ìè±¥ÍÑmÍÑÉt€ômt(€€€±½Ý•É•€ô˜‰íÑ…É•Ð¹Í½ÕÉ•ôíÑ…É•Ð¹ÁÉ¥µ…Éå}ÕÉ±ôˆ¹±½Ý•È ¤(€€€¥˜€‰É•ÕÑ•ÉÌˆ¥¸±½Ý•É•è(€€€€€€€ÅÕ•É¥•Ì¹•áÑ•¹¡m˜‰íÑ¥Ñ±•ôe…¡½¼¥¹…¹”ˆ°˜‰íÑ¥Ñ±•ô9…Í‘…Ä‰t¤(€€€•±¥˜€‰¹‰Œˆ¥¸±½Ý•É•è(€€€€€€€ÅÕ•É¥•Ì¹•áÑ•¹¡m˜œ‰íÑ¥Ñ±•ôˆœ°˜‰íÑ¥Ñ±•ôe…¡½¼¥¹…¹”‰t¤(€€€•±Í”è(€€€€€€€ÅÕ•É¥•Ì¹•áÑ•¹¡m˜œ‰íÑ¥Ñ±•ôˆœ°˜‰íÑ¥Ñ±•ô	…É¡…ÉÐ‰t¤(€€€É•ÑÕÉ¸}‘•‘ÕÁ•}ÍÑÉ¥¹Ì¡ÅÕ•É¥•Ì¥lé5a}11	-}EUI%M}AI}IQ%1t(()‘•˜}…¹‘¥‘…Ñ•}…±Ñ•É¹…Ñ•Ì¡Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð°…±±}Ñ…É•ÑÌè±¥ÍÑmÕ±±Q•áÑQ…É•Ñt¤€´ø±¥ÍÑmÕ±±Q•áÑQ…É•Ñtè(€€€…±Ñ•É¹…Ñ•Ìè±¥ÍÑmÑÕÁ±•m™±½…Ð°Õ±±Q•áÑQ…É•Ñut€ômt(€€€™½È…¹‘¥‘…Ñ”¥¸…±±}Ñ…É•ÑÌè(€€€€€€€¥˜…¹‘¥‘…Ñ”¹ÁÉ¥µ…Éå}ÕÉ°€ôôÑ…É•Ð¹ÁÉ¥µ…Éå}ÕÉ°è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜¹½Ð}Í…µ•}½¹Ñ•¹Ñ}…¹‘¥‘…Ñ”¡Ñ…É•Ð°…¹‘¥‘…Ñ”¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€…±Ñ•É¹…Ñ•Ì¹…ÁÁ•¹ ¡}Ý½É‘}½Ù•É±…Á}Í½É”¡Ñ…É•Ð¹Ñ¥Ñ±”°…¹‘¥‘…Ñ”¹Ñ¥Ñ±”¤°…¹‘¥‘…Ñ”¤¤(€€€…±Ñ•É¹…Ñ•Ì¹Í½ÉÐ¡­•äõ±…µ‰‘„É½ÜèÉ½ÝlÁt°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸m…¹‘¥‘…Ñ”™½È|°…¹‘¥‘…Ñ”¥¸…±Ñ•É¹…Ñ•ÍlèÅut(()‘•˜}Í…µ•}½¹Ñ•¹Ñ}…¹‘¥‘…Ñ”¡„èÕ±±Q•áÑQ…É•Ð°ˆèÕ±±Q•áÑQ…É•Ð¤€´ø‰½½°è(€€€Ñ¥Ñ±•}Í½É”€ô}Ý½É‘}½Ù•É±…Á}Í½É”¡„¹Ñ¥Ñ±”°ˆ¹Ñ¥Ñ±”¤(€€€¥˜Ñ¥Ñ±•}Í½É”€øô€À¸ÜÈè(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€É•ÑÕÉ¸Ñ¥Ñ±•}Í½É”€øô€À¸ÐÔ…¹}¡…Í}Ñ¡•µ•}½Ù•É±…À¡„¹Ñ¥Ñ±”€¬€ˆ€ˆ€¬„¹Í¹¥ÁÁ•Ð°ˆ¹Ñ¥Ñ±”€¬€ˆ€ˆ€¬ˆ¹Í¹¥ÁÁ•Ð¤(()‘•˜}Ý½É‘}½Ù•É±…Á}Í½É”¡±•™ÐèÍÑÈ°É¥¡ÐèÍÑÈ¤€´ø™±½…Ðè(€€€±•™Ñ}Ý½É‘Ì€ô}µ•…¹¥¹™Õ±}Ý½É‘Ì¡±•™Ð¤(€€€É¥¡Ñ}Ý½É‘Ì€ô}µ•…¹¥¹™Õ±}Ý½É‘Ì¡É¥¡Ð¤(€€€¥˜¹½Ð±•™Ñ}Ý½É‘Ì½È¹½ÐÉ¥¡Ñ}Ý½É‘Ìè(€€€€€€€É•ÑÕÉ¸€À¸À(€€€É•ÑÕÉ¸±•¸¡±•™Ñ}Ý½É‘Ì€˜É¥¡Ñ}Ý½É‘Ì¤€¼µ…à¡±•¸¡±•™Ñ}Ý½É‘Ì¤°±•¸¡É¥¡Ñ}Ý½É‘Ì¤¤(()‘•˜}µ•…¹¥¹™Õ±}Ý½É‘Ì¡Ù…±Õ”èÍÑÈ¤€´øÍ•ÑmÍÑÉtè(€€€ÍÑ½À€ôì‰Ñ¡”ˆ°€‰…¹ˆ°€‰™½Èˆ°€‰Ý¥Ñ ˆ°€‰™É½´ˆ°€‰Ñ¡…Ðˆ°€‰Ñ¡¥Ìˆ°€‰¥¹Ñ¼ˆ°€‰Í…åÌˆ°€‰µ…äˆ°€‰…É”ˆ°€‰Ý…Ìˆ°€‰Ý¡ä‰ô(€€€É•ÑÕÉ¸íÝ½É™½ÈÝ½É¥¸É”¹™¥¹‘…±°¡È‰m„µèÀ´åuìÌ±ôˆ°Ù…±Õ”¹±½Ý•È ¤¤¥˜Ý½É¹½Ð¥¸ÍÑ½Áô(()‘•˜}¡…Í}Ñ¡•µ•}½Ù•É±…À¡±•™ÐèÍÑÈ°É¥¡ÐèÍÑÈ¤€´ø‰½½°è(€€€±•™Ñ}Ñ•áÐ€ô±•™Ð¹±½Ý•È ¤(€€€É¥¡Ñ}Ñ•áÐ€ôÉ¥¡Ð¹±½Ý•È ¤(€€€É•ÑÕÉ¸…¹ä¡Ñ•É´¥¸±•™Ñ}Ñ•áÐ…¹Ñ•É´¥¸É¥¡Ñ}Ñ•áÐ™½ÈÑ•É´¥¸Q!5}QI5L¤(()‘•˜}ÅÕ…±¥Ñå}ÕÉ°¡Ù…±Õ”èÍÑÈ¤€´ø‰½½°è(€€€¡½ÍÐ€ôÕÉ±Á…ÉÍ”¡Ù…±Õ”¤¹¹•Ñ±½Œ¹±½Ý•È ¤¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€É•ÑÕÉ¸…¹ä¡¡½ÍÐ€ôô‘½µ…¥¸½È¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡˜ˆ¹í‘½µ…¥¹ôˆ¤™½È‘½µ…¥¸¥¸EU1%Qe}=5%9L¤(()‘•˜}É•©•Ñ•‘}ÕÉ°¡Ù…±Õ”èÍÑÈ¤€´ø‰½½°è(€€€Á…ÉÍ•€ôÕÉ±Á…ÉÍ”¡Ù…±Õ”¤(€€€¡½ÍÐ€ôÁ…ÉÍ•¹¹•Ñ±½Œ¹±½Ý•È ¤¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€Ñ•áÐ€ôÙ…±Õ”¹±½Ý•È ¤(€€€¥˜¹½ÐÁ…ÉÍ•¹Í¡•µ”¹ÍÑ…ÉÑÍÝ¥Ñ  ‰¡ÑÑÀˆ¤½È¹½Ð¡½ÍÐè(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€¥˜…¹ä¡¡½ÍÐ€ôô‘½µ…¥¸½È¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡˜ˆ¹í‘½µ…¥¹ôˆ¤™½È‘½µ…¥¸¥¸I)Q}=5%9L¤è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€É•©•Ñ•‘}µ…É­•ÉÌ€ô€ (€€€€€€€€ˆ½ÅÕ½Ñ”¼ˆ°(€€€€€€€€‰™¥¹…¹”¹å…¡½¼¹½´½ÅÕ½Ñ”ˆ°(€€€€€€€€‰™…•‰½½¬¹½´ˆ°(€€€€€€€€‰ÑÝ¥ÑÑ•È¹½´ˆ°(€€€€€€€€‰à¹½´¼ˆ°(€€€€€€€€‰±¥¹­•‘¥¸¹½´½Á½ÍÑÌˆ°(€€€€€€€€‰É•ÕÑ•ÉÌ¹½´½Á±ÕÌˆ°(€€€€¤(€€€É•ÑÕÉ¸…¹ä¡µ…É­•È¥¸Ñ•áÐ™½Èµ…É­•È¥¸É•©•Ñ•‘}µ…É­•ÉÌ¤(()‘•˜}Í…µ•}‘½µ…¥¹}™…µ¥±ä¡±•™ÐèÍÑÈ°É¥¡ÐèÍÑÈ¤€´ø‰½½°è(€€€±•™Ñ}¡½ÍÐ€ôÕÉ±Á…ÉÍ”¡±•™Ð¤¹¹•Ñ±½Œ¹±½Ý•È ¤¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€É¥¡Ñ}¡½ÍÐ€ôÕÉ±Á…ÉÍ”¡É¥¡Ð¤¹¹•Ñ±½Œ¹±½Ý•È ¤¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€¥˜¹½Ð±•™Ñ}¡½ÍÐ½È¹½ÐÉ¥¡Ñ}¡½ÍÐè(€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸±•™Ñ}¡½ÍÐ€ôôÉ¥¡Ñ}¡½ÍÐ½È±•™Ñ}¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡É¥¡Ñ}¡½ÍÐ¤½ÈÉ¥¡Ñ}¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡±•™Ñ}¡½ÍÐ¤(()‘•˜}ÁÕ‰±¥Í¡•‘}‘…Ñ•Í}±½Í”¡±•™ÐèÍÑÈ°É¥¡ÐèÍÑÈ¤€´ø‰½½°è(€€€±•™Ñ}‘…Ñ”€ô}Á…ÉÍ•}‘…Ñ”¡±•™Ð¤(€€€É¥¡Ñ}‘…Ñ”€ô}Á…ÉÍ•}‘…Ñ”¡É¥¡Ð¤(€€€¥˜±•™Ñ}‘…Ñ”¥Ì9½¹”½ÈÉ¥¡Ñ}‘…Ñ”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€É•ÑÕÉ¸…‰Ì ¡±•™Ñ}‘…Ñ”€´É¥¡Ñ}‘…Ñ”¤¹‘…åÌ¤€ðô€ÐÔ(()‘•˜}Á…ÉÍ•}‘…Ñ”¡Ù…±Õ”è¹ä¤è(€€€Ñ•áÐ€ô}±•…¸¡Ù…±Õ”¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸9½¹”(€€€¥˜Ñ•áÐ¹•¹‘ÍÝ¥Ñ  ‰hˆ¤è(€€€€€€€Ñ•áÐ€ôÑ•áÑlè´Åt€¬€ˆ¬ÀÀèÀÀˆ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¹…ÍÑ¥µ•é½¹”¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹‘…Ñ” ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€Á…ÍÌ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ôÁ…ÉÍ•‘…Ñ•}Ñ½}‘…Ñ•Ñ¥µ”¡Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¹…ÍÑ¥µ•é½¹”¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹‘…Ñ” ¤(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È°%¹‘•áÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€Á…ÍÌ(€€€™½È™µÐ¥¸€ ˆ•d´•´´•ˆ°€ˆ•ˆ€•°€•dˆ°€ˆ•€•°€•dˆ°€ˆ•´¼•¼•dˆ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡Ñ•áÐ°™µÐ¤¹‘…Ñ” ¤(€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€É•ÑÕÉ¸9½¹”(()‘•˜}É•±…Ñ•‘}¥¹‘•á•Í}™É½µ}Ñ•áÐ¡Ù…±Õ”èÍÑÈ¤€´ø±¥ÍÑmÍÑÉtè(€€€Ñ•áÐ€ôÙ…±Õ”¹±½Ý•È ¤(€€€¥¹‘•á•Ì€ômt(€€€¥˜€‰¹…Í‘…Äˆ¥¸Ñ•áÐè(€€€€€€€¥¹‘•á•Ì¹…ÁÁ•¹ ‰9MDˆ¤(€€€¥˜€‰Í½àˆ¥¸Ñ•áÐ½È€‰Í•µ¥½¹‘ÕÑ½Èˆ¥¸Ñ•áÐ½È€‰¡¥Àˆ¥¸Ñ•áÐè(€€€€€€€¥¹‘•á•Ì¹…ÁÁ•¹ ‰M=`ˆ¤(€€€É•ÑÕÉ¸¥¹‘•á•Ì(()‘•˜}ÍÕµµ…Éå}™É½µ}Á…å±½…¡Á…å±½…è‘¥ÑmÍÑÈ°¹åt¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€ÍÕµµ…Éä€ôÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤(€€€¥˜¥Í¥¹ÍÑ…¹”¡ÍÕµµ…Éä°‘¥Ð¤è(€€€€€€€É•ÑÕÉ¸ÍÕµµ…Éä(€€€¥Ñ•µÌ€ôÁ…å±½…¹•Ð ‰¥Ñ•µÌˆ°mt¤(€€€Õ¹É•…‘…‰±”€ôÁ…å±½…¹•Ð ‰Õ¹É•…‘…‰±”ˆ°mt¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰Ñ…É•Ñ}½Õ¹Ðˆè±•¸¡¥Ñ•µÌ¤€¬±•¸¡Õ¹É•…‘…‰±”¤°(€€€€€€€€‰É•…‘…‰±•}½Õ¹ÐˆèÍÕ´ Ä™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰É•…‘…‰±”ˆ¤°(€€€€€€€€‰…±Ñ•É¹…Ñ•}É•…‘…‰±•}½Õ¹ÐˆèÍÕ´ Ä™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰…±Ñ•É¹…Ñ•}É•…‘…‰±”ˆ¤°(€€€€€€€€‰Õ¹É•…‘…‰±•}½Õ¹Ðˆè±•¸¡Õ¹É•…‘…‰±”¤°(€€€€€€€€‰Ñ½Ñ…±}™Õ±±}Ñ•áÑ}¡…ÉÌˆèÍÕ´¡¥¹Ð¡¥Ñ•´¹•Ð ‰™Õ±±}Ñ•áÑ}¡…É}½Õ¹Ðˆ¤½È€À¤™½È¥Ñ•´¥¸¥Ñ•µÌ¤°(€€€€€€€€‰É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”€À°(€€€€€€€€‰Õ¹É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰Õ¹É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”±•¸¡Õ¹É•…‘…‰±”¤°(€€€€€€€€‰™…±±‰…­}…ÑÑ•µÁÑ•‘}½Õ¹ÐˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰™…±±‰…­}…ÑÑ•µÁÑ•‘}½Õ¹Ðˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”€À°(€€€€€€€€‰™…±±‰…­}ÍÕ•ÍÍ}½Õ¹ÐˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰™…±±‰…­}ÍÕ•ÍÍ}½Õ¹Ðˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”€À°(€€€€€€€€‰™…±±‰…­}™…¥±•‘}½Õ¹ÐˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰™…±±‰…­}™…¥±•‘}½Õ¹Ðˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”€À°(€€€€€€€€‰™…±±‰…­}ÅÕ•Éå}½Õ¹ÐˆèÁ…å±½…¹•Ð ‰ÍÕµµ…Éäˆ°íô¤¹•Ð ‰™…±±‰…­}ÅÕ•Éå}½Õ¹Ðˆ°€À¤¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•Ð ‰ÍÕµµ…Éäˆ¤°‘¥Ð¤•±Í”€À°(€€€ô(()‘•˜}•µÁÑå}Á…å±½…¡Ñ…É•Ñ}‘…Ñ”èÍÑÈ¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€É•ÑÕÉ¸ì(€€€€€€€€‰‘…Ñ”ˆèÑ…É•Ñ}‘…Ñ”°(€€€€€€€€‰•¹•É…Ñ•‘}…Ñ}©ÍÐˆè}¹½Ý}©ÍÐ ¤°(€€€€€€€€‰½±±•Ñ½Èˆè€‰…ÉÑ¥±•}™Õ±±Ñ•áÑ}½±±•Ñ½Èˆ°(€€€€€€€€‰ÍÕµµ…Éäˆèì(€€€€€€€€€€€€‰Ñ…É•Ñ}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰É•…‘…‰±•}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰…±Ñ•É¹…Ñ•}É•…‘…‰±•}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰Õ¹É•…‘…‰±•}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰Ñ½Ñ…±}™Õ±±}Ñ•áÑ}¡…ÉÌˆè€À°(€€€€€€€€€€€€‰É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆè€À°(€€€€€€€€€€€€‰Õ¹É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆè€À°(€€€€€€€€€€€€‰™…±±‰…­}…ÑÑ•µÁÑ•‘}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰™…±±‰…­}ÍÕ•ÍÍ}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰™…±±‰…­}™…¥±•‘}½Õ¹Ðˆè€À°(€€€€€€€€€€€€‰™…±±‰…­}ÅÕ•Éå}½Õ¹Ðˆè€À°(€€€€€€€ô°(€€€€€€€€‰¥Ñ•µÌˆèmt°(€€€€€€€€‰Õ¹É•…‘…‰±”ˆèmt°(€€€€€€€€‰™…±±‰…­}Í•…É¡}ÍÕÁÁ±•µ•¹ÑÌˆèmt°(€€€€€€€€‰™…±±‰…­}…ÑÑ•µÁÑÌˆèmt°(€€€ô(()‘•˜}…ÉÑ¥±•}¥¡Ñ…É•ÐèÕ±±Q•áÑQ…É•Ð¤€´øÍÑÈè(€€€‘¥•ÍÐ€ô¡…Í¡±¥ˆ¹Í¡„Ä¡Ñ…É•Ð¹ÁÉ¥µ…Éå}ÕÉ°¹•¹½‘” ‰ÕÑ˜´àˆ¤¤¹¡•á‘¥•ÍÐ ¥lèÄÁt(€€€ÁÉ•™¥à€ôì‰µÕÍÐˆè€‰µÕÍÐˆ°€‰½ÁÑ¥½¹…°ˆè€‰½ÁÑ¥½¹…°ˆ°€‰ÍÕÁÁ±•µ•¹Ðˆè€‰ÍÕÁÁ±•µ•¹Ð‰ô¹•Ð¡Ñ…É•Ð¹É•Ù¥•Ý}ÁÉ¥½É¥Ñä°€‰…ÉÑ¥±”ˆ¤(€€€É•ÑÕÉ¸˜‰íÁÉ•™¥áôµí‘¥•ÍÑôˆ(()‘•˜}Ù…±¥‘}¡ÑÑÁ}ÕÉ°¡Ù…±Õ”èÍÑÈ¤€´ø‰½½°è(€€€Á…ÉÍ•€ôÕÉ±Á…ÉÍ”¡Ù…±Õ”¤(€€€É•ÑÕÉ¸Á…ÉÍ•¹Í¡•µ”¥¸ì‰¡ÑÑÀˆ°€‰¡ÑÑÁÌ‰ô…¹‰½½°¡Á…ÉÍ•¹¹•Ñ±½Œ¤(()‘•˜}‘•‘ÕÁ•}©½¥¸¡±¥¹•Ìè±¥ÍÑmÍÑÉt¤€´øÍÑÈè(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt(€€€™½È±¥¹”¥¸±¥¹•Ìè(€€€€€€€¹½Éµ…±¥é•€ô}±•…¹}Ñ•áÐ¡±¥¹”¤(€€€€€€€­•ä€ô¹½Éµ…±¥é•¹±½Ý•È ¤(€€€€€€€¥˜¹½Ð¹½Éµ…±¥é•½È­•ä¥¸Í••¸è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Í••¸¹…‘¡­•ä¤(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡¹½Éµ…±¥é•¤(€€€É•ÑÕÉ¸€‰q¹q¸ˆ¹©½¥¸¡½ÕÑÁÕÐ¤(()‘•˜}…ÁÁ•¹‘}É•ÍÕ•}¹½Ñ”¡•á¥ÍÑ¥¹œèÍÑÈ°¹½Ñ”èÍÑÈ¤€´øÍÑÈè(€€€•á¥ÍÑ¥¹}±•…¸€ô}±•…¸¡•á¥ÍÑ¥¹œ¤(€€€¥˜¹½Ð•á¥ÍÑ¥¹}±•…¸è(€€€€€€€É•ÑÕÉ¸¹½Ñ”(€€€¥˜¹½Ñ”¥¸•á¥ÍÑ¥¹}±•…¸è(€€€€€€€É•ÑÕÉ¸•á¥ÍÑ¥¹}±•…¸(€€€É•ÑÕÉ¸˜‰í•á¥ÍÑ¥¹}±•…¹ôí¹½Ñ•ôˆ(()‘•˜}‘•‘ÕÁ•}…¹‘¥‘…Ñ•}¥Ñ•µÌ¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€Í••¸èÍ•ÑmÑÕÁ±•mÍÑÈ°ÍÑÉut€ôÍ•Ð ¤(€€€½ÕÑÁÕÐè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€Ñ¥Ñ±”€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤¤(€€€€€€€ÕÉ°€ô}±•…¸¡¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤¤(€€€€€€€¥˜¹½ÐÑ¥Ñ±”½È¹½ÐÕÉ°è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€­•ä€ô€¡Ñ¥Ñ±”¹±½Ý•È ¤°ÕÉ°¹±½Ý•È ¤¤(€€€€€€€¥˜­•ä¥¸Í••¸è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Í••¸¹…‘¡­•ä¤(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡¥Ñ•´¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}‘•‘ÕÁ•}ÍÑÉ¥¹Ì¡¥Ñ•µÌè±¥ÍÑmÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt(€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€±•…¹•€ô}±•…¸¡¥Ñ•´¤(€€€€€€€­•ä€ô±•…¹•¹±½Ý•È ¤(€€€€€€€¥˜±•…¹•…¹­•ä¹½Ð¥¸Í••¸è(€€€€€€€€€€€Í••¸¹…‘¡­•ä¤(€€€€€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡±•…¹•¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}¥Í}½¹™¥ÕÉ•‘}…Á¥}­•ä¡Ù…±Õ”èÍÑÈ°ÁÉ½Ù¥‘•ÈèÍÑÈ¤€´ø‰½½°è(€€€­•ä€ôÙ…±Õ”¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð­•äè(€€€€€€€É•ÑÕÉ¸…±Í”(€€€±½Ý•É•€ô­•ä¹±½Ý•È ¤(€€€¥˜±½Ý•É•¥¸%9Y1%}-e}Y1ULè(€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸±½Ý•É•€„ô˜‰å½ÕÉ}íÁÉ½Ù¥‘•Éõ}…Á¥}­•äˆ(()‘•˜}±•…¹}Ñ•áÐ¡Ù…±Õ”èÍÑÈ¤€´øÍÑÈè(€€€Ñ•áÐ€ôÉ”¹ÍÕˆ¡È‰qÌ¬ˆ°€ˆ€ˆ°Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€Ñ•áÐ€ôÑ•áÐ¹É•Á±…” ‰qÔÀÁ„Àˆ°€ˆ€ˆ¤(€€€É•ÑÕÉ¸É”¹ÍÕˆ¡Èˆ€¬ˆ°€ˆ€ˆ°Ñ•áÐ¤¹ÍÑÉ¥À ¤(()‘•˜}±•…¹}±¥ÍÐ¡Ù…±Õ”è¹ä¤€´ø±¥ÍÑmÍÑÉtè(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡Ù…±Õ”°±¥ÍÐ¤è(€€€€€€€É•ÑÕÉ¸mt(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt(€€€™½È¥Ñ•´¥¸Ù…±Õ”è(€€€€€€€±•…¹•€ô}±•…¸¡¥Ñ•´¤(€€€€€€€¥˜±•…¹•…¹±•…¹•¹½Ð¥¸½ÕÑÁÕÐè(€€€€€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡±•…¹•¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(()‘•˜}±•…¸¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€¥˜Ù…±Õ”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€É•ÑÕÉ¸ÍÑÈ¡Ù…±Õ”¤¹ÍÑÉ¥À ¤(()‘•˜}¹½Ý}©ÍÐ ¤€´øÍÑÈè(€€€©ÍÐ€ôi½¹•%¹™¼ ‰Í¥„½Q½­å¼ˆ¤¥˜i½¹•%¹™¼•±Í”Ñ¥µ•é½¹”¡Ñ¥µ•‘•±Ñ„¡¡½ÕÉÌôä¤¤(€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤¹…ÍÑ¥µ•é½¹”¡©ÍÐ¤¹É•Á±…”¡µ¥É½Í•½¹ôÀ¤¹¥Í½™½Éµ…Ð ¤(