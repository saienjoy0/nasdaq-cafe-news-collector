from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.config import LONGBRIDGE_FIXED_SYMBOLS, RunConfig, missing
from nasdaq_cafe.processing.normalize import utc_now_iso


QUOTE_RAW_FILE = "longbridge_quotes.json"
SAFE_COMMANDS = {
    "auth status",
    "quote",
    "intraday",
    "kline history",
}


def ensure_longbridge_quotes(config: RunConfig) -> dict[str, Any]:
    raw_path = config.raw_dir / QUOTE_RAW_FILE
    if raw_path.exists() and not config.refresh:
        return {
            "status": "cache",
            "cache_used": True,
            "raw_path": str(raw_path),
            "missing_data": [],
        }

    result = fetch_longbridge_quotes(list(LONGBRIDGE_FIXED_SYMBOLS))
    if result["status"] != "fetched":
        return result

    payload = {
        "source": "Longbridge",
        "kind": "quotes",
        "fetched_by": "longbridge-cli",
        "generated_at": utc_now_iso(),
        "symbols": list(LONGBRIDGE_FIXED_SYMBOLS),
        "items": result["items"],
    }
    write_json(raw_path, payload)
    return {
        "status": "fetched",
        "cache_used": False,
        "raw_path": str(raw_path),
        "missing_data": [],
    }


def fetch_longbridge_quotes(symbols: list[str]) -> dict[str, Any]:
    executable, auth_error = _authenticated_longbridge()
    if executable is None:
        return _missing_result(auth_error or "Longbridge CLI is unavailable.")

    quote = _run_longbridge([str(executable), "quote", *symbols, "--format", "json"])
    if quote["returncode"] != 0:
        return _missing_result("Longbridge CLI quote command failed.")

    try:
        items = json.loads(quote["stdout"] or "[]")
    except json.JSONDecodeError:
        return _missing_result("Longbridge CLI quote command did not return JSON.")
    if not isinstance(items, list):
        return _missing_result("Longbridge CLI quote JSON root must be an array.")

    return {
        "status": "fetched",
        "cache_used": False,
        "items": items,
        "missing_data": [],
    }


