"""Bluff: Monte Carlo over the hidden states consistent with what we saw. A sandbox JOB.

Owner: luckydevs-3. Status: real (written without a hidden-information practice game; see the hand-over).

Never imported by the bot (bot/engines/bluff.py sends it to a sandbox child). Standalone: standard
library only, plus sim_common.py loaded by path.

For hidden-information games (betting, challenges, bluffing) where it is OUR turn in an alternating step:

  1. WORLDS. The hidden parts of our view (None in the parsed state) are filled in by the simulator
     under many seeds (contract C11: "consistent with all feedback so far"); every different filled-in
     value is one possible world (sim_common.sample_hypotheses). If nothing is hidden the worlds are
     the state itself and only chance differs between samples.
  2. EXPECTED VALUE PER ACTION. For every legal action and every sampled world: apply the action in
     that world, play the rest out and average our return. The opponent follows a simple policy:
     uniform over its legal actions, and in a share of the samples (opp_share) ONE level smarter at
     its first decision: it looks at each of its actions with a few random playouts in the world
     (so it uses its own hidden value) and takes the best. All actions share the same random
     numbers in one sample (common random numbers), so the differences between actions are
     precise even when single playouts are noisy.
  3. MIXED PLAY. Softmax over the expected values with a temperature tied to the spread of the
     values (and to the standard error, so noise means more mixing): p_i ~ exp((EV_i - EV_max) / tau).
     The parent engine draws ONE action from these probabilities with a seeded generator, never from
     chat, and offers the actions that have at least a minimum probability.

payload: observation, legal_actions, variant, seat, budget_s, unknown, opp_share, worlds.
Result: {"ok": True, "live": True, "algo": "worlds", "actions": [{"action", "ev", "se", "p", "n"}],
         "worlds", "exact", "hidden", "samples", "spread", "tau", "best"} or {"ok": False, "live": ..., "reason"}.
"""

import math
import os
import random
import types
import zlib

MAX_WORLDS = 240
WORLD_SEEDS = 6000
OPP_SHARE = 0.35           # share of the samples in which the opponent looks one level ahead
OPP_PLAYOUTS = 2           # random playouts per action of the one-level opponent
TAU_REL = 0.35             # softmax temperature as a share of the spread of the values
TAU_MIN = 0.03
SE_FACTOR = 1.5            # noise: the temperature is at least this many standard errors
SAMPLES_CAP = 4000         # per action
PLAYOUT_CAP = 400
WORLD_SHARE = 0.15         # of the slice for sampling the worlds


