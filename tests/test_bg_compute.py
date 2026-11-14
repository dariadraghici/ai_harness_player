"""Tests for idle computation safeguards and tactical search precedence."""

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from bot import memory
from bot.engines import bg_compute, search_job
from bot.interfaces import GameProfile, VariantSpec
from bot.prep import sandbox


class TacticalSimulator:
    def __init__(self, with_win):
        self.with_win = with_win

    def parse_observation(self, observation, variant, seat):
        return {"me": seat, "turn": 0, "with_win": self.with_win, "move": ""}

    def legal_actions(self, state):
        if state["turn"] == 0:
            return (["win", "block", "bad"] if state["with_win"]
                    else ["block", "bad"])
        if state["move"] == "bad":
            return ["threat"]
        return ["safe"]

    def apply_action(self, state, action):
        if state["turn"] == 0 and action == "win":
            return {"me": state["me"], "turn": 2, "with_win": state["with_win"], "move": action}
        if state["turn"] == 1 and action == "threat":
            return {"me": state["me"], "turn": 2, "with_win": state["with_win"], "move": action}
        return {"me": state["me"], "turn": 1 if state["turn"] == 0 else 2,
                "with_win": state["with_win"], "move": action}

    def is_terminal(self, state):
        return state["turn"] >= 2

    def returns(self, state):
        if state["move"] in ("win", "threat"):
            return [1.0, -1.0] if state["move"] == "win" else [-1.0, 1.0]
        return [0.0, 0.0]

    def current_player(self, state):
        return 0 if state["turn"] == 0 else 1


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ld-background-")
        memory.reset_root_for_tests(self.root)

    def tearDown(self):
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.root, ignore_errors=True)

    def spec(self):
        return VariantSpec(
            "g", "Game", "variant", "Rules", "hash",
            profile=GameProfile(family="think_ahead"),
            sample_turn={"observation": "state", "legal_actions": ["a"]},
            simulator_path="/tmp/simulator.py",
            verified=True,
        )

    def test_policy_is_bounded_and_loaded_atomically(self):
        spec = self.spec()
        manager = bg_compute.BackgroundCompute()
        for _ in range(bg_compute.MAX_RECORDS + 20):
            manager._persist(spec, {
                "algo": "search",
                "actions": [{"action": "a" * 1000, "value": 1, "probability": 1}],
            })
        data = memory.read_json(os.path.join(
            memory.variant_dir("g", "hash"), bg_compute.BACKGROUND_FILE), {})
        self.assertEqual(len(data["records"]), bg_compute.MAX_RECORDS)
        self.assertLess(os.path.getsize(os.path.join(
            memory.variant_dir("g", "hash"), bg_compute.BACKGROUND_FILE)), 256 * 1024 * 1024)
        self.assertEqual(len(bg_compute.load_policy(spec)["records"]), bg_compute.MAX_RECORDS)

    def test_pause_kills_active_child_and_does_not_call_llm(self):
        spec = self.spec()
        started = threading.Event()
        killed = threading.Event()

        class FakeWorker:
            def __init__(self, *args, **kwargs):
                pass

            def job(self, *args, **kwargs):
                started.set()
                while not killed.is_set():
                    time.sleep(0.005)
                raise sandbox.SandboxError("paused", "closed")

            def kill(self, reason=""):
                killed.set()

            def close(self):
                killed.set()

        with mock.patch.object(bg_compute.sandbox, "Worker", FakeWorker), \
                mock.patch("bot.llm.complete") as complete:
            manager = bg_compute.BackgroundCompute()
            manager.start([spec])
            self.assertTrue(started.wait(1.0))
            manager.pause()
            self.assertTrue(killed.wait(1.0))
            manager.stop()
        complete.assert_not_called()

    def test_tactical_immediate_win_precedes_search(self):
        result = search_job.run(TacticalSimulator(True), {
            "observation": "state", "legal_actions": ["win", "block", "bad"],
            "variant": {}, "seat": 0, "budget_s": 0.2,
        })
        self.assertEqual(result["algo"], "tactical")
        self.assertEqual([row["action"] for row in result["actions"]], ["win"])

    def test_tactical_block_removes_immediate_opponent_threat(self):
        result = search_job.run(TacticalSimulator(False), {
            "observation": "state", "legal_actions": ["block", "bad"],
            "variant": {}, "seat": 0, "budget_s": 0.2,
        })
        self.assertEqual(result["algo"], "tactical")
        self.assertEqual([row["action"] for row in result["actions"]], ["block"])


if __name__ == "__main__":
    unittest.main()
