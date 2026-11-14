"""Final validation and emission of turn actions.

The decision ladder may propose actions from several sources, but this module
is the only place that turns one into a protocol action object.
"""

import os
import re
import time

from . import config, log, memory, protocol
from .engines import guardrails
from .engines import wordle
from .prep import classify

_BLACKLIST = {}
_MAX_REJECTIONS = 64 * 1024


def normalize(value):
    return " ".join(str(value or "").split()).casefold()


def _game_key(ctx):
    start = ctx.get("start") or {}
    turn = ctx.get("turn") or {}
    return (
        str(start.get("match_id") or "local"),
        str(start.get("game_index") or turn.get("game_index") or 0),
        str(start.get("game_id") or "unknown"),
    )


def _grammar_path(spec):
    if not spec:
        return None
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "grammar.json")


def _range(rules):
    text = str(rules or "")
    patterns = (
        r"\bfrom\s+(-?\d+)\s+to\s+(-?\d+)\b",
        r"\bbetween\s+(-?\d+)\s+and\s+(-?\d+)\b",
        r"\b(?:number|integer|value)\s+in\s+the\s+range\s+(-?\d+)\s*[-.]\.?\s*(-?\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            return (min(low, high), max(low, high))
    return None


def build_grammar(rules="", sample_turn=None, profile=None):
    """Build a conservative JSON grammar from rules and prep evidence."""
    rules = str(rules or "")
    sample_turn = sample_turn if isinstance(sample_turn, dict) else {}
    legal = sample_turn.get("legal_actions")
    exact = [str(a) for a in legal if isinstance(a, (str, int, float))] if isinstance(legal, list) else []
    examples = classify.compatible_examples(rules, sample_turn)
    examples = list(dict.fromkeys(exact + [str(a) for a in examples]))
    length = classify.action_length(rules, sample_turn)
    action_format = str(getattr(profile, "action_format", "") or "")
    lower = (rules + " " + action_format).casefold()
    word_action = bool(re.search(r"\b(?:guess|word)\b", lower)) and length is not None
    dictionary = []
    dictionary_match = re.search(
        r"\b(?:allowed|valid|accepted)\s+(?:guess\s+)?words?\s*:\s*([^\n.]+)",
        rules, re.IGNORECASE)
    if dictionary_match:
        dictionary = sorted(set(re.findall(r"\b[A-Za-z]+\b", dictionary_match.group(1).casefold())))
    for example in examples:
        words = re.findall(r"[A-Za-z]+", example.casefold())
        if words and word_action:
            dictionary.append(words[-1])
    value_range = _range(rules)
    total = None
    total_match = re.search(r"\b(?:sum|total)\s+(?:of\s+)?(?:exactly\s+)?(\d+)\b", rules, re.I)
    if total_match:
        total = int(total_match.group(1))
    count_match = re.search(r"\b(\d+)\s+fields?\b", rules, re.IGNORECASE)
    field_count = int(count_match.group(1)) if count_match else None

    if word_action:
        syntax = r"^\s*(?:guess\s+)?[A-Za-z]+(?:\s+[A-Za-z]+)*\s*$"
    elif exact:
        syntax = r"^\s*.+\s*$"
    elif value_range is not None:
        syntax = r"^\s*(?:[A-Za-z_]+\s+)?-?\d+\s*$"
    elif re.search(r"\b(?:roll|hold|pass|fold|call|check|accept|reject)\b", lower):
        syntax = r"^\s*[A-Za-z]+(?:\s+[A-Za-z]+)*\s*$"
    else:
        syntax = r"^\s*.+\s*$"
    return {
        "version": 1,
        "syntax": syntax,
        "examples": examples[:64],
        "exact": exact[:256],
        "word_length": length if word_action else None,
        "dictionary": sorted(set(dictionary))[:10000],
        "range": list(value_range) if value_range else None,
        "total": total,
        "field_count": field_count,
        "case_sensitive": bool(exact) and any(a != a.casefold() for a in exact),
    }


def save_grammar(spec):
    """Persist the derived grammar atomically; malformed inputs never abort prep."""
    try:
        path = _grammar_path(spec)
        if path:
            memory.write_json_atomic(path, build_grammar(spec.rules, spec.sample_turn, spec.profile))
            return True
    except Exception:
        log.exception("cannot save action grammar")
    return False


def _load_grammar(spec, ctx):
    try:
        path = _grammar_path(spec)
        grammar = memory.read_json(path, None) if path else None
        if isinstance(grammar, dict) and grammar.get("version") == 1:
            return grammar
        return build_grammar(getattr(spec, "rules", ""), (ctx.get("turn") or {}), getattr(spec, "profile", None))
    except Exception:
        log.exception("cannot load action grammar")
        return {"version": 1, "syntax": r"^\s*.+\s*$", "examples": [], "exact": []}


def _valid(action, ctx, grammar, legal=None, rejected=None):
    if not isinstance(action, str) or not action.strip():
        return False
    action = " ".join(action.split())
    if rejected is not None and normalize(action) == normalize(rejected):
        return False
    blocked = _BLACKLIST.get(_game_key(ctx), set())
    if normalize(action) in blocked:
        return False
    if legal:
        for item in legal:
            if normalize(item) == normalize(action):
                return item
        return False
    if not re.fullmatch(grammar.get("syntax", r"\s*.+\s*"), action):
        return False
    length = grammar.get("word_length")
    if length is not None:
        words = re.findall(r"[A-Za-z]+", action)
        if not words or len(words[-1]) != int(length):
            return False
        dictionary = grammar.get("dictionary") or []
        if dictionary and words[-1].casefold() not in dictionary:
            return False
        spec = ctx.get("spec")
        if wordle.is_wordle_rules(getattr(spec, "rules", "")) \
                and not wordle.allowed_word(words[-1], int(length)):
            return False
        observation = str((ctx.get("turn") or {}).get("observation") or "")
        if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(action.casefold()),
                     observation.casefold()):
            return False
    value_range = grammar.get("range")
    if value_range is not None:
        numbers = re.findall(r"-?\d+", action)
        if not numbers or not (int(value_range[0]) <= int(numbers[-1]) <= int(value_range[1])):
            return False
    total = grammar.get("total")
    if total is not None:
        numbers = [int(value) for value in re.findall(r"-?\d+", action)]
        if len(numbers) >= 2 and sum(numbers) != int(total):
            return False
    return action


