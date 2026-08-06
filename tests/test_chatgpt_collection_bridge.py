from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / ".github" / "workflows" / "chatgpt-collection-request.yml"
DAILY = ROOT / ".github" / "workflows" / "daily-nasdaq-cafe.yml"


class ChatGPTCollectionBridgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = BRIDGE.read_text(encoding="utf-8")
        cls.daily = DAILY.read_text(encoding="utf-8") if DAILY.exists() else ""

    def test_bridge_is_issue_opened_only(self) -> None:
        self.assertIn("issues:\n    types:\n      - opened", self.bridge)
        self.assertNotIn("schedule:", self.bridge)

    def test_only_repository_owner_can_dispatch(self) -> None:
        self.assertIn("github.event.issue.user.login == github.repository_owner", self.bridge)
        self.assertIn("github.event.issue.author_association == 'OWNER'", self.bridge)
        self.assertIn("startsWith(github.event.issue.title, '[NASDAQ Cafe Collect]')", self.bridge)

    def test_request_body_contract_is_strict(self) -> None:
        self.assertIn("<!-- nasdaq-cafe-collection-request -->", self.bridge)
        self.assertIn(r"date:\s*(today|\d{4}-\d{2}-\d{2})", self.bridge)
        self.assertIn(r"refresh:\s*(true|false)", self.bridge)
        self.assertIn('datetime.strptime(target_date, "%Y-%m-%d")', self.bridge)

    def test_permissions_are_minimal_for_dispatch_and_issue_reporting(self) -> None:
        self.assertIn("actions: write", self.bridge)
        self.assertIn("contents: read", self.bridge)
        self.assertIn("issues: write", self.bridge)
        self.assertNotIn("contents: write", self.bridge)

    def test_existing_collector_is_reused_not_duplicated(self) -> None:
        self.assertIn("TARGET_WORKFLOW: daily-nasdaq-cafe.yml", self.bridge)
        self.assertIn('gh workflow run "$TARGET_WORKFLOW"', self.bridge)
        self.assertNotIn("python -m nasdaq_cafe.run collect", self.bridge)
        if self.daily:
            self.assertEqual(1, self.daily.count("python -m nasdaq_cafe.run collect"))

    def test_bridge_waits_for_exact_child_run_and_reports_artifact_contract(self) -> None:
        self.assertIn('gh run watch "$CHILD_RUN_ID"', self.bridge)
        self.assertIn("<!-- nasdaq-cafe-collection-result -->", self.bridge)
        self.assertIn("status: success", self.bridge)
        self.assertIn("run_id: $child_run_id", self.bridge)
        self.assertIn("artifact_name: nasdaq-cafe-$target_date", self.bridge)

    def test_failure_is_reported_and_issue_remains_open(self) -> None:
        failure_section = self.bridge.split('if [[ "$conclusion" == "success" ]]', 1)[1].split("else", 1)[1]
        self.assertIn("status: failure", failure_section)
        self.assertIn("chatgpt-collect-failed", failure_section)
        self.assertNotIn('gh issue close "$ISSUE_NUMBER"', failure_section)

    def test_longbridge_trade_surface_is_not_added(self) -> None:
        for forbidden in (
            "longbridge order",
            "longbridge positions",
            "longbridge portfolio",
            "longbridge assets",
            "longbridge trade",
        ):
            self.assertNotIn(forbidden, self.bridge)


if __name__ == "__main__":
    unittest.main()
