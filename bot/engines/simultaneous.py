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
import random
import zlib

from ..interfaces import Candidate
from . import base

# ---------------------------------------------------------------- constants (proposed for bot/config.py)
MIN_PROB = 0.08              # an action with less probability in the final mix is not offered
VALUE_STEP = 0.002           # value difference between consecutive candidates (all inside the margin)
PRIOR_KEEP = 0.5             # weight of older games when a game ends
JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simultaneous_job.py")


class SimultaneousEngine(base.SimEngine):
    family = "read_opponent"
    job_file = JOB_FILE

    def __init__(self):
        super().__init__()
        self._prior = {}                                  # (game_id, rules_hash) -> {action: weight}
        self._salt = "%016x" % random.SystemRandom().getrandbits(64)

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

    def sample(self, spec, turn_msg, acts, probs):
        """One action drawn from probs. Seeded from the salt, the game and the turn only."""
        seed = zlib.crc32(("%s|%s|%s|%s|%s" % (self._salt, spec.game_id, spec.rules_hash,
                                                turn_msg.get("game_index"), turn_msg.get("turn"))).encode("utf-8"))
        rng = random.Random(seed)
        x, acc = rng.random() * sum(probs), 0.0
        for a, p in zip(acts, probs):
            acc += p
            if x < acc:
                return a
        return acts[-1]

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
        value = max(-1.0, min(1.0, float(res.get("value", 0.0))))
        chosen = [r for r in rows if r["p_final"] >= MIN_PROB or r["action"] == pick]
        chosen.sort(key=lambda r: (r["action"] != pick, -r["p_final"]))
        mix = ", ".join("%s %.0f%%" % (r["action"], 100 * r["p_final"]) for r in sorted(rows, key=lambda r: -r["p_final"])
                        if r["p_final"] >= 0.005)
        head = "mixed strategy (%s, value %.2f)%s: %s" % (
            "zero-sum" if res.get("zero_sum") else "not zero-sum", value,
            "" if not res.get("w") else ", exploiting their pattern with weight %.2f (best response %s, %d observed "
            "actions)" % (res["w"], res.get("br"), res.get("n_obs", 0)), mix)
        out = []
        for rank, r in enumerate(chosen):
            note = head + ("; DRAWN for this turn" if r["action"] == pick else "; alternative with P=%.2f" % r["p_final"])
            out.append(Candidate(r["action"], value - VALUE_STEP * rank, note))
        return out


base.register(SimultaneousEngine())
