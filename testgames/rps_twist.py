"""Test game (Read the opponent family): rock-paper-scissors with a payoff twist.

Owner: luckydevs-1. Our own test game for the organizers' practice arena
(a subclass of arena.core.Game, as participant-kit/arena-lite/README.md asks).
scripts/arena_lite.py adds this folder to the arena's `games` folder; run it
with:  python scripts/practice.py --game rps_twist

Why it exists: the official practice games have no simultaneous-move game.
Both players get `turn` at the same time. Not part of the bot image.
"""

import random

from arena.core import Game

BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
MOVES = ["rock", "paper", "scissors"]


class RpsTwist(Game):
    id = "rps_twist"
    name = "RPS twist"
    category = "read"
    blurb = "Rock beats scissors, but a win with rock pays double: will you dare to play it?"
    howto = "Both players pick rock, paper or scissors at the same time. A win with rock scores 2, any other win 1."
    duplicate_safe = True   # nothing hidden in the deal and no randomness

    @classmethod
    def variants(cls):
        return [{"label": "5 rounds", "rounds": 5}, {"label": "9 rounds", "rounds": 9}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        self.rounds = max(1, int(self.variant.get("rounds", 5)))
        self.history = []            # [action of seat 0, action of seat 1] per round
        self.points = [0, 0]

    def rules(self):
        n = self.rounds
        return ("ROCK, PAPER, SCISSORS WITH A TWIST - %d rounds, simultaneous moves.\n\n"
                "Each round both players choose at the same time; neither sees the other's choice "
                "before both have answered.\n\n"
                "ACTION FORMAT\n    exactly one of: rock | paper | scissors (lower case). Example:  \"rock\"\n\n"
                "SCORING\nRock beats scissors, scissors beats paper, paper beats rock. A tie scores nothing.\n"
                "Twist: a win with rock scores 2 points; a win with paper or scissors scores 1 point.\n"
                "Most points after %d rounds wins the game; equal points is a draw.\n\n"
                "WHAT YOU OBSERVE\nThe round, both scores and every earlier round with both choices.\n"
                "Chat is free and both players see it." % (n, n))

    def to_move(self):
        return [] if self.is_over() else [0, 1]

    def observation(self, seat):
        hist = "\n".join("  Round %d: you %s, opponent %s" % (i + 1, r[seat], r[1 - seat])
                         for i, r in enumerate(self.history)) or "  (none yet)"
        head = "RPS TWIST (%d rounds). You are seat %d." % (self.rounds, seat)
        score = "Points: you %d, opponent %d." % (self.points[seat], self.points[1 - seat])
        if self.is_over():
            return "%s\nThe game is over. %s\nRounds:\n%s" % (head, score, hist)
        return "%s\nRound %d of %d. %s\nEarlier rounds:\n%s\nChoose: rock | paper | scissors" % (
            head, len(self.history) + 1, self.rounds, score, hist)

    def legal_actions(self, seat):
        return [] if self.is_over() else list(MOVES)

    def normalize(self, seat, action):
        a = self.clean(action).strip(" .!\"'`")
        return a if (a in BEATS and not self.is_over()) else None

    def apply(self, actions):
        a0, a1 = actions[0], actions[1]
        if BEATS[a0] == a1:
            self.points[0] += 2 if a0 == "rock" else 1
        elif BEATS[a1] == a0:
            self.points[1] += 2 if a1 == "rock" else 1
        self.history.append([a0, a1])

    def is_over(self):
        return len(self.history) >= self.rounds

    def result(self):
        p0, p1 = self.points
        winner = None if p0 == p1 else (0 if p0 > p1 else 1)
        return {"winner": winner if self.is_over() else None, "scores": [p0, p1],
                "reason": "points %d-%d after %d rounds" % (p0, p1, len(self.history))}

    def frame(self):
        return {"rounds": self.rounds, "history": [list(r) for r in self.history],
                "points": list(self.points), "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        return rng.choice(MOVES)
