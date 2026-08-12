from __future__ import annotations

from typing import Any

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.config import RunConfig

PROVIDERS = [
    ("Longbridge", "Longbridge", None, ["longbridge"]),
    ("FRED", "FRED", "FRED_API_KEY", ["fred"]),
    ("FMP", "FMP", "FMP_API_KEY", ["fmp"]),
    ("SEC / IR", "SEC / IR", "SEC_USER_AGENT", ["sec", "ir"]),
    ("SerpAPI", "SerpAPI", "SERPAPI_API_KEY", ["serpapi"]),
    ("Tavily", "Tavily", "TAVILY_API_KEY", ["tavily"]),
    ("GDELT Radar", "GDELT Radar", None, ["gdelt"]),
    ("Economic Calendar", "Economic Calendar", None, ["economic calendar", "bls"]),
    ("RSS", "RSS", None, ["rss"]),
    ("Raw Archive", "Raw Archive", None, ["raw archive", "article full text"]),
]


def _configured(config: RunConfig, key: str | None) -> bool:
    if key is None:
        return True
    return bool(str(config.env.get(key) or "").strip())


def _observed_status(raw: Any, configured: bool) -> str:
    value = str(raw or "unknown").strip().lower()
    if value in {"ok", "ready", "loaded", "fetched", "complete"}:
        return "ready"
    if "partial" in value or "degraded" in value:
        return "degraded"
    if "skip" in value:
        return "skipped" if configured else "missing"
    if "fail" in value or "error" in value:
        return "failed" if configured else "missing"
    if value in {"empty", "none", "missing", "not_set", "not set"}:
        return "missing"
    if value in {"not-applicable", "n/a"}:
        return "not-applicable"
    return "degraded"


def _reason_for(missing_data: list[dict[str, Any]], aliases: list[str]) -> str | None:
    matches: list[str] = []
    for row in missing_data:
        source = str(row.get("source") or "").lower()
        if not any(alias in source for alias in aliases):
            continue
        reason = str(row.get("reason") or "").strip()
        if reason and reason not in matches:
            matches.append(reason)
    if not matches:
        return None
    return " | ".join(matches[:3])


def build_provider_capability_report(
    config: RunConfig,
    pack: dict[str, Any],
) -> dict[str, Any]:
    statuses = pack.get("source_status", {})
    missing_data = pack.get("missing_data", [])
    if not isinstance(statuses, dict):
        statuses = {}
    if not isinstance(missing_data, list):
        missing_data = []

    providers = []
    for provider, status_key, config_key, aliases in PROVIDERS:
        configured = _configured(config, config_key)
        observed = _observed_status(statuses.get(status_key), configured)
        reason = _reason_for(missing_data, aliases)
        if not configured and reason is None:
            reason = f"{config_key} is not configured" if config_key else None
        providers.append(
            {
                "provider": provider,
                "configured": configured,
                "observedStatus": observed,
                "reason": reason,
            }
        )

    coverage = (
        pack.get("collector_metadata", {}).get("acquisitionCoverage", {})
        if isinstance(pack.get("collector_metadata"), dict)
        else {}
    )
    if isinstance(coverage, dict) and coverage.get("not_attempted_runtime_budget_unique", 0):
        for row in providers:
            if row["provider"] == "Raw Archive":
                row["observedStatus"] = "degraded"
                row["reason"] = (
                    f"technical runtime budget deferred "
                    f"{coverage['not_attempted_runtime_budget_unique']} eligible URLs"
                )
                break

    return {
        "contractVersion": "1.0.0",
        "date": config.target_date,
        "operationalOnly": True,
        "marketEvidence": False,
        "collectorMachineSourceOfTruth": "source_pack.json",
        "providers": providers,
    }


def write_provider_capability_report(
    config: RunConfig,
    pack: dict[str, Any],
) -> dict[str, Any]:
    report = build_provider_capability_report(config, pack)
    write_json(config.output_dir / "provider_capability_report.json", report)
    return report
