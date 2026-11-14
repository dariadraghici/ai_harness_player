"""Guessing: hypothesis filtering and best-split choice as a sandbox JOB (runs INSIDE the child).

Owner: luckydevs-3. Status: real.

Never imported by the bot (bot/engines/guess.py sends it to a sandbox child). Standalone: standard
library only, plus sim_common.py loaded by path.

The simulator is the oracle of consistency (contract C11): the state parsed from the observation
holds all feedback so far, and a hidden value that is needed is drawn "consistent with all feedback
so far" from random.Random(state["seed"]). So:

  1. HYPOTHESES. The hidden parts of our view are the paths that are None in the parsed state
     (sim_common.hidden_paths). Applying one legal action under many seeds fills them in; each
     different filled-in value is one hypothesis (one possible world). Small sets are complete
     ("exact"), large ones stay a uniform sample. A world is written back into the parsed state
     (hidden values fixed), so any action can be tried in it.
  2. FEEDBACK. For a candidate action and each hypothesis: apply the action in that world and
     read what our view of the result shows, that is, the new state without the hidden parts and
     the seed. Hypotheses that give the same view cannot be told apart by this action. The
     partition of the hypotheses by view is the feedback distribution of the action.
  3. VALUE. cost(a) = expected log2 of the number of hypotheses still possible after the feedback
     + (1 - P(hit)) * TEMPO_BITS, where a hit is a world in which the action ends the game in our
     favor or names the hidden value (every scalar of the hidden value is a token of the action).
     The hit term makes the engine prefer a possible answer over an equally good split when few
     hypotheses remain. value = 1 - VALUE_SLOPE * (cost - best cost), so the best split is 1.0
     and the ladder's margin (0.05) admits actions at most 0.1 bit worse.
  4. WHICH ACTIONS. When the legal list is long: a spread sample of the list, the actions that are
     themselves hypotheses, and then every action next to the best ones (list neighbors).

payload: observation, legal_actions, variant, seat, budget_s, margin.
Result: {"ok": True, "live": True, "algo": "split", "hypotheses", "exact", "scored", "entropy_bits",
         "actions": [{"action", "value", "cost", "expected_left", "p_hit", "classes"}]} or
        {"ok": False, "live": ..., "reason"}.
"""

import math
import os
import random
import types
import zlib

SAMPLE_SEEDS = 40000
MAX_HYPOTHESES = 2000
SCORING_HYPOTHESES = 220         # hypotheses used to score one action (a random subset when more)
COARSE_ACTIONS = 90              # spread sample of a long legal list
HYPOTHESIS_ACTIONS = 90          # actions that are hypotheses themselves
TEMPO_BITS = 1.0
VALUE_SLOPE = 0.5                # value lost per bit of extra cost
MAX_RESULT_ACTIONS = 14
SAMPLE_SHARE = 0.35              # of the slice for sampling the hypotheses


