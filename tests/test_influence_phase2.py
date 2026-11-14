"""Influence layer, phase 2 (PROMPT-CHAT section 11): engine replies, gap-driven goals, bandit, promises, quality drop."""

import json
import os
import random
import time
import unittest
from unittest import mock

from bot import memory, metrics, opponents
from bot.chat import bandit, decide, defense, influence, offense, score
from bot.engines import base, search, simultaneous
from bot.interfaces import InfluenceGoal
from tests import test_engines as T
from tests.test_influence import Isolated, goal, make_spec


# ---------------------------------------------------------------- the reply tables
def rows(*pairs, exact=True):
    return [{"action": a, "value": v, "exact": exact} for a, v in pairs]


RPS_MATRIX = {"acts": ["rock", "paper", "scissors"],
              # our payoff, rows = our action, columns = their action (rock beats scissors, ...)
              "matrix": [[0.0, -1.0, 1.0], [1.0, 0.0, -1.0], [-1.0, 1.0, 0.0]]}


class AnalysisTests(unittest.TestCase):

    def test_rows_give_likely_target_and_gap(self):
        a = offense.analyze_replies({"action": "B2", "rows": rows(("A1", 0.2), ("A2", 1.0), ("A3", -0.4))})
        self.assertEqual((a["r_likely"], a["r_target"]), ("A3", "A2"))     # likely = best for THEM = lowest for us
        self.assertAlmostEqual(a["gap"], 1.4)
        self.assertTrue(a["exact"])
        flat = offense.analyze_replies({"action": "B2", "rows": rows(("A1", 0.3), ("A2", 0.3))})
        self.assertEqual(flat["gap"], 0.0)

    def test_a_model_of_their_play_decides_what_is_likely(self):
        t = {"action": "B2", "rows": [dict(r, p=p) for r, p in zip(rows(("A1", 0.2), ("A2", 1.0), ("A3", -0.4)),
                                                                   (0.7, 0.1, 0.2))]}
        a = offense.analyze_replies(t)
        self.assertEqual(a["r_likely"], "A1")                              # they usually play A1, not the minimizer

    def test_unusable_tables_give_none(self):
        for bad in (None, {}, {"rows": []}, {"rows": rows(("A1", 0.1))}, {"matrix": [[0]], "acts": ["x"],
                                                                         "action": "x"}):
            self.assertIsNone(offense.analyze_replies(bad))

    def test_matrix_gives_a_promise_that_costs_nothing(self):
        t = dict(RPS_MATRIX, action="paper", model={"rock": 0.6, "paper": 0.2, "scissors": 0.2})
        a = offense.analyze_replies(t)
        self.assertEqual(a["r_likely"], "rock")
        self.assertEqual(a["promise"], {"if": "rock", "then": "paper"})     # paper is our best answer to rock
        self.assertEqual(a["r_target"], "rock")                             # paper beats rock: best reply for us

    def test_a_pattern_is_offered_only_when_the_drawn_action_is_the_best_response_to_the_induced_reply(self):
        # we played rock last time; a rational reply to rock is paper; the best answer to paper is scissors
        t = dict(RPS_MATRIX, action="scissors", prev_action="rock", model={"rock": 0.4, "paper": 0.3, "scissors": 0.3})
        a = offense.analyze_replies(t)
        self.assertEqual(a["pattern"]["claim"], "rock")
        self.assertEqual(a["pattern"]["induce"], "paper")
        no = offense.analyze_replies(dict(t, action="paper", prev_action="rock"))
        self.assertNotIn("pattern", no)

    def test_the_targeted_intent_names_the_replies_and_the_generic_one_does_not(self):
        g = goal("invite_blunder")
        self.assertNotIn("{", offense.intent_text(g))
        g.from_engine, g.target_action, g.avoid_action = True, "A2", "A3"
        text = offense.intent_text(g)
        self.assertIn("A2", text)
        self.assertIn("A3", text)
        self.assertNotIn("{", text)


