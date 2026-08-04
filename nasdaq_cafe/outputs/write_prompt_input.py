from __future__ import annotations

from pathlib import Path
from typing import Any


def write_prompt_input(output_dir: Path, pack: dict[str, Any]) -> None:
    (output_dir / "prompt_input.md").write_text(render_prompt_input(pack), encoding="utf-8")


def render_prompt_input(pack: dict[str, Any]) -> str:
    watchlist_lines = []
    for item in pack.get("watchlist", []):
        watchlist_lines.append(
            f"- {item.get('ticker', '')}: price={item.get('price')}, "
            f"change={item.get('change')}, change_percent={item.get('change_percent')}, "
            f"session={item.get('session', 'unknown')}, source_symbol={item.get('source_symbol', '')}"
        )
    if not watchlist_lines:
        watchlist_lines.append("- 取得済みquoteなし")

    news_lines = []
    for item in pack.get("news_items", [])[:20]:
        news_lines.append(f"- {item.get('title', '')} ({item.get('source', '')})")
        news_lines.append(f"  - url: {item.get('url', '')}")
        news_lines.append(f"  - snippet: {item.get('snippet', '')}")
        news_lines.append(f"  - related_tickers: {', '.join(item.get('related_tickers', []))}")
        news_lines.append(f"  - why_relevant: {item.get('why_relevant', '')}")
        news_lines.append(f"  - confidence: {item.get('confidence', '')}")
        news_lines.append(f"  - driver_type: {item.get('driver_type', '')}")
        news_lines.append(f"  - relevance_score: {item.get('relevance_score', '')}")
        news_lines.append(f"  - causal_bridge: {item.get('causal_bridge', '')}")
        news_lines.append(f"  - selected_for_handoff: {item.get('selected_for_handoff', '')}")
        news_lines.append(f"  - filter_reason: {item.get('filter_reason', '')}")
    if not news_lines:
        news_lines.append("- 取得済みニュースなし")

    mover_lines = []
    for mover in pack.get("market_movers", [])[:20]:
        mover_lines.append(
            f"- {mover.get('ticker', '')}: category={mover.get('category', '')}, "
            f"change={mover.get('change_percent')}, reason={mover.get('reason', '')}, "
            f"source={mover.get('source', '')}, confidence={mover.get('confidence', '')}"
        )
    if not mover_lines:
        mover_lines.append("- 取得済み候補なし")

    economic_lines = _render_economic_events(pack.get("economic_events", {}))
    article_review_lines = _render_article_review_targets(pack.get("article_review_targets", {}))
    gdelt_lines = _render_gdelt_radar(pack)

    missing_lines = []
    for item in pack.get("missing_data", []):
        missing_lines.append(f"- {item.get('source', '')}: {item.get('reason', '')} ({item.get('severity', '')})")
    if not missing_lines:
        missing_lines.append("- なし")

    return "\n".join(
        [
            "# 情報パック：朝のNASDAQカフェ",
            "",
            "このファイルは、朝のNASDAQカフェ用の情報パックです。",
            "",
            "## 識別情報",
            "",
            "番組名：朝のNASDAQカフェ",
            "",
            "## 対象",
            "",
            "NASDAQ",
            "SOX",
            "米10年金利",
            "ドル円",
            "",
            "NVDA",
            "MSFT",
            "AAPL",
            "AMZN",
            "GOOGL",
            "META",
            "AVGO",
            "TSM",
            "AMD",
            "TSLA",
            "",
            "Market Moversから採用された銘柄",
            "",
            "## 情報源",
            "",
            "### 取得メタデータ",
            "",
            f"- date: {pack.get('date', '')}",
            f"- generated_at: {pack.get('generated_at', '')}",
            f"- cache_used: {pack.get('cache', {}).get('cache_used')}",
            f"- refresh: {pack.get('cache', {}).get('refresh')}",
            f"- session_note: {pack.get('market_status', {}).get('session_note', '')}",
            "",
            "### 市場データ",
            "",
            f"- NASDAQ: {pack.get('market_data', {}).get('NASDAQ')}",
            f"- SOX: {pack.get('market_data', {}).get('SOX')}",
            f"- 米10年金利 DGS10: {pack.get('macro', {}).get('DGS10')}",
            f"- ドル円 USDJPY: {pack.get('market_data', {}).get('USDJPY')}",
            "",
            "### 固定ウォッチ銘柄quote",
            "",
            *watchlist_lines,
            "",
            "### Market Movers候補",
            "",
            *mover_lines,
            "",
            "### 経済イベント",
            "",
            *economic_lines,
            "",
            "### Article Review Targets",
            "",
            *article_review_lines,
            "",
            "### GDELT Radar / Statement Radar",
            "",
            *gdelt_lines,
            "",
            "### 取得ニュース一覧",
            "",
            *news_lines,
            "",
            "### 未取得情報",
            "",
            *missing_lines,
            "",
        ]
    )


