"""Think ahead: alpha-beta / MCTS on the verified simulator.

Owner: luckydevs-3. Status: real.

Perfect-information games (and chance games, see below). The search itself is
the sandbox job bot/engines/search_job.py; this file is the parent side: the
payload, the cache of solved roots in /memory, the candidates.

  * iterative deepening alpha-beta with a transposition table when the game has
    no unknown outcomes, the module plays both seats, returns are zero-sum and
    depth 2 is affordable in the slice (measured by the job on this very game);
  * when alpha-beta (exact leaves of value 0, so it proves forced wins and losses inside its
    depth and never guesses) does not solve the game in 60 percent of the slice, MCTS ranks the
    moves that are not proven losses in the rest of it. Random playouts at the leaves of the
    alpha-beta tree were tried and rejected: a minimum over hundreds of noisy leaf estimates is
    almost always the worst value, so every move looked lost;
  * otherwise MCTS (UCT, open loop, one possible world per playout): chance
    games, hidden values, a module that plays the opponent's turn itself, or
    branching too large for alpha-beta;
  * a SOLVED root (every line searched to the end) gives exact values and is
    cached in <variant_dir>/solved.json (tied to the hash of the simulator,
    small, atomic write), so the same position costs nothing next time and in
    our other containers. Solved positions near the root are also kept in the
    engine for the next turns of the same game.

Candidates: every root action with its value for us in [-1, 1], best first.
Alpha-beta values are exact when the note says "solved", otherwise search
estimates (leaf estimates are scaled by 0.9, proven results are not). MCTS values
are mean returns; candidates that are statistically tied with the best (difference
below two standard errors) get the best value, so the ladder's margin lets the model
choose among them, and the others keep their lower mean.
"""

import os
import time

from .. import config, log, memory, resources
from ..interfaces import Candidate
from . import base

# ---------------------------------------------------------------- constants (proposed for bot/config.py)
LEAF_PLAYOUTS = 0            # random playouts per depth-limited leaf; 0 = exact leaves (value 0), see the docstring
CACHE_FILE = "solved.json"
CACHE_MAX_ROOTS = 200
MAX_CANDIDATES = 12
JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_job.py")


def cache_path(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), CACHE_FILE)


def load_cache(spec):
    """{canonical root text: {action: value}} of solved roots of the current simulator."""
    d = memory.read_json(cache_path(spec), {})
    if not isinstance(d, dict) or d.get("sim") != base.sim_sha(spec) or not base.sim_sha(spec):
        return {}
    out = {}
    for item in d.get("roots") or []:
        if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
            out[item[0]] = item[1]
    return out


def save_root(spec, key, values, limit=None):
    """Add one solved root. Read-modify-write; a lost update only loses a cache entry.

    limit: roots kept (newest last); None = what the resource plan allows (CACHE_MAX_ROOTS, more in prep)."""
    try:
        limit = max(CACHE_MAX_ROOTS, int(limit or resources.plan("turn_search").cache_roots))
        sha = base.sim_sha(spec)
        d = memory.read_json(cache_path(spec), {})
        roots = d.get("roots") if isinstance(d, dict) and d.get("sim") == sha else []
        roots = [r for r in (roots or []) if isinstance(r, list) and r and r[0] != key]
        roots.append([key, values])
        out = {"sim": sha, "roots": roots[-limit:]}
        if isinstance(d, dict) and d.get("sim") == sha and d.get("presolved"):
            out["presolved"] = d["presolved"]          # prep's marker (bot/prep/solve.py) survives the match
        memory.write_json_atomic(cache_path(spec), out)
    except Exception:
        log.exception("search: cannot save the solved root")


def _merge_alphabeta(results):
    acts, seen = [], set()
    for r in results:                                    # part order: ties keep the order of the parts
        for a in r.get("actions") or []:
            if a["action"] not in seen:
                seen.add(a["action"])
                acts.append(dict(a))
    acts.sort(key=lambda a: (-a["value"], a.get("bound") is not None, a.get("i", 0)))
    out = dict(results[0])
    exact = [a["value"] for a in acts if a.get("bound") is None]
    out.update(actions=acts, solved=all(bool(r.get("solved")) for r in results),
               depth=min(int(r.get("depth") or 0) for r in results),
               nodes=sum(int(r.get("nodes") or 0) for r in results),
               playouts=sum(int(r.get("playouts") or 0) for r in results),
               best_value=max(a["value"] for a in acts), best_exact=max(exact or [-1.0]))
    known = {}
    for r in results:
        known.update(r.get("known") or {})
    out["known"] = known
    return out


