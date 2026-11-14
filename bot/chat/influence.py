"""Per-match influence state and the glue between planner, scorer, metrics and the opponent profile.

Owner: luckydevs-2 area. Status: real (phase 1). Standard library only. Everything here NEVER raises: a failure
means "no influence this turn", the turn still answers.

  prepare()  at the start of a decision: score the predictions that are due, classify the opponent, plan the goal.
  on_sent()  when an answer was really sent: log the `influence` event, record the claim in the ledger, queue the
             prediction for scoring.

The state lives in the memory of the running process (one entry per match id); only counters and the opponent class
are written to /memory/opponents/<team_id>.json (opponents.py).
"""

import os
import random
import threading
import time
import zlib

from .. import log, metrics, opponents
from . import bandit as bandit_mod
from . import offense, score

_lock = threading.Lock()
_states = {}
_SALT = "%016x" % random.SystemRandom().getrandbits(64)     # the control draw is not predictable by the opponent
MAX_STATES = 12


class MatchInfluence:
    def __init__(self, key, team_id):
        self.key = key
        self.team_id = team_id
        self.ledger = offense.Ledger()
        self.opp_class = opponents.prior_class(team_id) or "unknown" if team_id else "unknown"
        self.signals = {}
        self.decisions = 0              # our decisions in this match (a re-sent turn is the same decision)
        self.used = set()               # once-per-match tactics already spent
        self.last_goal = None
        self.last_turn_key = None
        self.pending = []               # [{"goal", "ref_obs", "since", "family", "id"}]
        self.sent = 0
        self.explored = 0               # bandit exploration picks in this match (bounded)
        self.last_obs = ""              # observation at the previous decision (promise triggers)
        self.last_action = ""           # the action we sent last
        self.owed = None                # an open promise whose trigger just happened


def _key(start_msg):
    sm = start_msg or {}
    return str(sm.get("match_id") or "%s|%s" % (sm.get("opponent"), sm.get("game_id")))


def state_for(start_msg):
    key = _key(start_msg)
    with _lock:
        st = _states.get(key)
        if st is None:
            st = _states[key] = MatchInfluence(key, (start_msg or {}).get("opponent"))
            for old in list(_states)[:-MAX_STATES]:
                _states.pop(old, None)
        return st


def reset():
    with _lock:
        _states.clear()


def _rng(st, turn_msg):
    seed = zlib.crc32(("%s|%s|%s|%s" % (_SALT, st.key, turn_msg.get("game_index"), turn_msg.get("turn"))).encode())
    return random.Random(seed)


def _family(spec):
    return spec.profile.family if spec and spec.profile else "unknown"


def wants_replies(spec, start_msg):
    """Ask the engine for r_likely / r_target? Only where they are used: think_ahead and read_opponent, against an
    opponent that is not known to be deaf to us. Costs about 8 percent of the engine slice."""
    try:
        if not enabled() or spec is None or spec.profile is None:
            return False
        if spec.profile.family not in ("think_ahead", "read_opponent") or offense.detect_chat_is_move(spec):
            return False
        return state_for(start_msg).opp_class not in offense.QUIET_CLASSES
    except Exception:
        return False


def _replies(turn_msg):
    """The engine's reply table of THIS turn, or None (never a stale one)."""
    try:
        from ..engines import base
        r = dict(base.LAST_REPLIES)
        if r and str(r.get("game")) == str(turn_msg.get("game_index")) and str(r.get("turn")) == str(turn_msg.get("turn")):
            return r
    except Exception:
        pass
    return None


def _score_pending(st, spec, turn_msg):
    warned = bool(turn_msg.get("warning"))
    keep = []
    for p in st.pending:
        goal = p["goal"]
        if warned:
            # the previous answer was rejected: its chat was dropped, the prediction is void
            metrics.record_influence_result(
                id=p["id"], match=st.key, game=goal.game, turn=turn_msg.get("turn"), tactic=goal.tactic_id,
                hit=None, scored=False, reason="warning", lag=goal.lag, control=goal.is_control)
            continue
        p["since"] += 1
        out = score.score_prediction(goal, turn_msg, spec, p["ref_obs"], p["since"])
        if out.scored:
            metrics.record_influence_result(
                id=p["id"], match=st.key, game=goal.game, turn=turn_msg.get("turn"), family=p["family"],
                tactic=goal.shadow_tactic if goal.is_control else goal.tactic_id, hit=out.hit,
                their_action=out.their_action, their_quality_drop=out.their_quality_drop,
                drop_exact=out.drop_exact, lag=out.lag, scored=True, control=goal.is_control, opp=st.team_id)
            opponents.record_tactic(st.team_id, goal.shadow_tactic or goal.tactic_id, True, out.hit,
                                    control=goal.is_control, family=p["family"])
            continue
        if out.reason == "too early (lag)":
            # simultaneous game: their reaction is one round later, so the reference moves to this observation
            p["ref_obs"] = str(turn_msg.get("observation") or p["ref_obs"])
            keep.append(p)
    st.pending = keep


def enabled():
    """BOT_INFLUENCE=off switches the whole layer to plain quiet chat (the kill switch before a freeze)."""
    return os.environ.get("BOT_INFLUENCE", "on").strip().lower() not in ("0", "off", "false", "no", "none")


