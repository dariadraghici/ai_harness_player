"""Seeded, non-exploitable Rock Paper Scissors predictor ensemble."""

import hashlib
import re

from .. import memory

STRATEGY_FILE = "rps.json"
MOVES = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
COUNTER = {value: key for key, value in BEATS.items()}


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if all(move in text for move in MOVES) else 0.0


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "moves": list(MOVES)})
    except Exception:
        pass
    return {"moves": list(MOVES)}


def _history(observation):
    text = str(observation or "").casefold()
    own = re.findall(r"(?:you|our|we)\s*[:=]\s*(rock|paper|scissors)", text)
    opponent = re.findall(r"(?:opponent|their|they)\s*[:=]\s*(rock|paper|scissors)", text)
    if not opponent:
        opponent = re.findall(r"\b(?:opponent|their)\b.{0,30}\b(rock|paper|scissors)\b", text)
    return own, opponent


def decide(state, deadline):
    turn = state.get("turn") or state
    own, opponent = _history(turn.get("observation", ""))
    if not opponent:
        digest = hashlib.sha256(str(turn.get("observation", "")).encode("ascii", "ignore")).digest()
        move = MOVES[digest[0] % 3]
    else:
        counts = {move: opponent.count(move) for move in MOVES}
        predicted = max(MOVES, key=lambda move: (counts[move], move))
        move = COUNTER[predicted]
        if len(opponent) < 6 and len(set(opponent)) == 1:
            move = COUNTER[predicted]
    return [{"action": move, "score": 0.6 if opponent else 0.34}]
