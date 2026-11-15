"""scripts/demo_page.py: one self-contained page, honest joins, nothing injected into it."""

import json
import os
import re
import tempfile
import unittest

from scripts import demo_page as D


def replay(match="m1", game_id="pig", team="luckydevs", steps=3, ms=250, chat=True):
    timeline, chat_rows = [], []
    for step in range(1, steps + 1):
        timeline.append({"step": step, "actions": {"0": {"action": "roll", "ms": ms}}})
        if chat:
            chat_rows.append({"n": step, "game": 1, "game_id": game_id, "step": step, "phase": "play",
                              "kind": "chat", "seat": 0, "team": team, "text": "line %d" % step})
    return {"match_id": match, "costs": {team: 0.01}, "chat": chat_rows,
            "games": [{"game_id": game_id, "game_name": game_id, "category": "think", "index": 1,
                       "variant": {"label": "v"}, "seats": [team, "other"], "warnings": {"0": 0},
                       "study": {"0": {"ms": 400}}, "timeline": timeline,
                       "result": {"winner": team, "scores": [1, 0], "reason": "reached 50"}}]}


class DemoPageTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ld-demo-")
        self.replays = os.path.join(self.dir, "matches")
        self.memory = os.path.join(self.dir, "memory")
        self.metrics = os.path.join(self.memory, "metrics")
        for d in (self.replays, self.metrics):
            os.makedirs(d)
        self.out = os.path.join(self.dir, "demo.html")

    def write(self, doc, name=None):
        with open(os.path.join(self.replays, (name or doc["match_id"]) + ".json"), "w") as f:
            json.dump(doc, f)

    def write_metrics(self, rows):
        with open(os.path.join(self.metrics, "c1.jsonl"), "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def write_prep(self, game_id="pig", label="v", verified=True):
        folder = os.path.join(self.memory, game_id, "h" + label)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "meta.json"), "w") as f:
            json.dump({"label": label, "harness_version": "0.2.0"}, f)
        with open(os.path.join(folder, "profile.json"), "w") as f:
            json.dump({"family": "think_ahead"}, f)
        with open(os.path.join(folder, "verify.json"), "w") as f:
            json.dump({"verified": verified, "winner": {"playouts_per_s": 4200.0}}, f)

    def build(self, argv=()):
        return D.main(["--replays", self.replays, "--metrics", self.metrics, "--memory", self.memory,
                       "--out", self.out] + list(argv))

    def page(self):
        with open(self.out, encoding="ascii") as f:
            return f.read()

    def test_the_page_is_one_offline_ascii_file_with_no_script(self):
        self.write(replay())
        self.write_prep()
        self.write_metrics([{"kind": "turn", "game_id": "pig", "game_index": 1, "turn": 1, "level": 3,
                             "why": "engine best", "action": "roll", "elapsed": 0.3, "container": "c1"}])
        self.assertEqual(self.build(), 0)
        page = self.page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertTrue(page.isascii())
        self.assertNotIn("<script", page.lower())
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        for section in ("The harness", "How a specialist was built", "Why this move", "Results by family"):
            self.assertIn(section, page)

    def test_the_move_table_joins_the_reason_only_when_the_action_matches(self):
        self.write(replay(steps=2))
        self.write_metrics([
            {"kind": "turn", "game_id": "pig", "game_index": 1, "turn": 1, "level": 3, "why": "engine best",
             "action": "roll", "container": "c1"},
            # the same turn number but another action: a different decision, its reason must not be shown
            {"kind": "turn", "game_id": "pig", "game_index": 1, "turn": 2, "level": 2, "why": "model said hold",
             "action": "hold", "container": "c1"},
        ])
        self.build()
        page = self.page()
        self.assertIn("engine best", page)
        self.assertNotIn("model said hold", page)

    def test_a_metrics_line_without_an_action_is_still_used(self):
        """Lines written before the action was recorded still carry the level and the reason."""
        self.write(replay(steps=1))
        self.write_metrics([{"kind": "turn", "game_id": "pig", "game_index": 1, "turn": 1, "level": 1,
                             "why": "safe move", "container": "c1"}])
        self.build()
        self.assertIn("safe move", self.page())

    def test_the_chat_line_of_that_very_step_is_shown(self):
        self.write(replay(steps=2))
        self.build()
        page = self.page()
        self.assertIn("line 1", page)
        self.assertIn("line 2", page)

    def test_a_match_with_real_timings_is_preferred(self):
        self.write(replay(match="fake-run", steps=9, ms=0))
        self.write(replay(match="real-run", steps=3, ms=300))
        self.build()
        self.assertIn("real-run", self.page())
        self.build(["--match", "fake-run"])
        self.assertIn("fake-run", self.page())

    def test_html_in_the_data_cannot_break_the_page(self):
        doc = replay(steps=1)
        doc["chat"][0]["text"] = "<script>alert(1)</script> and \"quotes\" & <b>bold</b>"
        doc["games"][0]["timeline"][0]["actions"]["0"]["action"] = "<img src=x onerror=1>"
        self.write(doc)
        self.write_metrics([{"kind": "turn", "game_id": "pig", "game_index": 1, "turn": 1, "level": 2,
                             "why": "</td></tr><h1>injected", "action": "<img src=x onerror=1>",
                             "container": "c1"}])
        self.build()
        page = self.page()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<h1>injected", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_no_data_at_all_is_still_a_page(self):
        self.assertEqual(self.build(), 0)
        self.assertIn("games played", self.page())

    def test_prep_rows_show_what_was_verified(self):
        self.write(replay())
        self.write_prep(verified=True)
        self.write_prep(label="w", verified=False)
        self.build()
        page = self.page()
        self.assertIn("verified", page)
        self.assertIn("no simulator", page)
        self.assertIn("4200", page)
        self.assertIn("1 of 2 prepared variant(s)", page)


if __name__ == "__main__":
    unittest.main()
