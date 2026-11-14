"""WP2: warm worker per game, parallel parts, deterministic merges, watchdog for stale children.

Uses the fixtures of tests/test_engines.py (small hand-written simulators, only ever run in sandbox children)."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

from bot.engines import base, guess, search
from bot.prep import sandbox
from tests import test_engines as T

LEGAL3 = ["take 1", "take 2", "take 3"]


def cand(c):
    return [(x.action, round(x.value, 9), x.note) for x in c]


class ParallelBase(T.Base):
    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def setUp(self):
        super().setUp()
        self.env(BOT_CPUS="2", BOT_MEM_LIMIT_MB="4096")
        self.addCleanup(base.shutdown_workers)
        self.addCleanup(sandbox.set_capacity, sandbox.capacity())
        sandbox.set_capacity(2)


class WarmWorkerTests(ParallelBase):

    def test_one_child_serves_every_turn_of_a_game_and_dies_with_it(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.assertEqual(sandbox.live_count(), 0)
        for n, stones in enumerate((9, 7, 5), start=1):
            c = self.ask(eng, spec, self.turn(T.PILE_OBS % stones, LEGAL3, game_index=1, turn=n))
            self.assertTrue(c)
            self.assertEqual(sandbox.live_count(), 1)                    # warm between turns
        entry = next(iter(base._warm.values()))
        self.assertEqual(entry.worker.starts, 1)                         # the start cost was paid once
        self.assertEqual(base.shutdown_workers(), 1)                     # `end` / idle
        self.assertEqual(sandbox.live_count(), 0)
        self.assertEqual(base.warm_count(), 0)

    def test_a_new_game_closes_the_warm_child_of_the_old_one(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3, game_index=1, turn=1))
        first = next(iter(base._warm.values())).worker
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3, game_index=2, turn=1))
        self.assertFalse(first.alive())
        self.assertEqual(sandbox.live_count(), 1)

    def test_warm_off_is_a_fresh_child_per_turn_and_nothing_survives(self):
        self.env(BOT_SEARCH_WORKERS="1", BOT_WARM_WORKER="off")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.assertTrue(self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3)))
        self.assertEqual(sandbox.live_count(), 0)
        self.assertEqual(base.warm_count(), 0)

    def test_idle_watchdog_closes_a_stale_child_and_then_stops_itself(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        with mock.patch.object(base, "WARM_IDLE_MIN_S", 0.2), mock.patch.object(base, "WARM_WATCH_PERIOD_S", 0.1):
            self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3))
            self.assertEqual(sandbox.live_count(), 1)
            # clock + 5 is the idle limit for a normal clock; make the entry stale by hand
            for e in base._warm.values():
                e.idle_s = 0.2
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (sandbox.live_count() or base._warm_thread is not None):
                time.sleep(0.05)
        self.assertEqual(sandbox.live_count(), 0)
        self.assertIsNone(base._warm_thread)                             # no timer loop while idle

    def test_the_cpu_of_a_warm_child_is_reported_for_the_metrics(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        base.LAST_RUN.clear()
        self.ask(eng, spec, self.turn(T.PILE_OBS % 12, LEGAL3))
        entry = next(iter(base._warm.values()))
        self.assertGreater(entry.worker.cpu_used, 0.0)
        self.assertGreater(base.LAST_RUN.get("warm_cpu_s", 0.0), 0.0)
        if sys.platform.startswith("linux"):
            self.assertGreater(base.LAST_RUN.get("child_rss_mb", 0.0), 1.0)         # the live child, from /proc
        from bot import metrics
        snap = metrics.perf_start()
        f = metrics.perf_fields(snap, {"l3_s": 0.1}, dict(base.LAST_RUN))
        self.assertGreater(f["child_cpu_s"], 0.0)
        self.assertEqual((f["workers"], f["warm"], f["l3_s"]), (1, True, 0.1))

    def test_a_busy_warm_child_is_not_shared_a_fresh_one_answers(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3, turn=1))
        entry = next(iter(base._warm.values()))
        with entry.busy:                                                 # a late thread of an earlier turn
            c = self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3, turn=2))
        self.assertTrue(c)
        self.assertEqual(entry.worker.starts, 1)
        self.assertEqual(sandbox.live_count(), 1)                        # the fresh child is gone again

    def test_a_moving_memory_figure_does_not_rebuild_the_warm_child(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3, turn=1))
        entry = next(iter(base._warm.values()))
        for used in ("700", "900", "300"):                      # the plan's child memory moves with the memory in use
            self.env(BOT_MEM_LIMIT_MB=str(2048 + int(used)))
            self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3, turn=2))
        self.assertIs(next(iter(base._warm.values())), entry)
        self.assertEqual(entry.worker.starts, 1)

    def test_warm_child_survives_a_timeout_by_restarting(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3, turn=1))
        entry = next(iter(base._warm.values()))
        entry.worker.kill()                                              # killed from outside, as kill_all does
        c = self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3, turn=2))
        self.assertTrue(c)
        self.assertEqual(entry.worker.starts, 2)

    def test_looping_simulator_leaves_no_child_and_the_next_turn_still_works(self):
        self.env(BOT_SEARCH_WORKERS="1")
        spec = self.install(T.LOOP_AFTER_PARSE)
        eng = self.engine(search.SearchEngine)
        self.assertEqual(self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3)), [])
        self.assertEqual(sandbox.live_count(), 0)


class ParallelSearchTests(ParallelBase):

    def run_turns(self, spec, obs_list, legal, **env):
        for k, v in env.items():
            self.env(**{k: v})
        eng = self.engine(search.SearchEngine)
        out = []
        for n, obs in enumerate(obs_list, start=1):
            out.append(cand(self.ask(eng, spec, self.turn(obs, legal, game_index=5, turn=n))))
        base.shutdown_workers()
        return out

    def test_one_worker_is_identical_to_the_old_sequential_code(self):
        spec = self.install(T.PILE)
        obs = [T.PILE_OBS % n for n in (7, 8, 9, 11)]
        off = self.run_turns(spec, obs, LEGAL3, BOT_RESOURCES="off")
        one = self.run_turns(self.install(T.PILE, game="g1"), obs, LEGAL3, BOT_RESOURCES="on", BOT_SEARCH_WORKERS="1")
        one_fresh = self.run_turns(self.install(T.PILE, game="g2"), obs, LEGAL3, BOT_SEARCH_WORKERS="1",
                                   BOT_WARM_WORKER="off")
        strip = lambda rows: [[(a, v) for a, v, _ in r] for r in rows]       # node counts are in the notes
        self.assertEqual(strip(off), strip(one))
        self.assertEqual(strip(off), strip(one_fresh))
        self.assertEqual(sandbox.live_count(), 0)

    def test_two_workers_return_the_same_best_move_and_value_as_one(self):
        """With one worker a proven win stops the search; with two, the part that proves the win stops the other,
        so the merged list holds that part's moves (the win is still first, with the same value)."""
        obs = [T.PILE_OBS % n for n in (7, 9, 10, 12)]
        one = self.run_turns(self.install(T.PILE, game="a"), obs, LEGAL3, BOT_SEARCH_WORKERS="1")
        two = self.run_turns(self.install(T.PILE, game="b"), obs, LEGAL3, BOT_SEARCH_WORKERS="2")
        for r1, r2 in zip(one, two):
            self.assertEqual(r1[0][:2], r2[0][:2])                           # same best move, same value
            self.assertTrue({a for a, _, _ in r2} <= {a for a, _, _ in r1})

    def test_without_a_win_both_parts_run_and_the_values_are_exact(self):
        legal = LEGAL3
        one = self.run_turns(self.install(T.PILE, game="c"), [T.PILE_OBS % 8], legal, BOT_SEARCH_WORKERS="1")
        two = self.run_turns(self.install(T.PILE, game="d"), [T.PILE_OBS % 8], legal, BOT_SEARCH_WORKERS="2")
        self.assertEqual({a: v for a, v, _ in one[0]}, {a: v for a, v, _ in two[0]})     # 8 stones: every move loses
        self.assertEqual(len(two[0]), 3)

    def test_a_proven_win_in_one_part_stops_the_other_at_once(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        win = {"ok": True, "live": True, "algo": "alphabeta", "solved": False, "depth": 3, "nodes": 5, "playouts": 0,
               "best_value": 1.0, "root_key": "k", "branching": 3, "i": 0,
               "actions": [{"action": "take 3", "value": 1.0, "bound": None, "i": 2},
                           {"action": "take 1", "value": 0.0, "bound": "<=", "i": 0}]}

        def job(self_, job_file, payload, timeout):
            if payload.get("part") == 0:
                return win
            end = time.monotonic() + 8                           # part 1 would search for the whole slice
            while time.monotonic() < end:
                if self_._stopped:
                    raise sandbox.SandboxError("stopped: %s" % self_._stopped, "closed")
                time.sleep(0.02)
            return dict(win, actions=[])
        t = time.monotonic()
        with mock.patch.object(sandbox.Worker, "job", job):
            c = self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3))
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertEqual(c[0].action, "take 3")
        self.assertEqual(c[0].value, 1.0)

    def test_decisive_means_a_proven_exact_win_and_nothing_else(self):
        eng = search.SearchEngine()
        row = lambda v, b=None: {"action": "a", "value": v, "bound": b}
        self.assertTrue(eng.decisive({"algo": "alphabeta", "actions": [row(1.0)]}))
        self.assertFalse(eng.decisive({"algo": "alphabeta", "actions": [row(1.0, "<=")]}))
        self.assertFalse(eng.decisive({"algo": "alphabeta", "actions": [row(0.5)]}))
        self.assertFalse(eng.decisive({"algo": "mcts", "actions": [row(1.0)]}))
        self.assertFalse(eng.decisive({"algo": "alphabeta", "actions": []}))
        self.assertFalse(guess.GuessEngine().decisive({"algo": "split", "actions": [row(1.0)]}))

    def test_two_workers_play_tic_tac_toe_from_the_opening_to_the_same_values(self):
        legal = ["mark %d" % i for i in range(9)]
        obs = [T.ttt_obs(list("........."), 0), T.ttt_obs(list("....X...."), 1), T.ttt_obs(list("X...O...."), 0)]
        legal_by = [legal, [a for a in legal if a != "mark 4"], [a for a in legal if a not in ("mark 0", "mark 4")]]
        for spec_name, workers in (("a", "1"), ("b", "2")):
            self.env(BOT_SEARCH_WORKERS=workers)
            spec = self.install(T.TTT, game="ttt" + spec_name)
            eng = self.engine(search.SearchEngine)
            rows = [cand(self.ask(eng, spec, self.turn(o, lg, game_index=3, turn=i + 1)))
                    for i, (o, lg) in enumerate(zip(obs, legal_by))]
            base.shutdown_workers()
            if workers == "1":
                ref = [[(a, v) for a, v, _ in r] for r in rows]
            else:
                got = [[(a, v) for a, v, _ in r] for r in rows]
                for r, g in zip(ref, got):
                    self.assertEqual(r[0][1], g[0][1])                         # same best value
                    self.assertEqual(sorted(r), sorted(g))                     # same exact value for every move

    def test_two_workers_use_two_children_and_both_are_warm(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 8, LEGAL3))      # 8 stones: no winning move, so no part stops the other
        self.assertEqual(sandbox.live_count(), 2)
        self.assertEqual(base.warm_count(), 2)
        self.assertEqual(base.LAST_RUN["workers"], 2)
        base.shutdown_workers()
        self.assertEqual(sandbox.live_count(), 0)

    def test_workers_never_exceed_the_sandbox_capacity(self):
        self.env(BOT_SEARCH_WORKERS="4", BOT_CPUS="8")
        sandbox.set_capacity(2)
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, LEGAL3))
        self.assertEqual(base.LAST_RUN["workers"], 2)
        self.assertLessEqual(sandbox.live_count(), 2)

    def test_a_part_that_fails_degrades_to_the_part_that_answered(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        real = sandbox.Worker.job

        def job(self_, job_file, payload, timeout):
            if payload.get("part") == 1:
                self_.kill()
                raise sandbox.SandboxError("timeout: the loop", "timeout")
            return real(self_, job_file, payload, timeout)
        with mock.patch.object(sandbox.Worker, "job", job):
            c = self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3))
        self.assertTrue(c)                                               # part 0's moves (take 1, take 3)
        self.assertIn("take 3", [x.action for x in c])
        self.assertLessEqual(sandbox.live_count(), 1)
        base.shutdown_workers()
        self.assertEqual(sandbox.live_count(), 0)

    def test_all_parts_failing_is_an_empty_list_not_an_exception(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        with mock.patch.object(sandbox.Worker, "job", side_effect=sandbox.SandboxError("dead", "crash")):
            self.assertEqual(self.ask(eng, spec, self.turn(T.PILE_OBS % 7, LEGAL3)), [])

    def test_the_partial_result_is_never_cached_as_a_solved_root(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.PILE)
        eng = self.engine(search.SearchEngine)
        real = sandbox.Worker.job

        def job(self_, job_file, payload, timeout):
            if payload.get("part") == 1:
                raise sandbox.SandboxError("dead", "crash")
            return real(self_, job_file, payload, timeout)
        with mock.patch.object(sandbox.Worker, "job", job):
            self.ask(eng, spec, self.turn(T.PILE_OBS % 8, LEGAL3))
        self.assertEqual(search.load_cache(spec), {})                    # one move of three is not a solved root

    def test_chance_game_two_workers_still_rank_moves(self):
        self.env(BOT_SEARCH_WORKERS="2")
        spec = self.install(T.GAMBLE, unknown=True, both=False)
        eng = self.engine(search.SearchEngine)
        c = self.ask(eng, spec, self.turn(T.GAMBLE_OBS, ["sure", "gamble"]))
        self.assertTrue(c)
        self.assertEqual({x.action for x in c}, {"sure", "gamble"})
        self.assertLessEqual(max(x.value for x in c), 1.0)


class MergeTests(unittest.TestCase):

    def ab(self, acts, solved=True, depth=5, nodes=10):
        return {"ok": True, "algo": "alphabeta", "solved": solved, "depth": depth, "nodes": nodes, "playouts": 0,
                "actions": [{"action": a, "value": v, "bound": b} for a, v, b in acts], "best_value": 0.0,
                "root_key": "k", "branching": sum(len(acts) for _ in [0])}

    def test_alphabeta_merge_is_a_function_of_the_parts_in_order(self):
        p0 = self.ab([("a", 0.0, None), ("c", -1.0, None)])
        p1 = self.ab([("b", 1.0, None), ("d", 0.0, "<=")], solved=False, depth=3, nodes=7)
        m = search.merge_results([p0, p1])
        self.assertEqual([x["action"] for x in m["actions"]], ["b", "a", "d", "c"])
        self.assertFalse(m["solved"])
        self.assertEqual((m["depth"], m["nodes"], m["best_value"]), (3, 17, 1.0))
        self.assertEqual(m, search.merge_results([p0, p1]))

    def mc(self, rows, algo="mcts"):
        acts = [{"action": a, "value": mean, "bound": None, "visits": n, "mean": mean, "se": se, "tied": False}
                if n else {"action": a, "value": -1.0, "bound": None, "visits": 0, "lost_in": lost}
                for a, n, mean, se, lost in rows]
        return {"ok": True, "algo": algo, "solved": False, "depth": 2, "nodes": 3, "playouts": 100, "actions": acts,
                "best_value": 0.5, "root_key": "k", "branching": len(rows)}

    def test_mcts_merge_adds_visits_pools_means_and_keeps_proven_losses_lost(self):
        p0 = self.mc([("a", 80, 0.5, 0.05, None), ("b", 20, 0.1, 0.1, None), ("x", 0, 0, 0, 3)], "alphabeta+mcts")
        p1 = self.mc([("a", 60, 0.3, 0.06, None), ("b", 40, 0.1, 0.08, None), ("x", 5, -1.0, 0.2, None)],
                     "alphabeta+mcts")
        m = search.merge_results([p0, p1])
        by = {x["action"]: x for x in m["actions"]}
        self.assertEqual(by["a"]["visits"], 140)
        self.assertAlmostEqual(by["a"]["mean"], (80 * 0.5 + 60 * 0.3) / 140)
        self.assertEqual(by["x"]["value"], -1.0)
        self.assertEqual(by["x"]["visits"], 0)
        self.assertEqual(m["algo"], "alphabeta+mcts")
        self.assertEqual(m["playouts"], 200)
        self.assertEqual(m["actions"][0]["action"], "a")

    def test_a_proven_win_in_one_part_beats_the_estimates_of_the_other(self):
        win = self.ab([("w", 1.0, None), ("z", -1.0, None)], solved=False)
        est = self.mc([("a", 50, 0.2, 0.1, None), ("w", 20, 0.0, 0.2, None)])
        m = search.merge_results([est, win])
        self.assertEqual(m["actions"][0]["action"], "w")
        self.assertEqual(m["actions"][0]["value"], 1.0)

    def test_empty_and_single_parts(self):
        self.assertFalse(search.merge_results([{"ok": True, "actions": []}])["ok"])
        one = self.ab([("a", 0.0, None)])
        self.assertIs(search.merge_results([one, {"ok": True, "actions": []}]), one)

    def test_guess_merge_pools_ranks_by_cost_and_revalues_against_the_pooled_best(self):
        eng = guess.GuessEngine()
        row = lambda a, c, k: {"action": a, "value": 0.0, "cost": c, "expected_left": 1.0, "p_hit": 0.1, "classes": k}
        p0 = {"ok": True, "algo": "split", "scored": 3, "of": 6, "actions": [row("g 1", 2.0, 3), row("g 3", 3.0, 3)]}
        p1 = {"ok": True, "algo": "split", "scored": 3, "of": 6, "actions": [row("g 2", 1.0, 4), row("g 3", 3.0, 3)]}
        m = eng.merge([p0, p1], {})
        self.assertEqual([a["action"] for a in m["actions"]], ["g 2", "g 1", "g 3"])
        self.assertEqual([a["value"] for a in m["actions"]], [1.0, 0.5, 0.0])
        self.assertEqual(m["scored"], 6)

    def test_two_workers_find_the_same_best_guess(self):
        pass


class GuessParallel(ParallelBase):

    def test_two_workers_give_the_same_first_guess_as_one(self):
        rows = {}
        for workers in ("1", "2"):
            self.env(BOT_SEARCH_WORKERS=workers)
            spec = self.install(T.GUESS, game="gu" + workers, family="guessing")
            eng = self.engine(guess.GuessEngine)
            c = self.ask(eng, spec, self.turn(T.guess_obs(1, 100, []), None, game_index=1, turn=1))
            base.shutdown_workers()
            rows[workers] = [(x.action, round(x.value, 6)) for x in c]
        self.assertTrue(rows["1"])
        self.assertEqual(rows["1"][0][1], rows["2"][0][1])
        self.assertEqual(rows["1"][0][0], rows["2"][0][0])


if __name__ == "__main__":
    unittest.main()
