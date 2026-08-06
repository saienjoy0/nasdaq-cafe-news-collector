from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


VALID_TOKEN_STATES = {"valid", "refresh pending", "refresh_pending"}
REQUIRED_ACCOUNT_CHANNEL = "lb_papertrading"
REQUIRED_US_QUOTE_PACKAGE = "US_QBBO_OpenAPI"


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Longbridge auth status file was not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Longbridge auth status was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Longbridge auth status must be a JSON object.")
    return payload


def validate_auth_status(payload: dict[str, Any]) -> dict[str, str]:
    token = payload.get("token")
    account = payload.get("account")
    if not isinstance(token, dict) or not isinstance(account, dict):
        raise ValueError("Longbridge auth status is missing token or account data.")

    token_status = str(token.get("status") or "").strip().lower()
    if token_status not in VALID_TOKEN_STATES:
        raise ValueError(f"Longbridge OAuth token is not reusable: {token_status or 'missing'}")

    account_channel = str(account.get("account_channel") or "").strip()
    if account_channel != REQUIRED_ACCOUNT_CHANNEL:
        raise ValueError(
            "Longbridge OAuth token is not bound to the required paper-trading account "
            f"({REQUIRED_ACCOUNT_CHANNEL}); got {account_channel or 'missing'}."
        )

    packages = account.get("activated_packages")
    if not isinstance(packages, list):
        raise ValueError("Longbridge auth status is missing activated quote packages.")
    package_keys = {
        str(item.get("package_key") or "")
        for item in packages
        if isinstance(item, dict)
    }
    if REQUIRED_US_QUOTE_PACKAGE not in package_keys:
        raise ValueError(
            "Longbridge paper account does not have the required US OpenAPI quote package "
            f"({REQUIRED_US_QUOTE_PACKAGE})."
        )

    return {
        "token_status": token_status,
        "account_channel": account_channel,
        "us_quote_package": REQUIRED_US_QUOTE_PACKAGE,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_longbridge_auth_status.py <auth-status.json>", file=sys.stderr)
        return 2
    try:
        summary = validate_auth_status(_load_payload(Path(argv[1])))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
