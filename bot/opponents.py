"""Compact opponent behavior profiles in <memory>/opponents/<team_id>.json."""

import hashlib
import re
import threading
import time

from . import config, log, memory

_lock = threading.Lock()
_OFFER_WORDS = re.compile(r"\b(?:offer|propose|bid|split|accept|reject)\b", re.I)
_COMMITMENT_WORDS = re.compile(r"\b(?:promise|agree|cooperate|trust)\b", re.I)
_PRESSURE_WORDS = re.compile(r"\b(?:must|only chance|last chance|or else|walk away)\b", re.I)
_HEDGE_WORDS = re.compile(r"\b(?:maybe|perhaps|probably|might|not sure)\b", re.I)
_MOVE_WORDS = re.compile(r"\b(?:accept|reject|offer|bid|cooperate|betray|pass|fold|raise|call)\b", re.I)


def load(team_id):
    profile = memory.read_json(memory.opponent_path(team_id), {})
    return profile if isinstance(profile, dict) else {}


def _count(container, key, amount=1):
    value = container.get(key, 0)
    try:
        container[key] = max(0, int(value)) + amount
    except (TypeError, ValueError):
        container[key] = amount


def _number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def summary(team_id):
    """Return a short aggregate-only summary, never raw opponent chat."""
    p = load(team_id)
    if not p:
        return ""
    games = p.get("games", 0)
    outcomes = "we won %s, lost %s, drew %s" % (
        p.get("win", 0), p.get("loss", 0), p.get("draw", 0))
    chat = p.get("chat_profile") if isinstance(p.get("chat_profile"), dict) else {}
    seen = _number(chat.get("opponent_messages"))
    details = []
    if seen:
        details.append("%d opponent chat/move lines observed" % seen)
        injections = _number(chat.get("injection_attempts"))
        if injections:
            details.append("%d flagged instruction-like lines" % injections)
        reactive = _number(chat.get("after_our_chat"))
        if reactive:
            details.append("%d lines followed our team's chat in the transcript" % reactive)
        styles = (
            ("offers", "offer_lines"),
            ("commitments", "commitment_lines"),
            ("pressure", "pressure_lines"),
            ("hedging", "hedge_lines"),
        )
        strongest = sorted(
            ((name, _number(chat.get(key))) for name, key in styles),
            key=lambda item: item[1],
            reverse=True,
        )
        if strongest and strongest[0][1]:
            details.append("most observed style: %s (%d)" % strongest[0])
    suffix = " Chat: " + "; ".join(details) + "." if details else ""
    return "Past games vs this team: %s (%s).%s" % (games, outcomes, suffix)


def _chat_signature(entry):
    fields = (
        entry.get("from"), entry.get("game"), entry.get("game_name"),
        entry.get("phase"), entry.get("turn"), entry.get("kind"), entry.get("text"),
    )
    return hashlib.sha256("\x1f".join(str(x or "") for x in fields).encode("utf-8")).hexdigest()


def _record_chat(p, entries):
    if not isinstance(entries, list):
        return
    chat = p.setdefault("chat_profile", {})
    if not isinstance(chat, dict):
        chat = p["chat_profile"] = {}
    previous = p.get("seen_chat", [])
    seen = set(previous) if isinstance(previous, list) else set()
    last_sender = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sender = entry.get("from")
        if sender not in ("you", "opponent"):
            continue
        text = entry.get("text")
        text = text if isinstance(text, str) else ""
        signature = _chat_signature(entry)
        if signature not in seen:
            if sender == "opponent":
                _count(chat, "opponent_messages")
                if entry.get("kind") == "move":
                    _count(chat, "move_lines")
                    if _MOVE_WORDS.search(text):
                        _count(chat, "recognized_move_lines")
                else:
                    _count(chat, "chat_lines")
                from .chat.defense import injection_flags
                if injection_flags(text):
                    _count(chat, "injection_attempts")
                if _OFFER_WORDS.search(text):
                    _count(chat, "offer_lines")
                if _COMMITMENT_WORDS.search(text):
                    _count(chat, "commitment_lines")
                if _PRESSURE_WORDS.search(text):
                    _count(chat, "pressure_lines")
                if _HEDGE_WORDS.search(text):
                    _count(chat, "hedge_lines")
                if last_sender == "you":
                    _count(chat, "after_our_chat")
            else:
                _count(chat, "our_messages")
            seen.add(signature)
        last_sender = sender
    p["seen_chat"] = list(seen)[-300:]


