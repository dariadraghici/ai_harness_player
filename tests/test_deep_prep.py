"""Focused tests for budgeted prep self-play and strategy persistence."""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from bot import memory
from bot.chat import decide, defense
from bot.interfaces import GameProfile, VariantSpec
from bot.prep import mastery, mastery_job


class TinySimulator:
    def parse_observation(self, observation, variant, seat):
        return {"turn": 0, "me": seat}

    def legal_actions(self, state):
        return ["a", "b"] if state["turn"] < 4 else []

    def apply_action(self, state, action):
        return {"turn": state["turn"] + 1, "me": state["me"]}

    def is_terminal(self, state):
        return state["turn"] >= 4

    def returns(self, state):
        return [1.0, -1.0]

    def current_player(self, state):
        return state["turn"] % 2


class DeepPrepTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ld-mastery-")
        memory.reset_root_for_tests(self.root)

    def tearDown(self):
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.root, ignore_errors=True)

    def spec(self, verified=True):
        return VariantSpec(
            "g", "Game", "variant", "Rules", "hash",
            profile=GameProfile(family="think_ahead"),
            playbook="Use legal moves.",
            sample_turn={"observation": "state"},
            simulator_path="/tmp/simulator.py" if verified else None,
            verified=verified,
        )

    def test_safety_buffer_stops_short_budget_without_self_play(self):
        spec = self.spec()
        deadline = time.monotonic() + 0.05
        started = time.monotonic()
        with mock.patch.object(mastery.sandbox, "run_job") as run:
            totals = mastery.refine([spec], {}, deadline)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(run.called)
        self.assertEqual(totals[id(spec)]["games"], 0)
        self.assertTrue(os.path.isfile(os.path.join(
            memory.variant_dir("g", "hash"), mastery.STRATEGY_FILE)))

    def test_self_play_findings_are_distilled_and_loaded(self):
        spec = self.spec()
        summary = {
            "games": 12, "wins": 7, "losses": 3, "draws": 2, "loops": 1,
            "illegal": 0, "errors": 0, "repeated_actions": 2, "steps": 48,
            "elapsed_s": 0.2,
        }
        mastery._persist(spec, summary, True)
        loaded = memory.load_variant("g", "hash", "Rules")
        self.assertIsNotNone(loaded)
        self.assertIn("PREP SELF-TEST", loaded.playbook)
        self.assertIn("7 wins", loaded.playbook)
        strategy = memory.read_json(os.path.join(
            memory.variant_dir("g", "hash"), mastery.STRATEGY_FILE), {})
        self.assertEqual(strategy["summary"]["games"], 12)
        messages = decide.build_decide_messages(
            loaded, {}, {"observation": "state", "legal_actions": ["a"]},
            defense.build_context([]), [], ["a"])
        self.assertIn("PREP SELF-TEST", messages[0]["content"])

    def test_unverified_variants_get_safe_fallback_playbook(self):
        spec = self.spec(verified=False)
        mastery.refine([spec], {}, time.monotonic() + 0.1)
        loaded = memory.load_variant("g", "hash", "Rules")
        self.assertIn("SIMULATOR UNAVAILABLE", loaded.playbook)
        self.assertIn("sample-deal values", loaded.playbook)

    def test_sandbox_job_runs_both_seats_and_checks_moves(self):
        result = mastery_job.run(TinySimulator(), {
            "observation": "state", "variant": {"label": "variant"},
            "budget_s": 0.02, "seed": 1,
        })
        self.assertTrue(result["ok"])
        self.assertGreater(result["summary"]["games"], 0)
        self.assertEqual(result["summary"]["illegal"], 0)
        self.assertEqual(result["summary"]["errors"], 0)
        self.assertEqual(set(result["summary"]["seats"]), {"0", "1"})


if __name__ == "__main__":
    unittest.main()
