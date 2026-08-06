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
            "content_type": content_type,
        }
        common["attempt"].update(
            {
                "read_url": read_url,
                "http_status": int(response.status_code),
                "content_type": content_type,
                "raw_file": raw_display,
            }
        )
        if exceeded:
            return {
                **common,
                "fulltext_status": "failed",
                "access_status": "failed",
                "failure_reason": f"configured max article size exceeded ({max_bytes} bytes)",
            }
        if response.status_code == 429:
            return {**common, "fulltext_status": "failed", "access_status": "rate_limited", "failure_reason": "HTTP 429"}
        if response.status_code in {401, 403, 451}:
            return {**common, "fulltext_status": "failed", "access_status": "blocked", "failure_reason": f"HTTP {response.status_code}"}
        if response.status_code == 402:
            return {**common, "fulltext_status": "failed", "access_status": "paywalled", "failure_reason": "HTTP 402"}
        if response.status_code < 200 or response.status_code >= 300:
            return {**common, "fulltext_status": "failed", "access_status": "failed", "failure_reason": f"HTTP {response.status_code}"}

        if _is_pdf(content_type, read_url):
            text, pdf_error = _extract_pdf_text(content)
            if pdf_error:
                return {**common, "fulltext_status": "failed", "access_status": "extraction_failed", "failure_reason": pdf_error}
        elif "html" in content_type.lower() or "text" in content_type.lower() or not content_type:
            html = _decode_response(content, content_type)
            blocked_status = _detect_blocked_or_paywalled(html)
            if blocked_status:
                return {
                    **common,
                    "fulltext_status": "failed",
                    "access_status": blocked_status,
                    "failure_reason": "paywall/login/CAPTCHA marker detected; no bypass attempted",
                }
            text = _extract_article_text(html)
        else:
            return {
                **common,
                "fulltext_status": "failed",
                "access_status": "failed",
                "failure_reason": f"unsupported content-type: {content_type or 'unknown'}",
            }

        if len(text) < MIN_ARTICLE_CHARS:
            return {
                **common,
                "fulltext_status": "failed",
                "access_status": "extraction_failed",
                "failure_reason": f"extracted article text was too short ({len(text)} chars)",
            }
        text_path = config.raw_dir / "articles" / f"{document_id}.txt"
        text_path.write_text(text, encoding="utf-8")
        return {
            **common,
            "fulltext_status": "complete",
            "access_status": "readable",
            "extracted_text_file": _display_path(text_path),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "char_count": len(text),
            "failure_reason": None,
        }
    finally:
        try:
            response.close()
        except Exception:
            pass


def _rescue_with_tavily(config: RunConfig, documents: list[dict[str, Any]]) -> dict[str, int]:
    key = str(config.env.get("TAVILY_API_KEY") or "").strip()
    basic_limit = _env_nonnegative_int(
        config.env,
        "NASDAQ_CAFE_TAVILY_EXTRACT_BASIC_LIMIT",
        DEFAULT_TAVILY_BASIC_LIMIT,
    )
    advanced_limit = _env_nonnegative_int(
        config.env,
        "NASDAQ_CAFE_TAVILY_EXTRACT_ADVANCED_LIMIT",
        DEFAULT_TAVILY_ADVANCED_LIMIT,
    )
    usage = {
        "basic_limit": basic_limit,
        "advanced_limit": advanced_limit,
        "basic_attempted": 0,
        "advanced_attempted": 0,
        "success_count": 0,
        "failed_count": 0,
    }
    failed = [document for document in documents if document.get("fulltext_status") == "failed"]
    if not _configured_tavily_key(key):
        return usage

    for document in failed:
        rescued = False
        if usage["basic_attempted"] < basic_limit:
            usage["basic_attempted"] += 1
            rescued = _attempt_tavily_extract(config, document, key, "basic")
        else:
            _append_attempt(
                document,
                {
                    "route": "tavily_extract_basic",
                    "status": "not_attempted_limit",
                    "reason": "configured Tavily basic extract limit reached",
                },
            )
        if not rescued:
            if usage["advanced_attempted"] < advanced_limit:
                usage["advanced_attempted"] += 1
                rescued = _attempt_tavily_extract(config, document, key, "advanced")
            else:
                _append_attempt(
                    document,
                    {
                        "route": "tavily_extract_advanced",
                        "status": "not_attempted_limit",
                        "reason": "configured Tavily advanced extract limit reached",
                    },
                )
        if rescued:
            usage["success_count"] += 1
        else:
            usage["failed_count"] += 1
    return usage


