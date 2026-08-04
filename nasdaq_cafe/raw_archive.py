from __future__ import annotations

import hashlib
import io
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.collectors.article_fulltext_collector import (
    BLOCKED_TEXT_MARKERS,
    HEADERS,
    MIN_ARTICLE_CHARS,
    PAYWALL_TEXT_MARKERS,
    _detect_blocked_or_paywalled,
    _extract_article_text,
)
from nasdaq_cafe.config import ROOT_DIR, RunConfig

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


MANIFEST_FILE_NAME = "manifest.json"
ARTICLE_INDEX_FILE_NAME = "article_fulltext.json"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ARTICLE_BYTES = 5_000_000
DEFAULT_FULLTEXT_WORKERS = 6
DEFAULT_TAVILY_BASIC_LIMIT = 20
DEFAULT_TAVILY_ADVANCED_LIMIT = 5

URL_KEYS = ("url", "link", "href", "canonical_url")
TITLE_KEYS = ("title", "headline", "name")
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
EXCLUDED_HOSTS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "stocktwits.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
}
INVALID_KEY_VALUES = {"", "todo", "dummy", "test", "none", "null", "your_tavily_api_key"}


def build_raw_archive_manifest(
    config: RunConfig,
    collector_payloads: dict[str, Any],
) -> dict[str, Any]:
    """Register every article-like URL found by collectors before relevance filtering."""
    path = config.raw_dir / MANIFEST_FILE_NAME
    existing = read_json(path)
    manifest = existing if isinstance(existing, dict) else _empty_manifest(config.target_date)
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        documents = []
        manifest["documents"] = documents

    by_canonical = {
        str(document.get("canonical_url") or ""): document
        for document in documents
        if isinstance(document, dict) and document.get("canonical_url")
    }

    for route, payload in collector_payloads.items():
        for record in _walk_article_records(payload):
            _register_record(documents, by_canonical, record, route, config.target_date)

    manifest["date"] = config.target_date
    manifest["generated_at"] = _now_jst()
    manifest["collection_window"] = _collection_window(config.target_date)
    _normalize_discovery_counts(documents)
    _assign_duplicate_groups(documents)
    manifest["summary"] = _manifest_summary(documents)
    write_json(path, manifest)
    return manifest


def load_raw_archive_manifest(config: RunConfig) -> dict[str, Any]:
    payload = read_json(config.raw_dir / MANIFEST_FILE_NAME)
    if isinstance(payload, dict):
        return payload
    return _empty_manifest(config.target_date)


