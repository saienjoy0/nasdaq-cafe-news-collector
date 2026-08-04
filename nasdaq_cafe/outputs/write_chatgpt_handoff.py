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
        "# ä»Šæ—¥è¦‹ã‚‹ãƒ•ã‚¡ã‚¤ãƒ«",
        "",
        "## ChatGPTã«é€ã‚‹æ­£å¼ãƒ•ã‚¡ã‚¤ãƒ«",
        "",
        f"- CHATGPT_HANDOFF_{target_date}.md",
        "",
        "## ç¢ºèªç”¨ã‚·ãƒ§ãƒ¼ãƒˆã‚«ãƒƒãƒˆ",
        "",
        "- output/latest/chatgpt_handoff.md",
        "",
        "## æœ¬æ–‡å…¥ã‚Šä¸€æ™‚handoff",
        "",
        f"- CHATGPT_FULLTEXT_HANDOFF_{target_date}.md",
        "- output/latest/chatgpt_fulltext_handoff.md",
        "",
        "## æ™®æ®µã¯è¦‹ãªãã¦ã‚ˆã„ãƒ•ã‚¡ã‚¤ãƒ«",
        "",
        "- source_pack.md",
        "- source_pack.json",
        "- raw/",
        "- phase1_check.md",
        "- cleanup_report.md",
        "",
        "## æ³¨æ„",
        "",
        "ChatGPTã«é€ã‚‹ã®ã¯æ—¥ä»˜ä»˜ãå¤§æ–‡å­—ç‰ˆã§ã™ã€‚",
        "latestã¯ç¢ºèªç”¨ã§ã™ã€‚",
        "Article Full Text Packageã‚’ä½¿ã†å ´åˆã ã‘ã€æœ¬æ–‡å…¥ã‚Šä¸€æ™‚handoffã‚’ChatGPTã¸æ‰‹å‹•æŠ•å…¥ã—ã¾ã™ã€‚",
        "æœ¬æ–‡å…¥ã‚Šä¸€æ™‚handoffã¯Gmailé€ä¿¡ã‚„è‡ªå‹•é€ä¿¡ã«ã¯ä½¿ã„ã¾ã›ã‚“ã€‚",
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
        "# æƒ…å ±ãƒ‘ãƒƒã‚¯ï¼šæœã®NASDAQã‚«ãƒ•ã‚§",
        "",
        f"target_date_jst: {target_date}",
        f"market_session_date_us: {market_session_date}",
        f"generated_at_jst: {generated_at_jst}",
        f"generated_at_utc: {generated_at_utc}",
        "",
        "## ä»Šæ—¥ã®å¸‚å ´ãƒ‡ãƒ¼ã‚¿",
        "",
        *_render_market_data(pack),
        "",
        "## å›ºå®šã‚¦ã‚©ãƒƒãƒéŠ˜æŸ„quote",
        "",
        *_render_watchlist(watchlist),
        "",
        "## Market Moverså€™è£œ",
        "",
        *_render_market_movers(movers),
        "",
        "## æ³¨ç›®éŠ˜æŸ„å€™è£œ",
        "",
        "å›ºå®šã‚¦ã‚©ãƒƒãƒãƒªã‚¹ãƒˆã‹ã‚‰å€™è£œ 1ã€œ2ä»¶ï¼š",
        *_render_fixed_notables(notable_fixed),
        "",
        "Market Moversã‹ã‚‰å€™è£œ 1ä»¶ï¼š",
        *_render_mover_notable(notable_mover),
        "",
        "## çµŒæ¸ˆã‚¤ãƒ™ãƒ³ãƒˆ",
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
        "## æœªå–å¾—ãƒ»æ³¨æ„æƒ…å ±",
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
    lines: list[str] = ["å–å¾—æ¸ˆã¿ãƒ‡ãƒ¼ã‚¿ã®ã¿è¨˜è¼‰ã€‚æœªå–å¾—ã®æŒ‡æ¨™ã¯æœ«å°¾ã®ã€Œæœªå–å¾—ãƒ»æ³¨æ„æƒ…å ±ã€ã«ã¾ã¨ã‚ã‚‹ã€‚", ""]
    market_data = pack.get("market_data", {})
    labels = {
        "NASDAQ": "NASDAQ",
        "SOX": "SOX",
        "USDJPY": "ãƒ‰ãƒ«å†† USDJPY",
        "QQQ": "QQQ",
        "SMH": "SMH",
        "SPY": "SPY",
        "XLK": "XLK",
        "VIX": "VIX",
        "US2Y": "ç±³2å¹´é‡‘åˆ©",
    }
    for key, label in labels.items():
        value = market_data.get(key)
        if value is not None:
            lines.append(f"{label}: {_format_compact(value)}")

    dgs10 = pack.get("macro", {}).get("DGS10")
    if dgs10:
        lines.extend(
            [
                "ç±³10å¹´é‡‘åˆ© DGS10:",
                f"date: {_clean(dgs10.get('date'))}",
                f"value: {_clean(dgs10.get('value'))}",
                "note: FREDæœ€æ–°å–å¾—æ—¥ã®å€¤ã§ã‚ã‚Šã€å½“æ—¥ãƒªã‚¢ãƒ«ã‚¿ã‚¤ãƒ å€¤ã§ã¯ãªã„",
            ]
        )
    return lines


