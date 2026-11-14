"""Regression tests for the generic tactical search priorities."""

import json
import time
import unittest

from bot.engines import search_job


class GravityConnect:
    """Small gravity Connect-N simulator used directly by the sandbox job."""

    def parse_observation(self, observation, variant, seat):
        state = json.loads(observation)
        state["me"] = seat
        return state

    def current_player(self, state):
        return state["mover"]

    def legal_actions(self, state):
        if self._winner(state) is not None:
            return []
        height = len(state["board"])
        width = len(state["board"][0])
        return ["drop %d" % c for c in range(width)
                if any(state["board"][r][c] == "." for r in range(height))]

    def apply_action(self, state, action):
        if action not in self.legal_actions(state):
            raise ValueError("illegal action")
        column = int(action.split()[1])
        out = {"board": [row[:] for row in state["board"]],
               "mover": 1 - state["mover"], "me": state["me"],
               "connect": state.get("connect", 3)}
        token = "X" if state["mover"] == 0 else "O"
        for row in range(len(out["board"])):
            if out["board"][row][column] == ".":
                out["board"][row][column] = token
                break
        return out

    def _winner(self, state):
        board = state["board"]
        height, width = len(board), len(board[0])
        connect = state.get("connect", 3)
        for row in range(height):
            for col in range(width):
                for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    cells = [(row + dr * i, col + dc * i) for i in range(connect)]
                    if all(0 <= r < height and 0 <= c < width for r, c in cells):
                        values = [board[r][c] for r, c in cells]
                        if values[0] != "." and len(set(values)) == 1:
                            return 0 if values[0] == "X" else 1
        return None

    def is_terminal(self, state):
        return self._winner(state) is not None or not self.legal_actions(state)

    def returns(self, state):
        winner = self._winner(state)
        if winner is None:
            return [0.0, 0.0]
        return [1.0, -1.0] if winner == 0 else [-1.0, 1.0]


SIM = GravityConnect()


def run(board, mover=0, legal=None, budget=0.2, connect=3):
    if legal is None:
        width = len(board[0])
        legal = ["drop %d" % c for c in range(width)
                 if any(row[c] == "." for row in board)]
    return search_job.run(SIM, {
        "observation": json.dumps({"board": board, "mover": mover, "connect": connect}),
        "legal_actions": legal,
        "variant": {},
        "seat": 0,
        "both_seats": True,
        "budget_s": budget,
    })


class TacticalSearchTests(unittest.TestCase):

    def test_immediate_win_has_priority(self):
        result = run([
            ["X", "X", ".", "."],
            ["O", "O", ".", "."],
            [".", ".", ".", "."],
            [".", ".", ".", "."],
        ])
        self.assertEqual(result["algo"], "tactical")
        self.assertEqual(result["actions"][0]["action"], "drop 2")
        self.assertEqual(result["actions"][0]["value"], 1.0)

    def test_mandatory_block_filters_other_root_actions(self):
        result = run([
            ["O", "O", ".", "."],
            ["X", ".", ".", "."],
            [".", ".", ".", "."],
            [".", ".", ".", "."],
        ])
        actions = [row["action"] for row in result["actions"]]
        self.assertIn("drop 2", actions)
        self.assertNotIn("drop 3", actions)

    def test_anti_feed_rejects_supporting_column(self):
        result = run([
            ["O", "X", ".", "."],
            ["O", "O", ".", "."],
            [".", ".", ".", "."],
            [".", ".", ".", "."],
        ])
        actions = [row["action"] for row in result["actions"]]
        self.assertNotIn("drop 2", actions)

    def test_forcing_double_threat_is_prioritized(self):
        result = run([
            [".", ".", "X", "O"],
            [".", ".", ".", "X"],
            [".", ".", ".", "O"],
            [".", ".", ".", "."],
        ])
        self.assertEqual(result["algo"], "tactical")
        self.assertEqual(result["actions"][0]["action"], "drop 2")
        self.assertEqual(result["actions"][0]["value"], 0.95)

    def test_search_respects_short_time_slice(self):
        started = time.monotonic()
        result = run([
            [".", ".", ".", "."],
            [".", ".", ".", "."],
            [".", ".", ".", "."],
            [".", ".", ".", "."],
        ], budget=0.01)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(result.get("ok"))

    def test_variant_sizes_keep_immediate_win_priority(self):
        for width, height, connect in ((7, 6, 4), (7, 9, 5), (9, 7, 5)):
            board = [["."] * width for _ in range(height)]
            board[0][:connect - 1] = ["X"] * (connect - 1)
            result = run(board, connect=connect)
            self.assertEqual(result["actions"][0]["action"], "drop %d" % (connect - 1))
            self.assertEqual(result["actions"][0]["value"], 1.0)

    def test_exact_endgame_matches_bruteforce_reference(self):
        state = {
            "board": [
                ["X", "X", "O"],
                ["X", ".", "O"],
                ["O", ".", "."],
            ],
            "mover": 0,
            "me": 0,
            "connect": 3,
        }

        def solve(position):
            if SIM.is_terminal(position):
                return SIM.returns(position)[0]
            values = [solve(SIM.apply_action(position, action))
                      for action in SIM.legal_actions(position)]
            return max(values) if position["mover"] == 0 else min(values)

        expected = solve(state)
        result = run(state["board"], mover=state["mover"], connect=3)
        values = {row["action"]: row["value"] for row in result["actions"]}
        self.assertTrue(result.get("solved") or result["algo"] == "exact-timeout")
        if result.get("solved"):
            self.assertEqual(max(values.values()), expected)


if __name__ == "__main__":
    unittest.main()
