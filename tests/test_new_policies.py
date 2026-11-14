import random
import re
import time
import unittest

from bot.policies import bulls_cows, choose, nim, pig


class NewPolicyTests(unittest.TestCase):

    def test_router_selects_new_policy_families(self):
        self.assertIs(choose("Nim with four piles of stones"), nim)
        self.assertIs(choose("Bulls and cows: guess four distinct digits"), bulls_cows)
        self.assertIs(choose("Pig: roll or hold, a 1 loses the turn total"), pig)

    def test_nim_move_is_optimal_on_small_positions(self):
        for first in range(9):
            for second in range(9):
                piles = [first, second, 3]
                if not any(piles):
                    continue
                observation = "Piles: %d %d 3" % (first, second)
                row = nim.decide({"turn": {"observation": observation}},
                                 time.monotonic() + 1)[0]
                amount, pile = map(int, re.findall(r"\d+", row["action"]))
                after = piles[:]
                after[pile - 1] -= amount
                if nim.winning(piles):
                    self.assertFalse(nim.winning(after))

    def test_bulls_and_cows_feedback_and_distinct_guesses(self):
        self.assertEqual(bulls_cows.feedback("0123", "0132"), (2, 2))
        row = bulls_cows.decide({"turn": {"observation": ""}},
                                time.monotonic() + 2)[0]
        guess = row["action"].split()[-1]
        self.assertEqual(guess, "0123")
        self.assertEqual(len(set(guess)), 4)

    def test_bulls_and_cows_preserves_secret_after_feedback(self):
        secret = "9876"
        result = bulls_cows.feedback("0123", secret)
        observation = "guess 0123 -> %d/%d" % result
        _, candidates = bulls_cows.parse_state(observation)
        self.assertIn(secret, candidates)
        row = bulls_cows.decide({"turn": {"observation": observation}},
                                time.monotonic() + 2)[0]
        self.assertNotEqual(row["action"].split()[-1], "0123")

    def test_pig_emits_only_valid_actions(self):
        for total in (0, 5, 20, 35):
            row = pig.decide({"turn": {"observation":
                                       "Your score: 20 Opponent score: 18 Turn total: %d" % total}},
                             time.monotonic() + 8)[0]
            self.assertIn(row["action"], ("roll", "hold"))

