"""Unit tests for extended metrics logging (bot/metrics.py and bot/llm.py).

Owner: luckydevs-1.
"""

import json
import os
import shutil
import tempfile
import unittest

from bot import config, llm, memory, metrics


class MetricsTests(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ldmem-metrics-")
        memory.reset_root_for_tests(self.test_dir)

    def tearDown(self):
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_calculate_cost(self):
        # 1M prompt uncached = $0.10, 1M out = $0.50
        cost = metrics.calculate_cost(prompt_tokens=1000, completion_tokens=200, cached_tokens=0)
        expected = (1000 * 0.10 / 1e6) + (200 * 0.50 / 1e6)
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

        # With cache read tokens
        cost_cached = metrics.calculate_cost(prompt_tokens=1000, completion_tokens=200, cached_tokens=800)
        expected_cached = (200 * 0.10 / 1e6) + (800 * 0.01 / 1e6) + (200 * 0.50 / 1e6)
        self.assertAlmostEqual(cost_cached, round(expected_cached, 6), places=6)

    def test_record_decision_explicit_tokens_and_cost(self):
        tokens = {
            "prompt_tokens": 150,
            "completion_tokens": 30,
            "total_tokens": 180,
            "cached_tokens": 50,
        }
        metrics.record_decision(
            game_id="tic_tac_toe",
            level=2,
            elapsed=0.45,
            why="corner move",
            tokens=tokens,
            turn=1,
            game_index=1,
        )

        p = memory.metrics_path(metrics.CONTAINER_ID)
        self.assertTrue(os.path.isfile(p), "Metrics file should exist")

        with open(p, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 1)
        entry = lines[0]
        self.assertEqual(entry["game_id"], "tic_tac_toe")
        self.assertEqual(entry["level"], 2)
        self.assertEqual(entry["elapsed"], 0.45)
        self.assertEqual(entry["why"], "corner move")
        self.assertEqual(entry["tokens"]["prompt_tokens"], 150)
        self.assertEqual(entry["tokens"]["completion_tokens"], 30)
        self.assertEqual(entry["tokens"]["total_tokens"], 180)
        self.assertIn("cost", entry)
        self.assertGreater(entry["cost"], 0.0)
        self.assertIn("ts", entry)
        self.assertEqual(entry["container"], metrics.CONTAINER_ID)

    def test_record_turn_auto_extracts_from_llm(self):
        # Fake complete to populate llm._last_call
        llm.complete([{"role": "user", "content": "test"}], tag="turn_test")

        metrics.record(
            "turn",
            game_id="pig",
            level=2,
            elapsed=0.35,
            why="hold above 20",
            turn=2,
        )

        p = memory.metrics_path(metrics.CONTAINER_ID)
        with open(p, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertGreaterEqual(len(lines), 1)
        entry = lines[-1]
        self.assertEqual(entry["game_id"], "pig")
        self.assertEqual(entry["level"], 2)
        self.assertEqual(entry["elapsed"], 0.35)
        self.assertEqual(entry["why"], "hold above 20")
        self.assertIn("tokens", entry)
        self.assertEqual(entry["tokens"]["prompt_tokens"], 100)  # From fake LLM
        self.assertEqual(entry["tokens"]["completion_tokens"], 20)  # From fake LLM
        self.assertEqual(entry["tokens"]["total_tokens"], 120)
        self.assertIn("cost", entry)
        self.assertAlmostEqual(entry["cost"], 0.00002, places=6)

    def test_record_never_raises(self):
        # Pass an un-serializable object or invalid path
        try:
            metrics.record("turn", bad_obj=lambda: None)
        except Exception as e:
            self.fail("metrics.record raised unexpectedly: %s" % e)


if __name__ == "__main__":
    unittest.main()
