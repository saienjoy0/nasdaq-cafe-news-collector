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

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower() and "text" not in content_type.lower():
        return FetchResult("failed", read_url, "", f"unsupported content-type {content_type}; no full text retrieved.")

    html = response.text or ""
    status = _detect_blocked_or_paywalled(html)
    if status:
        note = "paywall/login/CAPTCHA marker detected; no bypass attempted."
        return FetchResult(status, read_url, "", note)

    text = _extract_article_text(html)
    if len(text) < MIN_ARTICLE_CHARS:
        return FetchResult(
            "extraction_failed",
            read_url,
            "",
            f"extraction failed because article text was too short ({len(text)} chars).",
        )

    note = "full text extraction may include boilerplate."
    return FetchResult("readable", read_url, text, note)


def _extract_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    jsonld_text = _extract_jsonld_article_body(soup)
    if len(jsonld_text) >= MIN_ARTICLE_CHARS:
        return jsonld_text

    for tag in soup(["script", "style", "noscript", "svg", "form", "button", "nav", "footer", "aside", "iframe"]):
        tag.decompose()

    candidate_texts: list[str] = []
    selectors = (
        "article",
        "main",
        "[role='main']",
        "[data-module*='Article']",
        ".ArticleBody-articleBody",
        ".article-body",
        ".body__content",
        ".caas-body",
        ".content",
    )
    for selector in selectors:
        for node in soup.select(selector):
            text = _paragraph_text_from_node(node)
            if text:
                candidate_texts.append(text)

    body_text = _paragraph_text_from_node(soup.body or soup)
    if body_text:
        candidate_texts.append(body_text)
    if jsonld_text:
        candidate_texts.append(jsonld_text)

    if not candidate_texts:
        return ""
    return max(candidate_texts, key=len)


def _extract_jsonld_article_body(soup: BeautifulSoup) -> str:
    bodies: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        bodies.extend(_walk_json_for_article_body(payload))
    return _clean_text("\n\n".join(bodies))


def _walk_json_for_article_body(value: Any) -> list[str]:
    bodies: list[str] = []
    if isinstance(value, dict):
        article_body = value.get("articleBody")
        if isinstance(article_body, str):
            bodies.append(article_body)
        for child in value.values():
            bodies.extend(_walk_json_for_article_body(child))
    elif isinstance(value, list):
        for child in value:
            bodies.extend(_walk_json_for_article_body(child))
    return bodies


def _paragraph_text_from_node(node: Any) -> str:
    blocks: list[str] = []
    for element in node.find_all(["h2", "h3", "p", "li"], recursive=True):
        line = _clean_text(element.get_text(" ", strip=True))
        if _keep_line(line):
            blocks.append(line)
    return _dedupe_join(blocks)


def _keep_line(line: str) -> bool:
    lowered = line.lower().strip()
    if len(lowered) < 25:
        return False
    if any(marker in lowered for marker in BOILERPLATE_LINES):
        return False
    if lowered.startswith(("advertisement", "related:", "image source", "getty images")):
        return False
    return True


def _detect_blocked_or_paywalled(html: str) -> str:
    sample = BeautifulSoup(html[:120_000], "html.parser").get_text(" ", strip=True).lower()
    if any(marker in sample for marker in PAYWALL_TEXT_MARKERS):
        return "paywalled"
    if any(marker in sample for marker in BLOCKED_TEXT_MARKERS):
        return "blocked"
    return ""


def _item_from_result(
    target: FullTextTarget,
    result: FetchResult,
    *,
    used_alternate: bool,
    alternate_sources: list[dict[str, str]],
) -> dict[str, Any]:
    full_text = result.full_text
    return {
        "article_id": _article_id(target),
        "review_priority": target.review_priority,
        "source_group": target.source_group,
        "title": target.title,
        "source": target.source,
        "published_at": target.published_at,
        "primary_url": target.primary_url,
        "read_url": result.read_url,
        "access_status": result.access_status,
        "used_alternate_source": used_alternate,
        "alternate_sources": alternate_sources,
        "related_tickers": target.related_tickers,
        "related_indexes": target.related_indexes,
        "full_text": full_text,
        "full_text_char_count": len(full_text),
        "retrieved_at_jst": _now_jst(),
        "notes_for_chatgpt": result.note,
    }


