from __future__ import annotations

from typing import Any

from nasdaq_cafe.config import missing


REVIEW_THEME_TERMS = (
    "samsung",
    "ai leadership",
    "ai",
    "semiconductor",
    "semiconductors",
    "chip",
    "chips",
    "nvidia",
    "nvda",
    "broadcom",
    "avgo",
    "apple",
    "aapl",
    "amd",
    "tsm",
    "tsmc",
    "treasury",
    "yield",
    "nasdaq",
    "sox",
)


def build_article_review_targets(news_items: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    missing_data: list[dict[str, str]] = []
    core_candidates = _sort_news([item for item in news_items if item.get("driver_type") == "core_driver"])
    context_candidates = _sort_news([item for item in news_items if item.get("driver_type") == "context_candidate"])

    must_review: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in core_candidates:
        if not _has_review_fields(item) or _is_blocked_review_url(item.get("url")):
            missing_data.append(
                missing("Article Review Targets", f"Core Driver has missing or blocked review URL: {_clean(item.get('title'))}", "low")
            )
            continue
        target = _target_from_item(
            item,
            "must",
            "Core Driver selected for the main NASDAQ/AI/semiconductor narrative; verify the article body before treating it as a main material.",
            "Use as a possible main-story fact base only after body review confirms the title/snippet and causal bridge.",
        )
        url_key = target["url"].strip().lower()
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        must_review.append(target)

    core_text = " ".join(_item_text(item) for item in must_review)
    optional_review: list[dict[str, Any]] = []
    for item in context_candidates:
        if len(optional_review) >= 3:
            break
        if not _has_review_fields(item) or _is_blocked_review_url(item.get("url")):
            continue
        if not _has_strong_optional_bridge(item, core_text):
            continue
        target = _target_from_item(
            item,
            "optional",
            "Context Candidate may reinforce a Core Driver theme if the body adds concrete support.",
            "Use only as background or a supporting bridge, not as the main cause, unless body review shows stronger evidence.",
        )
        url_key = target["url"].strip().lower()
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        optional_review.append(target)

    usually_do_not_review = [
        {
            "title": "Excluded / Low Value Summary",
            "source": "Derived from low-value news classification",
            "url": "",
            "related_tickers": [],
            "review_priority": "usually_do_not_review",
            "reason_to_review": "Usually skip; these items have weak causal bridge, low relevance, blocked URL shape, or insufficient fields.",
            "expected_use": "Do not use for scripts, Canva copy, thumbnails, or descriptions unless a human explicitly asks to inspect an excluded item.",
        }
    ]

    return (
        {
            "must_review": must_review,
            "optional_review": optional_review,
            "usually_do_not_review": usually_do_not_review,
        },
        missing_data,
    )


def _target_from_item(item: dict[str, Any], priority: str, reason: str, expected_use: str) -> dict[str, Any]:
    return {
        "title": _clean(item.get("title")),
        "source": _clean(item.get("source")),
        "published_at": _clean(item.get("published_at")),
        "url": _clean(item.get("url")),
        "snippet": _clean(item.get("snippet")),
        "related_tickers": [ticker for ticker in item.get("related_tickers", []) if _clean(ticker)],
        "why_relevant": _clean(item.get("why_relevant")),
        "confidence": _clean(item.get("confidence")),
        "causal_bridge": _clean(item.get("causal_bridge")),
        "review_priority": priority,
        "reason_to_review": reason,
        "expected_use": expected_use,
    }


def _sort_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (item.get("selected_for_handoff") is True, _as_int(item.get("relevance_score")), _clean(item.get("published_at"))),
        reverse=True,
    )


def _has_review_fields(item: dict[str, Any]) -> bool:
    return bool(_clean(item.get("title")) and _clean(item.get("source")) and _clean(item.get("url")))


def _is_blocked_review_url(value: Any) -> bool:
    url = _clean(value).lower()
    if not url:
        return True
    blocked = (
        "facebook.com",
        "twitter.com",
        "x.com/",
        "linkedin.com/posts",
        "reuters.com/plus",
        "finance.yahoo.com/quote",
        "/market-activity/stocks/",
        "/quote/",
    )
    if any(marker in url for marker in blocked):
        return True
    return url.rstrip("/").endswith(("reuters.com", "cnbc.com", "marketwatch.com", "nasdaq.com", "finance.yahoo.com"))


def _has_strong_optional_bridge(item: dict[str, Any], core_text: str) -> bool:
    text = _item_text(item)
    if not _clean(item.get("causal_bridge")):
        return False
    if _contains_any(text, REVIEW_THEME_TERMS):
        return True
    return any(term in text and term in core_text for term in REVIEW_THEME_TERMS)


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("title"),
        item.get("source"),
        item.get("url"),
        item.get("snippet"),
        item.get("why_relevant"),
        item.get("causal_bridge"),
        " ".join(str(ticker) for ticker in item.get("related_tickers", [])),
    ]
    return " ".join(_clean(part).lower() for part in parts if part is not None)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
