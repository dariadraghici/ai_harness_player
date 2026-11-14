"""Engine interface, registry, and the helper every simulator engine uses.

Owner: luckydevs-3. Status: real.

The ladder calls get_engine(profile.family) and then
engine.candidates(specialist, turn_msg, deadline). An engine that is not ready
raises NotImplementedError or returns []; the ladder treats both as
"L3 not available" and goes down to L2.

SimEngine is the base of the four engines that run on a verified simulator
(think_ahead, guessing, read_opponent, bluff). Its candidates() does, in this
order, and NEVER raises:

  1. the switch BOT_L3 and the verified simulator file are checked;
  2. "off for this game" (a failed live check earlier in this game) ends it;
  3. the engine's job file (a standalone program, see sim_common.py) runs in a
     fresh sandbox child with a time SLICE (a fraction of the move clock, never
     "whatever is left"); the first thing every job does is the LIVE CHECK: parse
     THIS turn's observation and compare the legal actions with the arena's list;
  4. a failed live check switches the engine off for the rest of this game and is
     counted per variant in <variant_dir>/live.json (prep/verify.py note_live), so that
     a simulator that keeps failing stops being used by all our containers
     (verify.is_done reads it);
  5. the job's answer becomes list[Candidate].

No child is alive after candidates() returns. Opponent chat, and anything
derived from it, never goes into a job payload: the model sees the candidates
AND the chat and makes the one decision.
"""

import os
import threading
import time
from typing import Dict, List, Optional

from .. import config, log, memory
from ..interfaces import Candidate, Engine, VariantSpec  # noqa: F401  (re-exported)

# ---------------------------------------------------------------- constants (proposed for bot/config.py)
SLICE_FRACTION = 0.15            # of the move clock: 3 s of 20
SLICE_MIN_S = 0.4                # below this the engine does not start
SLICE_BACKSTOP_S = 1.5           # the parent kills the child this long after the slice
# Families on by default when BOT_L3 is not set. Decided by measurement (see the hand-over): think_ahead and
# read_opponent ran in real practice matches on their games (tic_tac_toe, nim_deal, pig; rps_twist), guessing is
# measured on the real guess_number simulators; bluff has no practice game, so it is off until it has one.
L3_DEFAULT = "think_ahead,guessing,read_opponent"
L3_ALL = ("think_ahead", "guessing", "read_opponent", "bluff")
JOB_ERRORS_OFF = 2               # simulator exceptions in jobs before "off for this game"


class NotAvailable(Exception):
    """Raised by an engine that cannot help for this turn."""


def enabled(family):
    """BOT_L3: unset = the default families; 0/off/none = none; a comma list = only those; all = all."""
    raw = os.environ.get("BOT_L3")
    if raw is None or not raw.strip():
        raw = L3_DEFAULT
    raw = raw.strip().lower()
    if raw in ("0", "off", "none", "false", "no"):
        return False
    if raw == "all":
        return family in L3_ALL
    return family in [x.strip() for x in raw.split(",")]


class BaseEngine:
    family = "unknown"

    def candidates(self, specialist: VariantSpec, turn_msg: dict, deadline: float) -> List[Candidate]:
        raise NotImplementedError("%s engine not implemented" % self.family)


# ---------------------------------------------------------------- the helper
class _Game:
    __slots__ = ("key", "last_turn", "off", "reason", "counted", "errors", "known", "extra")

    def __init__(self, key):
        self.key = key
        self.last_turn = 0
        self.off = False
        self.reason = ""
        self.counted = False
        self.errors = 0
        self.known = {}
        self.extra = {}