def _render_economic_events(economic_events: dict[str, Any]) -> list[str]:
    if not economic_events:
        return ["- 取得済み経済イベントなし"]

    lines: list[str] = ["#### Past Events"]
    lines.extend(_render_economic_event_items(economic_events.get("past_events", []), include_actual=True))

    lines.extend(["", "#### Upcoming Events"])
    lines.extend(_render_economic_event_items(economic_events.get("upcoming_events", []), include_actual=False))

    lines.extend(["", "#### Low Value Macro Summary"])
    summary = economic_events.get("low_value_macro_summary", [])
    if summary:
        for item in summary:
            lines.append(f"- {item.get('source', '')}: count={item.get('count')}, reason={item.get('reason', '')}")
    else:
        lines.append("- 低価値マクロ候補なし")

    return lines


def _render_economic_event_items(items: list[dict[str, Any]], include_actual: bool) -> list[str]:
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

        if include_actual:
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
    if not isinstance(targets, dict) or not targets:
        return ["- article_review_targetsなし"]

    lines: list[str] = ["#### Must Review"]
    lines.extend(_render_review_target_items(targets.get("must_review", [])))

    lines.extend(["", "#### Optional Review"])
    lines.extend(_render_review_target_items(targets.get("optional_review", [])))

    lines.extend(["", "#### Usually Do Not Review"])
    usually = targets.get("usually_do_not_review", [])
    if usually:
        for item in usually:
            lines.append(f"- {item.get('title', '')}")
            lines.append(f"  - source: {item.get('source', '')}")
            lines.append(f"  - reason_to_review: {item.get('reason_to_review', '')}")
            lines.append(f"  - expected_use: {item.get('expected_use', '')}")
    else:
        lines.append("- none")

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


