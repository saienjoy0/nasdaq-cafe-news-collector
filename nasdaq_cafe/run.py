from __future__ import annotations

import argparse
import sys
from typing import Any

from nasdaq_cafe.cache import read_json
from nasdaq_cafe.collection_policy import collect_manifest_fulltext_with_policy
from nasdaq_cafe.collectors.economic_calendar_collector import collect_economic_calendar
from nasdaq_cafe.collectors.fred_collector import collect_fred_dgs10
from nasdaq_cafe.collectors.fmp_collector import collect_fmp
from nasdaq_cafe.collectors.gdelt_radar_collector import (
    collect_gdelt_radar,
    gdelt_daily_lead_theme_candidates,
    gdelt_fulltext_candidates,
    gdelt_radar_candidates,
    gdelt_radar_status,
    statement_fulltext_candidates,
    statement_radar_candidates,
    statement_radar_status,
)
from nasdaq_cafe.collectors.longbridge_cli_quotes import ensure_longbridge_quotes
from nasdaq_cafe.collectors.longbridge_raw_loader import load_longbridge_raw
from nasdaq_cafe.collectors.market_movers_collector import collect_market_movers
from nasdaq_cafe.collectors.rss_collector import collect_rss_news
from nasdaq_cafe.collectors.search_collector import collect_search_news
from nasdaq_cafe.collectors.sec_ir_collector import collect_sec_ir
from nasdaq_cafe.config import RunConfig, WATCHLIST, build_config, unique_missing
from nasdaq_cafe.outputs.write_chatgpt_handoff import write_chatgpt_handoff
from nasdaq_cafe.outputs.write_fulltext_handoff import write_chatgpt_fulltext_handoff
from nasdaq_cafe.outputs.write_prompt_input import write_prompt_input
from nasdaq_cafe.outputs.write_source_pack import write_source_pack
from nasdaq_cafe.processing.article_review_targets import build_article_review_targets
from nasdaq_cafe.processing.news_drivers import enrich_news_drivers
from nasdaq_cafe.processing.normalize import dedupe_news, utc_now_iso
from nasdaq_cafe.processing.relevance import select_candidates
from nasdaq_cafe.provider_capability import write_provider_capability_report
from nasdaq_cafe.raw_archive import (
    article_fulltext_status,
    build_raw_archive_manifest,
    register_and_fetch_url,
    retry_failed_fulltext,
)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = "collect"
    if arguments and arguments[0] in {"collect", "fetch-url", "retry-failed"}:
        command = arguments.pop(0)

    if command == "fetch-url":
        parser = argparse.ArgumentParser(description="Register one URL in the Raw Archive and fetch its full text.")
        parser.add_argument("url")
        parser.add_argument("--title", default="")
        parser.add_argument("--date", default="today", help="'today' or YYYY-MM-DD")
        parser.add_argument("--refresh", action="store_true")
        args = parser.parse_args(arguments)
        config = build_config(args.date, args.refresh)
        result = register_and_fetch_url(config, args.url, args.title)
        _print_raw_archive_result(config, result)
        return 0

    if command == "retry-failed":
        parser = argparse.ArgumentParser(description="Retry failed and limit-deferred Raw Archive URLs.")
        parser.add_argument("--date", default="today", help="'today' or YYYY-MM-DD")
        args = parser.parse_args(arguments)
        config = build_config(args.date, False)
        result = retry_failed_fulltext(config)
        _print_raw_archive_result(config, result)
        return 0

    parser = argparse.ArgumentParser(description="Collect the morning NASDAQ Cafe source pack and Raw Archive.")
    parser.add_argument("--date", default="today", help="'today' or YYYY-MM-DD")
    parser.add_argument("--refresh", action="store_true", help="Fetch again instead of using raw JSON cache.")
    args = parser.parse_args(arguments)

    config = build_config(args.date, args.refresh)
    pack = build_source_pack(config)
    write_provider_capability_report(config, pack)
    write_source_pack(config.output_dir, pack)
    write_prompt_input(config.output_dir, pack)
    write_chatgpt_handoff(config.output_dir, pack)
    article_fulltext = read_json(config.raw_dir / "article_fulltext.json") or {}
    write_chatgpt_fulltext_handoff(config.output_dir, pack, article_fulltext)

    print(f"Generated: {config.output_dir / 'source_pack.md'}")
    print(f"Generated: {config.output_dir / 'source_pack.json'}")
    print(f"Generated: {config.output_dir / 'provider_capability_report.json'}")
    print(f"Generated: {config.raw_dir / 'manifest.json'}")
    print(f"Generated: {config.raw_dir / 'article_fulltext.json'}")
    print(f"Generated: {config.output_dir / f'CHATGPT_HANDOFF_{config.target_date}.md'}")
    print(f"Generated: {config.output_dir / f'CHATGPT_FULLTEXT_HANDOFF_{config.target_date}.md'}")
    print(f"Generated: {config.output_dir / f'daily_source_package_{config.target_date}.md'}")
    print(f"Generated: {config.output_dir / 'chatgpt_submission' / f'daily_source_package_{config.target_date}.md'}")
    print(f"Generated: {config.output_dir / 'README_FOR_HUMAN.md'}")
    print(f"Generated: {config.output_dir.parent / 'latest' / 'chatgpt_handoff.md'}")
    print(f"Generated: {config.output_dir.parent / 'latest' / 'chatgpt_fulltext_handoff.md'}")
    return 0


