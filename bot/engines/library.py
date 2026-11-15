"""Playing with a generated adapter and an installed library (TASK section 5).

Owner: luckydevs-3 area. Status: real.

What prep built (bot/libs/) is used here, time-boxed and judged. The adapter never runs in the bot process: it
runs in a sandbox child through bot/libs/adapter_job.py, with a memory cap, a CPU cap, no model key and no
stdin, and its stdout goes nowhere near our protocol.

The trust level decides what the output is worth (bot/libs/validate.py judged it in prep):

    A  exact or dominant: the candidates ARE the move; the model may only pick among the equal-value ones
    B  strong but unproven: the candidates go to the one model call of the turn, which must choose among them
    C  advisory: the rows are evidence in the prompt, the model is free
    X  rejected: nothing is used

One child per game is kept warm (importing numpy or scipy costs more than the search itself), and it is killed
at the end of the game, when the container goes idle (bot/resources.py) and when it has been unused for longer
than a move clock. No child is alive while the container waits for the next game.
"""

import os
import threading
import time

from .. import config, log, memory, resources
from ..interfaces import Candidate
from . import base

JOB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs", "adapter_job.py")
SLICE_MIN_S = 0.4
BACKSTOP_S = 1.5
WARM_CPU_S = 900
IDLE_S = 25.0
MAX_CANDIDATES = 12
ADVISORY_ROWS = 6              # rows of a level C engine that go into the prompt as evidence

_warm = {}                     # (game_id, rules_hash) -> [Worker, last_used, game_index]
_lock = threading.Lock()


def engine_enabled():
    """BOT_LIBS_ENGINE=off keeps prep's work but never uses it at turn time."""
    if os.environ.get("BOT_LIBS_ENGINE", "on").strip().lower() in ("0", "off", "false", "no", "none"):
        return False
    from .. import libs
    return libs.enabled()


def trust_for(spec):
    """(level, trust dict) of this variant, or (None, {}). Cheap: one small JSON read, cached by mtime."""
    try:
        from ..libs import validate
        data = validate.load(spec)
        if not isinstance(data, dict) or data.get("harness_version") != config.HARNESS_VERSION:
            return None, {}
        level = data.get("level")
        if level not in ("A", "B", "C"):
            return None, {}
        if not os.path.isfile(adapter_file(spec)):
            return None, {}
        return level, data
    except Exception:
        log.exception("library: trust unreadable")
        return None, {}


def adapter_file(spec):
    from ..libs import adapter_gen
    return adapter_gen.adapter_path(spec)


def shutdown_workers(reason="idle"):
    """Kill every warm adapter child of this process. Called by bot/resources.py and at the end of a game."""
    with _lock:
        rows = list(_warm.items())
        _warm.clear()
    for _key, row in rows:
        try:
            row[0].close()
        except Exception:
            log.exception("library: worker close")
    if rows:
        log.info("library: %d adapter child(ren) closed (%s)" % (len(rows), reason))
    return len(rows)


def _worker(spec, game_index, mem_mb):
    """The warm child of this variant, restarted when the game changed or it sat unused too long."""
    from ..prep import sandbox
    key = (spec.game_id, spec.rules_hash)
    now = time.monotonic()
    stale = None
    with _lock:
        row = _warm.get(key)
        if row is not None and (row[2] != game_index or now - row[1] > IDLE_S * 4 or not row[0].alive()):
            stale, row = row, None
            _warm.pop(key, None)
        if row is None:
            row = [sandbox.Worker(None, mem_mb=mem_mb, cpu_s=WARM_CPU_S, name="adapter"), now, game_index]
            _warm[key] = row
        row[1] = now
        worker = row[0]
    if stale is not None:
        try:
            stale[0].close()
        except Exception:
            pass
    return worker


def _payload(spec, trust, turn_msg, budget, events):
    legal = turn_msg.get("legal_actions")
    seat = turn_msg.get("seat") if turn_msg.get("seat") in (0, 1) else getattr(spec, "seat", None)
    profile = spec.profile
    return {"mode": "suggest", "adapter_path": adapter_file(spec), "lib_path": trust.get("lib_path") or "",
            "observation": str(turn_msg.get("observation") or ""),
            "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None,
            "variant": {"label": spec.label}, "seat": seat, "budget_s": round(budget, 3),
            "action_regex": _regex(spec),
            "profile": {"family": profile.family, "action_format": profile.action_format,
                        "example_action": profile.example_action, "simultaneous": profile.simultaneous,
                        "perfect_information": profile.perfect_information,
                        "speech_restrictions": profile.speech_restrictions,
                        "capability_tags": list(trust.get("tags") or ())},
            "events": list(events or ())}


def _regex(spec):
    try:
        from ..prep import classify
        a = classify.load_analysis(spec.game_id, spec.rules_hash) or {}
        return str(a.get("action_regex") or "")
    except Exception:
        return ""