def _render_gdelt_radar(pack: dict[str, Any]) -> list[str]:
    status = pack.get("gdelt_radar_status", {})
    daily_lead_candidates = pack.get("daily_lead_theme_candidates", [])
    statement_status = pack.get("statement_radar_status", {})
    statement_candidates = pack.get("statement_radar_candidates", [])
    statement_fulltext_candidates = pack.get("statement_fulltext_candidates", [])

    if not isinstance(status, dict) or not status:
        return ["- gdelt_radar_statusなし"]

    lines: list[str] = [
        "- GDELT Radar: 本文未確認の候補URL",
        "- Statement Radar: 発言候補メタデータ",
        "",
        "#### GDELT Radar Status",
        f"- status: {status.get('status', '')}",
        f"- query_count: {status.get('query_count', 0)}",
        f"- raw_result_count: {status.get('raw_result_count', 0)}",
        f"- candidate_count: {status.get('candidate_count', 0)}",
        f"- accepted_count: {status.get('accepted_count', 0)}",
        f"- rejected_count: {status.get('rejected_count', 0)}",
        f"- fulltext_candidate_count: {status.get('fulltext_candidate_count', 0)}",
        "",
        "#### Daily Lead Theme Candidates",
    ]

    if daily_lead_candidates:
        for item in daily_lead_candidates[:5]:
            lines.append(f"- candidate_theme: {item.get('candidate_theme', '')}")
            lines.append(f"  - main_category: {item.get('main_category', '')}")
            lines.append(f"  - related_tickers: {', '.join(item.get('related_tickers', []))}")
            lines.append(f"  - candidate_count: {item.get('candidate_count', 0)}")
            lines.append(f"  - statement_signal_count: {item.get('statement_signal_count', 0)}")
            lines.append(f"  - radar_score: {item.get('radar_score', 0)}")
            lines.append(f"  - body_verified: {item.get('body_verified', False)}")
            lines.append(f"  - use_as_script_evidence: {item.get('use_as_script_evidence', False)}")
            lines.append(f"  - needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
            lines.append(f"  - market_causality_confirmed: {item.get('market_causality_confirmed', False)}")
    else:
        lines.append("- none")

    lines.extend(["", "#### Statement Radar Status"])

    if isinstance(statement_status, dict) and statement_status:
        lines.append(f"- status: {statement_status.get('status', '')}")
        lines.append(f"- candidate_count: {statement_status.get('candidate_count', 0)}")
        lines.append(f"- telop_candidate_count: {statement_status.get('telop_candidate_count', 0)}")
        lines.append(f"- fulltext_candidate_count: {statement_status.get('fulltext_candidate_count', 0)}")
        lines.append(f"- top_speakers: {statement_status.get('top_speakers', [])}")
        lines.append(f"- top_topics: {statement_status.get('top_topics', [])}")
    else:
        lines.append("- none")

    lines.extend(["", "#### Statement Radar Candidates"])

    if statement_candidates:
        for item in statement_candidates[:10]:
            lines.append(f"- speaker_name: {item.get('speaker_name', '')}")
            lines.append(f"  - speaker_role_or_entity: {item.get('speaker_role_or_entity', '')}")
            lines.append(f"  - statement_topic: {item.get('statement_topic', '')}")
            lines.append(f"  - title: {item.get('title', '')}")
            lines.append(f"  - source: {item.get('source', '')}")
            lines.append(f"  - url: {item.get('url', '')}")
            lines.append(f"  - verification_status: {item.get('verification_status', '')}")
            lines.append(f"  - telop_candidate: {item.get('telop_candidate', False)}")
            lines.append(f"  - body_verified: {item.get('body_verified', False)}")
            lines.append(f"  - use_as_script_evidence: {item.get('use_as_script_evidence', False)}")
            lines.append(f"  - needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
            lines.append(f"  - market_causality_confirmed: {item.get('market_causality_confirmed', False)}")
    else:
        lines.append("- none")

    lines.extend(["", "#### Statement Fulltext Candidates"])

    if statement_fulltext_candidates:
        for item in statement_fulltext_candidates[:5]:
            lines.append(f"- speaker_name: {item.get('speaker_name', '')}")
            lines.append(f"  - speaker_role_or_entity: {item.get('speaker_role_or_entity', '')}")
            lines.append(f"  - statement_topic: {item.get('statement_topic', '')}")
            lines.append(f"  - title: {item.get('title', '')}")
            lines.append(f"  - source: {item.get('source', '')}")
            lines.append(f"  - url: {item.get('url', '')}")
            lines.append(f"  - telop_candidate: {item.get('telop_candidate', False)}")
            lines.append(f"  - verification_status: {item.get('verification_status', '')}")
            lines.append(f"  - body_verified: {item.get('body_verified', False)}")
            lines.append(f"  - use_as_script_evidence: {item.get('use_as_script_evidence', False)}")
            lines.append(f"  - needs_fulltext_before_script: {item.get('needs_fulltext_before_script', True)}")
            lines.append(f"  - market_causality_confirmed: {item.get('market_causality_confirmed', False)}")
    else:
        lines.append("- none")

    return lines
