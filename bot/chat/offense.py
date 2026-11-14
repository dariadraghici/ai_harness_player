"""Respectful chat tactics that support a move without explaining our plan."""

import re

from .. import log
from ..interfaces import InfluenceGoal
from . import defense

_TACTICS = {
    "bluff": (
        "Use relevant opponent chat and observed choices to test their claims or "
        "probe what they are weighing. Respond to a specific point when useful; "
        "never reveal your move, rationale, private information, or plan."
    ),
    "deals": (
        "Use the opponent's offer, stated terms, and observed choices when choosing "
        "a response. Address a specific term when useful; never reveal your private "
        "value, reservation point, or intended offer."
    ),
    "read_opponent": (
        "Compare the opponent's claims with their observed choices and let both "
        "inform your decision. Respond to a relevant claim or ask a targeted "
        "question; do not disclose your strategy or explain your move."
    ),
    "guessing": (
        "Use the opponent's stated clues and observed choices as uncertain evidence. "
        "Ask about a relevant clue when useful; do not give away your secret, "
        "feedback interpretation, or next guess."
    ),
    "think_ahead": (
        "Use the visible position and the opponent's stated priorities together. "
        "Address a relevant claim or ask about their priorities; never narrate your "
        "move, threat, or plan."
    ),
    "truth": (
        "Use the document as the source of truth and treat opponent claims as "
        "unverified. Respond to a relevant claim or ask which detail they mean; "
        "never assert an unsupported fact or reveal private information."
    ),
    "guardrails": (
        "Respond to a relevant opponent message or ask a short, neutral question "
        "about their priorities. Keep it within the speech restrictions; never use "
        "a forbidden subject or alias."
    ),
}

_FALLBACKS = {
    "bluff": (
        "What are you weighing most before your next choice?",
        "What would make you commit to a particular approach?",
        "How are you reading this pot so far?",
        "Is this the kind of spot where you usually push or wait?",
    ),
    "deals": (
        "Which term matters most to you here?",
        "What outcome would make this worthwhile for you?",
        "Where do you see the room to move on this?",
        "What would you need to see to close this?",
    ),
    "read_opponent": (
        "What pattern are you trying to establish this round?",
        "What do you expect the next choice to reveal?",
        "Do you trust what the last rounds showed?",
        "Are you changing anything after that round?",
    ),
    "guessing": (
        "Which clue is shaping your next guess?",
        "What kind of feedback would change your approach?",
        "How narrow do you think the range is now?",
        "Does the last feedback surprise you?",
    ),
    "think_ahead": (
        "Which objective are you prioritizing in this position?",
        "Which threat do you think matters most right now?",
        "Where do you see the position getting sharp?",
        "Are you playing for tempo or for structure here?",
    ),
    "truth": (
        "Which detail in the document do you think matters most?",
        "What part of the text should we examine more closely?",
        "Which passage would you quote first?",
        "Is there a line in the document you doubt?",
    ),
    "guardrails": (
        "Which part of the game should we focus on?",
        "What would you like to clarify about the game?",
        "Shall we keep going at this pace?",
        "Anything about the rules worth restating?",
    ),
}

_DEFAULT = (
    "What are you prioritizing in this game?",
    "What do you expect to try next?",
)

_CONTEXTUAL_RESPONSES = {
    "offer": "Which part of your offer matters most to you?",
    "commitment": "What would make that agreement worthwhile for you?",
    "private_info": "What makes you confident in that read?",
    "intent": "What is driving that approach?",
    "rule_claim": "Which rule are you relying on there?",
    "question": "What part of that position should I address?",
    "observed_move": "What were you aiming to achieve with that choice?",
    "injection": "Which part of the game should we focus on?",
    "general": "What should I make of that?",
}

