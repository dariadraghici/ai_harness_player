"""Scoring of influence predictions (PROMPT-CHAT section 7).

Owner: luckydevs-2 area. Status: real (phase 1: the simple `hit` case). Standard library only.

Every influence attempt commits a falsifiable prediction: the opponent will (or will not) play `target_action`. It
is checked later, from what the observation shows:

  * sequential games: the opponent answers our line in their next move, which we see at our next decision
    (lag 0, one decision after the send);
  * simultaneous games: our line reaches them in their NEXT round, so their reaction is the round after that
    (lag 1, one extra decision before the score);
  * a `warning` on the turn means our previous answer was rejected and its chat was dropped: nothing arrived,
    nothing is scored (the goal is sent again);
  * `their_quality_drop` (the honest metric; `hit` alone can be luck) needs the engine's reply table
    (goal.replies): the value, for us, of every reply they had. The reply they played is the one whose name
    appeared in the new text; the drop is how much better for us than their best reply it was, in our value units
    (0 = they played their best reply). Only when the played reply is unambiguous and the game is sequential.

What the opponent did is taken from the observation itself, without game-specific parsing: the lines that are new
since the reference observation, and whether the target action appears in them.
"""

import re

from ..interfaces import Outcome


def new_text(reference, current):
    """The lines of `current` that were not in `reference` (what happened since), joined."""
    before = set(" ".join(l.split()) for l in str(reference or "").splitlines())
    fresh = [" ".join(l.split()) for l in str(current or "").splitlines()]
    return "\n".join(l for l in fresh if l and l not in before)


def occurrences(text, action):
    action = " ".join(str(action or "").split())
    if not action:
        return 0
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(x) for x in action.split()) + r"(?!\w)"
    return len(re.findall(pattern, str(text or ""), re.I))


def appears(text, action):
    return occurrences(text, action) > 0


def appeared_since(reference, current, action):
    """The action shows up MORE often than before. A history line that is rewritten every move ("Moves so far: ...")
    repeats old moves; only a new occurrence counts as something the opponent just did."""
    return occurrences(current, action) > occurrences(reference, action)


def quality_drop(goal, reference, current):
    """(drop, exact) of the reply they played, from goal.replies; (None, None) when it cannot be told."""
    try:
        rows = (getattr(goal, "replies", None) or {}).get("rows") or []
        if len(rows) < 2 or int(getattr(goal, "lag", 0) or 0) != 0:
            return None, None
        played = [r for r in rows if appeared_since(reference, current, r["action"])]
        if len(played) != 1:
            return None, None
        best = min(float(r["value"]) for r in rows)             # their best reply = the lowest value for us
        drop = max(0.0, float(played[0]["value"]) - best)
        return round(drop, 4), all(bool(r.get("exact")) for r in rows)
    except Exception:
        return None, None


def due(goal, decisions_since):
    """A prediction can be scored once the decisions since the send reach 1 + lag."""
    return decisions_since >= 1 + int(getattr(goal, "lag", 0) or 0)


def score_prediction(prev_goal, turn_msg, specialist=None, reference_observation="", decisions_since=1):
    """Outcome of the previous goal, judged at this turn. Never raises."""
    try:
        if prev_goal is None:
            return Outcome(reason="no previous goal")
        base = dict(tactic_id=prev_goal.tactic_id, lag=int(prev_goal.lag or 0), is_control=bool(prev_goal.is_control))
        if isinstance(turn_msg, dict) and turn_msg.get("warning"):
            return Outcome(reason="warning: the previous answer was rejected, its chat never arrived", **base)
        if not due(prev_goal, decisions_since):
            return Outcome(reason="too early (lag)", **base)
        target = (prev_goal.target_action or "").strip()
        if not target:
            return Outcome(reason="no target action", **base)
        current = (turn_msg or {}).get("observation")
        fresh = new_text(reference_observation, current)
        seen = appeared_since(reference_observation, current, target)
        hit = (not seen) if prev_goal.kind == "deter_move" else seen
        drop, exact = quality_drop(prev_goal, reference_observation, current)
        return Outcome(scored=True, hit=bool(hit), their_action=" ".join(fresh.split())[:80], reason="scored",
                       their_quality_drop=drop, drop_exact=exact, **base)
    except Exception:
        return Outcome(reason="score failed")
