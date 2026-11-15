"""scripts/engine_arena.py: the headless strength harness must not call the model and must play by the rules."""

import os
import unittest

from scripts import engine_arena as EA

HAVE_KIT = os.path.isfile(os.path.join(EA.REPO, "participant-kit", "arena-lite", "arena", "core.py"))


@unittest.skipUnless(HAVE_KIT, "needs participant-kit/arena-lite")
class EngineArenaTests(unittest.TestCase):

    def setUp(self):
        from bot import llm
        self.real = llm.complete
        self.addCleanup(setattr, llm, "complete", self.real)      # bot.llm is shared by the whole process

    def test_the_model_is_silenced_while_a_run_lasts_and_restored_after(self):
        from bot import llm
        real = EA.silence_model()
        self.assertIs(real, self.real)
        self.assertIsNone(llm.complete([{"role": "user", "content": "hello"}], timeout=5))
        EA.restore_model(real)
        self.assertIs(llm.complete, self.real)
        EA.run("taboo", "", ("random", "first"), 1, 2.0, seed=2)
        self.assertIs(llm.complete, self.real, "a run must put the model back")

    def test_a_game_of_ours_is_found_by_its_id(self):
        cls = EA.load_game("taboo")
        self.assertEqual(cls.id, "taboo")
        self.assertTrue(cls.variants())
        with self.assertRaises(SystemExit):
            EA.load_game("not-a-game-we-have")

    def test_random_against_first_plays_a_whole_game_without_the_model(self):
        out = EA.run("taboo", "", ("random", "first"), 4, 2.0, seed=3)
        self.assertEqual(out["games"], 4)
        self.assertEqual(sum(out["wins"]) + out["draws"], 4)
        self.assertEqual(out["illegal"], [0, 0])
        self.assertEqual(out["no_answer"], [0, 0])
        self.assertIn("random", out["players"])
        self.assertIn("vs", EA.report(out))

    def test_seats_are_swapped_so_a_first_move_edge_cannot_decide_it(self):
        out = EA.run("taboo", "", ("first", "random"), 6, 2.0, seed=1)
        swapped = EA.run("taboo", "", ("random", "first"), 6, 2.0, seed=1)
        self.assertEqual(out["games"], swapped["games"])
        self.assertEqual(out["wins"][0] + out["wins"][1], swapped["wins"][0] + swapped["wins"][1])

    def test_an_unprepared_game_says_so_instead_of_pretending(self):
        import tempfile
        from bot import memory
        memory.reset_root_for_tests(tempfile.mkdtemp(prefix="ld-arena-"))
        self.addCleanup(memory.reset_root_for_tests, None)
        out = EA.run("taboo", "", ("engine", "random"), 2, 2.0, seed=5)
        self.assertIsNone(out["specialist"])
        self.assertIn("none in /memory", EA.report(out))
        self.assertGreater(out["no_answer"][0], 0)        # the engine had nothing, the game went on anyway

    def test_the_result_is_reproducible_for_a_seed(self):
        first = EA.run("taboo", "", ("random", "first"), 5, 2.0, seed=11)
        again = EA.run("taboo", "", ("random", "first"), 5, 2.0, seed=11)
        self.assertEqual((first["wins"], first["draws"]), (again["wins"], again["draws"]))


if __name__ == "__main__":
    unittest.main()
