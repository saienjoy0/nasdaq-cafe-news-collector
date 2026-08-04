from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from nasdaq_cafe.cache import load_or_fetch, write_json
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.normalize import utc_now_iso


FRED_SERIES = [
    "DGS2",
    "DGS10",
    "DGS30",
    "T10Y2Y",
    "T10Y3M",
    "DFII10",
    "T10YIE",
    "VIXCLS",
    "DTWEXBGS",
    "DCOILWTICO",
    "DCOILBRENTEU",
]


def collect_fred_dgs10(config: RunConfig) -> dict[str, Any]:
    """Compatibility entry point that now collects the configured FRED series set."""
    path = config.raw_dir / "fred_series.json"
    compatibility_path = config.raw_dir / "fred_dgs10.json"
    api_key = config.env.get("FRED_API_KEY", "").strip()

    def fetcher() -> dict[str, Any]:
        if not api_key:
            return {
                "source": "FRED",
                "status": "skipped",
                "reason": "FRED_API_KEY is not set.",
                "generated_at": utc_now_iso(),
                "series": {
                    series_id: {
                        "series_id": series_id,
                        "status": "skipped",
                        "reason": "FRED_API_KEY is not set.",
                        "observations": [],
                        "latest": None,
                    }
                    for series_id in FRED_SERIES
                },
            }

        series_results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(_fetch_series, api_key, series_id): series_id
                for series_id in FRED_SERIES
            }
            for future in as_completed(future_map):
                series_id = future_map[future]
                try:
                    series_results[series_id] = future.result()
                except Exception as exc:
                    series_results[series_id] = {
                        "series_id": series_id,
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "observations": [],
                        "latest": None,
                    }
        ok_count = sum(1 for value in series_results.values() if value.get("status") == "ok")
        return {
            "source": "FRED",
            "status": "ok" if ok_count == len(FRED_SERIES) else ("partial" if ok_count else "error"),
            "generated_at": utc_now_iso(),
            "series_count": len(FRED_SERIES),
            "ok_count": ok_count,
            "series": {series_id: series_results[series_id] for series_id in FRED_SERIES},
        }

    payload, cache_used = load_or_fetch(path, config.refresh, fetcher)
    dgs10 = payload.get("series", {}).get("DGS10", {}) if isinstance(payload, dict) else {}
    write_json(
        compatibility_path,
        {
            "source": "FRED",
            "series": "DGS10",
            "status": dgs10.get("status", payload.get("status", "unknown")),
            "reason": dgs10.get("reason", payload.get("reason", "")),
            "generated_at": payload.get("generated_at", utc_now_iso()),
            "observations": dgs10.get("observations", []),
            "latest": dgs10.get("latest"),
            "compatibility_note": "DGS10 subset of raw/fred_series.json",
        },
    )

    missing_data = []
    for series_id in FRED_SERIES:
        result = payload.get("series", {}).get(series_id, {})
        if result.get("status") != "ok":
            missing_data.append(
                missing("FRED", f"{series_id}: {result.get('reason', 'not retrieved')}", "low")
            )
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "value": dgs10.get("latest"),
        "values": {
            series_id: payload.get("series", {}).get(series_id, {}).get("latest")
            for series_id in FRED_SERIES
        },
        "raw": payload,
        "missing_data": missing_data,
    }


def _fetch_series(api_key: str, series_id: str) -> dict[str, Any]:
    response = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 10,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    observations = [
        {"date": item.get("date"), "value": parsed}
        for item in payload.get("observations", [])
        if (parsed := _parse_float(item.get("value"))) is not None
    ]
    latest = observations[0] if observations else None
    return {
        "series_id": series_id,
        "status": "ok" if latest else "empty",
        "reason": "" if latest else "FRED returned no numeric observations.",
        "retrieved_at": utc_now_iso(),
        "observations": observations,
        "latest": latest,
    }


def _parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
