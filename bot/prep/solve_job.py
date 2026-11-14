"""Pre-solving as a sandbox JOB (runs INSIDE the child next to the verified simulator).

Owner: luckydevs-3. Status: real. Standalone: standard library only; sim_common.py and search_job.py of
bot/engines are loaded by path. Never imported by the bot (bot/prep/solve.py sends it to a child).

Prep has 600 seconds of free CPU. The match gives the engine 3 seconds per move, so a position that is
solved here costs 0 ms and 0 tokens there. This job starts from the position of the sample turn and walks
forward through ALL legal moves of both players for a few plies (breadth first, positions deduplicated by the
canonical state text of sim_common.Game.key). Every position where it is OUR move is a root: it is searched by
the very alpha-beta of search_job.py with a generous budget, and a root that is solved to the end with exact
values for every move is returned as {"action": value}.

Nothing here is a fact about the sample deal: a root is keyed by its canonical state text, so it is used only
when a real game reaches exactly that position, and the cache is tied to the hash of the simulator.

payload: observation, legal_actions (optional), variant, seat, budget_s (whole job), root_s (per root),
         max_plies, max_nodes, tt_entries, margin, unknown, both_seats, part, parts, as_seat.
Result: {"ok": True, "live": True, "roots": [[key, {action: value}], ...], "tried", "solved", "positions",
         "plies"} or {"ok": False, "live": ..., "reason"}.
"""

import os
import random
import types
import zlib


def _load(name):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines", name)
    mod = types.ModuleType(name[:-3])
    mod.__file__ = path
    with open(path, "r", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


GIVE_UP_AFTER = 12            # roots tried without ONE solved: stop (the first roots are the smallest trees)
GIVE_UP_SHARE = 0.3           # ... or when this share of the budget is gone without one

S = _load("search_job.py")
C = S.C


def positions(g, start, max_plies, max_nodes, clock):
    """[(ply, state)] of the positions where it is our move, breadth first, deduplicated."""
    seen = {g.key(start)}
    frontier, roots, nodes = [start], [], 0
    for ply in range(max_plies + 1):
        nxt = []
        for s in frontier:
            if clock.up() or nodes > max_nodes:
                return roots
            if g.terminal(s):
                continue
            cp = g.player(s)
            if cp == g.me:
                roots.append((ply, s))
            if ply == max_plies or cp not in (0, 1):
                continue                 # a simultaneous step has no place in a perfect-information tree
            for a in g.legal(s):
                nodes += 1
                c = g.apply(s, a)
                k = g.key(c)
                if k not in seen:
                    seen.add(k)
                    nxt.append(c)
        frontier = nxt
        if not frontier:
            break
    return roots


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator loaded"}
    clock = C.Clock(payload.get("budget_s") or 10.0)
    g, state, reason = C.resolve(sim, payload)
    if g is None:
        return {"ok": False, "live": False, "reason": reason}
    if payload.get("unknown") or payload.get("both_seats") is False:
        return {"ok": True, "live": True, "roots": [], "tried": 0, "solved": 0, "positions": 0, "plies": 0,
                "reason": "not an alpha-beta game (unknown outcomes or one seat only)"}
    if int(payload.get("as_seat") or 0) == 1 and g.me == 0:
        state = dict(state)
        state["me"] = 1
        g = C.Game(sim, 1)
    la = g.legal(state)
    rng = random.Random(zlib.crc32(C.dump(payload.get("observation", "")).encode("utf-8")))
    _, _, zero_sum = S.calibrate(g, state, la, rng, 0.1)
    if not zero_sum:
        return {"ok": True, "live": True, "roots": [], "tried": 0, "solved": 0, "positions": 0, "plies": 0,
                "reason": "returns are not zero-sum"}
    max_plies = max(0, int(payload.get("max_plies") or 4))
    roots = positions(g, state, max_plies, int(payload.get("max_nodes") or 20000), C.Clock(0.3 * clock.left()))
    try:
        parts = max(1, int(payload.get("parts") or 1))
        part = int(payload.get("part") or 0)
        part = part if 0 <= part < parts else 0
    except (TypeError, ValueError):
        part, parts = 0, 1
    mine = roots[part::parts]
    out, tried = [], 0
    root_s = float(payload.get("root_s") or 4.0)
    sp = {"margin": float(payload.get("margin") or 0.05), "known": None, "leaf_k": 0,
          "tt_entries": int(payload.get("tt_entries") or S.TT_CAP)}
    for ply, s in mine:
        if clock.up():
            break
        if not out and (tried >= GIVE_UP_AFTER or clock.used() > GIVE_UP_SHARE * (clock.used() + clock.left())):
            break                        # a game this size is not solvable in the time given: do not burn the window
        try:
            la_s = g.legal(s)
            if len(la_s) < 2:
                continue
            tried += 1
            res = S.alpha_beta(g, s, list(la_s), C.Clock(min(root_s, clock.left())), sp)
        except S.Unsupported:
            continue
        except Exception:
            continue
        if res is None or not res.get("solved") or any(a.get("bound") is not None for a in res["actions"]):
            continue
        if set(a["action"] for a in res["actions"]) != set(la_s):
            continue
        out.append([g.key(s), dict((a["action"], float(a["value"])) for a in res["actions"])])
    return {"ok": True, "live": True, "roots": out, "tried": tried, "solved": len(out), "positions": len(roots),
            "plies": max_plies}
