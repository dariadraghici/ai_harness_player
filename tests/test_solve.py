"""WP4: pre-solving the positions near the prep sample into solved.json (bot/prep/solve.py, solve_job.py)."""

import json
import os
import time
import unittest
from unittest import mock

from bot import resources
from bot.engines import base, search
from bot.prep import sandbox, solve
from tests import test_engines as T

TTT_SAMPLE = {"observation": T.ttt_obs(list("X...O...."), 0),
              "legal_actions": ["mark %d" % i for i in range(9) if i not in (0, 4)]}


class SolveTests(T.Base):

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"BOT_CPUS": "2", "BOT_MEM_LIMIT_MB": "4096"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(sandbox.set_capacity, sandbox.capacity())
        sandbox.set_capacity(2)
        resources.reset()
        self.addCleanup(resources.reset)

    def spec_with_sample(self, code=T.TTT, sample=TTT_SAMPLE, **kw):
        spec = self.install(code, **kw)
        spec.sample_turn = dict(sample)
        return spec

    def test_roots_near_the_sample_are_solved_stored_and_served_from_the_cache(self):
        spec = self.spec_with_sample()
        info = solve.solve_variant(spec, time.monotonic() + 60)
        self.assertGreater(info["solved"], 5, info)
        self.assertEqual(sandbox.live_count(), 0)
        cache = search.load_cache(spec)
        self.assertEqual(len(cache), info["stored"])
        # the sample position itself is one of them, with exact values for every legal move
        key = next(k for k, v in cache.items() if set(v) == set(TTT_SAMPLE["legal_actions"]))
        self.assertTrue(all(-1.0 <= x <= 1.0 for x in cache[key].values()))
        # the engine answers that position from the cache: "solved earlier", same values as a fresh search
        eng = self.engine(search.SearchEngine)
        c = self.ask(eng, spec, self.turn(TTT_SAMPLE["observation"], TTT_SAMPLE["legal_actions"]))
        self.assertIn("cache", c[0].note)
        base.shutdown_workers()
        self.assertEqual({x.action: x.value for x in c}, cache[key])

    def test_stored_values_equal_what_the_engine_computes_without_a_cache(self):
        spec = self.spec_with_sample()
        solve.solve_variant(spec, time.monotonic() + 60)
        cache = search.load_cache(spec)
        fresh = self.install(T.TTT, game="other")                  # no cache for this variant
        eng = self.engine(search.SearchEngine)
        checked = 0
        for key, values in list(cache.items())[:12]:
            state = json.loads(key)
            board = state.get("board") or state.get("cells")
            if not isinstance(board, list):
                continue
            legal = sorted(values)
            c = self.ask(eng, fresh, self.turn(T.ttt_obs(["." if x in (".", None, " ") else x for x in board],
                                                          state["me"]), legal))
            if c and c[0].note.startswith("solved"):
                self.assertEqual({x.action: round(x.value, 6) for x in c}, {a: round(v, 6) for a, v in values.items()})
                checked += 1
        base.shutdown_workers()
        self.assertGreaterEqual(checked, 1)

    def test_a_changed_simulator_invalidates_everything_stored(self):
        spec = self.spec_with_sample()
        solve.solve_variant(spec, time.monotonic() + 60)
        self.assertTrue(search.load_cache(spec))
        from bot import memory
        from bot.prep import verify
        memory.write_text_atomic(verify.simulator_file(spec), T.TTT + "\n# repaired\n")
        d = memory.read_json(verify.verify_file(spec))
        d["winner"]["sha256"] = T.sha(T.TTT + "\n# repaired\n")
        memory.write_json_atomic(verify.verify_file(spec), d)
        self.assertEqual(search.load_cache(spec), {})

    def test_the_file_holds_only_state_keys_and_numbers_no_prose(self):
        spec = self.spec_with_sample()
        solve.solve_variant(spec, time.monotonic() + 60)
        with open(search.cache_path(spec)) as f:
            data = json.load(f)
        self.assertEqual(set(data), {"sim", "roots", "presolved"})
        for key, values in data["roots"]:
            json.loads(key)                                       # a canonical state, not a sentence
            self.assertTrue(all(isinstance(v, float) for v in values.values()))

    def test_a_finished_pass_is_not_repeated_and_the_marker_survives_the_match(self):
        spec = self.spec_with_sample()
        solve.solve_variant(spec, time.monotonic() + 60)
        self.assertTrue(solve.is_done(spec))
        with mock.patch("bot.prep.sandbox.run_job") as job:
            self.assertEqual(solve.solve_variant(spec, time.monotonic() + 60)["solved"], 0)
            job.assert_not_called()
        search.save_root(spec, "some key", {"mark 1": 0.0})            # the engine saves a root during a match
        self.assertTrue(solve.is_done(spec))
        spec.sample_turn = dict(TTT_SAMPLE, observation=T.ttt_obs(list("XO..X...."), 1))
        self.assertFalse(solve.is_done(spec))                          # another sample: a new pass

    def test_both_seats_are_covered(self):
        spec = self.spec_with_sample()
        solve.solve_variant(spec, time.monotonic() + 60)
        mes = set(json.loads(k)["me"] for k in search.load_cache(spec))
        self.assertEqual(mes, {0, 1})

    def test_one_job_and_two_jobs_find_the_same_roots(self):
        one = self.spec_with_sample(game="one")
        two = self.spec_with_sample(game="two")
        with mock.patch.dict(os.environ, {"BOT_MAX_CHILDREN": "2"}):
            sandbox.set_capacity(2)
            solve.solve_variant(one, time.monotonic() + 60)
        with mock.patch.dict(os.environ, {"BOT_MAX_CHILDREN": "4", "BOT_CPUS": "4"}):
            sandbox.set_capacity(4)
            solve.solve_variant(two, time.monotonic() + 60)
        self.assertEqual(search.load_cache(one), search.load_cache(two))

    def test_not_eligible_is_a_noop(self):
        for kw, sample in (({"family": "guessing"}, TTT_SAMPLE), ({"verified": False}, TTT_SAMPLE),
                           ({}, {"observation": ""}), ({"unknown": True}, TTT_SAMPLE)):
            spec = self.spec_with_sample(sample=sample, **kw)
            info = solve.solve_variant(spec, time.monotonic() + 20)
            self.assertEqual(info["stored"], 0, kw)
            self.assertEqual(search.load_cache(spec), {})

    def test_off_switch_skips_the_pass(self):
        spec = self.spec_with_sample()
        with mock.patch.dict(os.environ, {"BOT_RESOURCES": "off"}):
            self.assertEqual(solve.solve_all([spec], time.monotonic() + 200), {})
        self.assertEqual(search.load_cache(spec), {})

    def test_deadline_is_respected_and_no_child_is_left(self):
        spec = self.spec_with_sample(code=T.SLOW)                  # 10 ms per move: far too slow to finish
        spec.sample_turn = {"observation": T.PILE_OBS % 40, "legal_actions": ["take 1", "take 2", "take 3"]}
        t = time.monotonic()
        solve.solve_variant(spec, t + 4.0)
        self.assertLess(time.monotonic() - t, 4.0 + 4.0)
        self.assertEqual(sandbox.live_count(), 0)

    def test_a_game_that_cannot_be_solved_gives_up_early(self):
        spec = self.spec_with_sample(code=T.SLOW)                  # 10 ms per move: the big pile is never solved
        spec.sample_turn = {"observation": T.PILE_OBS % 90, "legal_actions": ["take 1", "take 2", "take 3"]}
        t = time.monotonic()
        info = solve.solve_variant(spec, t + 40.0)
        self.assertLess(time.monotonic() - t, 25.0)
        self.assertEqual(info["solved"], 0)
        self.assertLessEqual(info["tried"], 2 * 2 * 13)             # a few roots per job, then the job gives up
        self.assertEqual(sandbox.live_count(), 0)

    def test_save_roots_keeps_old_entries_and_replaces_duplicates(self):
        spec = self.spec_with_sample()
        solve.save_roots(spec, [("a", {"x": 1.0}), ("b", {"x": 0.0})], 800)
        n = solve.save_roots(spec, [("b", {"x": -1.0}), ("c", {"x": 0.0})], 800)
        self.assertEqual(n, 3)
        self.assertEqual(search.load_cache(spec)["b"], {"x": -1.0})

    def test_save_roots_limit_keeps_the_newest(self):
        spec = self.spec_with_sample()
        solve.save_roots(spec, [("k%d" % i, {"x": 0.0}) for i in range(10)], 5)
        self.assertLessEqual(len(search.load_cache(spec)), max(5, search.CACHE_MAX_ROOTS))


if __name__ == "__main__":
    unittest.main()
