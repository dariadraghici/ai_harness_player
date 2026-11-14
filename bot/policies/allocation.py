"""Deterministic simultaneous troop-allocation policy."""

import re
import time


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.95 if (re.search(r"\b(?:troops?|soldiers?)\b", text)
                    and re.search(r"\bfields?\b|\bbattlefields?\b", text)) else 0.0


def prep(ctx):
    return {}


def _numbers(text):
    return [int(value) for value in re.findall(r"\b\d+\b", str(text or ""))]


def parse_state(rules, observation, variant=None):
    text = "%s\n%s" % (rules or "", observation or "")
    variant = variant if isinstance(variant, dict) else {}
    total = next((int(variant[key]) for key in ("troops", "total", "soldiers")
                  if str(variant.get(key, "")).isdigit()), None)
    fields = next((int(variant[key]) for key in ("fields", "battlefields", "count")
                   if str(variant.get(key, "")).isdigit()), None)
    match = re.search(r"\b(\d+)\s+(?:troops?|soldiers?)\b", text, re.I)
    if total is None and match:
        total = int(match.group(1))
    match = re.search(r"\b(\d+)\s+(?:battle)?fields?\b", text, re.I)
    if fields is None and match:
        fields = int(match.group(1))
    worths = None
    for line in str(observation or "").splitlines():
        if re.search(r"\b(?:worth|value|values|points?)\b", line, re.I):
            values = _numbers(line)
            if fields and len(values) >= fields:
                worths = values[-fields:]
                break
    if worths is None:
        for line in str(rules or "").splitlines():
            if re.search(r"\b(?:worth|value|values|points?)\b", line, re.I):
                values = _numbers(line)
                if fields and len(values) >= fields:
                    worths = values[-fields:]
                    break
    if fields is None and worths:
        fields = len(worths)
    return total, fields, worths or ([1] * fields if fields else [])


def _history(observation, fields):
    if not fields:
        return []
    rows = []
    for line in str(observation or "").splitlines():
        values = _numbers(line)
        if len(values) >= fields and not re.search(
                r"\b(?:worth|value|round|troops?|soldiers?)\b", line, re.I):
            row = tuple(values[-fields:])
            if sum(row) > 0:
                rows.append(row)
    return rows


def _target_subsets(worths):
    threshold = sum(worths) / 2.0
    subsets = []
    for mask in range(1, 1 << len(worths)):
        value = sum(worths[i] for i in range(len(worths)) if mask & (1 << i))
        if value > threshold:
            indexes = [i for i in range(len(worths)) if mask & (1 << i)]
            subsets.append((len(indexes), value, indexes))
    return sorted(subsets, key=lambda item: (item[0], -item[1], item[2]))


def _allocation(total, worths, indexes, rotation=0):
    values = [0] * len(worths)
    if not indexes:
        values[0] = total
        return tuple(values)
    weights = [max(1, worths[index]) for index in indexes]
    for offset, index in enumerate(indexes):
        values[index] = total * weights[offset] // sum(weights)
    for offset in range(total - sum(values)):
        values[indexes[(offset + rotation) % len(indexes)]] += 1
    return tuple(values)


def round_winner(ours, opponent, worths):
    """Return 1, -1, or 0 using strict field majorities and field worths."""
    if len(ours) != len(opponent) or len(ours) != len(worths):
        return 0
    own = sum(worth for value, other, worth in zip(ours, opponent, worths) if value > other)
    theirs = sum(worth for value, other, worth in zip(ours, opponent, worths) if other > value)
    return 1 if own > theirs else -1 if theirs > own else 0


def decide(state, deadline):
    spec = state.get("spec")
    rules = getattr(spec, "rules", "") if spec else state.get("rules", "")
    turn = state.get("turn") or state
    total, fields, worths = parse_state(
        rules, turn.get("observation", ""), turn.get("variant"))
    if not total or not fields or len(worths) != fields:
        return []
    used = set(_history(turn.get("observation", ""), fields))
    candidates = []
    for rotation, (_, _, indexes) in enumerate(_target_subsets(worths)):
        if time.monotonic() >= deadline:
            break
        row = _allocation(total, worths, indexes, rotation)
        if row not in used:
            candidates.append({"action": " ".join(str(value) for value in row),
                               "score": 1.0 - rotation * 0.01})
    if not candidates:
        row = _allocation(total, worths, list(range(fields)))
        if row in used:
            row = _allocation(total, worths, list(range(fields)), 1)
        candidates.append({"action": " ".join(str(value) for value in row), "score": 0.5})
    return candidates
