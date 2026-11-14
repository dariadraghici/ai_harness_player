"""Guessing: hypothesis filtering and best-split choice.

Owner: luckydevs-3. Status: real.

For deduction-from-feedback games. The work is the sandbox job
bot/engines/guess_job.py (see its docstring for the method); this file is the
parent side: the payload and the candidates.

  * hypotheses: the hidden values the simulator can draw "consistent with all
    feedback so far" (contract C11), sampled by seeds from the state parsed from
    THIS turn's observation. A small set is complete, a large one a sample;
  * every candidate action is applied in every hypothesis and the hypotheses are
    grouped by what our view then shows (the feedback); the best split leaves the
    fewest hypotheses (expected log2), an action that names a possible answer gets
    credit for the chance that it is right;
  * long action lists (guess a number from 1 to 1000): a spread sample of the list,
    the actions that are hypotheses, then the neighbors of the best ones.

Candidate values are RELATIVE to the best split: the best is 1.0 and every 0.1 bit
of expected information lost costs 0.05, so the ladder's margin keeps the actions
that are practically as good and the model may choose among them. The note carries
the numbers (hypotheses, exact or sample, expected hypotheses left, probability of
being right).
"""

import os
import re

from ..interfaces import Candidate
from . import base

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guess_job.py")


def action_seen(action, observation):
    """Whether an action already appears as a complete token in the observation."""
    if not isinstance(action, str) or not isinstance(observation, str):
        return False
    text = " ".join(observation.casefold().split())
    needle = " ".join(action.casefold().split())
    if not needle:
        return False
    return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(needle), text) is not None


def unseen_actions(actions, observation):
    """Keep actions not already recorded by the game observation."""
    if not isinstance(actions, list):
        return []
    return [action for action in actions if not action_seen(action, observation)]


VALUE_SLOPE = 0.5               # same as guess_job.py (the job is standalone and cannot be imported)
MAX_RESULT_ACTIONS = 14


class GuessEngine(base.SimEngine):
    family = "guessing"
    job_file = JOB_FILE
    splittable = True

    def merge(self, results, payload):
        """Every part sampled the same hypotheses and scored its share of the actions: pool the scored actions,
        re-rank by cost (ties: more feedback classes, then position in the legal list) and value them against the pooled best."""
        if any(r.get("algo") != "split" for r in results):
            return results[0]
        rows, seen = [], set()
        for r in results:
            for a in r.get("actions") or []:
                if a["action"] not in seen:
                    seen.add(a["action"])
                    rows.append(dict(a))
        if not rows:
            return results[0]
        rows.sort(key=lambda a: (a["cost"], -a.get("classes", 0), a.get("i", 0)))
        best = rows[0]["cost"]
        rows = rows[:MAX_RESULT_ACTIONS]
        for a in rows:
            a["value"] = max(-1.0, 1.0 - VALUE_SLOPE * (a["cost"] - best))
        out = dict(results[0])
        out.update(actions=rows, scored=sum(int(r.get("scored") or 0) for r in results))
        return out

    def build(self, res, spec, turn_msg, game):
        out = []
        n = res.get("hypotheses", 0)
        rows = res.get("actions") or []
        unseen = set(unseen_actions([str(a.get("action", "")) for a in rows],
                                    str(turn_msg.get("observation") or "")))
        for a in rows:
            if str(a.get("action", "")) not in unseen:
                continue
            if res.get("algo") == "single":
                note = "only legal move"
            else:
                note = "best split of %d %s hypotheses: %.1f left on average in %d feedback classes, " \
                       "P(right) %.3f, scored %d of %d actions" % (
                           n, "exact" if res.get("exact") else "sampled", a.get("expected_left", 0.0),
                           a.get("classes", 0), a.get("p_hit", 0.0), res.get("scored", 0), res.get("of", 0))
            out.append(Candidate(a["action"], max(-1.0, min(1.0, float(a["value"]))), note))
        return out


base.register(GuessEngine())
