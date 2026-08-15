import unittest
from unittest.mock import patch

from scraper.ultimate_canada import ingest_event, source_for_client


class FakeClient:
    base_url = "https://cuc2026.ultimatecentral.com"


class OfficialUltimateCanadaTests(unittest.TestCase):
    def test_source_is_qualified_by_official_tenant_host(self):
        self.assertEqual(
            "ultimate-central:cuc2026.ultimatecentral.com",
            source_for_client(FakeClient()),
        )

    def test_ingest_wrapper_passes_official_provenance_and_url(self):
        with patch("scraper.ultimate_canada.ingest_ultimate_central_event") as ingest:
            ingest_event(object(), FakeClient(), "42")
        ingest.assert_called_once_with(
            unittest.mock.ANY,
            unittest.mock.ANY,
            "42",
            source="ultimate-central:cuc2026.ultimatecentral.com",
            event_url_base="https://cuc2026.ultimatecentral.com",
        )


if __name__ == "__main__":
    unittest.main()
