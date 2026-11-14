"""Game family and game profile from the rules text.

Owner: luckydevs-3. Status: real.

Three layers, so the result is never worse than the old default and still
useful when the model is down:

  1. evidence(rules, sample_turn)   pure code, milliseconds, never fails:
     format sentences, quoted examples, per-family signal scores, a
     "confident" flag, simultaneous / hidden-information signals, facts of
     the sample turn.
  2. one model call (thinking on in prep) that returns the profile as JSON
     (templates in bot/prep/prompts/classify_*.txt).
  3. code checks every field against the evidence, the rules text and the
     sample turn; on a disagreement ONE repair call with the specific
     complaint; then code decides: validated model answer, else evidence,
     else the old default.

Extra facts that GameProfile has no field for (family confidence, runner-up,
quote, disagreements, trusted action regex, turn structure, hidden info,
chance, whether a simulator is worth generating) go to analysis.json in the
variant folder (write_analysis / load_analysis).

Also home of call_model(): the deadline-checked, process-wide capped way
every prep stage calls the model.
"""

import json
import re
import threading
import time

from .. import config, llm, log, memory
from ..chat.decide import parse_json_object
from ..engines.guardrails import prepare_profile
from ..interfaces import FAMILIES, GameProfile
from . import templates

# ---------------------------------------------------------------- constants
# proposed for bot/config.py
PREP_MAX_MODEL_CALLS_IN_FLIGHT = 6     # process-wide cap on prep model calls
CLASSIFY_REPAIR_MIN_S = 6.0            # start the repair call only with this much time left
PLAYBOOK_MAX_LINES = 12
PLAYBOOK_MAX_CHARS = 900
SPEECH_MAX_CHARS = 300
ACTION_FORMAT_MAX_CHARS = 400
ACTION_REGEX_MAX_CHARS = 300
SIMULATOR_FAMILIES = ("think_ahead", "guessing", "read_opponent", "bluff")
ANALYSIS_FILE = "analysis.json"

# Families whose information / move order is implied (for contradiction checks).
_FAMILY_IMPLIES = {
    "think_ahead": {"perfect_information": True},
    "read_opponent": {"simultaneous": True},
    "bluff": {"perfect_information": False},
}

# ---------------------------------------------------------------- model calls
_SLOTS = threading.BoundedSemaphore(PREP_MAX_MODEL_CALLS_IN_FLIGHT)


def call_model(messages, max_tokens, deadline, thinking_budget=None, tag="prep",
               cap_s=120.0, margin_s=1.0, min_s=2.0):
    """llm.complete with deadline discipline. Returns text or None, never raises.

    Checks the deadline before the call (never starts a call after it), waits
    for a free slot at most until the deadline, and passes
    timeout = min(cap_s, time left - margin_s).
    """
    try:
        left = deadline - time.monotonic() - margin_s
        if left < min_s:
            log.info("model call %s skipped: %.1fs left" % (tag, left + margin_s))
            return None
        if not _SLOTS.acquire(timeout=max(0.01, left - min_s)):
            log.info("model call %s skipped: no free slot before the deadline" % tag)
            return None
        try:
            left = deadline - time.monotonic() - margin_s
            if left < min_s:
                log.info("model call %s skipped after waiting: %.1fs left" % (tag, left + margin_s))
                return None
            return llm.complete(messages, max_tokens=max_tokens, timeout=min(cap_s, left),
                                thinking_budget=thinking_budget, tag=tag)
        finally:
            _SLOTS.release()
    except Exception:
        log.exception("call_model %s failed" % tag)
        return None


# ---------------------------------------------------------------- evidence (code only)
_EXAMPLE_RE = re.compile(
    r"(?:exactly like|for example|for instance|e\.g\.|such as|examples?:|like)\s*:?\s*\"([^\"\n]{1,60})\"",
    re.IGNORECASE)


def example_from_rules(rules):
    """Generic heuristic: the first quoted example action in the rules text, or ""."""
    text = str(rules or "")
    idx = text.lower().find("action format")
    for chunk in ((text[idx:] if idx >= 0 else ""), text):
        m = _EXAMPLE_RE.search(chunk)
        if m:
            return m.group(1).strip()
    return ""


