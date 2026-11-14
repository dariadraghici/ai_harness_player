"""Think ahead: the search as a sandbox JOB. Runs INSIDE the child, next to the simulator.

Owner: luckydevs-3. Status: real.

Never imported by the bot (bot/engines/search.py sends it to a sandbox child with
sandbox.run_job). Standalone: standard library only, plus sim_common.py loaded by path.

run(sim, payload). Payload:
  observation, legal_actions, variant, seat   the real turn (the live check runs first)
  budget_s       the slice: the job stops by itself inside it
  unknown        True when chance or hidden values exist (verify.json: unknown_outcomes)
  both_seats     False when the module plays the other player's turn itself
  margin         the ladder's candidate margin (exact searches ask for exact values only inside it)
  known          {canonical state text: exact value for us}   solved positions from earlier turns
  cache          {canonical root text: {action: value}}       solved roots from /memory
  leaf_k         random playouts per depth-limited leaf (0: value 0)
Result: {"ok": True, "algo", "solved", "depth", "nodes", "playouts", "actions": [{"action", "value",
         "bound", "visits"}], "known": {...}, "root_key"} or {"ok": False, "reason"}.

Two searches, chosen from measured speed and the kind of game:
  alpha-beta  iterative deepening negamax-style search (we maximize our return, the opponent
              minimizes it), transposition table keyed by the canonical state text without the seed,
              previous best move first, a stop at the budget that keeps the last COMPLETED depth.
              Depth-limited leaves are the mean of a few seeded random playouts times 0.9, so an
              estimate is never mistaken for a proven result. SOLVED = a completed depth in which no
              line was cut by the depth limit: the values are exact.
  MCTS        UCT with random playouts, "open loop": tree nodes are action sequences and every
              iteration plays in its own possible world (another state["seed"], contract C11), so
              dice and hidden values are averaged over. Values are mean returns with their
              standard errors; the result says which candidates are statistically tied.
"""

import math
import os
import random
import types
import zlib

BIG = 10 ** 9
INF = float("inf")
EXACT, LOWER, UPPER = 0, 1, 2
TT_CAP = 350000
KNOWN_CAP = 3000
EXPORT_NODES = 2500
UCT_C = 1.0
PLAYOUT_CAP = 400
AB_SHARE = 0.6            # of the slice for alpha-beta when the game is not solved; MCTS gets the rest
VAR_FLOOR = 0.05          # floor of the variance of a mean return, so 1-2 playouts never look certain
LEAF_SCALE = 0.9          # an estimated leaf never reaches the value of a proven result