def prepare(spec, start_msg, turn_msg, ctx, candidates=None):
    """The goal of this decision. Never raises; falls back to a quiet goal."""
    try:
        if not enabled():
            return offense._quiet()
        st = state_for(start_msg)
        warned = bool(turn_msg.get("warning"))
        _score_pending(st, spec, turn_msg)
        if not warned:
            st.decisions += 1
            st.owed = _promise_due(st, turn_msg)
            st.last_obs = str(turn_msg.get("observation") or "")
        if st.team_id:
            prior = opponents.prior_class(st.team_id)
            cls, signals = opponents.classify(ctx, st.decisions, prior, opponents.tactic_stats(st.team_id))
            st.opp_class, st.signals = cls, signals
        family = _family(spec)
        own, glob = opponents.arm_stats(st.team_id, family)
        goal = offense.plan_influence(spec, turn_msg, candidates, None, _rng(st, turn_msg), st, start_msg,
                                      replies=_replies(turn_msg), bandit=bandit_mod.Bandit(own, glob, st.explored))
        if goal.arm_info and goal.arm_info.get("explore") and not goal.resend:
            st.explored += 1
        return goal
    except Exception:
        log.exception("influence.prepare failed")
        return offense._quiet()


def on_sent(goal, spec, start_msg, turn_msg, action, chat):
    """The answer carrying `chat` was sent. Log the attempt, update the ledger, queue the prediction."""
    try:
        if goal is None or not isinstance(turn_msg, dict):
            return
        st = state_for(start_msg)
        family = _family(spec)
        goal.game, goal.turn = turn_msg.get("game_index"), turn_msg.get("turn")
        ident = "%s:%s:%s" % (st.key, goal.game, goal.turn)
        shadow = goal.shadow_tactic
        metrics.record_influence(
            id=ident, match=st.key, game=goal.game, turn=goal.turn, family=family, opp=st.team_id,
            tactic=goal.tactic_id, shadow_tactic=shadow or None, control=bool(goal.is_control), goal=goal.kind,
            target=goal.target_action or None, avoid=goal.avoid_action or None, gap=goal.gap,
            from_engine=bool(goal.from_engine), line_len=len(chat or ""), opp_class=goal.opp_class,
            resend=bool(goal.resend), lag=goal.lag, arm=goal.arm_info)
        if goal.tactic_id != "quiet" and not goal.is_control:
            st.ledger.add(goal, chat)
            if goal.tactic_id in offense.ONCE_PER_MATCH:
                st.used.add(goal.tactic_id)
        st.last_goal = goal
        st.sent += 1
        if goal.tactic_id != "quiet" or goal.is_control:
            st.pending.append({"goal": goal, "ref_obs": str(turn_msg.get("observation") or ""), "since": 0,
                               "family": family, "id": ident})
            opponents.record_tactic(st.team_id, shadow or goal.tactic_id, False, False, control=goal.is_control,
                                    family=family)
        if goal.tactic_id == "commitment" and not goal.resend:
            st.ledger.promise(goal)
        st.last_action = str(action or "")
        st.last_obs = str(turn_msg.get("observation") or "")     # what the board looked like when we spoke
    except Exception:
        log.exception("influence.on_sent failed")


def on_game_end(start_msg, ctx):
    """Persist the class this opponent showed in this match (once per game end)."""
    try:
        st = state_for(start_msg)
        if st.team_id:
            opponents.record_class(st.team_id, st.opp_class, st.signals,
                                   [l.text for l in (ctx.theirs if ctx else [])][-3:])
    except Exception:
        log.exception("influence.on_game_end failed")


# ---------------------------------------------------------------- promises (commitment with a kept-promises ledger)
def _promise_due(st, turn_msg):
    """An open promise "when they play X we answer Y" whose X just happened, or None. X counts as played when it
    shows up in the observation more often than at our previous decision (our own move, if it has the same
    name, counts once)."""
    try:
        open_ = st.ledger.open_promises()
        if not open_ or not st.last_obs:
            return None
        cur = str(turn_msg.get("observation") or "")
        for p in open_:
            growth = score.occurrences(cur, p["if"]) - score.occurrences(st.last_obs, p["if"])
            need = 2 if st.last_action and st.last_action.strip().casefold() == p["if"].strip().casefold() else 1
            if growth >= need:
                return p
    except Exception:
        log.exception("influence._promise_due failed")
    return None


def honor_promise(start_msg, action, allowed):
    """If a promise is due, the action to play: the promised answer when the engine lists it among its candidates
    (it is our best response, so it costs nothing), else the action we had; the promise is then marked kept or
    broken. Returns (action, status or None). Never raises."""
    try:
        st = state_for(start_msg)
        p, st.owed = st.owed, None
        if p is None:
            return action, None
        want = p["then"]
        norm = lambda a: " ".join(str(a).split()).casefold()
        if norm(action) == norm(want):
            p["status"] = "kept"
            return action, "kept"
        pool = [a for a in (allowed or []) if norm(a) == norm(want)]
        if pool:
            p["status"] = "kept"
            return pool[0], "kept"
        p["status"] = "broken"
        return action, "broken"
    except Exception:
        log.exception("influence.honor_promise failed")
        return action, None
