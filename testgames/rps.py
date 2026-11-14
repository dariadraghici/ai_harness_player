"""Test game (Read the opponent family): rock-paper-scissors with a payoff twist. Owner: luckydevs-1.

Simultaneous moves: both players get the turn at the same time.
Loaded by arena_local, not in the image.
"""

RULES = """Rock, paper, scissors with a twist, {rounds} rounds, simultaneous moves.
Each round both players choose at the same time; neither sees the other's choice before both answered.
Action format: exactly one of rock, paper, scissors (lower case).
Rock beats scissors, scissors beats paper, paper beats rock. A tie scores nothing.
Twist: a win with rock scores 2 points; a win with paper or scissors scores 1 point.
Most points after {rounds} rounds wins the game. Chat is free and both players see it."""

BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


class Game:
    game_id = "rps_twist"
    game_name = "RPS twist"
    family = "read_opponent"
    simultaneous = True
    variants = [{"label": "r5", "rules": RULES.format(rounds=5), "settings": {"rounds": 5}}]

    def new(self, variant, rng):
        return {"rounds": variant["settings"]["rounds"], "history": [], "points": [0, 0]}

    def to_move(self, s):
        return [0, 1]

    def over(self, s):
        return len(s["history"]) >= s["rounds"]

    def scores(self, s):
        return list(s["points"])

    def observation(self, s, seat):
        h = "; ".join("round %d: you %s, opponent %s" % (i + 1, r[seat], r[1 - seat])
                      for i, r in enumerate(s["history"])) or "none"
        return "Round %d of %d. Points: you %d, opponent %d. History: %s" % (
            len(s["history"]) + 1, s["rounds"], s["points"][seat], s["points"][1 - seat], h)

    def legal(self, s, seat):
        return ["rock", "paper", "scissors"]

    def check(self, s, seat, action):
        return (action in BEATS), "must be rock, paper or scissors"

    def apply(self, s, actions):
        a0, a1 = actions[0], actions[1]
        if BEATS[a0] == a1:
            s["points"][0] += 2 if a0 == "rock" else 1
        elif BEATS[a1] == a0:
            s["points"][1] += 2 if a1 == "rock" else 1
        s["history"].append([a0, a1])

    def random_action(self, s, seat, rng):
        return rng.choice(["rock", "paper", "scissors"])


GAME = Game()
