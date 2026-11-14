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
    check_proxy_and_security,
    check_source_and_commit_label,
    docker_cmd,
    idle_problems,
    parse_top,
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

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_check_proxy_and_security_pass(self, mock_popen, mock_run):
        proc = MagicMock()
        proc.stdout.readline.return_value = '{"type": "ready", "chat": "Ready."}\n'
        mock_popen.return_value = proc
        mock_run.return_value = MagicMock(returncode=0, stdout="null\n", stderr="")
        ok, detail = check_proxy_and_security("myimage:tag")
        self.assertTrue(ok)
        self.assertIn("proxy environment supported", detail)

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_check_proxy_and_security_exposed_port(self, mock_popen, mock_run):
        proc = MagicMock()
        proc.stdout.readline.return_value = '{"type": "ready", "chat": "Ready."}\n'
        mock_popen.return_value = proc
        mock_run.return_value = MagicMock(returncode=0, stdout='{"8080/tcp":{}}\n', stderr="")
        ok, detail = check_proxy_and_security("myimage:tag")
        self.assertFalse(ok)
        self.assertIn("exposes unnecessary listening ports", detail)

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_check_proxy_and_security_crash(self, mock_popen, mock_run):
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        mock_popen.return_value = proc
        mock_run.return_value = MagicMock(returncode=0, stdout="null\n", stderr="")
        ok, detail = check_proxy_and_security("myimage:tag")
        self.assertFalse(ok)
        self.assertIn("crashed or produced no answer", detail)


class IdleCheckTests(unittest.TestCase):
    TOP = "PID RSS COMMAND\n1 30000 python -u -m bot.main\n57 22000 python -I -S -B /bot/prep/sandbox_child.py 512 120\n"

    def test_parse_top(self):
        rows = parse_top(self.TOP)
        self.assertEqual([(r[0], r[1]) for r in rows], [(1, 30000), (57, 22000)])
        self.assertEqual(parse_top(""), [])

    def test_clean_idle_has_no_problems(self):
        before = parse_top("PID RSS COMMAND\n1 30000 python -u -m bot.main\n")
        after = parse_top("PID RSS COMMAND\n1 41000 python -u -m bot.main\n")
        self.assertEqual(idle_problems(before, after), [])

    def test_a_live_child_is_a_problem(self):
        before = parse_top("PID RSS COMMAND\n1 30000 python -u -m bot.main\n")
        self.assertTrue(any("sandbox child" in p for p in idle_problems(before, parse_top(self.TOP))))

    def test_memory_far_above_baseline_is_a_problem(self):
        before = parse_top("PID RSS COMMAND\n1 30000 python -u -m bot.main\n")
        after = parse_top("PID RSS COMMAND\n1 400000 python -u -m bot.main\n")
        self.assertTrue(any("memory" in p for p in idle_problems(before, after)))

    def test_no_process_list_is_a_problem(self):
        self.assertTrue(idle_problems([], []))


if __name__ == "__main__":
    unittest.main()
