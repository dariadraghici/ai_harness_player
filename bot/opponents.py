"""Opponent profiles in <memory>/opponents/<team_id>.json.

Owner: luckydevs-2. Status: stub (basic counters only).

Done means: per opponent team we know how they chat (tone, honesty of claims,
injection attempts), how they play per family, and whether they react to our
chat. decide.py reads summary() and puts it into the decision prompt.
"""

import threading
import time

from . import log, memory

_lock = threading.Lock()


def load(team_id):
    return memory.read_json(memory.opponent_path(team_id), {}) or {}


def summary(team_id):
    """Short text for the decision prompt. Empty when we know nothing."""
    p = load(team_id)
    if not p:
        return ""
    return "Past games vs this team: %d (we won %d, lost %d, drew %d)." % (
        p.get("games", 0), p.get("win", 0), p.get("loss", 0), p.get("draw", 0))


def record_game_end(team_id, end_msg, game_id=None):
    """Update counters after an end message. Never raises.

    Read-modify-write is not atomic across containers; a lost update of a
    counter is acceptable. The write itself is atomic.
    """
    if not team_id:
        return
    try:
        with _lock:
            p = load(team_id)
            p["team_id"] = team_id
            p["games"] = p.get("games", 0) + 1
            res = end_msg.get("result")
            if res in ("win", "loss", "draw"):
                p[res] = p.get(res, 0) + 1
            p.setdefault("by_game", {})
            g = p["by_game"].setdefault(str(game_id), {"games": 0})
            g["games"] += 1
            p["updated"] = int(time.time())
            memory.write_json_atomic(memory.opponent_path(team_id), p)
    except Exception:
        log.exception("opponents.record_game_end failed")


def save_all():
    """Called on shutdown. Profiles are written as we go, nothing to flush."""
    return None