def collect_manifest_fulltext(
    config: RunConfig,
    manifest: dict[str, Any],
    *,
    force: bool | None = None,
    retry_failed: bool = False,
    only_document_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Attempt full-text retrieval for every eligible manifest URL.

    Relevance, review priority, core-driver, low-value and handoff flags are
    deliberately not consulted here. The only count cap is the explicitly
    configured network safeguard NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS.
    """
    documents = manifest.get("documents", [])
    if not isinstance(documents, list):
        documents = []
        manifest["documents"] = documents

    force_fetch = config.refresh if force is None else force
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        document_id = str(document.get("document_id") or "")
        if only_document_ids is not None and document_id not in only_document_ids:
            continue
        if not _eligible_status(document, force_fetch, retry_failed):
            continue
        exclusion_reason = _preflight_exclusion(str(document.get("url") or ""), config.env)
        if exclusion_reason:
            excluded.append(document)
            _mark_excluded(document, exclusion_reason)
        else:
            candidates.append(document)

    configured_limit = _env_nonnegative_int(config.env, "NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS", 0)
    attempt_documents = candidates if configured_limit == 0 else candidates[:configured_limit]
    limit_documents = [] if configured_limit == 0 else candidates[configured_limit:]
    for document in limit_documents:
        _mark_not_attempted_limit(document, configured_limit)

    articles_dir = config.raw_dir / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)
    workers = _env_positive_int(config.env, "NASDAQ_CAFE_FULLTEXT_WORKERS", DEFAULT_FULLTEXT_WORKERS, maximum=16)
    if attempt_documents:
        with ThreadPoolExecutor(max_workers=min(workers, len(attempt_documents))) as executor:
            future_map = {
                executor.submit(_fetch_direct, config, deepcopy(document)): document
                for document in attempt_documents
            }
            for future in as_completed(future_map):
                document = future_map[future]
                try:
                    patch = future.result()
                except Exception as exc:  # Defensive: one URL must never stop the daily run.
                    patch = _unexpected_failure_patch(document, exc)
                _apply_document_patch(document, patch)

    tavily_usage = _rescue_with_tavily(config, attempt_documents)
    for document in documents:
        if isinstance(document, dict):
            _write_document_metadata(config, document)

    _assign_duplicate_groups(documents)
    manifest["generated_at"] = _now_jst()
    manifest["summary"] = _manifest_summary(documents)
    manifest["retrieval_policy"] = {
        "all_manifest_article_urls_attempted": True,
        "selection_fields_ignored": [
            "relevance_score",
            "review_priority",
            "core_driver",
            "low_value",
            "selected_for_handoff",
        ],
        "configured_fulltext_attempt_limit": configured_limit or None,
        "configured_fulltext_workers": workers,
        "tavily_extract_basic_limit": tavily_usage["basic_limit"],
        "tavily_extract_advanced_limit": tavily_usage["advanced_limit"],
    }
    write_json(config.raw_dir / MANIFEST_FILE_NAME, manifest)

    payload = _article_fulltext_payload(config, manifest)
    write_json(config.raw_dir / ARTICLE_INDEX_FILE_NAME, payload)
    summary = payload["summary"]
    status = "empty" if not documents else ("ok" if summary["complete_count"] else "error")
    if summary["complete_count"] and (
        summary["failed_count"] or summary["excluded_count"] or summary["not_attempted_limit_count"]
    ):
        status = "partial"
    return {
        "status": status,
        "cache_used": not bool(attempt_documents or excluded or limit_documents),
        "raw_path": config.raw_dir / ARTICLE_INDEX_FILE_NAME,
        "manifest_path": config.raw_dir / MANIFEST_FILE_NAME,
        "payload": payload,
        "summary": summary,
        "fallback_search_supplements": [],
    }


def article_fulltext_status(result: dict[str, Any], config: RunConfig) -> dict[str, Any]:
    summary = result.get("summary", {}) if isinstance(result, dict) else {}
    return {
        "raw_path": f"output/{config.target_date}/raw/{ARTICLE_INDEX_FILE_NAME}",
        "manifest_path": f"output/{config.target_date}/raw/{MANIFEST_FILE_NAME}",
        "articles_path": f"output/{config.target_date}/raw/articles/",
        "cache_used": result.get("cache_used", False),
        "target_count": summary.get("target_count", 0),
        "attempted_count": summary.get("attempted_count", 0),
        "complete_count": summary.get("complete_count", 0),
        "readable_count": summary.get("complete_count", 0),
        "alternate_readable_count": summary.get("alternate_readable_count", 0),
        "failed_count": summary.get("failed_count", 0),
        "unreadable_count": summary.get("failed_count", 0) + summary.get("excluded_count", 0),
        "excluded_count": summary.get("excluded_count", 0),
        "not_attempted_limit_count": summary.get("not_attempted_limit_count", 0),
        "pending_count": summary.get("pending_count", 0),
        "raw_html_count": summary.get("raw_html_count", 0),
        "extracted_text_count": summary.get("extracted_text_count", 0),
        "total_full_text_chars": summary.get("total_full_text_chars", 0),
        "fallback_attempted_count": summary.get("tavily_attempted_count", 0),
        "fallback_success_count": summary.get("tavily_success_count", 0),
        "fallback_failed_count": summary.get("tavily_failed_count", 0),
        "fallback_query_count": 0,
    }


def register_and_fetch_url(config: RunConfig, url: str, title: str = "") -> dict[str, Any]:
    manifest = build_raw_archive_manifest(
        config,
        {
            "manual": [
                {
                    "title": title.strip() or url,
                    "url": url,
                    "source": "manual",
                    "published_at": "",
                }
            ]
        },
    )
    canonical = _canonicalize_url(url)
    ids = {
        str(document.get("document_id") or "")
        for document in manifest.get("documents", [])
        if isinstance(document, dict) and document.get("canonical_url") == canonical
    }
    return collect_manifest_fulltext(config, manifest, force=True, only_document_ids=ids)


def retry_failed_fulltext(config: RunConfig) -> dict[str, Any]:
    manifest = load_raw_archive_manifest(config)
    return collect_manifest_fulltext(config, manifest, retry_failed=True)


def _walk_article_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        url = _first(value, URL_KEYS)
        title = _first(value, TITLE_KEYS)
        if url and title:
            yield value
            return
        for child in value.values():
            yield from _walk_article_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_article_records(child)


def _register_record(
    documents: list[dict[str, Any]],
    by_canonical: dict[str, dict[str, Any]],
    record: dict[str, Any],
    route: str,
    target_date: str,
) -> None:
    url = str(_first(record, URL_KEYS) or "").strip()
    if not url:
        return
    canonical = _canonicalize_url(url)
    document = by_canonical.get(canonical)
    if document is not None:
        routes = document.get("retrieval_routes", []) if isinstance(document.get("retrieval_routes"), list) else []
        discovered_urls = document.get("discovered_urls", []) if isinstance(document.get("discovered_urls"), list) else []
        is_new_discovery = route not in routes or url not in discovered_urls
        _append_unique(document, "retrieval_routes", route)
        _append_unique(document, "discovered_urls", url)
        if is_new_discovery:
            document["discovery_count"] = int(document.get("discovery_count") or 1) + 1
        return

    title = str(_first(record, TITLE_KEYS) or "").strip()
    publisher = str(
        _first(record, ("publisher", "sourceCommonName", "source", "provider", "domain", "site"))
        or _domain(url)
    ).strip()
    document_id = "article_" + hashlib.sha1(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]
    document = {
        "document_id": document_id,
        "source_type": _source_type(url),
        "title": title,
        "publisher": publisher,
        "published_at": str(
            _first(record, ("published_at", "published", "pubDate", "date", "datetime", "seendate")) or ""
        ).strip(),
        "retrieved_at": None,
        "language": str(_first(record, ("language", "lang")) or "").strip(),
        "region": str(_first(record, ("region", "source_country", "sourcecountry", "country")) or "").strip(),
        "url": url,
        "canonical_url": canonical,
        "discovered_urls": [url],
        "discovery_count": 1,
        "raw_file": None,
        "extracted_text_file": None,
        "metadata_file": f"output/{target_date}/raw/articles/{document_id}.json",
        "tavily_response_file": None,
        "fulltext_status": "pending",
        "access_status": "not_attempted",
        "content_hash": None,
        "duplicate_group_id": None,
        "duplicate_of": None,
        "syndicated_copy": False,
        "retrieval_routes": [route],
        "char_count": 0,
        "http_status": None,
        "content_type": None,
        "failure_reason": None,
        "attempts": [],
    }
    # Fill the date lazily so callers can construct records without knowing the path.
    documents.append(document)
    by_canonical[canonical] = document


def _fetch_direct(config: RunConfig, document: dict[str, Any]) -> dict[str, Any]:
    url = str(document.get("url") or "")
    document_id = str(document.get("document_id") or "")
    attempted_at = _now_jst()
    timeout = _env_positive_int(
        config.env,
        "NASDAQ_CAFE_REQUEST_TIMEOUT_SECONDS",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        maximum=60,
    )
    max_bytes = _env_positive_int(
        config.env,
        "NASDAQ_CAFE_MAX_ARTICLE_BYTES",
        DEFAULT_MAX_ARTICLE_BYTES,
        maximum=50_000_000,
    )
    base_patch = {
        "retrieved_at": attempted_at,
        "retrieval_route": "requests",
        "attempt": {"route": "requests", "attempted_at": attempted_at, "url": url},
    }
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=(5, timeout),
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException as exc:
        return {
            **base_patch,
            "fulltext_status": "failed",
            "access_status": "failed",
            "failure_reason": f"{type(exc).__name__}: {exc}",
        }

    try:
        content, exceeded = _read_response_bytes(response, max_bytes)
        content_type = str(response.headers.get("content-type", ""))
        read_url = str(getattr(response, "url", "") or url)
        raw_suffix = ".pdf" if _is_pdf(content_type, read_url) else ".html"
        raw_path = config.raw_dir / "articles" / f"{document_id}{raw_suffix}"
        if content:
            raw_path.write_bytes(content)
        raw_display = _display_path(raw_path) if content else None
        common = {
            **base_patch,
            "read_url": read_url,
            "raw_file": raw_display,
            "http_status": int(response.status_code),
            "content_type": content_tãu¶‰žËkºwµçI•…‘…‰±”ˆ(€€€€¤(€€€Õ¹É•Í½±Ù•‘}…™Ñ•É}™…±±‰…¬€ôÍÕ´ (€€€€€€€€Ä™½È‘½Õµ•¹Ð¥¸…ÑÑ•µÁÑ•¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰™…¥±•ˆ(€€€€¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰Ñ…É•Ñ}½Õ¹Ðˆè±•¸¡Ù…±¥‘}‘½Õµ•¹ÑÌ¤°(€€€€€€€€‰…ÑÑ•µÁÑ•‘}½Õ¹Ðˆè±•¸¡…ÑÑ•µÁÑ•¤°(€€€€€€€€‰½µÁ±•Ñ•}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰½µÁ±•Ñ”ˆ¤°(€€€€€€€€‰É•…‘…‰±•}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰É•…‘…‰±”ˆ¤°(€€€€€€€€‰…±Ñ•É¹…Ñ•}É•…‘…‰±•}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰…±Ñ•É¹…Ñ•}É•…‘…‰±”ˆ¤°(€€€€€€€€‰™…¥±•‘}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰™…¥±•ˆ¤°(€€€€€€€€‰•á±Õ‘•‘}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰•á±Õ‘•ˆ¤°(€€€€€€€€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ñ}½Õ¹ÐˆèÍÕ´ (€€€€€€€€€€€€Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ(€€€€€€€€¤°(€€€€€€€€‰Á•¹‘¥¹}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰Á•¹‘¥¹œˆ¤°(€€€€€€€€‰É…Ý}¡Ñµ±}½Õ¹ÐˆèÍÕ´ (€€€€€€€€€€€€Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜ÍÑÈ¡‘½Õµ•¹Ð¹•Ð ‰É…Ý}™¥±”ˆ¤½È€ˆˆ¤¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ  ˆ¹¡Ñµ°ˆ¤(€€€€€€€€¤°(€€€€€€€€‰•áÑÉ…Ñ•‘}Ñ•áÑ}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰•áÑÉ…Ñ•‘}Ñ•áÑ}™¥±”ˆ¤¤°(€€€€€€€€‰Ñ½Ñ…±}™Õ±±}Ñ•áÑ}¡…ÉÌˆèÍÕ´¡¥¹Ð¡¥Ñ•´¹•Ð ‰™Õ±±}Ñ•áÑ}¡…É}½Õ¹Ðˆ¤½È€À¤™½È¥Ñ•´¥¸¥Ñ•µÌ¤°(€€€€€€€€‰Ñ…Ù¥±å}…ÑÑ•µÁÑ•‘}½Õ¹Ðˆè±•¸¡Ñ…Ù¥±å}…ÑÑ•µÁÑÌ¤°(€€€€€€€€‰Ñ…Ù¥±å}ÍÕ•ÍÍ}½Õ¹ÐˆèÑ…Ù¥±å}ÍÕ•ÍÍ}½Õ¹Ð°(€€€€€€€€‰Ñ…Ù¥±å}™…¥±•‘}½Õ¹ÐˆèÕ¹É•Í½±Ù•‘}…™Ñ•É}™…±±‰…¬°(€€€€€€€€Œ½µÁ…Ñ¥‰¥±¥Ñä™¥•±‘ÌÕÍ•‰ä½±‘•È½ÕÑÁÕÐÉ•¹‘•É•ÉÌ¸(€€€€€€€€‰É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆèÍÕ´ (€€€€€€€€€€€€Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰É•…‘…‰±”ˆ(€€€€€€€€¤°(€€€€€€€€‰Õ¹É•…‘…‰±•}½Õ¹Ñ}‰•™½É•}™…±±‰…¬ˆè±•¸¡…ÑÑ•µÁÑ•¤(€€€€€€€€´ÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥‘}‘½Õµ•¹ÑÌ¥˜‘½Õµ•¹Ð¹•Ð ‰…•ÍÍ}ÍÑ…ÑÕÌˆ¤€ôô€‰É•…‘…‰±”ˆ¤°(€€€€€€€€‰™…±±‰…­}…ÑÑ•µÁÑ•‘}½Õ¹Ðˆè±•¸¡Ñ…Ù¥±å}…ÑÑ•µÁÑÌ¤°(€€€€€€€€‰™…±±‰…­}ÍÕ•ÍÍ}½Õ¹ÐˆèÑ…Ù¥±å}ÍÕ•ÍÍ}½Õ¹Ð°(€€€€€€€€‰™…±±‰…­}™…¥±•‘}½Õ¹ÐˆèÕ¹É•Í½±Ù•‘}…™Ñ•É}™…±±‰…¬°(€€€€€€€€‰™…±±‰…­}ÅÕ•Éå}½Õ¹Ðˆè€À°(€€€ô(()‘•˜}•±¥¥‰±•}ÍÑ…ÑÕÌ¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°™½É”è‰½½°°É•ÑÉå}™…¥±•è‰½½°¤€´ø‰½½°è(€€€ÍÑ…ÑÕÌ€ôÍÑÈ¡‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤½È€‰Á•¹‘¥¹œˆ¤(€€€¥˜™½É”è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€¥˜É•ÑÉå}™…¥±•è(€€€€€€€É•ÑÕÉ¸ÍÑ…ÑÕÌ¥¸ì‰™…¥±•ˆ°€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ°€‰Á•¹‘¥¹œ‰ô(€€€É•ÑÕÉ¸ÍÑ…ÑÕÌ€ôô€‰Á•¹‘¥¹œˆ(()‘•˜}ÁÉ•™±¥¡Ñ}•á±ÕÍ¥½¸¡ÕÉ°èÍÑÈ°•¹Øè‘¥ÑmÍÑÈ°ÍÑÉt¤€´øÍÑÈè(€€€Á…ÉÍ•€ôÕÉ±Á…ÉÍ”¡ÕÉ°¤(€€€¥˜Á…ÉÍ•¹Í¡•µ”¹½Ð¥¸ì‰¡ÑÑÀˆ°€‰¡ÑÑÁÌ‰ô½È¹½ÐÁ…ÉÍ•¹¹•Ñ±½Œè(€€€€€€€É•ÑÕÉ¸€‰¥¹Ù…±¥‘}ÕÉ°ˆ(€€€¡½ÍÐ€ôÁ…ÉÍ•¹¹•Ñ±½Œ¹±½Ý•È ¤¹ÍÁ±¥Ð ˆèˆ°€Ä¥lÁt¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€¥˜…¹ä¡¡½ÍÐ€ôô¥Ñ•´½È¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡˜ˆ¹í¥Ñ•µôˆ¤™½È¥Ñ•´¥¸a1U}!=MQL¤è(€€€€€€€É•ÑÕÉ¸€‰Í½¥…±}½É}Ù¥‘•½}ÕÉ°ˆ(€€€±½Ý•É•€ôÕÉ°¹±½Ý•È ¤(€€€¥˜€ˆ½ÅÕ½Ñ”¼ˆ¥¸±½Ý•É•½È€‰™¥¹…¹”¹å…¡½¼¹½´½ÅÕ½Ñ”ˆ¥¸±½Ý•É•è(€€€€€€€É•ÑÕÉ¸€‰ÅÕ½Ñ•}Á…•}¹½Ñ}…ÉÑ¥±”ˆ(€€€‘¥Í…±±½Ý•€ôì(€€€€€€€¥Ñ•´¹ÍÑÉ¥À ¤¹±½Ý•È ¤¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(€€€€€€€™½È¥Ñ•´¥¸ÍÑÈ¡•¹Ø¹•Ð ‰9ME}}U11QaQ}%M11=]}=5%9Lˆ¤½È€ˆˆ¤¹ÍÁ±¥Ð ˆ°ˆ¤(€€€€€€€¥˜¥Ñ•´¹ÍÑÉ¥À ¤(€€€ô(€€€¥˜…¹ä¡¡½ÍÐ€ôô¥Ñ•´½È¡½ÍÐ¹•¹‘ÍÝ¥Ñ ¡˜ˆ¹í¥Ñ•µôˆ¤™½È¥Ñ•´¥¸‘¥Í…±±½Ý•¤è(€€€€€€€É•ÑÕÉ¸€‰½¹™¥ÕÉ•‘}‘¥Í…±±½Ý•‘}‘½µ…¥¸ˆ(€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}µ…É­}•á±Õ‘•¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°É•…Í½¸èÍÑÈ¤€´ø9½¹”è(€€€‘½Õµ•¹Ð¹ÕÁ‘…Ñ” (€€€€€€€ì(€€€€€€€€€€€€‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆè€‰•á±Õ‘•ˆ°(€€€€€€€€€€€€‰…•ÍÍ}ÍÑ…ÑÕÌˆè€‰¹½Ñ}…ÑÑ•µÁÑ•ˆ°(€€€€€€€€€€€€‰™…¥±ÕÉ•}É•…Í½¸ˆèÉ•…Í½¸°(€€€€€€€€€€€€‰É•ÑÉ¥•Ù•‘}…Ðˆè}¹½Ý}©ÍÐ ¤°(€€€€€€€ô(€€€€¤(€€€}…ÁÁ•¹‘}…ÑÑ•µÁÐ¡‘½Õµ•¹Ð°ì‰É½ÕÑ”ˆè€‰ÁÉ•™±¥¡Ðˆ°€‰ÍÑ…ÑÕÌˆè€‰•á±Õ‘•ˆ°€‰É•…Í½¸ˆèÉ•…Í½¹ô¤(()‘•˜}µ…É­}¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ð¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°±¥µ¥Ðè¥¹Ð¤€´ø9½¹”è(€€€¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰½µÁ±•Ñ”ˆè(€€€€€€€}…ÁÁ•¹‘}…ÑÑ•µÁÐ (€€€€€€€€€€€‘½Õµ•¹Ð°(€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰É½ÕÑ”ˆè€‰É•ÅÕ•ÍÑÌˆ°(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ°(€€€€€€€€€€€€€€€€‰É•…Í½¸ˆè€‰½¹™¥ÕÉ•‘}™Õ±±Ñ•áÑ}…ÑÑ•µÁÑ}±¥µ¥ÐìÁÉ•Ù¥½ÕÍ±ä½µÁ±•Ñ•…¡”É•Ñ…¥¹•ˆ°(€€€€€€€€€€€ô°(€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸(€€€‘½Õµ•¹Ð¹ÕÁ‘…Ñ” (€€€€€€€ì(€€€€€€€€€€€€‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆè€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ°(€€€€€€€€€€€€‰…•ÍÍ}ÍÑ…ÑÕÌˆè€‰¹½Ñ}…ÑÑ•µÁÑ•ˆ°(€€€€€€€€€€€€‰™…¥±ÕÉ•}É•…Í½¸ˆè˜‰½¹™¥ÕÉ•™Õ±°µÑ•áÐ…ÑÑ•µÁÐ±¥µ¥ÐÉ•…¡•€¡í±¥µ¥Ñô¤ìUI0É•Ñ…¥¹•™½ÈÉ•ÑÉäˆ°(€€€€€€€€€€€€‰É•ÑÉ¥•Ù•‘}…Ðˆè9½¹”°(€€€€€€€ô(€€€€¤(€€€}…ÁÁ•¹‘}…ÑÑ•µÁÐ (€€€€€€€‘½Õµ•¹Ð°(€€€€€€€ì(€€€€€€€€€€€€‰É½ÕÑ”ˆè€‰É•ÅÕ•ÍÑÌˆ°(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ°(€€€€€€€€€€€€‰É•…Í½¸ˆè€‰½¹™¥ÕÉ•‘}™Õ±±Ñ•áÑ}…ÑÑ•µÁÑ}±¥µ¥Ðˆ°(€€€€€€€ô°(€€€€¤(()‘•˜}Õ¹•áÁ•Ñ•‘}™…¥±ÕÉ•}Á…Ñ ¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°•áŒèá•ÁÑ¥½¸¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€…ÑÑ•µÁÑ•‘}…Ð€ô}¹½Ý}©ÍÐ ¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰É•ÑÉ¥•Ù•‘}…Ðˆè…ÑÑ•µÁÑ•‘}…Ð°(€€€€€€€€‰É•ÑÉ¥•Ù…±}É½ÕÑ”ˆè€‰É•ÅÕ•ÍÑÌˆ°(€€€€€€€€‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ°(€€€€€€€€‰…•ÍÍ}ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ°(€€€€€€€€‰™…¥±ÕÉ•}É•…Í½¸ˆè˜‰¥¹Ñ•É¹…°½±±•Ñ½È•ÉÉ½ÈèíÑåÁ”¡•áŒ¤¹}}¹…µ•}}ôèí•áôˆ°(€€€€€€€€‰…ÑÑ•µÁÐˆèì(€€€€€€€€€€€€‰É½ÕÑ”ˆè€‰É•ÅÕ•ÍÑÌˆ°(€€€€€€€€€€€€‰…ÑÑ•µÁÑ•‘}…Ðˆè…ÑÑ•µÁÑ•‘}…Ð°(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰™…¥±•ˆ°(€€€€€€€€€€€€‰É•…Í½¸ˆè˜‰¥¹Ñ•É¹…°½±±•Ñ½È•ÉÉ½ÈèíÑåÁ”¡•áŒ¤¹}}¹…µ•}}ôèí•áôˆ°(€€€€€€€ô°(€€€ô(()‘•˜}…ÁÁ±å}‘½Õµ•¹Ñ}Á…Ñ ¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°Á…Ñ è‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è(€€€…ÑÑ•µÁÐ€ôÁ…Ñ ¹Á½À ‰…ÑÑ•µÁÐˆ°9½¹”¤(€€€É½ÕÑ”€ôÁ…Ñ ¹Á½À ‰É•ÑÉ¥•Ù…±}É½ÕÑ”ˆ°9½¹”¤(€€€‘½Õµ•¹Ð¹ÕÁ‘…Ñ”¡Á…Ñ ¤(€€€¥˜É½ÕÑ”è(€€€€€€€}…ÁÁ•¹‘}Õ¹¥ÅÕ”¡‘½Õµ•¹Ð°€‰É•ÑÉ¥•Ù…±}É½ÕÑ•Ìˆ°ÍÑÈ¡É½ÕÑ”¤¤(€€€¥˜¥Í¥¹ÍÑ…¹”¡…ÑÑ•µÁÐ°‘¥Ð¤è(€€€€€€€…ÑÑ•µÁÐ¹Í•Ñ‘•™…Õ±Ð ‰ÍÑ…ÑÕÌˆ°‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤¤(€€€€€€€…ÑÑ•µÁÐ¹Í•Ñ‘•™…Õ±Ð ‰É•…Í½¸ˆ°‘½Õµ•¹Ð¹•Ð ‰™…¥±ÕÉ•}É•…Í½¸ˆ¤¤(€€€€€€€}…ÁÁ•¹‘}…ÑÑ•µÁÐ¡‘½Õµ•¹Ð°…ÑÑ•µÁÐ¤(()‘•˜}ÝÉ¥Ñ•}‘½Õµ•¹Ñ}µ•Ñ…‘…Ñ„¡½¹™¥œèIÕ¹½¹™¥œ°‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è(€€€‘½Õµ•¹Ñ}¥€ôÍÑÈ¡‘½Õµ•¹Ð¹•Ð ‰‘½Õµ•¹Ñ}¥ˆ¤½È€ˆˆ¤(€€€¥˜¹½Ð‘½Õµ•¹Ñ}¥è(€€€€€€€É•ÑÕÉ¸(€€€Á…Ñ €ô½¹™¥œ¹É…Ý}‘¥È€¼€‰…ÉÑ¥±•Ìˆ€¼˜‰í‘½Õµ•¹Ñ}¥‘ô¹©Í½¸ˆ(€€€‘½Õµ•¹Ñl‰µ•Ñ…‘…Ñ…}™¥±”‰t€ô}‘¥ÍÁ±…å}Á…Ñ ¡Á…Ñ ¤(€€€ÝÉ¥Ñ•}©Í½¸¡Á…Ñ °‘½Õµ•¹Ð¤(()‘•˜}É•…‘}É•ÍÁ½¹Í•}‰åÑ•Ì¡É•ÍÁ½¹Í”è¹ä°µ…á}‰åÑ•Ìè¥¹Ð¤€´øÑÕÁ±•m‰åÑ•Ì°‰½½±tè(€€€¡Õ¹­Ìè±¥ÍÑm‰åÑ•Ít€ômt(€€€Ñ½Ñ…°€ô€À(€€€¥˜¡…Í…ÑÑÈ¡É•ÍÁ½¹Í”°€‰¥Ñ•É}½¹Ñ•¹Ðˆ¤è(€€€€€€€¥Ñ•É…Ñ½È€ôÉ•ÍÁ½¹Í”¹¥Ñ•É}½¹Ñ•¹Ð¡¡Õ¹­}Í¥é”ôØÕ|ÔÌØ¤(€€€•±Í”è€€ŒM¥µÁ±”É•ÍÁ½¹Í”‘½Õ‰±•ÌÕÍ•¥¸Ñ•ÍÑÌ¸(€€€€€€€¥Ñ•É…Ñ½È€ôm•Ñ…ÑÑÈ¡É•ÍÁ½¹Í”°€‰½¹Ñ•¹Ðˆ°ˆˆˆ¥t(€€€™½È¡Õ¹¬¥¸¥Ñ•É…Ñ½Èè(€€€€€€€¥˜¹½Ð¡Õ¹¬è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Ñ½Ñ…°€¬ô±•¸¡¡Õ¹¬¤(€€€€€€€¥˜Ñ½Ñ…°€øµ…á}‰åÑ•Ìè(€€€€€€€€€€€É•µ…¥¹¥¹œ€ôµ…à À°µ…á}‰åÑ•Ì€´ÍÕ´¡±•¸¡¥Ñ•´¤™½È¥Ñ•´¥¸¡Õ¹­Ì¤¤(€€€€€€€€€€€¥˜É•µ…¥¹¥¹œè(€€€€€€€€€€€€€€€¡Õ¹­Ì¹…ÁÁ•¹¡¡Õ¹­léÉ•µ…¥¹¥¹t¤(€€€€€€€€€€€É•ÑÕÉ¸ˆˆˆ¹©½¥¸¡¡Õ¹­Ì¤°QÉÕ”(€€€€€€€¡Õ¹­Ì¹…ÁÁ•¹¡¡Õ¹¬¤(€€€É•ÑÕÉ¸ˆˆˆ¹©½¥¸¡¡Õ¹­Ì¤°…±Í”(()‘•˜}‘•½‘•}É•ÍÁ½¹Í”¡½¹Ñ•¹Ðè‰åÑ•Ì°½¹Ñ•¹Ñ}ÑåÁ”èÍÑÈ¤€´øÍÑÈè(€€€µ…Ñ €ôÉ”¹Í•…É ¡È‰¡…ÉÍ•Ðô¡mxíqÍt¬¤ˆ°½¹Ñ•¹Ñ}ÑåÁ”°™±…ÌõÉ”¹%9=IM¤(€€€•¹½‘¥¹Ì€ômµ…Ñ ¹É½ÕÀ Ä¤¹ÍÑÉ¥À œ‰pœœ¥t¥˜µ…Ñ •±Í”mt(€€€•¹½‘¥¹Ì¹•áÑ•¹¡l‰ÕÑ˜´àˆ°€‰ÀÄÈÔÈ‰t¤(€€€™½È•¹½‘¥¹œ¥¸•¹½‘¥¹Ìè(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸½¹Ñ•¹Ð¹‘•½‘”¡•¹½‘¥¹œ¤(€€€€€€€•á•ÁÐ€¡1½½­ÕÁÉÉ½È°U¹¥½‘••½‘•ÉÉ½È¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€É•ÑÕÉ¸½¹Ñ•¹Ð¹‘•½‘” ‰ÕÑ˜´àˆ°•ÉÉ½ÉÌô‰É•Á±…”ˆ¤(()‘•˜}•áÑÉ…Ñ}Á‘™}Ñ•áÐ¡½¹Ñ•¹Ðè‰åÑ•Ì¤€´øÑÕÁ±•mÍÑÈ°ÍÑÉtè(€€€ÑÉäè(€€€€€€€™É½´ÁåÁ‘˜¥µÁ½ÉÐA‘™I•…‘•È€€ŒÑåÁ”è¥¹½É”(€€€•á•ÁÐ%µÁ½ÉÑÉÉ½Èè(€€€€€€€É•ÑÕÉ¸€ˆˆ°€‰AÝ…ÌÍ…Ù•°‰ÕÐÁåÁ‘˜¥Ì¹½Ð¥¹ÍÑ…±±•™½ÈÑ•áÐ•áÑÉ…Ñ¥½¸ˆ(€€€ÑÉäè(€€€€€€€É•…‘•È€ôA‘™I•…‘•È¡¥¼¹	åÑ•Í%<¡½¹Ñ•¹Ð¤¤(€€€€€€€Á…•Ì€ôl¡Á…”¹•áÑÉ…Ñ}Ñ•áÐ ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤™½ÈÁ…”¥¸É•…‘•È¹Á…•Ít(€€€€€€€Ñ•áÐ€ô€‰q¹q¸ˆ¹©½¥¸¡Á…”™½ÈÁ…”¥¸Á…•Ì¥˜Á…”¤(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É•ÑÕÉ¸€ˆˆ°˜‰A•áÑÉ…Ñ¥½¸™…¥±•èíÑåÁ”¡•áŒ¤¹}}¹…µ•}}ôèí•áôˆ(€€€¥˜±•¸¡Ñ•áÐ¤€ð5%9}IQ%1}!ILè(€€€€€€€É•ÑÕÉ¸€ˆˆ°˜‰AÑ•áÐÝ…ÌÑ½¼Í¡½ÉÐ€¡í±•¸¡Ñ•áÐ¥ô¡…ÉÌ¤ˆ(€€€É•ÑÕÉ¸Ñ•áÐ°€ˆˆ(()‘•˜}Á±…¥¹}Ñ•áÑ}‰±½­}ÍÑ…ÑÕÌ¡Ñ•áÐèÍÑÈ¤€´øÍÑÈè(€€€±½Ý•É•€ôÑ•áÐ¹±½Ý•È ¤(€€€¥˜…¹ä¡µ…É­•È¥¸±½Ý•É•™½Èµ…É­•È¥¸Ae]11}QaQ}5I-IL¤è(€€€€€€€É•ÑÕÉ¸€‰Á…åÝ…±°µ…É­•È‘•Ñ•Ñ•¥¸Q…Ù¥±äÉ•ÍÁ½¹Í”ˆ(€€€¥˜…¹ä¡µ…É­•È¥¸±½Ý•É•™½Èµ…É­•È¥¸	1=-}QaQ}5I-IL¤è(€€€€€€€É•ÑÕÉ¸€‰‰±½¬½±½¥¸½AQ!µ…É­•È‘•Ñ•Ñ•¥¸Q…Ù¥±äÉ•ÍÁ½¹Í”ˆ(€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}¥Í}Á‘˜¡½¹Ñ•¹Ñ}ÑåÁ”èÍÑÈ°ÕÉ°èÍÑÈ¤€´ø‰½½°è(€€€É•ÑÕÉ¸€‰…ÁÁ±¥…Ñ¥½¸½Á‘˜ˆ¥¸½¹Ñ•¹Ñ}ÑåÁ”¹±½Ý•È ¤½ÈÕÉ±Á…ÉÍ”¡ÕÉ°¤¹Á…Ñ ¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ  ˆ¹Á‘˜ˆ¤(()‘•˜}…¹½¹¥…±¥é•}ÕÉ°¡Ù…±Õ”èÍÑÈ¤€´øÍÑÈè(€€€Ñ•áÐ€ôÙ…±Õ”¹ÍÑÉ¥À ¤(€€€Á…ÉÍ•€ôÕÉ±Á…ÉÍ”¡Ñ•áÐ¤(€€€¥˜¹½ÐÁ…ÉÍ•¹Í¡•µ”½È¹½ÐÁ…ÉÍ•¹¹•Ñ±½Œè(€€€€€€€É•ÑÕÉ¸Ñ•áÐ(€€€Í¡•µ”€ôÁ…ÉÍ•¹Í¡•µ”¹±½Ý•È ¤(€€€¡½ÍÐ€ôÁ…ÉÍ•¹¹•Ñ±½Œ¹±½Ý•È ¤(€€€¥˜Í¡•µ”€ôô€‰¡ÑÑÀˆ…¹¡½ÍÐ¹•¹‘ÍÝ¥Ñ  ˆèàÀˆ¤è(€€€€€€€¡½ÍÐ€ô¡½ÍÑlè´Ít(€€€¥˜Í¡•µ”€ôô€‰¡ÑÑÁÌˆ…¹¡½ÍÐ¹•¹‘ÍÝ¥Ñ  ˆèÐÐÌˆ¤è(€€€€€€€¡½ÍÐ€ô¡½ÍÑlè´Ñt(€€€ÅÕ•Éä€ôl(€€€€€€€€¡­•ä°Ù…°¤(€€€€€€€™½È­•ä°Ù…°¥¸Á…ÉÍ•}ÅÍ°¡Á…ÉÍ•¹ÅÕ•Éä°­••Á}‰±…¹­}Ù…±Õ•ÌõQÉÕ”¤(€€€€€€€¥˜¹½Ð­•ä¹±½Ý•È ¤¹ÍÑ…ÉÑÍÝ¥Ñ  ‰ÕÑµ|ˆ¤…¹­•ä¹±½Ý•È ¤¹½Ð¥¸QI-%9}EUIe}-eL(€€€t(€€€Á…Ñ €ôÁ…ÉÍ•¹Á…Ñ ½È€ˆ¼ˆ(€€€¥˜Á…Ñ €„ô€ˆ¼ˆè(€€€€€€€Á…Ñ €ôÁ…Ñ ¹ÉÍÑÉ¥À ˆ¼ˆ¤(€€€É•ÑÕÉ¸ÕÉ±Õ¹Á…ÉÍ” ¡Í¡•µ”°¡½ÍÐ°Á…Ñ °€ˆˆ°ÕÉ±•¹½‘”¡ÅÕ•Éä°‘½Í•ÄõQÉÕ”¤°€ˆˆ¤¤(()‘•˜}…ÍÍ¥¹}‘ÕÁ±¥…Ñ•}É½ÕÁÌ¡‘½Õµ•¹ÑÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø9½¹”è(€€€‰å}¡…Í è‘¥ÑmÍÑÈ°±¥ÍÑm‘¥ÑmÍÑÈ°¹åuut€ôíô(€€€™½È‘½Õµ•¹Ð¥¸‘½Õµ•¹ÑÌè(€€€€€€€‘½Õµ•¹Ñl‰‘ÕÁ±¥…Ñ•}É½ÕÁ}¥‰t€ô9½¹”(€€€€€€€‘½Õµ•¹Ñl‰‘ÕÁ±¥…Ñ•}½˜‰t€ô9½¹”(€€€€€€€‘½Õµ•¹Ñl‰Íå¹‘¥…Ñ•‘}½Áä‰t€ô…±Í”(€€€€€€€½¹Ñ•¹Ñ}¡…Í €ôÍÑÈ¡‘½Õµ•¹Ð¹•Ð ‰½¹Ñ•¹Ñ}¡…Í ˆ¤½È€ˆˆ¤(€€€€€€€¥˜½¹Ñ•¹Ñ}¡…Í è(€€€€€€€€€€€‰å}¡…Í ¹Í•Ñ‘•™…Õ±Ð¡½¹Ñ•¹Ñ}¡…Í °mt¤¹…ÁÁ•¹¡‘½Õµ•¹Ð¤(€€€™½È½¹Ñ•¹Ñ}¡…Í °µ•µ‰•ÉÌ¥¸‰å}¡…Í ¹¥Ñ•µÌ ¤è(€€€€€€€¥˜±•¸¡µ•µ‰•ÉÌ¤€ð€Èè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€É½ÕÁ}¥€ô€‰‘ÕÁ|ˆ€¬½¹Ñ•¹Ñ}¡…Í¡lèÄÉt(€€€€€€€É•ÁÉ•Í•¹Ñ…Ñ¥Ù•}¥€ôÍÑÈ¡µ•µ‰•ÉÍlÁt¹•Ð ‰‘½Õµ•¹Ñ}¥ˆ¤½È€ˆˆ¤(€€€€€€€™½È¥¹‘•à°µ•µ‰•È¥¸•¹Õµ•É…Ñ”¡µ•µ‰•ÉÌ¤è(€€€€€€€€€€€µ•µ‰•Él‰‘ÕÁ±¥…Ñ•}É½ÕÁ}¥‰t€ôÉ½ÕÁ}¥(€€€€€€€€€€€µ•µ‰•Él‰‘ÕÁ±¥…Ñ•}½˜‰t€ô9½¹”¥˜¥¹‘•à€ôô€À•±Í”É•ÁÉ•Í•¹Ñ…Ñ¥Ù•}¥(€€€€€€€€€€€µ•µ‰•Él‰Íå¹‘¥…Ñ•‘}½Áä‰t€ô¥¹‘•à€ø€À(()‘•˜}µ…¹¥™•ÍÑ}ÍÕµµ…Éä¡‘½Õµ•¹ÑÌè±¥ÍÑm¹åt¤€´ø‘¥ÑmÍÑÈ°¥¹Ñtè(€€€Ù…±¥€ôm‘½Õµ•¹Ð™½È‘½Õµ•¹Ð¥¸‘½Õµ•¹ÑÌ¥˜¥Í¥¹ÍÑ…¹”¡‘½Õµ•¹Ð°‘¥Ð¥t(€€€É•ÑÕÉ¸ì(€€€€€€€€‰‘½Õµ•¹Ñ}½Õ¹Ðˆè±•¸¡Ù…±¥¤°(€€€€€€€€‰½µÁ±•Ñ•}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰½µÁ±•Ñ”ˆ¤°(€€€€€€€€‰™…¥±•‘}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰™…¥±•ˆ¤°(€€€€€€€€‰•á±Õ‘•‘}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰•á±Õ‘•ˆ¤°(€€€€€€€€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ñ}½Õ¹ÐˆèÍÕ´ (€€€€€€€€€€€€Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰¹½Ñ}…ÑÑ•µÁÑ•‘}±¥µ¥Ðˆ(€€€€€€€€¤°(€€€€€€€€‰Á•¹‘¥¹}½Õ¹ÐˆèÍÕ´ Ä™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰™Õ±±Ñ•áÑ}ÍÑ…ÑÕÌˆ¤€ôô€‰Á•¹‘¥¹œˆ¤°(€€€€€€€€‰‘ÕÁ±¥…Ñ•}É½ÕÁ}½Õ¹Ðˆè±•¸ (€€€€€€€€€€€í‘½Õµ•¹Ð¹•Ð ‰‘ÕÁ±¥…Ñ•}É½ÕÁ}¥ˆ¤™½È‘½Õµ•¹Ð¥¸Ù…±¥¥˜‘½Õµ•¹Ð¹•Ð ‰‘ÕÁ±¥…Ñ•}É½ÕÁ}¥ˆ¥ô(€€€€€€€€¤°(€€€ô(()‘•˜}¹½Éµ…±¥é•}‘¥Í½Ù•Éå}½Õ¹ÑÌ¡‘½Õµ•¹ÑÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø9½¹”è(€€€™•Ñ¡}É½ÕÑ•Ì€ôì‰É•ÅÕ•ÍÑÌˆ°€‰Ñ…Ù¥±å}•áÑÉ…Ñ}‰…Í¥Œˆ°€‰Ñ…Ù¥±å}•áÑÉ…Ñ}…‘Ù…¹•‰ô(€€€™½È‘½Õµ•¹Ð¥¸‘½Õµ•¹ÑÌè(€€€€€€€É½ÕÑ•Ì€ôì(€€€€€€€€€€€ÍÑÈ¡É½ÕÑ”¤(€€€€€€€€€€€™½ÈÉ½ÕÑ”¥¸‘½Õµ•¹Ð¹•Ð ‰É•ÑÉ¥•Ù…±}É½ÕÑ•Ìˆ°mt¤(€€€€€€€€€€€¥˜ÍÑÈ¡É½ÕÑ”¤¹½Ð¥¸™•Ñ¡}É½ÕÑ•Ì(€€€€€€€ô(€€€€€€€ÕÉ±Ì€ôíÍÑÈ¡ÕÉ°¤™½ÈÕÉ°¥¸‘½Õµ•¹Ð¹•Ð ‰‘¥Í½Ù•É•‘}ÕÉ±Ìˆ°mt¤¥˜ÍÑÈ¡ÕÉ°¥ô(€€€€€€€‘½Õµ•¹Ñl‰‘¥Í½Ù•Éå}½Õ¹Ð‰t€ôµ…à Ä°±•¸¡É½ÕÑ•Ì¤°±•¸¡ÕÉ±Ì¤¤(()‘•˜}•µÁÑå}µ…¹¥™•ÍÐ¡Ñ…É•Ñ}‘…Ñ”èÍÑÈ¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€É•ÑÕÉ¸ì(€€€€€€€€‰‘…Ñ”ˆèÑ…É•Ñ}‘…Ñ”°(€€€€€€€€‰•¹•É…Ñ•‘}…Ðˆè}¹½Ý}©ÍÐ ¤°(€€€€€€€€‰½±±•Ñ¥½¹}Ý¥¹‘½Üˆè}½±±•Ñ¥½¹}Ý¥¹‘½Ü¡Ñ…É•Ñ}‘…Ñ”¤°(€€€€€€€€‰‘½Õµ•¹ÑÌˆèmt°(€€€€€€€€‰ÍÕµµ…Éäˆè}µ…¹¥™•ÍÑ}ÍÕµµ…Éä¡mt¤°(€€€ô(()‘•˜}½±±•Ñ¥½¹}Ý¥¹‘½Ü¡Ñ…É•Ñ}‘…Ñ”èÍÑÈ¤€´ø‘¥ÑmÍÑÈ°ÍÑÉtè(€€€©ÍÐ€ôi½¹•%¹™¼ ‰Í¥„½Q½­å¼ˆ¤¥˜i½¹•%¹™¼•±Í”Ñ¥µ•é½¹”¡Ñ¥µ•‘•±Ñ„¡¡½ÕÉÌôä¤¤(€€€ÑÉäè(€€€€€€€ÍÑ…ÉÐ€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡Ñ…É•Ñ}‘…Ñ”¤¹É•Á±…”¡Ñé¥¹™¼õ©ÍÐ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€ÍÑ…ÉÐ€ô‘…Ñ•Ñ¥µ”¹¹½Ü¡©ÍÐ¤¹É•Á±…”¡¡½ÕÈôÀ°µ¥¹ÕÑ”ôÀ°Í•½¹ôÀ°µ¥É½Í•½¹ôÀ¤(€€€•¹€ôÍÑ…ÉÐ€¬Ñ¥µ•‘•±Ñ„¡‘…åÌôÄ¤(€€€É•ÑÕÉ¸ì(€€€€€€€€‰ÍÑ…ÉÐˆèÍÑ…ÉÐ¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€‰•¹ˆè•¹¹¥Í½™½Éµ…Ð ¤°(€€€€€€€€‰Ñ¥µ•é½¹”ˆè€‰Í¥„½Q½­å¼ˆ°(€€€ô(()‘•˜}Í½ÕÉ•}ÑåÁ”¡ÕÉ°èÍÑÈ¤€´øÍÑÈè(€€€¡½ÍÐ€ô}‘½µ…¥¸¡ÕÉ°¤(€€€¥˜¡½ÍÐ€ôô€‰Í•Œ¹½Øˆ½È¡½ÍÐ¹•¹‘ÍÝ¥Ñ  ˆ¹Í•Œ¹½Øˆ¤è(€€€€€€€É•ÑÕÉ¸€‰Í•}™¥±¥¹œˆ(€€€¥˜¡½ÍÐ¹•¹‘ÍÝ¥Ñ  ˆ¹½Øˆ¤½È¡½ÍÐ¥¸ì‰™•‘•É…±É•Í•ÉÙ”¹½Øˆ°€‰Ý¡¥Ñ•¡½ÕÍ”¹½Ø‰ôè(€€€€€€€É•ÑÕÉ¸€‰½™™¥¥…±}‘½Õµ•¹Ðˆ(€€€¥˜ÕÉ±Á…ÉÍ”¡ÕÉ°¤¹Á…Ñ ¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ  ˆ¹Á‘˜ˆ¤è(€€€€€€€É•ÑÕÉ¸€‰Á‘™}‘½Õµ•¹Ðˆ(€€€É•ÑÕÉ¸€‰¹•ÝÍ}…ÉÑ¥±”ˆ(()‘•˜}‘¥ÍÁ±…å}Á…Ñ ¡Á…Ñ èA…Ñ ¤€´øÍÑÈè(€€€ÑÉäè(€€€€€€€É•ÑÕÉ¸Á…Ñ ¹É•Í½±Ù” ¤¹É•±…Ñ¥Ù•}Ñ¼¡I==Q}%H¹É•Í½±Ù” ¤¤¹…Í}Á½Í¥à ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€É•ÑÕÉ¸ÍÑÈ¡Á…Ñ ¹É•Í½±Ù” ¤¤(()‘•˜}É•…‘}Ý½É­ÍÁ…•}Ñ•áÐ¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€Ñ•áÐ€ôÍÑÈ¡Ù…±Õ”½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€Á…Ñ €ôA…Ñ ¡Ñ•áÐ¤(€€€¥˜¹½ÐÁ…Ñ ¹¥Í}…‰Í½±ÕÑ” ¤è(€€€€€€€Á…Ñ €ôI==Q}%H€¼Á…Ñ (€€€ÑÉäè(€€€€€€€É•ÑÕÉ¸Á…Ñ ¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€•á•ÁÐ=MÉÉ½Èè(€€€€€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}…ÁÁ•¹‘}Õ¹¥ÅÕ”¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°­•äèÍÑÈ°Ù…±Õ”èÍÑÈ¤€´ø9½¹”è(€€€¥Ñ•µÌ€ô‘½Õµ•¹Ð¹•Ð¡­•ä¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡¥Ñ•µÌ°±¥ÍÐ¤è(€€€€€€€¥Ñ•µÌ€ômt(€€€€€€€‘½Õµ•¹Ñm­•åt€ô¥Ñ•µÌ(€€€¥˜Ù…±Õ”…¹Ù…±Õ”¹½Ð¥¸¥Ñ•µÌè(€€€€€€€¥Ñ•µÌ¹…ÁÁ•¹¡Ù…±Õ”¤(()‘•˜}…ÁÁ•¹‘}…ÑÑ•µÁÐ¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt°…ÑÑ•µÁÐè‘¥ÑmÍÑÈ°¹åt¤€´ø9½¹”è(€€€…ÑÑ•µÁÑÌ€ô‘½Õµ•¹Ð¹•Ð ‰…ÑÑ•µÁÑÌˆ¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡…ÑÑ•µÁÑÌ°±¥ÍÐ¤è(€€€€€€€…ÑÑ•µÁÑÌ€ômt(€€€€€€€‘½Õµ•¹Ñl‰…ÑÑ•µÁÑÌ‰t€ô…ÑÑ•µÁÑÌ(€€€…ÑÑ•µÁÑÌ¹…ÁÁ•¹¡…ÑÑ•µÁÐ¤(()‘•˜}™¥ÉÍÐ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt°­•åÌè%Ñ•É…‰±•mÍÑÉt¤€´ø¹äè(€€€™½È­•ä¥¸­•åÌè(€€€€€€€¥˜¥Ñ•´¹•Ð¡­•ä¤¹½Ð¥¸€¡9½¹”°€ˆˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸¥Ñ•´¹•Ð¡­•ä¤(€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}™¥ÉÍÑ}É½ÕÑ”¡‘½Õµ•¹Ðè‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€É½ÕÑ•Ì€ô‘½Õµ•¹Ð¹•Ð ‰É•ÑÉ¥•Ù…±}É½ÕÑ•Ìˆ°mt¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡É½ÕÑ•Ì°±¥ÍÐ¤è(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€™½ÈÉ½ÕÑ”¥¸É½ÕÑ•Ìè(€€€€€€€¥˜É½ÕÑ”¹½Ð¥¸ì‰É•ÅÕ•ÍÑÌˆ°€‰Ñ…Ù¥±å}•áÑÉ…Ñ}‰…Í¥Œˆ°€‰Ñ…Ù¥±å}•áÑÉ…Ñ}…‘Ù…¹•‰ôè(€€€€€€€€€€€É•ÑÕÉ¸ÍÑÈ¡É½ÕÑ”¤(€€€É•ÑÕÉ¸ÍÑÈ¡É½ÕÑ•ÍlÁt¤¥˜É½ÕÑ•Ì•±Í”€ˆˆ(()‘•˜}‘½µ…¥¸¡ÕÉ°èÍÑÈ¤€´øÍÑÈè(€€€É•ÑÕÉ¸ÕÉ±Á…ÉÍ”¡ÕÉ°¤¹¹•Ñ±½Œ¹±½Ý•È ¤¹ÍÁ±¥Ð ˆèˆ°€Ä¥lÁt¹É•µ½Ù•ÁÉ•™¥à ‰ÝÝÜ¸ˆ¤(()‘•˜}•¹Ù}¹½¹¹•…Ñ¥Ù•}¥¹Ð¡•¹Øè‘¥ÑmÍÑÈ°ÍÑÉt°­•äèÍÑÈ°‘•™…Õ±Ðè¥¹Ð¤€´ø¥¹Ðè(€€€ÑÉäè(€€€€€€€Ù…±Õ”€ô¥¹Ð¡ÍÑÈ¡•¹Ø¹•Ð¡­•ä°‘•™…Õ±Ð¤¤¹ÍÑÉ¥À ¤¤(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è(€€€€€€€É•ÑÕÉ¸‘•™…Õ±Ð(€€€É•ÑÕÉ¸µ…à À°Ù…±Õ”¤(()‘•˜}•¹Ù}Á½Í¥Ñ¥Ù•}¥¹Ð (€€€•¹Øè‘¥ÑmÍÑÈ°ÍÑÉt°(€€€­•äèÍÑÈ°(€€€‘•™…Õ±Ðè¥¹Ð°(€€€€¨°(€€€µ…á¥µÕ´è¥¹Ð°(¤€´ø¥¹Ðè(€€€Ù…±Õ”€ô}•¹Ù}¹½¹¹•…Ñ¥Ù•}¥¹Ð¡•¹Ø°­•ä°‘•™…Õ±Ð¤(€€€¥˜Ù…±Õ”€ðô€Àè(€€€€€€€Ù…±Õ”€ô‘•™…Õ±Ð(€€€É•ÑÕÉ¸µ¥¸¡Ù…±Õ”°µ…á¥µÕ´¤(()‘•˜}½¹™¥ÕÉ•‘}Ñ…Ù¥±å}­•ä¡Ù…±Õ”èÍÑÈ¤€´ø‰½½°è(€€€É•ÑÕÉ¸‰½½°¡Ù…±Õ”…¹Ù…±Õ”¹±½Ý•È ¤¹½Ð¥¸%9Y1%}-e}Y1UL¤(()‘•˜}¹½Ý}©ÍÐ ¤€´øÍÑÈè(€€€©ÍÐ€ôi½¹•%¹™¼ ‰Í¥„½Q½­å¼ˆ¤¥˜i½¹•%¹™¼•±Í”Ñ¥µ•é½¹”¡Ñ¥µ•‘•±Ñ„¡¡½ÕÉÌôä¤¤(€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤¹…ÍÑ¥µ•é½¹”¡©ÍÐ¤¹É•Á±…”¡µ¥É½Í•½¹ôÀ¤¹¥Í½™½Éµ…Ð ¤(