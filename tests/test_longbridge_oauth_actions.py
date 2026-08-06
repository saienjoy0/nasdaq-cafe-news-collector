from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "daily-nasdaq-cafe.yml"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTH_VALIDATOR = _load_module(
    "validate_longbridge_auth_status",
    ROOT / "scripts" / "validate_longbridge_auth_status.py",
)
CHECK_VALIDATOR = _load_module(
    "validate_longbridge_check",
    ROOT / "scripts" / "validate_longbridge_check.py",
)
PREPARER = _load_module(
    "prepare_longbridge_oauth_session",
    ROOT / "scripts" / "prepare_longbridge_oauth_session.py",
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class LongbridgePortableOAuthTests(unittest.TestCase):
    def test_refresh_uses_public_client_and_accepts_rotated_token(self) -> None:
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["timeout"] = timeout
            return _FakeResponse(
                {
                    "access_token": "access-new",
                    "refresh_token": "refresh-new",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )

        token = PREPARER.refresh_oauth_token(
            "client-public",
            "refresh-old",
            opener=opener,
        )

        self.assertEqual(PREPARER.TOKEN_ENDPOINT, captured["url"])
        self.assertIn("grant_type=refresh_token", captured["body"])
        self.assertIn("client_id=client-public", captured["body"])
        self.assertIn("refresh_token=refresh-old", captured["body"])
        self.assertNotIn("client_secret", captured["body"])
        self.assertEqual("refresh-new", token["refresh_token"])

    def test_refresh_falls_back_when_server_does_not_rotate(self) -> None:
        token = PREPARER.refresh_oauth_token(
            "client-public",
            "refresh-current",
            opener=lambda request, timeout: _FakeResponse(
                {
                    "access_token": "access-new",
                    "expires_in": 3600,
                }
            ),
        )
        self.assertEqual("refresh-current", token["refresh_token"])

    def test_cli_session_is_plaintext_and_current_runner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            registration_path, auth_path = PREPARER.write_cli_session(
                home=home,
                client_id="client-id",
                token={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_in": 120,
                },
                now=1000,
            )

            registration = json.loads(registration_path.read_text(encoding="utf-8"))
            session = json.loads(auth_path.read_text(encoding="utf-8"))

            self.assertEqual("client-id", registration["client_id"])
            self.assertEqual("client-id", session["client_id"])
            self.assertEqual("access-token", session["access_token"])
            self.assertEqual("refresh-token", session["refresh_token"])
            self.assertEqual(1120, session["expires_at"])
            self.assertEqual(1000, session["logged_in_at"])


class LongbridgeOAuthValidatorTests(unittest.TestCase):
    def test_auth_status_accepts_reusable_paper_token_with_us_quotes(self) -> None:
        summary = AUTH_VALIDATOR.validate_auth_status(
            {
                "token": {"status": "valid", "path": "/secret/path"},
                "account": {
                    "account_channel": "lb_papertrading",
                    "activated_packages": [
                        {"package_key": "US_QBBO_OpenAPI"},
                        {"package_key": "HK_Basic"},
                    ],
                },
            }
        )
        self.assertEqual("lb_papertrading", summary["account_channel"])
        self.assertNotIn("path", summary)

    def test_auth_status_rejects_live_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "paper-trading"):
            AUTH_VALIDATOR.validate_auth_status(
                {
                    "token": {"status": "valid"},
                    "account": {
                        "account_channel": "lb_live",
                        "activated_packages": [{"package_key": "US_QBBO_OpenAPI"}],
                    },
                }
            )

    def test_auth_status_requires_us_openapi_quote_package(self) -> None:
        with self.assertRaisesRegex(ValueError, "US OpenAPI quote package"):
            AUTH_VALIDATOR.validate_auth_status(
                {
                    "token": {"status": "valid"},
                    "account": {
                        "account_channel": "lb_papertrading",
                        "activated_packages": [{"package_key": "HK_Basic"}],
                    },
                }
            )

    def test_check_accepts_one_reachable_endpoint(self) -> None:
        summary = CHECK_VALIDATOR.validate_check(
            {
                "connectivity": {
                    "cn": {"ok": False},
                    "global": {"ok": True, "ms": 120},
                },
                "region": {"active": "Global"},
                "session": {"token": "valid", "detail": "safe detail"},
            }
        )
        self.assertEqual(["global"], summary["reachable_endpoints"])
        self.assertEqual("Global", summary["active_region"])


class LongbridgeOAuthWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_uses_portable_oauth_secrets(self) -> None:
        self.assertIn("secrets.LONGBRIDGE_OAUTH_CLIENT_ID", self.workflow)
        self.assertIn("secrets.LONGBRIDGE_OAUTH_REFRESH_TOKEN", self.workflow)
        self.assertNotIn("secrets.LONGBRIDGE_CLI_AUTH_B64", self.workflow)
        self.assertIn("scripts/prepare_longbridge_oauth_session.py", self.workflow)
        self.assertNotIn("longbridge auth login", self.workflow)

    def test_rotator_write_is_proven_before_oauth_refresh(self) -> None:
        rotate_call = "gh secret set LONGBRIDGE_OAUTH_REFRESH_TOKEN"
        prepare_call = "python scripts/prepare_longbridge_oauth_session.py"
        first_rotate = self.workflow.index(rotate_call)
        prepare = self.workflow.index(prepare_call)
        second_rotate = self.workflow.index(rotate_call, first_rotate + 1)
        self.assertLess(first_rotate, prepare)
        self.assertGreater(second_rotate, prepare)

    def test_rotated_refresh_token_is_persisted_before_validation_and_collection(self) -> None:
        rotate_call = "gh secret set LONGBRIDGE_OAUTH_REFRESH_TOKEN"
        validator_call = "python scripts/validate_longbridge_auth_status.py"
        collect_call = "python -m nasdaq_cafe.run collect"
        final_rotate = self.workflow.rindex(rotate_call)
        self.assertLess(final_rotate, self.workflow.index(validator_call))
        self.assertLess(final_rotate, self.workflow.index(collect_call))

    def test_workflow_is_serialized_to_prevent_refresh_races(self) -> None:
        self.assertIn("group: daily-nasdaq-cafe", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_workflow_never_uploads_oauth_material(self) -> None:
        upload_section = self.workflow.split("- name: Upload safe daily package", 1)[1]
        upload_section = upload_section.split("- name: Remove temporary", 1)[0]
        self.assertNotIn(".longbridge", upload_section)
        self.assertNotIn("refresh-token", upload_section)
        self.assertNotIn("RUNNER_TEMP", upload_section)

    def test_workflow_does_not_enable_longbridge_trade_commands(self) -> None:
        forbidden = (
            "longbridge order",
            "longbridge positions",
            "longbridge portfolio",
            "longbridge assets",
            "longbridge trade",
        )
        for command in forbidden:
            self.assertNotIn(command, self.workflow)


if __name__ == "__main__":
    unittest.main()