def _render_watchlist(watchlist: list[dict[str, Any]]) -> list[str]:
    if not watchlist:
        return ["å–å¾—æ¸ˆã¿quoteãªã—ã€‚"]

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
        return ["Market Movers rawãŒãªã„å ´åˆã®ã¿ã€æœ«å°¾ã®æœªå–å¾—ãƒ»æ³¨æ„æƒ…å ±ã«è¨˜éŒ²ã€‚"]

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
                    "source: " + _cÛ®v¶‰žËkºwµçq•…¸¡¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¤¤°(€€€€€€€€€€€€€€€€‰ÕÉ°è€ˆ€¬}±•…¸¡¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤¤°(€€€€€€€€€€€€€€€€‰É•…Í½¹}Ñ½}É•Ù¥•Üè€ˆ€¬}±•…¸¡¥Ñ•´¹•Ð ‰É•…Í½¹}Ñ½}É•Ù¥•Üˆ¤¤°(€€€€€€€€€€€€€€€€‰•áÁ•Ñ•‘}ÕÍ”è€ˆ€¬}±•…¸¡¥Ñ•´¹•Ð ‰•áÁ•Ñ•‘}ÕÍ”ˆ¤¤°(€€€€€€€€€€€€€€€€‰É•±…Ñ•‘}Ñ¥­•ÉÌè€ˆ€¬€ ˆ°€ˆ¹©½¥¸¡É•±…Ñ•¤¥˜É•±…Ñ••±Í”€ˆˆ¤°(€€€€€€€€€€€€€€€€ˆˆ°(€€€€€€€€€€€t(€€€€€€€€¤(€€€É•ÑÕÉ¸±¥¹•Ílè´Åt(()‘•˜}É•¹‘•É}•á±Õ‘•‘}ÍÕµµ…Éä¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑmÍÑÉtè(€€€¥˜¹½Ð¥Ñ•µÌè(€€€€€€€É•ÑÕÉ¸lˆ´ƒ’ö;¦Z‹¦Ž/Ž—ŽóŽ
çŽ«Ž_Ž‰t(€€€½Õ¹ÑÌè‘¥ÑmÍÑÈ°¥¹Ñt€ôíô(€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€É•…Í½¸€ô}•á±Õ‘•‘}‰Õ­•Ð¡¥Ñ•´¤(€€€€€€€½Õ¹ÑÍmÉ•…Í½¹t€ô½Õ¹ÑÌ¹•Ð¡É•…Í½¸°€À¤€¬€Ä(€€€É•ÑÕÉ¸m˜ˆ´íÉ•…Í½¹ôèí½Õ¹Ñ÷’îØˆ™½ÈÉ•…Í½¸°½Õ¹Ð¥¸Í½ÉÑ•¡½Õ¹ÑÌ¹¥Ñ•µÌ ¤°­•äõ±…µ‰‘„É½ÜèÉ½ÝlÁt¥t(()‘•˜}•á±Õ‘•‘}‰Õ­•Ð¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€¥˜}¡…Í}¹½¥Í•}­•åÝ½É¡Ñ•áÐ¤è(€€€€€€€É•ÑÕÉ¸€‰½µµ½‘¥Ñä…É¥Õ±ÑÕÉ”€¼Á•ÉÍ½¹…°™¥¹…¹”€¼Õ¹É•±…Ñ•¹½¥Í”¸9MGŽï–6+–Â;’öOŽí'š‚«Ž£Ž»–nƒšzsŽ»š¦/Ž3–òÇŽŽŽ
¦f“–’Xˆ(€€€É•…Í½¸€ô}±•…¸¡¥Ñ•´¹•Ð ‰™¥±Ñ•É}É•…Í½¸ˆ¤¤(€€€¥˜€‰•…É¹¥¹Ìˆ¥¸É•…Í½¸¹±½Ý•È ¤è(€€€€€€€É•ÑÕÉ¸€‰Õ¹É•±…Ñ••…É¹¥¹Ì¸ƒ–në–ºkŽ
›Ž
§ŽŽŽ«Ž
çŽ#Ží5…É­•Ð5½Ù•ÉÏŽï–’Ÿ–z/ŽŽŽ
¿Ž£Ž»žnÓš:—¦Z‹’þŽ3–òÇŽŽŽ
¦f“–’Xˆ(€€€¥˜€‰…ÕÍ…°‰É¥‘”ˆ¥¸É•…Í½¸¹±½Ý•È ¤½È¹½Ð}±•…¸¡¥Ñ•´¹•Ð ‰…ÕÍ…±}‰É¥‘”ˆ¤¤è(€€€€€€€É•ÑÕÉ¸€‰Ý•…¬…ÕÍ…°‰É¥‘”¸ƒ’âïšvCšZgŽ£Ž_Ž›’öÿŽŽ¯Ž¿–nƒšzsŽ»š¦/Ž3–òÇŽŽŽ
¦f“–’Xˆ(€€€É•ÑÕÉ¸É•…Í½¸½È€‰±½Ü•áÁ±…¹…Ñ½ÉäÙ…±Õ”¸ƒšrwŽ¹9MGŽ
¯ŽWŽ
ŸŽ»’âïšvCšZgŽ£Ž_Ž›’ö;¦Z‹¦Œˆ(()‘•˜}Ñ¥•É}¹•ÝÍ}¥Ñ•µÌ¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´øÑÕÁ±•m±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°±¥ÍÑm‘¥ÑmÍÑÈ°¹åuutè(€€€Ù…±¥€ôm¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜}¡…Í}É•ÅÕ¥É•‘}¹•ÝÍ}™¥•±‘Ì¡¥Ñ•´¤…¹¹½Ð}¥Í}‰±½­•‘}¹•ÝÍ}ÕÉ°¡¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤¥t(€€€½É”€ôÍ½ÉÑ• (€€€€€€€m¥Ñ•´™½È¥Ñ•´¥¸Ù…±¥¥˜¥Ñ•´¹•Ð ‰‘É¥Ù•É}ÑåÁ”ˆ¤€ôô€‰½É•}‘É¥Ù•È‰t°(€€€€€€€­•äõ±…µ‰‘„¥Ñ•´è€¡¥Ñ•´¹•Ð ‰Í•±•Ñ•‘}™½É}¡…¹‘½™˜ˆ¤¥ÌQÉÕ”°¥Ñ•´¹•Ð ‰É•±•Ù…¹•}Í½É”ˆ°€À¤°}±•…¸¡¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ¤¤¤°(€€€€€€€É•Ù•ÉÍ”õQÉÕ”°(€€€€¥lèát(€€€½¹Ñ•áÐ€ôÍ½ÉÑ• (€€€€€€€m¥Ñ•´™½È¥Ñ•´¥¸Ù…±¥¥˜¥Ñ•´¹•Ð ‰‘É¥Ù•É}ÑåÁ”ˆ¤€ôô€‰½¹Ñ•áÑ}…¹‘¥‘…Ñ”‰t°(€€€€€€€­•äõ±…µ‰‘„¥Ñ•´è€¡¥Ñ•´¹•Ð ‰Í•±•Ñ•‘}™½É}¡…¹‘½™˜ˆ¤¥ÌQÉÕ”°¥Ñ•´¹•Ð ‰É•±•Ù…¹•}Í½É”ˆ°€À¤°}±•…¸¡¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ¤¤¤°(€€€€€€€É•Ù•ÉÍ”õQÉÕ”°(€€€€¥lèÄÁt(€€€±½Ü€ôm¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜¥Ñ•´¹•Ð ‰‘É¥Ù•É}ÑåÁ”ˆ¤€ôô€‰±½Ý}Ù…±Õ•}½É}¥ÉÉ•±•Ù…¹Ðˆ½È¥Ñ•´¹½Ð¥¸Ù…±¥‘t(€€€É•ÑÕÉ¸½É”°½¹Ñ•áÐ°±½Ü(()‘•˜}¡…Í}É•ÅÕ¥É•‘}¹•ÝÍ}™¥•±‘Ì¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø‰½½°è(€€€É•ÑÕÉ¸‰½½°¡¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤…¹¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¤…¹¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ¤…¹¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤…¹€¡¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ¤½È¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤¤¤(()‘•˜}µ¥ÍÍ¥¹}±¥¹•Ì¡Á…¬è‘¥ÑmÍÑÈ°¹åt°µ½Ù•ÉÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑmÍÑÉtè(€€€±¥¹•Ì€ômt(€€€µ…É­•Ñ}‘…Ñ„€ôÁ…¬¹•Ð ‰µ…É­•Ñ}‘…Ñ„ˆ°íô¤(€€€™½È­•ä°±…‰•°¥¸l(€€€€€€€€ ‰9MDˆ°€‰9MGžÞ?–B#š2šVÀˆ¤°(€€€€€€€€ ‰M=`ˆ°€‰M=cš2šVÀˆ¤°(€€€€€€€€ ‰UM)Adˆ°€‹Ž'Ž¯–ˆ¤°(€€€€€€€€ ‰EEDˆ°€‰EEDˆ¤°(€€€€€€€€ ‰M5 ˆ°€‰M5 ˆ¤°(€€€€€€€€ ‰MAdˆ°€‰MAdˆ¤°(€€€€€€€€ ‰a1,ˆ°€‰a1,ˆ¤°(€€€€€€€€ ‰Y%`ˆ°€‰Y%`ˆ¤°(€€€€€€€€ ‰ULÉdˆ°€‹žÆÌË–æÓ¦G–"¤ˆ¤°(€€€tè(€€€€€€€¥˜µ…É­•Ñ}‘…Ñ„¹•Ð¡­•ä¤¥Ì9½¹”è(€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹¡˜ˆ´í±…‰•±÷Ž¿šr«–>[–ú\ˆ¤((€€€¥˜Á…¬¹•Ð ‰µ…É¼ˆ°íô¤¹•Ð ‰LÄÀˆ¤è(€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ˆ´ƒžÆÌÄÃ–æÓ¦G–"§Ž½IšršZÃ–>[–ú_š^—Ž»–“ŽŸŽŽ
+Ž–öOš^—Ž«Ž
‹Ž¯Ž
ÿŽ
“Žƒ–“ŽŸŽ¿Ž«Žˆ¤(€€€•±Í”è(€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ˆ´ƒžÆÌÄÃ–æÓ¦G–"¤LÄÀƒŽ¿šr«–>[–ú\ˆ¤((€€€™½È¥Ñ•´¥¸Á…¬¹•Ð ‰µ¥ÍÍ¥¹}‘…Ñ„ˆ°mt¤è(€€€€€€€Í½ÕÉ”€ô}±•…¸¡¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¤¤(€€€€€€€É•…Í½¸€ô}±•…¸¡¥Ñ•´¹•Ð ‰É•…Í½¸ˆ¤¤(€€€€€€€Í•Ù•É¥Ñä€ô}±•…¸¡¥Ñ•´¹•Ð ‰Í•Ù•É¥Ñäˆ¤¤(€€€€€€€¥˜µ½Ù•ÉÌ…¹Í½ÕÉ”€ôô€‰5…É­•Ð5½Ù•ÉÌˆ…¹€‰5…É­•Ð5½Ù•ÉÌˆ¥¸É•…Í½¸…¹€‰AÉ•µ…É­•Ðˆ¹½Ð¥¸É•…Í½¸…¹€‰™Ñ•È!½ÕÉÌˆ¹½Ð¥¸É•…Í½¸è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜µ½Ù•ÉÌ…¹Í½ÕÉ”€ôô€‰1½¹‰É¥‘”ˆ…¹€‰5…É­•Ðµ½Ù•ÉÌÉ…Ü)M=8¥Ìµ¥ÍÍ¥¹œˆ¥¸É•…Í½¸è(€€€€€€€€€€€É•…Í½¸€ô€‰1½¹‰É¥‘—–/–"•É…Üµ…É­•Ñ}µ½Ù•ÉÏŽ¿šr«š*W–—¾ò#žÖÇ–B!5…É­•Ð5½Ù•ÉÏ–g¢ŽsŽ¿–>[–ú_šâ#Žÿ¾ò$ˆ(€€€€€€€±¥¹•Ì¹…ÁÁ•¹¡˜ˆ´íÍ½ÕÉ•ôèíÉ•…Í½¹ô€¡íÍ•Ù•É¥Ñåô¤ˆ¤((€€€É•ÑÕÉ¸}‘•‘ÕÁ•}ÁÉ•Í•ÉÙ•}½É‘•È¡±¥¹•Ì¤½Èlˆ´ƒŽ«Ž\‰t(()‘•˜}½É‘•É•‘}Ý…Ñ¡±¥ÍÐ¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€‰å}Ñ¥­•È€ôí}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤¤è¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÍô(€€€½É‘•É•è±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€™½ÈÑ¥­•È¥¸]Q!1%MQ}=IHè(€€€€€€€¥Ñ•´€ô‘¥Ð¡‰å}Ñ¥­•È¹•Ð¡Ñ¥­•È°ì‰Ñ¥­•ÈˆèÑ¥­•È°€‰Í½ÕÉ•}Íåµ‰½°ˆè˜‰íÑ¥­•Éô¹UL‰ô¤¤(€€€€€€€½É‘•É•¹…ÁÁ•¹¡¥Ñ•´¤(€€€É•ÑÕÉ¸½É‘•É•(()‘•˜}Í•±•Ñ}™¥á•‘}¹½Ñ…‰±”¡Ý…Ñ¡±¥ÍÐè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€ÅÕ½Ñ•€ôm¥Ñ•´™½È¥Ñ•´¥¸Ý…Ñ¡±¥ÍÐ¥˜}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤¥Ì¹½Ð9½¹•t(€€€ÅÕ½Ñ•¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è…‰Ì¡}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤½È€À¤°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸ÅÕ½Ñ•‘lèÉt(()‘•˜}Í•±•Ñ}µ…É­•Ñ}µ½Ù•ÉÌ¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€™¥±Ñ•É•€ôm¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜}¥¹±Õ‘•}µ½Ù•È¡¥Ñ•´¥t(€€€™¥±Ñ•É•¹Í½ÉÐ¡­•äõ}µ½Ù•É}Í½É”°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸™¥±Ñ•É•‘lèÄÁt(()‘•˜}¥¹±Õ‘•}µ½Ù•È¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø‰½½°è(€€€Ñ¥­•È€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤¤¹ÕÁÁ•È ¤¹É•Á±…” ˆ¹ULˆ°€ˆˆ¤(€€€ÁÉ¥”€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰ÁÉ¥”ˆ¤¤(€€€¥˜ÁÉ¥”¥Ì¹½Ð9½¹”…¹ÁÉ¥”€ð€Ôè(€€€€€€€É•ÑÕÉ¸…±Í”(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€¥˜}¡…Í}¹½¥Í•}­•åÝ½É¡Ñ•áÐ¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜Ñ¥­•È¥¸M5%}Q%-ILè(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€É•ÑÕÉ¸}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡Ñ•áÐ¤½È}¥Í}±…É•}…À¡¥Ñ•´¤½È€‰¹…Í‘…Ä€ÄÀÀˆ¥¸Ñ•áÐ½È€‰Í½à½µÁ½¹•¹Ðˆ¥¸Ñ•áÐ(()‘•˜}µ½Ù•É}Í½É”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø™±½…Ðè(€€€Ñ¥­•È€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤¤¹ÕÁÁ•È ¤¹É•Á±…” ˆ¹ULˆ°€ˆˆ¤(€€€…Ñ•½Éä€ô}±•…¸¡¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ¤¤¹±½Ý•È ¤(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€Í½É”€ô…‰Ì¡}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤½È€À¤(€€€¥˜€‰Í½à½µÁ½¹•¹Ðˆ¥¸…Ñ•½Éäè(€€€€€€€Í½É”€¬ô€äÀ(€€€¥˜€‰¹…Í‘…Ä€ÄÀÀˆ¥¸…Ñ•½Éäè(€€€€€€€Í½É”€¬ô€àÀ(€€€¥˜Ñ¥­•È¥¸M5%}Q%-ILè(€€€€€€€Í½É”€¬ô€ÜÀ(€€€¥˜}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡Ñ•áÐ¤è(€€€€€€€Í½É”€¬ô€ÐÀ(€€€¥˜}¥Í}±…É•}…À¡¥Ñ•´¤è(€€€€€€€Í½É”€¬ô€ÈÀ(€€€É•ÑÕÉ¸Í½É”(()‘•˜}Í•±•Ñ}¹•ÝÍ}¥Ñ•µÌ¡¥Ñ•µÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°µ½Ù•ÉÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut°Ñ…É•Ñ}‘…Ñ”èÍÑÈ¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€Í•±•Ñ•€ôm¥Ñ•´™½È¥Ñ•´¥¸¥Ñ•µÌ¥˜}¥¹±Õ‘•}¹•ÝÌ¡¥Ñ•´°Ñ…É•Ñ}‘…Ñ”¥t(€€€µ½Ù•É}Ñ¥­•ÉÌ€ôí}±•…¸¡µ½Ù•È¹•Ð ‰Ñ¥­•Èˆ¤¤¹ÕÁÁ•È ¤¹É•Á±…” ˆ¹ULˆ°€ˆˆ¤™½Èµ½Ù•È¥¸µ½Ù•ÉÍô(€€€Í•±•Ñ•¹Í½ÉÐ¡­•äõ±…µ‰‘„¥Ñ•´è}¹•ÝÍ}Í½É”¡¥Ñ•´°µ½Ù•É}Ñ¥­•ÉÌ¤°É•Ù•ÉÍ”õQÉÕ”¤(€€€É•ÑÕÉ¸Í•±•Ñ•‘lèÄÉt(()‘•˜}¥¹±Õ‘•}¹•ÝÌ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt°Ñ…É•Ñ}‘…Ñ”èÍÑÈ¤€´ø‰½½°è(€€€¥˜¹½Ð¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ¤½È¹½Ð¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½Ð€¡¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ¤½È¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½Ð}¥Í}É••¹Ñ}¹•ÝÍ}‘…Ñ”¡¥Ñ•´¹•Ð ‰ÁÕ‰±¥Í¡•‘}…Ðˆ¤°Ñ…É•Ñ}‘…Ñ”¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜}¥Í}‰±½­•‘}¹•ÝÍ}ÕÉ°¡¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€¥˜}¡…Í}¹½¥Í•}­•åÝ½É¡Ñ•áÐ¤…¹¹½Ð}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡Ñ•áÐ¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡Ñ•áÐ¤½È‰½½°¡}É•±…Ñ•‘}Ñ¥­•ÉÌ¡¥Ñ•´¤¤½È€‰µ…É­•Ðè¹…Í‘…Äˆ¥¸Ñ•áÐ½È€‰Ñ¡•µ”èÍ•µ¥½¹‘ÕÑ½ÉÌˆ¥¸Ñ•áÐ(()‘•˜}¥Í}‰±½­•‘}¹•ÝÍ}ÕÉ°¡Ù…±Õ”è¹ä¤€´ø‰½½°è(€€€ÕÉ°€ô}±•…¸¡Ù…±Õ”¤¹±½Ý•È ¤(€€€¥˜¹½ÐÕÉ°è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€‰±½­•€ô€ (€€€€€€€€‰™…•‰½½¬¹½´ˆ°(€€€€€€€€‰ÑÝ¥ÑÑ•È¹½´ˆ°(€€€€€€€€‰à¹½´¼ˆ°(€€€€€€€€‰±¥¹­•‘¥¸¹½´½Á½ÍÑÌˆ°(€€€€€€€€‰É•ÕÑ•ÉÌ¹½´½Á±ÕÌˆ°(€€€€€€€€‰™¥¹…¹”¹å…¡½¼¹½´½ÅÕ½Ñ”ˆ°(€€€€€€€€ˆ½µ…É­•Ðµ…Ñ¥Ù¥Ñä½ÍÑ½­Ì¼ˆ°(€€€€€€€€ˆ½ÅÕ½Ñ”¼ˆ°(€€€€¤(€€€¥˜…¹ä¡µ…É­•È¥¸ÕÉ°™½Èµ…É­•È¥¸‰±½­•¤è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€É•ÑÕÉ¸ÕÉ°¹ÉÍÑÉ¥À ˆ¼ˆ¤¹•¹‘ÍÝ¥Ñ   ‰É•ÕÑ•ÉÌ¹½´ˆ°€‰¹‰Œ¹½´ˆ°€‰µ…É­•ÑÝ…Ñ ¹½´ˆ°€‰¹…Í‘…Ä¹½´ˆ°€‰™¥¹…¹”¹å…¡½¼¹½´ˆ¤¤(()‘•˜}¥Í}É••¹Ñ}¹•ÝÍ}‘…Ñ”¡Ù…±Õ”è¹ä°Ñ…É•Ñ}‘…Ñ”èÍÑÈ€ô€ˆˆ¤€´ø‰½½°è(€€€ÁÕ‰±¥Í¡•€ô}Á…ÉÍ•}¹•ÝÍ}‘…Ñ”¡Ù…±Õ”¤(€€€¥˜ÁÕ‰±¥Í¡•¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€ÑÉäè(€€€€€€€Ñ…É•Ð€ô‘…Ñ•Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡Ñ…É•Ñ}‘…Ñ”°€ˆ•d´•´´•ˆ¤¹‘…Ñ” ¤¥˜Ñ…É•Ñ}‘…Ñ”•±Í”‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€Ñ…É•Ð€ô‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤(€€€É•ÑÕÉ¸Ñ…É•Ð€´Ñ¥µ•‘•±Ñ„¡‘…åÌôÌÀ¤€ðôÁÕ‰±¥Í¡•€ðôÑ…É•Ð€¬Ñ¥µ•‘•±Ñ„¡‘…åÌôÄ¤(()‘•˜}Á…ÉÍ•}¹•ÝÍ}‘…Ñ”¡Ù…±Õ”è¹ä¤è(€€€Ñ•áÐ€ô}±•…¸¡Ù…±Õ”¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸9½¹”(€€€¥Í½}Ñ•áÐ€ôÑ•áÑlè´Åt€¬€ˆ¬ÀÀèÀÀˆ¥˜Ñ•áÐ¹•¹‘ÍÝ¥Ñ  ‰hˆ¤•±Í”Ñ•áÐ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ô‘…Ñ•Ñ¥µ”¹™É½µ¥Í½™½Éµ…Ð¡¥Í½}Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¹…ÍÑ¥µ•é½¹”¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹‘…Ñ” ¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€Á…ÍÌ(€€€ÑÉäè(€€€€€€€Á…ÉÍ•€ôÁ…ÉÍ•‘…Ñ•}Ñ½}‘…Ñ•Ñ¥µ”¡Ñ•áÐ¤(€€€€€€€É•ÑÕÉ¸Á…ÉÍ•¹…ÍÑ¥µ•é½¹”¡Ñ¥µ•é½¹”¹ÕÑŒ¤¹‘…Ñ” ¤¥˜Á…ÉÍ•¹Ñé¥¹™¼•±Í”Á…ÉÍ•¹‘…Ñ” ¤(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È°%¹‘•áÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€Á…ÍÌ(€€€™½È™µÐ¥¸€ ˆ•d´•´´•ˆ°€ˆ•ˆ€•°€•dˆ°€ˆ•€•°€•dˆ°€ˆ•´¼•¼•dˆ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸‘…Ñ•Ñ¥µ”¹ÍÑÉÁÑ¥µ”¡Ñ•áÐ°™µÐ¤¹‘…Ñ” ¤(€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€É•ÑÕÉ¸9½¹”(()‘•˜}¹•ÝÍ}Í½É”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt°µ½Ù•É}Ñ¥­•ÉÌèÍ•ÑmÍÑÉt¤€´ø™±½…Ðè(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€Í½É”€ô€À¸À(€€€É•±…Ñ•€ôÍ•Ð¡}É•±…Ñ•‘}Ñ¥­•ÉÌ¡¥Ñ•´¤¤(€€€¥˜É•±…Ñ•¹¥¹Ñ•ÉÍ•Ñ¥½¸¡]Q!1%MQ}=IH¤è(€€€€€€€Í½É”€¬ô€àÀ(€€€¥˜É•±…Ñ•¹¥¹Ñ•ÉÍ•Ñ¥½¸¡µ½Ù•É}Ñ¥­•ÉÌ¤è(€€€€€€€Í½É”€¬ô€ÜÀ(€€€¥˜€‰¹…Í‘…Äˆ¥¸Ñ•áÐ½È€‰Í½àˆ¥¸Ñ•áÐè(€€€€€€€Í½É”€¬ô€ÔÀ(€€€¥˜€‰Í•µ¥½¹‘ÕÑ½Èˆ¥¸Ñ•áÐ½È€‰¡¥Àˆ¥¸Ñ•áÐ½È€‰µ•µ½Éäˆ¥¸Ñ•áÐ½È€‰ÁÔˆ¥¸Ñ•áÐè(€€€€€€€Í½É”€¬ô€ÐÔ(€€€¥˜€‰‘…Ñ„•¹Ñ•Èˆ¥¸Ñ•áÐ½È€‰±½Õˆ¥¸Ñ•áÐ½È€‰‰½¹ˆ¥¸Ñ•áÐè(€€€€€€€Í½É”€¬ô€ÌÀ(€€€Í½ÕÉ•}É…¹¬€ôì‰e…¡½¼¥¹…¹”ˆè€à°€‰5…É­•Ñ]…Ñ ˆè€Ü°€‰9MDˆè€Ü°€‰9	ˆè€Ø°€‰M•ÉÁA$ˆè€Íô(€€€Í½É”€¬ôÍ½ÕÉ•}É…¹¬¹•Ð¡}±•…¸¡¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¤¤°€À¤(€€€É•ÑÕÉ¸Í½É”(()‘•˜}‰É¥•™}ÅÕ½Ñ•}¹½Ñ”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ñ¥­•È€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤¤¹ÕÁÁ•È ¤(€€€¡…¹”€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤(€€€¥˜¡…¹”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸€‹šVÃ–“šr«–>[–ú\ˆ(€€€¥˜…‰Ì¡¡…¹”¤€ð€À¸Äè(€€€€€€€É•ÑÕÉ¸€‹š¢«ŽÃŽˆ(€€€¥˜Ñ¥­•È¥¸ì‰9Yˆ°€‰Y<ˆ°€‰QM4ˆ°€‰5‰ô…¹¡…¹”€ðô€´Ìè(€€€€€€€É•ÑÕÉ¸€‹–6+–Â;’öO¦Z‹¦Ž£Ž_Ž›’â/¢B÷Ž3žn»ž®/Žˆ(€€€¥˜Ñ¥­•È€ôô€‰QM1ˆ…¹¡…¹”€ðô€´Ìè(€€€€€€€É•ÑÕÉ¸€‰[¦Z‹¦Ž£Ž_Ž›–òÇŽˆ(€€€¥˜¡…¹”€øô€Ìè(€€€€€€€É•ÑÕÉ¸€‹–’Ÿ–z/ŽŽŽ
¿Ž»’â·ŽŸ’â+šbŽ3žn»ž®/Žˆ(€€€¥˜¡…¹”€ø€Àè(€€€€€€€É•ÑÕÉ¸€‹–Â?–æ’â+šbˆ(€€€É•ÑÕÉ¸€‹–Â?–æ’â/¢Bôˆ(()‘•˜}™¥á•‘}É•…Í½¸¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€¡…¹”€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤(€€€¥˜¡…¹”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸€‹–në–ºkŽ
›Ž
§ŽŽ¦*cš~ŽƒŽ3šVÃ–“šr«–>[–ú\ˆ(€€€É•ÑÕÉ¸˜‹–në–ºkŽ
›Ž
§ŽŽ¦*cš~–ŽŸ–’'–.WŽ3žn»ž®/Ž“¾ò!¡…¹•}Á•É•¹Ðõí¡…¹”è¸É™÷¾ò'Ž–/–"—Ž»šb;žŠëŽ«švCšZgŽ¿žŠë¢ª7ŽŸŽ7Ž›ŽŽûŽoŽ
Lˆ(()‘•˜}½¹™¥‘•¹•}™É½µ}ÅÕ½Ñ”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€É•ÑÕÉ¸€‰µ•‘¥Õ´ˆ¥˜}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰¡…¹•}Á•É•¹Ðˆ¤¤¥Ì¹½Ð9½¹”•±Í”€‰Õ¹­¹½Ý¸ˆ(()‘•˜}Ù½±Õµ•}¥¹™¼¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ù½±Õµ”€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰Ù½±Õµ”ˆ¤¤(€€€…Ù•É…”€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰…Ù•É…•}Ù½±Õµ”ˆ¤¤(€€€¥˜Ù½±Õµ”¥Ì9½¹”…¹…Ù•É…”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸€‹šr«–>[–ú\ˆ(€€€¥˜Ù½±Õµ”¥Ì¹½Ð9½¹”…¹…Ù•É…”è(€€€€€€€É•ÑÕÉ¸˜‰Ù½±Õµ”õíÙ½±Õµ”è¸Á™ô°…Ù•É…•}Ù½±Õµ”õí…Ù•É…”è¸Á™ô°É…Ñ¥¼õíÙ½±Õµ”€¼…Ù•É…”è¸É™ôˆ(€€€¥˜Ù½±Õµ”¥Ì¹½Ð9½¹”è(€€€€€€€É•ÑÕÉ¸˜‰Ù½±Õµ”õíÙ½±Õµ”è¸Á™ôˆ(€€€É•ÑÕÉ¸˜‰…Ù•É…•}Ù½±Õµ”õí…Ù•É…”è¸Á™ôˆ(()‘•˜}µ½Ù•É}É•±•Ù…¹”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ñ¥­•È€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤¤¹ÕÁÁ•È ¤¹É•Á±…” ˆ¹ULˆ°€ˆˆ¤(€€€…Ñ•½Éä€ô}±•…¸¡¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ¤¤(€€€É•…Í½¸€ô}±•…¸¡¥Ñ•´¹•Ð ‰É•…Í½¸ˆ¤¤(€€€¥˜Ñ¥­•È¥¸M5%}Q%-IL½È€‰M=`ˆ¥¸…Ñ•½Éäè(€€€€€€€É•ÑÕÉ¸˜‰í…Ñ•½Éå÷Ž»–6+–Â;’öO¦Z‹¦–g¢ŽsŽ	íÉ•…Í½¹ôˆ(€€€¥˜€‰9…Í‘…Ä€ÄÀÀˆ¥¸…Ñ•½Éäè(€€€€€€€É•ÑÕÉ¸˜‰9…Í‘…Ä€ÄÀÃ¦Z‹¦Ž¹5…É­•Ð5½Ù•ÉÏ–g¢ŽsŽ	íÉ•…Í½¹ôˆ(€€€¥˜}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤¤è(€€€€€€€É•ÑÕÉ¸˜‰$¿Ž
¿Ž§Ž
›Ž$¿–’Ÿ–z/ŽŽŽ
¿¦Z‹¦Ž¹5…É­•Ð5½Ù•ÉÏ–g¢ŽsŽ	íÉ•…Í½¹ôˆ(€€€É•ÑÕÉ¸É•…Í½¸½È€‰5…É­•Ð5½Ù•ÉÏ–g¢ŽsŽšb;žŠëŽ«–/–"—švCšZgŽ¿žŠë¢ª7ŽŸŽ7Ž›ŽŽûŽoŽ
Lˆ(()‘•˜}¹•ÝÍ}É•±•Ù…¹”¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ñ•áÐ€ô}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´¤(€€€•á¥ÍÑ¥¹œ€ô}±•…¸¡¥Ñ•´¹•Ð ‰Ý¡å}É•±•Ù…¹Ðˆ¤¤(€€€É•±…Ñ•€ô}É•±…Ñ•‘}Ñ¥­•ÉÌ¡¥Ñ•´¤(€€€¥˜€‰Í•µ¥½¹‘ÕÑ½Èˆ¥¸Ñ•áÐ½È€‰¡¥Àˆ¥¸Ñ•áÐ½È€‰µ•µ½Éäˆ¥¸Ñ•áÐ½È€‰ÁÔˆ¥¸Ñ•áÐ½È€‰Í…µÍÕ¹œˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‹–6+–Â;’öL½'Ž
“ŽÏŽWŽ§¦Z‹¦Ž/Ž—ŽóŽ
çŽ£Ž_Ž™9MGŽíM=cŽ»–rÃ–B#ŽžŠë¢ª7Ž¯šr'žR ˆ(€€€¥˜€‰¹…Í‘…Äˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‰9MGš2šVÃŽ£–’Ÿ–z/ŽŽŽ
¿Ž»–rÃ–B#ŽšvCšZdˆ(€€€¥˜€‰±½Õˆ¥¸Ñ•áÐ½È€‰‘…Ñ„•¹Ñ•Èˆ¥¸Ñ•áÐ½È€‰‰½¹ˆ¥¸Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€‰'ŽïŽ
¿Ž§Ž
›Ž'š*W¢ÎŽ
¢Î¦G¢ªÿ¦SŽ»šÖŽ
3Ž
KžŠë¢ª7ŽgŽ
/švCšZdˆ(€€€¥˜É•±…Ñ•è(€€€€€€€É•ÑÕÉ¸˜‹¦Z‹¦¦*cš~–g¢Žpèìœ°€œ¹©½¥¸¡É•±…Ñ•¥ôˆ(€€€É•ÑÕÉ¸•á¥ÍÑ¥¹œ½È€‹Ž/Ž—ŽóŽ
çšvCšZgŽ£Ž_Ž›žŠë¢ª7–¾û¢Æ„ˆ(()‘•˜}É•±…Ñ•‘}Ñ¥­•ÉÌ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø±¥ÍÑmÍÑÉtè(€€€É•±…Ñ•€ôm}±•…¸¡Ñ¥­•È¤¹ÕÁÁ•È ¤¹É•Á±…” ˆ¹ULˆ°€ˆˆ¤™½ÈÑ¥­•È¥¸¥Ñ•´¹•Ð ‰É•±…Ñ•‘}Ñ¥­•ÉÌˆ°mt¤¥˜}±•…¸¡Ñ¥­•È¥t(€€€É…Ý}Ñ•áÐ€ô}¥Ñ•µ}É…Ý}Ñ•áÐ¡¥Ñ•´¤(€€€™½ÈÑ¥­•È¥¸]Q!1%MQ}=IH€¬Í½ÉÑ•¡M5%}Q%-IL¤è(€€€€€€€¥˜}µ•¹Ñ¥½¹Í}Ñ¥­•È¡É…Ý}Ñ•áÐ°Ñ¥­•È¤…¹Ñ¥­•È¹½Ð¥¸É•±…Ñ•è(€€€€€€€€€€€É•±…Ñ•¹…ÁÁ•¹¡Ñ¥­•È¤(€€€É•ÑÕÉ¸}‘•‘ÕÁ•}ÁÉ•Í•ÉÙ•}½É‘•È¡mÑ¥­•È™½ÈÑ¥­•È¥¸É•±…Ñ•¥˜Ñ¥­•Ét¤(()‘•˜}µ•¹Ñ¥½¹Í}Ñ¥­•È¡Ñ•áÐèÍÑÈ°Ñ¥­•ÈèÍÑÈ¤€´ø‰½½°è(€€€Á…ÑÑ•É¸€ôÉ˜ˆ üð…mµhÀ´åt¥pýíÉ”¹•Í…Á”¡Ñ¥­•È¥ô üép¹UL¤ü ü…mµhÀ´åt¤ˆ(€€€É•ÑÕÉ¸É”¹Í•…É ¡Á…ÑÑ•É¸°Ñ•áÐ¤¥Ì¹½Ð9½¹”(()‘•˜}¡…Í}¹½¥Í•}­•åÝ½É¡Ñ•áÐèÍÑÈ¤€´ø‰½½°è(€€€É•ÑÕÉ¸…¹ä¡­•åÝ½É¥¸Ñ•áÐ™½È­•åÝ½É¥¸9]M}a1U}-e]=IL¤(()‘•˜}¡…Í}ÍÑÉ½¹}Ñ•¡}­•åÝ½É¡Ñ•áÐèÍÑÈ¤€´ø‰½½°è(€€€É•ÑÕÉ¸…¹ä¡­•åÝ½É¥¸Ñ•áÐ™½È­•åÝ½É¥¸9]M}MQI=9}Q!}-e]=IL¤(()‘•˜}¥Í}±…É•}…À¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´ø‰½½°è(€€€µ…É­•Ñ}…À€ô}Ñ½}™±½…Ð¡¥Ñ•´¹•Ð ‰µ…É­•Ñ}…Àˆ¤¤(€€€É•ÑÕÉ¸‰½½°¡µ…É­•Ñ}…À…¹µ…É­•Ñ}…À€øô€ÄÁ|ÀÀÁ|ÀÀÁ|ÀÀÀ¤(()‘•˜}¥Ñ•µ}Ñ•áÐ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€É•ÑÕÉ¸}¥Ñ•µ}É…Ý}Ñ•áÐ¡¥Ñ•´¤¹±½Ý•È ¤(()‘•˜}¥Ñ•µ}É…Ý}Ñ•áÐ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt¤€´øÍÑÈè(€€€Ù…±Õ•Ì€ôl(€€€€€€€¥Ñ•´¹•Ð ‰Ñ¥­•Èˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰¹…µ”ˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰…Ñ•½Éäˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰É•…Í½¸ˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰Ñ¥Ñ±”ˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰Í¹¥ÁÁ•Ðˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰Ý¡å}É•±•Ù…¹Ðˆ¤°(€€€€€€€¥Ñ•´¹•Ð ‰ÕÉ°ˆ¤°(€€€t(€€€É•ÑÕÉ¸€ˆ€ˆ¹©½¥¸¡}±•…¸¡Ù…±Õ”¤™½ÈÙ…±Õ”¥¸Ù…±Õ•Ì¥˜Ù…±Õ”¥Ì¹½Ð9½¹”¤(()‘•˜}™½Éµ…Ñ}½µÁ…Ð¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€¥˜¥Í¥¹ÍÑ…¹”¡Ù…±Õ”°‘¥Ð¤è(€€€€€€€É•ÑÕÉ¸€ˆ°€ˆ¹©½¥¸¡˜‰í­•åôõí}±•…¸¡Ù…°¥ôˆ™½È­•ä°Ù…°¥¸Ù…±Õ”¹¥Ñ•µÌ ¤¤(€€€É•ÑÕÉ¸}±•…¸¡Ù…±Õ”¤(()‘•˜}Í¡½ÉÑ}Í¹¥ÁÁ•Ð¡Ù…±Õ”è¹ä°±¥µ¥Ðè¥¹Ð€ô€ÈÈÀ¤€´øÍÑÈè(€€€Ñ•áÐ€ô€ˆ€ˆ¹©½¥¸¡}±•…¸¡Ù…±Õ”¤¹ÍÁ±¥Ð ¤¤(€€€¥˜±•¸¡Ñ•áÐ¤€ðô±¥µ¥Ðè(€€€€€€€É•ÑÕÉ¸Ñ•áÐ(€€€É•ÑÕÉ¸Ñ•áÑlè±¥µ¥Ð€´€Åt¹ÉÍÑÉ¥À ¤€¬€‹Š˜ˆ(()‘•˜}Ñ½}™±½…Ð¡Ù…±Õ”è¹ä¤€´ø™±½…Ðð9½¹”è(€€€¥˜Ù…±Õ”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸9½¹”(€€€¥˜¥Í¥¹ÍÑ…¹”¡Ù…±Õ”°€¡¥¹Ð°™±½…Ð¤¤è(€€€€€€€É•ÑÕÉ¸™±½…Ð¡Ù…±Õ”¤(€€€ÑÉäè(€€€€€€€É•ÑÕÉ¸™±½…Ð¡ÍÑÈ¡Ù…±Õ”¤¹É•Á±…” ˆ”ˆ°€ˆˆ¤¹É•Á±…” ˆ°ˆ°€ˆˆ¤¤(€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€É•ÑÕÉ¸9½¹”(()‘•˜}±•…¸¡Ù…±Õ”è¹ä¤€´øÍÑÈè(€€€¥˜Ù…±Õ”¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€É•ÑÕÉ¸ÍÑÈ¡Ù…±Õ”¤¹ÍÑÉ¥À ¤(()‘•˜}‘•‘ÕÁ•}ÁÉ•Í•ÉÙ•}½É‘•È¡¥Ñ•µÌè±¥ÍÑmÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè(€€€Í••¸€ôÍ•Ð ¤(€€€½ÕÑÁÕÐ€ômt(€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€¥˜¥Ñ•´¹½Ð¥¸Í••¸è(€€€€€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡¥Ñ•´¤(€€€€€€€€€€€Í••¸¹…‘¡¥Ñ•´¤(€€€É•ÑÕÉ¸½ÕÑÁÕÐ(