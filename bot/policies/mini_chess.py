"""Grammar-safe Mini Chess policy using arena legal actions as authority."""

import re

from .. import memory

STRATEGY_FILE = "mini_chess.json"


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if ("gardner" in text or "mini chess" in text) and "5x5" in text else 0.0


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "board": "5x5", "move_cap": 80})
    except Exception:
        pass
    return {"board": "5x5", "move_cap": 80}


def decide(state, deadline):
    turn = state.get("turn") or state
    legal = turn.get("legal_actions")
    if isinstance(legal, list):
        moves = [str(move) for move in legal if re.fullmatch(r"[a-e][1-5][a-e][1-5]", str(move), re.I)]
        if moves:
            return [{"action": move, "score": 0.7} for move in moves]
    matches = re.findall(r"\b([a-e][1-5][a-e][1-5])\b",
                         str(turn.get("observation", "")), re.I)
    if matches:
        return [{"action": matches[0], "score": 0.2}]
    return [{"action": "a1a2", "score": 0.0}]
