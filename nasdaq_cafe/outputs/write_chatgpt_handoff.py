from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from nasdaq_cafe.outputs.submission_bundle import copy_submission_files

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


WATCHLIST_ORDER = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSM", "AMD", "TSLA"]
SEMI_TICKERS = {
    "NVDA",
    "AVGO",
    "TSM",
    "AMD",
    "ALAB",
    "TER",
    "INTC",
    "MRVL",
    "MU",
    "SNDK",
    "WDC",
    "AMAT",
    "LRCX",
    "KLAC",
    "ENTG",
    "MPWR",
    "GFS",
    "ON",
    "MCHP",
}
COMPANY_TICKERS = {
    "nvidia": "NVDA",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "broadcom": "AVGO",
    "tsmc": "TSM",
    "taiwan semiconductor": "TSM",
    "amd": "AMD",
    "advanced micro": "AMD",
    "tesla": "TSLA",
    "micron": "MU",
    "marvell": "MRVL",
    "intel": "INTC",
    "applied materials": "AMAT",
    "lam research": "LRCX",
    "kla": "KLAC",
    "rivian": "RIVN",
    "alibaba": "BABA",
}
NEWS_EXCLUDE_KEYWORDS = {
    "cotton",
    "corn",
    "wheat",
    "soybean",
    "soybeans",
    "cocoa",
    "coffee",
    "sugar",
    "cattle",
    "hogs",
    "crop",
    "grain",
    "family finance",
    "personal finance",
    "sports betting",
    "spacex",
    "nato",
    "defense",
    "oil investors",
    "strait of malacca",
}
NEWS_STRONG_TECH_KEYWORDS = {
    "nasdaq",
    "sox",
    "semiconductor",
    "semiconductors",
    "chip",
    "chips",
    "chipmaker",
    "chipmakers",
    "gpu",
    "memory",
    "data center",
    "datacenter",
    "cloud capex",
    "cloud expansion",
    "ai infrastructure",
    "ai chip",
    "ai chips",
    "nvidia",
    "amd",
    "tsm",
    "tsmc",
    "broadcom",
    "micron",
    "marvell",
    "microsoft",
    "amazon",
    "google",
    "alphabet",
    "meta",
    "samsung",
    "deepseek",
}


def write_chatgpt_handoff(output_dir: Path, pack: dict[str, Any]) -> None:
    content = render_chatgpt_handoff(pack)
    dated_copy = output_dir / f"CHATGPT_HANDOFF_{output_dir.name}.md"
    dated_copy.write_text(content, encoding="utf-8")

    daily_path = output_dir / "chatgpt_handoff.md"
    daily_path.write_text(content, encoding="utf-8")

    latest_dir = output_dir.parent / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    (latest_dir / "chatgpt_handoff.md").write_text(content, encoding="utf-8")

    write_readme_for_human(output_dir)
    copy_submission_files(output_dir)


def write_readme_for_human(output_dir: Path) -> None:
    target_date = output_dir.name
    lines = [
        "# 今日見るファイル",
        "",
        "## ChatGPTに送る正式ファイル",
        "",
        f"- CHATGPT_HANDOFF_{target_date}.md",
        "",
        "## 確認用ショートカット",
        "",
        "- output/latest/chatgpt_handoff.md",
        "",
        "## 本文入り一時handoff",
        "",
        f"- CHATGPT_FULLTEXT_HANDOFF_{target_date}.md",
        "- output/latest/chatgpt_fulltext_handoff.md",
        "",
        "## 普段は見なくてよいファイル",
        "",
        "- source_pack.md",
        "- source_pack.json",
        "- raw/",
        "- phase1_check.md",
        "- cleanup_report.md",
        "",
        "## 注意",
        "",
        "ChatGPTに送るのは日付付き大文字版です。",
        "latestは確認用です。",
        "Article Full Text Packageを使う場合だけ、本文入り一時handoffをChatGPTへ手動投入します。",
        "本文入り一時handoffはGmail送信や自動送信には使いません。",
        "",
    ]
    (output_dir / "README_FOR_HUMAN.md").write_text("\n".join(lines), encoding="utf-8")


