"""Helpers shared by the engine jobs. Runs INSIDE a sandbox child, next to a generated simulator.

Owner: luckydevs-3. Status: real.

Never imported by the bot. The job files (search_job.py, guess_job.py,
simultaneous_job.py, bluff_job.py) load this file BY PATH, because a job
cannot import from `bot` and the child runs with `python -I -S` (no import
path):

    def _common():
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_common.py")
        mod = types.ModuleType("sim_common")
        exec(compile(open(path, encoding="utf-8").read(), path, "exec"), mod.__dict__)
        return mod

Standalone: standard library only. What it gives a job:

  Game(sim, me)         thin wrapper around the six simulator functions
  open_game(sim, payload)   seat resolution + the live check of the real turn
  seeded(state, k)      the same state in another possible world (contract C11)
  hidden_paths / world_of   which parts of the state are hidden values (None in our view)
  Clock(budget_s)       a time budget the job keeps by itself
"""

import copy
import json
import random
import re
import time

SEED_MAX = 2 ** 31


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))


def err(exc):
    text = str(exc)
    return "%s: %s" % (type(exc).__name__, text[:160]) if text else type(exc).__name__


class Clock:
    """A budget in seconds measured from now."""

    def __init__(self, budget_s):
        self.t0 = time.perf_counter()
        self.end = self.t0 + max(0.0, float(budget_s))

    def left(self):
        return self.end - time.perf_counter()

    def used(self):
        return time.perf_counter() - self.t0

    def up(self):
        return time.perf_counter() >= self.end


class Game:
    """The simulator seen from our seat. `me` is state["me"] of the parsed state."""

    def __init__(self, sim, me):
        self.sim = sim
        self.me = me
        self.calls = 0

    def legal(self, s):
        self.calls += 1
        return self.sim.legal_actions(s)

    def apply(self, s, action):
        self.calls += 1
        return self.sim.apply_action(s, action)

    def terminal(self, s):
        return self.sim.is_terminal(s) is True

    def player(self, s):
        return self.sim.current_player(s)

    def returns(self, s):
        r = self.sim.returns(s)
        return float(r[0]), float(r[1])

    def value(self, s):
        """Our return at a terminal state, in [-1, 1]."""
        return self.returns(s)[self.me]

    def key(self, s):
        """Canonical text of a state, without the seed (two states that differ only in the seed are
        the same position)."""
        if isinstance(s, dict) and "seed" in s:
            s = {k: v for k, v in s.items() if k != "seed"}
        return dump(s)

    def playout(self, s, rng, cap=400):
        """Random play to the end. Returns (our value, steps, final state); (0.0, cap, None) when the
        game does not end within cap steps. Simultaneous steps take two random actions (ours first
        for seat 0, the other order for seat 1: both players draw from the same list)."""
        steps = 0
        while steps < cap:
            if self.terminal(s):
                return self.value(s), steps, s
            la = self.legal(s)
            if not la:
                return self.value(s), steps, s
            s = self.apply(s, self.random_action(s, la, rng))
            steps += 1
        return 0.0, steps, None

    def random_action(self, s, la, rng):
        if self.player(s) == -1:
            pair = [la[rng.randrange(len(la))], la[rng.randrange(len(la))]]
            return pair if self.me == 0 else [pair[1], pair[0]]
        return la[rng.randrange(len(la))]


def seeded(state, k):
    """The same state with another state["seed"]: another possible world (contract C11)."""
    s = dict(state)
    s["seed"] = int(k)
    return s


def resolve(sim, payload):
    """Parse the real observation and run the live check. Returns (game, state, reason).

    game/state are None and reason says why when the simulator cannot be trusted for this turn:
    the observation does not parse, the move is not ours, or the legal actions differ from the
    arena's list (same set; a list of at most 50 actions is always sent when it is that short).
    The seat comes from the payload when it has one; otherwise both seats are tried and the
    observation must decide (both parses agree on state["me"])."""
    obs = payload.get("observation", "")
    variant = payload.get("variant") if isinstance(payload.get("variant"), dict) else {}
    seats = [payload["seat"]] if payload.get("seat") in (0, 1) else [0, 1]
    parsed, problems = [], []
    for seat in seats:
        try:
            st = sim.parse_observation(obs, variant, seat)
            if not isinstance(st, dict) or st.get("me") not in (0, 1):
                raise ValueError("no state[\"me\"]")
            parsed.append((seat, st))
        except Exception as e:
            problems.append("seat %s: %s" % (seat, err(e)))
    if not parsed:
        return None, None, "parse_observation failed (%s)" % "; ".join(problems)
    mes = set(st["me"] for _, st in parsed)
    if len(mes) > 1:
        return None, None, "the observation does not say which seat we are"
    seat, state = parsed[0]
    me = state["me"]
    g = Game(sim, me)
    try:
        cp = g.player(state)
        la = g.legal(state)
        if g.terminal(state):
            return None, None, "the simulator says the parsed state is terminal"
    except Exception as e:
        return None, None, "live check raised %s" % err(e)
    if cp not in (me, -1):
        return None, None, "current_player is %s but we are seat %s" % (cp, me)
    if not isinstance(la, list) or not la or not all(isinstance(a, str) for a in la):
        return None, None, "legal_actions is empty or not a list of strings"
    want = payload.get("legal_actions")
    if isinstance(want, list) and want:
        if sorted(str(a) for a in want) != sorted(la):
            missing = [a for a in want if a not in la][:4]
            extra = [a for a in la if a not in want][:4]
            return None, None, "legal actions differ from the arena's (missing %s, extra %s)" % (missing, extra)
    return g, state, ""