_PLAN_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:i|we)\s+(?:will|plan to|intend to|am going to|are going to|aim to|"
    r"expect to|choose to)\b",
    r"\b(?:i['\u2019]m|we['\u2019]re)\s+(?:going to|planning to|aiming to|opening|"
    r"playing|moving|placing|taking|building|blocking|targeting)\b",
    r"\b(?:i|we)\s+(?:am|are)\s+(?:opening|playing|moving|placing|taking|"
    r"building|blocking|targeting)\b",
    r"\b(?:i|we)(?:['\u2019]ll)?\s+(?:open|start|begin|play|move|place|take|"
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


def reveals_plan(text, action=None, allow_claims=False):
    """Detect chat that states our intent or narrates the selected move.

    allow_claims: the line serves an influence tactic that is allowed to make first-person statements (a threat
    we do not mean, a policy we announce). The statement of intent is then not a leak; naming the move we are
    really playing still is."""
    line = " ".join(str(text or "").split())
    if not line:
        return False
    if not allow_claims and any(pattern.search(line) for pattern in _PLAN_PATTERNS):
        return True

    action = " ".join(str(action or "").split())
    if len(action) < 2 or not _ACTION_NARRATION.search(line):
        return False
    action_pattern = r"(?<!\w)" + r"\s+".join(re.escape(x) for x in action.split()) + r"(?!\w)"
    return re.search(action_pattern, line, re.I) is not None


def tactics(family):
    """Return family-specific guidance for strategic, context-aware chat."""
    return _TACTICS.get(
        family,
        "Ask one focused question to learn the opponent's priorities. Never reveal "
        "or describe your move, rationale, private information, or plan.",
    )


def fallback_line(family, turn_msg=None, ctx=None):
    """Return a safe, contextual reply where a current-game message is available."""
    game_index = (turn_msg or {}).get("game_index")
    recent = defense.recent_opponent_lines(ctx, game_index, limit=1, current_only=True)
    if recent:
        return _CONTEXTUAL_RESPONSES[defense.message_signal(recent[-1])]
    lines = _FALLBACKS.get(family, _DEFAULT)
    turn = (turn_msg or {}).get("turn")
    game = (turn_msg or {}).get("game_index")
    try:        # rotate with the turn AND the game, so that a long match does not repeat one line (it reads as scripted)
        index = (int(turn or 0) + 3 * int(game or 0)) % len(lines)
    except (TypeError, ValueError):
        index = 0
    return lines[index]


# ======================================================================================================
# The influence planner (PROMPT-CHAT sections 4 to 6 and 11). The chat line is free talk in most games, so it is
# used to move the opponent. The goal is built here, in code, from the game family, the opponent class and, when the
# engine ran, from what it knows about THEIR replies to the move we play (r_likely, r_target, the gap). The model only
# writes the sentence, in the same call that picks the move. This module adds no model call.
# ======================================================================================================

CONTROL_RATE = 1.0 / 6.0          # share of would-be influence turns sent as a quiet control (the baseline)
SMALL_GAP = 0.10                  # below this there is nothing to win: a credibility line or quiet
LARGE_GAP = 0.30                  # a value gap this large makes a turn decisive: no control, no exploration
EARLY_TURNS = 3                   # "first turns" of the family defaults, counted in OUR decisions
CREDIBILITY_NEEDED = 2            # true remarks in the ledger before cash_credibility may be played
NEUTRAL_CLASSES = ("unknown",)    # classes that only get the trust-neutral tactics
FULL_CLASSES = ("reactive",)      # classes that get the whole library
QUIET_CLASSES = ("silent", "scripted")

_COMMIT_ACTION = r"\b(?:bet|raise|call|all[- ]?in|push|shove|commit|ante|stake|wager)\b"
_PASSIVE_ACTION = r"\b(?:check|pass|limp|wait|stand)\b"

# id -> data. The model writes the sentence from `intent` (generic) or `intent_t` (when the engine named the replies);
# nothing here is a canned line.
#   families  where it applies;  neutral  allowed against a class not yet known to be reactive
#   claim     (subject, value, scope) of the claim the line makes, for the contradiction ledger; "#" = the numbers
#   claims    the line may state an intent (first person);  needs  a condition the planner checks
#   when      regex on the ACTION we play: the tactic only applies then (else its fallback runs)
#   phase     1 = may be planned without the engine (the deterministic defaults of phase 1)
TACTICS = {
    "quiet": dict(
        families=("any",), neutral=True, claims=False, claim=None, phase=1, kind="quiet", target=False,
        intent="One short, neutral, natural sentence about the game. Reveal nothing, claim nothing."),
    "credibility": dict(
        families=("any",), neutral=True, claims=False, claim=None, phase=2, kind="build_credibility", target=False,
        intent="Make one small statement about the visible position that is TRUE and can be checked from the "
               "observation. No angle, no prediction, nothing about your plan, and do not name the move or the cell "
               "you are playing now."),
    "fake_threat": dict(
        families=("think_ahead",), neutral=False, claims=True, claim=None, phase=1, kind="induce_belief",
        target=True,
        intent="Imply that our plan is somewhere other than where your real move is, so that they spend their "
               "attention on the wrong area. Point at a different spot or idea than your real move. Never name "
               "your real move.",
        intent_t="Draw their attention away from {avoid}: imply that what matters is around {target}, so that "
                 "{target} looks urgent to them and {avoid} looks pointless. Never name your real move."),
    "invite_blunder": dict(
        families=("think_ahead", "guessing"), neutral=True, claims=False, claim=None, phase=1, kind="induce_move",
        target=True,
        intent="Make a reply that is actually weak for them sound like the solid, natural choice here. Offer it "
               "as a friendly observation or advice, not as an instruction, and never name your own move.",
        intent_t="Make {target} sound like the solid, natural reply for them here. Offer it as a friendly "
                 "observation or advice, not as an instruction. Say nothing that points at {avoid}, and never name "
                 "your own move."),
    "strength_claim": dict(
        families=("bluff",), neutral=False, claims=True, claim=("strength", "strong", "game"), phase=1,
        kind="induce_belief", target=True, when=_COMMIT_ACTION,
        intent="Imply that we are in a strong position, so that they back off. Do not name a value or a hand."),
    "weakness_bait": dict(
        families=("bluff",), neutral=False, claims=True, claim=("strength", "weak", "game"), phase=2,
        kind="induce_belief", target=True, when=_PASSIVE_ACTION,
        intent="Imply that we are unsure, to invite them to push into a spot we beat. Name no value or hand."),
    "range_polarize": dict(
        families=("bluff",), neutral=True, claims=False, claim=None, phase=1, kind="induce_belief", target=False,
        intent="Imply that our situation is either very strong or a pure bluff, so they cannot tell which. "
               "Name no value or hand."),
    "anchor": dict(
        families=("deals", "guessing"), neutral=True, claims=True, claim=("anchor", "#", "game"), phase=1,
        kind="induce_belief", target=False,
        intent="Mention one number as if it were the natural reference point for this game, so that their later "
               "estimates drift toward it. Keep it plausible and never tie it to our real private values."),
    "false_constraint": dict(
        families=("deals",), neutral=False, claims=True, claim=("limit", "#", "game"), phase=1,
        kind="induce_belief", target=False,
        intent="Claim a limit we do not really have, in one sentence, to move the split in our favor. Use one "
               "number, and keep it consistent with everything we said before."),
    "commitment": dict(
        families=("read_opponent",), neutral=True, claims=True, claim=("policy", "mixed", "match"), phase=1,
        kind="induce_belief", target=False,
        intent="Say that you mix up your choices and never follow a pattern, so that reading you is a waste of "
               "their effort. This is a policy we really follow.",
        intent_t="Announce a policy in one sentence: whenever they play {avoid}, your answer is {answer}. It is a "
                 "policy we will keep, so state it plainly as a fact about how you play."),
    "cash_credibility": dict(
        families=("think_ahead", "deals", "read_opponent"), neutral=False, claims=True, claim=None, phase=2,
        kind="induce_move", target=True, needs="credibility_gap",
        intent="Remind them in a few words that we have been straight with them so far, then make the one claim "
               "that matters.",
        intent_t="Remind them in a few words that we have been straight with them all match, then say plainly that "
                 "{target} is the move for them, and that {avoid} is a mistake."),
    "pattern_bait": dict(
        families=("read_opponent",), neutral=False, claims=True, claim=None, phase=2, kind="induce_move",
        target=True, needs="pattern",
        intent="Confirm the pattern we have been showing.",
        intent_t="Confirm, in one casual sentence, the pattern we seem to show: that you keep coming back to "
                 "{pattern}. Do not mention any other action."),
    "tempo_pressure": dict(
        families=("any",), neutral=False, claims=False, claim=None, phase=1, kind="induce_move", target=True,
        needs="lead",
        intent="Point out, as an observation, that playing it safe still leaves them behind in this match, so a "
               "bolder line is the only way back. Do not insult them.",
        intent_t="Point out, as an observation, that playing it safe still leaves them behind in this match, so a "
                 "bolder line such as {target} is the only way back. Do not insult them."),
    "rule_doubt": dict(
        families=("think_ahead", "deals", "read_opponent"), neutral=False, claims=False, claim=None, phase=2,
        kind="deter_move", target=True, needs="gap",
        intent="Ask whether their intended move pays off the way they assume. A question or a hedge only.",
        intent_t="Ask them, as a question or a hedge, whether {avoid} really pays off the way they assume. Never "
                 "assert anything about the rules; only a doubt we would defend on stage."),
    "instruction_probe": dict(
        families=("any",), neutral=False, claims=False, claim=None, phase=2, kind="induce_belief", target=False,
        needs="probe",
        intent="A brief aside that reads like a note to the reader, to see whether their harness passes raw chat "
               "into their model. Harmless text only."),
}
ONCE_PER_MATCH = ("cash_credibility", "instruction_probe")

# The no-engine default per family: (early tactic, later tactic) for a class that is `reactive` (phase 1).
FAMILY_DEFAULTS = {
    "think_ahead": ("fake_threat", "invite_blunder"),
    "bluff": ("range_polarize", "range_polarize"),
    "guessing": ("anchor", "quiet"),
    "read_opponent": ("commitment", "quiet"),
    "deals": ("anchor", "false_constraint"),
    "truth": ("quiet", "quiet"),
    "guardrails": ("quiet", "quiet"),
}
# The arms of the bandit per family (phase 2); "quiet" is always an arm (its statistics are the controls).
FAMILY_ARMS = {
    "think_ahead": ("invite_blunder", "fake_threat", "tempo_pressure", "rule_doubt", "cash_credibility"),
    "bluff": ("range_polarize", "strength_claim", "weakness_bait"),
    "guessing": ("anchor", "invite_blunder"),
    "read_opponent": ("commitment", "pattern_bait", "rule_doubt", "cash_credibility"),
    "deals": ("anchor", "false_constraint", "cash_credibility", "rule_doubt"),
    "truth": (),
    "guardrails": (),
}
FALLBACK_TACTIC = {"bluff": "range_polarize"}      # what runs when a `when` condition on the action does not hold

_FREE_TEXT_FORMAT = re.compile(
    r"\b(?:free[- ]?text|free[- ]?form|any text|in words|in your own words|a message|an utterance|a sentence|"
    r"say\b|accuse|a statement|natural language|answer in words)", re.I)
_PLACEHOLDER = re.compile(r"<[^>]{1,20}>|\[[^\]]{1,20}\]|\bexactly like\b|\\d|\{[^}]{1,20}\}", re.I)


def detect_chat_is_move(spec, turn_msg=None, ctx=None):
    """True when talking IS the move (PROMPT-CHAT section 1). Cheap, pure code, stored on the profile.

    1. a sample turn or a chat entry with kind "move"; 2. an action format that is free text; 3. a list of
    short tokens or a fixed pattern is free talk; 4. when unsure: free talk (the chat then stays consistent
    with the action, which is right in both cases)."""
    profile = spec.profile if spec else None
    if profile is None:
        return False
    if getattr(profile, "chat_is_move", False):
        return True
    try:
        sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
        entries = list(sample.get("chat") or []) + list((turn_msg or {}).get("chat") or [])
        if any(isinstance(e, dict) and e.get("kind") == "move" for e in entries):
            return True
        if ctx is not None and any(getattr(line, "kind", "") == "move" for line in ctx.all_lines):
            return True
        legal = (turn_msg or {}).get("legal_actions") or sample.get("legal_actions")
        if isinstance(legal, list) and legal:
            return False
        fmt = str(profile.action_format or "")
        if fmt and not _PLACEHOLDER.search(fmt) and _FREE_TEXT_FORMAT.search(fmt):
            return True
    except Exception:
        log.exception("detect_chat_is_move failed")
    return False


# ---------------------------------------------------------------- the claims ledger (per match)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


class Ledger:
    """What we asserted in this match, whether it was true, in which game. A new claim about the same quantity
    that says the opposite is a contradiction (it would burn our credibility for the rest of the match). Also
    keeps the promises we made (commitment) and whether we kept them."""

    MAX = 40

    def __init__(self):
        self.entries = []
        self.promises = []             # {"if", "then", "game", "turn", "status": open|kept|broken}

    def add(self, goal, line, truthful=None):
        subject = getattr(goal, "claim_subject", "") if goal is not None else ""
        if truthful is None and goal is not None and getattr(goal, "tactic_id", "") == "credibility":
            truthful = True
        self.entries.append({
            "subject": subject, "value": getattr(goal, "claim_value", "") if goal is not None else "",
            "scope": getattr(goal, "claim_scope", "game") if goal is not None else "game",
            "game": getattr(goal, "game", None) if goal is not None else None,
            "turn": getattr(goal, "turn", None) if goal is not None else None,
            "tactic": getattr(goal, "tactic_id", "") if goal is not None else "",
            "numbers": sorted(set(_NUMBER.findall(line or ""))), "line": (line or "")[:200], "truthful": truthful})
        del self.entries[:-self.MAX]

    def truthful_count(self):
        return sum(1 for e in self.entries if e.get("truthful"))

    def contradicts(self, subject, value, game, scope, line=""):
        if not subject:
            return False
        numbers = set(_NUMBER.findall(line or ""))
        for e in self.entries:
            if e["subject"] != subject:
                continue
            if scope == "game" and str(e["game"]) != str(game):
                continue
            if value == "#":
                if numbers and e["numbers"] and numbers.isdisjoint(e["numbers"]):
                    return True
            elif e["value"] and value and e["value"] != value:
                return True
        return False

    def recent(self, n=4):
        return [e for e in self.entries if e["line"]][-n:]

    # promises
    def promise(self, goal):
        if goal is not None and goal.promise_if and goal.promise_then:
            self.promises.append({"if": goal.promise_if, "then": goal.promise_then, "game": goal.game,
                                  "turn": goal.turn, "status": "open"})
            del self.promises[:-12]

    def open_promises(self):
        return [p for p in self.promises if p["status"] == "open"]

    def broken(self):
        return any(p["status"] == "broken" for p in self.promises)


# ---------------------------------------------------------------- what the engine knows about THEIR replies
def _argmax(values):
    best, best_i = None, None
    for i, v in enumerate(values):
        if best is None or v > best:
            best, best_i = v, i
    return best_i


def analyze_replies(replies):
    """From the reply table of the engine (base.LAST_REPLIES) to {action, r_likely, r_target, v_likely, v_target,
    gap, exact, promise?, pattern?}. r_likely is the reply that is best for THEM (what they will probably play),
    r_target the one that is worst for them (best for us). gap = value(r_target) - value(r_likely), in our units,
    >= 0. None when the table is unusable. Pure code, never raises."""
    try:
        if not replies:
            return None
        if replies.get("matrix"):
            return _analyze_matrix(replies)
        rows = [r for r in (replies.get("rows") or []) if isinstance(r, dict) and r.get("action")]
        if len(rows) < 2:
            return None
        vals = [float(r["value"]) for r in rows]
        t = _argmax(vals)
        ps = [float(r.get("p") or 0.0) for r in rows]
        lk = _argmax(ps) if sum(ps) > 0 else _argmax([-v for v in vals])
        return {"action": replies.get("action"), "r_target": rows[t]["action"], "v_target": vals[t],
                "r_likely": rows[lk]["action"], "v_likely": vals[lk], "gap": max(0.0, vals[t] - vals[lk]),
                "exact": all(bool(r.get("exact")) for r in rows)}
    except Exception:
        log.exception("analyze_replies failed")
        return None


def _analyze_matrix(replies):
    acts, matrix, pick = list(replies.get("acts") or []), replies.get("matrix"), replies.get("action")
    if pick not in acts or not matrix or len(acts) < 2:
        return None
    i = acts.index(pick)
    row = [float(v) for v in matrix[i]]
    p = [float((replies.get("model") or {}).get(a, 0.0)) for a in acts]
    if sum(p) <= 0:
        p = [float((replies.get("opp_eq") or {}).get(a, 0.0)) for a in acts]
    lk = _argmax(p) if sum(p) > 0 else _argmax([-v for v in row])
    t = _argmax(row)
    out = {"action": pick, "r_target": acts[t], "v_target": row[t], "r_likely": acts[lk], "v_likely": row[lk],
           "gap": max(0.0, row[t] - row[lk]), "exact": False}
    # a promise that costs nothing: when they play X we answer with our best response to X
    col = [float(matrix[r][lk]) for r in range(len(acts))]
    best_resp = _argmax(col)
    if best_resp is not None and acts[best_resp] != acts[lk]:
        out["promise"] = {"if": acts[lk], "then": acts[best_resp]}
    # a pattern we can break: we really played L last time; a rational reply to L is c; the action we play now
    # (already drawn by the engine) is the best response to c
    prev = replies.get("prev_action")
    if prev in acts and prev != pick:
        l = acts.index(prev)
        lrow = [float(v) for v in matrix[l]]
        c = min(range(len(acts)), key=lambda j: lrow[j])
        best_to_c = _argmax([float(matrix[r][c]) for r in range(len(acts))])
        if best_to_c == i and float(matrix[i][c]) - row[lk] >= SMALL_GAP:
            out["pattern"] = {"claim": prev, "induce": acts[c], "gain": float(matrix[i][c]) - row[lk]}
    return out


def intent_text(goal, tactic_id=None):
    """The intent the model is given: the targeted form when the engine named the replies, else the generic one."""
    t = TACTICS.get(tactic_id or goal.tactic_id, TACTICS["quiet"])
    text = t.get("intent_t") if goal.from_engine and t.get("intent_t") else t["intent"]
    if "{" in text:
        text = text.format(target=goal.target_action or "that reply", avoid=goal.avoid_action or "their usual reply",
                           answer=goal.promise_then or "our best answer",
                           pattern=goal.promise_if or "our last move")
    return text


# ---------------------------------------------------------------- planning
def _quiet(reason="", gap=None, cls="unknown", game=None, turn=None):
    return InfluenceGoal(kind="quiet", tactic_id="quiet", belief=TACTICS["quiet"]["intent"], gap=gap,
                         confidence=0.0, opp_class=cls, game=game, turn=turn)


def _goal_from(tactic_id, cls, game, turn, lag, gap=None, confidence=0.3):
    t = TACTICS[tactic_id]
    claim = t["claim"] or ("", "", "game")
    return InfluenceGoal(kind=t["kind"], tactic_id=tactic_id, belief=t["intent"], gap=gap, confidence=confidence,
                         lag=lag, opp_class=cls, claim_subject=claim[0], claim_value=claim[1], claim_scope=claim[2],
                         allow_claims=bool(t["claims"]), game=game, turn=turn)


def _allowed(tactic_id, cls, used):
    t = TACTICS.get(tactic_id)
    if not t or t["phase"] != 1:
        return False
    if tactic_id in ONCE_PER_MATCH and tactic_id in used:
        return False
    if cls in QUIET_CLASSES:
        return tactic_id == "quiet"
    if cls in NEUTRAL_CLASSES:
        return bool(t["neutral"])
    return cls in FULL_CLASSES


def _score_lead(turn_msg, start_msg):
    """True when the opponent is behind on the match score (tempo_pressure)."""
    try:
        score = (turn_msg or {}).get("match_score") or (start_msg or {}).get("match_score") or {}
        return float(score.get("you", 0)) > float(score.get("opponent", 0))
    except (TypeError, ValueError, AttributeError):
        return False


def _probe_on():
    import os
    return os.environ.get("BOT_INSTRUCTION_PROBE", "off").strip().lower() in ("1", "on", "true", "yes")


def eligible_arms(family, cls, used, analysis, lead, truthful, decisions):
    """The tactics the bandit may choose from this turn (always ends with the "quiet" arm)."""
    out = []
    for tid in FAMILY_ARMS.get(family, ()):
        t = TACTICS[tid]
        if tid in ONCE_PER_MATCH and tid in used:
            continue
        if cls in NEUTRAL_CLASSES and not t["neutral"]:
            continue
        need = t.get("needs")
        gap = analysis["gap"] if analysis else None
        if need == "gap" and not (gap is not None and gap >= LARGE_GAP):
            continue
        if need == "credibility_gap" and not (gap is not None and gap >= LARGE_GAP and truthful >= CREDIBILITY_NEEDED):
            continue
        if need == "lead" and not lead:
            continue
        if need == "pattern" and not (analysis and analysis.get("pattern")):
            continue
        if tid == "commitment" and family == "read_opponent" and analysis is not None and "promise" not in analysis:
            continue
        out.append(tid)
    if cls in FULL_CLASSES and _probe_on() and decisions <= 2 and "instruction_probe" not in used:
        out.append("instruction_probe")
    return out + ["quiet"]


def _apply_analysis(goal, tactic_id, analysis, replies):
    """Fill the targets the engine named. Called for the tactic that will run and for the shadow of a control."""
    t = TACTICS[tactic_id]
    goal.gap, goal.from_engine = analysis["gap"], True
    goal.engine_action, goal.replies = analysis["action"], replies
    goal.confidence = 0.9 if analysis.get("exact") else 0.5
    if t["kind"] == "deter_move":
        goal.target_action = goal.avoid_action = analysis["r_likely"]
    elif t.get("target"):
        goal.target_action, goal.avoid_action = analysis["r_target"], analysis["r_likely"]
    if tactic_id == "commitment" and analysis.get("promise"):
        goal.promise_if, goal.promise_then = analysis["promise"]["if"], analysis["promise"]["then"]
        goal.avoid_action = goal.promise_if
    if tactic_id == "pattern_bait" and analysis.get("pattern"):
        goal.promise_if = analysis["pattern"]["claim"]
        goal.target_action = analysis["pattern"]["induce"]


def plan_influence(spec, turn_msg, candidates, opp_profile, rng, state=None, start_msg=None, replies=None,
                   bandit=None):
    """The influence goal of one turn. NEVER raises: any failure degrades to `quiet`.

    state: per-match influence state (chat/influence.py): opp_class, decisions, used tactics, last goal, ledger.
    replies: base.LAST_REPLIES of this turn (what the engine knows about their replies to the move it recommends).
    bandit: chat/bandit.Bandit; without one the deterministic family defaults of phase 1 decide."""
    game = (turn_msg or {}).get("game_index")
    turn = (turn_msg or {}).get("turn")
    cls = getattr(state, "opp_class", "unknown") or "unknown"
    try:
        profile = spec.profile if spec else None
        family = profile.family if profile else "unknown"
        if state is not None and (turn_msg or {}).get("warning") and getattr(state, "last_goal", None) is not None:
            again = state.last_goal
            again.resend = True        # our previous chat was dropped with the rejected answer: same goal again
            return again
        if spec is None or detect_chat_is_move(spec, turn_msg):
            if profile is not None:
                profile.chat_is_move = bool(profile.chat_is_move or detect_chat_is_move(spec, turn_msg))
            return _quiet(cls=cls, game=game, turn=turn)
        if cls in QUIET_CLASSES or family not in FAMILY_DEFAULTS:
            return _quiet(cls=cls, game=game, turn=turn)
        used = getattr(state, "used", set())
        decisions = int(getattr(state, "decisions", 0))
        lead = _score_lead(turn_msg, start_msg)
        simultaneous = bool(profile.simultaneous) if profile else False
        lag = 1 if simultaneous else 0
        analysis = analyze_replies(replies) if replies else None
        gap = analysis["gap"] if analysis else None
        decisive = gap is not None and gap >= LARGE_GAP
        truthful = state.ledger.truthful_count() if state is not None and hasattr(state, "ledger") else 0
        info = None
        if bandit is None:
            early_t, late_t = FAMILY_DEFAULTS[family]
            tactic = early_t if decisions < EARLY_TURNS else late_t
            if cls in FULL_CLASSES and lead and _allowed("tempo_pressure", cls, used) and decisions >= EARLY_TURNS:
                tactic = "tempo_pressure"
            if not _allowed(tactic, cls, used):
                tactic = "quiet"
        elif analysis is not None and gap < SMALL_GAP:
            tactic = "credibility" if cls in FULL_CLASSES and truthful < 3 else "quiet"      # nothing to win here
        else:
            arms = eligible_arms(family, cls, used, analysis, lead, truthful, decisions)
            explored = int(getattr(state, "explored", 0))
            bandit.explored = explored
            tactic, info = bandit.choose(arms, rng, greedy=decisive, may_explore=not decisive)
            if tactic == "quiet" and cls in FULL_CLASSES and truthful < 3 and not decisive:
                tactic = "credibility"           # still quiet-class for the statistics: a true remark, no angle
        t = TACTICS[tactic]
        goal = _goal_from(tactic, cls, game, turn, lag, gap)
        goal.arm_info = info
        if bandit is None and family == "bluff" and tactic == "range_polarize" and cls in FULL_CLASSES \
                and _allowed("strength_claim", cls, used):
            goal.alt_tactic, goal.alt_when = "strength_claim", _COMMIT_ACTION      # resolved from the action later
        if t.get("when"):                        # the tactic only applies to some actions: the fallback runs otherwise
            fb = FALLBACK_TACTIC.get(family, "quiet")
            fallback = _goal_from(fb, cls, game, turn, lag, gap)
            fallback.alt_tactic, fallback.alt_when, fallback.arm_info = tactic, t["when"], info
            goal = fallback
            t = TACTICS[fb]
            tactic = fb
        if analysis is not None and TACTICS[goal.alt_tactic or goal.tactic_id]["kind"] != "quiet":
            _apply_analysis(goal, goal.alt_tactic or goal.tactic_id, analysis, replies)
        spoken = {" ".join(str(x).split()).casefold() for x in (goal.target_action, goal.avoid_action, goal.promise_then,
                                                                  goal.promise_if) if x}
        if goal.from_engine and goal.engine_action and goal.tactic_id != "quiet" \
                and " ".join(goal.engine_action.split()).casefold() in spoken:
            # the line would have to name the move we are playing (a simultaneous game, same action names): that is
            # a leak, so this turn carries no tactic
            return _quiet(gap=gap, cls=cls, game=game, turn=turn)
        if goal.tactic_id in ("quiet", "credibility") and not goal.alt_tactic:
            return goal
        # a control: the same turn sent quiet, so that every tactic is compared with a baseline, not with luck
        if rng is not None and not decisive and rng.random() < CONTROL_RATE:
            ctrl = _quiet(cls=cls, game=game, turn=turn)
            ctrl.is_control, ctrl.shadow_tactic, ctrl.lag = True, goal.alt_tactic or goal.tactic_id, lag
            ctrl.arm_info = info
            if analysis is not None:
                _apply_analysis(ctrl, ctrl.shadow_tactic, analysis, replies)
            return ctrl
        return goal
    except Exception:
        log.exception("plan_influence failed, degrading to quiet")
        return _quiet(cls=cls, game=game, turn=turn)


def resolve_goal(goal, action):
    """Switch to the alternative tactic when the action we chose matches its condition (bluff: chips go in).
    Called after the model answered, so the logged tactic is the one the line was written for."""
    try:
        if goal is not None and goal.alt_tactic and goal.alt_when and action \
                and re.search(goal.alt_when, str(action), re.I):
            alt = _goal_from(goal.alt_tactic, goal.opp_class, goal.game, goal.turn, goal.lag, goal.gap,
                             goal.confidence)
            alt.resend, alt.arm_info = goal.resend, goal.arm_info
            for f in ("target_action", "avoid_action", "from_engine", "engine_action", "replies", "promise_if",
                      "promise_then"):
                setattr(alt, f, getattr(goal, f))
            return alt
    except Exception:
        log.exception("resolve_goal failed")
    return goal
