from __future__ import annotations

import importlib.util
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from nasdaq_cafe.cache import load_or_fetch
from nasdaq_cafe.config import ROOT_DIR, RSS_SOURCES, RunConfig, missing
from nasdaq_cafe.processing.normalize import clamp_text, normalize_news_item, strip_html, utc_now_iso
from nasdaq_cafe.processing.relevance import explain_relevance


VENDOR_REPO_PATH = ROOT_DIR / "vendor" / "finance-news-aggregator"


def collect_rss_news(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "rss_news.json"

    def fetcher() -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        feed_status: list[dict[str, Any]] = []
        collector_metadata = _collector_metadata()
        for source, url in RSS_SOURCES.items():
            source_items, route_status = _fetch_one_feed(source, url)
            collector_metadata["feed_routes"].append(route_status)
            if route_status["status"] == "ok":
                normalized_items = [_normalize_rss_item(item, source) for item in source_items]
                items.extend(normalized_items)
                feed_status.append(
                    {
                        "source": source,
                        "url": url,
                        "status": "ok",
                        "count": len(normalized_items),
                        "route": route_status.get("route", ""),
                        "attempts": route_status.get("attempts", []),
                    }
                )
            else:
                error = {
                    "source": source,
                    "url": url,
                    "error": route_status.get("error", "RSS route failed."),
                }
                errors.append(error)
                feed_status.append(
                    {
                        "source": source,
                        "url": url,
                        "status": "error",
                        "count": 0,
                        "error": error["error"],
                        "route": route_status.get("route", ""),
                        "attempts": route_status.get("attempts", []),
                    }
                )
        collector_metadata["route_summary"] = _route_summary(feed_status)
        collector_metadata["actual_route"] = _actual_route(feed_status)
        collector_metadata["implementation_note"] = _implementation_note(collector_metadata)
        return {
            "source": "RSS",
            "status": _rss_status(items, errors),
            "generated_at": utc_now_iso(),
            "items": items,
            "errors": errors,
            "feed_status": feed_status,
            "collector_metadata": collector_metadata,
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    normalized = []
    for item in payload.get("items", []):
        news = _normalize_rss_item(item, item.get("source", "RSS"))
        normalized.append(news)

    missing_data = []
    for error in payload.get("errors", []):
        source = error.get("source", "unknown RSS source")
        detail = error.get("error", "unknown error")
        missing_data.append(missing("RSS", f"{source} RSS failed: {detail}", "low"))

    if not normalized:
        reason = "RSS feeds did not return Phase 1 news."
        if payload.get("errors"):
            reason += " Errors: " + "; ".join(error.get("source", "") for error in payload.get("errors", []))
        missing_data.append(missing("RSS", reason, "low"))

    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "news_items": normalized,
        "raw": payload,
        "collector_metadata": payload.get("collector_metadata", {}),
        "missing_data": missing_data,
    }


def _rss_status(items: list[dict[str, Any]], errors: list[dict[str, str]]) -> str:
    if items and errors:
        return "partial"
    if items:
        return "ok"
    return "empty"


def _normalize_rss_item(item: dict[str, Any], fallback_source: str) -> dict[str, Any]:
    news = normalize_news_item(item, fallback_source)
    if not news.get("snippet"):
        news["snippet"] = clamp_text(news.get("title", ""))
    news["why_relevant"] = news.get("why_relevant") or explain_relevance(news)
    return news


def _fetch_one_feed(source: str, url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    routes = [
        ("vendor_finance_news_aggregator", lambda: _fetch_with_vendor_finance_news_aggregator(source)),
        ("feedparser", lambda: _fetch_with_feedparser(source, url)),
        ("requests+ElementTree", lambda: _fetch_with_elementtree(source, url)),
    ]
    for route_name, fetcher in routes:
        try:
            items = fetcher()
            if items:
                attempts.append({"route": route_name, "status": "ok", "count": len(items)})
                return items, {
                    "source": source,
                    "url": url,
                    "status": "ok",
                    "route": route_name,
                    "count": len(items),
                    "attempts": attempts,
                }
            attempts.append({"route": route_name, "status": "empty", "count": 0})
        except ImportError as exc:
            attempts.append({"route": route_name, "status": "unavailable", "error": str(exc)})
        except Exception as exc:
            attempts.append({"route": route_name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
    return [], {
        "source": source,
        "url": url,
        "status": "error",
        "route": "",
        "count": 0,
        "attempts": attempts,
        "error": "; ".join(
            f"{attempt.get('route')}: {attempt.get('status')} {attempt.get('error', '')}".strip()
            for attempt in attempts
        ),
    }


def _fetch_with_vendor_finance_news_aggregator(source: str) -> list[dict[str, Any]]:
    News = _import_vendor_news_class()
    client = News(cache_ttl=0)
    fetchers = {
        "Yahoo Finance": lambda: client.yahoo_finance.news(),
        "MarketWatch": lambda: client.market_watch.top_stories(),
        "NASDAQ": lambda: client.nasdaq.stocks_feed(),
        "CNBC": lambda: client.cnbc.news_feed(topic="top_news"),
    }
    fetcher = fetchers.get(source)
    if fetcher is None:
        raise RuntimeError(f"vendored finance-news-aggregator has no configured adapter for {source}.")
    return _normalize_finnews_payload(fetcher(), source)


def _fetch_with_feedparser(source: str, url: str) -> list[dict[str, Any]]:
    if not _module_available("feedparser"):
        raise ImportError("feedparser is not installed.")
    import requests
    import feedparser  # type: ignore

    response = requests.get(url, headers={"User-Agent": "nasdaq-cafe-phase1/0.1"}, timeout=8)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    entries = getattr(parsed, "entries", []) or []
    output = []
    for entry in entries[:20]:
        output.append(
            {
                "title": strip_html(entry.get("title", "")),
                "source": source,
                "published_at": strip_html(entry.get("published", "") or entry.get("updated", "")),
                "url": strip_html(entry.get("link", "")),
                "snippet": clamp_text(entry.get("summary", "") or entry.get("description", "")),
            }
        )
    return [item for item in output if item.get("title") and item.get("url")]


def _fetch_with_elementtree(source: str, url: str) -> list[dict[str, Any]]:
    import requests

    response = requests.get(url, headers={"User-Agent": "nasdaq-cafe-phase1/0.1"}, timeout=6)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    output: list[dict[str, Any]] = []

    rss_items = root.findall(".//item")
    if rss_items:
        for item in rss_items[:20]:
            output.append(
                {
                    "title": _child_text(item, "title"),
                    "source": source,
                    "published_at": _child_text(item, "pubDate"),
                    "url": _child_text(item, "link"),
                    "snippet": clamp_text(_child_text(item, "description")),
                }
            )
        return output

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns)[:20]:
        link = ""
        link_node = entry.find("atom:link", ns)
        if link_node is not None:
            link = link_node.attrib.get("href", "")
        output.append(
            {
                "title": _child_text(entry, "title", ns),
                "source": source,
                "published_at": _child_text(entry, "updated", ns) or _child_text(entry, "published", ns),
                "url": link,
                "snippet": clamp_text(_child_text(entry, "summary", ns)),
            }
        )
    return output


def _child_text(node: ET.Element, tag: str, namespace: dict[str, str] | None = None) -> str:
    if namespace:
        child = node.find(f"atom:{tag}", namespace)
    else:
        child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return strip_html(child.text)


def _normalize_finnews_payload(payload: Any, fallback_source: str) -> list[dict[str, Any]]:
    records = _records_from_payload(payload)
    output: list[dict[str, Any]] = []
    for record in records[:20]:
        if not isinstance(record, dict):
            continue
        item = {
            "title": _first(record, ["title", "headline", "name"]),
            "source": _first(record, ["source", "publisher", "site"], fallback_source),
            "published_at": _first(record, ["published_at", "published", "pubDate", "date", "datetime", "time"]),
            "url": _first(record, ["url", "link", "href"]),
            "snippet": clamp_text(_first(record, ["snippet", "summary", "description", "abstract"], "")),
        }
        item["title"] = strip_html(item["title"])
        item["source"] = strip_html(item["source"] or fallback_source)
        item["published_at"] = strip_html(item["published_at"])
        item["url"] = strip_html(item["url"])
        if not item["snippet"]:
            item["snippet"] = clamp_text(item["title"])
        if item["title"] and item["url"]:
            output.append(item)
    return output


def _records_from_payload(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if hasattr(payload, "to_dict"):
        try:
            records = payload.to_dict("records")
            if isinstance(records, list):
                return records
        except TypeError:
            converted = payload.to_dict()
            return _records_from_payload(converted)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "news", "articles", "entries", "feed", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _records_from_payload(value)
                if nested:
                    return nested
        if any(key in payload for key in ("title", "headline", "url", "link")):
            return [payload]
        values = list(payload.values())
        if values and all(isinstance(value, dict) for value in values):
            return values
    return []


def _first(item: dict[str, Any], keys: list[str], default: str = "") -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _collector_metadata() -> dict[str, Any]:
    vendor = _vendor_repo_metadata()
    feedparser_installed = _module_available("feedparser")
    notes = [
        "Phase 1.11h uses the vendored GitHub repo vendor/finance-news-aggregator as the primary RSS adapter.",
        "The vendored repo is not updated during normal runs; its commit hash is recorded for reproducibility.",
        "pip packages fin-news / FinNews are not treated as the primary route.",
        "feedparser is the fallback for each RSS feed.",
        "requests+ElementTree is retained as a built-in final fallback so the run can continue without optional packages.",
    ]
    if not vendor["vendor_repo_present"]:
        notes.append("vendor/finance-news-aggregator is missing; RSS collection will use fallback routes.")
    elif not vendor["vendor_finnews_importable"]:
        notes.append(f"Vendored finnews is not importable: {vendor.get('vendor_finnews_import_error', '')}")
    if not feedparser_installed:
        notes.append("feedparser is listed in requirements.txt but is not importable in the current environment; ElementTree fallback may be used.")
    metadata = {
        "phase": "Phase 1.11h",
        "collector": "rss_collector",
        "strategy": "vendor_finance_news_aggregator primary; feedparser fallback; requests+ElementTree final fallback",
        **vendor,
        "finance_news_aggregator": {
            "source": "GitHub vendored repo",
            "url": "https://github.com/areed1192/finance-news-aggregator.git",
            "role": "primary",
            "pip_package_candidates": ["fin-news (import finnews)", "FinNews"],
            "pip_primary": False,
        },
        "feedparser": {
            "installed": feedparser_installed,
            "role": "fallback",
        },
        "implementation_note": " ".join(notes),
        "feed_routes": [],
        "route_summary": {},
        "actual_route": "",
    }
    return metadata


def _route_summary(feed_status: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in feed_status:
        route = item.get("route") or "none"
        summary[route] = summary.get(route, 0) + 1
    return summary


def _actual_route(feed_status: list[dict[str, Any]]) -> str:
    routes = [str(item.get("route") or "") for item in feed_status if item.get("status") == "ok"]
    unique = sorted({route for route in routes if route})
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return "mixed: " + ", ".join(unique)


def _implementation_note(metadata: dict[str, Any]) -> str:
    base_note = str(metadata.get("implementation_note", "")).strip()
    route_summary = metadata.get("route_summary", {})
    feed_routes = metadata.get("feed_routes", [])
    parts = [base_note] if base_note else []
    if route_summary:
        parts.append(f"Actual RSS route summary for this run: {route_summary}.")
    if feed_routes:
        details = []
        for route in feed_routes:
            details.append(
                f"{route.get('source')}: route={route.get('route') or 'none'}, "
                f"status={route.get('status')}, count={route.get('count')}"
            )
        parts.append("Feed route details: " + "; ".join(details) + ".")
    return " ".join(parts)


def _vendor_repo_metadata() -> dict[str, Any]:
    path = VENDOR_REPO_PATH
    present = path.exists()
    importable, import_error = _vendor_finnews_import_status()
    return {
        "vendor_repo_path": str(path),
        "vendor_repo_present": present,
        "vendor_repo_commit_hash": _vendor_commit_hash(path) if present else "",
        "vendor_license_present": (path / "LICENSE").exists(),
        "vendor_readme_present": (path / "README.md").exists(),
        "vendor_pyproject_present": (path / "pyproject.toml").exists(),
        "vendor_requirements_present": (path / "requirements.txt").exists(),
        "vendor_finnews_package_present": (path / "finnews").is_dir(),
        "vendor_finnews_importable": importable,
        "vendor_finnews_import_error": import_error,
    }


def _vendor_commit_hash(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return f"unavailable: {(completed.stderr or completed.stdout).strip()}"
    return completed.stdout.strip()


def _vendor_finnews_import_status() -> tuple[bool, str]:
    try:
        _import_vendor_news_class()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _import_vendor_news_class():
    if not VENDOR_REPO_PATH.exists():
        raise ImportError(f"{VENDOR_REPO_PATH} does not exist.")
    if not (VENDOR_REPO_PATH / "finnews").is_dir():
        raise ImportError(f"{VENDOR_REPO_PATH / 'finnews'} does not exist.")

    vendor_root = str(VENDOR_REPO_PATH.resolve())
    if vendor_root not in sys.path:
        sys.path.insert(0, vendor_root)

    for module_name in list(sys.modules):
        if module_name == "finnews" or module_name.startswith("finnews."):
            module = sys.modules[module_name]
            module_file = Path(str(getattr(module, "__file__", "") or "")).resolve()
            if not _path_is_relative_to(module_file, VENDOR_REPO_PATH.resolve()):
                sys.modules.pop(module_name, None)

    import finnews  # type: ignore
    from finnews import News  # type: ignore

    module_file = Path(str(getattr(finnews, "__file__", "") or "")).resolve()
    if not _path_is_relative_to(module_file, VENDOR_REPO_PATH.resolve()):
        raise ImportError(f"finnews resolved outside vendor repo: {module_file}")
    return News


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