def _record_warning(ctx, warning):
    if not isinstance(warning, dict):
        return
    rejected = warning.get("rejected")
    if not isinstance(rejected, str) or not rejected.strip():
        return
    key = _game_key(ctx)
    _BLACKLIST.setdefault(key, set()).add(normalize(rejected))
    spec = ctx.get("spec")
    try:
        path = os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "rejections.log") \
            if spec else os.path.join(memory.root(), "rejections.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s\t%s\t%s\n" % (
                time.time(), warning.get("reason", "rejected"), rejected[:200]))
        if os.path.getsize(path) > _MAX_REJECTIONS:
            with open(path, "rb") as handle:
                tail = handle.read()[-_MAX_REJECTIONS // 2:]
            memory.write_bytes_atomic(path, tail)
    except Exception:
        log.exception("cannot record rejected action")
    log.warning("action rejected by arena: %r (%s)" % (rejected, warning.get("reason", "unknown")))


def _candidate_parts(candidate):
    if isinstance(candidate, dict):
        return candidate.get("action"), candidate.get("chat", "")
    return getattr(candidate, "action", None), getattr(candidate, "chat", "")


def _last_resort(grammar, ctx):
    examples = grammar.get("examples") or []
    for action in examples:
        if _valid(action, ctx, grammar, None, None):
            return action
    value_range = grammar.get("range")
    if value_range:
        return str((int(value_range[0]) + int(value_range[1])) // 2)
    length = grammar.get("word_length")
    if length:
        dictionary = grammar.get("dictionary") or []
        word = dictionary[0] if dictionary else ("a" * int(length))
        prefix = "guess " if "guess" in str(getattr(ctx.get("spec"), "rules", "")).casefold() else ""
        return prefix + word
    total = grammar.get("total")
    field_count = grammar.get("field_count")
    if total is not None and field_count:
        values = [int(total) // int(field_count)] * int(field_count)
        for index in range(int(total) % int(field_count)):
            values[index] += 1
        return " ".join(str(value) for value in values)
    return "pass"


def emit_action(game_ctx, candidates, deadline):
    """Return one validated protocol action, never raising.

    Candidates must be ordered by preference. The first valid candidate wins;
    the final candidates are deterministic grammar-derived fallbacks.
    """
    try:
        ctx = game_ctx if isinstance(game_ctx, dict) else {}
        turn = ctx.get("turn") or {}
        spec = ctx.get("spec")
        warning = turn.get("warning")
        _record_warning(ctx, warning)
        legal = turn.get("legal_actions") if isinstance(turn.get("legal_actions"), list) else None
        grammar = _load_grammar(spec, ctx)
        profile = getattr(spec, "profile", None)
        rejected = warning.get("rejected") if isinstance(warning, dict) else None
        pool = list(candidates or [])
        if legal:
            pool.extend({"action": item, "chat": ""} for item in legal)
        else:
            pool.extend({"action": item, "chat": ""} for item in grammar.get("examples", []))
            if profile and profile.example_action:
                pool.append({"action": profile.example_action, "chat": ""})
            value_range = grammar.get("range")
            if value_range:
                pool.append({"action": str((int(value_range[0]) + int(value_range[1])) // 2), "chat": ""})
        for candidate in pool:
            action, chat = _candidate_parts(candidate)
            valid = _valid(action, ctx, grammar, legal, rejected)
            if valid:
                try:
                    valid = guardrails.filter_outgoing(valid, "action", profile) or ""
                except Exception:
                    log.exception("action guardrails failed")
                    valid = ""
            if valid:
                return protocol.action(valid, str(chat or config.CHAT_FALLBACK)[:200])
        action = _last_resort(grammar, ctx)
        valid = _valid(action, ctx, grammar, legal, None)
        if not valid:
            raise ValueError("no grammar-valid action candidate")
        return protocol.action(valid, config.CHAT_FALLBACK)
    except Exception:
        log.exception("action emission failed")
        return protocol.action(_last_resort(locals().get("grammar", {}), locals().get("ctx", {})),
                               config.CHAT_FALLBACK)
