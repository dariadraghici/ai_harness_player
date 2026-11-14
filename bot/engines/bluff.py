"""Bluff: Monte Carlo over hidden states, expected value per action, mixed play.

Owner: luckydevs-3. Status: real, but OFF by default (see BOT_L3 in base.py): no practice game of
the Bluff family exists locally, so there is no measurement that supports turning it on.

The computation is the sandbox job bot/engines/bluff_job.py (see its docstring); this file is the
parent side. The job samples the worlds consistent with what we saw (the simulator draws the
hidden values under many seeds), estimates the expected value of every legal action by playouts
against a simple opponent policy and returns softmax probabilities (temperature tied to the
spread of the values and to the noise). Here ONE action is drawn from those probabilities
(base.MixedEngine.sample: seeded from a per-process salt, the game and the turn, never from chat) and
returned as the top candidate; the other actions with at least MIN_PROB are alternatives inside the
ladder's margin. The notes carry the expected values and probabilities, so the model can bluff in
the chat about a move that the engine drew.

Not done: the opponent model of read_opponent (their past actions) is not used, because in
alternating games the history in a parsed state interleaves both players and the generic probe
cannot separate them; see the hand-over for what bot/opponents.py would have to store.
"""

import os

from ..interfaces import Candidate
from . import base

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bluff_job.py")


class BluffEngine(base.MixedEngine):
    family = "bluff"
    job_file = JOB_FILE

    def build(self, res, spec, turn_msg, game):
        rows = res.get("actions") or []
        if not rows:
            return []
        if res.get("algo") == "single":
            return [Candidate(rows[0]["action"], 0.0, "only legal move")]
        acts = [r["action"] for r in rows]
        probs = [max(0.0, float(r["p"])) for r in rows]
        pick = self.sample(spec, turn_msg, acts, probs)
        by = {r["action"]: r for r in rows}
        value = by[pick]["ev"]
        head = "expected value over %d %s possible worlds, %d samples; mixed play (temperature %.2f)" % (
            res.get("worlds", 0), "all" if res.get("exact") else "sampled", res.get("samples", 0), res.get("tau", 0.0))
        return self.mixed([{"action": r["action"], "p": r["p"],
                            "note": "EV %.2f +- %.2f" % (r["ev"], r["se"])} for r in rows], pick, value, head)


base.register(BluffEngine())
