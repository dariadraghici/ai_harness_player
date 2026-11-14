"""Unit tests for scripts/batch_runner.py.

Owner: luckydevs-1. Tests CLI parsing, replay metric extraction, metric aggregation,
and ASCII table formatting.
"""

import json
import os
import tempfile
import unittest

from scripts.batch_runner import (
    AggregateGroup,
    MatchResult,
    build_arg_parser,
    discover_available_games,
    format_ascii_table,
    parse_replay_metrics,
)


class BatchRunnerTests(unittest.TestCase):

    def test_cli_parsing_defaults(self):
        parser = build_arg_parser()
        args = parser.parse_args([])
        self.assertEqual(args.matches, 1)
        self.assertEqual(args.opponents, "random,self")
        self.assertEqual(args.games, "all")
        self.assertFalse(args.fake)
        self.assertFalse(args.prep)
        self.assertFalse(args.dry_run)

    def test_cli_parsing_custom_args(self):
        parser = build_arg_parser()
        args = parser.parse_args([
            "-n", "5",
            "--opponents", "random,self",
            "--games", "tic_tac_toe,pig",
            "--fake",
            "--prep",
            "--match-timeout", "120",
            "--best-of", "3",
            "--output", "test_report.json",
        ])
        self.assertEqual(args.matches, 5)
        self.assertEqual(args.opponents, "random,self")
        self.assertEqual(args.games, "tic_tac_toe,pig")
        self.assertTrue(args.fake)
        self.assertTrue(args.prep)
        self.assertEqual(args.match_timeout, 120)
        self.assertEqual(args.best_of, 3)
        self.assertEqual(args.output, "test_report.json")

    def test_discover_available_games(self):
        games = discover_available_games()
        self.assertIn("nim_deal", games)
        self.assertIn("rps_twist", games)
        self.assertIn("tic_tac_toe", games)
        self.assertIn("pig", games)
        self.assertIn("guess_number", games)

    def test_parse_replay_metrics_clean(self):
        fd, path = tempfile.mkstemp(prefix="replay-clean-", suffix=".json")
        os.close(fd)
        try:
            sample_doc = {
                "status": "done",
                "games": [
                    {
                        "index": 1,
                        "seats": ["luckydevs", "opponent"],
                        "result": {"winner": "luckydevs", "reason": "3 in a row"},
                        "warnings": {"0": 0, "1": 0},
                        "study": {"0": {"ms": 150}, "1": {"ms": 200}},
                        "timeline": [
                            {"step": 1, "actions": {"0": {"action": "A1", "ms": 400}}},
                            {"step": 2, "actions": {"1": {"action": "B2", "ms": 500}}},
                            {"step": 3, "actions": {"0": {"action": "A2", "ms": 600}}},
                        ]
                    }
                ]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sample_doc, f)

            stats = parse_replay_metrics(path, "luckydevs")
            self.assertEqual(stats["games_count"], 1)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["losses"], 0)
            self.assertEqual(stats["draws"], 0)
            self.assertEqual(stats["warnings"], 0)
            self.assertEqual(stats["timeouts"], 0)
            # Answer times: study (0.15s) + step 1 (0.4s) + step 3 (0.6s)
            self.assertEqual(len(stats["answer_times"]), 3)
            self.assertAlmostEqual(stats["answer_times"][0], 0.15)
            self.assertAlmostEqual(stats["answer_times"][1], 0.40)
            self.assertAlmostEqual(stats["answer_times"][2], 0.60)
            self.assertEqual(len(stats["problems"]), 0)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_parse_replay_metrics_with_warnings_and_timeout(self):
        fd, path = tempfile.mkstemp(prefix="replay-warn-", suffix=".json")
        os.close(fd)
        try:
            sample_doc = {
                "status": "done",
                "games": [
                    {
                        "index": 1,
                        "seats": ["opponent", "luckydevs"],  # luckydevs is seat 1
                        "result": {"winner": "opponent", "reason": "lost by timeout"},
                        "warnings": {"0": 0, "1": 2},
                        "study": {"1": {"ms": 300}},
                        "timeline": [
                            {"step": 1, "actions": {"1": {"action": "bad", "warning": {"reason": "syntax"}}}},
                            {"step": 2, "actions": {"1": {"error": "lost by timeout"}}},
                        ]
                    }
                ]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sample_doc, f)

            stats = parse_replay_metrics(path, "luckydevs")
            self.assertEqual(stats["games_count"], 1)
            self.assertEqual(stats["wins"], 0)
            self.assertEqual(stats["losses"], 1)
            self.assertEqual(stats["warnings"], 2)
            self.assertGreaterEqual(stats["timeouts"], 1)
            self.assertGreaterEqual(len(stats["problems"]), 1)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_aggregate_group_and_table_format(self):
        agg = AggregateGroup("tic_tac_toe", "random")
        agg.matches_planned = 2

        mr1 = MatchResult("tic_tac_toe", "random", 1, "m1")
        mr1.status = "CLEAN"
        mr1.games_count = 1
        mr1.wins = 1
        mr1.warnings = 0
        mr1.timeouts = 0
        mr1.answer_times = [0.2, 0.4]
        mr1.cost_usd = 0.0002

        mr2 = MatchResult("tic_tac_toe", "random", 2, "m2")
        mr2.status = "CLEAN"
        mr2.games_count = 1
        mr2.wins = 1
        mr2.warnings = 1
        mr2.timeouts = 0
        mr2.answer_times = [0.3, 0.5]
        mr2.cost_usd = 0.0004

        agg.add_match(mr1)
        agg.add_match(mr2)

        self.assertEqual(agg.matches_completed, 2)
        self.assertEqual(agg.wins, 2)
        self.assertEqual(agg.warnings, 1)
        self.assertEqual(agg.timeouts, 0)
        self.assertAlmostEqual(agg.win_pct, 100.0)
        self.assertAlmostEqual(agg.avg_time_s, 0.35)
        self.assertAlmostEqual(agg.max_time_s, 0.5)
        self.assertAlmostEqual(agg.cost_per_match, 0.0003)

        table = format_ascii_table([agg], 2, 5.0)
        # Check required columns are present
        self.assertIn("Game", table)
        self.assertIn("Opponent", table)
        self.assertIn("Matches", table)
        self.assertIn("Wins", table)
        self.assertIn("Warnings", table)
        self.assertIn("Timeouts", table)
        self.assertIn("Avg (s)", table)
        self.assertIn("Max (s)", table)
        self.assertIn("Cost/Match", table)
        self.assertIn("TOTAL / OVERALL", table)
        self.assertIn("tic_tac_toe", table)
        self.assertIn("random", table)
        self.assertIn("2/2 (100%)", table)
        self.assertIn("$0.0003", table)


