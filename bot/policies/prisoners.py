"""Small opponent-model policy for the talking Prisoner's Dilemma."""

import re

from .. import memory

STRATEGY_FILE = "prisoners.json"


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "prisoner" in text and "cooperate" in text and "defect" in text else 0.0


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "policy": "forgiving-tit-for-tat"})
    except Exception:
        pass
    return {"policy": "forgiving-tit-for-tat"}


def _opponent_moves(observation):
    return re.findall(r"(?:opponent|their|they)\s*[:=]\s*(cooperate|defect)",
                      str(observation or ""), re.I)


def decide(state, deadline):
    turn = state.get("turn") or state
    moves = [move.casefold() for move in _opponent_moves(turn.get("observation", ""))]
    if len(moves) >= 3 and all(move == "defect" for move in moves[-3:]):
        action = "defect"
    elif moves and moves[-1] == "defect" and len(moves) >= 2 and moves[-2] == "defect":
        action = "defect"
    else:
        action = "cooperate"
    return [{"action": action, "score": 0.7 if action == "cooperate" else 0.6}]
