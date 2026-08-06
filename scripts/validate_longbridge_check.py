from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


VALID_TOKEN_STATES = {"valid", "refresh pending", "refresh_pending"}


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Longbridge check file was not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Longbridge check output was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Longbridge check output must be a JSON object.")
    return payload


def validate_check(payload: dict[str, Any]) -> dict[str, Any]:
    connectivity = payload.get("connectivity")
    session = payload.get("session")
    region = payload.get("region")
    if not isinstance(connectivity, dict) or not isinstance(session, dict):
        raise ValueError("Longbridge check output is missing connectivity or session data.")

    reachable = [
        name
        for name, item in connectivity.items()
        if isinstance(item, dict) and item.get("ok") is True
    ]
    if not reachable:
        raise ValueError("Longbridge could not reach any configured OpenAPI endpoint.")

    token_status = str(session.get("token") or "").strip().lower()
    if token_status not in VALID_TOKEN_STATES:
        raise ValueError(f"Longbridge API session token is not usable: {token_status or 'missing'}")

    active_region = ""
    if isinstance(region, dict):
        active_region = str(region.get("active") or "")

    return {
        "token_status": token_status,
        "reachable_endpoints": sorted(reachable),
        "active_region": active_region,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_longbridge_check.py <check.json>", file=sys.stderr)
        return 2
    try:
        summary = validate_check(_load_payload(Path(argv[1])))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
