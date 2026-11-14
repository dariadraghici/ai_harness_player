import time
import unittest

from bot.engines import wordle
from bot.interfaces import GameProfile, VariantSpec


RULES = (
    "Wordle Duel. Guess one 6-letter word. Feedback marks each letter as "
    "green for right spot, yellow for in the word, or gray for not in the word. "
    'Action format: guess <word>, exactly like "guess planet". There are 7 rounds.'
)


class WordleTests(unittest.TestCase):

    def test_duplicate_feedback_uses_two_pass_scoring(self):
        self.assertEqual(wordle.score("coffee", "office"), (1, 1, 2, 1, 0, 2))
        self.assertEqual(wordle.score("borrow", "narrow"), (0, 0, 2, 2, 2, 2))

    def test_observation_parser_reads_history_and_round(self):
        observation = (
            "WORDLE DUEL. Round 3 of 7.\n"
            "Your guesses:\n"
            "guess ORANGE -> 011100\n"
            "guess PLOVED -> 010000\n"
        )
        parsed = wordle.parse_observation(observation, RULES)
        self.assertEqual(parsed["round"], (3, 7))
        self.assertEqual([guess for guess, _ in parsed["history"]], ["orange", "ploved"])
        self.assertEqual(parsed["history"][0][1], (0, 1, 1, 1, 0, 0))

    def test_narrow_survives_reported_feedback_and_planet_is_not_repeated(self):
        guesses = ["orange", "ploved", "planet"]
        rows = ["WORDLE DUEL. Round 1 of 7.", "Your guesses:"]
        for index, guess in enumerate(guesses, 1):
            rows.append("guess %s -> %s" % (
                guess, "".join(str(value) for value in wordle.score(guess, "narrow"))))
        observation = "\n".join(rows)
        parsed = wordle.parse_observation(observation, RULES)
        _, answers = wordle.load_dictionary()
        remaining = wordle.filter_candidates(
            [word for word in answers if len(word) == 6], parsed["history"])
        self.assertIn("narrow", remaining)
        guess, _, _ = wordle.choose_guess(RULES, observation)
        self.assertNotEqual(guess, "planet")
        self.assertNotIn(guess, set(parsed["guesses"]))

    def test_solver_emits_exact_one_space_action_and_uses_code_only(self):
        spec = VariantSpec("wordle", "Wordle Duel", "6 letters", RULES, "wordle-rules",
                           profile=GameProfile(family="guessing"))
        result = wordle.candidates(spec, {"observation": "Round 1 of 7."},
                                   time.monotonic() + 2.0)
        self.assertTrue(result)
        self.assertRegex(result[0].action, r"^guess [a-z]{6}$")
        self.assertNotIn("  ", result[0].action)

    def test_fuzzed_observation_still_returns_a_dictionary_word(self):
        spec = VariantSpec("wordle", "Wordle Duel", "6 letters", RULES, "wordle-rules",
                           profile=GameProfile(family="guessing"))
        for observation in ("", "truncated guess", "\x00\x01", "Round ???", "guessSLIDE"):
            result = wordle.candidates(spec, {"observation": observation},
                                       time.monotonic() + 1.0)
            self.assertTrue(wordle.allowed_word(result[0].action.split()[-1], 6))

