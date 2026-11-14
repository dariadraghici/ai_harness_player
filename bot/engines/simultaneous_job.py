"""Read the opponent: payoff matrix, equilibrium, opponent model, capped exploitation. A sandbox JOB.

Owner: luckydevs-3. Status: real.

Never imported by the bot (bot/engines/simultaneous.py sends it to a sandbox child). Standalone:
standard library only, plus sim_common.py loaded by path.

For the current simultaneous step (current_player(state) == -1; contract C5: apply_action takes
[seat 0 action, seat 1 action], legal_actions are OURS and the opponent is assumed to have the same set):

  1. PAYOFF MATRICES. For every pair (our action a, their action b): apply the pair. When the new
     state is terminal the cell is exact; otherwise it is the mean of random playouts of the rest
     (another state["seed"] for every sample when the game has outcomes we cannot know). A is our
     return, B is theirs; when A + B is about 0 the game is zero-sum and B = -A.
  2. EQUILIBRIUM. Regret matching (regret matching+, linear averaging) on both matrices. The
     average strategies are x (ours) and y (theirs). Their payoff against x and ours against y
     give the value of the game.
  3. OPPONENT MODEL. The history of their past actions is read from the parsed state: a probe
     (apply one pair of two different actions) shows which list of the state grows and where their
     action sits in the new item. Counts are decayed (recent moves weigh more), smoothed, and mixed
     with what the engine kept from earlier games (prior_counts). If their last action has predicted
     the next one clearly better than the plain frequencies, that transition is mixed in.
  4. CAPPED EXPLOITATION. The best response BR to the model gets the weight
     w = W_MAX * n / (n + W_HALF) (only when it gains something): final = (1 - w) * x + w * onehot(BR).
     w never exceeds W_MAX, so an opponent that fakes a pattern cannot make us predictable.

payload: observation, legal_actions, variant, seat, budget_s, unknown, prior_counts {action: weight}.
Result: {"ok": True, "live": True, "algo": "equilibrium", "actions": [{"action", "p_eq", "p_final",
         "ev_eq", "ev_model"}], "value", "zero_sum", "n_obs", "w", "br", "model": {action: q},
         "counts": {action: weight}, "samples_per_cell", "exact_cells", "cells"} or
        {"ok": False, "live": ..., "reason"}.
"""

import math
import os
import random
import types
import zlib

W_MAX = 0.5                # the largest weight exploitation can ever get
W_HALF = 8.0               # observations at which exploitation has half of W_MAX
MIN_GAIN = 0.1             # the best response must beat the equilibrium against the model by this share of the
                           # matrix spread (max - min): a round of a long game is worth little in absolute terms
DECAY = 0.9                # per round, for the opponent's older actions
SMOOTH = 0.5               # pseudo-count per action in the frequency model
MAX_ACTIONS = 24           # more than this: a spread sample of the list
PRIOR_CAP = 10.0           # total weight of what earlier games contribute
PLAYOUT_CAP = 400
MATRIX_SHARE = 0.6
RM_ITERS = 6000
SAMPLES_CAP = 400          # playouts per cell: the matrix is precise enough, the slice is not used up for nothing


