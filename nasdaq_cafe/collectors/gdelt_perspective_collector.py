from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.collectors import gdelt_radar_collector as legacy_gdelt
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.research_candidate_pool import PERSPECTIVE_BY_ID


PERSPECTIVE_CONTRACT_VERSION = "market-transmission-perspectives-v1.1"

PERSPECTIVE_GDELT_CATEGORIES: dict[str, dict[str, Any]] = {
    "P1_ai_semiconductor": {
        "perspective_id": "P1",
        "query": '(AI OR "artificial intelligence" OR GPU OR semiconductor OR chips OR HBM OR foundry) (Nvidia OR AMD OR TSMC OR Broadcom OR memory OR datacenter OR "data center")',
        "terms": ["ai", "artificial intelligence", "gpu", "semiconductor", "chips", "hbm", "foundry", "nvidia", "amd", "tsmc", "broadcom", "memory", "datacenter", "data center"],
    },
    "P2_large_tech_cloud_software": {
        "perspective_id": "P2",
        "query": '(Microsoft OR Amazon OR Google OR Alphabet OR Meta OR Apple OR hyperscaler OR cloud OR software) (markets OR earnings OR capex OR AI OR enterprise)',
        "terms": ["microsoft", "amazon", "google", "alphabet", "meta", "apple", "hyperscaler", "cloud", "software", "capex", "enterprise"],
    },
    "P3_japan": {
        "perspective_id": "P3",
        "query": '(Japan OR Japanese OR Nikkei OR TOPIX OR "Bank of Japan" OR BOJ OR yen) (markets OR stocks OR policy OR exports OR supply chain OR economy)',
        "terms": ["japan", "japanese", "nikkei", "topix", "bank of japan", "boj", "yen", "exports", "supply chain", "economy"],
    },
    "P4_china_hong_kong": {
        "perspective_id": "P4",
        "query": '(China OR Chinese OR "Hong Kong" OR "Hang Seng" OR PBoC OR renminbi OR yuan OR Alibaba OR Tencent OR Baidu) (markets OR policy OR economy OR exports OR manufacturing OR property OR technology)',
        "terms": ["china", "chinese", "hong kong", "hang seng", "pboc", "renminbi", "yuan", "alibaba", "tencent", "baidu", "exports", "manufacturing", "property"],
    },
    "P5_macro_rates_fx": {
        "perspective_id": "P5",
        "query": '(Federal Reserve OR Fed OR inflation OR CPI OR PCE OR Treasury OR yields OR dollar OR currency OR "central bank") (markets OR stocks OR economy OR rates)',
        "terms": ["federal reserve", "fed", "inflation", "cpi", "pce", "treasury", "yields", "dollar", "currency", "central bank", "rates"],
    },
    "P6_energy_power_commodities": {
        "perspective_id": "P6",
        "query": '(oil OR crude OR "natural gas" OR electricity OR power OR nuclear OR uranium OR copper OR "rare earth" OR commodities) (markets OR prices OR supply OR demand OR inflation OR datacenter)',
        "terms": ["oil", "crude", "natural gas", "electricity", "power", "nuclear", "uranium", "copper", "rare earth", "commodities", "prices", "supply", "demand"],
    },
    "P7_geopolitics_trade_sanctions": {
        "perspective_id": "P7",
        "query": '(tariffs OR sanctions OR "export controls" OR war OR NATO OR Hormuz OR "Taiwan Strait" OR "Strait of Malacca" OR "Red Sea") (markets OR trade OR shipping OR energy OR chips OR supply)',
        "terms": ["tariffs", "sanctions", "export controls", "war", "nato", "hormuz", "taiwan strait", "strait of malacca", "red sea", "trade", "shipping", "energy", "chips", "supply"],
    },
    "P8_supply_chain_logistics_global_economy": {
        "perspective_id": "P8",
        "query": '("supply chain" OR shipping OR ports OR logistics OR manufacturing OR "factory disruption" OR "rare earth" OR earthquake OR "global demand") (markets OR trade OR chips OR economy OR prices)',
        "terms": ["supply chain", "shipping", "ports", "logistics", "manufacturing", "factory disruption", "rare earth", "earthquake", "global demand", "trade", "economy", "prices"],
    },
}

THEME_LABELS = {
    "P1_ai_semiconductor": "AI / Semiconductor",
    "P2_large_tech_cloud_software": "Large Tech / Cloud / Software",
    "P3_japan": "Japan Market / Policy / Tech",
    "P4_china_hong_kong": "China / Hong Kong Market / Policy / Tech",
    "P5_macro_rates_fx": "Macro / Rates / FX",
    "P6_energy_power_commodities": "Energy / Power / Commodities",
    "P7_geopolitics_trade_sanctions": "Geopolitics / Trade / Sanctions",
    "P8_supply_chain_logistics_global_economy": "Supply Chain / Logistics / Global Economy",
}