def _unreadable_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": item.get("article_id", ""),
        "review_priority": item.get("review_priority", ""),
        "source_group": item.get("source_group", ""),
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "published_at": item.get("published_at", ""),
        "primary_url": item.get("primary_url", ""),
        "access_status": item.get("access_status", "failed"),
        "related_tickers": item.get("related_tickers", []),
        "related_indexes": item.get("related_indexes", []),
        "reason": item.get("notes_for_chatgpt", "title/snippet only; no full text retrieved."),
        "notes_for_chatgpt": item.get("notes_for_chatgpt", "title/snippet only; no full text retrieved."),
    }


def _build_targets(article_review_targets: dict[str, Any], search_supplements: list[dict[str, Any]]) -> list[FullTextTarget]:
    targets: list[FullTextTarget] = []
    seen_urls: set[str] = set()

    for item in article_review_targets.get("must_review", []) if isinstance(article_review_targets, dict) else []:
        _append_target(targets, seen_urls, _target_from_review_item(item, "must", "core_driver"))

    optional_items = article_review_targets.get("optional_review", []) if isinstance(article_review_targets, dict) else []
    for item in optional_items[:3]:
        _append_target(targets, seen_urls, _target_from_review_item(item, "optional", "context_candidate"))

    core_text = " ".join(target.title + " " + target.snippet for target in targets if target.review_priority == "must")
    supplement_count = 0
    for item in search_supplements:
        if supplement_count >= MAX_SUPPLEMENT_TARGETS:
            break
        target = _target_from_search_item(item)
        if not target:
            continue
        if not _has_theme_overlap(target.title + " " + target.snippet, core_text):
            continue
        if _append_target(targets, seen_urls, target):
            supplement_count += 1

    return targets


def _append_target(targets: list[FullTextTarget], seen_urls: set[str], target: FullTextTarget | None) -> bool:
    if not target or not target.primary_url:
        return False
    key = target.primary_url.strip().lower()
    if key in seen_urls:
        return False
    seen_urls.add(key)
    targets.append(target)
    return True


def _target_from_review_item(item: dict[str, Any], priority: str, group: str) -> FullTextTarget | None:
    url = _clean(item.get("url"))
    title = _clean(item.get("title"))
    source = _clean(item.get("source"))
    if not (url and title and source):
        return None
    return FullTextTarget(
        review_priority=priority,
        source_group=group,
        title=title,
        source=source,
        published_at=_clean(item.get("published_at")),
        primary_url=url,
        related_tickers=_clean_list(item.get("related_tickers")),
        related_indexes=_related_indexes_from_text(title + " " + _clean(item.get("snippet"))),
        snippet=_clean(item.get("snippet")),
    )


def _target_from_search_item(item: dict[str, Any]) -> FullTextTarget | None:
    url = _clean(item.get("url"))
    title = _clean(item.get("title"))
    source = _clean(item.get("source"))
    snippet = _clean(item.get("snippet"))
    if not (url and title and source and snippet):
        return None
    return FullTextTarget(
        review_priority="supplement",
        source_group="search_supplement",
        title=title,
        source=source,
        published_at=_clean(item.get("published_at")),
        primary_url=url,
        related_tickers=_clean_list(item.get("related_tickers")),
        related_indexes=_related_indexes_from_text(title + " " + snippet),
        snippet=snippet,
    )


def _target_from_fulltext_item(item: dict[str, Any]) -> FullTextTarget | None:
    url = _clean(item.get("primary_url"))
    title = _clean(item.get("title"))
    source = _clean(item.get("source"))
    if not (url and title and source):
        return None
    return FullTextTarget(
        review_priority=_clean(item.get("review_priority")) or "supplement",
        source_group=_clean(item.get("source_group")) or "search_supplement",
        title=title,
        source=source,
        published_at=_clean(item.get("published_at")),
        primary_url=url,
        related_tickers=_clean_list(item.get("related_tickers")),
        related_indexes=_clean_list(item.get("related_indexes")) or _related_indexes_from_text(title),
        snippet="",
    )


