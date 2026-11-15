"""The closed capability vocabulary of the engine subsystem (TASK section 2/4).

Owner: luckydevs-3 area. Status: real. Standard library only.

A capability TAG says what kind of computation would win this game, never which game it is. Tags are the only
thing that reaches the catalog, the PyPI lookups and the search queries, so no game content can leak into the
network layer (rulebook 7.9/7.11): `tags_for()` is a pure function of the profile that prep already built, and
`query_terms()` returns phrases from a FIXED vocabulary that is written out in this file.

No game-specific code lives here (rulebook 3.7): the mapping is per family and per profile flag, and the
keyword evidence below is about computation ("dice", "auction", "document"), never about a named game.
"""

import re

# ---------------------------------------------------------------- the closed tag vocabulary
TAGS = (
    "perfect_info_search",        # exact alpha-beta / negamax / MCTS on a fully observable state
    "imperfect_info_cfr",         # CFR / sequence-form LP for hidden information
    "matrix_game_lp",             # Nash equilibrium of a (bi)matrix game
    "simultaneous_exploitation",  # opponent modelling and regret learning in repeated simultaneous play
    "feedback_deduction",         # candidate filtering plus entropy / minimax guessing
    "dice_expectimax",            # expectiminimax / value iteration with chance nodes
    "bargaining_auction",         # backward induction, Bayes-Nash, best response in money space
    "document_qa",                # retrieval plus extractive answering with an "unknown" default
    "text_guardrail",             # deterministic outgoing-text filtering
    "external_binary_engine",     # a baked binary engine driven over a text protocol
)

# Family -> tags. The families are the ones of bot/interfaces.py FAMILIES.
FAMILY_TAGS = {
    "think_ahead": ("perfect_info_search",),
    "bluff": ("imperfect_info_cfr",),
    "read_opponent": ("matrix_game_lp", "simultaneous_exploitation"),
    "guessing": ("feedback_deduction",),
    "deals": ("bargaining_auction",),
    "truth": ("document_qa",),
    "guardrails": ("text_guardrail",),
    "unknown": (),
}

# Generic computation words in a rules text that add a tag. Never a game name, never a strategy.
EVIDENCE = (
    ("dice_expectimax", re.compile(r"\b(?:dice|die|roll(?:s|ed|ing)?|d6|random(?:ly)? draw|coin flip|chance)\b", re.I)),
    ("matrix_game_lp", re.compile(r"\b(?:simultaneous(?:ly)?|at the same time|both players choose|reveal(?:ed)? "
                                  r"together|hidden until)\b", re.I)),
    ("feedback_deduction", re.compile(r"\b(?:feedback|clue|higher or lower|how many|guess(?:es|ing)?|deduce|"
                                      r"narrow(?:s|ing)? down)\b", re.I)),
    ("bargaining_auction", re.compile(r"\b(?:offer|bid|auction|negotiat\w+|split|accept or reject|price|payoff "
                                      r"table)\b", re.I)),
    ("document_qa", re.compile(r"\b(?:document|passage|text below|according to the text|quote|excerpt|answer "
                               r"from the)\b", re.I)),
    ("text_guardrail", re.compile(r"\b(?:never (?:say|mention)|forbidden (?:subject|topic|word)|must not "
                                  r"mention|restricted (?:subject|topic))\b", re.I)),
    ("imperfect_info_cfr", re.compile(r"\b(?:hidden card|private (?:card|value|hand)|bluff\w*|your hand|face "
                                      r"down|secretly)\b", re.I)),
)

# The FIXED query vocabulary. A search query may only be built from these phrases (net.query_terms_ok).
# One list per tag, plus the shared words. Nothing here depends on any game.
SHARED_TERMS = ("python", "library", "package", "pypi", "algorithm", "solver")
TAG_TERMS = {
    "perfect_info_search": ("alpha-beta search", "negamax", "minimax", "transposition table",
                            "monte carlo tree search", "mcts", "game tree search", "board game engine"),
    "imperfect_info_cfr": ("counterfactual regret minimization", "cfr", "imperfect information game",
                           "sequence form linear program", "extensive form game"),
    "matrix_game_lp": ("nash equilibrium", "zero sum matrix game", "bimatrix game", "linear programming",
                       "game theory"),
    "simultaneous_exploitation": ("regret matching", "multi armed bandit", "opponent modelling",
                                  "repeated game strategies"),
    "feedback_deduction": ("constraint satisfaction", "sat solver", "entropy information gain",
                           "candidate elimination", "word frequency list"),
    "dice_expectimax": ("expectiminimax", "value iteration", "markov decision process", "dynamic programming",
                        "probability distribution"),
    "bargaining_auction": ("bargaining model", "auction theory", "best response optimisation",
                           "numerical optimisation"),
    "document_qa": ("bm25 ranking", "information retrieval", "extractive question answering",
                    "sentence similarity"),
    "text_guardrail": ("text normalisation", "fuzzy string matching", "unicode transliteration",
                       "regular expressions"),
    "external_binary_engine": ("uci protocol", "gtp protocol", "chess engine wrapper"),
}


def tags_for(spec, analysis=None):
    """The capability tags of one variant, best first. Pure, cheap, never raises."""
    out = []

    def add(tag):
        if tag in TAGS and tag not in out:
            out.append(tag)

    try:
        profile = getattr(spec, "profile", None)
        family = getattr(profile, "family", "unknown") or "unknown"
        for tag in FAMILY_TAGS.get(family, ()):
            add(tag)
        if profile is not None:
            if profile.simultaneous:
                add("matrix_game_lp")
                add("simultaneous_exploitation")
            if profile.perfect_information is False and family != "guessing":
                add("imperfect_info_cfr")
            if profile.speech_restrictions:
                add("text_guardrail")
        a = analysis or {}
        if a.get("unknown_outcomes") or a.get("has_chance"):
            add("dice_expectimax")
        rules = str(getattr(spec, "rules", "") or "")
        if rules:
            for tag, rx in EVIDENCE:
                if rx.search(rules):
                    add(tag)
    except Exception:          # a profile we cannot read is not a reason to fail prep
        pass
    return out


def query_terms(tag):
    """The phrases a discovery query for this tag may use (from the fixed vocabulary)."""
    return tuple(TAG_TERMS.get(tag, ())) + SHARED_TERMS


def all_terms():
    out = set(SHARED_TERMS)
    for terms in TAG_TERMS.values():
        out.update(terms)
    return out
