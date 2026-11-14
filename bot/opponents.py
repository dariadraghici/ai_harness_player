"""Compact opponent behavior profiles in <memory>/opponents/<team_id>.json."""

import hashlib
import re
import threading
import time

from . import log, memory

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


def save_all():
    """Profiles are written atomically as each game ends."""
    return None
