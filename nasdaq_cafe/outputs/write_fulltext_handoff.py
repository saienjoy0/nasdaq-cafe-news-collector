from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nasdaq_cafe.outputs.submission_bundle import copy_submission_files
from nasdaq_cafe.outputs.write_chatgpt_handoff import render_chatgpt_handoff

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


def write_chatgpt_fulltext_handoff(
    output_dir: Path,
    pack: dict[str, Any],
    article_fulltext: dict[str, Any],
) -> None:
    content = render_chatgpt_fulltext_handoff(pack, article_fulltext)
    dated_copy = output_dir / f"CHATGPT_FULLTEXT_HANDOFF_{output_dir.name}.md"
    dated_copy.write_text(content, encoding="utf-8")

    latest_dir = output_dir.parent / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    (latest_dir / "chatgpt_fulltext_handoff.md").write_text(content, encoding="utf-8")
    copy_submission_files(output_dir)


def render_chatgpt_fulltext_handoff(
    pack: dict[str, Any],
    article_fulltext: dict[str, Any],
) -> str:
    target_date = str(pack.get("date", "")).strip()
    market_session_date = str(pack.get("researchTradingDate", "")).strip()
    generated_at_jst, generated_at_utc = _generated_timestamps()
    summary = article_fulltext.get("summary", {}) if isinstance(article_fulltext, dict) else {}
    items = article_fulltext.get("items", []) if isinstance(article_fulltext, dict) else []
    unreadable = article_fulltext.get("unreadable", []) if isinstance(article_fulltext, dict) else []

    lines = [
        "# Article Full Text Package",
        "",
        f"target_date_jst: {target_date}",
        f"market_session_date_us: {market_session_date}",
        f"generated_at_jst: {generated_at_jst}",
        f"generated_at_utc: {generated_at_utc}",
        f"manifest: output/{target_date}/raw/manifest.json",
        f"normal_handoff_safe_copy: output/{target_date}/CHATGPT_HANDOFF_{target_date}.md",
        "",
        "重要:",
        "- Codexは本文の取得・抽出・保存だけを行い、要約・解釈・因果判断は行っていません。",
        "- relevance、review priority、handoff選定に関係なく、manifest登録URLを全文取得対象にしています。",
        "- paywall、login、CAPTCHAは回避していません。失敗も正常な取得結果として記録しています。",
        "",
        "## Package Summary",
        "",
        f"- target_count: {summary.get('target_count', 0)}",
        f"- attempted_count: {summary.get('attempted_count', 0)}",
        f"- complete_count: {summary.get('complete_count', 0)}",
        f"- failed_count: {summary.get('failed_count', 0)}",
        f"- excluded_count: {summary.get('excluded_count', 0)}",
        f"- not_attempted_limit_count: {summary.get('not_attempted_limit_count', 0)}",
        f"- total_full_text_chars: {summary.get('total_full_text_chars', 0)}",
        f"- tavily_attempted_count: {summary.get('tavily_attempted_count', 0)}",
        f"- tavily_success_count: {summary.get('tavily_success_count', 0)}",
        "",
        "## All Acquired Full Text",
        "",
        *_render_fulltext_items(items if isinstance(items, list) else []),
        "",
        "## Failed / Excluded / Not Attempted by Configured Limit",
        "",
        *_render_unreadable(unreadable if isinstance(unreadable, list) else []),
        "",
        "## Regular Handoff Context",
        "",
        render_chatgpt_handoff(pack),
    ]
    return "\n".join(lines)


def _render_fulltext_items(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["該当記事なし。"]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"### Article {index}",
                "",
                f"- article_id: {_clean(item.get('article_id'))}",
                f"- title: {_clean(item.get('title'))}",
                f"- source: {_clean(item.get('source'))}",
                f"- published_at: {_clean(item.get('published_at'))}",
                f"- primary_url: {_clean(item.get('primary_url'))}",
                f"- read_url: {_clean(item.get('read_url'))}",
                f"- access_status: {_clean(item.get('access_status'))}",
                f"- raw_file: {_clean(item.get('raw_file'))}",
                f"- extracted_text_file: {_clean(item.get('extracted_text_file'))}",
                f"- content_hash: {_clean(item.get('content_hash'))}",
                f"- retrieval_routes: {_format_list(item.get('retrieval_routes'))}",
                "",
                "full_text:",
                "",
                "```text",
                _verbatim(item.get("full_text")),
                "```",
                "",
            ]
        )
    return lines[:-1]


def _render_unreadable(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["該当記事なし。"]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                f"- article_id: {_clean(item.get('article_id'))}",
                f"  - title: {_clean(item.get('title'))}",
                f"  - source: {_clean(item.get('source'))}",
                f"  - primary_url: {_clean(item.get('primary_url'))}",
                f"  - fulltext_status: {_clean(item.get('fulltext_status'))}",
                f"  - access_status: {_clean(item.get('access_status'))}",
                f"  - reason: {_clean(item.get('reason'))}",
            ]
        )
    return lines


def _generated_timestamps() -> tuple[str, str]:
    jst = ZoneInfo("Asia/Tokyo") if ZoneInfo else timezone(timedelta(hours=9))
    now_utc = datetime.now(UTC).replace(microsecond=0)
    return now_utc.astimezone(jst).isoformat(), now_utc.isoformat()


def _format_list(value: Any) -> str:
    if not isinstance(value, list):
        return "[]"
    return "[" + ", ".join(_clean(item) for item in value) + "]"


def _verbatim(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
