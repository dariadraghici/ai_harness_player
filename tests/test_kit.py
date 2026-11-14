"""Tests added with the participant kit: real message shapes and the official arena-lite.

Owner: luckydevs-1. The arena-lite tests are skipped when participant-kit/ is
not present (it is not in our Git; see READMEFIRST.md).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from bot import memory, specialist
from bot.chat import decide, defense
from bot.interfaces import GameProfile, VariantSpec
from bot.prep.classify import example_from_rules

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LITE = os.environ.get("BOT_ARENA_LITE") or os.path.join(REPO, "participant-kit", "arena-lite")
HAVE_LITE = os.path.isfile(os.path.join(LITE, "arena", "__main__.py"))

# The action format section of an official practice game (Guess the Number), shortened.
KIT_RULES = '''GUESS THE NUMBER - range 1-100, at most 12 guesses each.

ACTION FORMAT
    guess <n>
where <n> is a whole number from 1 to 100 (digits only). Examples:  "guess 50"   "guess 1"   "100"
(a bare number also works).

HOW THE GAME ENDS AND IS SCORED
the player whose feedback narrows the number down to FEWER possible values wins
(e.g. "between 40 and 44" = 5 values beats "between 10 and 29" = 20 values).'''


class KitShapeTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ldmem-kit-")
        memory.reset_root_for_tests(self.dir)

    def tearDown(self):
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_example_action_from_kit_rules(self):
        # "Examples:" must win over a later 'e.g. "between 40 and 44"' that is not an action.
        self.assertEqual(example_from_rules(KIT_RULES), "guess 50")

    def test_prepared_spec_found_by_label_when_rules_differ(self):
        prep_rules = "Rules of the variant.\nSample deal: heaps 3, 4, 5."
        game_rules = "Rules of the variant.\nThis game: heaps 7, 1, 2."
        spec = VariantSpec(game_id="kitgame", game_name="Kit game", label="three heaps", rules=prep_rules,
                           rules_hash=memory.rules_hash(prep_rules),
                           profile=GameProfile(family="think_ahead", example_action="take 1 from A"),
                           playbook="leave a balanced position", source="prep")
        self.assertTrue(memory.save_variant(spec))
        start = {"game_id": "kitgame", "game_name": "Kit game", "rules": game_rules,
                 "variant": {"label": "three heaps", "heaps": 3}}
        got = specialist.for_start(start, time.monotonic() + 5)
        self.assertEqual(got.source, "label")                    # no cold prep, no model call
        self.assertEqual(got.profile.family, "think_ahead")
        self.assertEqual(got.playbook, "leave a balanced position")
        self.assertEqual(got.rules, game_rules)                  # the decision sees THIS game's rules
        self.assertEqual(got.rules_hash, spec.rules_hash)        # still the storage key of prep's work
        # Another label of the same game is not a match: that is a cold prep.
        other = dict(start, variant={"label": "five heaps"})
        self.assertEqual(specialist.for_start(other, time.monotonic() + 30).source, "cold")

    def test_label_lookup_prefers_this_process(self):
        a = VariantSpec("kitgame2", "G", "v1", "rules A", memory.rules_hash("rules A"),
                        playbook="from this process", source="prep")
        specialist.put(a)
        got = specialist.lookup_by_label("kitgame2", "v1", "rules B")
        self.assertEqual((got.playbook, got.rules, got.source), ("from this process", "rules B", "label"))
        self.assertIsNone(specialist.lookup_by_label("kitgame2", "", "rules B"))
        self.assertIsNone(specialist.lookup_by_label("kitgame2", "v9", "rules B"))
        self.assertEqual(a.rules, "rules A")                     # the prepared spec is not changed

    def test_variant_settings_reach_the_decision_prompt(self):
        start = {"seat": 1, "variant": {"label": "3x3 classic", "size": 3, "connect": 3}}
        turn = {"observation": "obs", "legal_actions": ["A1"], "chat": []}
        msgs = decide.build_decide_messages(VariantSpec("g", "G", "3x3 classic", "RULES", "h"), start, turn,
                                            defense.build_context([]), [], ["A1"])
        self.assertIn('"connect": 3', msgs[1]["content"])


@unittest.skipUnless(HAVE_LITE, "participant-kit/arena-lite not present")
class ArenaLiteTests(unittest.TestCase):
    """Our bot on the organizers' engine, fake LLM: the result must be CLEAN."""

    def _practice(self, *args):
        r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "practice.py"), "--fake",
                            "--set", "BOT_LOG_LEVEL=info"] + list(args),
                           cwd=REPO, capture_output=True, text=True, timeout=240,
                           env=dict(os.environ, ARENA_ROOT=self.root, BOT_ARENA_LITE=LITE))
        return r.returncode, r.stdout + r.stderr

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ld-arena-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cold_match_is_clean(self):
        code, out = self._practice("--game", "tic_tac_toe", "--seed", "3")
        self.assertIn("RESULT: CLEAN", out, out[-1500:])
        self.assertEqual(code, 0)

    def test_null_legal_actions_match_is_clean(self):
        code, out = self._practice("--game", "guess_number", "--seed", "3")
        self.assertIn("RESULT: CLEAN", out, out[-1500:])

    def test_prep_then_warm_match_is_clean(self):
        code, out = self._practice("--game", "pig", "--prep", "--seed", "3")
        self.assertIn("prep luckydevs x pig: ready", out, out[-1500:])
        self.assertIn("RESULT: CLEAN", out, out[-1500:])

    def test_prepared_specialist_survives_a_new_deal_in_the_rules(self):
        # nim_deal writes the dealt heaps into the rules text: prep and start differ (kit note K2).
        code, out = self._practice("--game", "nim_deal", "--prep", "--seed", "4")
        self.assertIn("RESULT: CLEAN", out, out[-1500:])
        log_path = os.path.join(self.root, "results", "practice", "logs", "warm", "luckydevs--nim_deal.log")
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log_text = f.read()
        self.assertIn("reused by label", log_text)
        self.assertNotIn("cold prep", log_text)

    def test_own_simultaneous_test_game_is_clean(self):
        code, out = self._practice("--game", "rps_twist", "--seed", "3")
        self.assertIn("RESULT: CLEAN", out, out[-1500:])


if __name__ == "__main__":
    unittest.main()