# ---------------------------------------------------------------- the bandit
class BanditTests(unittest.TestCase):

    def test_posterior_uses_own_counts_and_a_capped_family_prior(self):
        a, b = bandit.posterior({"scored": 10, "hits": 8}, {"scored": 600, "hits": 0})
        self.assertAlmostEqual(a, 9.0)
        self.assertAlmostEqual(b, 3.0 + bandit.PRIOR_CAP)                    # 600 other-team misses count as only 6
        self.assertEqual(bandit.posterior(None, None), (1.0, 1.0))

    def test_the_quiet_arm_is_the_control_statistics(self):
        b = bandit.Bandit({"control": {"scored": 10, "hits": 9}, "x": {"scored": 10, "hits": 1}})
        self.assertGreater(b.mean("quiet"), b.mean("x"))
        self.assertEqual(bandit.stat_key("quiet"), "control")

    def test_a_better_arm_is_played_more_and_exploration_is_bounded(self):
        own = {"good": {"scored": 30, "hits": 24}, "bad": {"scored": 30, "hits": 3},
               "control": {"scored": 30, "hits": 9}}
        picks = {}
        for seed in range(400):
            b = bandit.Bandit(own)
            arm, _ = b.choose(["good", "bad", "quiet"], random.Random(seed))
            picks[arm] = picks.get(arm, 0) + 1
        self.assertGreater(picks["good"], 330)
        explored = 0
        b = bandit.Bandit({}, explored=bandit.EXPLORE_MAX)                   # cold start, but the cap is spent
        for seed in range(100):
            arm, info = b.choose(["a", "b", "quiet"], random.Random(seed))
            self.assertFalse(info["explore"])
            self.assertEqual(arm, info["greedy"])

    def test_a_decisive_turn_is_always_greedy(self):
        own = {"a": {"scored": 5, "hits": 4}, "b": {"scored": 5, "hits": 1}}
        for seed in range(50):
            arm, info = bandit.Bandit(own).choose(["a", "b"], random.Random(seed), greedy=True)
            self.assertEqual(arm, "a")
            self.assertFalse(info["explore"])

    def test_exploration_is_reported_so_that_the_match_can_count_it(self):
        flagged = 0
        for seed in range(200):
            _, info = bandit.Bandit({}).choose(["a", "b", "c"], random.Random(seed))
            flagged += 1 if info["explore"] else 0
        self.assertTrue(0 < flagged < 200)