def record_game_end(team_id, end_msg, game_id=None):
    """Aggregate outcomes and observable chat behavior; never retain raw chat."""
    if not team_id:
        return
    try:
        with _lock:
            p = load(team_id)
            p["team_id"] = str(team_id)
            _count(p, "games")
            res = end_msg.get("result") if isinstance(end_msg, dict) else None
            if res in ("win", "loss", "draw"):
                _count(p, res)
            by_game = p.setdefault("by_game", {})
            if not isinstance(by_game, dict):
                by_game = p["by_game"] = {}
            game = by_game.setdefault(str(game_id or "unknown"), {"games": 0})
            if not isinstance(game, dict):
                game = by_game[str(game_id or "unknown")] = {"games": 0}
            _count(game, "games")
            if res in ("win", "loss", "draw"):
                _count(game, res)
            if isinstance(end_msg, dict):
                _record_chat(p, end_msg.get("chat"))
            p["updated"] = int(time.time())
            memory.write_json_atomic(memory.opponent_path(team_id), p)
    except Exception:
        log.exception("opponents.record_game_end failed")


# ---------------------------------------------------------------- influence: who is listening (PROMPT-CHAT 6)
CLASSES = ("silent", "scripted", "reactive", "unknown")
MIN_DECISIONS = 3                 # our decisions before the chat of this match can say anything
SILENT_SHARE = 0.2                # their lines per decision at or below this: nobody writes
SCRIPTED_SIMILARITY = 0.7         # average similarity of consecutive lines at or above this: templated chatter
REFERENCE_SHARE = 0.34            # share of their replies that echo our content words
_STOP = {"the", "and", "that", "this", "with", "have", "your", "you", "are", "for", "was", "but", "not", "can",
         "will", "what", "when", "then", "than", "they", "them", "their", "there", "from", "about", "would", "could",
         "should", "just", "like", "game", "move", "turn", "here", "into", "over", "some", "very", "much", "more",
         "now", "its", "let", "got", "our", "out", "all", "one", "too", "also", "still", "well"}
_WORD = re.compile(r"[a-z0-9']+")


def content_words(text, min_len=4):
    return set(w for w in _WORD.findall(str(text or "").casefold()) if len(w) >= min_len and w not in _STOP)


def _similarity(a, b):
    wa, wb = content_words(a, 3), content_words(b, 3)
    if not wa and not wb:
        return 1.0 if str(a).strip() == str(b).strip() else 0.0
    return len(wa & wb) / float(len(wa | wb) or 1)


def classify(ctx, decisions, prior=None, tactic_stats=None):
    """(class, signals): silent, scripted, reactive or unknown, from the match chat so far. Pure and cheap.

    ctx: ChatContext of the whole match chat. decisions: how many decisions of ours this match has seen.
    prior: the class this team had in earlier matches (used until this match has evidence).
    tactic_stats: {tactic: {"scored": n, "hits": n}} of earlier influence attempts against this team."""
    theirs = [l for l in (ctx.theirs if ctx else []) if getattr(l, "kind", "chat") != "move"]
    texts = [l.text for l in theirs]
    n = max(0, int(decisions))
    signals = {"decisions": n, "their_lines": len(texts)}
    # echo: their line right after one of ours that shares content words with it
    pairs = refs = 0
    last_ours = None
    for line in (ctx.all_lines if ctx else []):
        if line.sender == "you":
            last_ours = line
        elif last_ours is not None and getattr(line, "kind", "chat") != "move":
            pairs += 1
            if content_words(last_ours.text, 5) & content_words(line.text, 5):
                refs += 1
            last_ours = None
    signals.update(reply_pairs=pairs, echoes=refs)
    ratio = (refs / float(pairs)) if pairs else 0.0
    sims = [_similarity(texts[i], texts[i + 1]) for i in range(max(0, len(texts) - 6), len(texts) - 1)]
    dupes = len(texts) - len(set(" ".join(t.split()).casefold() for t in texts))
    signals.update(repetition=round(sum(sims) / len(sims), 2) if sims else 0.0, duplicates=dupes)
    stats = tactic_stats or {}
    real = [v for k, v in stats.items() if k != "control" and isinstance(v, dict)]     # the baseline is not an attempt
    scored = sum(_number(v.get("scored")) for v in real)
    hits = sum(_number(v.get("hits")) for v in real)
    signals.update(scored=scored, hits=hits)
    reactive = (refs >= 2 and ratio >= REFERENCE_SHARE) or (scored >= 3 and hits / float(scored) >= 0.5)
    scripted = len(texts) >= 3 and ((sims and sum(sims) / len(sims) >= SCRIPTED_SIMILARITY) or dupes >= 2)
    if n >= MIN_DECISIONS and len(texts) <= SILENT_SHARE * n:
        cls = "silent"
    elif reactive:
        cls = "reactive"
    elif scripted:
        cls = "scripted"
    else:
        cls = "unknown"
    if cls == "unknown" and prior in ("silent", "scripted", "reactive") and n < MIN_DECISIONS:
        cls = prior                       # no evidence yet in this match: what earlier matches taught us
    return cls, signals


