"""Deterministic Wordle-style deduction and entropy selection."""

import math
import os
import re
import time
from collections import Counter, defaultdict

from ..interfaces import Candidate

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STRATEGY_FILE = "wordle.json"
_WORD_RE = re.compile(r"\bguess\s+([A-Za-z]+)", re.IGNORECASE)
_LENGTH_RE = re.compile(r"\b(\d+)\s*[- ]?letters?\b", re.IGNORECASE)
_PATTERN_RE = re.compile(r"\b[012]{3,20}\b")
_FEEDBACK_WORDS = {
    "green": 2, "correct": 2, "right": 2, "spot": 2,
    "yellow": 1, "present": 1, "misplaced": 1, "word": 1,
    "gray": 0, "grey": 0, "absent": 0, "wrong": 0, "missing": 0,
}


def is_wordle_rules(rules):
    text = str(rules or "").casefold()
    return bool(re.search(r"\bguess\b", text) and
                re.search(r"\b(?:green|yellow|gray|grey|feedback|right spot)\b", text))


def word_length(rules="", observation="", variant=None):
    texts = [str(rules or ""), str(observation or "")]
    if isinstance(variant, dict):
        texts.extend("%s %s" % (key, value) for key, value in variant.items())
    for text in texts:
        match = _LENGTH_RE.search(text)
        if match:
            return int(match.group(1))
    for text in texts:
        match = _WORD_RE.search(text)
        if match:
            return len(match.group(1))
    return None


def score(guess, secret):
    """Return Wordle feedback as a tuple: 2 green, 1 yellow, 0 gray."""
    guess = str(guess or "").casefold()
    secret = str(secret or "").casefold()
    if len(guess) != len(secret):
        raise ValueError("guess and secret must have the same length")
    result = [0] * len(guess)
    remaining = Counter()
    for index, letter in enumerate(secret):
        if guess[index] == letter:
            result[index] = 2
        else:
            remaining[letter] += 1
    for index, letter in enumerate(guess):
        if result[index] == 0 and remaining[letter]:
            result[index] = 1
            remaining[letter] -= 1
    return tuple(result)


def _words(path):
    out = []
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                word = line.strip().casefold()
                if re.fullmatch(r"[a-z]+", word):
                    out.append(word)
    except Exception:
        return []
    return list(dict.fromkeys(out))


def load_dictionary():
    allowed = _words(os.path.join(_DATA, "wordle_allowed.txt"))
    answers = _words(os.path.join(_DATA, "wordle_answers.txt"))
    if not answers:
        answers = list(allowed)
    if not allowed:
        allowed = list(answers)
    return allowed, answers


def allowed_word(word, length=None):
    allowed, _ = load_dictionary()
    word = str(word or "").casefold()
    return bool(re.fullmatch(r"[a-z]+", word) and
                (length is None or len(word) == int(length)) and word in allowed)


def _feedback_from_text(text, length):
    text = str(text or "").casefold()
    match = _PATTERN_RE.search(text)
    if match and len(match.group(0)) == length:
        return tuple(int(value) for value in match.group(0))
    values = []
    for token in re.findall(r"[a-z]+", text):
        if token in _FEEDBACK_WORDS:
            values.append(_FEEDBACK_WORDS[token])
    return tuple(values[-length:]) if len(values) >= length else None


def parse_observation(observation, rules="", variant=None):
    """Extract prior guesses and feedback from the current observation.

    The parser accepts the arena's prose labels, compact 0/1/2 patterns, and
    color-word rows. Unknown or truncated rows are retained as repeats but do
    not impose a false constraint.
    """
    text = str(observation or "")
    length = word_length(rules, text, variant)
    rows = []
    matches = list(_WORD_RE.finditer(text))
    for index, match in enumerate(matches):
        guess = match.group(1).casefold()
        if length is not None and len(guess) != length:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        feedback = _feedback_from_text(segment, len(guess))
        rows.append((guess, feedback))
    return {"length": length, "history": rows,
            "guesses": [guess for guess, _ in rows],
            "round": _round_info(text)}


def _round_info(text):
    match = re.search(r"\b(?:round|row|turn)\s+(\d+)(?:\s+of\s+(\d+))?", text, re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2)) if match.group(2) else None) if match else (None, None)


def filter_candidates(words, history):
    candidates = list(words)
    for guess, feedback in history:
        if feedback is not None:
            candidates = [word for word in candidates if score(guess, word) == feedback]
    return candidates


