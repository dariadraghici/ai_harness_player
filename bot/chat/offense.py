"""Respectful chat tactics that support a move without explaining our plan."""

_TACTICS = {
    "bluff": (
        "Project confidence or doubt that fits your move, but do not reveal or "
        "describe private information, the move's rationale, or your next plan."
    ),
    "deals": (
        "Use respectful leverage: emphasize mutual value, patience, or the cost "
        "of a poor deal. Do not reveal your private value or reservation point."
    ),
    "read_opponent": (
        "Use light pattern talk to influence the opponent, but do not disclose "
        "your actual strategy or explain the move you selected."
    ),
    "guessing": (
        "Stay friendly and give away no secret, feedback interpretation, or "
        "next guess. Do not state how close either player is."
    ),
    "think_ahead": (
        "Sound calm and confident without narrating the move, threat, or plan "
        "you selected."
    ),
    "truth": (
        "Stay measured. Never assert a fact unless it is directly supported by "
        "the supplied document; do not reveal private information."
    ),
    "guardrails": (
        "Keep chat brief and focused on the game. Never use the forbidden "
        "subject or a synonym, even to quote or deny it."
    ),
}

_FALLBACKS = {
    "bluff": (
        "I am comfortable with where this is going.",
        "There is more to this position than meets the eye.",
    ),
    "deals": (
        "There is room to make this worthwhile for both of us.",
        "A little patience can help us find better terms.",
    ),
    "read_opponent": (
        "Patterns can be misleading; I am watching closely.",
        "I wonder whether this round will follow the usual pattern.",
    ),
    "guessing": (
        "Your next move may tell us more than you think.",
        "This is an interesting puzzle; let's see what you reveal.",
    ),
    "think_ahead": (
        "This position still has plenty of possibilities.",
        "I am enjoying the challenge so far.",
    ),
    "truth": (
        "I will stick to what the evidence supports.",
        "Let's see what the document actually tells us.",
    ),
    "guardrails": (
        "Let's keep this focused on the game.",
        "Good luck; I am ready to continue.",
    ),
}

_DEFAULT = (
    "Good game so far. Let us see how this plays out.",
    "Good luck; I am ready to continue.",
)


def tactics(family):
    """Return compact family-specific chat guidance for the decision prompt."""
    return _TACTICS.get(family, "Keep chat brief, respectful, and in service of your move.")


def fallback_line(family, turn_msg=None):
    """Return a short line that does not disclose a move or private state."""
    lines = _FALLBACKS.get(family, _DEFAULT)
    turn = (turn_msg or {}).get("turn")
    try:
        index = int(turn or 0) % len(lines)
    except (TypeError, ValueError):
        index = 0
    return lines[index]
