"""Chat tactics per family.

Owner: luckydevs-2. Status: stub.

Done means: per family, guidance for the decision prompt (tactics()) that
makes our chat line serve the move: bluffs consistent with the move in Bluff,
offers and pressure in Deals, misleading pattern talk in Read the opponent,
calm confidence elsewhere; plus fallback_line() when the model is not
available. Always respectful: no insults, hate, harassment, real people.
"""

_TACTICS = {
    "bluff": "Project confidence that fits a strong hand when betting, doubt when checking; never state your real private cards.",
    "deals": "Anchor high, concede slowly, praise cooperation, never reveal your private value or reservation price.",
    "read_opponent": "Talk about a pattern you are NOT going to follow; sound predictable while staying unpredictable.",
    "guessing": "Keep it short and friendly; never reveal your secret or how close the opponent is.",
    "think_ahead": "Sound calm and confident; hint at threats; never explain your real plan.",
    "truth": "Be precise and factual; never claim facts that are not in the document.",
    "guardrails": "Keep chat short and on the game; never touch the forbidden subject in any form.",
}

_FALLBACK = {
    "bluff": "I like my chances this round.",
    "deals": "I am open to a fair deal that works for both of us.",
    "read_opponent": "I think I have figured out your pattern.",
    "think_ahead": "Interesting position. I see a plan here.",
}


def tactics(family):
    """One line of chat tactics for the decision prompt."""
    return _TACTICS.get(family, "Keep chat short, friendly, and in service of your move.")


def fallback_line(family, turn_msg=None):
    """A short line when the model is not available. Must be safe in any game."""
    # TODO(luckydevs-2): vary lines; keep them guardrails-safe (generic words only).
    return _FALLBACK.get(family, "Good game so far. Let us see how this plays out.")