# ---------------------------------------------------------------- hidden values
def walk(value, prefix=()):
    """Yield (path, leaf) for every dict key and list index below value."""
    yield prefix, value
    if isinstance(value, dict):
        for k in value:
            for item in walk(value[k], prefix + (k,)):
                yield item
    elif isinstance(value, list):
        for i, v in enumerate(value):
            for item in walk(v, prefix + (i,)):
                yield item


def get_path(value, path):
    for p in path:
        value = value[p]
    return value


def hidden_paths(state):
    """Paths of the parts of our view that are unknown (None) at the top two levels."""
    out = []
    if not isinstance(state, dict):
        return out
    for k, v in state.items():
        if v is None:
            out.append((k,))
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if v2 is None:
                    out.append((k, k2))
        elif isinstance(v, list):
            for i, v2 in enumerate(v):
                if v2 is None and len(v) <= 8:
                    out.append((k, i))
    return out


def world_of(before, after, paths):
    """The values at the hidden paths in `after` (a state a simulator drew a world into), as a
    comparable text; "" when none of them has been drawn."""
    vals = []
    for p in paths:
        try:
            vals.append(get_path(after, p))
        except (KeyError, IndexError, TypeError):
            vals.append(None)
    if all(v is None for v in vals):
        return ""
    return dump(vals)


def sample_worlds(g, state, legal, clock, max_worlds, max_seeds, rng):
    """Sample the worlds a simulator can draw. Applies one legal action under many seeds and
    reads which hidden parts of the state were filled in.

    Returns (worlds, paths, exact): worlds is a list of states (the state after the probe action,
    which is only used for its drawn hidden values: callers take the values at `paths`), paths the
    hidden paths, exact True when no new value appeared in the last stretch of seeds (the set is
    then complete for practical purposes). Empty when nothing hidden gets drawn."""
    paths = hidden_paths(state)
    if not paths:
        return [], paths, False
    probe = legal[0]
    seen, worlds = {}, []
    quiet = 0
    for i in range(max_seeds):
        if clock.up():
            break
        k = rng.randrange(SEED_MAX)
        try:
            after = g.apply(seeded(state, k), probe)
        except Exception:
            break
        w = world_of(state, after, paths)
        if not w:
            return [], paths, False
        if w in seen:
            quiet += 1
        else:
            seen[w] = True
            worlds.append((k, after))
            quiet = 0
            if len(worlds) >= max_worlds:
                return worlds, paths, False
        if len(worlds) > 0 and quiet >= max(60, 3 * len(worlds)):
            return worlds, paths, True
    return worlds, paths, False


# ---------------------------------------------------------------- worlds as values of the hidden paths
TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")


def set_path(state, path, value):
    cur = state
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = copy.deepcopy(value)


def scalars(value, out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for k in sorted(value, key=str):
            scalars(value[k], out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            scalars(v, out)
    elif value is not None and not isinstance(value, bool):
        out.append(str(value))
    return out


def names_value(action, tokens_of_world):
    """Does the action text contain every scalar of the hidden value as a whole token?"""
    if not tokens_of_world:
        return False
    have = TOKEN.findall(action)
    pool = list(have)
    for t in tokens_of_world:
        if t in pool:
            pool.remove(t)
        else:
            return False
    return True


def view_key(g, ns, paths):
    """What our view shows of the state ns: without the hidden parts and the seed."""
    s = copy.deepcopy(ns) if isinstance(ns, dict) else ns
    if isinstance(s, dict):
        s.pop("seed", None)
        for p in paths:
            try:
                set_path(s, p, None)
            except (KeyError, IndexError, TypeError):
                pass
    return dump(s)


def sample_hypotheses(g, state, la, clock, rng, max_worlds=2000, max_seeds=40000):
    """Returns (hyps, paths, exact): hyps is a list of value lists, one per world, at the hidden paths."""
    worlds, paths, exact = sample_worlds(g, state, la, clock, max_worlds, max_seeds, rng)
    hyps = []
    for _, after in worlds:
        vals = []
        for p in paths:
            try:
                vals.append(get_path(after, p))
            except (KeyError, IndexError, TypeError):
                vals.append(None)
        hyps.append(vals)
    return hyps, paths, exact


def world_state(state, paths, vals):
    s = copy.deepcopy(state)
    for p, v in zip(paths, vals):
        set_path(s, p, v)
    return s