def _attempt_tavily_extract(
    config: RunConfig,
    document: dict[str, Any],
    api_key: str,
    depth: str,
) -> bool:
    route = f"tavily_extract_{depth}"
    attempted_at = _now_jst()
    url = str(document.get("url") or "")
    document_id = str(document.get("document_id") or "")
    attempt = {"route": route, "attempted_at": attempted_at, "url": url}
    try:
        response = requests.post(
            "https://api.tavily.com/extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "urls": [url],
                "extract_depth": depth,
                "format": "text",
                "include_images": False,
                "include_usage": True,
            },
            timeout=45 if depth == "advanced" else 20,
        )
        status_code = int(response.status_code)
        payload = response.json()
    except Exception as exc:
        attempt.update({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
        _append_attempt(document, attempt)
        return False

    response_path = config.raw_dir / "articles" / f"{document_id}.tavily-{depth}.json"
    write_json(response_path, payload)
    attempt.update(
        {
            "http_status": status_code,
            "response_file": _display_path(response_path),
            "request_id": payload.get("request_id") if isinstance(payload, dict) else None,
            "usage": payload.get("usage") if isinstance(payload, dict) else None,
        }
    )
    if status_code < 200 or status_code >= 300 or not isinstance(payload, dict):
        attempt.update({"status": "failed", "reason": f"HTTP {status_code}"})
        _append_attempt(document, attempt)
        return False

    results = payload.get("results", [])
    result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    text = str(result.get("raw_content") or "")
    marker_status = _plain_text_block_status(text)
    if marker_status or len(text) < MIN_ARTICLE_CHARS:
        reason = marker_status or f"Tavily text was too short ({len(text)} chars)"
        attempt.update({"status": "failed", "reason": reason})
        _append_attempt(document, attempt)
        return False

    text_path = config.raw_dir / "articles" / f"{document_id}.txt"
    text_path.write_text(text, encoding="utf-8")
    document.update(
        {
            "retrieved_at": attempted_at,
            "fulltext_status": "complete",
            "access_status": "alternate_readable",
            "extracted_text_file": _display_path(text_path),
            "tavily_response_file": _display_path(response_path),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "char_count": len(text),
            "failure_reason": None,
        }
    )
    _append_unique(document, "retrieval_routes", route)
    attempt["status"] = "complete"
    _append_attempt(document, attempt)
    return True


def _article_fulltext_payload(
    config: RunConfig,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    documents = manifest.get("documents", []) if isinstance(manifest, dict) else []
    for document in documents:
        if not isinstance(document, dict):
            continue
        if document.get("fulltext_status") == "complete":
            text = _read_workspace_text(document.get("extracted_text_file"))
            items.append(
                {
                    "article_id": document.get("document_id", ""),
                    "review_priority": "unranked",
                    "source_group": _first_route(document),
                    "title": document.get("title", ""),
                    "source": document.get("publisher", ""),
                    "published_at": document.get("published_at", ""),
                    "primary_url": document.get("url", ""),
                    "read_url": document.get("read_url", document.get("url", "")),
                    "access_status": document.get("access_status", "readable"),
                    "used_alternate_source": document.get("access_status") == "alternate_readable",
                    "alternate_sources": [],
                    "related_tickers": [],
                    "related_indexes": [],
                    "full_text": text,
                    "full_text_char_count": len(text),
                    "retrieved_at_jst": document.get("retrieved_at"),
                    "notes_for_chatgpt": "Full text is stored without summarization or shortening.",
                    "raw_file": document.get("raw_file"),
                    "extracted_text_file": document.get("extracted_text_file"),
                    "content_hash": document.get("content_hash"),
                    "retrieval_routes": document.get("retrieval_routes", []),
                }
            )
        else:
            unreadable.append(
                {
                    "article_id": document.get("document_id", ""),
                    "review_priority": "unranked",
                    "source_group": _first_route(document),
                    "title": document.get("title", ""),
                    "source": document.get("publisher", ""),
                    "published_at": document.get("published_at", ""),
                    "primary_url": document.get("url", ""),
                    "access_status": document.get("access_status", "failed"),
                    "fulltext_status": document.get("fulltext_status", "pending"),
                    "reason": document.get("failure_reason") or "full text was not retrieved",
                    "notes_for_chatgpt": document.get("failure_reason") or "full text was not retrieved",
                }
            )
    summary = _fulltext_summary(documents, items)
    return {
        "date": config.target_date,
        "generated_at_jst": _now_jst(),
        "collector": "raw_archive_fulltext_collector",
        "manifest_path": f"output/{config.target_date}/raw/{MANIFEST_FILE_NAME}",
        "summary": summary,
        "items": items,
        "unreadable": unreadable,
        "fallback_search_supplements": [],
        "fallback_attempts": [
            attempt
            for document in documents
            if isinstance(document, dict)
            for attempt in document.get("attempts", [])
            if isinstance(attempt, dict) and str(attempt.get("route", "")).startswith("tavily_extract")
        ],
    }


def _fulltext_summary(
    documents: list[Any],
    items: list[dict[str, Any]],
) -> dict[str, int]:
    valid_documents = [document for document in documents if isinstance(document, dict)]
    attempted = [
        document
        for document in valid_documents
        if any(
            isinstance(attempt, dict)
            and attempt.get("route") == "requests"
            and attempt.get("status") != "not_attempted_limit"
            for attempt in document.get("attempts", [])
        )
    ]
    tavily_attempts = [
        attempt
        for document in valid_documents
        for attempt in document.get("attempts", [])
        if isinstance(attempt, dict)
        and str(attempt.get("route") or "").startswith("tavily_extract_")
        and attempt.get("status") != "not_attempted_limit"
    ]
    tavily_attempted_documents = [
        document
        for document in valid_documents
        if any(
            isinstance(attempt, dict)
            and str(attempt.get("route") or "").startswith("tavily_extract_")
            and attempt.get("status") != "not_attempted_limit"
            for attempt in document.get("attempts", [])
        )
    ]
    tavily_success_count = sum(
        1 for document in tavily_attempted_documents if document.get("access_status") == "alternate_readable"
    )
    unresolved_after_fallback = sum(
        1 for document in attempted if document.get("fulltext_status") == "failed"
    )
    return {
        "target_count": len(valid_documents),
        "attempted_count": len(attempted),
        "complete_count": sum(1 for document in valid_documents if document.get("fulltext_status") == "complete"),
        "readable_count": sum(1 for document in valid_documents if document.get("access_status") == "readable"),
        "alternate_readable_count": sum(1 for document in valid_documents if document.get("access_status") == "alternate_readable"),
        "failed_count": sum(1 for document in valid_documents if document.get("fulltext_status") == "failed"),
        "excluded_count": sum(1 for document in valid_documents if document.get("fulltext_status") == "excluded"),
        "not_attempted_limit_count": sum(
            1 for document in valid_documents if document.get("fulltext_status") == "not_attempted_limit"
        ),
        "pending_count": sum(1 for document in valid_documents if document.get("fulltext_status") == "pending"),
        "raw_html_count": sum(
            1 for document in valid_documents if str(document.get("raw_file") or "").lower().endswith(".html")
        ),
        "extracted_text_count": sum(1 for document in valid_documents if document.get("extracted_text_file")),
        "total_full_text_chars": sum(int(item.get("full_text_char_count") or 0) for item in items),
        "tavily_attempted_count": len(tavily_attempts),
        "tavily_success_count": tavily_success_count,
        "tavily_failed_count": unresolved_after_fallback,
        # Compatibility fields used by older output renderers.
        "readable_count_before_fallback": sum(
            1 for document in valid_documents if document.get("access_status") == "readable"
        ),
        "unreadable_count_before_fallback": len(attempted)
        - sum(1 for document in valid_documents if document.get("access_status") == "readable"),
        "fallback_attempted_count": len(tavily_attempts),
        "fallback_success_count": tavily_success_count,
        "fallback_failed_count": unresolved_after_fallback,
        "fallback_query_count": 0,
    }


def _eligible_status(document: dict[str, Any], force: bool, retry_failed: bool) -> bool:
    status = str(document.get("fulltext_status") or "pending")
    if force:
        return True
    if retry_failed:
        return status in {"failed", "not_attempted_limit", "pending"}
    return status == "pending"


def _preflight_exclusion(url: str, env: dict[str, str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "invalid_url"
    host = parsed.netloc.lower().split(":", 1)[0].removeprefix("www.")
    if any(host == item or host.endswith(f".{item}") for item in EXCLUDED_HOSTS):
        return "social_or_video_url"
    lowered = url.lower()
    if "/quote/" in lowered or "finance.yahoo.com/quote" in lowered:
        return "quote_page_not_article"
    disallowed = {
        item.strip().lower().removeprefix("www.")
        for item in str(env.get("NASDAQ_CAFE_FULLTEXT_DISALLOWED_DOMAINS") or "").split(",")
        if item.strip()
    }
    if any(host == item or host.endswith(f".{item}") for item in disallowed):
        return "configured_disallowed_domain"
    return ""


def _mark_excluded(document: dict[str, Any], reason: str) -> None:
    document.update(
        {
            "fulltext_status": "excluded",
            "access_status": "not_attempted",
            "failure_reason": reason,
            "retrieved_at": _now_jst(),
        }
    )
    _append_attempt(document, {"route": "preflight", "status": "excluded", "reason": reason})


def _mark_not_attempted_limit(document: dict[str, Any], limit: int) -> None:
    if document.get("fulltext_status") == "complete":
        _append_attempt(
            document,
            {
                "route": "requests",
                "status": "not_attempted_limit",
                "reason": "configured_fulltext_attempt_limit; previously completed cache retained",
            },
        )
        return
    document.update(
        {
            "fulltext_status": "not_attempted_limit",
            "access_status": "not_attempted",
            "failure_reason": f"configured full-text attempt limit reached ({limit}); URL retained for retry",
            "retrieved_at": None,
        }
    )
    _append_attempt(
        document,
        {
            "route": "requests",
            "status": "not_attempted_limit",
            "reason": "configured_fulltext_attempt_limit",
        },
    )


def _unexpected_failure_patch(document: dict[str, Any], exc: Exception) -> dict[str, Any]:
    attempted_at = _now_jst()
    return {
        "retrieved_at": attempted_at,
        "retrieval_route": "requests",
        "fulltext_status": "failed",
        "access_status": "failed",
        "failure_reason": f"internal collector error: {type(exc).__name__}: {exc}",
        "attempt": {
            "route": "requests",
            "attempted_at": attempted_at,
            "status": "failed",
            "reason": f"internal collector error: {type(exc).__name__}: {exc}",
        },
    }


def _apply_document_patch(document: dict[str, Any], patch: dict[str, Any]) -> None:
    attempt = patch.pop("attempt", None)
    route = patch.pop("retrieval_route", None)
    document.update(patch)
    if route:
        _append_unique(document, "retrieval_routes", str(route))
    if isinstance(attempt, dict):
        attempt.setdefault("status", document.get("fulltext_status"))
        attempt.setdefault("reason", document.get("failure_reason"))
        _append_attempt(document, attempt)


def _write_document_metadata(config: RunConfig, document: dict[str, Any]) -> None:
    document_id = str(document.get("document_id") or "")
    if not document_id:
        return
    path = config.raw_dir / "articles" / f"{document_id}.json"
    document["metadata_file"] = _display_path(path)
    write_json(path, document)


def _read_response_bytes(response: Any, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    if hasattr(response, "iter_content"):
        iterator = response.iter_content(chunk_size=65_536)
    else:  # Simple response doubles used in tests.
        iterator = [getattr(response, "content", b"")]
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            remaining = max(0, max_bytes - sum(len(item) for item in chunks))
            if remaining:
                chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
    return b"".join(chunks), False


def _decode_response(content: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    encodings = [match.group(1).strip('"\'')] if match else []
    encodings.extend(["utf-8", "cp1252"])
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _extract_pdf_text(content: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return "", "PDF was saved, but pypdf is not installed for text extraction"
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join(page for page in pages if page)
    except Exception as exc:
        return "", f"PDF extraction failed: {type(exc).__name__}: {exc}"
    if len(text) < MIN_ARTICLE_CHARS:
        return "", f"PDF text was too short ({len(text)} chars)"
    return text, ""


def _plain_text_block_status(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in PAYWALL_TEXT_MARKERS):
        return "paywall marker detected in Tavily response"
    if any(marker in lowered for marker in BLOCKED_TEXT_MARKERS):
        return "block/login/CAPTCHA marker detected in Tavily response"
    return ""


def _is_pdf(content_type: str, url: str) -> bool:
    return "application/pdf" in content_type.lower() or urlparse(url).path.lower().endswith(".pdf")


def _canonicalize_url(value: str) -> str:
    text = value.strip()
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    if scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, host, path, "", urlencode(query, doseq=True), ""))


def _assign_duplicate_groups(documents: list[dict[str, Any]]) -> None:
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        document["duplicate_group_id"] = None
        document["duplicate_of"] = None
        document["syndicated_copy"] = False
        content_hash = str(document.get("content_hash") or "")
        if content_hash:
            by_hash.setdefault(content_hash, []).append(document)
    for content_hash, members in by_hash.items():
        if len(members) < 2:
            continue
        group_id = "dup_" + content_hash[:12]
        representative_id = str(members[0].get("document_id") or "")
        for index, member in enumerate(members):
            member["duplicate_group_id"] = group_id
            member["duplicate_of"] = None if index == 0 else representative_id
            member["syndicated_copy"] = index > 0


def _manifest_summary(documents: list[Any]) -> dict[str, int]:
    valid = [document for document in documents if isinstance(document, dict)]
    return {
        "document_count": len(valid),
        "complete_count": sum(1 for document in valid if document.get("fulltext_status") == "complete"),
        "failed_count": sum(1 for document in valid if document.get("fulltext_status") == "failed"),
        "excluded_count": sum(1 for document in valid if document.get("fulltext_status") == "excluded"),
        "not_attempted_limit_count": sum(
            1 for document in valid if document.get("fulltext_status") == "not_attempted_limit"
        ),
        "pending_count": sum(1 for document in valid if document.get("fulltext_status") == "pending"),
        "duplicate_group_count": len(
            {document.get("duplicate_group_id") for document in valid if document.get("duplicate_group_id")}
        ),
    }


def _normalize_discovery_counts(documents: list[dict[str, Any]]) -> None:
    fetch_routes = {"requests", "tavily_extract_basic", "tavily_extract_advanced"}
    for document in documents:
        routes = {
            str(route)
            for route in document.get("retrieval_routes", [])
            if str(route) not in fetch_routes
        }
        urls = {str(url) for url in document.get("discovered_urls", []) if str(url)}
        document["discovery_count"] = max(1, len(routes), len(urls))


def _empty_manifest(target_date: str) -> dict[str, Any]:
    return {
        "date": target_date,
        "generated_at": _now_jst(),
        "collection_window": _collection_window(target_date),
        "documents": [],
        "summary": _manifest_summary([]),
    }


def _collection_window(target_date: str) -> dict[str, str]:
    jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
    try:
        start = datetime.fromisoformat(target_date).replace(tzinfo=jst)
    except ValueError:
        start = datetime.now(jst).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": "Asia/Tokyo",
    }


def _source_type(url: str) -> str:
    host = _domain(url)
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return "sec_filing"
    if host.endswith(".gov") or host in {"federalreserve.gov", "whitehouse.gov"}:
        return "official_document"
    if urlparse(url).path.lower().endswith(".pdf"):
        return "pdf_document"
    return "news_article"


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT_DIR.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _read_workspace_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if not path.is_absolute():
        path = ROOT_DIR / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _append_unique(document: dict[str, Any], key: str, value: str) -> None:
    items = document.get(key)
    if not isinstance(items, list):
        items = []
        document[key] = items
    if value and value not in items:
        items.append(value)


def _append_attempt(document: dict[str, Any], attempt: dict[str, Any]) -> None:
    attempts = document.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
        document["attempts"] = attempts
    attempts.append(attempt)


def _first(item: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item.get(key)
    return ""


def _first_route(document: dict[str, Any]) -> str:
    routes = document.get("retrieval_routes", [])
    if not isinstance(routes, list):
        return ""
    for route in routes:
        if route not in {"requests", "tavily_extract_basic", "tavily_extract_advanced"}:
            return str(route)
    return str(routes[0]) if routes else ""


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0].removeprefix("www.")


def _env_nonnegative_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _env_positive_int(
    env: dict[str, str],
    key: str,
    default: int,
    *,
    maximum: int,
) -> int:
    value = _env_nonnegative_int(env, key, default)
    if value <= 0:
        value = default
    return min(value, maximum)


def _configured_tavily_key(value: str) -> bool:
    return bool(value and value.lower() not in INVALID_KEY_VALUES)


def _now_jst() -> str:
    jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
    return datetime.now(UTC).astimezone(jst).replace(microsecond=0).isoformat()
