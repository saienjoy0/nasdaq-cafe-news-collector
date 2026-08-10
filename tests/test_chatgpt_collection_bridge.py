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

    def test_request_body_contract_preserves_broad_mode(self) -> None:
        self.assertIn("<!-- nasdaq-cafe-collection-request -->", self.bridge)
        self.assertIn(r"date:\s*(today|\d{4}-\d{2}-\d{2})", self.bridge)
        self.assertIn(r"refresh:\s*(true|false)", self.bridge)
        self.assertIn(r"mode:\s*(broad|followup)", self.bridge)
        self.assertIn('mode = (mode_match.group(1).lower() if mode_match else "broad")', self.bridge)

    def test_followup_json_contract_is_strict_and_bounded(self) -> None:
        self.assertIn("<!-- nasdaq-cafe-research-acquisition-request -->", self.bridge)
        self.assertIn('request.get("contractVersion") != "1.0.0"', self.bridge)
        self.assertIn('request.get("episodeDate") != target_date', self.bridge)
        self.assertIn("wave not in {1, 2}", self.bridge)
        self.assertIn("1 <= len(requests) <= 12", self.bridge)

    def test_permissions_are_minimal_for_dispatch_and_issue_reporting(self) -> None:
        self.assertIn("actions: write", self.bridge)
        self.assertIn("contents: read", self.bridge)
        self.assertIn("issues: write", self.bridge)
        self.assertNotIn("contents: write", self.bridge)

    def test_existing_daily_workflow_is_reused_not_duplicated(self) -> None:
        self.assertIn("TARGET_WORKFLOW: daily-nasdaq-cafe.yml", self.bridge)
        self.assertIn('gh workflow run "$TARGET_WORKFLOW"', self.bridge)
        self.assertNotIn("python -m nasdaq_cafe.run collect", self.bridge)
        self.assertIn("-f mode=\"$MODE\"", self.bridge)
        self.assertIn("-f request_json_b64=\"$REQUEST_JSON_B64\"", self.bridge)

    def test_daily_workflow_keeps_one_broad_collector_entrypoint(self) -> None:
        if self.daily:
            self.assertEqual(1, self.daily.count("python -m nasdaq_cafe.run collect"))
            self.assertEqual(1, self.daily.count("python -m nasdaq_cafe.research_acquisition"))
            self.assertIn("mode:", self.daily)
            self.assertIn("request_json_b64:", self.daily)
            self.assertIn("Scheduled collection is broad-only.", self.daily)

    def test_bridge_reports_dynamic_artifact_contract(self) -> None:
        self.assertIn("previous_run_id=", self.bridge)
        self.assertIn("select(.databaseId > $previous_run_id)", self.bridge)
        self.assertIn('gh run watch "$CHILD_RUN_ID"', self.bridge)
        self.assertIn("<!-- nasdaq-cafe-collection-result -->", self.bridge)
        self.assertIn("artifact_name: $artifact_name", self.bridge)
        self.assertIn("nasdaq-cafe-followup-", self.bridge)

    def test_failure_is_reported_and_issue_remains_open(self) -> None:
        failure_section = self.bridge.split('if [[ "$conclusion" == "success" ]]', 1)[1].split("else", 1)[1]
        self.assertIn("status: failure", failure_section)
        self.assertIn("chatgpt-collect-failed", failure_section)
        self.assertNotIn('gh issue close "$ISSUE_NUMBER"', failure_section)

    def test_child_failure_is_propagated_after_reporting(self) -> None:
        self.assertIn("Propagate collector failure", self.bridge)
        self.assertIn("steps.wait.outputs.conclusion != 'success'", self.bridge)
        report_index = self.bridge.index("Report request result")
        propagate_index = self.bridge.index("Propagate collector failure")
        self.assertLess(report_index, propagate_index)

    def test_longbridge_trade_surface_is_not_added(self) -> None:
        for forbidden in (
            "longbridge order",
            "longbridge positions",
            "longbridge portfolio",
            "longbridge assets",
            "longbridge trade",
        ):
            self.assertNotIn(forbidden, self.bridge)
            self.assertNotIn(forbidden, self.daily)


if __name__ == "__main__":
    unittest.main()