def _merge_mcts(results):
    """Root parallelism: every part ran MCTS over (nearly) the same moves with its own random numbers. Visits add up,
    means are visit-weighted, variances are pooled; a move that any part PROVED lost stays lost."""
    lost, stats, order, idx = {}, {}, [], {}
    for r in results:
        for a in r.get("actions") or []:
            name = a["action"]
            idx.setdefault(name, a.get("i", 0))
            if name not in order:
                order.append(name)
            if a.get("lost_in"):
                lost[name] = a["lost_in"]
            elif a.get("visits"):
                stats.setdefault(name, []).append((int(a["visits"]), float(a["mean"]), float(a["se"])))
    rows = []
    for name, parts in stats.items():
        if name in lost:
            continue
        n = sum(p[0] for p in parts)
        mean = sum(p[0] * p[1] for p in parts) / n
        var = sum(p[0] * (p[2] ** 2 * p[0] + (p[1] - mean) ** 2) for p in parts) / n
        rows.append({"action": name, "mean": mean, "n": n, "se": (var / n) ** 0.5, "i": idx.get(name, 0)})
    if not rows:
        return results[0]
    best = max(rows, key=lambda x: (x["n"], x["mean"], -x["i"]))
    acts = []
    for x in sorted(rows, key=lambda x: (-x["n"], -x["mean"], x["i"])):
        gap = best["mean"] - x["mean"]
        tied = x is best or gap < 2.0 * (x["se"] ** 2 + best["se"] ** 2) ** 0.5
        acts.append({"action": x["action"], "value": best["mean"] if tied else min(x["mean"], best["mean"]),
                     "bound": None, "visits": x["n"], "mean": x["mean"], "se": x["se"],
                     "tied": bool(tied and x is not best)})
    for name in order:
        if name not in stats or name in lost:
            acts.append({"action": name, "value": -1.0 if name in lost else max(-1.0, best["mean"] - 1.0),
                         "bound": None, "visits": 0, "lost_in": lost.get(name)})
    out = dict(results[0])
    ab = [r for r in results if r.get("algo") == "alphabeta+mcts"]
    out.update(algo="alphabeta+mcts" if len(ab) == len(results) else "mcts", actions=acts,
               best_value=best["mean"], playouts=sum(int(r.get("playouts") or 0) for r in results),
               depth=min([int(r.get("depth") or 0) for r in ab] or [0]),
               nodes=sum(int(r.get("nodes") or 0) for r in results), solved=False)
    known = {}
    for r in results:
        known.update(r.get("known") or {})
    out["known"] = known
    return out


def merge_results(results):
    """Merge the ok results of the parts of one search (given in part order). A function of the results only."""
    results = [r for r in results if r.get("actions")]
    if not results:
        return {"ok": False, "live": True, "reason": "no part produced an action"}
    if len(results) == 1:
        return results[0]
    if all(r.get("algo") in ("cache", "single", "tactical") for r in results):
        return results[0]                                # decided before the search: every part says the same
    ab = [r for r in results if r.get("algo") in ("alphabeta", "search-timeout")]
    mc = [r for r in results if r.get("algo") in ("mcts", "alphabeta+mcts")]
    if len(ab) == len(results):
        return _merge_alphabeta(results)
    if len(mc) == len(results):
        return _merge_mcts(results)
    if any(a.get("value", 0) >= 0.999 and a.get("bound") is None for r in ab for a in r["actions"]):
        return _merge_alphabeta(ab)                      # a proven win in one part beats every estimate
    return _merge_mcts(mc) if mc else _merge_alphabeta(ab)


def _name(v):
    return "win" if v >= 0.999 else "loss" if v <= -0.999 else "draw" if abs(v) < 1e-9 else "%.2f" % v


class SearchEngine(base.SimEngine):
    family = "think_ahead"
    job_file = JOB_FILE
    slice_fraction = 0.55
    splittable = True

    def merge(self, results, payload):
        return merge_results(results)

    def decisive(self, result):
        """A proven win (alpha-beta, exact, no bound): the sequential search stops there as well."""
        return result.get("algo") == "alphabeta" and any(
            a.get("value", 0.0) >= 0.999 and a.get("bound") is None for a in result.get("actions") or [])

    def payload(self, spec, turn_msg, game):
        variant = turn_msg.get("variant")
        return {
            "leaf_k": LEAF_PLAYOUTS,
            "known": game.known,
            "cache": load_cache(spec),
            "variant": dict(variant) if isinstance(variant, dict) else {"label": spec.label},
        }

    def build(self, res, spec, turn_msg, game):
        acts = res.get("actions") or []
        if not acts:
            return []
        if isinstance(res.get("known"), dict):
            game.known = res["known"]
        algo, solved = res.get("algo"), bool(res.get("solved"))
        if solved and algo == "alphabeta" and res.get("root_key") and all(a.get("bound") is None for a in acts) \
                and len(acts) == res.get("branching", len(acts)):
            save_root(spec, res["root_key"], {a["action"]: a["value"] for a in acts})
        out = []
        for a in acts[:MAX_CANDIDATES]:
            v = max(-1.0, min(1.0, float(a["value"])))
            if algo == "tactical":
                note = "tactical: %s" % a.get("tactical", "priority safeguard")
            elif algo in ("mcts", "alphabeta+mcts"):
                if not a.get("visits"):
                    note = ("alpha-beta: lost within %d plies" % res.get("depth", 0)) if a.get("lost_in") \
                        else "not visited by the search"
                else:
                    note = "%smcts %d playouts, %d visits, mean %.3f +- %.3f%s" % (
                        "alpha-beta depth %d (no forced result), " % res.get("depth", 0) if algo != "mcts" else "",
                        res.get("playouts", 0), a.get("visits", 0), a.get("mean", v), a.get("se", 0.0),
                        ", tied with best" if a.get("tied") else "")
            elif algo == "cache":
                note = "solved earlier (cache): %s" % _name(v)
            elif algo == "single":
                note = "only legal move"
            elif solved:
                note = "solved: exact value %s%s" % (_name(v), "" if a.get("bound") is None else " or worse (bound)")
            elif v >= 0.999 and a.get("bound") is None:
                note = "forced win within %d plies (proven)" % res.get("depth", 0)
            else:
                note = "alpha-beta depth %d, %d nodes, estimate%s" % (
                    res.get("depth", 0), res.get("nodes", 0), "" if a.get("bound") is None else " (upper bound)")
            out.append(Candidate(a["action"], v, note))
        return out


register_engine = base.register(SearchEngine())
