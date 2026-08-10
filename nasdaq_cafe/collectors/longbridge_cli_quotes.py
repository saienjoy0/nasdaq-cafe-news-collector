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
        return _missing_result("Longbridge CLI intraday command failed.")

    try:
        rows = json.loads(intraday["stdout"] or "[]")
    except json.JSONDecodeError:
        return _missing_result("Longbridge CLI intraday command did not return JSON.")
    if not isinstance(rows, list):
        return _missing_result("Longbridge CLI intraday JSON root must be an array.")

    try:
        points = [_normalize_intraday_row(row) for row in rows]
    except (KeyError, TypeError, ValueError) as exc:
        return _missing_result(f"Longbridge intraday row validation failed: {exc}")

    points.sort(key=lambda item: item["timestamp"])
    seen: set[str] = set()
    for point in points:
        timestamp = point["timestamp"]
        if timestamp in seen:
            return _missing_result("Longbridge intraday returned duplicate minute timestamps.")
        seen.add(timestamp)

    return {
        "status": "fetched",
        "cache_used": False,
        "rawRows": rows,
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
            "points": points,
        },
        "missing_data": [],
    }


def _normalize_intraday_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError("row must be an object")
    raw_time = str(row["time"]).strip()
    parsed = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return {
        "timestamp": parsed.isoformat().replace("+00:00", "Z"),
        "price": float(str(row["price"])),
        "avgPrice": float(str(row["avg_price"])),
        "volume": int(str(row["volume"])),
        "turnover": float(str(row["turnover"])),
    }


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
    # Safety gate: read-only quote access plus exact intraday retrieval only.
    command_text = " ".join(command[1:3]) if len(command) >= 3 else ""
    if command[1:3] == ["auth", "status"]:
        allowed = "auth status"
    elif len(command) >= 2 and command[1] == "quote":
        allowed = "quote"
    elif len(command) >= 2 and command[1] == "intraday":
        allowed = "intraday"
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
