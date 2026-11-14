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

from .. import config, log, memory
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


def save_root(spec, key, values):
    """Add one solved root. Read-modify-write; a lost update only loses a cache entry."""
    try:
        sha = base.sim_sha(spec)
        d = memory.read_json(cache_path(spec), {})
        roots = d.get("roots") if isinstance(d, dict) and d.get("sim") == sha else []
        roots = [r for r in (roots or []) if isinstance(r, list) and r and r[0] != key]
        roots.append([key, values])
        memory.write_json_atomic(cache_path(spec), {"sim": sha, "roots": roots[-CACHE_MAX_ROOTS:]})
    except Exception:
        log.exception("search: cannot save the solved root")


def _name(v):
    return "win" if v >= 0.999 else "loss" if v <= -0.999 else "draw" if abs(v) < 1e-9 else "%.2f" % v


class SearchEngine(base.SimEngine):
    family = "think_ahead"
    job_file = JOB_FILE

    def payload(self, spec, turn_msg, game):
        return {"leaf_k": LEAF_PLAYOUTS, "known": game.known, "cache": load_cache(spec)}

    def build(self, res, spec, turn_msg, game):
        acts = res.get("actions") or []
        if not acts:
            return []
        if isinstance(res.get("known"), dict):
            game.known = res["known"]
        algo, solved = res.get("algo"), bool(res.get("solved"))
        if solved and algo == "alphabeta" and res.get("root_key") and all(a.get("bound") is None for a in acts):
            save_root(spec, res["root_key"], {a["action"]: a["value"] for a in acts})
        out = []
        for a in acts[:MAX_CANDIDATES]:
            v = max(-1.0, min(1.0, float(a["value"])))
            if algo in ("mcts", "alphabeta+mcts"):
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