def _common():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_common.py")
    mod = types.ModuleType("sim_common")
    with open(path, "r", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


C = _common()


class Scorer:

    def __init__(self, g, state, paths, hyps, clock):
        self.g, self.paths, self.clock = g, paths, clock
        self.hyps = hyps
        self.worlds = [C.world_state(state, paths, h) for h in hyps]
        self.tokens = [C.scalars(h) for h in hyps]
        self.cache = {}

    def score(self, action):
        """(cost, expected hypotheses left, p_hit, number of feedback classes) or None when the action
        cannot be applied in some world."""
        hit = self.cache.get(action)
        if hit is not None:
            return hit
        g = self.g
        classes, hits, n = {}, 0, 0
        for w, toks in zip(self.worlds, self.tokens):
            ns = g.apply(w, action)
            key = C.view_key(g, ns, self.paths)
            classes[key] = classes.get(key, 0) + 1
            n += 1
            won = False
            try:
                if g.terminal(ns) and g.value(ns) > 0:
                    won = True
            except Exception:
                pass
            if won or C.names_value(action, toks):
                hits += 1
        total = float(n)
        left = sum(c * c for c in classes.values()) / total
        bits = sum(c * math.log(c, 2) for c in classes.values()) / total
        p_hit = hits / total
        out = (bits + (1.0 - p_hit) * TEMPO_BITS, left, p_hit, len(classes))
        self.cache[action] = out
        return out


def pick_actions(la, hyps_tokens, rng):
    """Coarse set: a spread sample of the list plus the actions that are hypotheses."""
    n = len(la)
    if n <= COARSE_ACTIONS + HYPOTHESIS_ACTIONS:
        return list(range(n))
    stride = n / float(COARSE_ACTIONS)
    chosen = set(int(i * stride) for i in range(COARSE_ACTIONS))
    chosen.add(n - 1)
    # the actions that name a hypothesis
    index = {}
    for i, a in enumerate(la):
        for t in set(C.TOKEN.findall(a)):
            index.setdefault(t, []).append(i)
    named = set()
    for toks in hyps_tokens:
        if not toks:
            continue
        cand = None
        for t in toks:
            s = set(index.get(t, ()))
            cand = s if cand is None else cand & s
            if not cand:
                break
        if cand:
            named.update(cand)
    named = sorted(named)
    if len(named) > HYPOTHESIS_ACTIONS:
        step = len(named) / float(HYPOTHESIS_ACTIONS)
        named = [named[int(i * step)] for i in range(HYPOTHESIS_ACTIONS)]
    chosen.update(named)
    return sorted(chosen)


def _part(payload):
    """(index, count) of this part when several children share one decision, else (0, 1)."""
    try:
        parts = max(1, int(payload.get("parts") or 1))
        part = int(payload.get("part") or 0)
        if 0 <= part < parts:
            return part, parts
    except (TypeError, ValueError):
        pass
    return 0, 1


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator loaded"}
    clock = C.Clock(payload.get("budget_s") or 2.0)
    g, state, reason = C.resolve(sim, payload)
    if g is None:
        return {"ok": False, "live": False, "reason": reason}
    la = g.legal(state)
    rng = random.Random(zlib.crc32(C.dump(payload.get("observation", "")).encode("utf-8")))
    if len(la) == 1:
        return {"ok": True, "live": True, "algo": "single", "hypotheses": 0, "exact": False, "scored": 0,
                "actions": [{"action": la[0], "value": 0.0, "cost": 0.0, "expected_left": 0.0, "p_hit": 0.0,
                             "classes": 0}]}
    grow = max(1.0, float(payload.get("samples_scale") or 1.0))       # default 1.0 = today's caps
    hyps, paths, exact = C.sample_hypotheses(g, state, la, C.Clock(SAMPLE_SHARE * clock.left()), rng,
                                           int(MAX_HYPOTHESES * grow), int(SAMPLE_SEEDS * grow))
    if not hyps:
        return {"ok": False, "live": True, "reason": "no hidden value to sample"}
    n_total = len(hyps)
    if len(hyps) > SCORING_HYPOTHESES:
        pool = list(range(len(hyps)))
        rng.shuffle(pool)
        hyps = [hyps[i] for i in sorted(pool[:SCORING_HYPOTHESES])]
    scale = n_total / float(len(hyps))              # scores are over the subset; report for the whole set
    part, parts = _part(payload)
    sc = Scorer(g, state, paths, hyps, clock)
    first = pick_actions(la, sc.tokens, rng)
    n_first = len(first)
    if parts > 1:
        first = first[part::parts]       # every part samples the same hypotheses (same seeds) and scores its share
    scored = {}

    def run_over(indices):
        for i in indices:
            if clock.up() and scored:
                return False
            a = la[i]
            if a in scored:
                continue
            try:
                r = sc.score(a)
            except ValueError:
                continue
            scored[a] = (i,) + r
        return True

    complete = run_over(first)
    if complete and len(la) > n_first:
        # every action next to the best ones in the list order
        stride = max(1, int(len(la) / float(COARSE_ACTIONS)))
        best = sorted(scored, key=lambda a: scored[a][1])[:3]
        near = []
        for a in best:
            i = scored[a][0]
            near.extend(range(max(0, i - stride), min(len(la), i + stride + 1)))
        run_over(sorted(set(near)))
    if not scored:
        return {"ok": False, "live": True, "reason": "no action could be scored in the time given"}
    ranked = sorted(scored.items(), key=lambda kv: (kv[1][1], -kv[1][3], kv[1][0]))
    best_cost = ranked[0][1][1]
    actions = []
    for a, (i, cost, left, p_hit, classes) in ranked[:MAX_RESULT_ACTIONS]:
        actions.append({"action": a, "value": max(-1.0, 1.0 - VALUE_SLOPE * (cost - best_cost)), "cost": cost,
                        "expected_left": left * scale, "p_hit": p_hit, "classes": classes, "i": i})
    return {"ok": True, "live": True, "algo": "split", "hypotheses": n_total, "exact": bool(exact),
            "scored": len(scored), "of": len(la), "entropy_bits": math.log(n_total, 2) if n_total > 1 else 0.0,
            "actions": actions}
