"""Forbidden-subject extraction and outgoing filtering for Guardrails games."""

import re

from .base import BaseEngine, register

_EXTRACTION_PATTERNS = (
    re.compile(
        r"\b(?:forbidden|banned|restricted)\s+"
        r"(?:subject|topic|word|phrase)\s*"
        r"(?:(?:is|called|named)\s+|[:=]\s*)([^\n:.;]+)",
        re.I,
    ),
    re.compile(
        r"\b(?:never|must not|must never|do not|don't)\s+(?:ever\s+)?"
        r"(?:say|mention|discuss|name|refer to)\s+(?:the\s+)?"
        r"(?:(?:forbidden|restricted)\s+)?(?:subject|topic)\s+of\s+([^\n.;]+)",
        re.I,
    ),
    re.compile(
        r"\b(?:never|must not|must never|do not|don't)\s+(?:ever\s+)?"
        r"(?:say|mention|discuss|name|refer to)\s+"
        r"[\"'`]?([A-Z][A-Za-z0-9' -]{0,60}?"
        r"(?:\s*\([^)]{1,100}\))?)[\"'`]?"
        r"(?=[,.!?;:\n]|\s+even\b|\s+under\b|$)",
        re.I,
    ),
    re.compile(
        r"\b(?:avoid|forbidden from)\s+(?:saying|mentioning|discussing)\s+"
        r"([^\n.;]+)",
        re.I,
    ),
)
_GENERIC_TERMS = {
    "it", "that", "this", "the subject", "the topic", "the forbidden subject",
    "the forbidden topic", "anything forbidden", "any forbidden subject",
    "any forbidden topic", "the restricted subject", "the restricted topic",
}
_SYNONYM_SPLIT = re.compile(
    r"\s*(?:\||/|,|;|\bor\b)\s*",
    re.I,
)
_ALIAS_MARKER = re.compile(r"\b(?:also known as|also called|aka|synonyms?:)\b", re.I)
_ALIAS_LIST_SPLIT = re.compile(r"\s*(?:\||/|,|;|\bor\b|\band\b)\s*", re.I)


def _normalize(text):
    return " ".join(str(text or "").casefold().split())


def _split_subjects(value):
    value = str(value or "").strip().strip(" \t\r\n\"'`[](){}")
    value = re.sub(
        r"^(?:the\s+)?(?:(?:forbidden|banned|restricted)\s+)?"
        r"(?:subject|topic|word|phrase)\s+(?:(?:is|called|named)\s+)?",
        "",
        value,
        flags=re.I,
    )
    value = re.split(
        r",\s*(?:even if|whether|because|if asked|when asked|for any reason)\b",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]
    if not value:
        return []
    marker = _ALIAS_MARKER.search(value)
    if marker:
        primary = value[:marker.start()]
        aliases = value[marker.end():]
        chunks = [primary] + _ALIAS_LIST_SPLIT.split(aliases)
    else:
        chunks = _SYNONYM_SPLIT.split(value)
    result = []
    for part in chunks:
        term = part.strip(" \t\r\n\"'`[](){}:,-")
        key = _normalize(term)
        if (not key or key in _GENERIC_TERMS or len(key) > 80
                or len(key.split()) > 10):
            continue
        if key not in {_normalize(x) for x in result}:
            result.append(term)
    return result


def extract_forbidden(rules_text):
    """Extract explicitly named forbidden terms and aliases from game rules."""
    text = str(rules_text or "")
    terms = []
    for pattern in _EXTRACTION_PATTERNS:
        for match in pattern.finditer(text):
            for term in _split_subjects(match.group(1)):
                if _normalize(term) not in {_normalize(x) for x in terms}:
                    terms.append(term)
    return terms


def _profile_terms(profile):
    if profile is None:
        return []
    raw = getattr(profile, "speech_restrictions", "")
    if isinstance(raw, (list, tuple, set)):
        values = [str(x) for x in raw]
    else:
        raw = str(raw or "").strip()
        if not raw:
            return []
        values = extract_forbidden(raw)
        if not values and not re.search(
            r"\b(?:never|must not|must never|do not|don't|forbidden|restricted)\b",
            raw,
            re.I,
        ):
            values = _split_subjects(raw)
    terms = []
    for value in values:
        for term in _split_subjects(value):
            if _normalize(term) not in {_normalize(x) for x in terms}:
                terms.append(term)
    return terms


def prepare_profile(profile, rules_text):
    """Supplement a classifier's restriction with explicit terms from the rules."""
    if profile is None:
        return
    terms = _profile_terms(profile) + extract_forbidden(rules_text)
    unique = []
    for term in terms:
        if _normalize(term) not in {_normalize(x) for x in unique}:
            unique.append(term)
    if unique:
        profile.speech_restrictions = " | ".join(unique)


def _matches(text, terms):
    folded = str(text or "").casefold()
    for term in terms:
        words = [re.escape(x) for x in _normalize(term).split()]
        if not words:
            continue
        pattern = r"(?<!\w)" + r"[\W_]+".join(words) + r"(?!\w)"
        if re.search(pattern, folded, re.UNICODE):
            return True
    return False


def safe_chat(profile=None):
    """Choose a short generic rewrite that avoids all known restricted terms."""
    terms = _profile_terms(profile)
    for candidate in (
        "Let's keep this focused on the game.",
        "Good luck; I am ready to continue.",
        "I am ready for the next turn.",
        "...",
    ):
        if not _matches(candidate, terms):
            return candidate
    return "..."


def filter_outgoing(text, kind="chat", profile=None):
    """Block forbidden output; rewrite chat safely and reject blocked actions."""
    if not isinstance(text, str):
        return ""
    terms = _profile_terms(profile)
    if not terms or not _matches(text, terms):
        return text
    if kind == "action":
        return ""
    return safe_chat(profile)


class GuardrailsEngine(BaseEngine):
    family = "guardrails"

    def candidates(self, specialist, turn_msg, deadline):
        # Guardrails primarily constrain the L2 answer through filter_outgoing.
        raise NotImplementedError("guardrails engine has no standalone move policy")


register(GuardrailsEngine())