def influence_section(profile):
    """The influence part of a profile, reset when it was written by another harness version."""
    sec = profile.get("influence") if isinstance(profile, dict) else None
    if not isinstance(sec, dict) or sec.get("version") != config.HARNESS_VERSION:
        sec = {"version": config.HARNESS_VERSION, "classes": {}, "tactics": {}, "last_lines": [], "tells": {},
               "by_family": {}}
    for key, default in (("classes", {}), ("tactics", {}), ("last_lines", []), ("tells", {}), ("by_family", {})):
        if not isinstance(sec.get(key), type(default)):
            sec[key] = default
    return sec


def prior_class(team_id):
    """The class this team showed most often in earlier matches (needs two sightings), else None."""
    try:
        counts = influence_section(load(team_id))["classes"]
        known = sorted(((_number(v), k) for k, v in counts.items() if k in ("silent", "scripted", "reactive")),
                       reverse=True)
        return known[0][1] if known and known[0][0] >= 2 else None
    except Exception:
        return None


def tactic_stats(team_id):
    try:
        return dict(influence_section(load(team_id))["tactics"])
    except Exception:
        return {}


def record_class(team_id, cls, signals=None, last_lines=()):
    """Count the class seen in this match once per game end (small, atomic, versioned)."""
    if not team_id or cls not in CLASSES:
        return
    try:
        with _lock:
            p = load(team_id)
            p["team_id"] = str(team_id)
            sec = influence_section(p)
            _count(sec["classes"], cls)
            if signals:
                sec["last_signals"] = {k: v for k, v in signals.items() if isinstance(v, (int, float))}
            lines = [" ".join(str(t).split())[:80] for t in last_lines if t][-3:]
            if lines:
                sec["last_lines"] = lines
            p["influence"] = sec
            memory.write_json_atomic(memory.opponent_path(team_id), p)
    except Exception:
        log.exception("opponents.record_class failed")


def _bump(row, scored, hit):
    _count(row, "attempts")
    if scored:
        _count(row, "scored")
        if hit:
            _count(row, "hits")


def global_path():
    import os
    return os.path.join(memory.root(), "influence_global.json")


def _global_section():
    g = memory.read_json(global_path(), {})
    if not isinstance(g, dict) or g.get("version") != config.HARNESS_VERSION or not isinstance(g.get("by_family"), dict):
        g = {"version": config.HARNESS_VERSION, "by_family": {}}
    return g


def record_tactic(team_id, tactic, scored, hit, control=False, family=None):
    """Per-tactic attempts, scored attempts and hits against this team (controls are counted apart), per family, and
    the same over all teams in /memory/influence_global.json (the prior of the bandit). A lost update only loses a
    count: several containers write at once."""
    if not tactic:
        return
    key = "control" if control else str(tactic)
    try:
        with _lock:
            if team_id:
                p = load(team_id)
                p["team_id"] = str(team_id)
                sec = influence_section(p)
                _bump(sec["tactics"].setdefault(key, {"attempts": 0, "scored": 0, "hits": 0}), scored, hit)
                if family:
                    fam = sec["by_family"].setdefault(str(family), {})
                    _bump(fam.setdefault(key, {"attempts": 0, "scored": 0, "hits": 0}), scored, hit)
                p["influence"] = sec
                memory.write_json_atomic(memory.opponent_path(team_id), p)
            if family and scored:
                g = _global_section()
                _bump(g["by_family"].setdefault(str(family), {}).setdefault(
                    key, {"attempts": 0, "scored": 0, "hits": 0}), scored, hit)
                memory.write_json_atomic(global_path(), g)
    except Exception:
        log.exception("opponents.record_tactic failed")


def arm_stats(team_id, family):
    """(own, global): {arm key: {"scored", "hits"}} for this team and for all teams, for one family."""
    try:
        own = influence_section(load(team_id))["by_family"].get(str(family), {}) if team_id else {}
        glob = _global_section()["by_family"].get(str(family), {})
        return dict(own), dict(glob)
    except Exception:
        return {}, {}


def save_all():
    """Profiles are written atomically as each game ends."""
    return None