def _prior(words):
    return {word: 1.0 / (1.0 + index * 0.02) for index, word in enumerate(words)}


def _pattern(guess, secret):
    return score(guess, secret)


def _opener(words, length):
    pool = [word for word in words if len(word) == length]
    if not pool:
        return ""
    frequency = Counter(letter for word in pool for letter in set(word))
    return max(pool, key=lambda word: (sum(frequency[c] for c in set(word)),
                                       len(set(word)), word))[0: length]


def prepare_strategy(game_id, rules_hash):
    """Persist cheap opener and second-turn tables for a prepared variant."""
    try:
        from .. import memory
        allowed, answers = load_dictionary()
        lengths = sorted(set(len(word) for word in allowed))
        openers = {str(length): _opener(allowed, length) for length in lengths}
        second = {}
        for length in lengths:
            opener = openers.get(str(length), "")
            if not opener:
                continue
            secrets = [word for word in answers if len(word) == length]
            buckets = defaultdict(list)
            for secret in secrets:
                buckets["".join(str(value) for value in score(opener, secret))].append(secret)
            for pattern, group in buckets.items():
                if group:
                    second[opener + ":" + pattern] = max(
                        group, key=lambda word: (_prior(group).get(word, 0.0), word))
        path = os.path.join(memory.variant_dir(game_id, rules_hash), STRATEGY_FILE)
        memory.write_json_atomic(path, {"version": 1, "openers": openers, "second": second})
        return True
    except Exception:
        return False


def _probe_words(allowed, candidates, history, limit=300):
    used = set(guess for guess, _ in history)
    candidate_set = set(candidates)
    unknown = Counter(letter for word in candidates for letter in set(word))
    pool = [word for word in allowed if word not in candidate_set and word not in used]
    pool.sort(key=lambda word: (sum(unknown[c] for c in set(word)), len(set(word))), reverse=True)
    return pool[:limit]


def choose_guess(rules, observation, variant=None, deadline=None):
    allowed, answers = load_dictionary()
    parsed = parse_observation(observation, rules, variant)
    length = parsed["length"] or (len(answers[0]) if answers else None)
    allowed = [word for word in allowed if length is None or len(word) == length]
    answers = [word for word in answers if length is None or len(word) == length]
    history = parsed["history"]
    used = {guess for guess, _ in history}
    candidates = filter_candidates(answers, history)
    if len(candidates) == 1:
        return candidates[0], candidates, parsed
    if not history:
        opener = _opener(allowed, length)
        if opener:
            return opener, candidates, parsed
    if not candidates:
        candidates = [word for word in allowed if word not in used]
    if not candidates:
        candidates = [word for word in answers if word not in used]
    if not candidates:
        return _opener(allowed, length) or "a" * int(length or 1), candidates, parsed

    prior = _prior(candidates)
    options = list(candidates) if len(candidates) <= 6 else candidates + _probe_words(
        allowed, candidates, history)
    secrets = candidates if len(candidates) <= 3000 else candidates[:3000]
    total_weight = sum(prior.get(secret, 1.0) for secret in secrets)
    best = None
    best_key = None
    deadline = deadline or (time.monotonic() + 5.0)
    for guess in options:
        if time.monotonic() >= deadline:
            break
        buckets = defaultdict(float)
        for secret in secrets:
            buckets[_pattern(guess, secret)] += prior.get(secret, 1.0)
        entropy = 0.0
        for weight in buckets.values():
            probability = weight / total_weight
            entropy -= probability * math.log(probability, 2)
        value = entropy + (0.25 * prior.get(guess, 0.0) if guess in prior else 0.0)
        key = (value, prior.get(guess, 0.0), guess)
        if best_key is None or key > best_key:
            best, best_key = guess, key
    if best is None:
        best = candidates[0]
    return best, candidates, parsed


def candidates(spec, turn_msg, deadline):
    rules = getattr(spec, "rules", "") if spec else ""
    observation = str(turn_msg.get("observation") or "")
    guess, remaining, parsed = choose_guess(
        rules, observation, turn_msg.get("variant"), max(time.monotonic() + 0.1, deadline))
    used = set(parsed["guesses"])
    if guess in used:
        allowed, answers = load_dictionary()
        pool = filter_candidates(answers, parsed["history"]) or answers or allowed
        guess = next((word for word in pool if word not in used), guess)
    prefix = "guess "
    value = 1.0 if len(remaining) <= 1 else 0.9
    return [Candidate(prefix + guess, value, "wordle candidates=%d" % len(remaining))]
