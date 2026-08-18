import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

import refresh_and_publish as publisher


class RefreshAndPublishTests(unittest.TestCase):
    def test_pipeline_refreshes_before_rebuilding_and_verifying(self):
        commands = publisher.pipeline_commands((2025, 2026))

        self.assertEqual(
            (sys.executable, "-m", "scraper.refresh", "2025", "2026"),
            commands[0],
        )
        self.assertEqual(
            [
                "scraper.refresh",
                "scraper.structure",
                "identity.resolve",
                "analysis.rankings",
                "analysis.site",
                "unittest",
            ],
            [command[2] for command in commands],
        )

    def test_publication_environment_cannot_target_sister_site(self):
        with patch.dict(
            os.environ,
            {
                "USAU_GQL_DB": "/tmp/other.db",
                "RANKINGS_DATA_DIR": "/tmp/data",
                "RANKINGS_SITE_OUT": "/tmp/index.html",
                "RATING_NAME": "Glicko-2",
            },
        ):
            env = publisher.publication_env()

        self.assertEqual(str(publisher.USAU_DB), env["USAU_GQL_DB"])
        self.assertEqual(str(publisher.DATA), env["RANKINGS_DATA_DIR"])
        self.assertEqual(str(publisher.DOCS / "index.html"), env["RANKINGS_SITE_OUT"])
        self.assertNotIn("RATING_NAME", env)

    def test_dry_run_never_starts_publication(self):
        with patch.object(publisher, "publish") as publish, redirect_stdout(StringIO()):
            result = publisher.main(("--season", "2025", "--season", "2026", "--dry-run"))

        self.assertEqual(0, result)
        publish.assert_not_called()

    def test_manual_workflow_is_write_scoped_and_self_hosted(self):
        workflow = (
            publisher.ROOT / ".github" / "workflows" / "refresh-rankings.yml"
        ).read_text()

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("runs-on: [self-hosted, macOS, rankings]", workflow)
        self.assertIn("RANKINGS_REPO_PATH", workflow)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("actions/deploy-pages@v4", workflow)

        deploy = (
            publisher.ROOT / ".github" / "workflows" / "deploy-pages.yml"
        ).read_text()
        self.assertIn("push:", deploy)
        self.assertIn("- docs/**", deploy)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)
        self.assertIn("actions/deploy-pages@v4", deploy)
        self.assertNotIn("self-hosted", deploy)

    def test_unchanged_build_is_not_committed_or_pushed(self):
        quiet = Mock(returncode=0)
        with (
            patch.object(publisher, "ensure_ready"),
            patch.object(publisher, "pipeline_commands", return_value=()),
            patch.object(publisher, "verify_outputs"),
            patch.object(publisher, "_run") as run,
            patch.object(publisher.subprocess, "run", return_value=quiet),
            redirect_stdout(StringIO()),
        ):
            changed = publisher.publish(
                (2026,), remote="origin", branch="main", message="Refresh rankings"
            )

        self.assertFalse(changed)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(("git", "add", "--", *publisher.PUBLISHED_PATHS), commands)
        self.assertFalse(any(command[:2] == ("git", "commit") for command in commands))
        self.assertFalse(any(command[:2] == ("git", "push") for command in commands))


if __name__ == "__main__":
    unittest.main()
