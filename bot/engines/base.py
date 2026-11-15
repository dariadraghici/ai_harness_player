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

Children (bot/resources.py decides the numbers; BOT_RESOURCES=off gives the old behavior: one fresh
child per turn, killed on return):
  * WARM WORKER: one sandbox child per (family, game, part) stays alive while a game is being played
    and is reused for every turn (the start plus the load of the simulator is paid once). The live check
    still runs in every job: reusing the child does not mean trusting the simulator longer. Warm
    children are killed at `end`, when the container goes idle (shutdown_workers) and by an idle
    watchdog when one has not been used for longer than a move clock. If a warm child is busy (a late
    thread of a turn the watchdog already answered), the new turn uses a fresh child instead.
  * PARALLEL PARTS: an engine that can split its decision (think_ahead: root split, guessing: share of
    the actions) runs `search_workers` children with the SAME slice, so parallelism buys resolution and
    not wall clock. The results are merged in PART order (never in arrival order), so the merged
    candidates are a function of the results; a part that fails or times out is dropped and the others
    still answer.

No child is alive once the game is over or the container is idle. Opponent chat, and anything
derived from it, never goes into a job payload: the model sees the candidates
AND the chat and makes the one decision.
"""

import os
import threading
import time
from typing import Dict, List, Optional

from .. import config, log, memory, resources
from ..interfaces import Candidate, Engine, VariantSpec  # noqa: F401  (re-exported)

# ---------------------------------------------------------------- constants (proposed for bot/config.py)
SLICE_FRACTION = 0.15            # of the move clock: 3 s of 20
SLICE_MIN_S = 0.4                # below this the engine does not start
SLICE_BACKSTOP_S = 1.5           # the parent kills the child this long after the slice
# Families on by default when BOT_L3 is not set. Decided by MEASUREMENT, never by opinion: think_ahead and
# read_opponent ran in real practice matches on their games (tic_tac_toe, nim_deal, pig; rps_twist), guessing is
# measured on the real guess_number simulators.
# bluff was off while no practice game of the family existed. testgames/kuhn_bet.py is that game, prep verified
# a simulator for both its variants, and the engine then beat the alternative twice over:
#   arena practice against the random bot, with prep: engine 5 won / 1 drawn / 1 lost over 7 games, the model
#   deciding instead 1 won / 1 drawn / 4 lost over 6 games (0 warnings either way);
#   headless (scripts/engine_arena.py, no model at all): engine 23 won / 2 drawn / 15 of 40 games against
#   random, the whole ladder 11 won / 3 drawn / 6 of 20, every move answered, none illegal, 0.71 s per move.
# It costs about one second more per answer, which is a tie-break, while losing the game is not.
# "unknown" is not an engine of its own: it is the switch for the generic fallback in resolve_engine() below,
# which lends an unclassified game the engine that fits the shape of its verified simulator. T3 brings nine
# games nobody has played, so this is the one family we cannot practise and the one that must not be empty.
L3_DEFAULT = "think_ahead,guessing,read_opponent,bluff,unknown"
# truth and deals are deliberately NOT in L3_ALL, so BOT_L3=all does not turn them on. Their engines need no
# simulator, so until the gate in ladder.py was fixed (it asked every engine for a verified simulator before
# it asked for a move, and prep never builds one for a family of words) they could not run at all. Measured
# the moment they could, on the only games of their families we have: the deals engine returns 0 candidates
# on all 53 turns of split_deal, and the truth engine answers "unknown" on all 60 turns of doc_quiz, which is
# a free-text game - it has no enumerated answers to find a supporting quote among, and the game rejects
# "unknown". So they stay off, and BOT_L3="...,truth" opts in explicitly (enabled() honours any name in the
# list, L3_ALL only decides what "all" means). guardrails has no entry on purpose: its engine raises
# NotImplementedError by design, the family is played through the outgoing filter, not by a move policy.
L3_ALL = ("think_ahead", "guessing", "read_opponent", "bluff", "unknown")
JOB_ERRORS_OFF = 2               # simulator exceptions in jobs before "off for this game"
WARM_CPU_S = 900                 # CPU backstop of a warm child (it lives for a whole game, not for one slice)
WARM_IDLE_MIN_S = 25.0           # a warm child unused for longer than max(this, move clock + 5) is killed
WARM_WATCH_PERIOD_S = 2.0

# What the last candidates() call did, for the metrics line: workers used, warm or fresh, children alive.
LAST_RUN: Dict[str, object] = {}

# What the engine knows about the OPPONENT's replies to the move it recommends (chat/offense.py turns it into r_likely,
# r_target and a gap; PROMPT-CHAT section 4). Cleared at the start of every candidates() call, so it is never stale.
#   {"family", "game", "turn", "action": the engine's top candidate, "rows": [{"action","value","exact","p"?}],
#    "matrix"?, "acts"?, "prev_action"?}
LAST_REPLIES: Dict[str, object] = {}


# ---------------------------------------------------------------- warm workers (one registry per process)
class _Warm:
    __slots__ = ("worker", "sha", "last", "idle_s", "busy")

    def __init__(self, worker, sha, idle_s):
        self.worker, self.sha, self.idle_s = worker, sha, idle_s
        self.last = time.monotonic()
        self.busy = threading.Lock()


_warm: Dict[tuple, _Warm] = {}
_warm_lock = threading.Lock()
_warm_thread = None


def warm_count():
    with _warm_lock:
        return len(_warm)


def _close_all(entries):
    for e in entries:
        try:
            e.worker.close()
        except Exception:
            log.exception("warm worker close")


def drop_workers(match=None):
    """Close the warm workers whose key satisfies match(key) (all when None). Returns how many."""
    with _warm_lock:
        keys = [k for k in _warm if match is None or match(k)]
        entries = [_warm.pop(k) for k in keys]
    _close_all(entries)
    return len(entries)


def shutdown_workers():
    """Kill every warm worker of this process: at `end`, when the container goes idle, at shutdown."""
    n = drop_workers()
    if n:
        log.info("engines: %d warm worker(s) closed" % n)
    return n


def _warm_watch():
    global _warm_thread
    while True:
        time.sleep(WARM_WATCH_PERIOD_S)
        now = time.monotonic()
        with _warm_lock:
            stale = [k for k, e in _warm.items() if not e.busy.locked() and now - e.last > e.idle_s]
            entries = [_warm.pop(k) for k in stale]
            if not _warm:
                _warm_thread = None
                done = True
            else:
                done = False
        if entries:
            log.info("engines: %d idle warm worker(s) closed" % len(entries))
            _close_all(entries)
        if done:
            return          # no warm worker, no timer loop: an idle specialist costs nothing


def _warm_get(key, path, sha, mem_mb, idle_s):
    """The warm entry for key, created if needed (replaced when the simulator changed)."""
    global _warm_thread
    from ..prep import sandbox
    old = None
    with _warm_lock:
        e = _warm.get(key)
        # Replaced only when the simulator changed. NOT when the plan's memory figure moved: the figure shrinks as
        # the warm children themselves raise the memory in use, and a worker rebuilt every turn is no warm worker.
        if e is not None and (e.sha != sha or e.worker.path != os.path.abspath(path)):
            if e.busy.locked():
                return None
            old = _warm.pop(key)
            e = None
        if e is None:
            e = _Warm(sandbox.Worker(path, mem_mb=mem_mb, cpu_s=WARM_CPU_S, name="warm-%s" % (key[0],)), sha, idle_s)
            _warm[key] = e
        e.idle_s = idle_s
        if _warm_thread is None:
            _warm_thread = threading.Thread(target=_warm_watch, name="warm-watch", daemon=True)
            _warm_thread.start()
    if old is not None:
        _close_all([old])
    return e


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

    # ---- parallel parts: an engine that can split its decision sets splittable and implements merge()
    splittable = False

    def merge(self, results: List[dict], payload: dict) -> dict:
        """Merge the ok results of several parts (given in PART order, never arrival order) into one."""
        return results[0]

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
                    drop_workers(lambda k, fam=self.family, kk=key: k[0] == fam and k[1:3] == tuple(kk))
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
        drop_workers(lambda k, fam=self.family: k[0] == fam)

    def _count(self, spec, game, ok):
        if game.counted:
            return
        game.counted = True
        from ..prep import verify
        verify.note_live(spec, ok, sim_sha(spec))

    # ---- running the job: one child, or several parts in parallel, warm or fresh
    def decisive(self, result: dict) -> bool:
        """True when this result settles the decision, so that the other parts need not run on (think_ahead: a
        proven win; the sequential search stops there too). The others are killed and their children restart
        on demand."""
        return False

    def _run_one(self, spec, path, payload, timeout, part, parts, warm, plan, budget, clock, handles=None, cancel=None):
        """One part. Returns the shape sandbox.run_job returns. Never raises. handles[part] receives the Worker
        in use, so that a decisive part can stop the others."""
        from ..prep import sandbox
        t0 = time.monotonic()
        pl = dict(payload)
        if parts > 1:
            pl["part"], pl["parts"] = part, parts
        entry = None
        if warm:
            key = (self.family, spec.game_id, spec.rules_hash, part)
            entry = _warm_get(key, path, sim_sha(spec), plan.child_mem_mb, max(WARM_IDLE_MIN_S, clock + 5.0))
            if entry is not None and not entry.busy.acquire(blocking=False):
                entry = None                 # a late thread of an earlier turn still uses it: fresh child
        if entry is None and parts <= 1:
            return sandbox.run_job(path, self.job_file, pl, timeout, mem_mb=plan.child_mem_mb,
                                   cpu_s=int(budget * 3 + 20))
        fresh = None
        if entry is None:                    # several parts without a warm child: one fresh child per part
            fresh = sandbox.Worker(path, mem_mb=plan.child_mem_mb, cpu_s=int(budget * 3 + 20))
        worker = entry.worker if entry is not None else fresh
        if handles is not None:
            handles[part] = worker
        cpu0 = worker.cpu_used
        try:
            if cancel is not None and cancel.is_set():       # another part settled the decision before this one began
                return {"ok": False, "kind": "cancelled", "error": "stopped: another part settled the decision"}
            res = worker.job(self.job_file, pl, timeout)
            return {"ok": True, "result": res, "seconds": round(time.monotonic() - t0, 3)}
        except sandbox.SandboxError as e:
            return {"ok": False, "error": str(e), "kind": e.kind, "etype": e.etype,
                    "seconds": round(time.monotonic() - t0, 3)}
        except Exception as e:
            log.exception("engine worker failed unexpectedly")
            worker.kill()
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "kind": "crash", "etype": "",
                    "seconds": round(time.monotonic() - t0, 3)}
        finally:
            if fresh is not None:
                fresh.close()
            if entry is not None:
                entry.last = time.monotonic()
                # a warm child is not reaped, so RUSAGE_CHILDREN cannot see its CPU: it reports its own with each answer
                delta = worker.cpu_used - cpu0 if worker.cpu_used >= cpu0 else worker.cpu_used
                with _warm_lock:
                    LAST_RUN["warm_cpu_s"] = round(float(LAST_RUN.get("warm_cpu_s") or 0.0) + max(0.0, delta), 3)
                    LAST_RUN["child_rss_mb"] = max(float(LAST_RUN.get("child_rss_mb") or 0.0), worker.rss_mb())
                entry.busy.release()

    def _run_parts(self, spec, path, payload, timeout, parts, warm, plan, budget, clock):
        """Outcomes in PART order (None for a part that never answered). Every child has the same slice. A part
        whose result is decisive stops the others at once (their outcome is then {"kind": "cancelled"})."""
        if parts <= 1:
            return [self._run_one(spec, path, payload, timeout, 0, 1, warm, plan, budget, clock)]
        outcomes = [None] * parts
        handles = [None] * parts
        cancel = threading.Event()

        def one(i):
            try:
                out = self._run_one(spec, path, payload, timeout, i, parts, warm, plan, budget, clock, handles, cancel)
                if cancel.is_set() and not out.get("ok"):
                    out = {"ok": False, "kind": "cancelled", "error": "stopped: another part settled the decision"}
                outcomes[i] = out
                if out.get("ok") and isinstance(out.get("result"), dict) and self.decisive(out["result"]) \
                        and not cancel.is_set():
                    cancel.set()
                    for j, w in enumerate(handles):
                        if j != i and w is not None:
                            w.kill("decisive")
            except Exception:
                log.exception("engine part %d failed" % i)

        threads = [threading.Thread(target=one, args=(i,), name="engine-part-%d" % i, daemon=True)
                   for i in range(parts)]
        for t in threads:
            t.start()
        end = time.monotonic() + timeout + 1.0
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        for i, t in enumerate(threads):
            if t.is_alive() and outcomes[i] is None:
                outcomes[i] = {"ok": False, "kind": "timeout", "error": "part %d did not answer in time" % i}
        return outcomes

    REPLY_BUDGET_S = 0.6         # time for the opponent's replies to the best move (influence layer, one short job)

    def _reply_pass(self, spec, path, payload, turn_msg, cands, deadline, plan, warm, clock):
        """After the search: ask the warm child for the value of every reply the opponent has to the move we
        recommend, and publish the table in LAST_REPLIES. Own clock, own table: the answer does not depend on how
        the search was split. Skipped when the deadline is near. Never raises."""
        try:
            top = max(cands, key=lambda c: c.value).action
            budget = min(self.REPLY_BUDGET_S, deadline - time.monotonic() - 0.1)
            if budget < 0.15:
                return
            pl = dict(payload, replies_only=True, action=top, budget_s=round(budget, 3))
            pl.pop("cache", None)
            pl.pop("known", None)
            out = self._run_one(spec, path, pl, budget + 1.0, 0, 1, warm, plan, budget, clock)
            res = out.get("result") if out.get("ok") else None
            table = (res or {}).get("replies")
            if isinstance(table, dict) and len(table.get("rows") or []) >= 2:
                LAST_REPLIES.update(family=self.family, game=turn_msg.get("game_index"), turn=turn_msg.get("turn"),
                                    action=top, rows=table["rows"])
        except Exception:
            log.exception("%s engine: reply table failed" % self.family)

    # ---- the one entry point
    want_replies = False         # set by the ladder when the influence layer wants r_likely / r_target (costs ~8% of a slice)

    def candidates(self, specialist, turn_msg, deadline):
        t0 = time.monotonic()
        LAST_REPLIES.clear()
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
        plan = resources.plan("turn_search")
        # BOT_SLICE_FRACTION wins; else the engine's own fraction (think_ahead 0.55), else the default
        fraction = plan.slice_fraction or float(getattr(self, "slice_fraction", SLICE_FRACTION))
        # the influence layer's reply table needs a little time AFTER the search: it comes out of this slice
        reserve = (self.REPLY_BUDGET_S + 0.2) if (self.want_replies and getattr(self, "reply_job", False)) else 0.0
        budget = min(fraction * clock, deadline - time.monotonic() - 0.4 - reserve)
        if budget < SLICE_MIN_S:
            log.info("%s engine: no time (%.2fs)" % (self.family, budget))
            return []
        legal = turn_msg.get("legal_actions")
        payload = {"observation": str(turn_msg.get("observation") or ""),
                   "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None,
                   "variant": {"label": spec.label}, "budget_s": round(budget, 3),
                   "margin": config.CANDIDATE_MARGIN, "family": self.family,
                   "tt_entries": plan.tt_entries, "samples_scale": plan.samples_scale,
                   "want_replies": bool(self.want_replies)}
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
        from ..prep import sandbox as _sb
        workers = max(1, min(plan.search_workers, _sb.capacity())) if self.splittable else 1
        warm = bool(plan.warm_worker)
        t_job = time.monotonic()
        outcomes = self._run_parts(spec, path, payload, timeout, workers, warm, plan, budget, clock)
        LAST_RUN.update(family=self.family, workers=workers, warm=warm, job_s=round(time.monotonic() - t_job, 3),
                        children=_sb.live_count(), ok=sum(1 for o in outcomes if o and o.get("ok")))
        good = [o for o in outcomes if o and o.get("ok")]
        if not good:
            out = next((o for o in outcomes if o), {"ok": False, "kind": "crash", "error": "no outcome"})
            game.errors += 1
            log.warning("%s engine: job failed (%s): %s" % (self.family, out.get("kind"), str(out.get("error"))[:200]))
            if game.errors >= JOB_ERRORS_OFF or out.get("kind") == "error":
                game.off, game.reason = True, "the simulator failed inside the engine job"
                self._count(spec, game, False)
            return []
        if len(good) < len(outcomes):
            bad = [o for o in outcomes if not (o and o.get("ok"))]
            real = [o for o in bad if (o or {}).get("kind") != "cancelled"]
            if real:
                log.warning("%s engine: %d of %d parts failed (%s), using the others" % (
                    self.family, len(real), len(outcomes), real[0].get("kind")))
            else:
                log.info("%s engine: a proven result from one part stopped the others" % self.family)
        results = [o.get("result") for o in good if isinstance(o.get("result"), dict)]
        if not results:
            return []
        dead = next((r for r in results if r.get("live") is False), None)
        res = dead
        if res is None:
            usable_results = [r for r in results if r.get("ok")]
            if len(usable_results) > 1:
                try:
                    res = self.merge(usable_results, payload)
                except Exception:
                    log.exception("%s engine: merge failed, using the first part" % self.family)
                    res = usable_results[0]
            else:
                res = usable_results[0] if usable_results else results[0]
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
        if cands and self.want_replies and getattr(self, "reply_job", False):
            self._reply_pass(spec, path, payload, turn_msg, cands, deadline, plan, warm, clock)
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


def needs_simulator(engine) -> bool:
    """Whether this engine can do nothing without a verified simulator.

    Every engine that searches a game is a SimEngine and runs the simulator in a sandbox child, so without a
    verified one it has nothing to search. The others (truth, deals) decide from the rules text and the turn
    alone - and bot/ladder.py was asking all of them for a verified simulator before it asked for a move, so
    those two engines never ran a single turn in a tournament: `deals`, `guardrails` and `truth` have 0
    verified simulators between them, because a game of words is the kind prep cannot simulate.
    """
    return isinstance(engine, SimEngine)


def resolve_engine(family: str, spec=None):
    """The engine that plays this variant: the family's own, or a generic one when the family is unknown.

    prep classifies a game into a family, and when it cannot it still builds and verifies a simulator for it
    (prep/pipeline.py gives an unknown family candidates whenever the clock allows). Nothing used to ask for
    that simulator at turn time, because no engine is registered under "unknown", so the whole L3 level was
    skipped and the game was played by one model call. The shape of the verified simulator is enough to pick a
    reasonable engine without knowing what the game IS:

      simultaneous step (both seats move at once) -> the mixed-strategy engine, which needs no more than a
                                                     payoff matrix it estimates itself
      otherwise                                   -> the search engine, which already has a mode for a
                                                     simulator whose outcomes it cannot score (MCTS instead of
                                                     alpha-beta, see search_job.py `unknown`)

    Returns (engine or None, the family whose BOT_L3 switch decides it). BOT_L3 without "unknown" in the list
    turns the fallback off and restores the old behaviour exactly.
    """
    engine = get_engine(family)                 # one accessor, so a caller that replaces it is honoured
    if engine is not None:
        return engine, family
    if not enabled("unknown"):
        return None, family
    if spec is None or not getattr(spec, "verified", False):
        return None, family
    profile = getattr(spec, "profile", None)
    # The borrow is safe in both directions: the mixed-strategy engine probes the live simulator and returns
    # nothing when the step is not simultaneous after all, and a job that cannot read the game answers with an
    # error, which costs this one turn and drops us to the next level of the ladder, exactly as today.
    borrowed = "read_opponent" if getattr(profile, "simultaneous", False) else "think_ahead"
    return get_engine(borrowed), borrowed


_loaded = False


def _load_all():
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Importing registers each engine. Guardrails and truth are filters /
    # answer policies used by other modules; they register an engine too.
    from . import bluff, deals, guardrails, guess, search, simultaneous, truth  # noqa: F401
