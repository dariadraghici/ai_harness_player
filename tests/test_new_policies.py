import random
import re
import time
import unittest

from bot.policies import (battleship, bulls_cows, choose, dots_boxes, hex, mini_chess,
                          nim, pig, prisoners, rps)


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

    def test_battleship_never_repeats_a_shot(self):
        state = {"turn": {"observation": "YOUR SHOTS: fire c4 was a miss"}}
        row = battleship.decide(state, time.monotonic() + 2)[0]
        self.assertNotEqual(row["action"], "fire c4")
        self.assertRegex(row["action"], r"^fire [a-f][1-6]$")

    def test_rps_counters_constant_opponent(self):
        observation = "Opponent: rock\nOpponent: rock\nOpponent: rock\n"
        row = rps.decide({"turn": {"observation": observation}},
                         time.monotonic() + 1)[0]
        self.assertEqual(row["action"], "paper")

    def test_prisoners_reacts_to_proven_defection(self):
        observation = "Opponent: defect\nOpponent: defect\nOpponent: defect\n"
        row = prisoners.decide({"turn": {"observation": observation}},
                               time.monotonic() + 1)[0]
        self.assertEqual(row["action"], "defect")

    def test_router_selects_battleship_and_talk_games(self):
        self.assertIs(choose("Battleship 6x6. Action format: fire c4."), battleship)
        self.assertIs(choose("Prisoner's Dilemma: cooperate or defect."), prisoners)
        self.assertIs(choose("Rock paper scissors, first to 5."), rps)

    def test_hex_takes_an_immediate_winning_move(self):
        observation = "X at a1, X at a2, X at a3, X at a4, X at a5, X at a6"
        row = hex.decide({"turn": {"observation": observation}},
                         time.monotonic() + 1)[0]
        self.assertEqual(row["action"], "place a7")

    def test_dots_takes_a_free_box(self):
        observation = "line a1-b1\nline a1-a2\nline b1-b2"
        row = dots_boxes.decide({"turn": {"observation": observation}},
                                time.monotonic() + 1)[0]
        self.assertIn(row["action"], ("line a2-b2", "line b1-b2"))

    def test_mini_chess_uses_exact_legal_action_spelling(self):
        row = mini_chess.decide({"turn": {"legal_actions": ["b2b3", "a1a2"]}},
                                time.monotonic() + 1)[0]
        self.assertEqual(row["action"], "b2b3")

    def test_router_selects_board_policies(self):
        self.assertIs(choose("Gardner 5x5 mini chess, move b2b3"), mini_chess)
        self.assertIs(choose("Hex 7x7, place c3"), hex)
        self.assertIs(choose("Dots and Boxes 4x4, line a1-b1"), dots_boxes)