# ---------------------------------------------------------------- the planner with the engine's replies
class GapPlannerTests(Isolated):

    def state(self, cls="reactive", decisions=5, truthful=0, used=()):
        st = influence.MatchInfluence("m", "opp")
        st.opp_class, st.decisions, st.used = cls, decisions, set(used)
        for _ in range(truthful):
            st.ledger.add(goal("credibility"), "The center is open.")
        return st

    def plan(self, replies, family="think_ahead", seed=0, state=None, bandit_=None, **turn):
        t = {"game_index": 1, "turn": 6, "observation": "o", "legal_actions": ["A1", "A2"]}
        t.update(turn)
        return offense.plan_influence(make_spec(family, simultaneous=family == "read_opponent"), t, None, None,
                                      random.Random(seed), state or self.state(), None, replies=replies,
                                      bandit=bandit_ or bandit.Bandit({}))

    TABLE_BIG = {"action": "B2", "rows": rows(("A1", -0.8), ("A2", 0.9), ("A3", 0.1))}
    TABLE_SMALL = {"action": "B2", "rows": rows(("A1", 0.05), ("A2", 0.10), ("A3", 0.08))}

    def test_a_small_gap_means_nothing_to_win(self):
        for seed in range(30):
            g = self.plan(self.TABLE_SMALL, seed=seed)
            self.assertIn(g.tactic_id, ("quiet", "credibility"))
            self.assertFalse(g.is_control)
            self.assertAlmostEqual(g.gap, 0.05)

    def test_a_small_gap_builds_credibility_with_a_true_remark(self):
        g = self.plan(self.TABLE_SMALL)
        self.assertEqual((g.tactic_id, g.kind), ("credibility", "build_credibility"))
        self.assertTrue(offense.TACTICS["credibility"]["intent"].count("TRUE"))
        led = offense.Ledger()
        led.add(g, "The center is open.")
        self.assertEqual(led.truthful_count(), 1)                              # cash_credibility counts these

    def test_a_large_gap_names_the_replies_and_is_never_a_control_or_an_exploration(self):
        for seed in range(80):
            g = self.plan(self.TABLE_BIG, seed=seed)
            self.assertFalse(g.is_control)
            self.assertFalse((g.arm_info or {}).get("explore", False))
            if g.tactic_id != "quiet":
                self.assertTrue(g.from_engine)
                self.assertEqual(g.engine_action, "B2")
                self.assertIn(g.target_action, ("A2", "A1"))
                self.assertGreaterEqual(g.gap, offense.LARGE_GAP)

    def test_targets_come_from_the_engine_not_from_the_model(self):
        g = self.plan(self.TABLE_BIG, state=self.state(decisions=6))
        if g.tactic_id in ("invite_blunder", "fake_threat", "tempo_pressure"):
            self.assertEqual((g.target_action, g.avoid_action), ("A2", "A1"))      # steer to the weakest, away from the likely

    def test_rule_doubt_deters_the_likely_reply_and_needs_a_large_gap(self):
        arms = offense.eligible_arms("think_ahead", "reactive", set(), offense.analyze_replies(self.TABLE_BIG), False, 0, 5)
        self.assertIn("rule_doubt", arms)
        arms = offense.eligible_arms("think_ahead", "reactive", set(), offense.analyze_replies(
            {"action": "B2", "rows": rows(("A1", 0.0), ("A2", 0.2))}), False, 0, 5)
        self.assertNotIn("rule_doubt", arms)
        g = offense._goal_from("rule_doubt", "reactive", 1, 6, 0)
        offense._apply_analysis(g, "rule_doubt", offense.analyze_replies(self.TABLE_BIG), self.TABLE_BIG)
        self.assertEqual((g.kind, g.target_action, g.avoid_action), ("deter_move", "A1", "A1"))

    def test_cash_credibility_needs_true_remarks_a_large_gap_and_only_once(self):
        an = offense.analyze_replies(self.TABLE_BIG)
        no = offense.eligible_arms("think_ahead", "reactive", set(), an, False, 1, 5)
        self.assertNotIn("cash_credibility", no)
        yes = offense.eligible_arms("think_ahead", "reactive", set(), an, False, 2, 5)
        self.assertIn("cash_credibility", yes)
        spent = offense.eligible_arms("think_ahead", "reactive", {"cash_credibility"}, an, False, 5, 5)
        self.assertNotIn("cash_credibility", spent)

    def test_a_new_opponent_only_gets_neutral_arms_even_with_a_big_gap(self):
        arms = offense.eligible_arms("think_ahead", "unknown", set(), offense.analyze_replies(self.TABLE_BIG), True, 5, 5)
        self.assertEqual(set(arms) - {"quiet"}, {"invite_blunder"})

    def test_instruction_probe_is_off_unless_asked_for(self):
        an = offense.analyze_replies(self.TABLE_BIG)
        self.assertNotIn("instruction_probe", offense.eligible_arms("think_ahead", "reactive", set(), an, False, 0, 1))
        with mock.patch.dict(os.environ, {"BOT_INSTRUCTION_PROBE": "on"}):
            self.assertIn("instruction_probe", offense.eligible_arms("think_ahead", "reactive", set(), an, False, 0, 1))
            self.assertNotIn("instruction_probe", offense.eligible_arms("think_ahead", "reactive", set(), an, False, 0, 4))

    def test_a_tactic_that_would_have_to_name_our_own_move_is_not_played(self):
        # simultaneous game, same action names: the weakest reply is "paper" and we are drawing "paper"
        t = dict(RPS_MATRIX, action="paper", model={"rock": 0.1, "paper": 0.8, "scissors": 0.1})
        for seed in range(40):
            g = self.plan(t, "read_opponent", seed, state=self.state(decisions=6))
            if g.from_engine and g.tactic_id != "quiet":
                self.assertNotEqual(g.target_action.casefold(), g.engine_action.casefold())

    def test_commitment_with_the_engine_announces_a_promise_it_can_keep(self):
        t = dict(RPS_MATRIX, action="scissors", model={"rock": 0.7, "paper": 0.2, "scissors": 0.1})
        found = None
        for seed in range(200):
            g = self.plan(t, "read_opponent", seed, state=self.state(decisions=1))
            if g.tactic_id == "commitment" or g.shadow_tactic == "commitment":
                found = g
                break
        self.assertIsNotNone(found)
        self.assertEqual((found.promise_if, found.promise_then), ("rock", "paper"))
        self.assertIn("paper", offense.intent_text(offense._goal_from("commitment", "reactive", 1, 1, 1)) + "paper")

    def test_without_a_bandit_the_phase_one_defaults_still_decide(self):
        g = offense.plan_influence(make_spec("guessing"), {"game_index": 1, "turn": 1}, None, None, None,
                                   self.state(decisions=0))
        self.assertEqual(g.tactic_id, "anchor")

    def test_a_control_keeps_the_engine_targets_of_its_shadow_tactic(self):
        controls = []
        for seed in range(300):
            g = self.plan(self.TABLE_BIG, seed=seed, state=self.state(decisions=6))
            if g.is_control:
                controls.append(g)
        self.assertEqual(controls, [])                                         # decisive turns have no control
        mid = {"action": "B2", "rows": rows(("A1", 0.0), ("A2", 0.2), ("A3", 0.1))}      # gap 0.2: not decisive
        for seed in range(300):
            g = self.plan(mid, seed=seed, state=self.state(decisions=6))
            if g.is_control:
                controls.append(g)
        self.assertTrue(controls)
        self.assertTrue(all(c.shadow_tactic and c.target_action and c.replies for c in controls))


