"""Our own test games (testgames/) are played by random moves and must never break the arena's contract.

Owner: luckydevs-1. These games are not in the bot image; they exist so that the families the kit's practice
games do not cover (bluff, deals, truth, guardrails) are played end to end before the tournament. A broken game
file would waste a practice run, so every game is played here instead: every seat that must move gets a legal
action, the observation is always text, the game ends, and the result is well formed.

Skipped when the participant kit is absent (arena.core is the base class).
"""

import os
import random
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LITE = os.environ.get("BOT_ARENA_LITE") or os.path.join(REPO, "participant-kit", "arena-lite")
GAMES_DIR = os.path.join(REPO, "testgames")
HAVE_KIT = os.path.isfile(os.path.join(LITE, "arena", "core.py"))


def load_games():
    if not HAVE_KIT:
        return []
    for path in (LITE, GAMES_DIR):
        if path not in sys.path:
            sys.path.insert(0, path)
    from arena.core import Game
    out = []
    for name in sorted(os.listdir(GAMES_DIR)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        module = __import__(name[:-3])
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, Game) and attr is not Game and attr.id:
                out.append(attr)
    return out


@unittest.skipUnless(HAVE_KIT, "needs participant-kit/arena-lite")
class TestGameContractTests(unittest.TestCase):

    def setUp(self):
        self.games = load_games()
        self.assertTrue(self.games, "no test game was found in testgames/")

    def test_metadata_is_complete(self):
        from arena.core import CATEGORIES
        seen = set()
        for G in self.games:
            self.assertTrue(G.id and G.name and G.blurb and G.howto, G.id)
            self.assertIn(G.category, CATEGORIES, G.id)
            self.assertNotIn(G.id, seen, "duplicate game id")
            seen.add(G.id)
            self.assertIsInstance(G.duplicate_safe, bool, G.id)
            variants = G.variants()
            self.assertGreaterEqual(len(variants), 2, G.id)
            for v in variants:
                self.assertTrue(str(v.get("label") or "").strip(), G.id)

    def test_the_rules_state_the_action_format_and_fit_a_prompt(self):
        for G in self.games:
            for v in G.variants():
                rules = G(v, 7).rules()
                self.assertIn("ACTION FORMAT", rules, G.id)
                self.assertTrue(rules.isascii(), G.id)
                self.assertLess(len(rules), 12000, G.id)

    def test_random_playouts_stay_legal_and_terminate(self):
        for G in self.games:
            for v in G.variants():
                for seed in range(12):
                    g = G(v, seed)
                    rng = random.Random(seed * 31 + 7)
                    for step in range(400):
                        if g.is_over():
                            break
                        seats = g.to_move()
                        self.assertTrue(seats, "%s: nobody to move and not over" % G.id)
                        actions = {}
                        for seat in seats:
                            self.assertIsInstance(g.observation(seat), str)
                            legal = g.legal_actions(seat)
                            if legal is not None:
                                self.assertTrue(legal, "%s: empty legal list for seat %d" % (G.id, seat))
                                self.assertLessEqual(len(legal), 50, G.id)
                            raw = g.random_action(seat, rng)
                            action = g.normalize(seat, raw)
                            self.assertIsNotNone(action, "%s: random_action %r is not legal" % (G.id, raw))
                            actions[seat] = action
                        g.apply(actions)
                    self.assertTrue(g.is_over(), "%s did not finish in 400 steps" % G.id)
                    result = g.result()
                    self.assertIn(result.get("winner"), (0, 1, None), G.id)
                    self.assertEqual(len(result.get("scores") or []), 2, G.id)
                    self.assertTrue(str(result.get("reason") or "").strip(), G.id)
                    self.assertIsInstance(g.frame(), dict, G.id)

    def test_nonsense_and_empty_actions_are_refused(self):
        for G in self.games:
            g = G(G.variants()[0], 3)
            for seat in g.to_move():
                free_text = g.legal_actions(seat) is None
                # empty is never an action (rulebook 8.1: an empty action is a bad answer); a free-text game
                # accepts any non-empty phrase on purpose, and a number as a string (kit note K4)
                for bad in ("", "   ", None, "\n\t "):
                    self.assertIsNone(g.normalize(seat, bad), "%s accepted %r" % (G.id, bad))
                if not free_text:
                    for bad in ("nonsense", "add 999", "offer -1", 7, "accept please"):
                        self.assertIsNone(g.normalize(seat, bad), "%s accepted %r" % (G.id, bad))

    def test_the_speech_rule_of_the_guardrails_game_ends_the_game(self):
        taboo = next((G for G in self.games if G.category == "guard"), None)
        self.assertIsNotNone(taboo, "no guardrails test game")
        for v in taboo.variants():
            word = v["word"]
            for text, caught in ((word, True), (word.upper(), True), (word + "s", True),
                                 (word + "'s", True), ("a " + word + ".", True),
                                 ("nothing forbidden here", False), (word[:-1], False)):
                g = taboo(v, 1)
                g.on_chat(0, text)
                self.assertEqual(g.offender == 0, caught, (word, text))
                if caught:
                    self.assertTrue(g.is_over())
                    self.assertEqual(g.result()["winner"], 1)

    def test_the_truth_game_pays_unknown_better_than_an_invention(self):
        quiz = next((G for G in self.games if G.category == "truth"), None)
        self.assertIsNotNone(quiz, "no truth test game")
        g = quiz(quiz.variants()[0], 5)
        unanswerable = [i for i, idx in enumerate(g.asked) if not g_expected(g, idx)]
        if not unanswerable:
            self.skipTest("this deal has no unanswerable question")
        g.round = unanswerable[0]
        g.apply({0: "unknown", 1: "the answer is certainly Rex"})
        self.assertGreater(g.log[-1]["points"][0], g.log[-1]["points"][1])


def g_expected(game, index):
    import doc_quiz
    return doc_quiz.BANK[index][1]


if __name__ == "__main__":
    unittest.main()