def fetch_longbridge_intraday(
    *,
    symbol: str,
    target_date: str,
    session: str,
) -> dict[str, Any]:
    """Fetch a date-bound one-minute series without guessing missing data.

    Longbridge CLI v0.26.0 exposes historical intraday through
    `/v1/quote/history-timeshares`.  In the real-day 2026-08-07 acceptance run
    that surface returned a valid JSON object with zero minute rows for QQQ.US,
    while the same authenticated account returned 390 one-minute historical
    K-lines for the same symbol/date.  Preserve the original surface as the
    primary attempt, but fall back to the official read-only 1m K-line history
    command only when the primary surface returns zero rows.

    K-line fallback points use the one-minute candle close as `price`; OHLC is
    retained explicitly so downstream consumers do not mistake the fallback
    for the historical timeshare line surface.
    """
    executable, auth_error = _authenticated_longbridge()
    if executable is None:
        return _missing_result(auth_error or "Longbridge CLI is unavailable.")

    compact_date = datetime.strptime(target_date, "%Y-%m-%d").strftime("%Y%m%d")
    command = [
        str(executable),
        "intraday",
        symbol,
        "--date",
        compact_date,
        "--format",
        "json",
    ]
    if session == "all":
        command.extend(["--session", "all"])

    intraday = _run_longbridge(command)
    if intraday["returncode"] != 0:
        return _missing_result("Longbridge CLI historical intraday command failed.")

    try:
        payload = json.loads(intraday["stdout"] or "[]")
    except json.JSONDecodeError:
        return _missing_result("Longbridge CLI historical intraday command did not return JSON.")

    try:
        rows = _extract_intraday_rows(payload)
        points = [_normalize_intraday_row(row) for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        return _missing_result(f"Longbridge historical intraday row validation failed: {exc}")

    if points:
        duplicate_error = _sort_and_validate_unique_timestamps(points)
        if duplicate_error:
            return _missing_result(duplicate_error)
        return _intraday_success(
            symbol=symbol,
            target_date=target_date,
            session=session,
            points=points,
            raw_rows=payload,
            provider_surface="history-timeshares",
            price_basis="intraday-line-price",
        )

    kline_command = [
        str(executable),
        "kline",
        "history",
        symbol,
        "--period",
        "1m",
        "--start",
        target_date,
        "--end",
        target_date,
    ]
    if session == "all":
        kline_command.extend(["--session", "all"])
    kline_command.extend(["--format", "json"])

    kline = _run_longbridge(kline_command)
    if kline["returncode"] != 0:
        return _missing_result(
            "Longbridge historical intraday returned zero rows and the official 1m K-line history fallback failed."
        )

    try:
        kline_payload = json.loads(kline["stdout"] or "[]")
    except json.JSONDecodeError:
        return _missing_result(
            "Longbridge historical intraday returned zero rows and the 1m K-line history fallback did not return JSON."
        )
    if not isinstance(kline_payload, list):
        return _missing_result(
            "Longbridge 1m K-line history JSON root must be an array when used as historical intraday fallback."
        )

    try:
        kline_points = [_normalize_kline_row(row) for row in kline_payload]
    except (KeyError, TypeError, ValueError) as exc:
        return _missing_result(f"Longbridge 1m K-line fallback row validation failed: {exc}")

    if not kline_points:
        return _missing_result(
            "Longbridge historical intraday and 1m K-line history both returned zero rows for the requested date/session."
        )

    duplicate_error = _sort_and_validate_unique_timestamps(kline_points)
    if duplicate_error:
        return _missing_result(duplicate_error)

    return _intraday_success(
        symbol=symbol,
        target_date=target_date,
        session=session,
        points=kline_points,
        raw_rows={
            "historicalIntraday": payload,
            "fallbackKlineHistory": kline_payload,
        },
        provider_surface="kline-history-fallback",
        price_basis="minute-close",
    )


def _intraday_success(
    *,
    symbol: str,
    target_date: str,
    session: str,
    points: list[dict[str, Any]],
    raw_rows: Any,
    provider_surface: str,
    price_basis: str,
) -> dict[str, Any]:
    return {
        "status": "fetched",
        "cache_used": False,
        "rawRows": raw_rows,
        "series": {
            "source": "Longbridge",
            "kind": "intraday",
            "fetched_by": "longbridge-cli",
            "generated_at": utc_now_iso(),
            "symbol": symbol,
            "marketDate": target_date,
            "timezone": "UTC",
            "session": session,
            "resolution": "1m",
            "precision": "verified-intraday-series",
            "providerSurface": provider_surface,
            "priceBasis": price_basis,
            "points": points,
        },
        "missing_data": [],
    }


def _sort_and_validate_unique_timestamps(points: list[dict[str, Any]]) -> str | None:
    points.sort(key=lambda item: item["timestamp"])
    seen: set[str] = set()
    for point in points:
        timestamp = point["timestamp"]
        if timestamp in seen:
            return "Longbridge minute series returned duplicate minute timestamps."
        seen.add(timestamp)
    return None


def _extract_intraday_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize the two documented Longbridge CLI intraday JSON surfaces."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise TypeError("JSON root must be an array or historical timeshares object")

    timeshares = payload.get("timeshares")
    if not isinstance(timeshares, list):
        raise ValueError("historical intraday JSON must contain timeshares[]")

    rows: list[dict[str, Any]] = []
    for group in timeshares:
        if not isinstance(group, dict):
            raise TypeError("historical timeshare group must be an object")
        minutes = group.get("minutes")
        if minutes is None:
            continue
        if not isinstance(minutes, list):
            raise TypeError("historical timeshare minutes must be an array")
        for minute in minutes:
            if not isinstance(minute, dict):
                raise TypeError("historical intraday minute must be an object")
            rows.append(
                {
                    "time": minute["timestamp"],
                    "price": minute["price"],
                    "avg_price": minute["avg_price"],
                    "volume": minute["amount"],
                    "turnover": minute["balance"],
                }
            )
    return rows


def _parse_intraday_time(raw_time: Any) -> datetime:
    if isinstance(raw_time, bool):
        raise ValueError("boolean intraday timestamp is invalid")
    if isinstance(raw_time, (int, float)):
        return datetime.fromtimestamp(int(raw_time), tz=timezone.utc)

    text = str(raw_time).strip()
    if not text:
        raise ValueError("empty intraday timestamp")
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return datetime.fromtimestamp(int(text), tz=timezone.utc)

    if text.endswith("Z"):
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_intraday_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError("row must be an object")
    parsed = _parse_intraday_time(row["time"])
    return {
        "timestamp": parsed.isoformat().replace("+00:00", "Z"),
        "price": float(str(row["price"])),
        "avgPrice": float(str(row["avg_price"])),
        "volume": int(str(row["volume"])),
        "turnover": float(str(row["turnover"])),
    }


def _normalize_kline_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError("K-line row must be an object")
    parsed = _parse_intraday_time(row["time"])
    close = float(str(row["close"]))
    point = {
        "timestamp": parsed.isoformat().replace("+00:00", "Z"),
        "price": close,
        "open": float(str(row["open"])),
        "high": float(str(row["high"])),
        "low": float(str(row["low"])),
        "close": close,
        "volume": int(str(row["volume"])),
        "turnover": float(str(row["turnover"])),
    }
    session = str(row.get("session") or "").strip()
    if session:
        point["session"] = session
    return point


def _authenticated_longbridge() -> tuple[Path | None, str | None]:
    executable = _find_longbridge_executable()
    if executable is None:
        return None, "Longbridge CLI executable was not found."

    auth = _run_longbridge([str(executable), "auth", "status", "--format", "json"])
    if auth["returncode"] != 0:
        return None, "Longbridge CLI auth status failed."

    try:
        auth_payload = json.loads(auth["stdout"] or "{}")
    except json.JSONDecodeError:
        return None, "Longbridge CLI auth status did not return JSON."

    token_status = (auth_payload.get("token") or {}).get("status")
    if token_status != "valid":
        return None, "Longbridge CLI auth token is not valid. User authentication is required."
    return executable, None


def _find_longbridge_executable() -> Path | None:
    discovered = shutil.which("longbridge")
    if discovered:
        return Path(discovered)

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Programs" / "longbridge" / "longbridge.exe"
        if candidate.exists():
            return candidate
    return None


def _run_longbridge(command: list[str]) -> dict[str, Any]:
    # Safety gate: read-only quote, intraday, and historical K-line retrieval only.
    command_text = " ".join(command[1:3]) if len(command) >= 3 else ""
    if command[1:3] == ["auth", "status"]:
        allowed = "auth status"
    elif len(command) >= 2 and command[1] == "quote":
        allowed = "quote"
    elif len(command) >= 2 and command[1] == "intraday":
        allowed = "intraday"
    elif len(command) >= 3 and command[1:3] == ["kline", "history"]:
        allowed = "kline history"
    else:
        raise ValueError(f"Blocked non-market-data Longbridge command: {command_text}")
    if allowed not in SAFE_COMMANDS:
        raise ValueError(f"Blocked Longbridge command: {command_text}")

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _missing_result(detail: str) -> dict[str, Any]:
    return {
        "status": "missing",
        "cache_used": False,
        "missing_data": [
            missing(
                "Longbridge",
                f"Longbridge CLI / plugin unavailable. SDK credentials not provided yet. Detail: {detail}",
                "medium",
            )
        ],
    }