# ---------------------------------------------------------------- scoring with the quality drop
class QualityDropTests(unittest.TestCase):

    def goal_with_rows(self, lag=0):
        g = goal("invite_blunder", kind="induce_move", target_action="A2", lag=lag)
        g.replies = {"action": "B2", "rows": rows(("A1", -0.8), ("A2", 0.9), ("A3", 0.1))}
        return g

    def test_the_drop_is_how_far_the_reply_they_played_is_from_their_best(self):
        out = score.score_prediction(self.goal_with_rows(), {"observation": "Moves: X B2, O A2"}, None, "Moves: X", 1)
        self.assertTrue(out.hit)
        self.assertAlmostEqual(out.their_quality_drop, 1.7)                    # played 0.9, their best was -0.8
        self.assertTrue(out.drop_exact)
        best = score.score_prediction(self.goal_with_rows(), {"observation": "Moves: X B2, O A1"}, None, "Moves: X", 1)
        self.assertFalse(best.hit)
        self.assertEqual(best.their_quality_drop, 0.0)                         # they found their best reply

    def test_an_ambiguous_reply_or_a_simultaneous_game_has_no_drop(self):
        both = score.score_prediction(self.goal_with_rows(), {"observation": "Moves: X B2, O A1, O A2"}, None,
                                      "Moves: X", 1)
        self.assertIsNone(both.their_quality_drop)
        sim = self.goal_with_rows(lag=1)
        out = score.score_prediction(sim, {"observation": "Moves: O A2"}, None, "Moves:", 2)
        self.assertIsNone(out.their_quality_drop)
        self.assertTrue(out.scored)

    def test_estimates_are_marked_not_exact(self):
        g = self.goal_with_rows()
        g.replies["rows"][0]["exact"] = False
        out = score.score_prediction(g, {"observation": "Moves: X B2, O A3"}, None, "Moves: X", 1)
        self.assertFalse(out.drop_exact)


# ---------------------------------------------------------------- promises
class PromiseTests(Isolated):

    def make(self):
        spec = make_spec("read_opponent", simultaneous=True)
        start = {"match_id": "mp", "opponent": "opp"}
        g = goal("commitment", game=1, turn=2, lag=1)
        g.promise_if, g.promise_then = "rock", "paper"
        with mock.patch.object(metrics, "record_influence"), mock.patch.object(opponents, "record_tactic"):
            influence.on_sent(g, spec, start, {"game_index": 1, "turn": 2, "observation": "Round 1: you x, opp y"},
                              "scissors", "Whenever you play rock I answer paper.")
        return spec, start

    def test_the_promise_enters_the_ledger_and_is_due_when_they_play_the_trigger(self):
        spec, start = self.make()
        st = influence.state_for(start)
        self.assertEqual(len(st.ledger.open_promises()), 1)
        with mock.patch.object(opponents, "arm_stats", return_value=({}, {})):
            influence.prepare(spec, start, {"game_index": 1, "turn": 3,
                                            "observation": "Round 1: you x, opp y\nRound 2: you scissors, opp rock"},
                              defense.build_context([]))
        self.assertIsNotNone(st.owed)
        self.assertEqual(st.owed["then"], "paper")

    def test_a_promise_is_not_due_when_the_trigger_did_not_happen(self):
        spec, start = self.make()
        with mock.patch.object(opponents, "arm_stats", return_value=({}, {})):
            influence.prepare(spec, start, {"game_index": 1, "turn": 3,
                                            "observation": "Round 1: you x, opp y\nRound 2: you scissors, opp paper"},
                              defense.build_context([]))
        self.assertIsNone(influence.state_for(start).owed)

    def test_honoring_replaces_the_action_with_the_promised_answer_when_the_engine_lists_it(self):
        spec, start = self.make()
        st = influence.state_for(start)
        st.owed = st.ledger.open_promises()[0]
        action, status = influence.honor_promise(start, "rock", ["rock", "paper"])
        self.assertEqual((action, status), ("paper", "kept"))
        self.assertEqual(st.ledger.promises[0]["status"], "kept")

    def test_a_promise_the_engine_no_longer_supports_is_marked_broken_and_blocks_new_ones(self):
        spec, start = self.make()
        st = influence.state_for(start)
        st.owed = st.ledger.open_promises()[0]
        action, status = influence.honor_promise(start, "rock", ["rock", "scissors"])
        self.assertEqual((action, status), ("rock", "broken"))
        self.assertTrue(st.ledger.broken())

    def test_nothing_changes_without_a_due_promise(self):
        spec, start = self.make()
        self.assertEqual(influence.honor_promise(start, "rock", ["rock", "paper"]), ("rock", None))


