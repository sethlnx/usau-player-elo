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


if __name__ == "__main__":
    unittest.main()