def _common():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_common.py")
    mod = types.ModuleType("sim_common")
    with open(path, "r", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


C = _common()


def playout(g, s, prng, smart, steps_cap=PLAYOUT_CAP):
    """Play s to the end. At the first decision of the other player, when `smart`, it takes the action with the
    best mean of OPP_PLAYOUTS random playouts in this very world. Returns our return (0 if no end)."""
    steps = 0
    first = True
    while steps < steps_cap and not g.terminal(s):
        la = g.legal(s)
        if not la:
            break
        cp = g.player(s)
        if smart and first and cp in (0, 1) and cp != g.me and len(la) > 1:
            first = False
            best_a, best_v = None, -9.0
            for a in la:
                tot = 0.0
                for _ in range(OPP_PLAYOUTS):
                    sub = random.Random(prng.getrandbits(32))
                    t, k = g.apply(s, a), 0
                    while k < PLAYOUT_CAP and not g.terminal(t):
                        l2 = g.legal(t)
                        if not l2:
                            break
                        t = g.apply(t, g.random_action(t, l2, sub))
                        k += 1
                    tot += g.returns(t)[cp] if (g.terminal(t) or k < PLAYOUT_CAP) else 0.0
                v = tot / OPP_PLAYOUTS + prng.random() * 1e-9
                if v > best_v:
                    best_a, best_v = a, v
            s = g.apply(s, best_a)
        else:
            if cp in (0, 1) and cp != g.me:
                first = False                      # the other player has had its decision
            s = g.apply(s, g.random_action(s, la, prng))
        steps += 1
    if g.terminal(s) or steps < steps_cap:
        return g.value(s)
    return 0.0


def softmax(evs, ses):
    best = max(evs)
    spread = best - min(evs)
    tau = max(TAU_MIN, TAU_REL * spread)
    if ses:
        tau = max(tau, SE_FACTOR * sorted(ses)[len(ses) // 2])
    w = [math.exp(max(-50.0, (e - best) / tau)) for e in evs]
    t = sum(w)
    return [x / t for x in w], tau, spread


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator loaded"}
    clock = C.Clock(payload.get("budget_s") or 2.0)
    g, state, reason = C.resolve(sim, payload)
    if g is None:
        return {"ok": False, "live": False, "reason": reason}
    if g.player(state) != g.me:
        return {"ok": False, "live": True, "reason": "a simultaneous step is not played by this engine"}
    la = g.legal(state)
    grow = max(1.0, float(payload.get("samples_scale") or 1.0))      # default 1.0 = today's caps
    rng = random.Random(zlib.crc32(C.dump(payload.get("observation", "")).encode("utf-8")))
    if len(la) == 1:
        return {"ok": True, "live": True, "algo": "single", "actions": [
            {"action": la[0], "ev": 0.0, "se": 0.0, "p": 1.0, "n": 0}], "worlds": 0}
    hyps, paths, exact = C.sample_hypotheses(g, state, la, C.Clock(WORLD_SHARE * clock.left()), rng,
                                             int(payload.get("worlds") or int(MAX_WORLDS * grow)), WORLD_SEEDS)
    if hyps:
        worlds = [C.world_state(state, paths, h) for h in hyps]
        drawn = False
    else:
        # No world could be read back from the simulator (it may only reveal the hidden value at a showdown,
        # or overwrite it with the next deal). That is not a reason to give up: every sample below re-seeds the
        # state, so the simulator draws the hidden value itself and the expected value still averages over the
        # worlds - it is the WORLD LIST we cannot show, not the average.
        worlds, exact, drawn = [state], False, True
    opp_share = float(payload.get("opp_share", OPP_SHARE))
    n = len(la)
    sums, sq, cnt = [0.0] * n, [0.0] * n, [0] * n
    samples = 0
    while True:
        w = worlds[samples % len(worlds)] if samples < len(worlds) * 4 else rng.choice(worlds)
        seed = rng.randrange(C.SEED_MAX)
        smart = rng.random() < opp_share
        for i, a in enumerate(la):
            if clock.up() and samples > 0:
                break
            try:
                s0 = C.seeded(w, seed)
                prng = random.Random(seed)
                v = playout(g, g.apply(s0, a), prng, smart)
            except ValueError:
                continue
            sums[i] += v
            sq[i] += v * v
            cnt[i] += 1
        samples += 1
        if clock.up() or samples >= int(SAMPLES_CAP * grow):
            break
    ok_idx = [i for i in range(n) if cnt[i] > 0]
    if not ok_idx:
        return {"ok": False, "live": True, "reason": "no action could be evaluated in the time given"}
    evs = [sums[i] / cnt[i] for i in ok_idx]
    ses = [math.sqrt(max(1e-4, sq[i] / cnt[i] - (sums[i] / cnt[i]) ** 2) / cnt[i]) for i in ok_idx]
    probs, tau, spread = softmax(evs, ses)
    rows = [{"action": la[i], "ev": evs[k], "se": ses[k], "p": probs[k], "n": cnt[i]} for k, i in enumerate(ok_idx)]
    rows.sort(key=lambda r: -r["p"])
    return {"ok": True, "live": True, "algo": "worlds", "actions": rows, "worlds": len(worlds),
            "worlds_drawn": bool(drawn),
            "exact": bool(exact), "hidden": len(paths), "samples": samples, "spread": spread, "tau": tau,
            "best": max(evs)}
