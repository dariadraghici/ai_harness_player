"""Outgoing chat checks, in code (PROMPT-CHAT section 2). Nothing ships to the opponent without passing here.

Owner: luckydevs-2 area. Status: real. Standard library only.

check_outgoing(line, specialist, ledger, ...) -> (ok, reason, repaired)

  1. format: a string, ONE line, at most 200 characters (cut at a word or sentence end, never mid-word), never
     empty, no control characters, no code fences, no JSON leaking out of a model answer;
  2. leak: the line must not contain our private state (secret number, cards, hand: tokens found in the
     observation) nor our chosen action, unless the goal is a deliberate reveal;
  3. collusion: no arrangement that lives outside the current game (throw this one, trade wins, standings, other
     matches). Out-of-game arrangements are an offence;
  4. abuse: no insults, hate, harassment, sexual content;
  5. plan statements: a line that states our intent is replaced, unless the goal is a tactic that is allowed to
     claim (the claim is checked by the ledger instead);
  6. contradiction: a claim that contradicts what we asserted earlier in the match (same quantity, opposite
     claim) is blocked unless the goal is a reveal;
  7. speech rules of the game: engines/guardrails.py filter_outgoing (Guardrails family).

When a line is blocked the STORED safe line is sent. No repaired guess is ever sent under time pressure:
`repaired` is either the normalized original (ok) or the safe line (not ok).
It never raises.
"""

import re
import unicodedata

from .. import config, log
from ..engines import guardrails
from . import offense

# ---------------------------------------------------------------- format
_FENCE = re.compile(r"```[a-zA-Z]*|```")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_JSON_CHAT = re.compile(r'"chat"\s*:\s*"((?:\\.|[^"\\])*)"')


def _strip_controls(text):
    return "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in text)


def normalize(line, limit=None):
    """A string, one line, no control characters, no fences, cut to `limit` (default 200) at a word or sentence end.
    Returns "" when nothing usable is left."""
    limit = int(limit or config.CHAT_MAX_CHARS)
    if not isinstance(line, str):
        return ""
    text = line
    if text.lstrip().startswith("{") and '"chat"' in text:           # a whole model answer leaked into the chat
        m = _JSON_CHAT.search(text)
        if m:
            try:
                import json
                text = json.loads('"%s"' % m.group(1))
            except Exception:
                text = m.group(1)
    text = _FENCE.sub(" ", text)
    text = " ".join(_strip_controls(text).split())
    if len(text) > limit:
        cut = text[:limit]
        ends = [m.end() for m in _SENTENCE_END.finditer(cut)]
        if ends and ends[-1] >= limit * 0.5:
            cut = cut[:ends[-1]]
        else:
            nxt = text[limit:limit + 1]
            if nxt and not nxt.isspace() and " " in cut:
                cut = cut[:cut.rfind(" ")]           # never end in half a word
        text = cut.rstrip(" ,;:-")
    return text.strip()


# ---------------------------------------------------------------- private tokens (leak check)
_PRIVATE_LINE = re.compile(
    r"\b(?:secret|hidden|private|confidential|your\s+(?:hand|cards?|number|value|valuation|budget|prize|role|word|code"
    r"|bid|secret)|you\s+(?:hold|have|are\s+holding|were\s+dealt|drew|own)|reservation|dealt\s+to\s+you)\b",
    re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)")
_CARD = re.compile(r"\b(?:10|[2-9AKQJT])[shdc\u2660\u2665\u2666\u2663]\b|\b[AKQJ]\b")
_STOP = {"the", "and", "you", "your", "none", "hidden", "unknown", "secret", "private", "not", "yet", "are", "was",
         "have", "has", "hold", "holding", "with", "that", "this", "for", "from", "card", "cards", "hand", "number",
         "value", "dealt", "drew", "own", "only", "one", "two", "turn", "game", "seat"}


def private_tokens(observation, extra=()):
    """Tokens of our private state found in an observation: numbers and short items on lines that talk about a
    secret, hidden or private value, a hand, a card, a budget, a reservation. Conservative on purpose: a false
    positive only costs one safe line."""
    tokens = set(str(t) for t in extra if t)
    for raw in str(observation or "").splitlines():
        m = _PRIVATE_LINE.search(raw)
        if not m:
            continue
        rest = raw[m.start():]
        for n in _NUMBER.findall(rest):
            tokens.add(n)
        for c in _CARD.findall(rest):
            tokens.add(c)
        tail = re.split(r"[:=]", rest, maxsplit=1)
        if len(tail) == 2:
            for item in re.split(r"[,;/]", tail[1]):
                item = item.strip(" .\"'`()[]")
                if 3 <= len(item) <= 30 and item.casefold() not in _STOP and not item.isdigit():
                    tokens.add(item)
    return tokens


def _has_token(text, token):
    token = str(token)
    if not token:
        return False
    if token.replace(".", "", 1).isdigit():
        return re.search(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(token), text) is not None
    return re.search(r"(?<!\w)%s(?!\w)" % re.escape(token), text, re.I) is not None


def leaks_private(text, tokens):
    return [t for t in tokens if _has_token(text, t)]


def mentions_action(text, action):
    """The line names the action we are playing (a leak of the move)."""
    action = " ".join(str(action or "").split())
    if not action:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(x) for x in action.split()) + r"(?!\w)"
    return re.search(pattern, text, re.I) is not None


