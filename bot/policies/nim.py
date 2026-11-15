"""Normal-play Nim policy using the Sprague-Grundy nim-sum."""

import re
import time

from .. import memory

STRATEGY_FILE = "nim.json"


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if re.search(r"\bnim\b", text) and re.search(r"\bpile", text) else 0.0


def _piles(text):
    pairs = re.findall(r"pile\s*(\d+)\s*[:=]\s*(\d+)", str(text or ""), re.I)
    if pairs:
        return [int(value) for _, value in sorted(pairs, key=lambda pair: int(pair[0]))]
    match = re.search(r"\bpiles?\s*[:=]\s*([0-9]+(?:\s+[0-9]+)+)", str(text or ""), re.I)
    if match:
        return [int(value) for value in match.group(1).split()]
    return []


def parse_state(rules, observation, variant=None):
    variant = variant if isinstance(variant, dict) else {}
    piles = variant.get("piles")
    if isinstance(piles, list) and all(str(value).isdigit() for value in piles):
        return [int(value) for value in piles]
    return _piles(observation) or _piles(rules)


def _action(piles, index, amount):
    return "take %d from pile %d" % (amount, index + 1)


def prep(ctx):
    try:
        game_id = ctx["game_id"]
        rules_hash = ctx["rules_hash"]
        memory.write_json_atomic(memory.variant_dir(game_id, rules_hash) + "/" + STRATEGY_FILE,
                                 {"version": 1, "method": "sprague-grundy"})
    except Exception:
        pass
    return {"method": "sprague-grundy"}


def decide(state, deadline):
    spec = state.get("spec")
    rules = getattr(spec, "rules", "") if spec else state.get("rules", "")
    turn = state.get("turn") or state
    piles = parse_state(rules, turn.get("observation", ""), turn.get("variant"))
    if not piles:
        return []
    used = set(re.findall(r"take\s+\d+\s+from\s+pile\s+\d+", turn.get("observation", ""), re.I))
    nim_sum = 0
    for pile in piles:
        nim_sum ^= pile
    moves = []
    if nim_sum:
        for index, pile in enumerate(piles):
            target = pile ^ nim_sum
            if target < pile:
                amount = pile - target
                moves.append((_action(piles, index, amount), 1.0))
    else:
        largest = max(range(len(piles)), key=lambda index: piles[index])
        if piles[largest]:
            moves.append((_action(piles, largest, 1), 0.5))
    for index, pile in enumerate(piles):
        if time.monotonic() >= deadline:
            break
        for amount in range(1, pile + 1):
            action = _action(piles, index, amount)
            if action.casefold() not in {item.casefold() for item in used}:
                moves.append((action, 0.2))
    return [{"action": action, "score": score} for action, score in moves]


def winning(piles):
    """Return whether the position is winning under normal-play Nim."""
    result = 0
    for pile in piles:
        result ^= int(pile)
    return result != 0