def _fallback_targets_from_search_supplements(items: list[dict[str, Any]]) -> list[FullTextTarget]:
    targets: list[FullTextTarget] = []
    for item in items:
        target = _target_from_search_item(item)
        if target:
            targets.append(target)
    return targets


def _rank_existing_fallback_candidates(target: FullTextTarget, candidates: list[FullTextTarget]) -> list[FullTextTarget]:
    ranked: list[tuple[float, FullTextTarget]] = []
    for candidate in candidates:
        if candidate.primary_url.strip().lower() == target.primary_url.strip().lower():
            continue
        if _rejected_url(candidate.primary_url):
            continue
        score = _fallback_similarity_score(target, candidate)
        if score <= 0:
            continue
        ranked.append((score, candidate))
    ranked.sort(key=lambda row: row[0], reverse=True)
    return [candidate for _, candidate in ranked[:5]]


def _acceptable_fallback_candidate(target: FullTextTarget, candidate: FullTextTarget) -> bool:
    if _rejected_url(candidate.primary_url):
        return False
    if not _quality_url(candidate.primary_url):
        return False
    if not _published_dates_close(target.published_at, candidate.published_at):
        return False
    if target.related_tickers:
        candidate_tickers = set(candidate.related_tickers)
        if candidate_tickers and not set(target.related_tickers).intersection(candidate_tickers):
            return False
    return _fallback_similarity_score(target, candidate) >= 0.46


def _fallback_similarity_score(target: FullTextTarget, candidate: FullTextTarget) -> float:
    title_score = _word_overlap_score(target.title, candidate.title)
    combined_target = f"{target.title} {target.snippet} {' '.join(target.related_tickers)} {' '.join(target.related_indexes)}"
    combined_candidate = f"{candidate.title} {candidate.snippet} {' '.join(candidate.related_tickers)} {' '.join(candidate.related_indexes)}"
    theme_bonus = 0.18 if _has_theme_overlap(combined_target, combined_candidate) else 0.0
    ticker_bonus = 0.12 if set(target.related_tickers).intersection(candidate.related_tickers) else 0.0
    source_bonus = 0.05 if _same_domain_family(target.primary_url, candidate.primary_url) else 0.0
    return title_score + theme_bonus + ticker_bonus + source_bonus


def _fetch_fallback_search_candidates(config: RunConfig, target: FullTextTarget, remaining_budget: int) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    query_count = 0
    if remaining_budget <= 0:
        return {"candidates": [], "targets": [], "query_count": 0}

    serpapi_key = config.env.get("SERPAPI_API_KEY", "").strip()
    tavily_key = config.env.get("TAVILY_API_KEY", "").strip()
    queries = _fallback_queries(target)[: min(MAX_FALLBACK_QUERIES_PER_ARTICLE, remaining_budget)]
    for query in queries:
        if query_count >= remaining_budget:
            break
        fetched: list[dict[str, Any]] = []
        if _is_configured_api_key(serpapi_key, "serpapi"):
            fetched = _fetch_serpapi_fallback(serpapi_key, query)
            query_count += 1
        elif _is_configured_api_key(tavily_key, "tavily"):
            fetched = _fetch_tavily_fallback(tavily_key, query)
            query_count += 1
        if not fetched:
            continue
        candidates.extend(fetched)

    normalized = _dedupe_candidate_items(candidates)
    targets = [target_item for item in normalized if (target_item := _target_from_search_item(item))]
    return {"candidates": normalized, "targets": targets, "query_count": query_count}