# ---------------------------------------------------------------- collusion and abuse
_COLLUSION = tuple(re.compile(p, re.I) for p in (
    r"\b(?:throw|tank|dump|forfeit|concede|sandbag)\b.{0,25}\b(?:this|the|a|that|our|next|first|second|last)\b"
    r".{0,15}\b(?:game|match|round|one)\b",
    r"\b(?:let|make)\s+you\s+win\b",
    r"\b(?:let|help)\s+me\s+(?:win|advance|pass|through|qualify)\b",
    r"\blose\s+(?:on\s+purpose|deliberately|intentionally)\b",
    r"\b(?:trade|swap|alternate|take\s+turns)\b.{0,25}\b(?:wins?|games?|matches|points?|victor(?:y|ies))\b",
    r"\b(?:split|share)\b.{0,15}\b(?:the\s+)?(?:points?|wins?|ranking|standings?)\b",
    r"\b(?:standings?|leaderboard|bracket|round[- ]robin|tournament|seeding|tie-?break(?:er)?s?|qualif\w+)\b",
    r"\b(?:other|another|different|later|future|following)\s+(?:game|match|board|round)s?\b",
    r"\b(?:next|second|third|fourth|final|last)\s+(?:game|match)\b.{0,50}"
    r"\b(?:if|deal|agree|promise|in\s+return|in\s+exchange|owe|favou?r)\b",
    r"\b(?:if|deal|agree|promise|in\s+return|in\s+exchange|owe|favou?r)\b.{0,50}"
    r"\b(?:next|second|third|fourth|final|last)\s+(?:game|match)\b",
    r"\b(?:you\s+win|i\s+win)\s+(?:this|that|the)\b.{0,30}\b(?:and|then|so)\b.{0,20}\b(?:you|i)\s+win\b",
))
_ABUSE = tuple(re.compile(p, re.I) for p in (
    r"\b(?:idiot|stupid|moron|dumb(?:ass)?|imbecile|retard\w*|loser|pathetic|worthless|trash|garbage|scum|"
    r"bastard|bitch\w*|fuck\w*|shit\w*|asshole|damn\s+you|hate\s+you|shut\s+up|go\s+die|kill\s+yourself)\b",
    r"\b(?:sex\w*|porn\w*|nude|naked|rape\w*|slut\w*|whore)\b",
    r"\b(?:racist|nazi|faggot|nigg\w*)\b",
))


def collusion(text):
    for rx in _COLLUSION:
        if rx.search(text):
            return rx.pattern[:40]
    return ""


def abusive(text):
    return any(rx.search(text) for rx in _ABUSE)


# ---------------------------------------------------------------- the safe line
def safe_line(spec=None, turn_msg=None, ctx=None):
    """The stored safe line: a contextual question from offense.fallback_line, passed through the speech rules.
    Nothing here is generated, so nothing here can be blocked later."""
    profile = spec.profile if spec else None
    family = profile.family if profile else "unknown"
    text = offense.fallback_line(family, turn_msg, ctx) if profile else config.CHAT_FALLBACK
    try:
        out = guardrails.filter_outgoing(text, "chat", profile)
    except Exception:
        out = guardrails.safe_chat(profile)
    if not isinstance(out, str) or not out.strip():
        out = guardrails.safe_chat(profile)
    return " ".join(out.split())[:config.CHAT_MAX_CHARS]


# ---------------------------------------------------------------- the check
def check_outgoing(line, specialist=None, ledger=None, goal=None, action="", observation="", ctx=None,
                   turn_msg=None, extra_private=()):
    """Returns (ok, reason, repaired). `repaired` is what may be sent: the normalized line when ok, the stored safe
    line when not. Never raises: any internal failure is a block."""
    try:
        return _check(line, specialist, ledger, goal, action, observation, ctx, turn_msg, extra_private)
    except Exception:
        log.exception("guards.check_outgoing failed, sending the safe line")
        try:
            return False, "internal-error", safe_line(specialist, turn_msg, ctx)
        except Exception:
            return False, "internal-error", config.CHAT_FALLBACK


def _check(line, spec, ledger, goal, action, observation, ctx, turn_msg, extra_private):
    profile = spec.profile if spec else None
    if profile is not None and spec is not None:
        guardrails.prepare_profile(profile, spec.rules)

    def block(reason):
        return False, reason, safe_line(spec, turn_msg, ctx)

    text = normalize(line)
    if not text:
        return block("empty-or-invalid")
    reveal = bool(goal is not None and getattr(goal, "reveal", False))
    if not reveal:
        hits = leaks_private(text, private_tokens(observation, extra_private))
        if hits:
            return block("private-token")
        if mentions_action(text, action):
            return block("action-leak")
    why = collusion(text)
    if why:
        return block("collusion")
    if abusive(text):
        return block("abuse")
    allow = bool(goal is not None and getattr(goal, "allow_claims", False))
    if offense.reveals_plan(text, action, allow_claims=allow):
        return block("plan-reveal")
    if goal is not None and ledger is not None and not reveal and getattr(goal, "claim_subject", ""):
        if ledger.contradicts(goal.claim_subject, goal.claim_value, getattr(goal, "game", None),
                              getattr(goal, "claim_scope", "game"), text):
            return block("contradiction")
    try:
        filtered = guardrails.filter_outgoing(text, "chat", profile)
    except Exception:
        log.exception("guardrails.filter_outgoing failed on chat")
        return block("speech-rule")
    if not isinstance(filtered, str) or not filtered.strip() or " ".join(filtered.split()) != text:
        return block("speech-rule")
    return True, "ok", text
