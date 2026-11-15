"""Deterministic four-distinct-digit Bulls and Cows solver."""

import itertools
import math
import re
import time
from functools import lru_cache

from .. import memory

STRATEGY_FILE = "bulls_cows.json"
ALL_CODES = tuple("".join(item) for item in itertools.permutations("0123456789", 4))


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "bull" in text and "cow" in text else 0.0


@lru_cache(maxsize=200000)
def feedback(guess, secret):
    guess, secret = str(guess), str(secret)
    bulls = sum(left == right for left, right in zip(guess, secret))
    cows = sum(min(guess.count(digit), secret.count(digit)) for digit in set(guess)) - bulls
    return bulls, cows


def _history(observation):
    rows = []
    text = str(observation or "")
    matches = list(re.finditer(r"\bguess\s+([0-9]{4})\b", text, re.I))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        pair = re.search(r"(\d+)\s*bull\w*[^0-9]+(\d+)\s*cow\w*", segment, re.I)
        if pair is None:
            pair = re.search(r"\b([0-4])\s*[,/]\s*([0-4])\b", segment)
        rows.append((match.group(1), (int(pair.group(1)), int(pair.group(2))) if pair else None))
    return rows


def parse_state(observation):
    history = _history(observation)
    candidates = list(ALL_CODES)
    for guess, result in history:
        if result is not None:
            candidates = [code for code in candidates if feedback(guess, code) == result]
    return history, candidates


def prep(ctx):
    try:
        path = memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE
        memory.write_json_atomic(path, {"version": 1, "first": "0123"})
    except Exception:
        pass
    return {"first": "0123"}


def _choose(candidates, used, deadline):
    if not candidates:
        return next(code for code in ALL_CODES if code not in used)
    if len(candidates) <= 2:
        return candidates[0]
    pool = list(candidates)
    if len(candidates) > 80:
        pool += [code for code in ALL_CODES[::max(1, len(ALL_CODES) // 500)] if code not in used]
    best = None
    best_key = None
    for guess in pool:
        if time.monotonic() >= deadline:
            break
        buckets = {}
        for secret in candidates:
            key = feedback(guess, secret)
            buckets[key] = buckets.get(key, 0) + 1
        worst = max(buckets.values())
        entropy = -sum((count / len(candidates)) * math.log(count / len(candidates), 2)
                       for count in buckets.values())
        key = (worst, -entropy, guess not in candidates, guess)
        if best_key is None or key < best_key:
            best, best_key = guess, key
    return best or next(code for code in ALL_CODES if code not in used)


def decide(state, deadline):
    turn = state.get("turn") or state
    history, candidates = parse_state(turn.get("observation", ""))
    used = {guess for guess, _ in history}
    if not history:
        guess = "0123"
    else:
        guess = _choose(candidates, used, deadline)
    if guess in used:
        guess = next((code for code in ALL_CODES if code not in used), "0123")
    return [{"action": "guess " + guess, "score": 1.0 if len(candidates) <= 2 else 0.8}]
