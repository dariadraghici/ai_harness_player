"""Unit tests for scripts/check_image.py.

Owner: luckydevs-1.
"""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from scripts.check_image import (
    check_docker_available,
    check_no_secrets,
    check_platform,
    check_practice_match,
    check_source_and_commit_label,
    docker_cmd,
)


class CheckImageTests(unittest.TestCase):

    def test_docker_cmd_formatting(self):
        cmd = docker_cmd("harbor.hackaton-2026.dvloper.io/luckydevs/bot:dev", {"BOT_FAKE_LLM": "1", "FOO": "bar"})
        self.assertIn("docker", cmd)
        self.assertIn("run", cmd)
        self.assertIn("--platform", cmd)
        self.assertIn("linux/amd64", cmd)
        self.assertIn("-e", cmd)
        self.assertIn("BOT_FAKE_LLM=1", cmd)
        self.assertIn("FOO=bar", cmd)
        self.assertEqual(cmd[-1], "harbor.hackaton-2026.dvloper.io/luckydevs/bot:dev")

    @patch("subprocess.run")
    def test_check_platform_pass(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="linux/amd64\n", stderr="")
        ok, detail = check_platform("myimage:tag")
        self.assertTrue(ok)
        self.assertIn("linux/amd64", detail)

    @patch("subprocess.run")
    def test_check_platform_fail(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="linux/arm64\n", stderr="")
        ok, detail = check_platform("myimage:tag")
        self.assertFalse(ok)
        self.assertIn("arm64", detail)

    @patch("subprocess.run")
    def test_check_no_secrets_clean(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, detail = check_no_secrets("myimage:tag")
        self.assertTrue(ok)
        self.assertIn("no secret files found", detail)

    @patch("subprocess.run")
    def test_check_no_secrets_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/bot/secrets.local.env\n", stderr="")
        ok, detail = check_no_secrets("myimage:tag")
        self.assertFalse(ok)
        self.assertIn("secrets.local.env", detail)

    @patch("scripts.check_image.git_commit", return_value="abcdef123456")
    @patch("scripts.check_image.git_dirty", return_value="")
    @patch("subprocess.run")
    def test_check_source_and_commit_label_matching(self, mock_run, mock_dirty, mock_commit):
        mock_run.return_value = MagicMock(returncode=0, stdout="abcdef123456\n", stderr="")
        ok, detail = check_source_and_commit_label("myimage:tag")
        self.assertTrue(ok)
        self.assertIn("matches HEAD", detail)

    def test_check_practice_match_skip(self):
        ok, detail = check_practice_match("myimage:tag", skip=True)
        self.assertTrue(ok)
        self.assertIn("skipped", detail)


if __name__ == "__main__":
    unittest.main()
