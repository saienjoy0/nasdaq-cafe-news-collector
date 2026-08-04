from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

import requests

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.config import RunConfig, missing
from nasdaq_cafe.processing.normalize import utc_now_iso


SEC_CIKS = {
    "NVDA": "0001045810",
    "MSFT": "0000789019",
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "AVGO": "0001730168",
    "TSM": "0001046179",
    "AMD": "0000002488",
    "TSLA": "0001318605",
}
TARGET_FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F"}


def collect_sec_ir(config: RunConfig) -> dict[str, Any]:
    path = config.raw_dir / "sec_ir.json"
    user_agent = str(config.env.get("SEC_USER_AGENT") or "").strip()

    def fetcher() -> dict[str, Any]:
        if not user_agent:
            return {
                "source": "SEC EDGAR / company IR",
                "schema_version": 2,
                "status": "skipped",
                "reason": "SEC_USER_AGENT is not set; SEC requests were not sent.",
                "generated_at": utc_now_iso(),
                "items": [],
                "submissions": {},
                "company_status": [],
            }

        lookback_days = _positive_int(config.env.get("NASDAQ_CAFE_SEC_LOOKBACK_DAYS"), 7, 90)
        max_filings = _positive_int(config.env.get("NASDAQ_CAFE_SEC_MAX_FILINGS_PER_COMPANY"), 100, 500)
        target = _target_date(config.target_date)
        start = target - timedelta(days=lookback_days)
        end = target + timedelta(days=1)
        items: list[dict[str, Any]] = []
        submissions: dict[str, Any] = {}
        company_status: list[dict[str, Any]] = []

        headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        }
        for ticker, cik in SEC_CIKS.items():
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            try:
                response = requests.get(url, headers=headers, timeout=20)
                status_code = int(response.status_code)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                company_status.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                time.sleep(0.15)
                continue

            submissions[ticker] = payload
            filings = _filing_records(payload, ticker, cik, start, end)
            truncated = len(filings) > max_filings
            items.extend(filings[:max_filings])
            company_status.append(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "status": "ok",
                    "http_status": status_code,
                    "eligible_filing_count": len(filings),
                    "registered_filing_count": min(len(filings), max_filings),
                    "truncated": truncated,
                    "truncation_reason": "configured SEC filing limit" if truncated else None,
                }
            )
            time.sleep(0.15)

        ok_count = sum(1 for item in company_status if item.get("status") == "ok")
        return {
            "source": "SEC EDGAR / company IR",
            "schema_version": 2,
            "status": "ok" if ok_count == len(SEC_CIKS) else ("partial" if ok_count else "error"),
            "generated_at": utc_now_iso(),
            "collection_window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "lookback_days": lookback_days,
            },
            "items": items,
            "submissions": submissions,
            "company_status": company_status,
            "implementation_note": "SEC submissions use a declared User-Agent, throttled requests, and read-only public filing data.",
        }

    cached = read_json(path)
    should_fetch = config.refresh or not isinstance(cached, dict)
    if user_agent and isinstance(cached, dict):
        should_fetch = should_fetch or cached.get("status") == "skipped" or cached.get("schema_version") != 2
    if should_fetch:
        payload = fetcher()
        write_json(path, payload)
        cache_used = False
    else:
        payload = cached
        cache_used = True
    missing_data = []
    if payload.get("status") != "ok":
        missing_data.append(
            missing(
                "SEC EDGAR / company IR",
                payload.get("reason", f"SEC collector status: {payload.get('status', 'unknown')}"),
                "low",
            )
        )
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": cache_used,
        "raw": payload,
        "items": payload.get("items", []),
        "missing_data": missing_data,
    }


def _filing_records(
    payload: dict[str, Any],
    ticker: str,
    cik: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return []
    columns = {
        key: value
        for key, value in recent.items()
        if isinstance(value, list)
    }
    size = max((len(value) for value in columns.values()), default=0)
    output: list[dict[str, Any]] = []
    cik_numeric = str(int(cik))
    for index in range(size):
        form = str(_at(columns.get("form"), index) or "")
        filing_date = str(_at(columns.get("filingDate"), index) or "")
        parsed_date = _parse_date(filing_date)
        if form not in TARGET_FORMS or parsed_date is None or not (start <= parsed_date <= end):
            continue
        accession = str(_at(columns.get("accessionNumber"), index) or "")
        primary_document = str(_at(columns.get("primaryDocument"), index) or "")
        if not accession or not primary_document:
            continue
        accession_compact = accession.replace("-", "")
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_numeric}/{accession_compact}/{primary_document}"
        output.append(
            {
                "title": f"{ticker} {form} filed {filing_date}",
                "source": "SEC EDGAR",
                "publisher": payload.get("name") or ticker,
                "published_at": filing_date,
                "url": filing_url,
                "form": form,
                "ticker": ticker,
                "cik": cik,
                "accession_number": accession,
                "primary_document": primary_document,
                "report_date": _at(columns.get("reportDate"), index),
                "filing_date": filing_date,
            }
        )
    return output


def _at(values: list[Any] | None, index: int) -> Any:
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _target_date(value: str) -> date:
    parsed = _parse_date(value)
    return parsed or date.today()


def _positive_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(str(value or default))
    except (TypeError, ValueError):
        parsed = default
    return min(max(1, parsed), maximum)
