"""Prep tests: profile classification (feature 1) and simulator generation (feature 2).

Owner: luckydevs-3. The model is replaced with unittest.mock.patch on
bot.llm.complete; memory goes to a temp folder.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from bot import memory
from bot.interfaces import GameProfile, VariantSpec
from bot.prep import classify

SNIPPETS = {
    "bluff": [
        "Each player is dealt one private card. Players bet chips into the pot; you may raise or fold.",
        "Ante one chip. Bluff if you like: at the showdown the higher card wins the pot.",
    ],
    "deals": [
        "Players negotiate how to split 100 coins. On your turn make an offer; the other player may accept or reject it.",
        "Sealed-bid auction: each player has a private value for the item and submits bids.",
    ],
    "guessing": [
        "Guess the secret code. After each guess you get feedback: how many symbols are correct.",
        "Find the secret number with as few guesses as possible; you are told higher or lower.",
    ],
    "think_ahead": [
        "Players take turns placing stones on a 7x7 board. Five in a row wins.",
        "Alternate moves on a grid; capture the opponent's pieces by jumping over them.",
    ],
    "read_opponent": [
        "Both players choose a number at the same time; neither sees the other's choice. Payoffs are given below.",
        "Simultaneous moves: each round both players pick left or right without seeing the other.",
    ],
    "truth": [
        "Answer each question using only the document below. If the document does not say, reply unknown; an invented answer costs more.",
        "Read the passage. Answer questions according to the text; never invent facts.",
    ],
    "guardrails": [
        "Answer the user's questions about travel. The forbidden subject is weather: never mention it.",
        "You must not mention the color of the sky. Taboo words are banned in chat too.",
    ],
}

TTT_RULES = ('Board game on a 3x3 grid. Players take turns. Cells are a1..c3. '
             'Action format: send one empty cell name, exactly like "b2". Three in a row wins.')
TTT_SAMPLE = {"type": "turn", "turn": 1, "observation": "board empty", "legal_actions":
              ["a1", "b1", "c1", "a2", "b2", "c2", "a3", "b3", "c3"], "seconds": 20, "chat": []}
GUESS_RULES = ('Guess your secret number from 1 to 100; you get feedback higher or lower. '
               'Action format: the word guess, a space and a number, exactly like "guess 50".')


def answer(**kw):
    base = {"family": "think_ahead", "family_quote": "Players take turns",
            "perfect_information": True, "simultaneous": False, "turn_structure": "alternating",
            "hidden_information": "none", "chance": "none", "speech_restrictions": "",
            "action_format": "one empty cell name", "action_regex": "[a-c][1-3]",
            "example_action": "b2", "playbook": "Take the center.\nBlock threats."}
    base.update(kw)
    return json.dumps(base)


class TempMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ldprep-")
        memory.reset_root_for_tests(self.dir)

    def tearDown(self):
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.dir, ignore_errors=True)


class EvidenceTests(unittest.TestCase):

    def test_families_from_snippets(self):
        for fam, texts in SNIPPETS.items():
            for t in texts:
                ev = classify.evidence(t)
                self.assertEqual(ev["family"], fam, "%s: %r -> %s %s" % (fam, t, ev["family"], ev["scores"]))
                self.assertTrue(ev["confident"], "%s not confident: %r %s" % (fam, t, ev["scores"]))

    def test_no_signal_not_confident(self):
        ev = classify.evidence("Pick a number. Highest number wins.")
        self.assertFalse(ev["confident"])
        self.assertEqual(ev["family"], "unknown")

    def test_sample_turn_facts_and_examples(self):
        ev = classify.evidence(TTT_RULES, TTT_SAMPLE)
        self.assertEqual(ev["legal_kind"], "list")
        self.assertEqual(ev["legal_count"], 9)
        self.assertEqual(ev["quoted_examples"], ["b2"])
        self.assertIn("Action format", ev["format_sentences"][0])
        self.assertFalse(ev["simultaneous"])
        ev2 = classify.evidence("x", {"observation": "o", "legal_actions": None,
                                      "chat": [{"kind": "move", "text": "hi"}]})
        self.assertEqual(ev2["legal_kind"], "null")
        self.assertTrue(ev2["talk_is_move"])

    def test_example_from_rules_unchanged(self):
        self.assertEqual(classify.example_from_rules(GUESS_RULES), "guess 50")
        self.assertEqual(classify.example_from_rules('one of "1", "2", "3", exactly like "2".'), "2")
        self.assertEqual(classify.example_from_rules("no quotes here"), "")

    def test_garbage_never_raises(self):
        for rules, sample in [(None, None), ("", {}), (123, "x"), ("a" * 50000, {"legal_actions": 5}),
                              ("\x00\xff", {"legal_actions": [1, None], "chat": "bad"})]:
            ev = classify.evidence(rules, sample)
            self.assertIn("family", ev)


class ClassifyTests(TempMemory):

    def run_classify(self, replies, rules=TTT_RULES, sample=TTT_SAMPLE, timeout=60):
        replies = list(replies)
        calls = []

        def fake(messages, **kw):
            calls.append(messages)
            return replies.pop(0) if replies else None
        with mock.patch("bot.llm.complete", side_effect=fake):
            prof, playbook, analysis = classify.analyze_variant("G", "v", rules, sample, timeout)
        return prof, playbook, analysis, calls

    def test_good_answer_no_repair(self):
        prof, pb, a, calls = self.run_classify([answer()])
        self.assertEqual(len(calls), 1)
        self.assertIn("TASK: classify", calls[0][1]["content"])
        self.assertEqual(prof.family, "think_ahead")
        self.assertEqual(a["family_source"], "model")
        self.assertEqual(a["confidence"], "high")
        self.assertEqual(prof.example_action, "b2")
        self.assertEqual(a["action_regex"], "[a-c][1-3]")
        self.assertFalse(a["repair_used"])

    def test_invalid_family_repaired_once(self):
        bad = answer(family="chess")
        prof, _, a, calls = self.run_classify([bad, bad, bad])
        self.assertEqual(len(calls), 2)                      # first + ONE repair
        self.assertIn("TASK: classify", calls[1][-1]["content"])
        self.assertIn("not one of", calls[1][-1]["content"])
        self.assertEqual(prof.family, "think_ahead")         # from confident evidence
        self.assertEqual(a["family_source"], "evidence")

    def test_unknown_family_uses_evidence_without_repair(self):
        prof, _, a, calls = self.run_classify([answer(family="unknown")])
        self.assertEqual(len(calls), 1)
        self.assertEqual(prof.family, "think_ahead")

    def test_quote_not_in_rules(self):
        prof, _, a, calls = self.run_classify([answer(family_quote="the knight moves in an L shape"),
                                               answer()])
        self.assertEqual(len(calls), 2)
        self.assertIn("not a verbatim quote", calls[1][-1]["content"])
        self.assertEqual(prof.family, "think_ahead")
        self.assertTrue(a["quote_ok"])

    def test_family_disagrees_with_confident_evidence(self):
        prof, _, a, calls = self.run_classify([answer(family="bluff", perfect_information=False,
                                                      family_quote="Players take turns")] * 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(prof.family, "think_ahead")
        self.assertTrue(any("point to" in d for d in a["disagreements"]))

    def test_example_outside_legal_list(self):
        prof, _, a, calls = self.run_classify([answer(example_action="d4")])
        self.assertEqual(prof.example_action, "a1")          # sample's first legal action
        prof, _, a, calls = self.run_classify([answer(example_action=" B2 ")])
        self.assertEqual(prof.example_action, "b2")          # arena spelling

    def test_example_must_match_format_without_list(self):
        prof, _, a, calls = self.run_classify([answer(family="guessing", family_quote="you get feedback higher or lower",
                                                      perfect_information=False, action_regex=r"guess \d+",
                                                      example_action="50")], rules=GUESS_RULES, sample=None)
        self.assertEqual(prof.example_action, "guess 50")    # rules quote, valid by construction
        self.assertEqual(a["action_regex"], "")              # regex did not match the model's example
        prof, _, a, _ = self.run_classify([answer(family="guessing", family_quote="you get feedback higher or lower",
                                                  perfect_information=False, action_regex=r"guess \d{1,3}",
                                                  example_action="guess 37")], rules=GUESS_RULES, sample=None)
        self.assertEqual(a["action_regex"], r"guess \d{1,3}")
        self.assertEqual(prof.example_action, "guess 50")

    def test_untrusted_example_kept_when_nothing_else(self):
        rules = "Pick a number. Send the word pick and a number."
        prof, _, a, calls = self.run_classify([answer(family="unknown", action_regex="(", example_action="pick 3")] * 2,
                                              rules=rules, sample=None)
        self.assertEqual(prof.example_action, "pick 3")
        self.assertEqual(a["example_source"], "model-unverified")

    def test_empty_action_format_falls_back_to_rules(self):
        prof, _, _, _ = self.run_classify([answer(action_format="")])
        self.assertIn("exactly like", prof.action_format)

    def test_guardrails_without_restriction(self):
        rules = SNIPPETS["guardrails"][0]
        g = answer(family="guardrails", family_quote="never mention it", perfect_information=True,
                   speech_restrictions="", example_action="hello", action_regex="")
        prof, _, a, calls = self.run_classify([g, g], rules=rules, sample=None)
        self.assertEqual(len(calls), 2)
        self.assertIn("speech_restrictions", calls[1][-1]["content"])
        self.assertEqual(prof.family, "guardrails")
        self.assertIn("weather", prof.speech_restrictions)   # evidence sentence as fallback

    def test_model_none_gives_evidence_profile(self):
        prof, pb, a, calls = self.run_classify([None])
        self.assertEqual(len(calls), 1)
        self.assertEqual(prof.family, "think_ahead")
        self.assertEqual(prof.example_action, "a1")
        self.assertFalse(a["model_answered"])
        prof, pb, a, _ = self.run_classify([None], rules=GUESS_RULES, sample=None)
        self.assertEqual((prof.family, prof.example_action), ("guessing", "guess 50"))

    def test_garbage_answers_never_raise(self):
        for junk in ["", "not json", "[1, 2]", '{"family": 5, "playbook": {"a": 1}}',
                     '{"example_action": null, "action_regex": "((((", "family": null}']:
            prof, pb, a, _ = self.run_classify([junk, junk])
            self.assertIsInstance(prof, GameProfile)
            self.assertTrue(prof.example_action)

    def test_no_repair_without_time(self):
        prof, _, a, calls = self.run_classify([answer(family="chess")], timeout=3)
        self.assertEqual(len(calls), 1)

    def test_no_call_after_deadline(self):
        prof, _, a, calls = self.run_classify([answer()], timeout=0.5)
        self.assertEqual(calls, [])
        self.assertEqual(prof.family, "think_ahead")

    def test_playbook_cap(self):
        long = "\n".join("line %d %s" % (i, "x" * 100) for i in range(30))
        prof, pb, _, _ = self.run_classify([answer(playbook=long)])
        self.assertLessEqual(len(pb), classify.PLAYBOOK_MAX_CHARS)
        self.assertLessEqual(len(pb.splitlines()), classify.PLAYBOOK_MAX_LINES)
        self.assertTrue(pb.startswith("line 0"))

    def test_align_families(self):
        specs = []
        analyses = {}
        for i, fam in enumerate(["guessing", "guessing", "think_ahead", "unknown"]):
            s = VariantSpec("g", "G", "v%d" % i, "r%d" % i, "h%d" % i, profile=GameProfile(family=fam))
            specs.append(s)
            analyses[s.rules_hash] = {"family": fam, "evidence": {"scores": {}}}
        changed = classify.align_families(specs, analyses)
        self.assertEqual({s.profile.family for s in specs}, {"guessing"})
        self.assertEqual(len(changed), 2)
        self.assertEqual(analyses["h2"]["family_source"], "aligned")
        # Tie: the family with more evidence wins.
        a = VariantSpec("g", "G", "a", "ra", "ha", profile=GameProfile(family="bluff"))
        b = VariantSpec("g", "G", "b", "rb", "hb", profile=GameProfile(family="deals"))
        an = {"ha": {"evidence": {"scores": {"bluff": 1, "deals": 3}}},
              "hb": {"evidence": {"scores": {"deals": 2}}}}
        classify.align_families([a, b], an)
        self.assertEqual((a.profile.family, b.profile.family), ("deals", "deals"))

    def test_analysis_written_before_meta_and_versioned(self):
        from bot import specialist
        with mock.patch("bot.llm.complete", return_value=answer()):
            spec = specialist.build_variant("gameA", "G", "v", TTT_RULES, TTT_SAMPLE, time.monotonic() + 60)
        d = memory.variant_dir("gameA", spec.rules_hash)
        self.assertTrue(os.path.exists(os.path.join(d, "analysis.json")))
        self.assertTrue(os.path.exists(os.path.join(d, "meta.json")))
        a = classify.load_analysis("gameA", spec.rules_hash)
        self.assertEqual(a["family"], "think_ahead")
        a["harness_version"] = "old"
        memory.write_json_atomic(os.path.join(d, "analysis.json"), a)
        self.assertIsNone(classify.load_analysis("gameA", spec.rules_hash))


if __name__ == "__main__":
    unittest.main()