def _print_raw_archive_result(config: RunConfig, result: dict[str, Any]) -> None:
    summary = result.get("summary", {})
    print(f"Generated: {config.raw_dir / 'manifest.json'}")
    print(f"Generated: {config.raw_dir / 'article_fulltext.json'}")
    print(
        "Full text: "
        f"targets={summary.get('target_count', 0)}, "
        f"attempted={summary.get('attempted_count', 0)}, "
        f"complete={summary.get('complete_count', 0)}, "
        f"failed={summary.get('failed_count', 0)}, "
        f"not_attempted_limit={summary.get('not_attempted_limit_count', 0)}, "
        f"not_attempted_runtime_budget={summary.get('not_attempted_runtime_budget_count', 0)}"
    )


def build_source_pack(config: RunConfig) -> dict[str, Any]:
    missing_data: list[dict[str, str]] = []
    cache_used_flags: list[bool] = []
    statuses: dict[str, str] = {}

    longbridge_quote_fetch = ensure_longbridge_quotes(config)
    missing_data.extend(longbridge_quote_fetch["missing_data"])
    cache_used_flags.append(longbridge_quote_fetch["cache_used"])

    longbridge = load_longbridge_raw(config.raw_dir, config.env)
    missing_data.extend(longbridge["missing_data"])
    statuses["Longbridge"] = longbridge["status"]
    statuses["Longbridge quote fetch"] = longbridge_quote_fetch["status"]

    fred = collect_fred_dgs10(config)
    missing_data.extend(fred["missing_data"])
    cache_used_flags.append(fred["cache_used"])
    statuses["FRED"] = fred["status"]

    fmp = collect_fmp(config)
    missing_data.extend(fmp["missing_data"])
    cache_used_flags.append(fmp["cache_used"])
    statuses["FMP"] = fmp["status"]

    sec_ir = collect_sec_ir(config)
    missing_data.extend(sec_ir["missing_data"])
    cache_used_flags.append(sec_ir["cache_used"])
    statuses["SEC / IR"] = sec_ir["status"]

    rss = collect_rss_news(config)
    missing_data.extend(rss["missing_data"])
    cache_used_flags.append(rss["cache_used"])
    statuses["RSS"] = rss["status"]

    search = collect_search_news(config)
    missing_data.extend(search["missing_data"])
    cache_used_flags.append(search["cache_used"])
    statuses["SerpAPI"] = search["status"].get("SerpAPI", "unknown")
    statuses["Tavily"] = search["status"].get("Tavily", "unknown")

    movers = collect_market_movers(
        config,
        longbridge["market_movers"],
        longbridge.get("market_mover_inputs", []),
    )
    missing_data.extend(movers["missing_data"])
    cache_used_flags.append(movers["cache_used"])
    statuses["Market Movers"] = movers["status"]

    economic_calendar = collect_economic_calendar(config)
    missing_data.extend(economic_calendar["missing_data"])
    cache_used_flags.append(economic_calendar["cache_used"])
    statuses["Economic Calendar"] = economic_calendar["status"]

    gdelt_radar = collect_gdelt_radar(config)
    missing_data.extend(gdelt_radar["missing_data"])
    cache_used_flags.append(gdelt_radar["cache_used"])
    statuses["GDELT Radar"] = gdelt_radar["status"]

    # Critical boundary: every collector URL is registered before relevance,
    # review priority, driver or handoff selection. The retrieval wrapper applies
    # only a technical runtime safeguard and never sees editorial fields.
    manifest = build_raw_archive_manifest(
        config,
        {
            "rss": rss.get("raw", {}),
            "serpapi": search.get("raw", {}).get("serpapi", {}),
            "tavily_search": search.get("raw", {}).get("tavily", {}),
            "longbridge_news": longbridge.get("raw", {}).get("news") or longbridge.get("news_items", []),
            "gdelt": gdelt_radar.get("raw", {}),
            "fmp": fmp.get("raw", {}),
            "sec_ir": sec_ir.get("raw", {}),
        },
    )
    article_fulltext = collect_manifest_fulltext_with_policy(config, manifest)
    cache_used_flags.append(article_fulltext["cache_used"])
    statuses["Raw Archive"] = article_fulltext["status"]
    statuses["Article Full Text"] = article_fulltext["status"]

    market_movers = movers["market_movers"] or longbridge["market_movers"]
    watchlist = _merge_watchlist(longbridge["watchlist"])
    news_items = enrich_news_drivers(
        dedupe_news(rss["news_items"] + longbridge.get("news_items", []) + fmp.get("news_items", [])),
        watchlist,
        market_movers,
    )
    search_supplements = dedupe_news(search.get("search_supplements", search["news_items"]))
    article_review_targets, article_review_missing = build_article_review_targets(news_items)
    missing_data.extend(article_review_missing)
    selected_candidates_raw = select_candidates(
        [item for item in news_items if item.get("selected_for_handoff")],
        market_movers,
    )
    selected_candidates = {
        "primary_candidates": selected_candidates_raw.get("main_topic", []),
        "ticker_candidates": selected_candidates_raw.get("ticker_topic", []),
    }
    market_data = {
        "NASDAQ": longbridge["market_data"].get("NASDAQ"),
        "SOX": longbridge["market_data"].get("SOX"),
        "USDJPY": longbridge["market_data"].get("USDJPY"),
    }
    raw_archive_status = article_fulltext_status(article_fulltext, config)
    coverage = article_fulltext.get("acquisition_coverage", {})
    if isinstance(coverage, dict):
        raw_archive_status["not_attempted_runtime_budget_count"] = coverage.get(
            "not_attempted_runtime_budget_unique", 0
        )

    return {
        "date": config.target_date,
        "researchTradingDate": config.research_trading_date,
        "researchTradingSession": {
            "calendar": config.research_trading_calendar,
            "marketOpen": config.research_trading_market_open,
            "marketClose": config.research_trading_market_close,
            "isHalfDay": config.research_trading_is_half_day,
            "resolution": "latest-completed-regular-session-before-episode-collection-cutoff",
        },
        "generated_at": utc_now_iso(),
        "cache": {
            "cache_used": any(cache_used_flags),
            "refresh": config.refresh,
        },
        "market_status": {
            "session_note": "Sessions remain separate when the source exposes session fields; otherwise session is unknown.",
        },
        "market_data": market_data,
        "macro": fred.get("values", {"DGS10": fred["value"]}),
        "watchlist": watchlist,
        "market_movers": market_movers,
        "economic_events": economic_calendar["economic_events"],
        "gdelt_radar_status": gdelt_radar_status(gdelt_radar, config),
        "gdelt_radar_candidates": gdelt_radar_candidates(gdelt_radar),
        "gdelt_fulltext_candidates": gdelt_fulltext_candidates(gdelt_radar),
        "daily_lead_theme_candidates": gdelt_daily_lead_theme_candidates(gdelt_radar),
        "statement_radar_status": statement_radar_status(gdelt_radar),
        "statement_radar_candidates": statement_radar_candidates(gdelt_radar),
        "statement_fulltext_candidates": statement_fulltext_candidates(gdelt_radar),
        "news_items": news_items,
        "search_supplements": search_supplements,
        "article_review_targets": article_review_targets,
        "article_fulltext_status": raw_archive_status,
        "raw_archive_status": raw_archive_status,
        "selected_candidates": selected_candidates,
        "collector_metadata": {
            "RSS": rss.get("collector_metadata", {}),
            "Search": search.get("collector_metadata", {}),
            "Raw Archive": manifest.get("retrieval_policy", {}),
            "acquisitionCoverage": coverage,
            "providerCapabilitySidecar": {
                "path": f"output/{config.target_date}/provider_capability_report.json",
                "operationalOnly": True,
                "marketEvidence": False,
            },
            "api_key_status": _api_key_status(config.env),
        },
        "missing_data": unique_missing(missing_data),
        "source_status": statuses,
    }


def _merge_watchlist(longbridge_watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ticker = {item.get("ticker"): item for item in longbridge_watchlist}
    output = []
    for ticker in WATCHLIST:
        output.append(
            by_ticker.get(ticker)
            or {
                "ticker": ticker,
                "source_symbol": f"{ticker}.US",
                "price": None,
                "change": None,
                "change_percent": None,
                "session": "unknown",
                "raw_source": "Longbridge",
            }
        )
    return output


def _api_key_status(env: dict[str, str]) -> dict[str, str]:
    return {
        "SERPAPI_API_KEY": _set_or_not(env.get("SERPAPI_API_KEY")),
        "TAVILY_API_KEY": _set_or_not(env.get("TAVILY_API_KEY")),
        "FRED_API_KEY": _set_or_not(env.get("FRED_API_KEY")),
        "FMP_API_KEY": _set_or_not(env.get("FMP_API_KEY")),
        "SEC_USER_AGENT": _set_or_not(env.get("SEC_USER_AGENT")),
        "LONGBRIDGE": "plugin_or_raw_preferred",
    }


def _set_or_not(value: Any) -> str:
    return "set" if str(value or "").strip() else "not_set"


if __name__ == "__main__":
    raise SystemExit(main())