def render_chatgpt_handoff(pack: dict[str, Any]) -> str:
    target_date = str(pack.get("date", "")).strip()
    market_session_date = _market_session_date(target_date)
    generated_at_jst, generated_at_utc = _generated_timestamps()

    watchlist = _ordered_watchlist(pack.get("watchlist", []))
    movers = _select_market_movers(pack.get("market_movers", []))
    economic_events = pack.get("economic_events", {})
    gdelt_radar_status = pack.get("gdelt_radar_status", {})
    gdelt_radar_candidates = pack.get("gdelt_radar_candidates", [])
    daily_lead_theme_candidates = pack.get("daily_lead_theme_candidates", [])
    statement_status = pack.get("statement_radar_status", {})
    statement_candidates = pack.get("statement_radar_candidates", [])
    statement_fulltext_candidates = pack.get("statement_fulltext_candidates", [])
    article_review_targets = pack.get("article_review_targets", {})
    core_drivers, context_candidates, low_value_items = _tier_news_items(pack.get("news_items", []))
    missing_lines = _missing_lines(pack, movers)
    notable_fixed = _select_fixed_notable(watchlist)
    notable_mover = movers[0] if movers else None

    lines = [
        "# 情報パック：朝のNASDAQカフェ",
        "",
        f"target_date_jst: {target_date}",
        f"market_session_date_us: {market_session_date}",
        f"generated_at_jst: {generated_at_jst}",
        f"generated_at_utc: {generated_at_utc}",
        "",
        "## 今日の市場データ",
        "",
        *_render_market_data(pack),
        "",
        "## 固定ウォッチ銘柄quote",
        "",
        *_render_watchlist(watchlist),
        "",
        "## Market Movers候補",
        "",
        *_render_market_movers(movers),
        "",
        "## 注目銘柄候補",
        "",
        "固定ウォッチリストから候補 1〜2件：",
        *_render_fixed_notables(notable_fixed),
        "",
        "Market Moversから候補 1件：",
        *_render_mover_notable(notable_mover),
        "",
        "## 経済イベント",
        "",
        *_render_economic_events(economic_events),
        "",
        "## GDELT Radar Status",
        "",
        *_render_gdelt_radar_status(
            gdelt_radar_status,
            gdelt_radar_candidates,
            daily_lead_theme_candidates,
            statement_status,
            statement_candidates,
            statement_fulltext_candidates,
        ),
        "",
        "## Article Review Targets",
        "",
        *_render_article_review_targets(article_review_targets),
        "",
        "## Core Drivers",
        "",
        *_render_news_items(core_drivers),
        "",
        "## Context Candidates",
        "",
        *_render_news_items(context_candidates),
        "",
        "## Excluded / Low Value Summary",
        "",
        *_render_excluded_summary(low_value_items),
        "",
        "## 未取得・注意情報",
        "",
        *missing_lines,
        "",
    ]
    return "\n".join(lines)


def _generated_timestamps() -> tuple[str, str]:
    jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
    now_utc = datetime.now(UTC).replace(microsecond=0)
    return now_utc.astimezone(jst).strftime("%Y-%m-%d %H:%M"), now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _market_session_date(target_date: str) -> str:
    try:
        return (datetime.fromisoformat(target_date).date() - timedelta(days=1)).isoformat()
    except ValueError:
        return ""


def _render_market_data(pack: dict[str, Any]) -> list[str]:
    lines: list[str] = ["取得済みデータのみ記載。未取得の指標は末尾の「未取得・注意情報」にまとめる。", ""]
    market_data = pack.get("market_data", {})
    labels = {
        "NASDAQ": "NASDAQ",
        "SOX": "SOX",
        "USDJPY": "ドル円 USDJPY",
        "QQQ": "QQQ",
        "SMH": "SMH",
        "SPY": "SPY",
        "XLK": "XLK",
        "VIX": "VIX",
        "US2Y": "米2年金利",
    }
    for key, label in labels.items():
        value = market_data.get(key)
        if value is not None:
            lines.append(f"{label}: {_format_compact(value)}")

    dgs10 = pack.get("macro", {}).get("DGS10")
    if dgs10:
        lines.extend(
            [
                "米10年金利 DGS10:",
                f"date: {_clean(dgs10.get('date'))}",
                f"value: {_clean(dgs10.get('value'))}",
                "note: FRED最新取得日の値であり、当日リアルタイム値ではない",
            ]
        )
    return lines


