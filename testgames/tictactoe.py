"""Test game (Think ahead family): tic-tac-toe. Owner: luckydevs-1.

Loaded by arena_local. Not part of the bot image.
"""

RULES = """Tic-tac-toe on a 3x3 board.
Seat 0 plays X and moves first, seat 1 plays O. Players alternate turns.
Cells are named by a column letter (a, b, c, left to right) followed by a row digit (1, 2, 3, top to bottom), for example b2 is the center.
Action format: on your turn send the name of one empty cell, exactly like "b2".
Three of your marks in a row, column or diagonal wins. A full board without a line is a draw.
Chat is free; it is part of the game."""

LINES = [("a1", "b1", "c1"), ("a2", "b2", "c2"), ("a3", "b3", "c3"),
         ("a1", "a2", "a3"), ("b1", "b2", "b3"), ("c1", "c2", "c3"),
         ("a1", "b2", "c3"), ("c1", "b2", "a3")]
CELLS = [c + r for r in "123" for c in "abc"]


class Game:
    game_id = "tictactoe"
    game_name = "Tic-tac-toe"
    family = "think_ahead"
    simultaneous = False
    variants = [{"label": "standard", "rules": RULES, "settings": {"size": 3}}]

    def new(self, variant, rng):
        return {"board": {c: "." for c in CELLS}, "to_move": 0, "history": []}

    def to_move(self, s):
        return [s["to_move"]]

    def _winner(self, s):
        b = s["board"]
        for line in LINES:
            v = {b[c] for c in line}
            if len(v) == 1 and "." not in v:
                return 0 if b[line[0]] == "X" else 1
        return None

    def over(self, s):
        return self._winner(s) is not None or all(v != "." for v in s["board"].values())

    def scores(self, s):
        w = self._winner(s)
        return [1, 0] if w == 0 else [0, 1] if w == 1 else [0, 0]

    def observation(self, s, seat):
        b = s["board"]
        rows = ["   a b c"] + ["%s  %s" % (r, " ".join(b[c + r] for c in "abc")) for r in "123"]
        return "You play %s. Moves so far: %s\n%s" % (
            "X" if seat == 0 else "O", " ".join(s["history"]) or "none", "\n".join(rows))

    def legal(self, s, seat):
        return [c for c in CELLS if s["board"][c] == "."]

    def check(self, s, seat, action):
        if action not in self.legal(s, seat):
            return False, "not an empty cell name"
        return True, ""

    def apply(self, s, actions):
        seat = s["to_move"]
        a = actions[seat]
        s["board"][a] = "X" if seat == 0 else "O"
        s["history"].append(a)
        s["to_move"] = 1 - seat

    def random_action(self, s, seat, rng):
        return rng.choice(self.legal(s, seat))


GAME = Game()
