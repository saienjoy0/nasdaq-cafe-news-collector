from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nasdaq_cafe.config import ROOT_DIR
from nasdaq_cafe.outputs.submission_bundle import (
    REGULAR_CONTEXT_MARKER,
    copy_submission_files,
    write_daily_source_package,
)


class SubmissionBundleTests(unittest.TestCase):
    def _output_dir(self, base: Path) -> Path:
        output_dir = base / "2026-07-10"
        output_dir.mkdir(parents=True)
        return output_dir

    def _write_five(self, output_dir: Path, *, alias: str | None = None) -> dict[str, str]:
        normal = "# Normal Handoff\nUNIQUE_MARKET_DATA\n"
        values = {
            "CHATGPT_HANDOFF_2026-07-10.md": normal,
            "chatgpt_handoff.md": normal if alias is None else alias,
            "source_pack.md": "# Source Pack\nUNIQUE_COLLECTION_RECORD\n",
            "prompt_input.md": "# Prompt Input\nUNIQUE_INTERNAL_INSTRUCTIONS\n",
            "CHATGPT_FULLTEXT_HANDOFF_2026-07-10.md": (
                "# Fulltext\nUNIQUE_ARTICLE_BODY\nUNIQUE_RETRIEVAL_FAILURE\n"
                + REGULAR_CONTEXT_MARKER
                + normal
            ),
        }
        for name, content in values.items():
            (output_dir / name).write_text(content, encoding="utf-8")
        return values

    def test_five_files_remain_and_one_deduplicated_package_is_added(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            output_dir = self._output_dir(Path(temp_name))
            values = self._write_five(output_dir)
            copied = copy_submission_files(output_dir)

            package_name = "daily_source_package_2026-07-10.md"
            package = (output_dir / package_name).read_text(encoding="utf-8")
            submission_dir = output_dir / "chatgpt_submission"
            self.assertEqual(6, len(copied))
            self.assertEqual(
                set(values) | {package_name},
                {path.name for path in submission_dir.glob("*.md")},
            )
            for sentinel in (
                "UNIQUE_MARKET_DATA",
                "UNIQUE_COLLECTION_RECORD",
                "UNIQUE_INTERNAL_INSTRUCTIONS",
                "UNIQUE_ARTICLE_BODY",
                "UNIQUE_RETRIEVAL_FAILURE",
            ):
                self.assertEqual(1, package.count(sentinel))
            self.assertIn("exact duplicate", package)

    def test_nonidentical_alias_is_retained(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT_DIR) as temp_name:
            output_dir = self._output_dir(Path(temp_name))
            self._write_five(output_dir, alias="# Legacy Alias\nUNIQUE_ALIAS_CONTENT\n")
            package_path = write_daily_source_package(output_dir)
            self.assertIsNotNone(package_path)
            package = package_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertEqual(1, package.count("UNIQUE_ALIAS_CONTENT"))
            self.assertIn("content differs; included separately", package)


if __name__ == "__main__":
    unittest.main()
