from __future__ import annotations

from pathlib import Path
from typing import Any

from nasdaq_cafe.cache import write_json


def write_source_pack(output_dir: Path, pack: dict[str, Any]) -> None:
    write_json(output_dir / "source_pack.json", pack)
    (output_dir / "source_pack.md").write_text(render_source_pack_md(pack), encoding="utf-8")


def render_source_pack_md(pack: dict[str, Any]) -> str:
    statuses = pack.get("source_status", {})
    cache = pack.get("cache", {})
    market_data = pack.get("market_data", {})
    macro = pack.get("macro", {})
    lines = [
        "# 朝のNASDAQカフェ source_pack",
        "",
        "## 取得日時",
        f"- date: {pack.get('date', '')}",
        f"- generated_at: {pack.get('generated_at', '')}",
        "",
        "## キャッシュ利用状況",
        f"- cache_used: {cache.get('cache_used')}",
        f"- refresh: {cache.get('refresh')}",
        "",
        "## 取得状況",
        f"- Longbridge: {statuses.get('Longbridge', '')}",
        f"- FRED: {statuses.get('FRED', '')}",
        f"- FMP: {statuses.get('FMP', '')}",
        f"- SEC / IR: {statuses.get('SEC / IR', '')}",
        f"- RSS: {statuses.get('RSS', '')}",
        f"- SerpAPI: {statuses.get('SerpAPI', '')}",
        f"- Tavily: {statuses.get('Tavily', '')}",
        f"- Market Movers: {statuses.get('Market Movers', '')}",
        f"- Economic Calendar: {statuses.get('Economic Calendar', '')}",
        f"- Raw Archive: {statuses.get('Raw Archive', '')}",
        f"- Article Full Text: {statuses.get('Article Full Text', '')}",
        f"- GDELT Radar: {statuses.get('GDELT Radar', '')}",
        "",
        "## Collector Metadata",
        *_render_collector_metadata(pack.get("collector_metadata", {})),
        "",
        "## 市場データ",
        f"- NASDAQ: {_format_value(market_data.get('NASDAQ'))}",
        f"- SOX: {_format_value(market_data.get('SOX'))}",
        f"- 米10年金利: {_format_value(macro.get('DGS10'))}",
        f"- ドル円: {_format_value(market_data.get('USDJPY'))}",
        "",
        "## 固定ウォッチ銘柄",
    ]

    for item in pack.get("watchlist", []):
        lines.append(f"- {item.get('ticker')}: {_format_quote(item)}")

    lines.extend(["", "## Market Movers候補"])
    movers = pack.get("market_movers", [])
    if movers:
        for mover in movers:
            lines.append(
                f"- {mover.get('ticker', '')}: category={mover.get('category', '')}, change={mover.get('change_percent')}, "
                f"reason={mover.get('reason', '')}, confidence={mover.get('confidence', '')}, "
                f"source={mover.get('source', '')}"
            )
    else:
        lines.append("- 取得済み候補なし")

    lines.extend(["", "## 経済イベント"])
    lines.extend(_render_economic_events(pack.get("economic_events", {})))

    lines.extend(["", "## Article Review Targets"])
    lines.extend(_render_article_review_targets(pack.get("article_review_targets", {})))
    lines.extend(["", "## Article Full Text Status"])
    lines.extend(_render_article_fulltext_status(pack.get("article_fulltext_status", {})))
    lines.extend(["", "## GDELT Radar Status"])
    lines.extend(
        _render_gdelt_radar_status(
            pack.get("gdelt_radar_status", {}),
            pack.get("gdelt_radar_candidates", []),
            pack.get("daily_lead_theme_candidates", []),
            pack.get("statement_radar_status", {}),
            pack.get("statement_radar_candidates", []),
            pack.get("statement_fulltext_candidates", []),
        )
    )

    lines.extend(["", "## 取得ニュース一覧"])
    news_items = pack.get("news_items", [])
    if news_items:
        for item in news_items:
            lines.append(f"- {item.get('title', '')}")
            lines.append(f"  - source: {item.get('source', '')}")
            lines.append(f"  - published_at: {item.get('published_at', '')}")
            lines.append(f"  - url: {item.get('url', '')}")
            lines.append(f"  - snippet: {item.get('snippet', '')}")
            lines.append(f"  - related_tickers: {', '.join(item.get('related_tickers', []))}")
            lines.append(f"  - why_relevant: {item.get('why_relevant', '')}")
            lines.append(f"  - confidence: {item.get('confidence', '')}")
            lines.append(f"  - driver_type: {item.get('driver_type', '')}")
            lines.append(f"  - relevance_score: {item.get('relevance_score', '')}")
            lines.append(f"  - causal_bridge: {item.get('causal_bridge', '')}")
            lines.append(f"  - selected_for_handoff: {item.get('selected_for_handoff', '')}")
            lines.append(f"  - filter_reason: {item.get('filter_reason', '')}")
    else:
        lines.append("- 取得済みニュースなし")

    selected = pack.get("selected_candidates", {})
    lines.extend(["", "## 採用候補", "", "### primary_candidates"])
    main_topics = selected.get("primary_candidates", [])
    if main_topics:
        for topic in main_topics:
            lines.append(f"- title: {topic.get('title', '')}")
            lines.append(f"  - reason: {topic.get('reason', '')}")
            lines.append(f"  - confidence: {topic.get('confidence', '')}")
            lines.append(f"  - sources: {', '.join(str(source) for source in topic.get('sources', []) if source)}")
    else:
        lines.append("- title:")
        lines.append("- reason:")
        lines.append("- confidence:")
        lines.append("- sources:")

    lines.extend(["", "### ticker_candidates"])
    ticker_topics = selected.get("ticker_candidates", [])
    if ticker_topics:
        for topic in ticker_topics:
            lines.append(f"- ticker: {topic.get('ticker', '')}")
            lines.append(f"  - reason: {topic.get('reason', '')}")
            lines.append(f"  - confidence: {topic.get('confidence', '')}")
            lines.append(f"  - sources: {', '.join(str(source) for source in topic.get('sources', []) if source)}")
    else:
        lines.append("- ticker:")
        lines.append("- reason:")
        lines.append("- confidence:")
        lines.append("- sources:")

    lines.extend(["", "## 未取得情報"])
    missing_data = pack.get("missing_data", [])
    if missing_data:
        for item in missing_data:
            lines.append(
                f"- {item.get('source', '')}: {item.get('reason', '')} "
                f"({item.get('severity', '')})"
            )
    else:
        lines.append("- なし")

    lines.append("")
    return "\n".join(lines)


