"""The parallel parts (root split) on top of the tactical / bitboard / exact-endgame search of search_job.py:
every part runs the same tactical analysis, an exact endgame is split by root move, and the merge gives back the
sequential answer."""

import json
import unittest

from bot.engines import search, search_job
from tests.test_tactical_search import SIM


def run_parts(board, parts, mover=0, connect=3, budget=3.0):
    legal = ["drop %d" % c for c in range(len(board[0])) if any(row[c] == "." for row in board)]
    out = []
    for part in range(parts):
        payload = {"observation": json.dumps({"board": board, "mover": mover, "connect": connect}),
                   "legal_actions": legal, "variant": {}, "seat": 0, "both_seats": True, "budget_s": budget}
        if parts > 1:
            payload.update(part=part, parts=parts)
        out.append(search_job.run(SIM, payload))
    return out


def values(result):
    return {a["action"]: round(a["value"], 9) for a in result["actions"]}


class MergedPartsTests(unittest.TestCase):

    def merged(self, board, parts, **kw):
        results = [r for r in run_parts(board, parts, **kw) if r.get("ok")]
        return search.merge_results(results), results

    def test_tactical_decisions_are_identical_in_every_part_and_merge_to_one(self):
        board = [["X", "X", ".", "."], ["O", "O", ".", "."], [".", ".", ".", "."], [".", ".", ".", "."]]
        one = run_parts(board, 1)[0]
        merged, results = self.merged(board, 3)
        self.assertEqual({r["algo"] for r in results}, {"tactical"})
        self.assertEqual(values(merged), values(one))
        self.assertEqual(merged["actions"][0]["action"], "drop 2")

    def test_exact_endgame_split_over_parts_gives_the_sequential_values(self):
        board = [["X", "X", "O"], ["X", ".", "O"], ["O", ".", "."]]
        one = run_parts(board, 1)[0]
        for parts in (2, 3):
            merged, results = self.merged(board, parts)
            self.assertTrue(merged.get("solved"), parts)
            self.assertEqual(values(merged), values(one), parts)
            self.assertEqual(merged["branching"], one["branching"])

    def test_an_empty_board_is_solved_exactly_by_two_parts_with_the_same_values(self):
        board = [["."] * 4 for _ in range(3)]
        one = run_parts(board, 1, budget=6.0)[0]
        merged, _ = self.merged(board, 2, budget=6.0)
        if one.get("solved"):
            self.assertEqual(values(merged), values(one))
        self.assertEqual(max(values(merged).values()), max(values(one).values()))

    def test_more_parts_than_moves_leaves_the_extra_parts_empty_and_the_merge_intact(self):
        board = [["X", "X", "O"], ["X", ".", "O"], ["O", ".", "."]]            # two legal moves
        one = run_parts(board, 1)[0]
        merged, results = self.merged(board, 4)
        self.assertTrue(any(r["algo"] == "none" for r in results) or len(results) == 4)
        self.assertEqual(values(merged), values(one))

    def test_part_zero_of_one_is_the_old_search(self):
        board = [["."] * 4 for _ in range(4)]
        a = run_parts(board, 1, budget=0.3)[0]
        self.assertTrue(a.get("ok"))
        self.assertEqual(search_job._part({"part": 0, "parts": 1}), (0, 1))


if __name__ == "__main__":
    unittest.main()
