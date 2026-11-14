"""Conservative bargaining and auction bounds for Deals-family games."""

import re
import time

from .base import BaseEngine, register
from ..interfaces import Candidate

_NUMBER = r"(-?\d+(?:\.\d+)?)"
_VALUE_PATTERNS = (
    re.compile(
        r"\b(?:your|our|my)\s+(?:(?:private|reservation|walk[- ]away|minimum|maximum)\s+)?"
        r"(?:value|valuation|worth|limit|price)\s*(?:is|:|=)?\s*\$?\s*" + _NUMBER,
        re.I,
    ),
    re.compile(
        r"\b(?:reservation(?:\s+value|\s+price)?|walk[- ]away(?:\s+(?:value|price))?|"
        r"minimum\s+acceptable(?:\s+(?:value|price))?|maximum\s+(?:bid|price))"
        r"\s*(?:is|:|=)?\s*\$?\s*" + _NUMBER,
        re.I,
    ),
)
_OFFER_PATTERNS = (
    re.compile(
        r"\b(?:current\s+)?(?:opponent'?s?\s+|other\s+player'?s?\s+|"
        r"seller'?s?\s+|buyer'?s?\s+)?(?:offer|proposal|bid)"
        r"(?:ed|s|ding)?\s*(?:of|is|:|=)?\s*\$?\s*" + _NUMBER,
        re.I,
    ),
    re.compile(
        r"\b(?:opponent|other player|seller|buyer)\s+(?:offers?|proposes?|bids?)"
        r"\s*\$?\s*" + _NUMBER,
        re.I,
    ),
)


def _role(text):
    match = re.search(r"\b(?:you are|your role is|role:)\s*(buyer|seller)\b", text, re.I)
    if match:
        return match.group(1).casefold()
    if re.search(r"\byou are the buyer\b|\byour role is buyer\b", text, re.I):
        return "buyer"
    if re.search(r"\byou are the seller\b|\byour role is seller\b", text, re.I):
        return "seller"
    return None


def _private_limit(text):
    for pattern in _VALUE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _current_offer(observation):
    for pattern in _OFFER_PATTERNS:
        match = pattern.search(str(observation or ""))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _action_kind(action, rules):
    text = str(action or "").casefold()
    if re.search(r"\b(?:accept|agree|deal|take it)\b", text):
        return "accept"
    if re.search(r"\b(?:reject|decline|walk away|no deal)\b", text):
        return "reject"
    if re.search(r"\b(?:offer|propose|bid)\b", text):
        return "offer"
    if re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", text) and re.search(
        r"\b(?:auction|bid|bidding)\b", rules, re.I
    ):
        return "bid"
    return None


def _action_amount(action):
    match = re.search(_NUMBER, str(action or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _candidate_set(spec, turn_msg):
    rules = str(getattr(spec, "rules", "") or "")
    observation = str(turn_msg.get("observation") or "")
    limit = _private_limit(observation)
    if limit is None:
        limit = _private_limit(rules)
    role = _role(observation) or _role(rules)
    offer = _current_offer(observation)
    legal = turn_msg.get("legal_actions")
    if limit is None or role not in ("buyer", "seller") or not isinstance(legal, list):
        return []

    candidates = []
    for action in legal:
        if not isinstance(action, str):
            continue
        kind = _action_kind(action, rules)
        if kind == "accept" and offer is not None:
            surplus = limit - offer if role == "buyer" else offer - limit
            if surplus >= 0:
                scale = max(abs(limit), abs(offer), 1.0)
                candidates.append(Candidate(action, 0.6 + min(0.4, surplus / scale), "offer meets our limit"))
        elif kind == "reject":
            candidates.append(Candidate(action, 0.0, "walk away from this offer"))
        elif kind in ("offer", "bid"):
            amount = _action_amount(action)
            if amount is None:
                continue
            surplus = limit - amount if role == "buyer" else amount - limit
            if surplus < 0:
                continue
            turn = turn_msg.get("turn", 1)
            try:
                progress = min(1.0, max(0.0, (int(turn) - 1) / 8.0))
            except (TypeError, ValueError):
                progress = 0.0
            if role == "buyer":
                target = limit * (0.65 + 0.25 * progress)
                if offer is not None:
                    target = min(limit, max(target, (target + offer) / 2.0))
            else:
                target = limit * (1.35 - 0.25 * progress)
                if offer is not None:
                    target = max(limit, min(target, (target + offer) / 2.0))
            scale = max(abs(limit), 1.0)
            score = 0.4 - min(0.35, abs(amount - target) / scale)
            candidates.append(Candidate(action, score, "within our value limit; target %.2f" % target))

    candidates.sort(key=lambda c: c.value, reverse=True)
    return candidates


class DealsEngine(BaseEngine):
    family = "deals"

    def candidates(self, specialist, turn_msg, deadline):
        if time.monotonic() >= deadline:
            return []
        return _candidate_set(specialist, turn_msg)


register(DealsEngine())
