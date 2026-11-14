"""Literal evidence checks and a conservative answer policy for Truth games."""

import re
import time

from .base import BaseEngine, register
from ..interfaces import Candidate

_DOCUMENT_HEADER = re.compile(
    r"(?:^|\n)\s*(?:source\s+)?(?:document|passage|source\s+text)\s*:\s*",
    re.I,
)
_SECTION_HEADER = re.compile(
    r"\n\s*(?:question|your question|task|answer format|instructions?)\s*:",
    re.I,
)
_UNKNOWN_WORDS = re.compile(
    r"\b(?:unknown|not\s+(?:stated|given|provided|known|available)|"
    r"cannot\s+be\s+(?:determined|answered)|can't\s+be\s+determined|"
    r"no\s+answer|i\s+don't\s+know|cannot\s+answer|can't\s+answer)\b",
    re.I,
)


def extract_document(observation):
    """Return the document section, excluding the question when it is labeled."""
    text = str(observation or "")
    header = _DOCUMENT_HEADER.search(text)
    if header:
        body = text[header.end():]
        end = _SECTION_HEADER.search(body)
        return body[:end.start() if end else len(body)].strip()

    question = _SECTION_HEADER.search(text)
    if question:
        return text[:question.start()].strip()
    lines = text.splitlines()
    if len(lines) > 1 and "?" in lines[-1]:
        return "\n".join(lines[:-1]).strip()
    return text.strip()


def find_support(document, answer):
    """Return the exact document substring supporting an answer, or None.

    This deliberately checks textual evidence only; it does not treat a
    paraphrase or a model's confidence as proof.
    """
    doc = str(document or "")
    candidate = str(answer or "").strip()
    if not doc or not candidate:
        return None
    pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(part) for part in candidate.split()
    ) + r"(?!\w)"
    match = re.search(pattern, doc, re.UNICODE)
    return match.group(0) if match else None


def _quoted_unknown(rules_text):
    text = str(rules_text or "")
    patterns = (
        r"\b(?:answer|respond|say|return|use|reply)\s+"
        r"(?:(?:with|using)\s+)?(?:exactly\s+)?(?:[:=]\s*)?"
        r"['\"]([^'\"\n]{1,100})['\"]",
        r"\bunknown\s+(?:answer|response)\s*(?:is|:)\s*['\"]([^'\"\n]{1,100})['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip()
    plain = re.search(
        r"\b(?:answer|respond|say|return|use|reply)\s+"
        r"(?:(?:with|using)\s+)?(?:exactly\s+)?(?:the\s+)?(?:word\s+)?"
        r"(unknown|not\s+stated|not\s+provided|not\s+given|"
        r"cannot\s+be\s+determined|can't\s+be\s+determined|"
        r"i\s+don't\s+know|cannot\s+answer|can't\s+answer)\b",
        text,
        re.I,
    )
    if plain:
        return " ".join(plain.group(1).split())
    for quote in re.findall(r"""["'`]([^"'`\n]{1,100})["'`]""", text):
        if _UNKNOWN_WORDS.search(quote):
            return quote.strip()
    return None


def unknown_action(rules_text, legal_actions=None):
    """Find the exact unknown response specified by the rules or legal list."""
    phrase = _quoted_unknown(rules_text)
    if phrase:
        if isinstance(legal_actions, list):
            for action in legal_actions:
                if isinstance(action, str) and action.casefold() == phrase.casefold():
                    return action
            return None
        return phrase
    if isinstance(legal_actions, list):
        for action in legal_actions:
            if isinstance(action, str) and _UNKNOWN_WORDS.search(action):
                return action
    return None


def supported_actions(spec, turn_msg):
    """List legal answers that occur verbatim in the current document."""
    observation = turn_msg.get("observation", "")
    document = extract_document(observation)
    legal = turn_msg.get("legal_actions")
    if not isinstance(legal, list):
        return []
    result = []
    for action in legal:
        if not isinstance(action, str):
            continue
        if _UNKNOWN_WORDS.search(action):
            continue
        quote = find_support(document, action)
        if quote:
            result.append(Candidate(action=action, value=1.0, note="document quote: " + quote[:180]))
    return result


def enforce_answer(action, spec, turn_msg):
    """Accept only a quoted answer, otherwise use the exact unknown response."""
    observation = turn_msg.get("observation", "")
    document = extract_document(observation)
    legal = turn_msg.get("legal_actions")
    legal = legal if isinstance(legal, list) else None
    rules = spec.rules if spec else ""
    proposed = str(action or "").strip()

    if legal:
        if proposed in legal and find_support(document, proposed):
            return proposed
    else:
        quote = find_support(document, proposed)
        if quote:
            return quote
    return unknown_action(rules, legal)


class TruthEngine(BaseEngine):
    family = "truth"

    def candidates(self, specialist, turn_msg, deadline):
        if time.monotonic() >= deadline:
            return []
        supported = supported_actions(specialist, turn_msg)
        if supported:
            return supported
        unknown = unknown_action(
            specialist.rules,
            turn_msg.get("legal_actions") if isinstance(turn_msg.get("legal_actions"), list) else None,
        )
        return [Candidate(action=unknown, value=0.0, note="no literal supporting quote")] if unknown else []


register(TruthEngine())
