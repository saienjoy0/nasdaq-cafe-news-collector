from __future__ import annotations

import re
from collections import Counter
from typing import Any

WATCHLIST_COMPANIES = {
    "NVDA": ["NVDA", "Nvidia", "NVIDIA"],
    "MSFT": ["MSFT", "Microsoft"],
    "AAPL": ["AAPL", "Apple"],
    "AMZN": ["AMZN", "Amazon"],
    "GOOGL": ["GOOGL", "GOOG", "Google", "Alphabet"],
    "META": ["META", "Meta"],
    "AVGO": ["AVGO", "Broadcom"],
    "TSM": ["TSM", "TSMC", "Taiwan Semiconductor"],
    "AMD": ["AMD", "Advanced Micro"],
    "TSLA": ["TSLA", "Tesla"],
}

DIRECT_CORE_TERMS = (
    "nasdaq 100",
    "sox",
    "qqq",
    "smh",
    "chip stocks",
    "chip stock",
    "chipmakers",
    "chipmaker",
    "semiconductor",
    "semiconductors",
    "nvidia",
    "amd",
    "tsm",
    "tsmc",
    "avgo",
    "ai infrastructure",
    "memory stocks",
    "chip trade",
    "chip sell-off",
)

SEMI_CONTEXT_TERMS = (
    "samsung",
    "deepseek",
    "china tariff",
    "tariff",
    "export control",
    "export controls",
    "us-china",
    "u.s.-china",
    "taiwan",
    "chip restriction",
    "chip restrictions",
)

MACRO_CONTEXT_TERMS = (
    "fed",
    "treasury",
    "treasury yield",
    "treasury yields",
    "yield",
    "yields",
    "dollar",
    "crude oil",
    "crude",
    "oil",
)

OTHER_CONTEXT_TERMS = (
    "electricity",
    "power demand",
    "data center",
    "datacenter",
    "geopolitics",
    "geopolitical",
)

TECH_BRIDGE_TERMS = (
    "nasdaq",
    "growth stocks",
    "tech stocks",
    "technology stocks",
    "semiconductor",
    "semiconductors",
    "chip",
    "chips",
    "chipmaker",
    "chipmakers",
    "ai",
    "nvidia",
    "amd",
    "tsm",
    "tsmc",
    "broadcom",
    "memory",
    "data center",
    "datacenter",
    "cloud",
)

LOW_VALUE_TERMS = (
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
    "commodity agriculture",
)

PERSONAL_FINANCE_TERMS = (
    "personal finance",
    "family finance",
    "student loans",
    "retirees",
    "parents' finances",
    "parents’ finances",
)


