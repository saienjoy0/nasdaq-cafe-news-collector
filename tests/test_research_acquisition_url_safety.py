from __future__ import annotations

import unittest

from nasdaq_cafe.research_acquisition import ResearchAcquisitionError, validate_exact_url


class ResearchAcquisitionUrlSafetyTests(unittest.TestCase):
    def test_localhost_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchAcquisitionError, "local/private"):
            validate_exact_url("http://localhost:8080/internal")

    def test_dot_local_hostname_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchAcquisitionError, "local/private"):
            validate_exact_url("https://collector.local/document")

    def test_private_ipv4_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchAcquisitionError, "non-public"):
            validate_exact_url("http://10.0.0.1/document")

    def test_loopback_ipv6_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResearchAcquisitionError, "non-public"):
            validate_exact_url("http://[::1]/document")

    def test_public_https_url_is_allowed(self) -> None:
        url = "https://www.sec.gov/Archives/edgar/data/example"
        self.assertEqual(url, validate_exact_url(url))


if __name__ == "__main__":
    unittest.main()
