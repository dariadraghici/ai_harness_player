"""Finite-horizon optimal hold-or-roll policy for Pig."""

import re

from .. import memory

STRATEGY_FILE = "pig.json"
TARGET = 50
TURN_MAX = 20


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "pig" in text and "roll" in text and "hold" in text else 0.0


def _score(text, names):
    for name in names:
        match = re.search(r"\b%s\s*[:=]\s*(\d+)" % name, str(text or ""), re.I)
        if match:
            return int(match.group(1))
    return 0


def parse_state(observation):
    text = str(observation or "")
    return (_score(text, ("your score", "my score", "you")),
            _score(text, ("opponent score", "their score", "them")),
            _score(text, ("turn total", "current turn", "turn")))


_TABLE = None


def _table():
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    size = TARGET + 1
    our = [[[0.0] * (TURN_MAX + 1) for _ in range(size)] for _ in range(size)]
    other = [[[0.0] * (TURN_MAX + 1) for _ in range(size)] for _ in range(size)]
    for me in range(size):
        for opponent in range(size):
            our[me][opponent][0] = 1.0 if me >= TARGET else 0.0
            other[me][opponent][0] = our[me][opponent][0]
    for _ in range(20):
        delta = 0.0
        for me in range(TARGET - 1, -1, -1):
            for opponent in range(TARGET - 1, -1, -1):
                for turn_total in range(TURN_MAX, -1, -1):
                    hold_our = our[me + turn_total][opponent][0] if me + turn_total < size else 1.0
                    roll_our = (other[me][opponent][0] +
                                sum(our[me][opponent][min(TURN_MAX, turn_total + value)]
                                    for value in range(2, 7))) / 6.0
                    hold_other = other[me][min(TARGET, opponent + turn_total)][0]
                    roll_other = (our[me][opponent][0] +
                                  sum(other[me][opponent][min(TURN_MAX, turn_total + value)]
                                      for value in range(2, 7))) / 6.0
                    next_our = max(hold_our, roll_our) if turn_total else roll_our
                    next_other = min(hold_other, roll_other) if turn_total else roll_other
                    delta = max(delta, abs(next_our - our[me][opponent][turn_total]),
                                abs(next_other - other[me][opponent][turn_total]))
                    our[me][opponent][turn_total] = next_our
                    other[me][opponent][turn_total] = next_other
        if delta < 1e-8:
            break
    _TABLE = (our, other)
    return _TABLE


def win_probability(me, opponent, turn_total, our_turn=True):
    table = _table()[0 if our_turn else 1]
    value = table[min(TARGET, int(me))][min(TARGET, int(opponent))][min(TURN_MAX, int(turn_total))]
    return value


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "target": TARGET})
    except Exception:
        pass
    return {"target": TARGET}


def decide(state, deadline):
    turn = state.get("turn") or state
    me, opponent, turn_total = parse_state(turn.get("observation", ""))
    hold = win_probability(me + turn_total, opponent, 0, False)
    roll = (win_probability(me, opponent, 0, False) +
            sum(win_probability(me, opponent, turn_total + value, True)
                for value in range(2, 7))) / 6.0
    return [{"action": "hold" if hold >= roll else "roll",
             "score": max(0.0, min(1.0, max(hold, roll)))}]
