"""Read the opponent: equilibrium from the payoff matrix, opponent model, capped exploitation.

Owner: luckydevs-3. Status: real.

For simultaneous-move games. The computation is the sandbox job
bot/engines/simultaneous_job.py (see its docstring); this file is the parent
side: the payload, the memory across the games of one match, and the sampling
of OUR action from the final mixed strategy.

Why sampling is here and not "the best action": a mixed strategy that always
plays its most likely action is a pure strategy and can be read. The job returns the
final mix (equilibrium, tilted towards the best response to the opponent model by a
capped weight). Here ONE action is drawn from that mix with a generator seeded from a
per-process secret salt, the game and the turn: never from the chat, and not
predictable by the opponent. That action is the top candidate. The other actions with at
least MIN_PROB are returned as alternatives with values just inside the ladder's margin
(an equilibrium makes every action of its support equally good, so this is also honest),
and the note carries the probabilities. The model can then use the chat to bluff about the
move, and it may pick another supported action.

Across games of one match (same container, the opponent is the same): the opponent's
decayed action counts of the last game are kept in the engine as a prior (halved each game).
Nothing is read from bot/opponents.py (it stores no actions); see the hand-over.
"""

import os

from ..interfaces import Candidate
from ..policies import allocation
from . import base

# ---------------------------------------------------------------- constants (proposed for bot/config.py)
PRIOR_KEEP = 0.5             # weight of older games when a game ends
JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simultaneous_job.py")


class SimultaneousEngine(base.MixedEngine):
    family = "read_opponent"
    job_file = JOB_FILE

    def __init__(self):
        super().__init__()
        self._prior = {}                                  # (game_id, rules_hash) -> {action: weight}

    def candidates(self, specialist, turn_msg, deadline):
        if allocation.can_handle(getattr(specialist, "rules", ""), specialist.sample_turn) > 0:
            return [Candidate(row["action"], row["score"], "allocation policy")
                    for row in allocation.decide(
                        {"spec": specialist, "turn": turn_msg}, deadline)]
        return super().candidates(specialist, turn_msg, deadline)

    def game_ended(self, key, game):
        counts = game.extra.get("counts")
        if counts:
            old = self._prior.get(key, {})
            merged = {a: PRIOR_KEEP * old.get(a, 0.0) + counts.get(a, 0.0) for a in set(old) | set(counts)}
            self._prior[key] = merged

    def reset(self):
        super().reset()
        self._prior.clear()

    def payload(self, spec, turn_msg, game):
        return {"prior_counts": self._prior.get((spec.game_id, spec.rules_hash), {})}

    def build(self, res, spec, turn_msg, game):
        rows = res.get("actions") or []
        if not rows:
            return []
        if res.get("counts"):
            game.extra["counts"] = res["counts"]
        if res.get("algo") == "single":
            return [Candidate(rows[0]["action"], 0.0, "only legal move")]
        acts = [r["action"] for r in rows]
        probs = [max(0.0, float(r["p_final"])) for r in rows]
        pick = self.sample(spec, turn_msg, acts, probs)
        value = float(res.get("value", 0.0))
        mix = ", ".join("%s %.0f%%" % (r["action"], 100 * r["p_final"]) for r in sorted(rows, key=lambda r: -r["p_final"])
                        if r["p_final"] >= 0.005)
        head = "mixed strategy (%s, value %.2f)%s: %s" % (
            "zero-sum" if res.get("zero_sum") else "not zero-sum", value,
            "" if not res.get("w") else ", exploiting their pattern with weight %.2f (best response %s, %d observed "
            "actions)" % (res["w"], res.get("br"), res.get("n_obs", 0)), mix)
        self._stash_replies(res, pick, spec, turn_msg, game)
        return self.mixed([{"action": r["action"], "p": r["p_final"]} for r in rows], pick, value, head)

    def _stash_replies(self, res, pick, spec, turn_msg, game):
        """The payoff matrix and our drawn action, for the influence layer (their likely and weakest reply, a
        promise we can keep, a pattern we can break). The previous drawn action is kept in game.extra."""
        try:
            prev = game.extra.get("last_pick")
            game.extra["last_pick"] = pick
            matrix, acts = res.get("matrix"), res.get("opp_actions")
            if not matrix or not acts or pick not in acts:
                return
            base.LAST_REPLIES.update(
                family=self.family, game=turn_msg.get("game_index"), turn=turn_msg.get("turn"), action=pick,
                matrix=matrix, acts=list(acts), model=dict(res.get("model") or {}), opp_eq=dict(res.get("opp_eq") or {}),
                prev_action=prev)
        except Exception:
            base.log.exception("simultaneous: reply table not published")


base.register(SimultaneousEngine())