def _render_watchlist(watchlist: list[dict[str, Any]]) -> list[str]:
    if not watchlist:
        return ["取得済みquoteなし。"]

    lines: list[str] = []
    for item in watchlist:
        ticker = _clean(item.get("ticker"))
        lines.extend(
            [
                f"{ticker}:",
                f"price: {_clean(item.get('price'))}",
                f"change: {_clean(item.get('change'))}",
                f"change_percent: {_clean(item.get('change_percent'))}",
                f"session: {_clean(item.get('session'))}",
                f"source_symbol: {_clean(item.get('source_symbol'))}",
                f"brief_note: {_brief_quote_note(item)}",
                "",
            ]
        )
    return lines[:-1]


def _render_market_movers(movers: list[dict[str, Any]]) -> list[str]:
    if not movers:
        return ["Market Movers rawがない場合のみ、末尾の未取得・注意情報に記録。"]

    lines: list[str] = []
    for mover in movers:
        lines.extend(
            [
                f"{_clean(mover.get('ticker'))}:",
                f"name: {_clean(mover.get('name'))}",
                f"category: {_clean(mover.get('category'))}",
                f"price: {_clean(mover.get('price'))}",
                f"change_percent: {_clean(mover.get('change_percent'))}",
                f"volume_info: {_volume_info(mover)}",
                f"why_relevant: {_mover_relevance(mover)}",
                f"confidence: {_clean(mover.get('confidence'))}",
                f"source: {_clean(mover.get('source'))}",
                "",
            ]
        )
    return lines[:-1]