def _fetch_serpapi_fallback(api_key: str, query: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": api_key, "num": 4},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for result in payload.get("organic_results", [])[:4]:
        items.append(
            {
                "title": _clean(result.get("title")),
                "source": "SerpAPI",
                "published_at": _clean(result.get("date")),
                "url": _clean(result.get("link")),
                "snippet": _clean(result.get("snippet")),
                "query": query,
                "search_role": "article_fulltext_fallback",
            }
        )
    return [item for item in items if item.get("title") and item.get("url") and item.get("snippet")]


def _fetch_tavily_fallback(api_key: str, query: str) -> list[dict[str, Any]]:
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "search_depth": "basic", "max_results": 4, "include_raw_content": False},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for result in payload.get("results", [])[:4]:
        items.append(
            {
                "title": _clean(result.get("title")),
                "source": "Tavily",
                "published_at": _clean(result.get("published_date")),
                "url": _clean(result.get("url")),
                "snippet": _clean(result.get("content")),
                "query": query,
                "search_role": "article_fulltext_fallback",
            }
        )
    return [item for item in items if item.get("title") and item.get("url") and item.get("snippet")]


def _fallback_queries(target: FullTextTarget) -> list[str]:
    title = target.title.strip()
    if not title:
        return []
    queries: list[str] = []
    lowered = f"{target.source} {target.primary_url}".lower()
    if "reuters" in lowered:
        queries.extend([f"{title} Yahoo Finance", f"{title} Nasdaq"])
    elif "cnbc" in lowered:
        queries.extend([f'"{title}"', f"{title} Yahoo Finance"])
    else:
        queries.extend([f'"{title}"', f"{title} Barchart"])
    return _dedupe_strings(queries)[:MAX_FALLBACK_QUERIES_PER_ARTICLE]


def _candidate_alternates(target: FullTextTarget, all_targets: list[FullTextTarget]) -> list[FullTextTarget]:
    alternates: list[tuple[float, FullTextTarget]] = []
    for candidate in all_targets:
        if candidate.primary_url == target.primary_url:
            continue
        if not _same_content_candidate(target, candidate):
            continue
        alternates.append((_word_overlap_score(target.title, candidate.title), candidate))
    alternates.sort(key=lambda row: row[0], reverse=True)
    return [candidate for _, candidate in alternates[:1]]


def _same_content_candidate(a: FullTextTarget, b: FullTextTarget) -> bool:
    title_score = _word_overlap_score(a.title, b.title)
    if title_score >= 0.72:
        return True
    return title_score >= 0.45 and _has_theme_overlap(a.title + " " + a.snippet, b.title + " " + b.snippet)


def _word_overlap_score(left: str, right: str) -> float:
    left_words = _meaningful_words(left)
    right_words = _meaningful_words(right)
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / max(len(left_words), len(right_words))


def _meaningful_words(value: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "into", "says", "may", "are", "was", "why"}
    return {word for word in re.findall(r"[a-z0-9]{3,}", value.lower()) if word not in stop}


def _has_theme_overlap(left: str, right: str) -> bool:
    left_text = left.lower()
    right_text = right.lower()
    return any(term in left_text and term in right_text for term in THEME_TERMS)


def _quality_url(value: str) -> bool:
    host = urlparse(value).netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in QUALITY_DOMAINS)


def _rejected_url(value: str) -> bool:
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    text = value.lower()
    if not parsed.scheme.startswith("http") or not host:
        return True
    if any(host == domain or host.endswith(f".{domain}") for domain in REJECT_DOMAINS):
        return True
    rejected_markers = (
        "/quote/",
        "finance.yahoo.com/quote",
        "facebook.com",
        "twitter.com",
        "x.com/",
        "linkedin.com/posts",
        "reuters.com/plus",
    )
    return any(marker in text for marker in rejected_markers)


def _same_domain_family(left: str, right: str) -> bool:
    left_host = urlparse(left).netloc.lower().removeprefix("www.")
    right_host = urlparse(right).netloc.lower().removeprefix("www.")
    if not left_host or not right_host:
        return False
    return left_host == right_host or left_host.endswith(right_host) or right_host.endswith(left_host)


def _published_dates_close(left: str, right: str) -> bool:
    left_date = _parse_date(left)
    right_date = _parse_date(right)
    if left_date is None or right_date is None:
        return True
    return abs((left_date - right_date).days) <= 45


