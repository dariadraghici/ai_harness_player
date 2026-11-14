"""Respectful chat tactics that support a move without explaining our plan."""

import re

_TACTICS = {
    "bluff": (
        "Ask a focused question that invites the opponent to reveal what they "
        "are weighing or intend to do. Sound confident or uncertain as useful, "
        "but never reveal your own move, rationale, private information, or plan."
    ),
    "deals": (
        "Ask which terms or outcomes matter most to the opponent, then use "
        "respectful leverage. Never reveal your private value, reservation point, "
        "or intended offer."
    ),
    "read_opponent": (
        "Ask a specific, neutral question about the opponent's intended pattern "
        "or next choice. Do not disclose your own strategy or explain your move."
    ),
    "guessing": (
        "Ask what clue, range, or approach the opponent is relying on. Do not "
        "give away your secret, feedback interpretation, or next guess."
    ),
    "think_ahead": (
        "Ask which threat or objective the opponent is prioritizing. Keep the "
        "question about their thinking; never narrate your move, threat, or plan."
    ),
    "truth": (
        "Ask which detail the opponent thinks is relevant. Never assert a fact "
        "unless directly supported by the document; do not reveal private information."
    ),
    "guardrails": (
        "Ask a short, neutral question about the opponent's priorities. Keep it "
        "within the speech restrictions; never use a forbidden subject or alias."
    ),
}

_FALLBACKS = {
    "bluff": (
        "What are you weighing most before your next choice?",
        "What would make you commit to a particular approach?",
    ),
    "deals": (
        "Which term matters most to you here?",
        "What outcome would make this worthwhile for you?",
    ),
    "read_opponent": (
        "What pattern are you trying to establish this round?",
        "What do you expect the next choice to reveal?",
    ),
    "guessing": (
        "Which clue is shaping your next guess?",
        "What kind of feedback would change your approach?",
    ),
    "think_ahead": (
        "Which objective are you prioritizing in this position?",
        "Which threat do you think matters most right now?",
    ),
    "truth": (
        "Which detail in the document do you think matters most?",
        "What part of the text should we examine more closely?",
    ),
    "guardrails": (
        "Which part of the game should we focus on?",
        "What would you like to clarify about the game?",
    ),
}

_DEFAULT = (
    "What are you prioritizing in this game?",
    "What do you expect to try next?",
)

_PLAN_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:i|we)\s+(?:will|plan to|intend to|am going to|are going to|aim to|"
    r"expect to|choose to)\b",
    r"\b(?:i['’]m|we['’]re)\s+(?:going to|planning to|aiming to|opening|"
    r"playing|moving|placing|taking|building|blocking|targeting)\b",
    r"\b(?:i|we)\s+(?:am|are)\s+(?:opening|playing|moving|placing|taking|"
    r"building|blocking|targeting)\b",
    r"\b(?:i|we)(?:['’]ll)?\s+(?:open|start|begin|play|move|place|take|"
    r"choose|build|block|target)\s+(?:in|at|on|to|from|with|the|a|my|our)\b",
    r"\b(?:my|our)\s+(?:next\s+)?(?:move|plan|strategy|approach|line|threat|"
    r"diagonal|row|column)\b",
    r"\b(?:blocking|building|setting up|creating|aiming for|moving toward)\b"
    r".{0,80}\b(?:my|our)\s+(?:line|threat|diagonal|row|column|position)\b",
))
_ACTION_NARRATION = re.compile(
    r"\b(?:for me|i(?:'m| am| will)?|we(?:'re| are)?|my|our|play(?:ing)?|"
    r"place(?:ing)?|move|corner|take|choose|chose)\b",
    re.I,
)


def reveals_plan(text, action=None):
    """Detect chat that states our intent or narrates the selected move."""
    line = " ".join(str(text or "").split())
    if not line:
        return False
    if any(pattern.search(line) for pattern in _PLAN_PATTERNS):
        return True

    action = " ".join(str(action or "").split())
    if len(action) < 2 or not _ACTION_NARRATION.search(line):
        return False
    action_pattern = r"(?<!\w)" + r"\s+".join(re.escape(x) for x in action.split()) + r"(?!\w)"
    return re.search(action_pattern, line, re.I) is not None


def tactics(family):
    """Return family-specific guidance for eliciting information without leaking."""
    return _TACTICS.get(
        family,
        "Ask one focused question to learn the opponent's priorities. Never reveal "
        "or describe your move, rationale, private information, or plan.",
    )


def fallback_line(family, turn_msg=None):
    """Return a short line that does not disclose a move or private state."""
    lines = _FALLBACKS.get(family, _DEFAULT)
    turn = (turn_msg or {}).get("turn")
    try:
        index = int(turn or 0) % len(lines)
    except (TypeError, ValueError):
        index = 0
    return lines[index]