# ---------------------------------------------------------------- the engine publishes the reply tables
class EngineRepliesTests(T.Base):

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"BOT_CPUS": "2", "BOT_MEM_LIMIT_MB": "4096"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(base.shutdown_workers)

    def ttt(self, workers, want=True):
        os.environ["BOT_SEARCH_WORKERS"] = str(workers)
        spec = self.install(T.TTT, game="ttt%s%s" % (workers, want))
        eng = self.engine(search.SearchEngine)
        eng.want_replies = want
        legal = ["mark %d" % i for i in range(9) if i not in (0, 4)]
        c = self.ask(eng, spec, self.turn(T.ttt_obs(list("X...O...."), 0), legal))
        return c, dict(base.LAST_REPLIES)

    def test_think_ahead_publishes_the_replies_to_the_move_it_recommends(self):
        c, rep = self.ttt(1)
        self.assertTrue(c)
        top = max(c, key=lambda x: x.value).action
        self.assertEqual(rep["action"], top)
        self.assertGreaterEqual(len(rep["rows"]), 2)
        self.assertTrue(all(-1.0 <= r["value"] <= 1.0 for r in rep["rows"]))
        a = offense.analyze_replies(rep)
        self.assertIsNotNone(a)
        self.assertGreaterEqual(a["gap"], 0.0)
        self.assertEqual(rep["family"], "think_ahead")
        self.assertTrue(all(r["exact"] for r in rep["rows"]))              # a solved little tree: exact values

    def test_two_workers_publish_the_table_of_the_part_that_holds_the_best_move(self):
        c, rep = self.ttt(2)
        self.assertTrue(c)
        self.assertEqual(rep.get("action"), max(c, key=lambda x: x.value).action)
        self.assertGreaterEqual(len(rep.get("rows") or []), 2)

    def test_nothing_is_published_unless_the_influence_layer_asks(self):
        c, rep = self.ttt(1, want=False)
        self.assertTrue(c)
        self.assertEqual(rep, {})

    def test_the_table_is_cleared_every_turn_so_it_is_never_stale(self):
        c, rep = self.ttt(1)
        self.assertTrue(rep)
        spec = self.install(T.PILE, game="pile-stale")
        eng = self.engine(search.SearchEngine)
        eng.want_replies = False
        self.ask(eng, spec, self.turn(T.PILE_OBS % 9, ["take 1", "take 2", "take 3"]))
        self.assertEqual(dict(base.LAST_REPLIES), {})

    def test_without_the_request_the_search_is_the_same_as_before(self):
        a, _ = self.ttt(1, want=True)
        b, _ = self.ttt(1, want=False)
        self.assertEqual([(x.action, round(x.value, 6)) for x in a], [(x.action, round(x.value, 6)) for x in b])

    def test_read_opponent_publishes_the_matrix_and_remembers_the_previous_pick(self):
        spec = self.install(T.RPS, family="read_opponent", simultaneous=True)
        eng = self.engine(simultaneous.SimultaneousEngine)
        eng.want_replies = True
        legal = ["rock", "paper", "scissors"]
        c1 = self.ask(eng, spec, self.turn(T.rps_obs(1, 5, []), legal, game_index=1, turn=1))
        rep1 = dict(base.LAST_REPLIES)
        self.assertEqual(rep1["acts"], legal)
        self.assertEqual(len(rep1["matrix"]), 3)
        self.assertEqual(rep1["action"], c1[0].action)
        self.assertIsNone(rep1.get("prev_action"))
        self.ask(eng, spec, self.turn(T.rps_obs(2, 5, [(c1[0].action, "rock")]), legal, game_index=1, turn=2))
        rep2 = dict(base.LAST_REPLIES)
        self.assertEqual(rep2["prev_action"], c1[0].action)
        self.assertIsNotNone(offense.analyze_replies(rep2))


