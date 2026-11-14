"""Test game (Think ahead family): Nim whose heap sizes are part of the RULES TEXT.

Owner: luckydevs-1. Our own test game for the organizers' practice arena
(a subclass of arena.core.Game). Run it with:
  python scripts/practice.py --game nim_deal --prep

Why it exists: in some tournament games the rules text embeds the setup of
the game (ARENA-CONTRACT, kit note K2). Then the rules text in `prep` (a
sample deal) differs from the rules text in `start` (the real deal). This
game does exactly that: the heaps are dealt from the seed and written into
the rules. A prepared specialist must still be found at `start` (by variant
label), and nothing built in prep may hard-code the sample heaps.
Not part of the bot image.
"""

import random
import re

from arena.core import Game

NAMES = "ABCDE"
_ACTION = re.compile(r"^take (\d+) from ([a-e])$")


class NimDeal(Game):
    id = "nim_deal"
    name = "Nim (dealt heaps)"
    category = "think"
    blurb = "Take stones from one heap at a time; whoever takes the last stone decides the game."
    howto = "On your turn take any number of stones from one heap. Normal play: taking the last stone wins. Misere: it loses."
    duplicate_safe = True   # open position, no randomness after the deal

    @classmethod
    def variants(cls):
        return [{"label": "3 heaps, last stone wins", "heaps": 3, "misere": False},
                {"label": "4 heaps, last stone loses", "heaps": 4, "misere": True}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        n = min(len(NAMES), max(2, int(self.variant.get("heaps", 3))))
        self.misere = bool(self.variant.get("misere", False))
        self.start = [self.rng.randint(2, 7) for _ in range(n)]    # the deal
        self.heaps = list(self.start)
        self.current = 0
        self.history = []            # (seat, action)
        self.winner = None

    def _show(self, heaps):
        return ", ".join("%s=%d" % (NAMES[i], h) for i, h in enumerate(heaps))

    def rules(self):
        last = "LOSES" if self.misere else "WINS"
        return ("NIM - %s. 2 players, alternating turns, seat 0 moves first.\n\n"
                "SETUP OF THIS GAME\nHeaps of stones: %s.\n\n"
                "ACTION FORMAT\n    take <n> from <heap>\n"
                "where <heap> is a heap letter and <n> is from 1 to the number of stones in that heap. "
                "Example:  \"take 2 from A\"\n\n"
                "GOAL\nOn your turn you take one or more stones from exactly one heap. "
                "The player who takes the last stone %s.\n\n"
                "WHAT YOU OBSERVE\nThe current heaps and every move so far. Chat is free." % (
                    self.variant.get("label", ""), self._show(self.start), last))

    def to_move(self):
        return [] if self.is_over() else [self.current]

    def observation(self, seat):
        moves = "\n".join("  %d. %s: %s" % (i + 1, "you" if s == seat else "opponent", a)
                          for i, (s, a) in enumerate(self.history)) or "  (none yet)"
        head = "NIM (%s). You are seat %d. Last stone %s." % (
            self.variant.get("label", ""), seat, "loses" if self.misere else "wins")
        state = "Heaps now: %s." % self._show(self.heaps)
        if self.is_over():
            return "%s\n%s\nThe game is over.\nMoves:\n%s" % (head, state, moves)
        turn = "It is YOUR turn: \"take <n> from <heap>\"." if seat == self.current else "It is the opponent's turn."
        return "%s\n%s\nMoves so far:\n%s\n%s" % (head, state, moves, turn)

    def legal_actions(self, seat):
        if self.is_over() or seat != self.current:
            return []
        return self.cap_list(["take %d from %s" % (n, NAMES[i]) for i, h in enumerate(self.heaps)
                              for n in range(1, h + 1)])

    def normalize(self, seat, action):
        if self.is_over() or seat != self.current:
            return None
        m = _ACTION.match(self.clean(action).strip(" .!\"'`"))
        if not m:
            return None
        n, i = int(m.group(1)), NAMES.lower().index(m.group(2))
        if i >= len(self.heaps) or not 1 <= n <= self.heaps[i]:
            return None
        return "take %d from %s" % (n, NAMES[i])

    def apply(self, actions):
        seat = self.current
        a = self.normalize(seat, actions.get(seat, ""))
        if a is None:
            raise ValueError("illegal action %r for seat %d" % (actions.get(seat), seat))
        m = _ACTION.match(a.lower())
        self.heaps[NAMES.lower().index(m.group(2))] -= int(m.group(1))
        self.history.append((seat, a))
        if sum(self.heaps) == 0:
            self.winner = (1 - seat) if self.misere else seat
        self.current = 1 - seat

    def is_over(self):
        return self.winner is not None

    def result(self):
        w = self.winner
        scores = [0, 0]
        if w is not None:
            scores[w] = 1
        return {"winner": w, "scores": scores,
                "reason": ("seat %d %s" % (w, "avoided" if self.misere else "took") + " the last stone")
                if w is not None else "in progress"}

    def frame(self):
        return {"start": list(self.start), "heaps": list(self.heaps), "current": self.current,
                "misere": self.misere, "history": [list(h) for h in self.history], "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        return rng.choice(self.legal_actions(seat))
