from __future__ import annotations

import importlib.util
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

    def test_auth_status_accepts_refresh_pending(self) -> None:
        summary = AUTH_VALIDATOR.validate_auth_status(
            {
                "token": {"status": "refresh pending"},
                "account": {
                    "account_channel": "lb_papertrading",
                    "activated_packages": [{"package_key": "US_QBBO_OpenAPI"}],
                },
            }
        )
        self.assertEqual("refresh pending", summary["token_status"])

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

    def test_check_rejects_no_connectivity(self) -> None:
        with self.assertRaisesRegex(ValueError, "any configured OpenAPI endpoint"):
            CHECK_VALIDATOR.validate_check(
                {
                    "connectivity": {
                        "cn": {"ok": False},
                        "global": {"ok": False},
                    },
                    "session": {"token": "valid"},
                }
            )


class LongbridgeOAuthWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_restores_only_the_expected_oauth_secret(self) -> None:
        self.assertIn("secrets.LONGBRIDGE_CLI_AUTH_B64", self.workflow)
        self.assertIn('$HOME/.longbridge/openapi/cli-auth', self.workflow)
        self.assertIn("base64 --decode", self.workflow)
        self.assertNotIn("longbridge auth login", self.workflow)

    def test_workflow_enforces_paper_account_before_collection(self) -> None:
        validator_call = "python scripts/validate_longbridge_auth_status.py"
        collect_call = "python -m nasdaq_cafe.run collect"
        self.assertIn(validator_call, self.workflow)
        self.assertLess(self.workflow.index(validator_call), self.workflow.index(collect_call))

    def test_workflow_rotates_only_the_longbridge_auth_secret(self) -> None:
        self.assertIn("secrets.LONGBRIDGE_SECRET_ROTATOR_TOKEN", self.workflow)
        self.assertIn(
            "gh secret set LONGBRIDGE_CLI_AUTH_B64",
            self.workflow,
        )
        self.assertNotIn("gh secret delete", self.workflow)

    def test_workflow_never_uploads_oauth_material(self) -> None:
        upload_section = self.workflow.split("- name: Upload safe daily package", 1)[1]
        self.assertNotIn(".longbridge", upload_section)
        self.assertNotIn("cli-auth", upload_section)
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
