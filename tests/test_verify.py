"""Verification tests (bot/prep/verify.py, bot/prep/verify_job.py).

Owner: luckydevs-3. Fixture simulators are written by hand, one defect each;
they only ever run inside a sandbox child. The model is replaced with
unittest.mock.patch on bot.llm.complete where a repair is tested.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from bot import ladder, memory, specialist
from bot.interfaces import GameProfile, VariantSpec
from bot.prep import codegen, pipeline, sandbox, verify
from tests.test_sandbox import GOOD, OBS

RULES = ("Pile game. A pile of stones, seat 0 moves first, players alternate. On your turn take 1, 2 or 3 "
         "stones. Action format: exactly like \"take 2\". Whoever takes the last stone wins.")
SAMPLE = {"type": "turn", "turn": 3, "observation": OBS, "legal_actions": ["take 1", "take 2", "take 3"],
          "seconds": 20, "chat": []}

BROKEN = {
    "wrong_legal": GOOD.replace("for n in (1, 2, 3) if", "for n in (1, 2) if"),
    "other_order": GOOD.replace("for n in (1, 2, 3) if", "for n in (3, 2, 1) if"),
    "mutates": GOOD.replace("    s = copy.deepcopy(state)\n", "    s = state\n"),
    "not_deterministic": GOOD.replace("import copy\n", "import copy\nimport itertools\n\nCOUNTER = itertools.count()\n")
                             .replace("    s[\"stones\"] -= int", "    s[\"n\"] = next(COUNTER)\n    s[\"stones\"] -= int"),
    "never_terminal": GOOD.replace("        s[\"winner\"] = s[\"to_move\"]\n", "        pass\n"),
    "returns_range": GOOD.replace("[1.0, -1.0] if", "[2.0, -2.0] if"),
    "no_value_error": GOOD.replace("    if action not in legal_actions(state):\n        raise ValueError(\"illegal action: %r\" % (action,))\n",
                                   "    if action not in legal_actions(state):\n        return copy.deepcopy(state)\n"),
    "parser_seat0_only": GOOD.replace("    stones = int(", "    if \"seat 0\" not in observation:\n"
                                      "        raise ValueError(\"unexpected header\")\n    stones = int("),
    # A different rule, consistent with itself: with 4 stones left you may not take 3.
    "other_rule": GOOD.replace("if n <= state[\"stones\"]]", "if n <= state[\"stones\"] and not (n == 3 and state[\"stones\"] == 4)]"),
    "no_constant": GOOD.replace("LEGAL_ACTIONS_COMPLETE = True\n", ""),
    "one_seat_only": GOOD.replace("    s[\"to_move\"] = 1 - s[\"to_move\"]\n", ""),
    "parse_raises": GOOD.replace("    stones = int(", "    raise ValueError(\"cannot read this\")\n    stones = int("),
    "endless": GOOD.replace("def legal_actions(state):\n", "def legal_actions(state):\n    while True:\n        pass\n"),
}

# Simultaneous game (C5): both pick 0 or 1; three rounds; seat 0 scores on equal picks.
PENNIES = '''"""Matching pennies."""
import copy

LEGAL_ACTIONS_COMPLETE = True
ROUNDS = 3


def parse_observation(observation, variant, seat):
    done = int(observation.split("Round ")[1].split(" ")[0]) - 1
    return {"me": seat, "done": done, "points": [0, 0], "seed": 0}


def legal_actions(state):
    return [] if state["done"] >= ROUNDS else ["heads", "tails"]


def apply_action(state, action):
    if not isinstance(action, list) or len(action) != 2 or any(a not in ("heads", "tails") for a in action):
        raise ValueError("a simultaneous step needs [seat 0 action, seat 1 action]")
    s = copy.deepcopy(state)
    s["points"][0 if action[0] == action[1] else 1] += 1
    s["done"] += 1
    return s


def is_terminal(state):
    return state["done"] >= ROUNDS


def returns(state):
    d = state["points"][0] - state["points"][1]
    return [d / float(ROUNDS), -d / float(ROUNDS)]


def current_player(state):
    return -1
'''
PENNIES_OBS = "PENNIES. You are seat 0. Round 1 of 3. Choose: heads | tails"

# Chance game (C11): roll a die until 10 points; the die comes from state["seed"].
DICE = '''"""Die race."""
import copy
import random

LEGAL_ACTIONS_COMPLETE = True


def parse_observation(observation, variant, seat):
    return {"me": seat, "points": [0, 0], "to_move": seat, "seed": 0}


def legal_actions(state):
    return [] if max(state["points"]) >= 10 else ["roll"]


def apply_action(state, action):
    if action != "roll" or max(state["points"]) >= 10:
        raise ValueError("illegal action")
    s = copy.deepcopy(state)
    rng = random.Random(s["seed"])
    s["points"][s["to_move"]] += rng.randint(1, 6)
    s["seed"] = rng.randrange(2 ** 31)
    s["to_move"] = 1 - s["to_move"]
    return s


def is_terminal(state):
    return max(state["points"]) >= 10


def returns(state):
    return [1.0, -1.0] if state["points"][0] >= 10 else [-1.0, 1.0]


def current_player(state):
    return state["to_move"]
'''
DICE_NO_SEED = DICE.replace("rng.randint(1, 6)", "3")
DICE_OBS = "DIE RACE. You are seat 0. Points: you 0, opponent 0. Choose: roll"


def fenced(code):
    return "Here it is:\n```python\n%s```\n" % code


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ld-verify-")
        memory.reset_root_for_tests(self.dir)
        specialist._registry.clear()
        for name, value in (("VERIFY_JOB_BUDGET_S", 0.3), ("VERIFY_JOB_EXTRA_S", 3.0), ("VERIFY_MIN_S", 1.0),
                            ("VERIFY_GAMES", 8)):                # small and fast: the fixtures are tiny games
            p = mock.patch.object(verify, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        sandbox.kill_all("test teardown")
        self.assertEqual(sandbox.live_count(), 0)
        memory.reset_root_for_tests(None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def spec(self, game="pile", sample=SAMPLE, rules=RULES, **profile):
        kw = dict(family="think_ahead", perfect_information=True, simultaneous=False, example_action="take 1")
        kw.update(profile)
        return VariantSpec(game_id=game, game_name="Pile", label="std", rules=rules,
                           rules_hash=memory.rules_hash(rules), profile=GameProfile(**kw), sample_turn=sample)

    def write(self, name, code):
        path = os.path.join(self.dir, name + ".py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        return path

    def report(self, code, spec=None, analysis=None, **kw):
        return verify.verify_simulator(spec or self.spec(), self.write("cand", code), time.monotonic() + 30,
                                       analysis or {}, **kw)

    def candidates(self, spec, codes):
        """Put static-ok candidates sim_1.. into the variant folder, like codegen does."""
        d = codegen.candidates_dir(spec)
        entries = []
        for i, code in enumerate(codes, 1):
            memory.write_text_atomic(os.path.join(d, "sim_%d.py" % i), code)
            entries.append({"n": i, "file": "sim_%d.py" % i, "brief": "b%d" % i, "static_ok": True,
                            "failures": [], "repaired": False, "chars": len(code)})
        codegen._update_manifest(spec, entries)
        return d


class CheckTests(Base):

    def test_good_module_passes_every_check(self):
        r = self.report(GOOD)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.checks, ["load", "parse", "legal", "contract", "selfplay"])
        self.assertEqual((r.failures, r.warnings, r.error), ([], [], ""))
        self.assertTrue(r.exact_order and r.both_seats and r.legal_complete)
        self.assertGreater(r.games, 2)
        self.assertGreater(r.playouts_per_s, 100)
        self.assertGreater(r.steps_per_game, 1)
        self.assertEqual(len(r.traces), verify.VERIFY_TRACE_GAMES)

    def test_each_defect_fails_its_check_with_a_sentence(self):
        cases = {
            "wrong_legal": ("legal", "misses [\"take 3\"]"),
            "mutates": ("contract", "changed its input state"),
            "not_deterministic": ("contract", "two different results"),
            "never_terminal": ("selfplay", "the game cannot continue"),
            "returns_range": ("selfplay", "in [-1, 1]"),
            "no_constant": ("load", "LEGAL_ACTIONS_COMPLETE"),
            "parse_raises": ("parse", "raised ValueError: cannot read this on the sample observation"),
        }
        for name, (check, words) in cases.items():
            r = self.report(BROKEN[name])
            self.assertFalse(r.passed, name)
            self.assertIn(check, r.failed_checks, name)
            self.assertTrue(any(words in f for f in r.failures), "%s: %s" % (name, r.failures))
            self.assertTrue(all(len(f) < 900 and "\n" not in f for f in r.failures), name)

    def test_soft_findings_do_not_fail(self):
        r = self.report(BROKEN["other_order"])
        self.assertTrue(r.passed)
        self.assertFalse(r.exact_order)                          # same strings, another order
        r = self.report(BROKEN["no_value_error"])
        self.assertTrue(r.passed)
        self.assertTrue(any("ValueError" in w for w in r.warnings))
        r = self.report(BROKEN["one_seat_only"])
        self.assertTrue(r.passed)
        self.assertFalse(r.both_seats)
        self.assertTrue(any("only seat" in w for w in r.warnings))

    def test_sandbox_level_failures_become_sentences(self):
        with mock.patch.object(verify, "VERIFY_JOB_EXTRA_S", 1.0):
            r = verify.verify_simulator(self.spec(), self.write("cand", BROKEN["endless"]),
                                        time.monotonic() + 2.5, {})
        self.assertEqual((r.passed, r.error), (False, "timeout"))
        self.assertIn("did not finish the checks", r.failures[0])
        r = verify.verify_simulator(self.spec(), self.write("cand", "def broken(:\n"), time.monotonic() + 30, {})
        self.assertIn("does not load", r.failures[0])
        r = verify.verify_simulator(self.spec(), os.path.join(self.dir, "missing.py"), time.monotonic() + 30, {})
        self.assertFalse(r.passed)

    def test_no_sample_and_no_time(self):
        with mock.patch("bot.prep.sandbox.run_job") as job:
            r = verify.verify_simulator(self.spec(sample=None), self.write("cand", GOOD), time.monotonic() + 30, {})
            self.assertEqual((r.passed, r.error), (False, "no-sample"))
            r = verify.verify_simulator(self.spec(), self.write("cand", GOOD), time.monotonic() - 1, {})
            self.assertEqual((r.passed, r.error), (False, "no-time"))
            job.assert_not_called()                                  # no child was started

    def test_simultaneous_contract(self):
        sample = dict(SAMPLE, observation=PENNIES_OBS, legal_actions=["heads", "tails"])
        spec = self.spec(game="pennies", sample=sample, family="read_opponent", perfect_information=False,
                         simultaneous=True, example_action="heads")
        self.assertFalse(verify.has_unknown_outcomes(spec, {"chance": "none"}))   # both actions are given (C5)
        r = self.report(PENNIES, spec)
        self.assertTrue(r.passed, r.failures)
        bad = self.report(PENNIES.replace("    return -1\n", "    return state[\"me\"]\n"), spec)
        self.assertIn("parse", bad.failed_checks)
        self.assertTrue(any("-1 (simultaneous step)" in f for f in bad.failures), bad.failures)

    def test_chance_game_needs_the_seed(self):
        sample = dict(SAMPLE, observation=DICE_OBS, legal_actions=["roll"])
        spec = self.spec(game="dice", sample=sample, example_action="roll")
        analysis = {"chance": "one six-sided die per roll"}
        self.assertTrue(verify.has_unknown_outcomes(spec, analysis))
        r = self.report(DICE, spec, analysis)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.checks, ["load", "parse", "legal", "contract", "selfplay", "seed"])
        self.assertEqual(r.traces, [])                               # no trajectory comparison with chance
        bad = self.report(DICE_NO_SEED, spec, analysis)
        self.assertEqual(bad.failed_checks, ["seed"])
        self.assertIn("C11", bad.failures[0])
        no_key = self.report(DICE.replace(", \"seed\": 0}", "}").replace("random.Random(s[\"seed\"])", "random.Random(1)")
                             .replace("    s[\"seed\"] = rng.randrange(2 ** 31)\n", ""), spec, analysis)
        self.assertTrue(any("state[\"seed\"] = 0" in f for f in no_key.failures), no_key.failures)

    def test_stored_real_turns_are_used(self):
        spec = self.spec()
        path = verify.stored_turns_file(spec)
        os.makedirs(os.path.dirname(path))
        rows = [{"seat": 1, "variant": {"label": "std", "stones": 9},
                 "turn": {"observation": "PILE. You are seat 1. Stones left: 2. Your turn.",
                          "legal_actions": ["take 1", "take 2"]}},
                "garbage", {"seat": 7, "turn": {"observation": "x"}}]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
        samples = verify.turn_samples(spec)
        self.assertEqual([(s["kind"], s["seat"]) for s in samples], [("prep", 0), ("stored", 1)])
        good = self.report(GOOD, spec)
        self.assertTrue(good.passed, good.failures)
        self.assertIn("stored_turns", good.checks)
        bad = self.report(BROKEN["parser_seat0_only"], spec)
        self.assertEqual(bad.failed_checks, ["stored_turns"])
        self.assertIn("this real observation of seat 1", bad.failures[0])

    def test_differential_minority_and_split(self):
        spec = self.spec()
        reps = {1: self.report(GOOD, spec), 2: self.report(BROKEN["other_order"], spec),
                3: self.report(BROKEN["other_rule"], spec)}
        self.assertTrue(all(r.own_passed for r in reps.values()))    # each one is consistent with itself
        summary = verify.compare(reps)
        self.assertEqual(summary["minority"], [3])
        self.assertEqual([reps[k].agreement for k in (1, 2, 3)], ["majority", "majority", "minority"])
        self.assertFalse(reps[3].passed)
        self.assertIn("agreement", reps[3].failed_checks)
        sentence = reps[3].failures[-1]
        self.assertIn("differential test: after the actions", sentence)
        self.assertIn("2 other independently written implementations", sentence)
        two = {1: self.report(GOOD, spec), 3: self.report(BROKEN["other_rule"], spec)}
        self.assertTrue(verify.compare(two)["split"])                # 1 against 1: nobody is punished
        self.assertTrue(two[1].passed and two[3].passed)
        self.assertEqual(two[1].agreement, "split")
        alone = {1: self.report(GOOD, spec), 2: self.report(BROKEN["wrong_legal"], spec)}
        verify.compare(alone)
        self.assertEqual((alone[1].agreement, alone[2].agreement), ("alone", "n/a"))

    def test_score_order(self):
        spec = self.spec()
        good, order, bad = self.report(GOOD, spec), self.report(BROKEN["other_order"], spec), \
            self.report(BROKEN["wrong_legal"], spec)
        self.assertGreater(verify.score(good, False), verify.score(order, False))    # exact order
        self.assertGreater(verify.score(order, False), verify.score(bad, False))     # verified first
        self.assertGreater(verify.score(good, False)[:5], verify.score(good, True)[:5])   # not repaired

    def test_check_turn_for_the_live_check(self):
        w = sandbox.Worker(self.write("sim", GOOD))
        try:
            self.assertEqual(verify.check_turn(w, SAMPLE, {"label": "std"}, 0), (True, ""))
            ok, why = verify.check_turn(w, dict(SAMPLE, legal_actions=["take 1"]), {}, 0)
            self.assertFalse(ok)
            self.assertIn("legal actions differ", why)
            ok, why = verify.check_turn(w, {"observation": "nonsense"}, {}, 0)
            self.assertFalse(ok)
        finally:
            w.close()


class VariantTests(Base):

    def test_winner_files_and_spec_fields_without_a_model_call(self):
        spec = self.spec()
        self.candidates(spec, [BROKEN["other_order"], GOOD, GOOD])
        with mock.patch("bot.llm.complete") as llm:
            out = verify.verify_variant(spec, time.monotonic() + 60, {})
            llm.assert_not_called()
        self.assertEqual((out["status"], out["verified"], out["rounds"]), ("verified", True, 0))
        self.assertIn(out["winner"], ("sim_2.py", "sim_3.py"))       # exact order beats sim_1; then speed
        d = memory.variant_dir(spec.game_id, spec.rules_hash)
        self.assertEqual(memory.read_text(os.path.join(d, "simulator.py")), GOOD)
        self.assertEqual(spec.simulator_path, os.path.join(d, "simulator.py"))
        self.assertTrue(spec.verified)
        v = memory.read_json(os.path.join(d, "verify.json"))
        self.assertTrue(v["verified"])
        self.assertEqual(v["winner"]["file"], out["winner"])
        self.assertEqual(v["contract"], codegen.contract_version())
        self.assertEqual(v["samples"], {"prep": 1, "stored": 0})
        self.assertEqual(len(v["candidates"]), 3)
        for c in v["candidates"]:
            for k in ("n", "file", "verified", "checks", "failures", "playouts_per_s", "agreement", "repair_rounds"):
                self.assertIn(k, c)
        self.assertGreater(v["winner"]["playouts_per_s"], 0)
        self.assertTrue(v["limits"])
        meta = memory.read_json(os.path.join(d, "meta.json"))
        self.assertTrue(meta["verified"])
        loaded = memory.load_variant(spec.game_id, spec.rules_hash, RULES)     # another container
        self.assertTrue(loaded.verified and verify.is_done(loaded))
        # A second run does not verify again.
        with mock.patch("bot.prep.sandbox.run_job") as job:
            self.assertEqual(verify.verify_variant(spec, time.monotonic() + 60, {})["status"], "cached")
            job.assert_not_called()
        # A winner that is no longer the file on disk is not trusted.
        memory.write_text_atomic(os.path.join(d, "simulator.py"), GOOD + "# changed\n")
        self.assertFalse(verify.is_done(loaded))
        self.assertFalse(loaded.verified)
        self.assertIsNone(loaded.simulator_path)

    def test_repair_loop_fixes_the_failing_candidates(self):
        spec = self.spec()
        d = self.candidates(spec, [GOOD, BROKEN["wrong_legal"], BROKEN["never_terminal"]])
        calls = []

        def model(messages, **kw):
            calls.append((kw.get("tag"), messages[-1]["content"]))
            return fenced(GOOD)
        with mock.patch("bot.llm.complete", side_effect=model):
            out = verify.verify_variant(spec, time.monotonic() + 120, {})
        self.assertEqual((out["verified"], out["rounds"]), (True, 1))
        self.assertEqual(sorted(t for t, _ in calls), ["codegen-repair-2", "codegen-repair-3"])
        by_tag = dict(calls)
        self.assertIn('misses ["take 3"]', by_tag["codegen-repair-2"])              # the sentence reached the model
        self.assertIn("the game cannot continue", by_tag["codegen-repair-3"])
        v = memory.read_json(verify.verify_file(spec))
        self.assertEqual([c["verified"] for c in v["candidates"]], [True, True, True])
        self.assertEqual([c["repaired"] for c in v["candidates"]], [False, True, True])
        self.assertEqual(v["winner"]["file"], "sim_1.py")                          # not repaired wins
        self.assertEqual(memory.read_text(os.path.join(d, "sim_2.py")), GOOD)

    def test_worse_repair_is_rolled_back_and_rounds_are_capped(self):
        spec = self.spec()
        d = self.candidates(spec, [BROKEN["mutates"], BROKEN["mutates"]])
        calls = []

        def model(messages, **kw):
            calls.append(kw.get("tag"))
            return fenced(BROKEN["parse_raises"])                                  # worse than what it replaces
        with mock.patch("bot.llm.complete", side_effect=model):
            out = verify.verify_variant(spec, time.monotonic() + 140, {})
        self.assertEqual((out["status"], out["verified"], out["winner"]), ("failed", False, None))
        self.assertEqual(out["rounds"], verify.VERIFY_MAX_REPAIR_ROUNDS)
        self.assertEqual(len(calls), 2 * verify.VERIFY_MAX_REPAIR_ROUNDS)
        self.assertEqual(memory.read_text(os.path.join(d, "sim_1.py")), BROKEN["mutates"])    # restored
        self.assertFalse(os.path.exists(verify.simulator_file(spec)))
        self.assertFalse(spec.verified)
        self.assertIsNone(spec.simulator_path)
        v = memory.read_json(verify.verify_file(spec))
        self.assertFalse(v["verified"])
        self.assertIsNone(v["winner"])
        self.assertIn("no candidate passed", v["note"])
        self.assertTrue(memory.read_json(os.path.join(d, "index.json"))["candidates"][0]["rolled_back"])

    def test_nothing_starts_without_time_candidates_or_sample(self):
        spec = self.spec()
        with mock.patch("bot.prep.sandbox.run_job") as job, mock.patch("bot.llm.complete") as llm:
            self.assertEqual(verify.verify_variant(spec, time.monotonic() + 60, {})["status"], "no-candidates")
            self.candidates(spec, [GOOD])
            self.assertEqual(verify.verify_variant(spec, time.monotonic() - 1, {})["status"], "no-time")
            nosample = self.spec(game="pile2", sample=None)
            self.candidates(nosample, [GOOD])
            self.assertEqual(verify.verify_variant(nosample, time.monotonic() + 60, {})["status"], "no-sample")
            job.assert_not_called()
            llm.assert_not_called()
        self.assertFalse(nosample.verified)
        self.assertFalse(os.path.exists(verify.simulator_file(nosample)))
        self.assertIn("no turn sample", memory.read_json(verify.verify_file(nosample))["note"])

    def test_candidates_of_another_contract_are_stale(self):
        spec = self.spec()
        self.candidates(spec, [GOOD])
        self.assertTrue(codegen.has_ok_candidates(spec))
        with mock.patch("bot.prep.codegen.contract_version", return_value="other"):
            self.assertFalse(codegen.has_ok_candidates(spec))
            self.assertEqual(codegen.ok_paths(spec), [])


class PipelineTests(Base):

    def test_prep_verifies_and_play_is_unchanged(self):
        from tests.test_prep import answer
        calls = []
        msg = {"type": "prep", "game_id": "pileprep", "game_name": "Pile",
               "variants": [{"label": "std", "rules": RULES, "sample_turn": SAMPLE}]}

        def model(messages, **kw):
            calls.append(kw.get("tag"))
            if "TASK: classify" in messages[-1]["content"]:
                return answer(action_format="take <n>", example_action="take 1", action_regex="take [123]")
            return fenced(GOOD)
        with mock.patch("bot.llm.complete", side_effect=model):
            done = pipeline.run_prep(msg, time.monotonic() + 400)
        spec = done[0]
        self.assertTrue(spec.verified)
        self.assertTrue(os.path.exists(spec.simulator_path))
        self.assertEqual(sorted(c for c in calls if c.startswith("codegen")),
                         ["codegen-1", "codegen-2", "codegen-3"])      # verification needed no repair call
        self.assertEqual(sandbox.live_count(), 0)
        # Play did not change: the engine is a stub, L3 is not available, the model decides (L2).
        turn = dict(SAMPLE)
        d = ladder.decide_turn(spec, {}, turn, time.monotonic() + 10)
        self.assertEqual((d.level, d.action), (2, "take 3"))         # the fake LLM picks the last legal action
        # A second prep of the same game: everything from the cache, no model call, no child.
        specialist._registry.clear()
        calls.clear()
        with mock.patch("bot.llm.complete", side_effect=model), mock.patch("bot.prep.sandbox.run_job") as job:
            again = pipeline.run_prep(msg, time.monotonic() + 400)
            job.assert_not_called()
        self.assertEqual(calls, [])
        self.assertTrue(again[0].verified)

    def test_short_clock_starts_no_child(self):
        from tests.test_prep import answer
        msg = {"type": "prep", "game_id": "pileshort", "variants": [{"label": "std", "rules": RULES,
                                                                      "sample_turn": SAMPLE}]}
        with mock.patch("bot.llm.complete", return_value=answer()), \
                mock.patch("bot.prep.sandbox.run_job") as job:
            done = pipeline.run_prep(msg, time.monotonic() + 6)
            job.assert_not_called()
        self.assertFalse(done[0].verified)

    def test_deadline_kills_the_children(self):
        spec = self.spec(game="pileslow")
        self.candidates(spec, [BROKEN["endless"], BROKEN["endless"]])
        with mock.patch.object(verify, "VERIFY_JOB_BUDGET_S", 20.0):
            t0 = time.monotonic()
            pipeline.run_chains([spec], {}, t0 + 1.0, t0 + 2.0)
        self.assertLess(time.monotonic() - t0, 6.0)
        self.assertEqual(sandbox.live_count(), 0)                    # killed at the deadline
        self.assertFalse(spec.verified)
        self.assertFalse(os.path.exists(verify.simulator_file(spec)))


if __name__ == "__main__":
    unittest.main()