def candidates(spec, turn_msg, deadline, events=()):
    """(level, [Candidate]): what the library engine proposes this turn. NEVER raises.

    level is the trust level the rows come from, so the ladder knows whether they are moves (A/B) or evidence
    (C). An empty list means "nothing from here this turn" and the ladder goes on as if the subsystem did not
    exist.
    """
    t0 = time.monotonic()
    try:
        if spec is None or not engine_enabled():
            return None, []
        level, trust = trust_for(spec)
        if level is None:
            return None, []
        clock = turn_msg.get("seconds")
        clock = float(clock) if isinstance(clock, (int, float)) and clock > 0 else float(config.TURN_CLOCK_S)
        plan = resources.plan("turn_search")
        fraction = plan.slice_fraction or base.SLICE_FRACTION
        budget = min(fraction * clock, deadline - time.monotonic() - 0.4)
        if budget < SLICE_MIN_S:
            log.info("library: no time (%.2fs)" % budget)
            return None, []
        worker = _worker(spec, turn_msg.get("game_index"), plan.child_mem_mb)
        payload = _payload(spec, trust, turn_msg, budget, events)
        timeout = min(budget + BACKSTOP_S, max(0.5, deadline - time.monotonic()))
        from ..prep import sandbox
        try:
            res = worker.job(JOB_FILE, payload, timeout)
        except sandbox.SandboxError as e:
            log.warning("library: adapter child failed (%s): %s" % (e.kind, str(e)[:160]))
            if e.kind in ("timeout", "crash", "error"):
                _demote(spec, trust, "the adapter %s at run time" % e.kind)
            return None, []
        rows = (res or {}).get("actions") or []
        if (res or {}).get("problems"):
            log.info("library: %s" % "; ".join(str(p) for p in res["problems"][:2])[:200])
        if not rows:
            log.info("library: no candidate (%s)" % str((res or {}).get("reason") or "")[:80])
            return None, []
        base.LAST_RUN.update(library=trust.get("library"), library_level=level,
                             library_s=round(time.monotonic() - t0, 3), library_rows=len(rows))
        out = _build(rows, level, trust)
        log.info("library: %d candidate(s) from %s (trust %s) in %.2fs" % (
            len(out), trust.get("library"), level, time.monotonic() - t0))
        return level, out
    except Exception:
        log.exception("library: engine failed, no candidates this turn")
        return None, []


def _build(rows, level, trust):
    """Candidate objects. A level C engine is advisory, so its values are never allowed to decide alone."""
    name = str(trust.get("library") or "library")
    out = []
    for i, row in enumerate(rows[:MAX_CANDIDATES]):
        value = row.get("value")
        value = 0.0 if value is None else max(-1.0, min(1.0, float(value)))
        if level == "C":
            value = min(value, 0.0) - 0.001 * i      # advisory: never claims a winning value
        note = "%s (trust %s%s)" % (name, level, ", exact" if row.get("exact") else "")
        extra = ", ".join("%s=%s" % (k, v) for k, v in (row.get("meta") or {}).items())
        if extra:
            note += ": " + extra[:120]
        if row.get("confidence") is not None:
            note += "; confidence %.2f" % float(row["confidence"])
        out.append(Candidate(str(row["action"]), value, note))
    return out


def advisory_text(rows):
    """The level C block for the decision prompt: evidence, not instructions."""
    if not rows:
        return ""
    items = []
    for c in rows[:ADVISORY_ROWS]:
        items.append({"action": c.action, "note": c.note[:120]})
    return ("ENGINE ADVICE (an installed open-source library, NOT verified by us: evidence only, you may play "
            "something else): " + __import__("json").dumps(items, ensure_ascii=True))


def _demote(spec, trust, why):
    """A run-time failure lowers the stored trust level, so the next game does not pay for it again."""
    try:
        from ..libs import validate
        order = {"A": "B", "B": "C", "C": "X"}
        level = order.get(trust.get("level") or "", "X")
        data = dict(trust)
        data["level"] = level
        data["reasons"] = [why] + list(trust.get("reasons") or ())[:3]
        data["demoted_at"] = int(time.time())
        memory.write_json_atomic(validate.trust_path(spec), data)
        log.warning("library: trust lowered to %s (%s)" % (level, why))
    except Exception:
        log.exception("library: demotion failed")


def record_result(spec, won):
    """Called at the end of a game: a trusted engine that keeps losing is demoted (TASK section 5 `end`)."""
    try:
        if spec is None:
            return
        from ..libs import validate
        level, trust = trust_for(spec)
        if level is None:
            return
        data = dict(trust)
        wins = int(data.get("wins") or 0) + (1 if won else 0)
        losses = int(data.get("losses") or 0) + (0 if won else 1)
        data["wins"], data["losses"] = wins, losses
        if losses >= 3 and wins == 0 and level in ("A", "B"):
            data["level"] = "C"
            data["reasons"] = ["lost %d games with no win: advisory only from now on" % losses] + \
                              list(trust.get("reasons") or ())[:3]
            log.warning("library: %s lowered to C after %d losses" % (data.get("library"), losses))
        memory.write_json_atomic(validate.trust_path(spec), data)
    except Exception:
        log.exception("library: result not recorded")