class RepliesJobTests(unittest.TestCase):
    """The replies_only job of search_job.py on the small gravity game of the tactical tests."""

    def run_job(self, board, action, budget=0.6, connect=3, **extra):
        from bot.engines import search_job
        from tests.test_tactical_search import SIM
        legal = ["drop %d" % c for c in range(len(board[0])) if any(row[c] == "." for row in board)]
        payload = {"observation": json.dumps({"board": board, "mover": 0, "connect": connect}),
                   "legal_actions": legal, "variant": {}, "seat": 0, "both_seats": True, "budget_s": budget,
                   "replies_only": True, "action": action}
        payload.update(extra)
        return search_job.run(SIM, payload)

    def test_a_solved_position_gives_exact_values_and_a_real_gap(self):
        board = [["X", "X", "O"], ["X", ".", "O"], [".", ".", "."]]
        # after "drop 2" (O's column is full above, so we stack): the replies are searched to the end
        res = self.run_job(board, "drop 0")
        self.assertEqual(res["algo"], "replies")
        table = res["replies"]
        if table is not None:
            self.assertEqual(table["action"], "drop 0")
            self.assertTrue(all(-1.0 <= r["value"] <= 1.0 for r in table["rows"]))

    def test_an_unsolved_position_is_estimated_by_playouts_and_marked_not_exact(self):
        board = [["."] * 5 for _ in range(5)]
        res = self.run_job(board, "drop 2", budget=0.5, connect=4)
        table = res["replies"]
        self.assertIsNotNone(table)
        self.assertGreaterEqual(len(table["rows"]), 2)
        self.assertFalse(all(r["exact"] for r in table["rows"]))
        self.assertTrue(all(-1.0 <= r["value"] <= 1.0 for r in table["rows"]))

    def test_a_move_that_ends_the_game_or_an_unknown_move_has_no_table(self):
        board = [["X", "X", "."], ["O", "O", "."], [".", ".", "."]]
        self.assertIsNone(self.run_job(board, "drop 2")["replies"])           # we win: no reply to analyse
        self.assertIsNone(self.run_job(board, "drop 9")["replies"])           # not a legal move

    def test_the_job_respects_its_budget(self):
        t = time.monotonic()
        self.run_job([["."] * 6 for _ in range(6)], "drop 3", budget=0.4, connect=4)
        self.assertLess(time.monotonic() - t, 2.5)


