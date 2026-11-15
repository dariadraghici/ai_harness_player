"""Safe-line and free-box policy for 4x4-box Dots and Boxes."""

import re
import time

from .. import memory

STRATEGY_FILE = "dots_boxes.json"
SIZE = 4


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "dots and boxes" in text and "line" in text else 0.0


def _point(token):
    match = re.fullmatch(r"([a-e])([1-5])", str(token or "").casefold())
    return (ord(match.group(1)) - 97, int(match.group(2)) - 1) if match else None


def _line(a, b):
    return tuple(sorted((a, b)))


def _all_lines():
    lines = []
    for row in range(5):
        for col in range(4):
            lines.append(_line((col, row), (col + 1, row)))
    for col in range(5):
        for row in range(4):
            lines.append(_line((col, row), (col, row + 1)))
    return lines


def _boxes():
    result = []
    for row in range(4):
        for col in range(4):
            top = _line((col, row), (col + 1, row))
            bottom = _line((col, row + 1), (col + 1, row + 1))
            left = _line((col, row), (col, row + 1))
            right = _line((col + 1, row), (col + 1, row + 1))
            result.append((top, right, bottom, left))
    return result


def parse_state(observation):
    drawn = set()
    for left, right in re.findall(r"\bline\s+([a-e][1-5])-([a-e][1-5])\b",
                                  str(observation or ""), re.I):
        a, b = _point(left), _point(right)
        if a and b:
            drawn.add(_line(a, b))
    return drawn


def _completes(line, drawn):
    return sum(line in box for box in _boxes() if line in box and
               sum(side in drawn for side in box) == 3)


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "size": SIZE})
    except Exception:
        pass
    return {"size": SIZE}


def decide(state, deadline):
    turn = state.get("turn") or state
    drawn = parse_state(turn.get("observation", ""))
    remaining = [line for line in _all_lines() if line not in drawn]
    if not remaining:
        return []
    completing = [line for line in remaining if _completes(line, drawn)]
    choice = completing[0] if completing else next(
        (line for line in remaining if _completes(line, drawn | {line}) == 0),
        remaining[0])
    return [{"action": "line %s-%s" % (
        "%s%d" % (chr(97 + choice[0][0]), choice[0][1] + 1),
        "%s%d" % (chr(97 + choice[1][0]), choice[1][1] + 1)),
        "score": 1.0 if completing else 0.6}]