class SimEngine(BaseEngine):
    """Base of the engines that run a job on a verified simulator. Subclasses set `family`,
    `job_file` and implement payload() and build()."""

    job_file = ""

    def __init__(self):
        self._games: Dict[tuple, _Game] = {}
        self._lock = threading.Lock()

    # ---- to be provided by the engines
    def payload(self, spec, turn_msg, game: _Game) -> dict:
        """The job payload beyond the common fields (observation, legal_actions, variant, budget_s)."""
        return {}

    def build(self, result: dict, spec, turn_msg, game: _Game) -> List[Candidate]:
        raise NotImplementedError

    # ---- bookkeeping
    def game_for(self, spec, turn_msg) -> _Game:
        key = (spec.game_id, spec.rules_hash)
        gi, turn = turn_msg.get("game_index"), turn_msg.get("turn")
        with self._lock:
            g = self._games.get(key)
            new = g is None or g.key != gi
            if not new and isinstance(turn, int) and isinstance(g.last_turn, int) and turn < g.last_turn:
                new = True                    # the turn counter went back: a new game
            if new:
                if g is not None:
                    try:
                        self.game_ended(key, g)
                    except Exception:
                        log.exception("game_ended failed")
                g = self._games[key] = _Game(gi)
            if isinstance(turn, int):
                g.last_turn = turn
            if len(self._games) > 16:
                for k in list(self._games)[:-8]:
                    self._games.pop(k, None)
            return g

    def game_ended(self, key, game: _Game):
        """Called (under the lock) when a new game replaces this one. For engines that carry something over."""

    def reset(self):
        with self._lock:
            self._games.clear()

    def _count(self, spec, game, ok):
        if game.counted:
            return
        game.counted = True
        from ..prep import verify
        verify.note_live(spec, ok, sim_sha(spec))

    # ---- the one entry point
    def candidates(self, specialist, turn_msg, deadline):
        t0 = time.monotonic()
        try:
            return self._candidates(specialist, turn_msg, deadline, t0)
        except Exception:
            log.exception("%s engine failed, no candidates this turn" % self.family)
            return []

    def _candidates(self, spec, turn_msg, deadline, t0):
        from ..prep import sandbox, verify          # late: prep imports chat, which imports the engines
        if not enabled(self.family):
            return []
        if spec is None or not spec.verified:
            return []
        path = verify.simulator_file(spec)
        if not os.path.isfile(path):
            log.warning("%s engine: simulator file missing: %s" % (self.family, path))
            return []
        game = self.game_for(spec, turn_msg)
        if game.off:
            return []
        if verify.live_disabled(spec, sim_sha(spec)):
            game.off, game.reason = True, "switched off for this variant (live check failed in several games)"
            log.warning("%s engine: %s" % (self.family, game.reason))
            return []
        clock = turn_msg.get("seconds")
        clock = float(clock) if isinstance(clock, (int, float)) and clock > 0 else float(config.TURN_CLOCK_S)
        budget = min(SLICE_FRACTION * clock, deadline - time.monotonic() - 0.4)
        if budget < SLICE_MIN_S:
            log.info("%s engine: no time (%.2fs)" % (self.family, budget))
            return []
        legal = turn_msg.get("legal_actions")
        payload = {"observation": str(turn_msg.get("observation") or ""),
                   "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None,
                   "variant": {"label": spec.label}, "budget_s": round(budget, 3),
                   "margin": config.CANDIDATE_MARGIN, "family": self.family}
        try:
            from .bg_compute import load_policy
            payload["background"] = load_policy(spec)
        except Exception:
            payload["background"] = {}
        seat = turn_msg.get("seat") if turn_msg.get("seat") in (0, 1) else getattr(spec, "seat", None)
        if seat in (0, 1):
            payload["seat"] = seat           # else the job tries both seats and needs the observation to decide
        facts = simulator_facts(spec)
        payload.update(unknown=facts["unknown_outcomes"], both_seats=facts["both_seats"],
                       playouts_per_s=facts["playouts_per_s"], steps_per_game=facts["steps_per_game"],
                       legal_complete=facts["legal_complete"])
        payload.update(self.payload(spec, turn_msg, game))
        timeout = min(budget + SLICE_BACKSTOP_S, max(0.5, deadline - time.monotonic()))
        out = sandbox.run_job(path, self.job_file, payload, timeout, cpu_s=int(budget * 3 + 20))
        if not out.get("ok"):
            game.errors += 1
            log.warning("%s engine: job failed (%s): %s" % (self.family, out.get("kind"), str(out.get("error"))[:200]))
            if game.errors >= JOB_ERRORS_OFF or out.get("kind") == "error":
                game.off, game.reason = True, "the simulator failed inside the engine job"
                self._count(spec, game, False)
            return []
        res = out.get("result")
        if not isinstance(res, dict):
            return []
        if res.get("live") is False:
            game.off, game.reason = True, "live check failed: %s" % res.get("reason")
            log.warning("%s engine OFF for this game: %s" % (self.family, game.reason))
            self._count(spec, game, False)
            return []
        if not res.get("ok"):
            log.info("%s engine: no result (%s)" % (self.family, res.get("reason")))
            return []
        self._count(spec, game, True)
        cands = self.build(res, spec, turn_msg, game) or []
        log.info("%s engine: %d candidates in %.2fs (%s)" % (
            self.family, len(cands), time.monotonic() - t0, res.get("algo", "")))
        return cands