def _format_quote(value: Any) -> str:
    if not value:
        return "null"
    return (
        f"price={value.get('price')}, change={value.get('change')}, "
        f"change_percent={value.get('change_percent')}, session={value.get('session', 'unknown')}"
    )


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return _format_quote(value) if "price" in value else ", ".join(f"{key}={val}" for key, val in value.items())
    return str(value)


def _render_collector_metadata(metadata: dict[str, Any]) -> list[str]:
    rss = metadata.get("RSS") if isinstance(metadata, dict) else None
    api_key_status = metadata.get("api_key_status", {}) if isinstance(metadata, dict) else {}
    search = metadata.get("Search", {}) if isinstance(metadata, dict) else {}
    lines: list[str] = []
    if isinstance(api_key_status, dict) and api_key_status:
        lines.extend(
            [
                "### API Key Status",
                f"- SERPAPI_API_KEY: {api_key_status.get('SERPAPI_API_KEY', '')}",
                f"- TAVILY_API_KEY: {api_key_status.get('TAVILY_API_KEY', '')}",
                f"- FRED_API_KEY: {api_key_status.get('FRED_API_KEY', '')}",
                f"- FMP_API_KEY: {api_key_status.get('FMP_API_KEY', '')}",
                f"- SEC_USER_AGENT: {api_key_status.get('SEC_USER_AGENT', '')}",
                f"- LONGBRIDGE: {api_key_status.get('LONGBRIDGE', '')}",
            ]
        )
    if isinstance(search, dict) and search:
        query_count = search.get("query_count", {})
        search_key_status = search.get("api_key_status", {})
        lines.extend(
            [
                "### Search",
                f"- SERPAPI_API_KEY: {search_key_status.get('SERPAPI_API_KEY', '')}",
                f"- TAVILY_API_KEY: {search_key_status.get('TAVILY_API_KEY', '')}",
                f"- query_count: {query_count}",
                f"- raw_item_count: {search.get('raw_item_count', {})}",
                f"- adopted_supplement_count: {search.get('adopted_supplement_count', {})}",
                f"- policy: {search.get('policy', '')}",
            ]
        )
    if not isinstance(rss, dict):
        if lines:
            lines.append("- RSS: metadataなし")
            return lines
        return ["- RSS: metadataなし"]
    lines.extend([
        "### RSS",
        f"- strategy: {rss.get('strategy', '')}",
        f"- actual_route: {rss.get('actual_route', '')}",
        f"- vendor_repo_path: {rss.get('vendor_repo_path', '')}",
        f"- vendor_repo_present: {rss.get('vendor_repo_present', '')}",
        f"- vendor_repo_commit_hash: {rss.get('vendor_repo_commit_hash', '')}",
        f"- vendor_license_present: {rss.get('vendor_license_present', '')}",
        f"- vendor_pyproject_present: {rss.get('vendor_pyproject_present', '')}",
        f"- vendor_requirements_present: {rss.get('vendor_requirements_present', '')}",
        f"- vendor_finnews_importable: {rss.get('vendor_finnews_importable', '')}",
        f"- implementation_note: {rss.get('implementation_note', '')}",
        f"- route_summary: {rss.get('route_summary', {})}",
    ])
    routes = rss.get("feed_routes", [])
    if routes:
        lines.append("- feed_routes:")
        for route in routes:
            lines.append(
                f"  - {route.get('source', '')}: status={route.get('status', '')}, "
                f"route={route.get('route', '')}, count={route.get('count', '')}"
            )
    return lines