def quoted_examples(rules):
    """All quoted examples in the rules (example_from_rules first), deduplicated."""
    out = []
    first = example_from_rules(rules)
    if first:
        out.append(first)
    for m in _EXAMPLE_RE.finditer(str(rules or "")):
        s = m.group(1).strip()
        if s and s not in out:
            out.append(s)
    return out


def _rx(*patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# Generic signals per family: about KINDS of rules, never about a game.
_SIGNALS = {
    "read_opponent": _rx(r"\bsimultaneous(ly)?\b", r"\bat the same (time|moment)\b",
                         r"\bboth players? (choose|pick|select|submit|reveal|name)\b",
                         r"\bneither (player )?(sees|knows|can see)\b",
                         r"\bwithout (seeing|knowing) (the other|your opponent|the opponent)",
                         r"\bpayoffs?\b", r"\bmixed strateg"),
    "bluff": _rx(r"\bbet(s|ting)?\b", r"\b(fold|folds|raise|raises)\b", r"\bpot\b", r"\bchips?\b",
                 r"\bbluff", r"\b(cards?|deck)\b", r"\bante\b", r"\b(show ?down|reveal (your|the) hand)\b"),
    "deals": _rx(r"\boffers?\b", r"\b(accept|accepts|reject|rejects)\b", r"\b(propos(e|es|al)|counter-?offer)\b",
                 r"\bnegotiat", r"\b(bids?|bidding|auction)\b", r"\bsplit\b",
                 r"\b(agreement|contract|make a deal|the deal)\b", r"\b(cooperat|defect|betray)",
                 r"\b(private|secret) (value|valuation|budget|reserve)\b"),
    "guessing": _rx(r"\bguess(es|ed|ing)?\b", r"\bfeedback\b", r"\b(higher|lower)\b",
                    r"\bsecret\b.{0,25}\b(number|word|code|combination|sequence)\b",
                    r"\b(deduce|deduction|hints?|clues?)\b", r"\b(correct|incorrect)\b",
                    r"\b(attempts?|tries) (left|remaining)\b"),
    "think_ahead": _rx(r"\b(board|grid)\b", r"\b(cells?|squares?)\b",
                       r"\b(alternate|alternating|take turns|in turn)\b", r"\bin a (row|line)\b",
                       r"\b(capture|captures|pieces?|stones?|pawns?|tokens?)\b",
                       r"\bperfect information\b", r"\bmoves? first\b",
                       r"\broll(s|ing)? (a|one|two|the)? ?(six-sided )?(die|dice)\b"),
    "truth": _rx(r"\bdocument\b", r"\b(passage|article|the text below|source text|the text)\b",
                 r"\baccording to\b", r"\bunknown\b", r"\banswers? (the |each |a )?questions?\b",
                 r"\b(invent|invented|made[- ]up|fabricat|hallucinat)", r"\b(quote|cite|citation)\b"),
    "guardrails": _rx(r"\bforbidden\b",
                      r"\b(must|may|should|can) not (mention|say|talk about|discuss|reveal|name|use the word)\b",
                      r"\b(never|do not|don't) (mention|say|talk about|discuss|name|use the word)\b",
                      r"\b(taboo|banned|off[- ]limits|prohibited)\b",
                      r"\b(topic|subject|word)s? (is|are) (forbidden|banned|not allowed)\b"),
}
_WEIGHT = {"guardrails": 2.0}   # rare and decisive when present

_SIMULTANEOUS_RX = _SIGNALS["read_opponent"][:5]
_ALTERNATING_RX = _rx(r"\b(alternate|alternating|take turns|in turn)\b", r"\bmoves? first\b",
                      r"\bthen (the other|your opponent|the opponent) (moves|plays)\b")
_HIDDEN_RX = _rx(r"\b(private|hidden|face[- ]down)\b", r"\bsecret\b",
                 r"\bonly (you|the player|each player) (can )?(see|sees|know|knows)\b",
                 r"\b(opponent|other player) (does not|doesn't|cannot|can't) see\b")
_PERFECT_RX = _rx(r"\bperfect information\b", r"\bboth players see (the whole|everything|the board)\b")
_TALK_MOVE_RX = _rx(r"\b(your|the) (chat|message)\b.{0,40}\b(is|counts as|serves as) (your|the|a) (move|action)\b",
                    r"\btalking is the move\b")
_FORMAT_CUES = ("action format", "format:", "your action", "your move", "send ", "answer with", "reply with")


def _sentences(text):
    out = []
    for line in str(text or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            if s.strip():
                out.append(s.strip())
    return out


def format_sentences(rules):
    """The sentence(s) of the rules that define the action format (at most 2)."""
    sents = _sentences(rules)
    first = [s for s in sents if "action format" in s.lower()]
    rest = [s for s in sents if s not in first and any(c in s.lower() for c in _FORMAT_CUES)]
    return [s[:300] for s in (first + rest)[:2]]


def evidence(rules, sample_turn=None):
    """Plain facts from the rules text and the optional sample turn. Never raises."""
    try:
        return _evidence(rules, sample_turn)
    except Exception:
        log.exception("evidence failed")
        return {"scores": {}, "family": "unknown", "runner_up": "unknown", "confident": False,
                "quoted_examples": [], "format_sentences": [], "legal_kind": "none",
                "legal_actions": None, "simultaneous": None, "perfect_information": None,
                "talk_is_move": False, "forbidden_sentence": "", "signals": {}, "observation": ""}


def _evidence(rules, sample_turn):
    text = str(rules or "")
    ev = {"quoted_examples": quoted_examples(text), "format_sentences": format_sentences(text)}
    signals, scores = {}, {}
    for fam, pats in _SIGNALS.items():
        hits = []
        for p in pats:
            m = p.search(text)
            if m:
                hits.append(m.group(0))
        signals[fam] = hits
        scores[fam] = round(len(hits) * _WEIGHT.get(fam, 1.0), 2)

    # Sample turn facts.
    legal, obs, talk_move = None, "", False
    if isinstance(sample_turn, dict):
        if isinstance(sample_turn.get("legal_actions"), list):
            legal = [str(a) for a in sample_turn["legal_actions"]]
        obs = str(sample_turn.get("observation") or "")
        for e in sample_turn.get("chat") or []:
            if isinstance(e, dict) and e.get("kind") == "move":
                talk_move = True
    talk_move = talk_move or any(p.search(text) for p in _TALK_MOVE_RX)

    simultaneous = None
    if any(p.search(text) for p in _SIMULTANEOUS_RX):
        simultaneous = True
    elif any(p.search(text) for p in _ALTERNATING_RX):
        simultaneous = False
    perfect = None
    if any(p.search(text) for p in _HIDDEN_RX):
        perfect = False
    elif any(p.search(text) for p in _PERFECT_RX):
        perfect = True

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top, top_s = ranked[0]
    second, second_s = ranked[1]
    confident = top_s >= 2 and top_s >= 1.8 * second_s
    forbidden = ""
    if signals.get("guardrails"):
        for s in _sentences(text):
            if any(p.search(s) for p in _SIGNALS["guardrails"]):
                forbidden = s[:SPEECH_MAX_CHARS]
                break
    ev.update({
        "signals": signals, "scores": scores,
        "family": top if top_s > 0 else "unknown",
        "runner_up": second if second_s > 0 else "unknown",
        "confident": bool(confident and top_s > 0),
        "simultaneous": simultaneous, "perfect_information": perfect, "talk_is_move": talk_move,
        "forbidden_sentence": forbidden,
        "legal_kind": "list" if legal is not None else ("null" if isinstance(sample_turn, dict) else "none"),
        "legal_actions": legal, "legal_count": len(legal) if legal is not None else None,
        "observation": obs[:3000],
    })
    return ev


# ---------------------------------------------------------------- field checks
def _norm(s):
    return " ".join(str(s).split()).casefold()


def match_legal(action, legal):
    """Same matching as ladder.match_legal: exact, then case/whitespace-insensitive."""
    if not isinstance(action, str) or not legal:
        return None
    if action in legal:
        return action
    n = _norm(action)
    for a in legal:
        if _norm(a) == n:
            return a
    return None


def quote_in_rules(quote, rules):
    """True when the quote (or every part around "...") appears verbatim in the rules."""
    if not isinstance(quote, str):
        return False
    q = _norm(quote.strip().strip("\"'`"))
    if len(q) < 8:
        return False
    r = _norm(rules)
    if q in r:
        return True
    parts = [p.strip(" .,;:") for p in re.split(r"\.\.\.|\u2026", q)]
    parts = [p for p in parts if len(p) >= 8]
    return bool(parts) and all(p in r for p in parts)


def _clean_line(s, cap):
    if not isinstance(s, str):
        return ""
    s = " ".join(s.split()).strip()
    return s[:cap]


def _clean_action(s):
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        s = str(s)
    s = _clean_line(s, 120)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    return s


def cap_playbook(text):
    if isinstance(text, list):
        text = "\n".join(str(x) for x in text)
    if not isinstance(text, str):
        return ""
    lines = [" ".join(l.split()) for l in text.replace("\r", "\n").split("\n")]
    lines = [l for l in lines if l][:PLAYBOOK_MAX_LINES]
    out = ""
    for l in lines:
        nxt = (out + "\n" + l) if out else l
        if len(nxt) > PLAYBOOK_MAX_CHARS:
            if not out:
                out = l[:PLAYBOOK_MAX_CHARS]
            break
        out = nxt
    return out


def trusted_regex(regex, model_example, rules_examples, legal=None):
    """Return the compiled regex when it compiles and fully matches every known valid string."""
    if not isinstance(regex, str) or not regex.strip() or len(regex) > ACTION_REGEX_MAX_CHARS:
        return None
    try:
        rx = re.compile(regex.strip())
    except (re.error, RecursionError, ValueError):
        return None
    tests = [s for s in ([model_example] if model_example else []) + list(rules_examples) + list(legal or [])]
    if not tests:
        return None
    try:
        if all(rx.fullmatch(t) for t in tests):
            return rx
    except Exception:
        return None
    return None


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def check_answer(obj, ev, rules):
    """Validate one parsed model answer. Returns (fields, complaints).

    fields: the cleaned values that passed their checks (missing key = rejected).
    complaints: human-readable disagreements that justify a repair call.
    """
    fields, complaints = {}, []
    if not isinstance(obj, dict):
        return fields, ["the answer was not one JSON object"]

    fam = str(obj.get("family") or "").strip().lower()
    quote = obj.get("family_quote")
    quote_ok = quote_in_rules(quote, rules)
    sim = _as_bool(obj.get("simultaneous"))
    perf = _as_bool(obj.get("perfect_information"))
    fields["quote"] = _clean_line(quote, 300) if isinstance(quote, str) else ""
    fields["quote_ok"] = quote_ok
    if sim is not None:
        fields["simultaneous"] = sim
    if perf is not None:
        fields["perfect_information"] = perf

    family_ok = fam in FAMILIES and fam != "unknown"
    if fam not in FAMILIES:
        complaints.append("family %r is not one of %s" % (fam, ", ".join(FAMILIES)))
    if family_ok:
        if ev.get("confident") and fam != ev.get("family"):
            complaints.append("you said family %r, but the rules contain %s, which point to %r" % (
                fam, ", ".join(repr(s) for s in ev["signals"].get(ev["family"], [])[:4]), ev["family"]))
            family_ok = False
        if not quote_ok:
            complaints.append("your family_quote %r is not a verbatim quote from the rules" % (
                str(quote)[:120],))
            family_ok = False
        for key, want in _FAMILY_IMPLIES.get(fam, {}).items():
            got = fields.get(key)
            if got is not None and got != want:
                complaints.append("family %r implies %s=%s, but you said %s" % (fam, key, want, got))
                family_ok = False
    if family_ok:
        fields["family"] = fam

    if ev.get("simultaneous") is not None and sim is not None and sim != ev["simultaneous"]:
        complaints.append("you said simultaneous=%s, but the rules say %r" % (
            sim, ", ".join(ev["signals"].get("read_opponent", [])[:2]) or "players alternate"))
    if ev.get("perfect_information") is False and perf is True:
        complaints.append("you said perfect_information=true, but the rules mention hidden or private information")

    speech = _clean_line(obj.get("speech_restrictions"), SPEECH_MAX_CHARS)
    fields["speech_restrictions"] = speech
    if fam == "guardrails" and not speech:
        complaints.append("family guardrails needs speech_restrictions: what exactly must never be said")

    fmt = _clean_line(obj.get("action_format"), ACTION_FORMAT_MAX_CHARS)
    if fmt:
        fields["action_format"] = fmt

    example = _clean_action(obj.get("example_action"))
    fields["example_raw"] = example
    legal = ev.get("legal_actions")
    rules_ex = ev.get("quoted_examples") or []
    regex = obj.get("action_regex") if isinstance(obj.get("action_regex"), str) else ""
    rx = trusted_regex(regex, example, rules_ex, legal)
    if rx is not None:
        fields["action_regex"] = rx.pattern
    if legal:
        m = match_legal(example, legal)
        if m:
            fields["example_action"] = m
    elif example:
        if rx is not None or not regex:
            fields["example_action"] = example
        if regex and rx is None and not rules_ex:
            complaints.append("your action_regex %r does not compile or does not fully match your "
                              "example_action %r" % (regex[:100], example))
    if not legal and not example and not rules_ex:
        complaints.append("example_action is empty")

    for k in ("hidden_information", "chance"):
        v = _clean_line(obj.get(k), 120)
        if v:
            fields[k] = v
    ts = str(obj.get("turn_structure") or "").strip().lower()
    if ts in ("alternating", "simultaneous", "talk_is_move"):
        fields["turn_structure"] = ts
    fields["playbook"] = cap_playbook(obj.get("playbook"))
    return fields, complaints


# ---------------------------------------------------------------- the model call
def _messages(game_name, label, rules, sample_turn):
    sample = ""
    if isinstance(sample_turn, dict):
        s = {k: sample_turn.get(k) for k in ("observation", "legal_actions", "turn")}
        sample = "SAMPLE TURN (one real turn message from a sample game):\n" + json.dumps(
            s, ensure_ascii=True)[:4000]
    return [{"role": "system", "content": templates.load("classify_system.txt")},
            {"role": "user", "content": templates.render("classify_user.txt", GAME_NAME=game_name,
                                                         LABEL=label, RULES=rules, SAMPLE=sample)}]


def classify_variant(game_name, label, rules, sample_turn, timeout, thinking=True):
    """Return (GameProfile, playbook_text). Never raises; defaults on failure."""
    profile, playbook, _ = analyze_variant(game_name, label, rules, sample_turn, timeout, thinking)
    return profile, playbook


def analyze_variant(game_name, label, rules, sample_turn, timeout, thinking=True):
    """Return (GameProfile, playbook, analysis dict). Never raises.

    Total time, repair call included, stays inside timeout.
    """
    t0 = time.monotonic()
    deadline = t0 + max(0.0, float(timeout or 0))
    ev = evidence(rules, sample_turn)
    try:
        return _analyze(game_name, label, rules, sample_turn, deadline, thinking, ev, t0)
    except Exception:
        log.exception("analyze_variant failed for %s/%s" % (game_name, label))
        profile, analysis = evidence_profile(rules, sample_turn, ev)
        return profile, "", analysis


def _call(msgs, deadline, thinking, tag):
    budget = config.PREP_THINKING_BUDGET if thinking else None
    max_tokens = config.PREP_MAX_TOKENS if thinking else 1500
    return call_model(msgs, max_tokens, deadline, thinking_budget=budget, tag=tag,
                      cap_s=config.PREP_CALL_TIMEOUT_S, margin_s=0.3, min_s=1.0)


def _analyze(game_name, label, rules, sample_turn, deadline, thinking, ev, t0):
    msgs = _messages(game_name, label, rules, sample_turn)
    text = _call(msgs, deadline, thinking, "classify")
    obj = parse_json_object(text)
    fields, complaints = check_answer(obj, ev, rules) if obj else ({}, [])
    repair_used = False
    all_complaints = list(complaints)
    if obj and complaints and deadline - time.monotonic() >= CLASSIFY_REPAIR_MIN_S:
        repair_used = True
        log.info("classify %s/%s repair: %s" % (game_name, label, "; ".join(complaints)[:300]))
        rmsgs = msgs + [{"role": "assistant", "content": text},
                        {"role": "user", "content": templates.render(
                            "classify_repair.txt", COMPLAINTS="\n".join("- " + c for c in complaints))}]
        rthinking = thinking and deadline - time.monotonic() > 20
        obj2 = parse_json_object(_call(rmsgs, deadline, rthinking, "classify-repair"))
        if obj2:
            f2, c2 = check_answer(obj2, ev, rules)
            # Keep every field that passed in either answer; the repaired one wins.
            merged = dict(fields)
            merged.update({k: v for k, v in f2.items() if v not in ("", None) or k not in merged})
            fields, complaints = merged, c2
            all_complaints += ["after repair: " + c for c in c2]
    if not obj:
        log.warning("classify %s/%s: no usable model answer, profile from evidence" % (game_name, label))
    profile, playbook, analysis = resolve(fields if obj else None, ev, rules, sample_turn)
    analysis.update({"disagreements": all_complaints, "repair_used": repair_used,
                     "model_answered": bool(obj), "seconds": round(time.monotonic() - t0, 2)})
    log.info("classify %s/%s: family=%s (%s, %s) example=%r regex=%s repair=%s %.1fs" % (
        game_name, label, profile.family, analysis["family_source"], analysis["confidence"],
        profile.example_action[:40], bool(analysis.get("action_regex")), repair_used,
        analysis["seconds"]))
    return profile, playbook, analysis


# ---------------------------------------------------------------- resolution
def resolve(fields, ev, rules, sample_turn):
    """Code decides every field: validated model answer, else evidence, else default."""
    fields = fields or {}
    model_family = fields.get("family")
    if model_family:
        family, source = model_family, "model"
        confidence = "high" if ev.get("confident") and ev.get("family") == model_family else "medium"
    elif ev.get("confident"):
        family, source, confidence = ev["family"], "evidence", "medium"
    elif ev.get("family", "unknown") != "unknown":
        family, source, confidence = ev["family"], "evidence-weak", "low"
    else:
        family, source, confidence = "unknown", "default", "low"

    sim = ev.get("simultaneous")
    if sim is None:
        sim = fields.get("simultaneous")
    if sim is None:
        sim = _FAMILY_IMPLIES.get(family, {}).get("simultaneous")
    perf = ev.get("perfect_information")
    if perf is None:
        perf = fields.get("perfect_information")
    if perf is None:
        perf = _FAMILY_IMPLIES.get(family, {}).get("perfect_information")

    speech = fields.get("speech_restrictions") or ""
    if not speech and ev.get("forbidden_sentence") and (family == "guardrails" or ev["scores"].get("guardrails")):
        speech = ev["forbidden_sentence"]

    action_format = fields.get("action_format") or " ".join(ev.get("format_sentences") or [])

    legal = ev.get("legal_actions")
    rules_ex = ev.get("quoted_examples") or []
    if legal:
        example = fields.get("example_action") or legal[0]
        example_source = "model" if fields.get("example_action") else "sample"
    elif rules_ex:
        example, example_source = rules_ex[0], "rules"
    elif fields.get("example_action"):
        example, example_source = fields["example_action"], "model"
    elif fields.get("example_raw"):
        # Failed the format check, but an empty example is worse (L1 sends it).
        example, example_source = fields["example_raw"], "model-unverified"
    else:
        example, example_source = "", "none"

    ts = "talk_is_move" if ev.get("talk_is_move") or fields.get("turn_structure") == "talk_is_move" \
        else "simultaneous" if sim is True else "alternating" if sim is False \
        else fields.get("turn_structure", "unknown")

    profile = GameProfile(family=family, perfect_information=perf, simultaneous=sim,
                          speech_restrictions=speech, action_format=action_format[:ACTION_FORMAT_MAX_CHARS],
                          example_action=example, notes="family from %s (%s)" % (source, confidence))
    prepare_profile(profile, rules)
    analysis = {
        "harness_version": config.HARNESS_VERSION,
        "family": family, "family_source": source, "confidence": confidence,
        "runner_up": ev.get("runner_up", "unknown") if ev.get("family") == family else ev.get("family", "unknown"),
        "quote": fields.get("quote", ""), "quote_ok": bool(fields.get("quote_ok")),
        "action_regex": fields.get("action_regex", ""),
        "example_source": example_source,
        "rules_examples": list(rules_ex)[:8],   # for the ladder: other format-valid actions after a warning
        "turn_structure": ts,
        "hidden_information": fields.get("hidden_information", "none" if perf else ""),
        "chance": fields.get("chance", ""),
        "wants_simulator": family in SIMULATOR_FAMILIES,
        "evidence": {"scores": ev.get("scores", {}), "confident": ev.get("confident", False),
                     "family": ev.get("family", "unknown"), "legal_kind": ev.get("legal_kind"),
                     "legal_count": ev.get("legal_count")},
        "disagreements": [], "repair_used": False, "model_answered": False, "seconds": 0.0,
    }
    return profile, fields.get("playbook", ""), analysis


def evidence_profile(rules, sample_turn, ev=None):
    """Profile from code only (model down or no time). Returns (GameProfile, analysis)."""
    ev = ev or evidence(rules, sample_turn)
    profile, _, analysis = resolve(None, ev, rules, sample_turn)
    return profile, analysis


def _default_profile(sample_turn, rules=""):
    """The old default (kept for callers): family unknown, example from rules or sample."""
    p = GameProfile()
    p.example_action = example_from_rules(rules)
    if isinstance(sample_turn, dict) and isinstance(sample_turn.get("legal_actions"), list) \
            and sample_turn["legal_actions"]:
        p.example_action = str(sample_turn["legal_actions"][0])
    return p


# ---------------------------------------------------------------- analysis.json
def write_analysis(game_id, rhash, analysis):
    """Write analysis.json (call BEFORE memory.save_variant writes meta.json). Never raises."""
    try:
        if analysis:
            path = "%s/%s" % (memory.variant_dir(game_id, rhash), ANALYSIS_FILE)
            memory.write_json_atomic(path, analysis)
    except Exception:
        log.exception("write_analysis failed")


def load_analysis(game_id, rhash):
    """analysis.json of a variant, or None (missing, unreadable, other harness version)."""
    try:
        a = memory.read_json("%s/%s" % (memory.variant_dir(game_id, rhash), ANALYSIS_FILE))
        if isinstance(a, dict) and a.get("harness_version") == config.HARNESS_VERSION:
            return a
    except Exception:
        log.exception("load_analysis failed")
    return None


# ---------------------------------------------------------------- variants of one game
def align_families(specs, analyses):
    """Variants of one game share a family: set every spec to the majority family.

    specs: list of VariantSpec; analyses: dict rules_hash -> analysis (updated in
    place). Unknown votes count only when every variant is unknown. Tie: the
    family with the highest total evidence score. Returns the specs that changed.
    """
    try:
        votes = {}
        for s in specs:
            f = s.profile.family
            if f != "unknown":
                votes[f] = votes.get(f, 0) + 1
        if not votes:
            return []

        def ev_score(f):
            return sum((analyses.get(s.rules_hash) or {}).get("evidence", {}).get("scores", {}).get(f, 0)
                       for s in specs)
        best = max(votes, key=lambda f: (votes[f], ev_score(f), f))
        changed = []
        for s in specs:
            if s.profile.family != best:
                log.info("align family of %s/%s (%s): %s -> %s" % (
                    s.game_id, s.rules_hash, s.label, s.profile.family, best))
                s.profile.family = best
                s.profile.notes = (s.profile.notes + "; aligned to majority").strip("; ")
                a = analyses.get(s.rules_hash)
                if a is not None:
                    a["disagreements"] = list(a.get("disagreements", [])) + [
                        "family %s changed to majority %s" % (a.get("family"), best)]
                    a["family"] = best
                    a["family_source"] = "aligned"
                    a["wants_simulator"] = best in SIMULATOR_FAMILIES
                changed.append(s)
        return changed
    except Exception:
        log.exception("align_families failed")
        return []
