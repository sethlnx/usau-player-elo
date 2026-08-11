import tempfile
import unittest
from pathlib import Path

from analysis.site import bucket_urls, content_version


class SiteAssetContractTests(unittest.TestCase):
    def test_sidecar_urls_share_content_version_and_change_with_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "history.json"
            source.write_text("first")
            first = content_version((source,))
            urls = bucket_urls(Path("p"), {0: {}, 31: {}}, first)
            self.assertEqual(f"p/0.js?v={first}", urls["0"])
            self.assertEqual(f"p/31.js?v={first}", urls["31"])

            source.write_text("second")
            self.assertNotEqual(first, content_version((source,)))


    def test_head_to_head_ignores_draws_and_self_fixtures(self):
        from analysis.history_split import head_to_head_records

        history = {
            "events": [["2026-01-01", "Test", 2026, 0]],
            "games": {
                "0": [
                    [1, 2, 10, 8, 0, 0, 0],
                    [2, 1, 7, 9, 0, 0, 0],
                    [1, 2, 8, 8, 0, 0, 0],
                    [3, 3, 10, 1, 0, 0, 0],
                ]
            },
        }

        self.assertEqual(
            head_to_head_records(history),
            [[1, 2, 2026, 2, 0]],
        )

if __name__ == "__main__":
    unittest.main()