def _parse_date(value: Any):
    text = _clean(value)
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
        parsed = parsedate_to_datetime(text)
        return parsed.astimezone(timezone.utc).date() if parsed.tzinfo else parsed.date()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _related_indexes_from_text(value: str) -> list[str]:
    text = value.lower()
    indexes = []
    if "nasdaq" in text:
        indexes.append("NASDAQ")
    if "sox" in text or "semiconductor" in text or "chip" in text:
        indexes.append("SOX")
    return indexes


def _summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    if isinstance(summary, dict):
        return summary
    items = payload.get("items", [])
    unreadable = payload.get("unreadable", [])
    return {
        "target_count": len(items) + len(unreadable),
        "readable_count": sum(1 for item in items if item.get("access_status") == "readable"),
        "alternate_readable_count": sum(1 for item in items if item.get("access_status") == "alternate_readable"),
        "unreadable_count": len(unreadable),
        "total_full_text_chars": sum(int(item.get("full_text_char_count") or 0) for item in items),
        "readable_count_before_fallback": payload.get("summary", {}).get("readable_count_before_fallback", 0) if isinstance(payload.get("summary"), dict) else 0,
        "unreadable_count_before_fallback": payload.get("summary", {}).get("unreadable_count_before_fallback", 0) if isinstance(payload.get("summary"), dict) else len(unreadable),
        "fallback_attempted_count": payload.get("summary", {}).get("fallback_attempted_count", 0) if isinstance(payload.get("summary"), dict) else 0,
        "fallback_success_count": payload.get("summary", {}).get("fallback_success_count", 0) if isinstance(payload.get("summary"), dict) else 0,
        "fallback_failed_count": payload.get("summary", {}).get("fallback_failed_count", 0) if isinstance(payload.get("summary"), dict) else 0,
        "fallback_query_count": payload.get("summary", {}).get("fallback_query_count", 0) if isinstance(payload.get("summary"), dict) else 0,
    }


def _empty_payload(target_date: str) -> dict[str, Any]:
    return {
        "date": target_date,
        "generated_at_jst": _now_jst(),
        "collector": "article_fulltext_collector",
        "summary": {
            "target_count": 0,
            "readable_count": 0,
            "alternate_readable_count": 0,
            "unreadable_count": 0,
            "total_full_text_chars": 0,
            "readable_count_before_fallback": 0,
            "unreadable_count_before_fallback": 0,
            "fallback_attempted_count": 0,
            "fallback_success_count": 0,
            "fallback_failed_count": 0,
            "fallback_query_count": 0,
        },
        "items": [],
        "unreadable": [],
        "fallback_search_supplements": [],
        "fallback_attempts": [],
    }


def _article_id(target: FullTextTarget) -> str:
    digest = hashlib.sha1(target.primary_url.encode("utf-8")).hexdigest()[:10]
    prefix = {"must": "must", "optional": "optional", "supplement": "supplement"}.get(target.review_priority, "article")
    return f"{prefix}-{digest}"


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _dedupe_join(lines: list[str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        normalized = _clean_text(line)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return "\n\n".join(output)


def _append_rescue_note(existing: str, note: str) -> str:
    existing_clean = _clean(existing)
    if not existing_clean:
        return note
    if note in existing_clean:
        return existing_clean
    return f"{existing_clean} {note}"


def _dedupe_candidate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        title = _clean(item.get("title"))
        url = _clean(item.get("url"))
        if not title or not url:
            continue
        key = (title.lower(), url.lower())
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        cleaned = _clean(item)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return output


def _is_configured_api_key(value: str, provider: str) -> bool:
    key = value.strip()
    if not key:
        return False
    lowered = key.lower()
    if lowered in INVALID_KEY_VALUES:
        return False
    return lowered != f"your_{provider}_api_key"


def _clean_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = text.replace("\u00a0", " ")
    return re.sub(r" +", " ", text).strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        cleaned = _clean(item)
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return output


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _now_jst() -> str:
    jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
    return datetime.now(UTC).astimezone(jst).replace(microsecond=0).isoformat()
