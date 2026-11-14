"""The fallback ladder L3 -> L2 -> L1 that produces every decision.

Shared file (owner: Sebi). Status: real (L3 is plumbing only until engines exist).

  L3 engine   : verified simulator + engine for the family -> ranked candidates;
                the model picks among candidates within CANDIDATE_MARGIN of the best.
  L2 model    : chat/decide.py; the action must match legal_actions (exact, then
                case/whitespace-insensitive, sent with the arena's spelling).
  L1 safe     : first legal action, or the example action stored in prep when
                legal_actions is null.

decide_turn() never raises. It calls progress(decision) with the best decision
so far (first the L1 move, immediately), so the watchdog in main.py always has
something legal to send at the hard deadline.
"""

import time

from . import clock, config, log
from .chat import decide as decide_mod
from .chat import defense, offense
from .engines import base as engines_base
from .engines import guardrails
from .interfaces import Decision


def norm(a):
    return " ".join(str(a).split()).casefold()


def match_legal(action, legal):
    """Return the arena's spelling of action if it is legal, else None."""
    if not isinstance(action, str) or not legal:
        return None
    if action in legal:
        return action
    n = norm(action)
    for a in legal:
        if norm(a) == n:
            return a
    return None


def _filtered_action(action, profile):
    try:
        out = guardrails.filter_outgoing(action, "action", profile)
        return out if isinstance(out, str) else ""
    except Exception:
        log.exception("guardrails.filter_outgoing failed on action")
        return action


def finalize(decision, spec, legal, rejected=None):
    """Last code check of anything we send: legal, not rejected, filtered, clean chat."""
    profile = spec.profile if spec else None
    action = decision.action
    if legal:
        action = match_legal(action, legal) or None
        if action is not None and rejected is not None and norm(action) == norm(rejected):
            action = None
        if action is not None and not _filtered_action(action, profile):
            action = None
        if action is None:
            action = safe_action(spec, legal, rejected, profile)
            decision = Decision(action, offense.fallback_line(profile.family if profile else "unknown"),
                                1, "finalize replaced an invalid action")
    else:
        action = " ".join(str(action or "").split())
        action = _filtered_action(action, profile) or action
        if not action:
            action = safe_action(spec, legal, rejected, profile)
    chat = decide_mod.clean_chat(decision.chat, profile)
    return Decision(action, chat, decision.level, decision.why)


def safe_action(spec, legal, rejected=None, profile=None, observation="", variant=None):
    """L1. Never raises, never returns an empty string."""
    try:
        if legal:
            for a in legal:
                if (rejected is None or norm(a) != norm(rejected)) and _filtered_action(a, profile):
                    return a
            for a in legal:
                if rejected is None or norm(a) != norm(rejected):
                    return a
            return legal[0]
        from .prep import classify
        current_turn = {"observation": observation} if observation else None
        examples = classify.compatible_examples(
            spec.rules if spec else "", current_turn, variant)
        ex = spec.profile.example_action if spec and spec.profile else ""
        examples = ([ex] if ex else []) + examples
        for candidate in examples:
            if classify.action_shape_valid(candidate, spec.rules if spec else "",
                                           current_turn, variant) and (rejected is None or norm(candidate) != norm(rejected)):
                return candidate
        if spec and spec.sample_turn and isinstance(spec.sample_turn.get("legal_actions"), list):
            for a in spec.sample_turn["legal_actions"]:
                if classify.action_shape_valid(str(a), spec.rules if spec else "", current_turn, variant) \
                        and (rejected is None or norm(a) != norm(rejected)):
                    return str(a)
        if ex and rejected is None and classify.action_shape_valid(ex, spec.rules if spec else "",
                                                                   current_turn, variant):
            return ex
        # Never resend the rejected action; "pass" is a last resort guess.
        return "pass" if norm("pass") != norm(rejected or "") else "0"
    except Exception:
        log.exception("safe_action failed")
        return (legal[0] if legal else "pass")


def emergency(spec, turn_msg):
    """The watchdog's last resort when the worker produced nothing at all."""
    try:
        legal = turn_msg.get("legal_actions") if isinstance(turn_msg.get("legal_actions"), list) else None
        w = turn_msg.get("warning") if isinstance(turn_msg.get("warning"), dict) else {}
        profile = spec.profile if spec else None
        a = safe_action(spec, legal, w.get("rejected"), profile,
                        turn_msg.get("observation", ""), turn_msg.get("variant"))
        return finalize(Decision(a, offense.fallback_line(profile.family if profile else "unknown"),
                                 1, "watchdog emergency"), spec, legal, w.get("rejected"))
    except Exception:
        log.exception("emergency failed")
        legal = turn_msg.get("legal_actions") or ["pass"]
        return Decision(str(legal[0]), config.CHAT_FALLBACK, 1, "emergency of emergency")


