import os
import tempfile
import unittest

from bot import action_guard, memory
from bot.interfaces import GameProfile, VariantSpec


class ActionGuardTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ld-action-")
        memory.reset_root_for_tests(self.root)
        self.spec = VariantSpec(
            "wordle", "Wordle Duel", "4 letters",
            'Action format: guess one 4-letter word, exactly like "guess mint".',
            "rules-hash", profile=GameProfile(family="guessing", example_action="guess mint"),
        )

    def tearDown(self):
        memory.reset_root_for_tests(None)
        for root, dirs, files in os.walk(self.root, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.root)

    def _ctx(self, legal=None, warning=None):
        return {
            "spec": self.spec,
            "start": {"match_id": "m", "game_index": 1, "game_id": "wordle"},
            "turn": {"game_index": 1, "legal_actions": legal, "warning": warning},
        }

    def test_exact_legal_member_and_canonical_spelling(self):
        out = action_guard.emit_action(
            self._ctx(["guess MINT", "guess TONE"]),
            [{"action": "guess mint", "chat": "ready"}],
            0,
        )
        self.assertEqual(out["type"], "action")
        self.assertEqual(out["action"], "guess MINT")

    def test_null_legal_rejects_wrong_length_and_uses_grammar(self):
        out = action_guard.emit_action(
            self._ctx(),
            [{"action": "guess slate"}, {"action": "guess mint"}],
            0,
        )
        self.assertEqual(out["action"], "guess mint")

    def test_warning_blacklists_rejected_action(self):
        warning = {"count": 1, "max": 2, "reason": "illegal", "rejected": "guess mint"}
        out = action_guard.emit_action(
            self._ctx(["guess mint", "guess tone"], warning),
            [{"action": "guess mint"}],
            0,
        )
        self.assertEqual(out["action"], "guess tone")

    def test_bad_candidates_have_a_nonempty_fallback(self):
        out = action_guard.emit_action(
            self._ctx(),
            [{"action": ""}, {"action": "guess wrong"}],
            0,
        )
        self.assertTrue(out["action"].startswith("guess "))
        self.assertEqual(len(out["action"].split()[-1]), 4)

    def test_grammar_is_cached_atomically(self):
        action_guard.save_grammar(self.spec)
        path = os.path.join(memory.variant_dir("wordle", "rules-hash"), "grammar.json")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(memory.read_json(path)["version"], 1)

    def test_explicit_word_list_and_allocation_total_are_enforced(self):
        word_rules = "Action format: guess one 4-letter word. Allowed words: MINT, TONE."
        word_spec = VariantSpec("wordle", "Wordle", "v", word_rules, "word-rules",
                                profile=GameProfile(family="guessing"))
        word_ctx = dict(self._ctx())
        word_ctx["spec"] = word_spec
        out = action_guard.emit_action(word_ctx, [{"action": "guess zzzz"}, {"action": "guess tone"}], 0)
        self.assertEqual(out["action"], "guess tone")

        allocation_rules = "Action format: four fields of non-negative integers with total 10."
        allocation_spec = VariantSpec("blotto", "Blotto", "v", allocation_rules, "alloc-rules")
        allocation_ctx = dict(self._ctx())
        allocation_ctx["spec"] = allocation_spec
        out = action_guard.emit_action(allocation_ctx, [{"action": "1 2 3 3"}, {"action": "2 2 3 3"}], 0)
        self.assertEqual(out["action"], "2 2 3 3")

    def test_only_guard_creates_action_protocol_objects(self):
        root = os.path.dirname(os.path.dirname(__file__))
        offenders = []
        for current, _, names in os.walk(os.path.join(root, "bot")):
            for name in names:
                if not name.endswith(".py") or name == "action_guard.py":
                    continue
                path = os.path.join(current, name)
                with open(path, encoding="utf-8") as handle:
                    if "protocol.action(" in handle.read():
                        offenders.append(os.path.relpath(path, root))
        self.assertEqual(offenders, [], offenders)

