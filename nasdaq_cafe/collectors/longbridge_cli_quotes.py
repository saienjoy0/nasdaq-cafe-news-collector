from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from nasdaq_cafe.cache import write_json
from nasdaq_cafe.config import LONGBRIDGE_FIXED_SYMBOLS, RunConfig, missing
from nasdaq_cafe.processing.normalize import utc_now_iso


QUOTE_RAW_FILE = "longbridge_quotes.json"
SAFE_COMMANDS = {
    "auth status",
    "quote",
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

    executable = _find_longbridge_executable()
    if executable is None:
        return _missing_result("Longbridge CLI executable was not found.")

    auth = _run_longbridge([str(executable), "auth", "status", "--format", "json"])
    if auth["returncode"] != 0:
        return _missing_result("Longbridge CLI auth status failed.")

    try:
        auth_payload = json.loads(auth["stdout"] or "{}")
    except json.JSONDecodeError:
        return _missing_result("Longbridge CLI auth status did not return JSON.")

    token_status = (auth_payload.get("token") or {}).get("status")
    if token_status != "valid":
        return _missing_result("Longbridge CLI auth token is not valid. User authentication is required.")

    symbols = list(LONGBRIDGE_FIXED_SYMBOLS)
    quote = _run_longbridge([str(executable), "quote", *symbols, "--format", "json"])
    if quote["returncode"] != 0:
        return _missing_result("Longbridge CLI quote command failed.")

    try:
        items = json.loads(quote["stdout"] or "[]")
    except json.JSONDecodeError:
        return _missing_result("Longbridge CLI quote command did not return JSON.")

    payload = {
        "source": "Longbridge",
        "kind": "quotes",
        "fetched_by": "longbridge-cli",
        "generated_at": utc_now_iso(),
        "symbols": symbols,
        "items": items,
    }
    write_json(raw_path, payload)
    return {
        "status": "fetched",
        "cache_used": False,
        "raw_path": str(raw_path),
        "missing_data": [],
    }


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
    # Safety gate: Phase 1.6 only allows auth-status checks and quote reads.
    command_text = " ".join(command[1:3]) if len(command) >= 3 else ""
    if command[1:3] == ["auth", "status"]:
        allowed = "auth status"
    elif len(command) >= 2 and command[1] == "quote":
        allowed = "quote"
    else:
        raise ValueError(f"Blocked non-quote Longbridge command: {command_text}")
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
