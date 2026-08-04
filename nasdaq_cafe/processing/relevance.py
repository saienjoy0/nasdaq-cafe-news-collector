from __future__ import annotations

from typing import Any

from nasdaq_cafe.config import WATCHLIST


THEME_KEYWORDS = {
    "AI": ["AI", "artificial intelligence", "Nvidia", "GPU"],
    "semiconductors": ["semiconductor", "chip", "SOX", "foundry", "TSMC"],
    "cloud": ["cloud", "Azure", "AWS", "Google Cloud"],
    "EV": ["EV", "electric vehicle", "Tesla"],
    "ads": ["advertising", "ads", "Meta", "Google"],
    "software": ["software", "SaaS"],
    "rates": ["Treasury", "yield", "rates", "DGS10"],
}


def explain_relevance(item: dict[str, Any]) -> str:
    text = f"{item.get('title', '')} {item.get('snippet', '')}"
    tickers = item.get("related_tickers") or []
    reasons: list[str] = []
    if tickers:
        reasons.append("watchlist ticker: " + ", ".join(tickers))
    lower = text.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword.lower() in lower for keyword in keywords):
            reasons.append(f"theme: {theme}")
    if "nasdaq" in lower:
        reasons.append("market: NASDAQ")
    if "sox" in lower or "semiconductor" in lower:
        reasons.append("market: SOX/semiconductors")
    return "; ".join(reasons)


def is_relevant_news(item: dict[str, Any]) -> bool:
    if item.get("related_tickers"):
        return True
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    if any(ticker.lower() in text for ticker in WATCHLIST):
        return True
    return any(
        keyword.lower() in text
        for keywords in THEME_KEYWORDS.values()
        for keyword in keywords
    ) or "nasdaq" in text


def select_candidates(news_items: list[dict[str, Any]], market_movers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    main_topic: list[dict[str, Any]] = []
    for item in news_items:
        if is_relevant_news(item):
            main_topic.append(
                {
                    "title": item.get("title", ""),
                    "reason": item.get("why_relevant", "") or explain_relevance(item),
                    "confidence": item.get("confidence", "unknown"),
                    "sources": [item.get("source", ""), item.get("url", "")],
                }
            )
        if len(main_topic) >= 3:
            break

    ticker_topic: list[dict[str, Any]] = []
    for mover in market_movers:
        ticker = mover.get("ticker", "")
        if ticker:
            ticker_topic.append(
                {
                    "ticker": ticker,
                    "reason": mover.get("reason", "") or "Market mover candidate from collected source data.",
                    "confidence": mover.get("confidence", "unknown"),
                    "sources": [mover.get("source", "")],
                }
            )
        if len(ticker_topic) >= 5:
            break

    return {"main_topic": main_topic, "ticker_topic": ticker_topic}