def collect_gdelt_radar(config: RunConfig) -> dict[str, Any]:
    """Run existing GDELT mechanics with eight market-transmission perspectives."""
    path = config.raw_dir / "gdelt_radar.json"
    payload = read_json(path) if path.exists() and not config.refresh else None
    cache_used = isinstance(payload, dict) and payload.get("perspective_contract_version") == PERSPECTIVE_CONTRACT_VERSION

    if not cache_used:
        with _perspective_globals():
            payload = legacy_gdelt._fetch_gdelt_radar(config.target_date)
        payload["perspective_contract_version"] = PERSPECTIVE_CONTRACT_VERSION
        payload["perspective_contract"] = {
            category: {
                "perspective_id": spec["perspective_id"],
                "label": THEME_LABELS[category],
            }
            for category, spec in PERSPECTIVE_GDELT_CATEGORIES.items()
        }
        write_json(path, payload)

    assert isinstance(payload, dict)
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    missing_data = []
    for item in payload.get("missing_data", []):
        if not isinstance(item, dict):
            continue
        missing_data.append(
            missing(
                item.get("source", "GDELT Radar"),
                item.get("reason", "GDELT Radar did not return usable data."),
                item.get("severity", "low"),
            )
        )
    if not summary.get("accepted_count"):
        missing_data.append(missing("GDELT Radar", "No accepted GDELT market-transmission candidates for this run.", "low"))

    result = {
        "status": legacy_gdelt._status_from_payload(payload),
        "cache_used": cache_used,
        "raw": payload,
        "summary": summary,
        "missing_data": missing_data,
    }
    result["discovery_coverage"] = _coverage(payload)
    return result


@contextmanager
def _perspective_globals() -> Iterator[None]:
    old_categories = legacy_gdelt.GDELT_CATEGORIES
    old_global_terms = legacy_gdelt.GLOBAL_RELEVANCE_TERMS
    old_labels = legacy_gdelt.THEME_LABELS
    legacy_gdelt.GDELT_CATEGORIES = PERSPECTIVE_GDELT_CATEGORIES
    legacy_gdelt.GLOBAL_RELEVANCE_TERMS = sorted(
        {
            term
            for category in PERSPECTIVE_GDELT_CATEGORIES.values()
            for term in category.get("terms", [])
        },
        key=len,
        reverse=True,
    )
    legacy_gdelt.THEME_LABELS = THEME_LABELS
    try:
        yield
    finally:
        legacy_gdelt.GDELT_CATEGORIES = old_categories
        legacy_gdelt.GLOBAL_RELEVANCE_TERMS = old_global_terms
        legacy_gdelt.THEME_LABELS = old_labels


def _coverage(payload: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        PERSPECTIVE_BY_ID[f"P{index}"]["key"]: {
            "attempted": False,
            "provider_attempts": {"gdelt": False},
            "provider_status": {"gdelt": "not_attempted"},
            "raw_count": 0,
            "candidate_count": 0,
        }
        for index in range(1, 9)
    }
    for record in payload.get("queries", []):
        if not isinstance(record, dict):
            continue
        category = str(record.get("category") or "")
        spec = PERSPECTIVE_GDELT_CATEGORIES.get(category)
        if spec is None:
            continue
        perspective = PERSPECTIVE_BY_ID[spec["perspective_id"]]
        row = output[perspective["key"]]
        row["attempted"] = True
        row["provider_attempts"]["gdelt"] = True
        status = str(record.get("status") or "unknown")
        row["provider_status"]["gdelt"] = status
        try:
            row["raw_count"] += int(record.get("result_count") or 0)
        except (TypeError, ValueError):
            pass
    for item in payload.get("accepted", []):
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or item.get("gdelt_query_category") or "")
        spec = PERSPECTIVE_GDELT_CATEGORIES.get(category)
        if spec is None:
            continue
        perspective = PERSPECTIVE_BY_ID[spec["perspective_id"]]
        output[perspective["key"]]["candidate_count"] += 1
    return output


# Preserve the original source-pack/handoff helper contract.
gdelt_radar_status = legacy_gdelt.gdelt_radar_status
gdelt_radar_candidates = legacy_gdelt.gdelt_radar_candidates
gdelt_fulltext_candidates = legacy_gdelt.gdelt_fulltext_candidates
gdelt_daily_lead_theme_candidates = legacy_gdelt.gdelt_daily_lead_theme_candidates
statement_radar_status = legacy_gdelt.statement_radar_status
statement_radar_candidates = legacy_gdelt.statement_radar_candidates
statement_fulltext_candidates = legacy_gdelt.statement_fulltext_candidates
