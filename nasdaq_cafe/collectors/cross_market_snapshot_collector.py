from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from nasdaq_cafe.cache import read_json, write_json
from nasdaq_cafe.collectors.longbridge_cli_quotes import _authenticated_longbridge, _run_longbridge
from nasdaq_cafe.config import RunConfig, WATCHLIST, missing
from nasdaq_cafe.processing.normalize import utc_now_iso

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


LOOKBACK_DAYS = 14

US_INSTRUMENTS: tuple[tuple[str, str], ...] = (
    ("NASDAQ", ".IXIC.US"),
    ("QQQ", "QQQ.US"),
    ("SOXX", "SOXX.US"),
    *tuple((ticker, f"{ticker}.US") for ticker in WATCHLIST),
)

HK_INSTRUMENTS: tuple[tuple[str, str], ...] = (
    ("Hang Seng", "HSI.HK"),
    ("Hang Seng Tech", "HSTECH.HK"),
)


def collect_cross_market_snapshot(
    config: RunConfig,
    *,
    macro: dict[str, Any] | None = None,
    existing_market_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = config.raw_dir / "cross_market_snapshot.json"
    if path.exists() and not config.refresh:
        cached = read_json(path)
        if isinstance(cached, dict) and cached.get("contract_version") == "cross-market-session-v1.1":
            return {
                "status": cached.get("status", "unknown"),
                "cache_used": True,
                "snapshot": cached,
                "missing_data": cached.get("missing_data", []),
            }

    payload = _build_snapshot(config, macro or {}, existing_market_data or {})
    write_json(path, payload)
    return {
        "status": payload.get("status", "unknown"),
        "cache_used": False,
        "snapshot": payload,
        "missing_data": payload.get("missing_data", []),
    }


def _build_snapshot(config: RunConfig, macro: dict[str, Any], existing_market_data: dict[str, Any]) -> dict[str, Any]:
    missing_data: list[dict[str, str]] = []
    markets: dict[str, Any] = {
        "us": [],
        "japan": [],
        "china": [],
        "hong_kong": [],
        "cross_asset": [],
    }
    probe: dict[str, Any] = {
        "provider": "Longbridge CLI",
        "mode": "read_only",
        "japan": "unsupported_or_unverified",
        "china": "exact_index_symbol_not_verified",
        "hong_kong": "HSI.HK/HSTECH.HK documented",
        "us": "existing project symbols",
    }

    executable, auth_error = _authenticated_longbridge()
    us_anchor_date: date | None = None
    us_anchor_reason = ""
    if executable is None:
        us_anchor_reason = auth_error or "Longbridge CLI unavailable."
        missing_data.append(missing("Cross-Market Longbridge", us_anchor_reason, "medium"))
    else:
        anchor_rows, anchor_error = _fetch_daily_rows(executable, ".IXIC.US", config.target_date)
        if anchor_error:
            us_anchor_reason = anchor_error
            missing_data.append(missing("Cross-Market US anchor", anchor_error, "medium"))
        else:
            us_anchor_date = _select_us_anchor_date(anchor_rows, config.target_date)
            if us_anchor_date is None:
                us_anchor_reason = "No completed NASDAQ regular-session daily row before the target morning."
                missing_data.append(missing("Cross-Market US anchor", us_anchor_reason, "medium"))

    if executable is not None and us_anchor_date is not None:
        for instrument, symbol in US_INSTRUMENTS:
            rows, error = _fetch_daily_rows(executable, symbol, config.target_date)
            markets["us"].append(
                _entry_from_rows(
                    market="us",
                    instrument=instrument,
                    symbol=symbol,
                    rows=rows,
                    target_session_date=us_anchor_date,
                    timezone_name="America/New_York",
                    relation="overnight_us_session",
                    error=error,
                    exact_date=True,
                )
            )
        for instrument, symbol in HK_INSTRUMENTS:
            rows, error = _fetch_daily_rows(executable, symbol, config.target_date)
            markets["hong_kong"].append(
                _entry_from_rows(
                    market="hong_kong",
                    instrument=instrument,
                    symbol=symbol,
                    rows=rows,
                    target_session_date=us_anchor_date,
                    timezone_name="Asia/Hong_Kong",
                    relation="preceding_us_session",
                    error=error,
                    exact_date=False,
                )
            )
    else:
        reason = us_anchor_reason or "US session anchor unavailable."
        for instrument, symbol in US_INSTRUMENTS:
            markets["us"].append(_unavailable_entry("us", instrument, symbol, "America/New_York", "overnight_us_session", reason))
        for instrument, symbol in HK_INSTRUMENTS:
            markets["hong_kong"].append(_unavailable_entry("hong_kong", instrument, symbol, "Asia/Hong_Kong", "preceding_us_session", reason))

    markets["japan"].extend(
        [
            _unavailable_entry(
                "japan",
                "Nikkei 225",
                "",
                "Asia/Tokyo",
                "preceding_us_session",
                "Longbridge capability probe does not expose a verified Japan index symbol; no substitute was used.",
            ),
            _unavailable_entry(
                "japan",
                "TOPIX",
                "",
                "Asia/Tokyo",
                "preceding_us_session",
                "Longbridge capability probe does not expose a verified Japan index symbol; no substitute was used.",
            ),
        ]
    )
    markets["china"].append(
        _unavailable_entry(
            "china",
            "CSI 300 or Shanghai Composite",
            "",
            "Asia/Shanghai",
            "preceding_us_session",
            "Exact Longbridge China index symbol was not verified by the read-only capability probe; no symbol was guessed.",
        )
    )

    markets["cross_asset"].append(_fred_dgs10_entry(macro.get("DGS10")))
    markets["cross_asset"].append(_existing_quote_entry("USDJPY", existing_market_data.get("USDJPY")))
    markets["cross_asset"].append(
        _unavailable_entry("cross_asset", "USDCNH", "", "UTC", "at_or_before_us_session", "No verified provider/symbol is configured for USDCNH in v1.1.")
    )
    markets["cross_asset"].append(
        _unavailable_entry("cross_asset", "crude", "", "UTC", "at_or_before_us_session", "No verified provider/symbol is configured for crude in v1.1.")
    )

    unavailable_count = sum(
        1
        for group in markets.values()
        for item in group
        if isinstance(item, dict) and item.get("status") == "unavailable"
    )
    available_count = sum(
        1
        for group in markets.values()
        for item in group
        if isinstance(item, dict) and item.get("status") == "available"
    )
    status = "ok" if available_count and not unavailable_count else ("partial" if available_count else "unavailable")

    return {
        "contract_version": "cross-market-session-v1.1",
        "target_date_jst": config.target_date,
        "generated_at": utc_now_iso(),
        "definition": (
            "For Asia markets, use the most recent completed regular session that ended before the overnight US regular session. "
            "Local session dates are preserved and may differ across markets."
        ),
        "us_anchor_market_date_local": us_anchor_date.isoformat() if us_anchor_date else None,
        "status": status,
        "probe": probe,
        "markets": markets,
        "available_count": available_count,
        "unavailable_count": unavailable_count,
        "missing_data": missing_data,
    }


def _fetch_daily_rows(executable: Any, symbol: str, target_date: str) -> tuple[list[dict[str, Any]], str]:
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return [], "Invalid target date."
    start = (target - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end = target.isoformat()
    command = [
        str(executable),
        "kline",
        "history",
        symbol,
        "--period",
        "1d",
        "--start",
        start,
        "--end",
        end,
        "--format",
        "json",
    ]
    result = _run_longbridge(command)
    if result.get("returncode") != 0:
        return [], f"Longbridge daily K-line history failed for {symbol}."
    try:
        payload = json.loads(result.get("stdout") or "[]")
    except json.JSONDecodeError:
        return [], f"Longbridge daily K-line history did not return JSON for {symbol}."
    if not isinstance(payload, list):
        return [], f"Longbridge daily K-line history root is not an array for {symbol}."
    rows = [row for row in payload if isinstance(row, dict) and row.get("time") is not None and row.get("close") is not None]
    return rows, "" if rows else f"Longbridge daily K-line history returned zero rows for {symbol}."


def _select_us_anchor_date(rows: list[dict[str, Any]], target_date: str) -> date | None:
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    latest_allowed = target - timedelta(days=1)
    dated = [(_row_local_date(row, "America/New_York"), row) for row in rows]
    valid_dates = [row_date for row_date, _ in dated if row_date is not None and row_date <= latest_allowed]
    return max(valid_dates) if valid_dates else None


def _entry_from_rows(
    *,
    market: str,
    instrument: str,
    symbol: str,
    rows: list[dict[str, Any]],
    target_session_date: date,
    timezone_name: str,
    relation: str,
    error: str,
    exact_date: bool,
) -> dict[str, Any]:
    if error:
        return _unavailable_entry(market, instrument, symbol, timezone_name, relation, error)
    dated: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        row_date = _row_local_date(row, timezone_name)
        if row_date is not None:
            dated.append((row_date, row))
    if exact_date:
        eligible = [(row_date, row) for row_date, row in dated if row_date == target_session_date]
    else:
        eligible = [(row_date, row) for row_date, row in dated if row_date <= target_session_date]
    if not eligible:
        return _unavailable_entry(
            market,
            instrument,
            symbol,
            timezone_name,
            relation,
            f"No completed regular-session daily row eligible before US session {target_session_date.isoformat()}.",
        )
    eligible.sort(key=lambda pair: pair[0])
    selected_date, selected = eligible[-1]
    all_before_or_equal = sorted([(row_date, row) for row_date, row in dated if row_date <= selected_date], key=lambda pair: pair[0])
    previous = all_before_or_equal[-2][1] if len(all_before_or_equal) >= 2 else None
    close = _float(selected.get("close"))
    previous_close = _float(previous.get("close")) if isinstance(previous, dict) else None
    change_percent = None
    if close is not None and previous_close not in (None, 0):
        change_percent = (close - previous_close) / previous_close * 100.0
    return {
        "status": "available",
        "market": market,
        "instrument": instrument,
        "market_date_local": selected_date.isoformat(),
        "timezone": timezone_name,
        "session": "regular",
        "session_status": "completed",
        "relation_to_us_session": relation,
        "price": close,
        "change_percent": change_percent,
        "source": "Longbridge",
        "source_symbol": symbol,
        "provider_timestamp": str(selected.get("time") or ""),
    }


def _unavailable_entry(
    market: str,
    instrument: str,
    symbol: str,
    timezone_name: str,
    relation: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "market": market,
        "instrument": instrument,
        "market_date_local": None,
        "timezone": timezone_name,
        "session": "regular" if market != "cross_asset" else "observation",
        "session_status": "unavailable",
        "relation_to_us_session": relation,
        "price": None,
        "change_percent": None,
        "source": "Longbridge" if market != "cross_asset" else "unavailable",
        "source_symbol": symbol or None,
        "reason": reason,
    }


def _fred_dgs10_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("value") in (None, "") or not value.get("date"):
        return _unavailable_entry("cross_asset", "US10Y", "DGS10", "America/New_York", "at_or_before_us_session", "FRED DGS10 observation unavailable.")
    return {
        "status": "available",
        "market": "cross_asset",
        "instrument": "US10Y",
        "market_date_local": str(value.get("date")),
        "timezone": "America/New_York",
        "session": "daily_observation",
        "session_status": "completed_observation",
        "relation_to_us_session": "at_or_before_us_session",
        "price": _float(value.get("value")),
        "change_percent": None,
        "source": "FRED",
        "source_symbol": "DGS10",
        "note": "FRED daily observation; not an intraday market quote.",
    }


def _existing_quote_entry(instrument: str, quote: Any) -> dict[str, Any]:
    if not isinstance(quote, dict) or quote.get("price") in (None, ""):
        return _unavailable_entry("cross_asset", instrument, "", "UTC", "at_or_before_us_session", f"Existing {instrument} quote is unavailable.")
    timestamp = None
    regular = quote.get("regular")
    if isinstance(regular, dict) and isinstance(regular.get("data"), dict):
        timestamp = regular["data"].get("timestamp")
    return {
        "status": "available",
        "market": "cross_asset",
        "instrument": instrument,
        "market_date_local": _timestamp_date_string(timestamp),
        "timezone": "UTC",
        "session": str(quote.get("session") or "unknown"),
        "session_status": "observed",
        "relation_to_us_session": "at_or_before_us_session",
        "price": _float(quote.get("price")),
        "change_percent": _float(quote.get("change_percent")),
        "source": str(quote.get("raw_source") or "Longbridge"),
        "source_symbol": quote.get("source_symbol"),
    }


def _row_local_date(row: dict[str, Any], timezone_name: str) -> date | None:
    parsed = _parse_timestamp(row.get("time"))
    if parsed is None:
        return None
    if ZoneInfo is None:
        return parsed.date()
    try:
        return parsed.astimezone(ZoneInfo(timezone_name)).date()
    except Exception:
        return parsed.date()


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp_date_string(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.date().isoformat() if parsed else None


def _float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