def _common():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_common.py")
    mod = types.ModuleType("sim_common")
    with open(path, "r", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


C = _common()


class TimeUp(Exception):
    pass


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- alpha-beta
class AlphaBeta:

    def __init__(self, g, clock, known, leaf_k):
        self.g = g
        self.clock = clock
        self.leaf_k = leaf_k
        self.tt = {}
        for k, v in (known or {}).items():
            self.tt[k] = (BIG, EXACT, float(v), False, None)
        self.nodes = 0
        self.playouts = 0

    def leaf(self, s, key):
        if self.leaf_k <= 0:
            return 0.0
        rng = random.Random(zlib.crc32(key.encode("utf-8")))
        total, n = 0.0, 0
        for _ in range(self.leaf_k):
            v, _, _ = self.g.playout(s, rng, PLAYOUT_CAP)
            total += v
            n += 1
        self.playouts += n
        return LEAF_SCALE * total / n

    def store(self, key, depth, flag, val, h, best):
        edepth = depth if h else BIG
        old = self.tt.get(key)
        if old is not None and old[0] > edepth:
            return
        if old is None and len(self.tt) >= TT_CAP and not (edepth == BIG and flag == EXACT):
            return
        self.tt[key] = (edepth, flag, val, h, best)

    def search(self, s, depth, alpha, beta):
        """Value for us of state s searched `depth` plies; (value, horizon_touched)."""
        self.nodes += 1
        if (self.nodes & 7) == 0 and self.clock.up():
            raise TimeUp()
        g = self.g
        key = g.key(s)
        e = self.tt.get(key)
        if e is not None and e[0] >= depth:
            _, flag, val, h, _ = e
            if flag == EXACT or (flag == LOWER and val >= beta) or (flag == UPPER and val <= alpha):
                return val, h
        if g.terminal(s):
            v = g.value(s)
            self.tt[key] = (BIG, EXACT, v, False, None)
            return v, False
        if depth <= 0:
            return self.leaf(s, key), True
        la = g.legal(s)
        if not la:
            v = g.value(s)
            self.tt[key] = (BIG, EXACT, v, False, None)
            return v, False
        cp = g.player(s)
        if cp not in (0, 1):
            raise Unsupported("a simultaneous step inside the tree")
        prev = e[4] if e is not None else None
        if prev in la:
            la = [prev] + [a for a in la if a != prev]
        maximize = cp == g.me
        a0, b0 = alpha, beta
        best_v, best_a, h_any = (-INF if maximize else INF), None, False
        for a in la:
            v, h = self.search(g.apply(s, a), depth - 1, alpha, beta)
            h_any = h_any or h
            if maximize:
                if v > best_v:
                    best_v, best_a = v, a
                if best_v > alpha:
                    alpha = best_v
            else:
                if v < best_v:
                    best_v, best_a = v, a
                if best_v < beta:
                    beta = best_v
            if alpha >= beta:
                break
        flag = UPPER if best_v <= a0 else LOWER if best_v >= b0 else EXACT
        self.store(key, depth, flag, best_v, h_any, best_a)
        return best_v, h_any

    def root_pass(self, state, order, depth, margin, full):
        """One iteration at `depth`. Returns {action: (value, exact, horizon)}; raises TimeUp."""
        g = self.g
        out = {}
        best = -INF
        for a in order:
            child = g.apply(state, a)
            alpha = -INF if (full or best == -INF) else best - margin - 1e-6
            v, h = self.search(child, depth - 1, alpha, INF)
            exact = v > alpha
            out[a] = (v, exact, h)
            if exact and v > best:
                best = v
        return out


def alpha_beta(g, state, la, clock, payload):
    margin = float(payload.get("margin") or 0.05)
    ab = AlphaBeta(g, clock, payload.get("known"), int(payload.get("leaf_k") or 0))
    order = list(la)
    done, depth, solved = None, 1, False
    while depth <= 400:
        try:
            res = ab.root_pass(state, order, depth, margin, False)
        except TimeUp:
            break
        done = (depth, res)
        horizon = any(h for (_, _, h) in res.values())
        ranked = sorted(res, key=lambda a: -res[a][0])
        order = ranked
        best_v = res[ranked[0]][0]
        if not horizon:
            solved = True
            break
        if best_v >= 1.0 - 1e-9:        # the largest value there is: deeper search cannot improve it
            break
        depth += 1
    if done is None:
        return None
    depth, res = done
    if solved and not clock.up():
        try:                              # exact values for the moves that only got a bound
            for a in [a for a in order if not res[a][1]]:
                v, h = ab.search(g.apply(state, a), depth - 1, -INF, INF)
                res[a] = (v, True, h)
        except TimeUp:
            pass
    actions = []
    for a in order:
        v, exact, h = res[a]
        actions.append({"action": a, "value": v, "bound": None if exact else "<="})
    best_exact = max([x["value"] for x in actions if x["bound"] is None] or [-1.0])
    best_value = max(x["value"] for x in actions)
    out = {"ok": True, "algo": "alphabeta", "solved": bool(solved), "depth": depth, "nodes": ab.nodes,
           "playouts": ab.playouts, "actions": actions, "best_value": best_value, "best_exact": best_exact}
    out["_ab"] = ab
    return out


def export_known(g, ab, state, clock):
    """Exact values of positions below the root (breadth first, capped), for the next turns of
    this game: the next root is a grandchild of this one."""
    known = {}
    try:
        frontier, visited = [state], 0
        while frontier and len(known) < KNOWN_CAP and visited < EXPORT_NODES:
            nxt = []
            for s in frontier:
                if clock.up():
                    return known
                if g.terminal(s):
                    continue
                for a in g.legal(s):
                    child = g.apply(s, a)
                    visited += 1
                    ck = g.key(child)
                    e = ab.tt.get(ck)
                    if e and e[0] == BIG and e[1] == EXACT:
                        known[ck] = e[2]
                    nxt.append(child)
                    if len(known) >= KNOWN_CAP or visited >= EXPORT_NODES:
                        return known
            frontier = nxt
    except Exception:
        pass
    return known


# ---------------------------------------------------------------- MCTS
class Node:
    __slots__ = ("n", "w0", "w1", "q", "kids", "untried", "mover")

    def __init__(self):
        self.n = 0
        self.w0 = 0.0
        self.w1 = 0.0
        self.q = 0.0
        self.kids = {}
        self.untried = None
        self.mover = None


def mcts(g, state, la, clock, payload, rng, cap=PLAYOUT_CAP):
    unknown = bool(payload.get("unknown"))
    root = Node()
    root.untried = list(la)
    rng.shuffle(root.untried)
    root.mover = g.me
    iters, failed = 0, 0
    while not clock.up():
        s = C.seeded(state, rng.randrange(C.SEED_MAX)) if unknown else state
        node, path = root, [root]
        try:
            while True:
                if node.untried is None:
                    if g.terminal(s):
                        break
                    node.untried = list(g.legal(s))
                    rng.shuffle(node.untried)
                    node.mover = g.player(s)
                    if node.mover not in (0, 1):
                        raise Unsupported("a simultaneous step inside the tree")
                if node.untried:
                    a = node.untried.pop()
                    child = Node()
                    node.kids[a] = child
                    s = g.apply(s, a)
                    node = child
                    path.append(node)
                    break
                if not node.kids:
                    break
                p, ln = node.mover, math.log(max(node.n, 1))
                best_a, best_u = None, -INF
                for a, c in node.kids.items():
                    if c.n == 0:
                        u = INF
                    else:
                        mean = (c.w0 if p == 0 else c.w1) / c.n
                        u = mean + UCT_C * math.sqrt(ln / c.n)
                    if u > best_u:
                        best_a, best_u = a, u
                s = g.apply(s, best_a)
                node = node.kids[best_a]
                path.append(node)
            steps = 0
            while steps < cap and not g.terminal(s):
                if (steps & 7) == 7 and clock.up():
                    raise TimeUp()              # a slow simulator: drop this playout, the slice is over
                la2 = g.legal(s)
                if not la2:
                    break
                s = g.apply(s, g.random_action(s, la2, rng))
                steps += 1
            r0, r1 = g.returns(s) if (g.terminal(s) or steps < cap) else (0.0, 0.0)
        except (Unsupported, TimeUp):
            if clock.up() and iters > 0:
                break
            raise
        except ValueError:
            failed += 1                     # an action that was legal in another world only
            if failed > 50 and failed > iters:
                raise Unsupported("too many actions are illegal in other worlds")
            continue
        rme = r0 if g.me == 0 else r1
        for nd in path:
            nd.n += 1
            nd.w0 += r0
            nd.w1 += r1
            nd.q += rme * rme
        iters += 1
    kids = [(a, c) for a, c in root.kids.items() if c.n > 0]
    if not kids:
        return None
    stats = []
    for a, c in kids:
        mean = (c.w0 if g.me == 0 else c.w1) / c.n
        var = max(VAR_FLOOR, c.q / c.n - mean * mean)       # few visits: never claim a certainty
        stats.append({"action": a, "mean": mean, "n": c.n, "se": math.sqrt(var / c.n)})
    best = max(stats, key=lambda x: (x["n"], x["mean"]))
    actions = []
    for x in sorted(stats, key=lambda x: (-x["n"], -x["mean"])):
        gap = best["mean"] - x["mean"]
        tied = x is best or gap < 2.0 * math.sqrt(x["se"] ** 2 + best["se"] ** 2)
        value = best["mean"] if tied else min(x["mean"], best["mean"])
        actions.append({"action": x["action"], "value": value, "bound": None, "visits": x["n"],
                        "mean": x["mean"], "se": x["se"], "tied": bool(tied and x is not best)})
    return {"ok": True, "algo": "mcts", "solved": False, "depth": 0, "nodes": 0, "playouts": iters,
            "actions": actions, "best_value": best["mean"], "unvisited": len(la) - len(kids)}


def refine(g, state, la, clock, payload, rng, ab_out):
    """Alpha-beta with exact leaves (value 0) proves wins and losses inside its depth but cannot rank
    the other moves; MCTS ranks them in the rest of the slice. Proven losers are left out of the
    tree and listed last with their value."""
    lost = {x["action"]: x for x in ab_out["actions"] if x["value"] <= -1.0 + 1e-9}
    allowed = [a for a in la if a not in lost] or list(la)
    m = mcts(g, state, allowed, C.Clock(max(0.05, clock.left())), payload, rng)
    if m is None:
        return ab_out
    seen = set(x["action"] for x in m["actions"])
    for a in la:
        if a not in seen:
            x = lost.get(a)
            m["actions"].append({"action": a, "value": -1.0 if x else max(-1.0, m["best_value"] - 1.0),
                                 "bound": None, "visits": 0, "lost_in": ab_out["depth"] if x else None})
    m["algo"] = "alphabeta+mcts"
    m["depth"] = ab_out["depth"]
    m["nodes"] = ab_out["nodes"]
    m["_ab"] = ab_out.get("_ab")
    return m


# ---------------------------------------------------------------- choosing
def calibrate(g, state, la, rng, limit_s=0.1):
    """Cost of one node (seconds), average branching one level down, and whether returns are zero-sum."""
    import time
    t0 = time.perf_counter()
    branching, n = 0, 0
    for a in la[:40]:
        child = g.apply(state, a)
        n += 1
        if not g.terminal(child):
            branching += len(g.legal(child))
        else:
            g.value(child)
    per_node = (time.perf_counter() - t0) / max(1, n)
    zero_sum = True
    for _ in range(3):
        if time.perf_counter() - t0 > limit_s:
            break                                   # a slow simulator: no time for the zero-sum check
        _, _, final = g.playout(state, rng, PLAYOUT_CAP)
        if final is not None:
            r0, r1 = g.returns(final)
            if abs(r0 + r1) > 1e-6:
                zero_sum = False
    return per_node, branching / float(max(1, n)), zero_sum


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator loaded"}
    clock = C.Clock(payload.get("budget_s") or 2.0)
    g, state, reason = C.resolve(sim, payload)
    if g is None:
        return {"ok": False, "live": False, "reason": reason}
    la = g.legal(state)
    rng = random.Random(zlib.crc32(C.dump(payload.get("observation", "")).encode("utf-8")))
    try:
        root_key = g.key(state)
        cached = (payload.get("cache") or {}).get(root_key)
        if isinstance(cached, dict) and set(cached) == set(la):
            acts = sorted(({"action": a, "value": float(v), "bound": None} for a, v in cached.items()),
                          key=lambda x: -x["value"])
            return {"ok": True, "live": True, "algo": "cache", "solved": True, "depth": 0, "nodes": 0,
                    "playouts": 0, "actions": acts, "best_value": acts[0]["value"], "root_key": root_key}
        if len(la) == 1:
            return {"ok": True, "live": True, "algo": "single", "solved": False, "depth": 0, "nodes": 0,
                    "playouts": 0, "actions": [{"action": la[0], "value": 0.0, "bound": None}],
                    "best_value": 0.0, "root_key": root_key}
        per_node, branching, zero_sum = calibrate(g, state, la, rng, 0.15 * clock.left())
        use_mcts = bool(payload.get("unknown")) or payload.get("both_seats") is False or not zero_sum
        if not use_mcts:
            leaf_cost = per_node * (1 + 4 * int(payload.get("leaf_k") or 0))
            depth2 = len(la) * max(branching, 1.0) * leaf_cost
            use_mcts = depth2 > 0.5 * clock.left()
        out = None
        if not use_mcts:
            out = alpha_beta(g, state, la, C.Clock(AB_SHARE * clock.left()), payload)
            if out is not None and not out["solved"] and out["best_value"] < 1.0 - 1e-9:
                out = refine(g, state, la, clock, payload, rng, out)
        if out is None:
            out = mcts(g, state, la, C.Clock(max(0.05, clock.left())), payload, rng)
        if out is None:
            return {"ok": False, "live": True, "reason": "the search produced no result within the budget"}
        ab = out.pop("_ab", None)
        if ab is not None:
            out["known"] = export_known(g, ab, state, C.Clock(min(0.1, max(0.0, 0.5 * clock.left()))))
        out["live"] = True
        out["root_key"] = root_key
        out["zero_sum"] = zero_sum
        out["branching"] = len(la)
        return out
    except Unsupported as e:
        return {"ok": False, "live": True, "reason": str(e)}