def enrich_news_drivers(
    news_items: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
    market_movers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mover_terms = _market_mover_terms(market_movers)
    enriched = []
    for item in news_items:
        classified = dict(item)
        _update_related_tickers(classified)
        score, reasons = _score_item(classified, mover_terms)
        bridge, affected_assets = _causal_bridge(classified, score)
        driver_type = _driver_type(classified, score, bridge)
        classified.update(
            {
                "driver_type": driver_type,
                "relevance_score": score,
                "causal_bridge": bridge,
                "affected_assets": affected_assets,
                "selected_for_handoff": False,
                "filter_reason": _filter_reason(classified, driver_type, reasons, bridge),
            }
        )
        enriched.append(classified)

    _mark_selected(enriched)
    return enriched


def summarize_low_value(news_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in news_items:
        if item.get("driver_type") == "low_value_or_irrelevant":
            counter[_summary_bucket(item)] += 1
    return [{"reason": reason, "count": count} for reason, count in counter.most_common()]


def _score_item(item: dict[str, Any], mover_terms: set[str]) -> tuple[int, list[str]]:
    title = _clean(item.get("title"))
    snippet = _clean(item.get("snippet"))
    title_lower = title.lower()
    text_lower = f"{title} {snippet}".lower()
    reasons: list[str] = []
    score = 0

    if _contains_any(text_lower, LOW_VALUE_TERMS) and not _has_bridge_terms(text_lower):
        score -= 8
        reasons.append("commodity agriculture or standalone commodity item without NASDAQ/tech bridge")
    if _contains_any(text_lower, PERSONAL_FINANCE_TERMS):
        score -= 8
        reasons.append("personal or family finance item")
    if "earnings" in text_lower and not _has_bridge_terms(text_lower) and not item.get("related_tickers"):
        score -= 8
        reasons.append("unrelated earnings item")

    if _contains_any(title_lower, DIRECT_CORE_TERMS):
        score += 6
        reasons.append("direct NASDAQ/SOX/semiconductor/AI infrastructure term in title")

    if _contains_any(text_lower, SEMI_CONTEXT_TERMS) and _has_semi_ai_bridge(text_lower):
        score += 5
        reasons.append("semiconductor or AI supply-chain context with visible bridge")

    if item.get("related_tickers"):
        score += 4
        reasons.append("direct fixed-watchlist relation")

    if _contains_any(text_lower, mover_terms):
        score += 3
        reasons.append("direct Market Movers relation")

    if _contains_any(text_lower, MACRO_CONTEXT_TERMS) and _has_macro_bridge(text_lower):
        score += 3
        reasons.append("macro factor with visible NASDAQ/growth/tech transmission")

    if _contains_any(text_lower, OTHER_CONTEXT_TERMS) and _has_bridge_terms(text_lower):
        score += 3
        reasons.append("infrastructure/geopolitical context with visible tech bridge")

    return score, reasons


def _driver_type(item: dict[str, Any], score: int, bridge: str) -> str:
    if score >= 6:
        return "core_driver"
    if score >= 3 and bridge:
        return "context_candidate"
    return "low_value_or_irrelevant"


def _causal_bridge(item: dict[str, Any], score: int) -> tuple[str, list[str]]:
    title = _clean(item.get("title"))
    snippet = _clean(item.get("snippet"))
    text_lower = f"{title} {snippet}".lower()
    evidence = _evidence_terms(text_lower)

    if score < 3:
        return "", []

    related = item.get("related_tickers") or []
    if _contains_any(text_lower, DIRECT_CORE_TERMS) or _has_semi_ai_bridge(text_lower):
        assets = _dedupe(["SOX", "SMH", "semiconductor watchlist", *related])
        return (
            f"{title} -> AI/semiconductor demand, expectations, or supply-chain repricing -> "
            f"{', '.join(assets)} -> title/snippet evidence: {evidence}",
            assets,
        )

    if _contains_any(text_lower, SEMI_CONTEXT_TERMS):
        assets = _dedupe(["SOX", "SMH", "NVDA", "AMD", "TSM", "AVGO", *related])
        return (
            f"{title} -> semiconductor supply chain, export controls, or AI-chip demand risk -> "
            f"{', '.join(assets)} -> title/snippet evidence: {evidence}",
            assets,
        )

    if _contains_any(text_lower, MACRO_CONTEXT_TERMS) and _has_macro_bridge(text_lower):
        assets = _dedupe(["Nasdaq 100", "QQQ", "growth stocks", *related])
        return (
            f"{title} -> rates, dollar, inflation, or risk-appetite channel -> "
            f"{', '.join(assets)} -> title/snippet evidence: {evidence}",
            assets,
        )

    if _contains_any(text_lower, OTHER_CONTEXT_TERMS):
        assets = _dedupe(["AI infrastructure", "data centers", "cloud capex", *related])
        return (
            f"{title} -> AI infrastructure capacity, power demand, or geopolitical risk channel -> "
            f"{', '.join(assets)} -> title/snippet evidence: {evidence}",
            assets,
        )

    if related:
        assets = _dedupe(related)
        return (
            f"{title} -> company-specific news or large-tech sentiment channel -> "
            f"{', '.join(assets)} -> title/snippet evidence: {evidence}",
            assets,
        )

    return "", []


def _filter_reason(item: dict[str, Any], driver_type: str, reasons: list[str], bridge: str) -> str:
    if driver_type == "core_driver":
        return "Directly usable as a main material for today's NASDAQ/AI/semiconductor narrative."
    if driver_type == "context_candidate":
        return "Background candidate only; use if it reinforces Core Drivers and market data."
    if not bridge:
        return "Weak or missing causal bridge to NASDAQ/SOX/AI/large tech."
    if reasons:
        return "; ".join(reasons)
    return "Low explanatory value for the handoff."


def _summary_bucket(item: dict[str, Any]) -> str:
    text = _text(item)
    if _contains_any(text, LOW_VALUE_TERMS):
        return "commodity agriculture articles: NASDAQ/semiconductor/AI stocksとの明確な接続がないためhandoff主材料から除外。"
    if _contains_any(text, PERSONAL_FINANCE_TERMS):
        return "personal/family finance articles: 市場ドライバーではないため除外。"
    if "earnings" in text:
        return "unrelated earnings: 固定ウォッチリスト/Market Movers/大型テックとの直接関係が弱いため除外。"
    return "weak causal bridge articles: 因果の橋が弱く、主材料として扱わない。"


def _mark_selected(items: list[dict[str, Any]]) -> None:
    core = sorted(
        [item for item in items if item.get("driver_type") == "core_driver"],
        key=lambda item: (item.get("relevance_score", 0), _clean(item.get("published_at"))),
        reverse=True,
    )[:8]
    context = sorted(
        [item for item in items if item.get("driver_type") == "context_candidate"],
        key=lambda item: (item.get("relevance_score", 0), _clean(item.get("published_at"))),
        reverse=True,
    )[:10]
    selected_ids = {id(item) for item in core + context}
    for item in items:
        item["selected_for_handoff"] = id(item) in selected_ids


def _update_related_tickers(item: dict[str, Any]) -> None:
    text = f"{_clean(item.get('title'))} {_clean(item.get('snippet'))}"
    existing = [_clean(ticker).upper().replace(".US", "") for ticker in item.get("related_tickers", []) if _clean(ticker)]
    for ticker, aliases in WATCHLIST_COMPANIES.items():
        if any(_mentions_alias(text, alias) for alias in aliases) and ticker not in existing:
            existing.append(ticker)
    item["related_tickers"] = _dedupe(existing)


def _market_mover_terms(market_movers: list[dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    for mover in market_movers:
        ticker = _clean(mover.get("ticker")).lower().replace(".us", "")
        name = _clean(mover.get("name")).lower()
        if ticker and len(ticker) >= 3:
            terms.add(ticker)
        if name:
            terms.add(name)
    return terms


def _has_bridge_terms(text: str) -> bool:
    return _contains_any(text, TECH_BRIDGE_TERMS)


def _has_semi_ai_bridge(text: str) -> bool:
    return _contains_any(text, ("chip", "chips", "semiconductor", "semiconductors", "ai", "gpu", "memory", "nvidia", "amd", "tsm", "tsmc", "broadcom"))


def _has_macro_bridge(text: str) -> bool:
    return _contains_any(text, ("nasdaq", "growth stocks", "tech stocks", "technology stocks", "semiconductors", "stock indexes", "stock market"))


def _evidence_terms(text: str) -> str:
    terms = [
        term
        for term in (
            "nasdaq",
            "sox",
            "chip",
            "chipmakers",
            "semiconductor",
            "ai",
            "nvidia",
            "amd",
            "tsm",
            "tsmc",
            "broadcom",
            "deepseek",
            "samsung",
            "treasury",
            "yield",
            "dollar",
            "crude",
            "oil",
            "data center",
            "export control",
            "tariff",
        )
        if term in text
    ]
    return ", ".join(_dedupe(terms)) or "matched title/snippet language"


def _mentions_alias(text: str, alias: str) -> bool:
    escaped = re.escape(alias)
    return re.search(rf"(?<![A-Za-z0-9])\$?{escaped}(?:\.US)?(?![A-Za-z0-9])", text, flags=re.IGNORECASE) is not None


def _contains_any(text: str, terms) -> bool:
    return any(term and _contains_term(text, term) for term in terms)


def _contains_term(text: str, term: str) -> bool:
    normalized = term.lower()
    if re.search(r"[a-z0-9]", normalized):
        return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None
    return normalized in text


def _text(item: dict[str, Any]) -> str:
    return f"{_clean(item.get('title'))} {_clean(item.get('snippet'))} {_clean(item.get('why_relevant'))}".lower()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return output