def _render_economic_events(economic_events: dict[str, Any]) -> list[str]:
    if not economic_events:
        return ["- 取得済み経済イベントなし"]
    lines = ["### Past Events"]
    lines.extend(_render_economic_event_items(economic_events.get("past_events", [])))
    lines.extend(["", "### Upcoming Events"])
    lines.extend(_render_economic_event_items(economic_events.get("upcoming_events", [])))
    lines.extend(["", "### Low Value Macro Summary"])
    summary = economic_events.get("low_value_macro_summary", [])
    if summary:
        for item in summary:
            lines.append(
                f"- {item.get('source', '')}: count={item.get('count', '')}, reason={item.get('reason', '')}"
            )
    else:
        lines.append("- 低価値マクロ候補なし")
    return lines


def _render_economic_event_items(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- 該当イベントなし"]
    lines: list[str] = []
    for item in items:
        lines.append(f"- {item.get('event_name', '')}")
        lines.append(f"  - event_date: {item.get('event_date', '')}")
        lines.append(f"  - event_time: {item.get('event_time', '')}")
        lines.append(f"  - event_window: {item.get('event_window', '')}")
        lines.append(f"  - importance: {item.get('importance', '')}")
        lines.append(f"  - event_impact_score: {item.get('event_impact_score', '')}")
        lines.append(f"  - actual: {item.get('actual')}")
        lines.append(f"  - forecast: {item.get('forecast')}")
        lines.append(f"  - previous: {item.get('previous')}")
        lines.append(f"  - market_expectation: {item.get('market_expectation', '')}")
        lines.append(f"  - why_viewer_should_care: {item.get('why_viewer_should_care', '')}")
        lines.append(f"  - driver_type: {item.get('driver_type', '')}")
        lines.append(f"  - causal_bridge: {item.get('causal_bridge', '')}")
        lines.append(f"  - confidence: {item.get('confidence', '')}")
        lines.append(f"  - source: {item.get('source', '')}")
        lines.append(f"  - url: {item.get('url', '')}")
        lines.append(f"  - filter_reason: {item.get('filter_reason', '')}")
    return lines


def _render_article_review_targets(targets: dict[str, Any]) -> list[str]:
    if not isinstance(targets, dict):
        return ["- article_review_targetsなし"]
    lines: list[str] = ["### Must Review Before Writing"]
    lines.extend(_render_review_target_items(targets.get("must_review", [])))
    lines.extend(["", "### Optional Review"])
    lines.extend(_render_review_target_items(targets.get("optional_review", [])))
    lines.extend(["", "### Usually Do Not Review"])
    usually = targets.get("usually_do_not_review", [])
    if usually:
        for item in usually:
            lines.append(f"- {item.get('title', '')}: {item.get('reason_to_review', '')}")
            lines.append(f"  - expected_use: {item.get('expected_use', '')}")
    else:
        lines.append("- Excluded / Low Value Summary は通常確認不要")
    return lines


def _render_article_fulltext_status(status: dict[str, Any]) -> list[str]:
    if not isinstance(status, dict) or not status:
        return ["- article_fulltext_status: not generated"]
    return [
        f"- raw_path: {status.get('raw_path', '')}",
        f"- manifest_path: {status.get('manifest_path', '')}",
        f"- articles_path: {status.get('articles_path', '')}",
        f"- cache_used: {status.get('cache_used', '')}",
        f"- target_count: {status.get('target_count', 0)}",
        f"- attempted_count: {status.get('attempted_count', 0)}",
        f"- complete_count: {status.get('complete_count', status.get('readable_count', 0))}",
        f"- failed_count: {status.get('failed_count', 0)}",
        f"- excluded_count: {status.get('excluded_count', 0)}",
        f"- not_attempted_limit_count: {status.get('not_attempted_limit_count', 0)}",
        f"- pending_count: {status.get('pending_count', 0)}",
        f"- raw_html_count: {status.get('raw_html_count', 0)}",
        f"- extracted_text_count: {status.get('extracted_text_count', 0)}",
        f"- readable_count: {status.get('readable_count', 0)}",
        f"- alternate_readable_count: {status.get('alternate_readable_count', 0)}",
        f"- unreadable_count: {status.get('unreadable_count', 0)}",
        f"- total_full_text_chars: {status.get('total_full_text_chars', 0)}",
        f"- readable_count_before_fallback: {status.get('readable_count_before_fallback', 0)}",
        f"- unreadable_count_before_fallback: {status.get('unreadable_count_before_fallback', 0)}",
        f"- fallback_attempted_count: {status.get('fallback_attempted_count', 0)}",
        f"- fallback_success_count: {status.get('fallback_success_count', 0)}",
        f"- fallback_failed_count: {status.get('fallback_failed_count', 0)}",
        f"- fallback_query_count: {status.get('fallback_query_count', 0)}",
    ]


def _render_gdelt_radar_status(
    status: dict[str, Any],
    candidates: list[dict[str, Any]],
    daily_lead_candidates: list[dict[str, Any]],
    statement_status: dict[str, Any],
    statement_candidates: list[dict[str, Any]],
    statement_fulltext_candidates: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(status, dict) or not status:
        return ["- gdelt_radar_status: not generated"]
    lines = [
        f"- raw_path: {status.get('raw_path', '')}",
        f"- cache_used: {status.get('cache_used', '')}",
        f"- status: {status.get('status', '')}",
        f"- query_count: {status.get('query_count', 0)}",
        f"- raw_result_count: {status.get('raw_result_count', 0)}",
        f"- candidate_count: {status.get('candidate_count', 0)}",
        f"- accepted_count: {status.get('accepted_count', 0)}",
        f"- rejected_count: {status.get('rejected_count', 0)}",
        f"- fulltext_candidate_count: {status.get('fulltext_candidate_count', 0)}",
    ]
    categories = status.get("categories", {})
    if isinstance(categories, dict) and categories:
        lines.append("- categories:")
        for category, counts in categories.items():
            if not isinstance(counts, dict):
                continue
            lines.append(
                f"  - {category}: raw={counts.get('raw_count', 0)}, "
                f"accepted={counts.get('accepted_count', 0)}, rejected={counts.get('rejected_count', 0)}, "
                f"fulltext_candidates={counts.get('fulltext_candidate_count', 0)}"
            )
    lines.append("- candidates:")
    if candidates:
        for item in candidates[:20]:
            lines.append(f"  - title: {item.get('title', '')}")
            lines.append(f"    category: {item.get('category', '')}")
            lines.append(f"    source: {item.get('source', '')}")
            lines.append(f"    url: {item.get('url', '')}")
            lines.append(f"    published_at: {item.get('published_at', '')}")
            lines.append(f"    seen_at: {item.get('seen_at', '')}")
            lines.append(f"    related_tickers: {', '.join(item.get('related_tickers', []))}")
            lines.append(f"    radar_score: {item.get('radar_score', 0)}")
            lines.append(f"    decision: {item.get('decision', '')}")
            lines.append(f"    decision_reason: {item.get('decision_reason', '')}")
            lines.append(f"    fulltext_candidate: {item.get('fulltext_candidate', False)}")
    else:
        lines.append("  - none")
    lines.append("- daily_lead_theme_candidates:")
    if daily_lead_candidates:
        for item in daily_lead_candidates[:5]:
            lines.append(f"  - candidate_theme: {item.get('candidate_theme', '')}")
            lines.append(f"    main_category: {item.get('main_category', '')}")
            lines.append(f"    related_tickers: {', '.join(item.get('related_tickers', []))}")
            lines.append(f"    candidate_count: {item.get('candidate_count', 0)}")
            lines.append(f"    statement_signal_count: {item.get('statement_signal_count', 0)}")
            lines.append(f"    radar_score: {item.get('radar_score', 0)}")
            lines.append(f"    body_verified: {item.get('body_verified', False)}")
            lines.append(f"    use_as_script_evidence: {item.get('use_as_script_evidence', False)}")
            lines.append(f"    needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
            lines.append(f"    market_causality_confirmed: {item.get('market_causality_confirmed', False)}")
    else:
        lines.append("  - none")
    lines.append("- statement_radar_status:")
    if isinstance(statement_status, dict) and statement_status:
        lines.append(f"  - status: {statement_status.get('status', '')}")
        lines.append(f"  - candidate_count: {statement_status.get('candidate_count', 0)}")
        lines.append(f"  - telop_candidate_count: {statement_status.get('telop_candidate_count', 0)}")
        lines.append(f"  - fulltext_candidate_count: {statement_status.get('fulltext_candidate_count', 0)}")
        lines.append(f"  - top_speakers: {statement_status.get('top_speakers', [])}")
        lines.append(f"  - top_topics: {statement_status.get('top_topics', [])}")
    else:
        lines.append("  - none")
    lines.append("- statement_radar_candidates:")
    if statement_candidates:
        for item in statement_candidates[:10]:
            lines.append(f"  - speaker_name: {item.get('speaker_name', '')}")
            lines.append(f"    statement_topic: {item.get('statement_topic', '')}")
            lines.append(f"    title: {item.get('title', '')}")
            lines.append(f"    source: {item.get('source', '')}")
            lines.append(f"    url: {item.get('url', '')}")
            lines.append(f"    verification_status: {item.get('verification_status', '')}")
            lines.append(f"    telop_candidate: {item.get('telop_candidate', False)}")
            lines.append(f"    body_verified: {item.get('body_verified', False)}")
            lines.append(f"    use_as_script_evidence: {item.get('use_as_script_evidence', False)}")
            lines.append(f"    needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
            lines.append(f"    market_causality_confirmed: {item.get('market_causality_confirmed', False)}")
    else:
        lines.append("  - none")
    lines.append("- statement_fulltext_candidates:")
    if statement_fulltext_candidates:
        for item in statement_fulltext_candidates[:5]:
            lines.append(f"  - speaker_name: {item.get('speaker_name', '')}")
            lines.append(f"    statement_topic: {item.get('statement_topic', '')}")
            lines.append(f"    url: {item.get('url', '')}")
            lines.append(f"    needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
    else:
        lines.append("  - none")
    return lines


def _render_review_target_items(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- 該当なし"]
    lines: list[str] = []
    for item in items:
        lines.append(f"- {item.get('title', '')}")
        lines.append(f"  - source: {item.get('source', '')}")
        lines.append(f"  - published_at: {item.get('published_at', '')}")
        lines.append(f"  - url: {item.get('url', '')}")
        lines.append(f"  - snippet: {item.get('snippet', '')}")
        lines.append(f"  - review_priority: {item.get('review_priority', '')}")
        lines.append(f"  - reason_to_review: {item.get('reason_to_review', '')}")
        lines.append(f"  - expected_use: {item.get('expected_use', '')}")
        lines.append(f"  - related_tickers: {', '.join(item.get('related_tickers', []))}")
        lines.append(f"  - confidence: {item.get('confidence', '')}")
        lines.append(f"  - causal_bridge: {item.get('causal_bridge', '')}")
    return lines
