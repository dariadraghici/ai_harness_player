"""Test game (Guessing family): find your own secret number. Owner: luckydevs-1.

More than 50 possible actions, so legal_actions is null and the bot must use
the action format from the rules. Loaded by arena_local, not in the image.
"""

import re

RULES = """Number race. The referee picks a secret whole number from 1 to {hi} for EACH player.
Players alternate turns, seat 0 first. On your turn you guess YOUR OWN secret number.
Action format: the word guess, one space, then a whole number, exactly like "guess 50".
After each guess you are told "higher" (your secret is larger), "lower" (it is smaller) or "correct".
The first player to guess correctly wins. If nobody is correct after {max_guesses} guesses each, it is a draw.
Your feedback is private; the opponent only sees how many guesses you made. Chat is free."""

_ACTION = re.compile(r"^guess (\d+)$")


class Game:
    game_id = "guess_number"
    game_name = "Number race"
    family = "guessing"
    simultaneous = False
    variants = [
        {"label": "hi100", "rules": RULES.format(hi=100, max_guesses=10), "settings": {"hi": 100, "max_guesses": 10}},
        {"label": "hi200", "rules": RULES.format(hi=200, max_guesses=11), "settings": {"hi": 200, "max_guesses": 11}},
    ]

    def new(self, variant, rng):
        st = variant["settings"]
        return {"hi": st["hi"], "max": st["max_guesses"],
                "secret": [rng.randint(1, st["hi"]), rng.randint(1, st["hi"])],
                "log": [[], []], "to_move": 0, "winner": None}

    def to_move(self, s):
        return [s["to_move"]]

    def over(self, s):
        return s["winner"] is not None or all(len(l) >= s["max"] for l in s["log"])

    def scores(self, s):
        w = s["winner"]
        return [1, 0] if w == 0 else [0, 1] if w == 1 else [0, 0]

    def observation(self, s, seat):
        mine = ", ".join("%d -> %s" % (g, fb) for g, fb in s["log"][seat]) or "none"
        return ("Your secret is between 1 and %d. Your guesses so far: %s. "
                "Opponent guesses so far: %d. Guesses left for you: %d." % (
                    s["hi"], mine, len(s["log"][1 - seat]), s["max"] - len(s["log"][seat])))

    def legal(self, s, seat):
        return None   # more than 50 actions

    def check(self, s, seat, action):
        m = _ACTION.match(action or "")
        if not m:
            return False, 'action must look like "guess 50"'
        if not 1 <= int(m.group(1)) <= s["hi"]:
            return False, "number out of range"
        return True, ""

    def apply(self, s, actions):
        seat = s["to_move"]
        g = int(_ACTION.match(actions[seat]).group(1))
        sec = s["secret"][seat]
        fb = "correct" if g == sec else "higher" if sec > g else "lower"
        s["log"][seat].append((g, fb))
        if fb == "correct":
            s["winner"] = seat
        s["to_move"] = 1 - seat

    def random_action(self, s, seat, rng):
        lo, hi = 1, s["hi"]
        for g, fb in s["log"][seat]:
            if fb == "higher":
                lo = max(lo, g + 1)
            elif fb == "lower":
                hi = min(hi, g - 1)
        return "guess %d" % rng.randint(lo, max(lo, hi))


GAME = Game()