class ResourceColumnsTests(unittest.TestCase):

    def test_percentile_nearest_rank(self):
        from scripts.batch_runner import percentile
        self.assertEqual(percentile([], 50), 0.0)
        self.assertEqual(percentile([5.0], 99), 5.0)
        self.assertEqual(percentile(list(range(1, 101)), 50), 50.0)
        self.assertEqual(percentile(list(range(1, 101)), 99), 99.0)

    def test_real_key_runs_are_never_parallel_fake_runs_use_the_host(self):
        from scripts.batch_runner import resolve_jobs
        jobs, note = resolve_jobs(4, fake=False)
        self.assertEqual(jobs, 1)
        self.assertIn("refused", note)
        self.assertEqual(resolve_jobs(0, fake=False), (1, ""))
        self.assertEqual(resolve_jobs(3, fake=True), (3, ""))
        self.assertGreaterEqual(resolve_jobs(0, fake=True)[0], 1)

    def test_table_has_the_resource_columns(self):
        from scripts.batch_runner import AggregateGroup, MatchResult, format_ascii_table
        agg = AggregateGroup("tic_tac_toe", "random")
        agg.matches_planned = 1
        mr = MatchResult("tic_tac_toe", "random", 1, "m")
        mr.games_count, mr.wins, mr.answer_times = 1, 1, [0.5, 1.0, 3.0]
        mr.rss_mb, mr.children_peak, mr.cpu_s = 88.0, 2, 12.5
        agg.add_match(mr)
        table = format_ascii_table([agg], 1, 5.0)
        for col in ("p50 (s)", "p99 (s)", "RSS MB", "Kids", "CPU s/match"):
            self.assertIn(col, table)
        self.assertIn("88", table)
        self.assertIn("12.5", table)
        self.assertEqual(agg.to_dict()["p99_time_s"], 3.0)


if __name__ == "__main__":
    unittest.main()