def _within_margin(candidates):
    best = max(c.value for c in candidates)
    return [c for c in candidates if c.value >= best - config.CANDIDATE_MARGIN]


def decide_turn(spec, start_msg, turn_msg, deadline, progress=None, move_seconds=None):
    """Produce the decision for one turn. Never raises."""
    t0 = time.monotonic()

    def report(d):
        if progress:
            try:
                progress(d)
            except Exception:
                log.exception("progress callback failed")

    try:
        legal = turn_msg.get("legal_actions")
        legal = [str(a) for a in legal] if isinstance(legal, list) and legal else None
        warning = turn_msg.get("warning") if isinstance(turn_msg.get("warning"), dict) else None
        rejected = warning.get("rejected") if warning else None
        profile = spec.profile if spec else None
        family = profile.family if profile else "unknown"
        if warning:
            log.warning("previous answer rejected (%s/%s): %r reason=%s" % (
                warning.get("count"), warning.get("max"), rejected, warning.get("reason")))
        usable = legal
        if family == "guessing" and legal:
            from .engines.guess import unseen_actions
            unseen = unseen_actions(legal, str(turn_msg.get("observation") or ""))
            if unseen:
                usable = unseen
        if legal and rejected is not None:
            usable = [a for a in usable if norm(a) != norm(rejected)] or usable
        # L1 first, so the watchdog always has a legal move.
        best = finalize(Decision(safe_action(spec, usable, rejected, profile,
                                             turn_msg.get("observation", ""), turn_msg.get("variant")),
                                 offense.fallback_line(family), 1, "safe move"), spec, usable, rejected)
        report(best)
    except Exception:
        log.exception("ladder setup failed")
        return emergency(spec, turn_msg)

    ctx = defense.build_context(turn_msg.get("chat") if turn_msg.get("chat") is not None
                                else (start_msg or {}).get("chat"))

    # ---- L3: engine on a verified simulator
    candidates = []
    try:
        engine = engines_base.get_engine(family)
        if spec is None or not spec.verified or not spec.simulator_path:
            raise engines_base.NotAvailable("no verified simulator")
        if engine is None:
            raise engines_base.NotAvailable("no engine for family %s" % family)
        raw = engine.candidates(spec, turn_msg, deadline - 2.0) or []
        for c in raw:
            a = match_legal(c.action, usable) if usable else c.action
            if a and (rejected is None or norm(a) != norm(rejected)):
                c.action = a
                candidates.append(c)
        if candidates:
            candidates.sort(key=lambda c: c.value, reverse=True)
            candidates = _within_margin(candidates)
            best = finalize(Decision(candidates[0].action, offense.fallback_line(family), 3,
                                     "engine best (%.3f)" % candidates[0].value), spec, usable, rejected)
            report(best)
    except (NotImplementedError, engines_base.NotAvailable) as e:
        log.debug("L3 not available: %s" % e)
        candidates = []
    except Exception:
        log.exception("L3 failed, going down")
        candidates = []

    # ---- L2: one model call for move + chat (also picks among L3 candidates)
    try:
        timeout = min(clock.llm_match_timeout(turn_msg, move_seconds), deadline - time.monotonic() - 1.0)
        if timeout < 1.0:
            log.warning("L2 skipped: only %.1fs left" % (deadline - time.monotonic()))
        else:
            res = decide_mod.decide(spec, start_msg, turn_msg, ctx, candidates, usable, timeout)
            if res:
                if candidates:
                    allowed = [c.action for c in candidates]
                    a = match_legal(res["action"], allowed)
                    if a:
                        best = finalize(Decision(a, res["chat"], 3, "engine+model: " + res["why"]),
                                        spec, usable, rejected)
                    else:
                        # Keep the engine move; the model's chat may not fit it.
                        best = finalize(Decision(candidates[0].action, offense.fallback_line(family), 3,
                                                 "model chose outside candidates"), spec, usable, rejected)
                elif usable:
                    a = match_legal(res["action"], usable)
                    if a:
                        best = finalize(Decision(a, res["chat"], 2, "model: " + res["why"]),
                                        spec, usable, rejected)
                    else:
                        log.warning("L2 action not legal: %r" % res["action"][:80])
                else:
                    a = " ".join(res["action"].split())
                    if a and (rejected is None or norm(a) != norm(rejected)):
                        best = finalize(Decision(a, res["chat"], 2, "model (no legal list): " + res["why"]),
                                        spec, usable, rejected)
                report(best)
    except Exception:
        log.exception("L2 failed, going down")

    log.info("decision level=%d action=%r in %.2fs (%s)" % (
        best.level, best.action[:60], time.monotonic() - t0, best.why[:80]))
    return best
