from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

TOKEN_ENDPOINT = "https://openapi.longbridge.com/oauth2/token"


class OAuthRefreshError(RuntimeError):
    """Raised when a portable Longbridge OAuth session cannot be prepared."""


def refresh_oauth_token(
    client_id: str,
    refresh_token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 30,
) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not print the response body because OAuth servers may include
        # credential-adjacent diagnostics.
        raise OAuthRefreshError(f"Longbridge OAuth refresh failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise OAuthRefreshError("Longbridge OAuth token endpoint was unreachable.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthRefreshError("Longbridge OAuth token endpoint returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise OAuthRefreshError("Longbridge OAuth token response was not an object.")

    access_token = payload.get("access_token")
    next_refresh_token = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")

    if not isinstance(access_token, str) or not access_token:
        raise OAuthRefreshError("Longbridge OAuth response did not include an access token.")
    if not isinstance(next_refresh_token, str) or not next_refresh_token:
        raise OAuthRefreshError("Longbridge OAuth response did not include a reusable refresh token.")
    try:
        expires_in_seconds = int(expires_in)
    except (TypeError, ValueError) as exc:
        raise OAuthRefreshError("Longbridge OAuth response did not include a valid expires_in value.") from exc
    if expires_in_seconds <= 0:
        raise OAuthRefreshError("Longbridge OAuth access-token lifetime was not positive.")

    return {
        "access_token": access_token,
        "refresh_token": next_refresh_token,
        "expires_in": expires_in_seconds,
        "token_type": str(payload.get("token_type") or "Bearer"),
    }


def write_cli_session(
    *,
    home: Path,
    client_id: str,
    token: dict[str, Any],
    now: int | None = None,
) -> tuple[Path, Path]:
    issued_at = int(time.time()) if now is None else int(now)
    auth_dir = home / ".longbridge" / "openapi"
    auth_dir.mkdir(parents=True, exist_ok=True)

    registration_path = auth_dir / "cli-registration"
    auth_path = auth_dir / "cli-auth"

    registration = {
        "client_id": client_id,
        "registration_access_token": None,
        "registration_client_uri": None,
    }
    session = {
        "client_id": client_id,
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "expires_at": issued_at + int(token["expires_in"]),
        "logged_in_at": issued_at,
    }

    registration_path.write_text(
        json.dumps(registration, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    auth_path.write_text(
        json.dumps(session, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    for path in (registration_path, auth_path):
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows and some containers may not expose POSIX mode bits.
            pass

    return registration_path, auth_path


def main() -> int:
    client_id = os.getenv("LONGBRIDGE_OAUTH_CLIENT_ID", "").strip()
    refresh_token = os.getenv("LONGBRIDGE_OAUTH_REFRESH_TOKEN", "").strip()
    next_refresh_path_raw = os.getenv("LONGBRIDGE_NEXT_REFRESH_TOKEN_PATH", "").strip()

    if not client_id or not refresh_token:
        raise OAuthRefreshError(
            "LONGBRIDGE_OAUTH_CLIENT_ID and LONGBRIDGE_OAUTH_REFRESH_TOKEN are required."
        )
    if not next_refresh_path_raw:
        raise OAuthRefreshError("LONGBRIDGE_NEXT_REFRESH_TOKEN_PATH is required.")

    token = refresh_oauth_token(client_id, refresh_token)
    registration_path, auth_path = write_cli_session(
        home=Path.home(),
        client_id=client_id,
        token=token,
    )

    next_refresh_path = Path(next_refresh_path_raw)
    next_refresh_path.parent.mkdir(parents=True, exist_ok=True)
    next_refresh_path.write_text(token["refresh_token"], encoding="utf-8")
    try:
        next_refresh_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    safe_summary = {
        "status": "prepared",
        "token_endpoint": TOKEN_ENDPOINT,
        "registration_path": str(registration_path),
        "auth_path": str(auth_path),
        "refresh_token_rotated": token["refresh_token"] != refresh_token,
        "expires_in": token["expires_in"],
        "contains_token_values": False,
    }
    print(json.dumps(safe_summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
