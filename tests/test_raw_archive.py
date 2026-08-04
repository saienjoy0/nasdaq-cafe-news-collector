from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nasdaq_cafe.config import ROOT_DIR, RunConfig
from nasdaq_cafe.raw_archive import build_raw_archive_manifest, collect_manifest_fulltext


ARTICLE_HTML = (
    "<html><body><article>"
    "<p>This is the first complete paragraph of the article and it contains source material.</p>"
    "<p>This is the second complete paragraph with enough detail to pass article extraction checks.</p>"
    "<p>This is the third complete paragraph and no part of it should be summarized or shortened.</p>"
    "<p>This is the fourth complete paragraph preserving the original order of the published body.</p>"
    "<p>This is the fifth complete paragraph with additional facts, context, names, dates, and figures.</p>"
    "<p>This is the sixth complete paragraph and it proves that the final paragraph was also retained in full.</p>"
    "</article></body></html>"
)


class FakeResponse:
    def __init__(self, url: str, *, status_code: int = 200, body: str = ARTICLE_HTML) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.content = body.encode("utf-8")

    def iter_content(self, chunk_size: int = 65_536):
        del chunk_size
        yield self.content

    def close(self) -> None:
        return None


class RawArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT_DIR)
        base = Path(self.temp.name)
        output_dir = base / "output" / "2026-07-10"
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True)
        self.config = RunConfig(
            target_date="2026-07-10",
            refresh=False,
            output_dir=output_dir,
            raw_dir=raw_dir,
            env={
                "TAVILY_API_KEY": "",
                "NASDAQ_CAFE_FULLTEXT_WORKERS": "3",
                "NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS": "0",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_discovered_urls_are_registered_and_attempted_before_selection(self) -> None:
        payloads = {
            "rss": {
                "items": [
                    {
                        "title": "Low score article",
                        "url": "https://example.com/low-score",
                        "source": "Example",
                        "relevance_score": 0,
                        "selected_for_handoff": False,
                    }
                ]
            },
            "serpapi": {
                "items": [
                    {
                        "title": "Low value article",
                        "url": "https://example.net/low-value",
                        "source": "SerpAPI",
                        "low_value": True,
                        "review_priority": "usually_do_not_review",
                    }
                ]
            },
            "gdelt": {
                "rejected": [
                    {
                        "title": "Rejected radar article",
                        "url": "https://example.org/rejected",
                        "source": "GDELT",
                        "decision": "rejected",
                        "core_driver": False,
                    }
                ]
            },
        }
        manifest = build_raw_archive_manifest(self.config, payloads)
        self.assertEqual(3, len(manifest["documents"]))

        calls: list[str] = []

        def fake_get(url: str, **kwargs):
            del kwargs
            calls.append(url)
            return FakeResponse(url)

        with patch("nasdaq_cafe.raw_archive.requests.get", side_effect=fake_get):
            result = collect_manifest_fulltext(self.config, manifest)

        self.assertCountEqual(
            [
                "https://example.com/low-score",
                "https://example.net/low-value",
                "https://example.org/rejected",
            ],
            calls,
        )
        self.assertEqual(3, result["summary"]["attempted_count"])
        self.assertEqual(3, result["summary"]["complete_count"])
        self.assertEqual(0, result["summary"]["not_attempted_limit_count"])
        for document in manifest["documents"]:
            self.assertEqual("complete", document["fulltext_status"])
            text_path = ROOT_DIR / document["extracted_text_file"]
            raw_path = ROOT_DIR / document["raw_file"]
            self.assertTrue(text_path.exists())
            self.assertTrue(raw_path.exists())
            text = text_path.read_text(encoding="utf-8")
            self.assertIn("final paragraph was also retained in full", text)
            self.assertEqual(len(text), document["char_count"])

    def test_configured_attempt_limit_retains_unattempted_urls_in_manifest(self) -> None:
        self.config.env["NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS"] = "1"
        manifest = build_raw_archive_manifest(
            self.config,
            {
                "rss": [
                    {"title": "Article one", "url": "https://example.com/one", "source": "Example"},
                    {"title": "Article two", "url": "https://example.com/two", "source": "Example"},
                    {"title": "Article three", "url": "https://example.com/three", "source": "Example"},
                ]
            },
        )
        with patch(
            "nasdaq_cafe.raw_archive.requests.get",
            side_effect=lambda url, **kwargs: FakeResponse(url),
        ) as mocked_get:
            result = collect_manifest_fulltext(self.config, manifest)

        self.assertEqual(1, mocked_get.call_count)
        self.assertEqual(3, len(manifest["documents"]))
        self.assertEqual(1, result["summary"]["attempted_count"])
        self.assertEqual(2, result["summary"]["not_attempted_limit_count"])
        deferred = [
            document
            for document in manifest["documents"]
            if document["fulltext_status"] == "not_attempted_limit"
        ]
        self.assertEqual(2, len(deferred))
        self.assertTrue(all("URL retained for retry" in document["failure_reason"] for document in deferred))

    def test_failures_are_persisted_as_normal_results(self) -> None:
        manifest = build_raw_archive_manifest(
            self.config,
            {"rss": [{"title": "Blocked article", "url": "https://example.com/blocked", "source": "Example"}]},
        )
        with patch(
            "nasdaq_cafe.raw_archive.requests.get",
            return_value=FakeResponse("https://example.com/blocked", status_code=403, body="Access denied"),
        ):
            result = collect_manifest_fulltext(self.config, manifest)

        document = manifest["documents"][0]
        self.assertEqual("failed", document["fulltext_status"])
        self.assertEqual("blocked", document["access_status"])
        self.assertEqual("HTTP 403", document["failure_reason"])
        self.assertEqual(1, result["summary"]["failed_count"])
        self.assertTrue((ROOT_DIR / document["metadata_file"]).exists())
        self.assertTrue((ROOT_DIR / document["raw_file"]).exists())

    def test_content_duplicates_are_grouped_without_deleting_documents(self) -> None:
        manifest = build_raw_archive_manifest(
            self.config,
            {
                "rss": [
                    {"title": "Original", "url": "https://one.example/article", "source": "One"},
                    {"title": "Syndicated", "url": "https://two.example/copy", "source": "Two"},
                ]
            },
        )
        with patch(
            "nasdaq_cafe.raw_archive.requests.get",
            side_effect=lambda url, **kwargs: FakeResponse(url),
        ):
            collect_manifest_fulltext(self.config, manifest)

        self.assertEqual(2, len(manifest["documents"]))
        groups = {document["duplicate_group_id"] for document in manifest["documents"]}
        self.assertEqual(1, len(groups))
        self.assertNotIn(None, groups)
        self.assertEqual(1, sum(1 for document in manifest["documents"] if document["duplicate_of"]))

    def test_completed_cache_is_reused_without_new_network_attempt(self) -> None:
        manifest = build_raw_archive_manifest(
            self.config,
            {"rss": [{"title": "Cached", "url": "https://example.com/cached", "source": "Example"}]},
        )
        with patch(
            "nasdaq_cafe.raw_archive.requests.get",
            return_value=FakeResponse("https://example.com/cached"),
        ):
            collect_manifest_fulltext(self.config, manifest)

        with patch("nasdaq_cafe.raw_archive.requests.get") as mocked_get:
            result = collect_manifest_fulltext(self.config, manifest)
        mocked_get.assert_not_called()
        self.assertTrue(result["cache_used"])
        self.assertEqual(1, result["summary"]["complete_count"])


if __name__ == "__main__":
    unittest.main()