class MixedEngine(SimEngine):
    """Base of the engines that play a MIXED strategy (read_opponent, bluff).

    A mixed strategy that always plays its most likely action is a pure strategy and can be read.
    So the job returns probabilities and ONE action is drawn here, with a generator seeded from a
    per-process secret salt, the game and the turn: never from the chat, not predictable by the
    opponent. The drawn action is the top candidate; the other actions with at least MIN_PROB are
    alternatives whose values stay inside the ladder's margin (the note carries the real numbers);
    the model may pick another supported action and can use the chat to bluff about the move."""

    MIN_PROB = 0.08              # an action with less probability is not offered (proposed for bot/config.py)
    VALUE_STEP = 0.002           # value difference between consecutive candidates (all inside the margin)

    def __init__(self):
        super().__init__()
        import random as _random
        self._salt = "%016x" % _random.SystemRandom().getrandbits(64)

    def sample(self, spec, turn_msg, acts, probs):
        """One action drawn from probs."""
        import random as _random
        import zlib as _zlib
        seed = _zlib.crc32(("%s|%s|%s|%s|%s" % (self._salt, spec.game_id, spec.rules_hash,
                                                turn_msg.get("game_index"), turn_msg.get("turn"))).encode("utf-8"))
        rng = _random.Random(seed)
        x, acc = rng.random() * sum(probs), 0.0
        for a, p in zip(acts, probs):
            acc += p
            if x < acc:
                return a
        return acts[-1]

    def mixed(self, rows, pick, value, head):
        """rows: [{"action", "p"}]. Returns the candidates: the drawn action first, then the supported ones."""
        value = max(-1.0, min(1.0, float(value)))
        chosen = [r for r in rows if r["p"] >= self.MIN_PROB or r["action"] == pick]
        chosen.sort(key=lambda r: (r["action"] != pick, -r["p"]))
        out = []
        for rank, r in enumerate(chosen):
            note = head + ("; DRAWN for this turn" if r["action"] == pick else "; alternative with P=%.2f" % r["p"])
            if r.get("note"):
                note += "; " + r["note"]
            out.append(Candidate(r["action"], value - self.VALUE_STEP * rank, note))
        return out


# ---------------------------------------------------------------- facts from verify.json
_FACTS: Dict[str, tuple] = {}


def sim_sha(spec):
    try:
        v = memory.read_json(os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "verify.json"), {})
        return str(((v or {}).get("winner") or {}).get("sha256") or "")
    except Exception:
        return ""


def simulator_facts(spec):
    """What prep measured about the winning simulator (verify.json). Safe defaults when unreadable."""
    facts = {"unknown_outcomes": False, "both_seats": True, "playouts_per_s": 0.0, "steps_per_game": 0.0,
             "legal_complete": None}
    try:
        from ..prep import verify
        path = verify.verify_file(spec)
        mtime = os.path.getmtime(path)
        hit = _FACTS.get(path)
        if hit and hit[0] == mtime:
            return dict(hit[1])
        v = memory.read_json(path, {}) or {}
        w = v.get("winner") or {}
        facts.update(unknown_outcomes=bool(v.get("unknown_outcomes")), both_seats=bool(w.get("both_seats", True)),
                     playouts_per_s=float(w.get("playouts_per_s") or 0.0),
                     steps_per_game=float(w.get("steps_per_game") or 0.0), legal_complete=w.get("legal_complete"))
        _FACTS[path] = (mtime, dict(facts))
    except Exception:
        pass
    return facts


_REGISTRY: Dict[str, BaseEngine] = {}


def register(engine: BaseEngine):
    _REGISTRY[engine.family] = engine
    return engine


def get_engine(family: str) -> Optional[BaseEngine]:
    _load_all()
    return _REGISTRY.get(family)


_loaded = False


def _load_all():
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Importing registers each engine. Guardrails and truth are filters /
    # answer policies used by other modules; they register an engine too.
    from . import bluff, deals, guardrails, guess, search, simultaneous, truth  # noqa: F401
