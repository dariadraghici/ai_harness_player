"""Deadlines and the watchdog that guarantees an answer in time.

Owner: luckydevs-1. Status: real.

All times are time.monotonic() seconds. The work for a request runs in a
worker thread (see main.py); the main thread waits until the hard deadline
from this module and then answers with the best decision found so far.
Heavy computation must run in a killable child process (prep/sandbox.py),
because a thread cannot be killed.
"""

import time

from . import config


def now():
    return time.monotonic()


def _num(v, default):
    try:
        v = float(v)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def prep_hard(msg, t0):
    clock = _num(msg.get("seconds"), config.PREP_CLOCK_S)
    return t0 + clock * config.PREP_HARD_S / config.PREP_CLOCK_S


def start_hard(msg, t0, prepared, prep_study_seconds=None):
    """Hard deadline for ready at start.

    prepared: this container answered a prep. Then the arena clock is the
    short one (30 s). We never trust a start field that would make a prepared
    container answer later than its prep said.
    """
    if prepared:
        clock = _num(msg.get("study_seconds"), _num(prep_study_seconds, config.START_WARM_CLOCK_S))
        if prep_study_seconds:
            clock = min(clock, _num(prep_study_seconds, clock))
        return t0 + clock * config.START_WARM_HARD_S / config.START_WARM_CLOCK_S
    clock = _num(msg.get("study_seconds"), config.START_COLD_CLOCK_S)
    return t0 + clock * config.START_COLD_HARD_S / config.START_COLD_CLOCK_S


def turn_clock(msg, move_seconds=None):
    return _num(msg.get("seconds"), _num(move_seconds, config.TURN_CLOCK_S))


def turn_hard(msg, t0, move_seconds=None):
    return t0 + turn_clock(msg, move_seconds) * config.TURN_HARD_S / config.TURN_CLOCK_S


def turn_soft(msg, t0, move_seconds=None):
    return t0 + turn_clock(msg, move_seconds) * config.TURN_SOFT_S / config.TURN_CLOCK_S


def llm_match_timeout(msg, move_seconds=None):
    return turn_clock(msg, move_seconds) * config.LLM_MATCH_TIMEOUT_S / config.TURN_CLOCK_S


def remaining(deadline, margin=0.0):
    return deadline - time.monotonic() - margin