def _render_gdelt_radar_status(
    status: dict[str, Any],
    candidates: list[dict[str, Any]],
    daily_lead_candidates: list[dict[str, Any]],
    statement_status: dict[str, Any],
    statement_candidates: list[dict[str, Any]],
    statement_fulltext_candidates: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(status, dict) or not status:
        return ["GDELT Radar unavailable."]
    lines = [
        "GDELT is metadata-only candidate radar. It is not a Daily Lead Theme selection and contains no full article text.",
        "GDELT Radar metadata: body_unverified_candidate_urls_only.",
        "Statement Radar metadata: statement_candidates_unverified.",
        f"query_count: {_clean(status.get('query_count'))}",
        f"accepted_count: {_clean(status.get('accepted_count'))}",
        f"rejected_count: {_clean(status.get('rejected_count'))}",
        f"fulltext_candidate_count: {_clean(status.get('fulltext_candidate_count'))}",
        "",
        "top_categories:",
    ]
    top_categories = _top_gdelt_categories(status.get("categories", {}))
    if top_categories:
        for category, counts in top_categories:
            lines.append(
                f"- {category}: accepted={counts.get('accepted_count', 0)}, "
                f"fulltext_candidates={counts.get('fulltext_candidate_count', 0)}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "daily_lead_theme_candidates:"])
    if daily_lead_candidates:
        for item in daily_lead_candidates[:5]:
            lines.extend(
                [
                    "candidate_theme: " + _clean(item.get("candidate_theme")),
                    "main_category: " + _clean(item.get("main_category")),
                    "related_tickers: " + _join_clean(item.get("related_tickers", [])),
                    "candidate_count: " + _clean(item.get("candidate_count")),
                    "statement_signal_count: " + _clean(item.get("statement_signal_count")),
                    "radar_score: " + _clean(item.get("radar_score")),
                    "body_verified: " + _clean(item.get("body_verified")),
                    "use_as_script_evidence: " + _clean(item.get("use_as_script_evidence")),
                    "needs_fulltext_before_script: " + _clean(item.get("needs_fulltext_before_script")),
                    "market_causality_confirmed: " + _clean(item.get("market_causality_confirmed")),
                    "",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "statement_radar:"])
    if isinstance(statement_status, dict) and statement_status:
        lines.extend(
            [
                "candidate_count: " + _clean(statement_status.get("candidate_count")),
                "telop_candidate_count: " + _clean(statement_status.get("telop_candidate_count")),
                "statement_fulltext_candidates_count: " + _clean(len(statement_fulltext_candidates)),
                "top_speakers: " + _format_count_rows(statement_status.get("top_speakers", [])),
                "top_topics: " + _format_count_rows(statement_status.get("top_topics", [])),
            ]
        )
    else:
        lines.append("- none")
    lines.extend(["", "statement_candidates:"])
    if statement_candidates:
        for item in statement_candidates[:5]:
            lines.extend(
                [
                    "speaker_name: " + _clean(item.get("speaker_name")),
                    "speaker_role_or_entity: " + _clean(item.get("speaker_role_or_entity")),
                    "statement_topic: " + _clean(item.get("statement_topic")),
                    "title: " + _clean(item.get("title")),
                    "source: " + _clean(item.get("source")),
                    "url: " + _clean(item.get("url")),
                    "verification_status: " + _clean(item.get("verification_status")),
                    "telop_candidate: " + _clean(item.get("telop_candidate")),
                    "body_verified: " + _clean(item.get("body_verified")),
                    "use_as_script_evidence: " + _clean(item.get("use_as_script_evidence")),
                    "needs_fulltext_before_script: " + _clean(item.get("needs_fulltext_before_script")),
                    "market_causality_confirmed: " + _clean(item.get("market_causality_confirmed")),
                    "",
                ]
            )
        if lines and lines[-1] == "":
            lines.pop()
    else:
        lines.append("- none")
    lines.extend(["", "top_candidates:"])
    if candidates:
        for item in candidates[:5]:
            related = item.get("related_tickers", [])
            lines.extend(
                [
                    "title: " + _clean(item.get("title")),
                    "category: " + _clean(item.get("category")),
                    "source: " + _clean(item.get("source")),
                    "published_at: " + _clean(item.get("published_at")),
                    "seen_at: " + _clean(item.get("seen_at")),
                    "url: " + _clean(item.get("url")),
                    "related_tickers: " + (", ".join(_clean(ticker) for ticker in related if _clean(ticker)) if isinstance(related, list) else ""),
                    "radar_score: " + _clean(item.get("radar_score")),
                    "decision_reason: " + _clean(item.get("decision_reason")),
                    "fulltext_candidate: " + _clean(item.get("fulltext_candidate")),
                    "body_verified: " + _clean(item.get("body_verified")),
                    "use_as_script_evidence: " + _clean(item.get("use_as_script_evidence")),
                    "needs_fulltext_before_script: " + _clean(item.get("needs_fulltext_before_script")),
                    "market_causality_confirmed: " + _clean(item.get("market_causality_confirmed")),
                    "",
                ]
            )
        return lines[:-1]
    lines.append("- none")
    return lines


def _top_gdelt_categories(categories: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(categories, dict):
        return []
    rows = [(str(category), counts) for category, counts in categories.items() if isinstance(counts, dict)]
    rows.sort(
        key=lambda row: (
            int(row[1].get("accepted_count") or 0),
            int(row[1].get("fulltext_candidate_count") or 0),
            int(row[1].get("raw_count") or 0),
        ),
        reverse=True,
    )
    return rows[:5]


def _join_clean(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return ", ".join(_clean(value) for value in values if _clean(value))


def _format_count_rows(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    return ", ".join(
        f"{_clean(item.get('name'))}:{_clean(item.get('count'))}"
        for item in rows
        if isinstance(item, dict) and _clean(item.get("name"))
    )


def _render_economic_events(economic_events: dict[str, Any]) -> list[str]:
    if not economic_events:
        return ["### Past Events", "該当イベントなし。", "", "### Upcoming Events", "該当イベントなし。", "", "### Low Value Macro Summary", "- 取得済み候補なし"]

    lines: list[str] = ["### Past Events"]
    lines.extend(_render_economic_event_items(economic_events.get("past_events", [])[:5], include_actual=True))
    lines.extend(["", "### Upcoming Events"])
    lines.extend(_render_economic_event_items(economic_events.get("upcoming_events", [])[:5], include_actual=False))
    lines.extend(["", "### Low Value Macro Summary"])
    summary = economic_events.get("low_value_macro_summary", [])
    if summary:
        for item in summary:
            lines.append(f"- {item.get('source', '')}: count={item.get('count', '')}, reason={item.get('reason', '')}")
    else:
        lines.append("- 低価値マクロ候補なし")
    return lines


def _render_economic_event_items(items: list[dict[str, Any]], include_actual: bool) -> list[str]:
    if not items:
        return ["該当イベントなし。"]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                "event_name: " + _clean(item.get("event_name")),
                "event_date: " + _clean(item.get("event_date")),
                "event_time: " + _clean(item.get("event_time")),
                "event_window: " + _clean(item.get("event_window")),
                "importance: " + _clean(item.get("importance")),
                "event_impact_score: " + _clean(item.get("event_impact_score")),
            ]
        )
        if include_actual:
            lines.extend(
                [
                    "actual: " + _clean(item.get("actual")),
                    "forecast: " + _clean(item.get("forecast")),
                    "previous: " + _clean(item.get("previous")),
                ]
            )
        lines.extend(
            [
                "market_expectation: " + _clean(item.get("market_expectation")),
                "why_viewer_should_care: " + _clean(item.get("why_viewer_should_care")),
                "driver_type: " + _clean(item.get("driver_type")),
                "causal_bridge: " + _clean(item.get("causal_bridge")),
                "filter_reason: " + _clean(item.get("filter_reason")),
                "confidence: " + _clean(item.get("confidence")),
                "source: " + _clean(item.get("source")),
                "url: " + _clean(item.get("url")),
                "",
            ]
        )
    return lines[:-1]


def _render_fixed_notables(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- 取得済みquoteからの候補なし / unknown"]
    return [f"- {_clean(item.get('ticker'))}: {_fixed_reason(item)} / {_confidence_from_quote(item)}" for item in items]


def _render_mover_notable(mover: dict[str, Any] | None) -> list[str]:
    if not mover:
        return ["- Market Movers候補なし / unknown"]
    return [f"- {_clean(mover.get('ticker'))}: {_mover_relevance(mover)} / {_clean(mover.get('confidence'))}"]


def _render_news_items(news_items: list[dict[str, Any]]) -> list[str]:
    if not news_items:
        return ["該当ニュース候補なし。"]

    lines: list[str] = []
    for item in news_items:
        related = _related_tickers(item)
        lines.extend(
            [
                "title: " + _clean(item.get("title")),
                "source: " + _clean(item.get("source")),
                "published_at: " + _clean(item.get("published_at")),
                "url: " + _clean(item.get("url")),
                "snippet: " + _short_snippet(item.get("snippet") or item.get("title")),
                "related_tickers: " + (", ".join(related) if related else ""),
                "why_relevant: " + _news_relevance(item),
                "confidence: " + _clean(item.get("confidence")),
                "relevance_score: " + _clean(item.get("relevance_score")),
                "driver_type: " + _clean(item.get("driver_type")),
                "causal_bridge: " + _clean(item.get("causal_bridge")),
                "filter_reason: " + _clean(item.get("filter_reason")),
                "",
            ]
        )
    return lines[:-1]


def _render_article_review_targets(targets: dict[str, Any]) -> list[str]:
    if not isinstance(targets, dict):
        return [
            "### Must Review",
            "該当記事なし。",
            "",
            "### Optional Review",
            "該当記事なし。",
            "",
            "### Usually Do Not Review",
            "- Excluded / Low Value Summary は通常確認不要。",
        ]

    lines: list[str] = ["### Must Review"]
    lines.extend(_render_review_target_items(targets.get("must_review", [])))
    lines.extend(["", "### Optional Review"])
    lines.extend(_render_review_target_items(targets.get("optional_review", [])))
    lines.extend(["", "### Usually Do Not Review"])
    usually = targets.get("usually_do_not_review", [])
    if usually:
        for item in usually:
            lines.append("- title: " + _clean(item.get("title")))
            lines.append("  source: " + _clean(item.get("source")))
            lines.append("  reason_to_review: " + _clean(item.get("reason_to_review")))
            lines.append("  expected_use: " + _clean(item.get("expected_use")))
    else:
        lines.append("- none")
    return lines


def _render_review_target_items(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["該当記事なし。"]
    lines: list[str] = []
    for item in items:
        related = [_clean(ticker) for ticker in item.get("related_tickers", []) if _clean(ticker)]
        lines.extend(
            [
                "title: " + _clean(item.get("title")),
                "source: " + _clean(item.get("source")),
                "url: " + _clean(item.get("url")),
                "reason_to_review: " + _clean(item.get("reason_to_review")),
                "expected_use: " + _clean(item.get("expected_use")),
                "related_tickers: " + (", ".join(related) if related else ""),
                "",
            ]
        )
    return lines[:-1]


def _render_excluded_summary(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- 低関連ニュースなし。"]
    counts: dict[str, int] = {}
    for item in items:
        reason = _excluded_bucket(item)
        counts[reason] = counts.get(reason, 0) + 1
    return [f"- {reason}: {count}件" for reason, count in sorted(counts.items(), key=lambda row: row[0])]


def _excluded_bucket(item: dict[str, Any]) -> str:
    text = _item_text(item)
    if _has_noise_keyword(text):
        return "commodity agriculture / personal finance / unrelated noise. NASDAQ・半導体・AI株との因果の橋が弱いため除外"
    reason = _clean(item.get("filter_reason"))
    if "earnings" in reason.lower():
        return "unrelated earnings. 固定ウォッチリスト・Market Movers・大型テックとの直接関係が弱いため除外"
    if "causal bridge" in reason.lower() or not _clean(item.get("causal_bridge")):
        return "weak causal bridge. 主材料として使うには因果の橋が弱いため除外"
    return reason or "low explanatory value. 朝のNASDAQカフェの主材料として低関連"


def _tier_news_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    valid = [item for item in items if _has_required_news_fields(item) and not _is_blocked_news_url(item.get("url"))]
    core = sorted(
        [item for item in valid if item.get("driver_type") == "core_driver"],
        key=lambda item: (item.get("selected_for_handoff") is True, item.get("relevance_score", 0), _clean(item.get("published_at"))),
        reverse=True,
    )[:8]
    context = sorted(
        [item for item in valid if item.get("driver_type") == "context_candidate"],
        key=lambda item: (item.get("selected_for_handoff") is True, item.get("relevance_score", 0), _clean(item.get("published_at"))),
        reverse=True,
    )[:10]
    low = [item for item in items if item.get("driver_type") == "low_value_or_irrelevant" or item not in valid]
    return core, context, low


def _has_required_news_fields(item: dict[str, Any]) -> bool:
    return bool(item.get("title") and item.get("source") and item.get("published_at") and item.get("url") and (item.get("snippet") or item.get("title")))


def _missing_lines(pack: dict[str, Any], movers: list[dict[str, Any]]) -> list[str]:
    lines = []
    market_data = pack.get("market_data", {})
    for key, label in [
        ("NASDAQ", "NASDAQ総合指数"),
        ("SOX", "SOX指数"),
        ("USDJPY", "ドル円"),
        ("QQQ", "QQQ"),
        ("SMH", "SMH"),
        ("SPY", "SPY"),
        ("XLK", "XLK"),
        ("VIX", "VIX"),
        ("US2Y", "米2年金利"),
    ]:
        if market_data.get(key) is None:
            lines.append(f"- {label}は未取得")

    if pack.get("macro", {}).get("DGS10"):
        lines.append("- 米10年金利はFRED最新取得日の値であり、当日リアルタイム値ではない")
    else:
        lines.append("- 米10年金利 DGS10 は未取得")

    for item in pack.get("missing_data", []):
        source = _clean(item.get("source"))
        reason = _clean(item.get("reason"))
        severity = _clean(item.get("severity"))
        if movers and source == "Market Movers" and "Market Movers" in reason and "Premarket" not in reason and "After Hours" not in reason:
            continue
        if movers and source == "Longbridge" and "Market movers raw JSON is missing" in reason:
            reason = "Longbridge個別raw market_moversは未投入（統合Market Movers候補は取得済み）"
        lines.append(f"- {source}: {reason} ({severity})")

    return _dedupe_preserve_order(lines) or ["- なし"]


def _ordered_watchlist(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {_clean(item.get("ticker")): item for item in items}
    ordered: list[dict[str, Any]] = []
    for ticker in WATCHLIST_ORDER:
        item = dict(by_ticker.get(ticker, {"ticker": ticker, "source_symbol": f"{ticker}.US"}))
        ordered.append(item)
    return ordered


def _select_fixed_notable(watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    quoted = [item for item in watchlist if _to_float(item.get("change_percent")) is not None]
    quoted.sort(key=lambda item: abs(_to_float(item.get("change_percent")) or 0), reverse=True)
    return quoted[:2]


def _select_market_movers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [item for item in items if _include_mover(item)]
    filtered.sort(key=_mover_score, reverse=True)
    return filtered[:10]


def _include_mover(item: dict[str, Any]) -> bool:
    ticker = _clean(item.get("ticker")).upper().replace(".US", "")
    price = _to_float(item.get("price"))
    if price is not None and price < 5:
        return False
    text = _item_text(item)
    if _has_noise_keyword(text):
        return False
    if ticker in SEMI_TICKERS:
        return True
    return _has_strong_tech_keyword(text) or _is_large_cap(item) or "nasdaq 100" in text or "sox component" in text


def _mover_score(item: dict[str, Any]) -> float:
    ticker = _clean(item.get("ticker")).upper().replace(".US", "")
    category = _clean(item.get("category")).lower()
    text = _item_text(item)
    score = abs(_to_float(item.get("change_percent")) or 0)
    if "sox component" in category:
        score += 90
    if "nasdaq 100" in category:
        score += 80
    if ticker in SEMI_TICKERS:
        score += 70
    if _has_strong_tech_keyword(text):
        score += 40
    if _is_large_cap(item):
        score += 20
    return score


def _select_news_items(items: list[dict[str, Any]], movers: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    selected = [item for item in items if _include_news(item, target_date)]
    mover_tickers = {_clean(mover.get("ticker")).upper().replace(".US", "") for mover in movers}
    selected.sort(key=lambda item: _news_score(item, mover_tickers), reverse=True)
    return selected[:12]


def _include_news(item: dict[str, Any], target_date: str) -> bool:
    if not item.get("published_at") or not item.get("url"):
        return False
    if not (item.get("snippet") or item.get("title")):
        return False
    if not _is_recent_news_date(item.get("published_at"), target_date):
        return False
    if _is_blocked_news_url(item.get("url")):
        return False
    text = _item_text(item)
    if _has_noise_keyword(text) and not _has_strong_tech_keyword(text):
        return False
    return _has_strong_tech_keyword(text) or bool(_related_tickers(item)) or "market: nasdaq" in text or "theme: semiconductors" in text


def _is_blocked_news_url(value: Any) -> bool:
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


def _is_recent_news_date(value: Any, target_date: str = "") -> bool:
    published = _parse_news_date(value)
    if published is None:
        return False
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else datetime.now(timezone.utc).date()
    except ValueError:
        target = datetime.now(timezone.utc).date()
    return target - timedelta(days=30) <= published <= target + timedelta(days=1)


def _parse_news_date(value: Any):
    text = _clean(value)
    if not text:
        return None
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
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


def _news_score(item: dict[str, Any], mover_tickers: set[str]) -> float:
    text = _item_text(item)
    score = 0.0
    related = set(_related_tickers(item))
    if related.intersection(WATCHLIST_ORDER):
        score += 80
    if related.intersection(mover_tickers):
        score += 70
    if "nasdaq" in text or "sox" in text:
        score += 50
    if "semiconductor" in text or "chip" in text or "memory" in text or "gpu" in text:
        score += 45
    if "data center" in text or "cloud" in text or "bond" in text:
        score += 30
    source_rank = {"Yahoo Finance": 8, "MarketWatch": 7, "NASDAQ": 7, "CNBC": 6, "SerpAPI": 3}
    score += source_rank.get(_clean(item.get("source")), 0)
    return score


def _brief_quote_note(item: dict[str, Any]) -> str:
    ticker = _clean(item.get("ticker")).upper()
    change = _to_float(item.get("change_percent"))
    if change is None:
        return "数値未取得"
    if abs(change) < 0.1:
        return "横ばい"
    if ticker in {"NVDA", "AVGO", "TSM", "AMD"} and change <= -3:
        return "半導体関連として下落が目立つ"
    if ticker == "TSLA" and change <= -3:
        return "EV関連として弱い"
    if change >= 3:
        return "大型テックの中で上昇が目立つ"
    if change > 0:
        return "小幅上昇"
    return "小幅下落"


def _fixed_reason(item: dict[str, Any]) -> str:
    change = _to_float(item.get("change_percent"))
    if change is None:
        return "固定ウォッチ銘柄だが数値未取得"
    return f"固定ウォッチ銘柄内で変動が目立つ（change_percent={change:.2f}）。個別の明確な材料は確認できていません"


def _confidence_from_quote(item: dict[str, Any]) -> str:
    return "medium" if _to_float(item.get("change_percent")) is not None else "unknown"


def _volume_info(item: dict[str, Any]) -> str:
    volume = _to_float(item.get("volume"))
    average = _to_float(item.get("average_volume"))
    if volume is None and average is None:
        return "未取得"
    if volume is not None and average:
        return f"volume={volume:.0f}, average_volume={average:.0f}, ratio={volume / average:.2f}"
    if volume is not None:
        return f"volume={volume:.0f}"
    return f"average_volume={average:.0f}"


def _mover_relevance(item: dict[str, Any]) -> str:
    ticker = _clean(item.get("ticker")).upper().replace(".US", "")
    category = _clean(item.get("category"))
    reason = _clean(item.get("reason"))
    if ticker in SEMI_TICKERS or "SOX" in category:
        return f"{category}の半導体関連候補。{reason}"
    if "Nasdaq 100" in category:
        return f"Nasdaq 100関連のMarket Movers候補。{reason}"
    if _has_strong_tech_keyword(_item_text(item)):
        return f"AI/クラウド/大型テック関連のMarket Movers候補。{reason}"
    return reason or "Market Movers候補。明確な個別材料は確認できていません"


def _news_relevance(item: dict[str, Any]) -> str:
    text = _item_text(item)
    existing = _clean(item.get("why_relevant"))
    related = _related_tickers(item)
    if "semiconductor" in text or "chip" in text or "memory" in text or "gpu" in text or "samsung" in text:
        return "半導体/AIインフラ関連ニュースとしてNASDAQ・SOXの地合い確認に有用"
    if "nasdaq" in text:
        return "NASDAQ指数と大型テックの地合い材料"
    if "cloud" in text or "data center" in text or "bond" in text:
        return "AI・クラウド投資や資金調達の流れを確認する材料"
    if related:
        return f"関連銘柄候補: {', '.join(related)}"
    return existing or "ニュース材料として確認対象"


def _related_tickers(item: dict[str, Any]) -> list[str]:
    related = [_clean(ticker).upper().replace(".US", "") for ticker in item.get("related_tickers", []) if _clean(ticker)]
    raw_text = _item_raw_text(item)
    for ticker in WATCHLIST_ORDER + sorted(SEMI_TICKERS):
        if _mentions_ticker(raw_text, ticker) and ticker not in related:
            related.append(ticker)
    return _dedupe_preserve_order([ticker for ticker in related if ticker])


def _mentions_ticker(text: str, ticker: str) -> bool:
    pattern = rf"(?<![A-Z0-9])\$?{re.escape(ticker)}(?:\.US)?(?![A-Z0-9])"
    return re.search(pattern, text) is not None


def _has_noise_keyword(text: str) -> bool:
    return any(keyword in text for keyword in NEWS_EXCLUDE_KEYWORDS)


def _has_strong_tech_keyword(text: str) -> bool:
    return any(keyword in text for keyword in NEWS_STRONG_TECH_KEYWORDS)


def _is_large_cap(item: dict[str, Any]) -> bool:
    market_cap = _to_float(item.get("market_cap"))
    return bool(market_cap and market_cap >= 10_000_000_000)


def _item_text(item: dict[str, Any]) -> str:
    return _item_raw_text(item).lower()


def _item_raw_text(item: dict[str, Any]) -> str:
    values = [
        item.get("ticker"),
        item.get("name"),
        item.get("category"),
        item.get("reason"),
        item.get("title"),
        item.get("snippet"),
        item.get("why_relevant"),
        item.get("url"),
    ]
    return " ".join(_clean(value) for value in values if value is not None)


def _format_compact(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={_clean(val)}" for key, val in value.items())
    return _clean(value)


def _short_snippet(value: Any, limit: int = 220) -> str:
    text = " ".join(_clean(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", ""))
    except ValueError:
        return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        if item not in seen:
            output.append(item)
            seen.add(item)
    return output