def _common():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_common.py")
    mod = types.ModuleType("sim_common")
    with open(path, "r", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


C = _common()


# ---------------------------------------------------------------- payoff matrices
def pair_for(me, ours, theirs):
    return [ours, theirs] if me == 0 else [theirs, ours]


def estimate_matrices(g, state, acts, clock, unknown, rng, grow=1.0):
    """Returns (A, B, samples per cell, exact cells), or None when some cell got no sample in time.
    A[i][j] is our return when we play acts[i] and they play acts[j]; B[i][j] is theirs."""
    n = len(acts)
    me = g.me
    sa = [[0.0] * n for _ in range(n)]
    sb = [[0.0] * n for _ in range(n)]
    cnt = [[0] * n for _ in range(n)]
    base, pending, exact = {}, [], 0
    for i in range(n):
        for j in range(n):
            if not unknown:
                s1 = g.apply(state, pair_for(me, acts[i], acts[j]))
                if g.terminal(s1):
                    r = g.returns(s1)
                    sa[i][j], sb[i][j], cnt[i][j] = r[me], r[1 - me], 1
                    exact += 1
                    continue
                base[(i, j)] = s1
            pending.append((i, j))
    rounds, stop = 0, False
    while pending and not stop:
        # Common random numbers: in one round every cell is played out with the same random choices
        # (and the same state["seed"]), so the noise of the continuation cancels in the differences
        # between cells, which is all the equilibrium depends on.
        round_seed = rng.randrange(C.SEED_MAX)
        for i, j in pending:
            if rounds > 0 and clock.up():
                stop = True
                break
            prng = random.Random(round_seed)
            s1 = base.get((i, j))
            if s1 is None:
                s1 = g.apply(C.seeded(state, round_seed), pair_for(me, acts[i], acts[j]))
            s, steps = s1, 0
            while steps < PLAYOUT_CAP and not g.terminal(s):
                la = g.legal(s)
                if not la:
                    break
                s = g.apply(s, g.random_action(s, la, prng))
                steps += 1
            if g.terminal(s) or steps < PLAYOUT_CAP:
                r = g.returns(s)
                sa[i][j] += r[me]
                sb[i][j] += r[1 - me]
            cnt[i][j] += 1
        rounds += 1
        if clock.up() or rounds >= int(SAMPLES_CAP * grow):
            stop = True
    if any(cnt[i][j] == 0 for i in range(n) for j in range(n)):
        return None
    A = [[sa[i][j] / cnt[i][j] for j in range(n)] for i in range(n)]
    B = [[sb[i][j] / cnt[i][j] for j in range(n)] for i in range(n)]
    return A, B, sum(sum(r) for r in cnt) / float(n * n), exact


# ---------------------------------------------------------------- regret matching
def _normalize(v):
    s = sum(v)
    return [x / s for x in v] if s > 0 else [1.0 / len(v)] * len(v)


def regret_matching(A, B, iters, clock):
    """Regret matching+ with linear averaging on both matrices. Returns (x, y)."""
    n, m = len(A), len(A[0])
    rx, ry = [0.0] * n, [0.0] * m
    sx, sy = [0.0] * n, [0.0] * m
    for t in range(1, iters + 1):
        if (t & 255) == 0 and clock.up():
            break
        x = _normalize([max(0.0, r) for r in rx])
        y = _normalize([max(0.0, r) for r in ry])
        ux = [sum(A[i][j] * y[j] for j in range(m)) for i in range(n)]
        uy = [sum(B[i][j] * x[i] for i in range(n)) for j in range(m)]
        vx = sum(x[i] * ux[i] for i in range(n))
        vy = sum(y[j] * uy[j] for j in range(m))
        for i in range(n):
            rx[i] = max(0.0, rx[i] + ux[i] - vx)
            sx[i] += t * x[i]
        for j in range(m):
            ry[j] = max(0.0, ry[j] + uy[j] - vy)
            sy[j] += t * y[j]
    return _normalize(sx), _normalize(sy)


# ---------------------------------------------------------------- the opponent model
def opponent_history(g, state, acts, la_state):
    """The opponent's past actions (oldest first) read from the parsed state, or None.

    A probe applies one pair of two different actions (ours a, theirs b) and looks for the list of the
    state that grew by one item: an item that is a pair of strings holds b at the index where the
    opponent's action sits; an item that is the string b means the list holds their actions."""
    if len(acts) < 2:
        return None
    a, b = acts[0], acts[1]
    try:
        after = g.apply(state, pair_for(g.me, a, b))
    except Exception:
        return None
    before_paths = {}
    for path, v in C.walk(state):
        if isinstance(v, list):
            before_paths[path] = v
    best = None
    for path, v in C.walk(after):
        old = before_paths.get(path)
        if not isinstance(v, list) or old is None or len(v) != len(old) + 1 or not v:
            continue
        last = v[-1]
        if isinstance(last, list) and len(last) == 2 and all(isinstance(x, str) for x in last):
            if last == [a, b]:
                idx = 1
            elif last == [b, a]:
                idx = 0
            else:
                continue
            ok = all(isinstance(it, list) and len(it) == 2 and all(isinstance(x, str) for x in it) for it in old)
            if ok:
                best = [it[idx] for it in old]
                break
        elif last == b and all(isinstance(x, str) for x in old) and C.get_path(state, path) == old:
            best = list(old)
            break
    if best is None:
        return None
    return [x for x in best if x in acts]


def model_distribution(acts, history, prior):
    """(q over acts, n observations, decayed counts, how the model was built)."""
    idx = {a: i for i, a in enumerate(acts)}
    counts = [0.0] * len(acts)
    n = len(history)
    for k, a in enumerate(history):
        counts[idx[a]] += DECAY ** (n - 1 - k)
    pw = 0.0
    if prior:
        tot = sum(v for v in prior.values() if v > 0)
        scale = min(1.0, PRIOR_CAP / tot) if tot > 0 else 0.0
        for a, v in prior.items():
            if a in idx and v > 0:
                counts[idx[a]] += v * scale
                pw += v * scale
    freq = _normalize([c + SMOOTH for c in counts])
    how = "frequencies"
    q = freq
    if n >= 8:
        last = history[-1]
        trans = [0.0] * len(acts)
        seen = 0
        for k in range(1, n):
            if history[k - 1] == last:
                trans[idx[history[k]]] += 1.0
                seen += 1
        if seen >= 4:
            tq = _normalize([t + SMOOTH for t in trans])
            if max(tq) >= max(freq) + 0.15:
                q = [0.5 * f + 0.5 * t for f, t in zip(freq, tq)]
                how = "frequencies and what follows their last action"
    return q, n + int(round(pw / 2.0)), counts, how


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator loaded"}
    clock = C.Clock(payload.get("budget_s") or 2.0)
    g, state, reason = C.resolve(sim, payload)
    if g is None:
        return {"ok": False, "live": False, "reason": reason}
    if g.player(state) != -1:
        return {"ok": False, "live": True, "reason": "the step is not simultaneous"}
    la = g.legal(state)
    rng = random.Random(zlib.crc32(C.dump(payload.get("observation", "")).encode("utf-8")))
    acts = list(la)
    if len(acts) > MAX_ACTIONS:
        stride = len(acts) / float(MAX_ACTIONS)
        acts = [acts[int(i * stride)] for i in range(MAX_ACTIONS)]
    if len(acts) < 2:
        return {"ok": True, "live": True, "algo": "single", "actions": [
            {"action": acts[0], "p_eq": 1.0, "p_final": 1.0, "ev_eq": 0.0, "ev_model": 0.0}],
            "value": 0.0, "zero_sum": True, "n_obs": 0, "w": 0.0, "br": acts[0], "model": {}, "counts": {}}
    unknown = bool(payload.get("unknown"))
    grow = max(1.0, float(payload.get("samples_scale") or 1.0))      # default 1.0 = today's caps
    est = estimate_matrices(g, state, acts, C.Clock(MATRIX_SHARE * clock.left()), unknown, rng, grow)
    if est is None:
        return {"ok": False, "live": True, "reason": "the payoff matrix could not be estimated in the time given"}
    A, B, per_cell, exact = est
    n = len(acts)
    zero_sum = max(abs(A[i][j] + B[i][j]) for i in range(n) for j in range(n)) < 0.02 or per_cell >= 1 and \
        max(abs(A[i][j] + B[i][j]) for i in range(n) for j in range(n)) < 0.08
    if zero_sum:
        A = [[(A[i][j] - B[i][j]) / 2.0 for j in range(n)] for i in range(n)]
        B = [[-v for v in row] for row in A]
    x, y = regret_matching(A, B, int(RM_ITERS * max(1.0, float(payload.get("samples_scale") or 1.0))), C.Clock(0.5 * max(0.05, clock.left())))
    ev_eq = [sum(A[i][j] * y[j] for j in range(n)) for i in range(n)]
    value = sum(x[i] * ev_eq[i] for i in range(n))
    hist = opponent_history(g, state, acts, la)
    prior = payload.get("prior_counts") if isinstance(payload.get("prior_counts"), dict) else {}
    model, n_obs, counts, how = (None, 0, [0.0] * n, "none")
    w, br = 0.0, None
    ev_model = [0.0] * n
    if hist is not None:
        model, n_obs, counts, how = model_distribution(acts, hist, prior)
        ev_model = [sum(A[i][j] * model[j] for j in range(n)) for i in range(n)]
        br = max(range(n), key=lambda i: ev_model[i])
        gain = ev_model[br] - sum(x[i] * ev_model[i] for i in range(n))
        spread = max(max(r) for r in A) - min(min(r) for r in A)
        if spread > 1e-9 and gain > MIN_GAIN * spread:
            w = W_MAX * n_obs / (n_obs + W_HALF)
    final = list(x)
    if w > 0:
        final = [(1.0 - w) * x[i] + (w if i == br else 0.0) for i in range(n)]
    actions = [{"action": acts[i], "p_eq": x[i], "p_final": final[i], "ev_eq": ev_eq[i], "ev_model": ev_model[i]}
               for i in range(n)]
    return {"ok": True, "live": True, "algo": "equilibrium", "actions": actions, "value": value,
            "zero_sum": bool(zero_sum), "n_obs": n_obs, "w": w, "br": acts[br] if br is not None else None,
            "model": {acts[i]: model[i] for i in range(n)} if model else {}, "model_how": how,
            "counts": {acts[i]: counts[i] for i in range(n)}, "history_found": hist is not None,
            "samples_per_cell": per_cell, "exact_cells": exact, "cells": n * n, "opp_actions": acts,
            "matrix": [[round(v, 4) for v in row] for row in A] if payload.get("want_replies") else None,
            "opp_eq": {acts[j]: round(y[j], 4) for j in range(n)} if payload.get("want_replies") else None}