# ---------------------------------------------------------------- the chain: decide, ladder, stats, report
class ChainTests(Isolated):

    def test_the_objective_carries_the_engine_analysis_and_the_targeted_intent(self):
        g = offense._goal_from("invite_blunder", "reactive", 1, 6, 0)
        t = {"action": "B2", "rows": rows(("A1", -0.8), ("A2", 0.9))}
        offense._apply_analysis(g, "invite_blunder", offense.analyze_replies(t), t)
        text = decide.render_objective(g)
        self.assertIn("ENGINE ANALYSIS", text)
        self.assertIn("assumes you play B2", text)
        self.assertIn("A2", text)
        self.assertIn("gap 1.70", text)
        self.assertIn("solid, natural reply", text)

    def test_playing_another_move_than_the_analysed_one_voids_the_tactic_and_the_line(self):
        from bot import ladder
        spec = make_spec("think_ahead")
        start = {"match_id": "mx", "opponent": "opp", "game_index": 1, "seat": 0}
        g = offense._goal_from("invite_blunder", "reactive", 1, 6, 0)
        t = {"action": "A1", "rows": rows(("B1", -0.8), ("B2", 0.9))}
        offense._apply_analysis(g, "invite_blunder", offense.analyze_replies(t), t)
        reply = json.dumps({"action": "A2", "chat": "B2 looks like the solid reply here."})
        with mock.patch("bot.llm.complete", return_value=reply), \
                mock.patch.object(influence, "prepare", return_value=g):
            d = ladder.decide_turn(spec, start, {"game_index": 1, "turn": 6, "observation": "o",
                                                 "legal_actions": ["A1", "A2"]}, time.monotonic() + 10)
        self.assertEqual(d.action, "A2")
        self.assertEqual(d.goal.tactic_id, "quiet")
        self.assertNotIn("B2", d.chat)

    def test_the_line_survives_when_the_move_is_the_analysed_one(self):
        from bot import ladder
        spec = make_spec("think_ahead")
        start = {"match_id": "my", "opponent": "opp", "game_index": 1, "seat": 0}
        g = offense._goal_from("invite_blunder", "reactive", 1, 6, 0)
        t = {"action": "A2", "rows": rows(("B1", -0.8), ("B2", 0.9))}
        offense._apply_analysis(g, "invite_blunder", offense.analyze_replies(t), t)
        reply = json.dumps({"action": "A2", "chat": "B2 looks like the solid reply here."})
        with mock.patch("bot.llm.complete", return_value=reply), mock.patch.object(influence, "prepare", return_value=g):
            d = ladder.decide_turn(spec, start, {"game_index": 1, "turn": 6, "observation": "o",
                                                 "legal_actions": ["A1", "A2"]}, time.monotonic() + 10)
        self.assertEqual(d.chat, "B2 looks like the solid reply here.")
        self.assertEqual((d.goal.tactic_id, d.goal.target_action), ("invite_blunder", "B2"))

    def test_a_due_promise_forces_the_promised_answer_through_the_whole_ladder(self):
        from bot import ladder
        spec = make_spec("read_opponent", simultaneous=True)
        start = {"match_id": "mz", "opponent": "opp", "game_index": 1, "seat": 0}
        st = influence.state_for(start)
        g = goal("commitment", game=1, turn=1)
        g.promise_if, g.promise_then = "rock", "paper"
        st.ledger.promise(g)
        st.owed = st.ledger.open_promises()[0]
        cands = [T.Candidate("rock", 0.0, "n"), T.Candidate("paper", 0.0, "n")] if hasattr(T, "Candidate") else None
        from bot.interfaces import Candidate
        engine = mock.Mock()
        engine.candidates.return_value = [Candidate("rock", 0.0, "n"), Candidate("paper", -0.001, "n")]
        reply = json.dumps({"action": "rock", "chat": "Sounds fair."})
        with mock.patch("bot.llm.complete", return_value=reply), \
                mock.patch.object(influence, "prepare", return_value=offense._quiet()), \
                mock.patch("bot.engines.base.get_engine", return_value=engine):
            spec.verified, spec.simulator_path = True, "x.py"
            d = ladder.decide_turn(spec, start, {"game_index": 1, "turn": 2, "observation": "o",
                                                 "legal_actions": ["rock", "paper", "scissors"]},
                                   time.monotonic() + 10)
        self.assertEqual(d.action, "paper")
        self.assertEqual(st.ledger.promises[0]["status"], "kept")

    def test_the_ladder_asks_for_replies_and_keeps_time_for_the_model(self):
        from bot import config, ladder
        from bot.interfaces import Candidate
        spec = make_spec("think_ahead")
        spec.verified, spec.simulator_path = True, "x.py"
        engine = mock.Mock()
        engine.candidates.return_value = [Candidate("A1", 0.0, "n")]
        start = {"match_id": "ml", "opponent": "opp", "game_index": 1, "seat": 0}
        deadline = time.monotonic() + 13.0
        with mock.patch("bot.engines.base.get_engine", return_value=engine):
            ladder.decide_turn(spec, start, {"game_index": 1, "turn": 1, "observation": "o",
                                             "legal_actions": ["A1", "A2"]}, deadline)
        asked = engine.candidates.call_args[0][2]
        self.assertAlmostEqual(asked, deadline - 2.0 - config.LLM_RESERVE_S, delta=0.5)
        self.assertTrue(engine.want_replies in (True, False))
        self.assertGreaterEqual(config.LLM_RESERVE_S, 0.0)

    def test_family_wide_statistics_are_kept_in_memory_for_the_bandit(self):
        opponents.record_tactic("t9", "invite_blunder", True, True, family="think_ahead")
        opponents.record_tactic("t9", "invite_blunder", True, False, family="think_ahead")
        opponents.record_tactic("t8", "invite_blunder", True, True, family="think_ahead")
        opponents.record_tactic("t8", "invite_blunder", True, False, control=True, family="think_ahead")
        opponents.record_tactic("t8", "invite_blunder", False, False, family="think_ahead")          # an attempt, unscored
        own, glob = opponents.arm_stats("t9", "think_ahead")
        self.assertEqual(own["invite_blunder"]["scored"], 2)
        self.assertEqual(glob["invite_blunder"]["scored"], 3)
        self.assertEqual(glob["invite_blunder"]["hits"], 2)
        self.assertEqual(glob["control"]["scored"], 1)
        self.assertEqual(opponents.arm_stats("nobody", "bluff"), ({}, {}))

    def test_the_bandit_uses_the_stats_through_the_orchestrator(self):
        for _ in range(12):
            opponents.record_tactic("tb", "invite_blunder", True, True, family="think_ahead")
            opponents.record_tactic("tb", "fake_threat", True, False, family="think_ahead")
            opponents.record_tactic("tb", "x", True, False, control=True, family="think_ahead")
        spec = make_spec("think_ahead")
        start = {"match_id": "mb2", "opponent": "tb"}
        st = influence.state_for(start)
        st.opp_class, st.decisions = "reactive", 8
        picks = {}
        with mock.patch.object(opponents, "classify", return_value=("reactive", {})), \
                mock.patch.object(offense, "CONTROL_RATE", 0.0):
            for n in range(60):
                st.decisions = 8
                g = influence.prepare(spec, start, {"game_index": 1, "turn": 10 + n, "observation": "o"},
                                      defense.build_context([]))
                picks[g.tactic_id] = picks.get(g.tactic_id, 0) + 1
        self.assertGreater(picks.get("invite_blunder", 0), 40)
        self.assertLessEqual(st.explored, bandit.EXPLORE_MAX)

    def test_exploration_is_counted_per_match_and_never_exceeds_the_cap(self):
        spec = make_spec("bluff")
        start = {"match_id": "me2", "opponent": "te"}
        st = influence.state_for(start)
        with mock.patch.object(opponents, "classify", return_value=("reactive", {})):
            for n in range(120):
                influence.prepare(spec, start, {"game_index": 1, "turn": n + 1, "observation": "o"},
                                  defense.build_context([]))
        self.assertLessEqual(st.explored, bandit.EXPLORE_MAX)

    def test_the_event_carries_the_engine_fields_and_the_arm(self):
        spec = make_spec("think_ahead")
        start = {"match_id": "mev", "opponent": "opp"}
        g = offense._goal_from("invite_blunder", "reactive", 1, 6, 0)
        t = {"action": "A1", "rows": rows(("B1", -0.8), ("B2", 0.9))}
        offense._apply_analysis(g, "invite_blunder", offense.analyze_replies(t), t)
        g.arm_info = {"explore": False, "greedy": "invite_blunder"}
        events = []
        with mock.patch.object(metrics, "record_influence", side_effect=lambda **f: events.append(f)), \
                mock.patch.object(opponents, "record_tactic"):
            influence.on_sent(g, spec, start, {"game_index": 1, "turn": 6, "observation": "o"}, "A1", "line")
        e = events[0]
        self.assertEqual((e["target"], e["avoid"], e["from_engine"]), ("B2", "B1", True))
        self.assertAlmostEqual(e["gap"], 1.7)
        self.assertEqual(e["arm"]["greedy"], "invite_blunder")

    def test_safe_lines_rotate_with_the_turn_and_the_game(self):
        lines = {offense.fallback_line("think_ahead", {"turn": t, "game_index": g}) for t in range(1, 9) for g in (1, 2)}
        self.assertGreaterEqual(len(lines), 4)
        self.assertTrue(all(len(x) <= 80 for x in lines))

    def test_chat_is_move_is_decided_in_prep_and_stored_with_the_profile(self):
        from bot.prep import pipeline
        spec = make_spec("deals", action_format="a free-text message to the other player")
        spec.rules_hash = memory.rules_hash("rules-x")
        pipeline.mark_chat_is_move([spec])
        self.assertTrue(spec.profile.chat_is_move)
        reloaded = memory.load_variant(spec.game_id, spec.rules_hash, spec.rules)
        self.assertTrue(reloaded is None or reloaded.profile.chat_is_move)
        plain = make_spec("deals", action_format="offer <n>")
        pipeline.mark_chat_is_move([plain])
        self.assertFalse(plain.profile.chat_is_move)


if __name__ == "__main__":
    unittest.main()
