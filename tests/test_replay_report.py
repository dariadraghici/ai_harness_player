"""scripts/replay_report.py: loss classification, per-family table, decisions, prep rows, slide lines."""

import json
import os
import tempfile
import unittest

from scripts import replay_report as R


def game(game_id="g1", category="think", outcome="win", reason="", errors=None, warnings=0, ms=1000,
         team="luckydevs", seat=0):
    winner = {"win": team, "loss": "other", "draw": None}[outcome]
    entry = {"action": "a", "ms": ms}
    if errors:
        entry = {"error": errors[0]}
    return {"game_id": game_id, "game_name": game_id, "category": category, "index": 1,
            "variant": {"label": "v"}, "seats": [team, "other"] if seat == 0 else ["other", team],
            "warnings": {str(seat): warnings},
            "study": {str(seat): {"ms": 500}},
            "result": {"winner": winner, "scores": [1, 0], "reason": reason},
            "timeline": [{"step": 1, "actions": {str(seat): entry}}]}


class LossKindTests(unittest.TestCase):

    def test_every_kind_is_recognised(self):
        self.assertEqual(R.classify_loss("no answer within 20 s", []), "timeout")
        self.assertEqual(R.classify_loss("", ["timeout after 20s"]), "timeout")
        self.assertEqual(R.classify_loss("second illegal action", []), "illegal")
        self.assertEqual(R.classify_loss("", ["invalid JSON"]), "illegal")
        self.assertEqual(R.classify_loss("bot crashed", []), "crash")
        self.assertEqual(R.classify_loss("points 3-6 after 9 rounds", []), "outplayed")
        self.assertIn(R.classify_loss("", []), R.LOSS_KINDS)


class ReportTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ld-report-")
        self.replays = os.path.join(self.dir, "matches")
        self.memory = os.path.join(self.dir, "memory")
        self.metrics = os.path.join(self.memory, "metrics")
        for d in (self.replays, self.metrics):
            os.makedirs(d)

    def write_replay(self, name, games, costs=None):
        doc = {"match_id": name, "games": games, "costs": costs or {"luckydevs": 0.01}}
        with open(os.path.join(self.replays, name + ".json"), "w") as f:
            json.dump(doc, f)

    def write_metrics(self, rows):
        with open(os.path.join(self.metrics, "c1.jsonl"), "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def write_prep(self, game_id, label, **kw):
        folder = os.path.join(self.memory, game_id, "hash" + label)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "meta.json"), "w") as f:
            json.dump({"label": label, "harness_version": "0.2.0", "verified": kw.get("verified", False)}, f)
        with open(os.path.join(folder, "profile.json"), "w") as f:
            json.dump({"family": kw.get("family", "think_ahead")}, f)
        if "verified" in kw:
            with open(os.path.join(folder, "verify.json"), "w") as f:
                json.dump({"verified": kw["verified"], "candidates": [{}, {}],
                           "winner": {"playouts_per_s": 1234.0}}, f)
        if kw.get("trust"):
            with open(os.path.join(folder, "trust.json"), "w") as f:
                json.dump({"level": kw["trust"], "library": "nashpy", "uses_library": True}, f)

    def build(self):
        return R.build([self.replays], [self.metrics], [self.memory])

    def test_families_wins_warnings_and_bad_losses(self):
        self.write_replay("m1", [game("ttt", "think", "win"),
                                 game("ttt", "think", "loss", reason="no answer within 20 s", warnings=1),
                                 game("poker", "bluff", "draw"),
                                 game("quiz", "truth", "loss", reason="points 1-3")])
        rep = self.build()
        self.assertEqual(rep["games"], 4)
        think = rep["families"]["think_ahead"]
        self.assertEqual((think["games"], think["win"], think["loss"]), (2, 1, 1))
        self.assertEqual(think["losses"]["timeout"], 1)
        self.assertEqual(think["lost_badly"], 1)
        self.assertEqual(think["warnings"], 1)
        self.assertEqual(rep["families"]["truth"]["losses"]["outplayed"], 1)
        self.assertEqual(rep["families"]["truth"]["lost_badly"], 0)
        self.assertEqual(rep["families"]["bluff"]["draw"], 1)
        text = R.format_report(rep)
        self.assertIn("RESULTS BY FAMILY", text)
        self.assertIn("WHY WE LOST", text)
        self.assertIn("timeout", text)
        self.assertTrue(text.isascii())

    def test_a_game_the_team_did_not_play_is_ignored(self):
        self.write_replay("m2", [{"game_id": "x", "category": "think", "seats": ["a", "b"],
                                  "result": {"winner": "a"}, "timeline": []}])
        self.assertEqual(self.build()["games"], 0)

    def test_the_other_seat_is_read_correctly(self):
        self.write_replay("m3", [game("ttt", "think", "win", seat=1)])
        rep = self.build()
        self.assertEqual(rep["families"]["think_ahead"]["win"], 1)

    def test_decisions_levels_times_and_cost(self):
        self.write_metrics([
            {"kind": "turn", "level": 3, "elapsed": 1.0, "l3_s": 0.4, "l2_s": 0.5, "watchdog": False,
             "tokens": {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 10}, "rss_mb": 40,
             "children": 2, "container": "c1", "llm": {"cost": 0.01}},
            {"kind": "turn", "level": 2, "elapsed": 3.0, "watchdog": True, "warning": True,
             "tokens": {"prompt_tokens": 50, "completion_tokens": 10}, "container": "c1",
             "library": "nashpy", "library_level": "B", "llm": {"cost": 0.02}},
            {"kind": "turn", "level": 1, "elapsed": 0.1, "container": "c1"},
            {"kind": "end", "result": "win", "container": "c1", "llm": {"cost": 0.03}},
            {"kind": "shutdown", "container": "c1", "llm": {"k": {"cost": 0.05}}},
        ])
        d = self.build()["decisions"]
        self.assertEqual(d["turns"], 3)
        self.assertEqual(d["levels"], {"3": 1, "2": 1, "1": 1})
        self.assertEqual((d["watchdog"], d["warning_turns"]), (1, 1))
        self.assertEqual(d["prompt_tokens"], 150)
        self.assertEqual(d["cached_tokens"], 10)
        self.assertEqual(d["cost"], 0.05)              # the highest figure of the container, not the sum
        self.assertEqual(d["games_ended"], 1)
        self.assertEqual(d["results"], {"win": 1})
        self.assertEqual(d["library"], {"nashpy/B": 1})
        self.assertEqual(d["children"], 2)

    def test_prep_rows_and_pass_rate(self):
        self.write_prep("ttt", "3x3", verified=True, family="think_ahead", trust="A")
        self.write_prep("ttt", "5x5", verified=False, family="think_ahead")
        rep = self.build()
        self.assertEqual(len(rep["prep"]), 2)
        self.assertEqual(sum(1 for p in rep["prep"] if p["verified"]), 1)
        row = next(p for p in rep["prep"] if p["label"] == "3x3")
        self.assertEqual((row["trust"], row["library"], row["uses_library"]), ("A", "nashpy", True))
        self.assertIn("verification pass rate: 1 of 2", R.format_report(rep))

    def test_slide_lines_and_markdown(self):
        self.write_replay("m4", [game("ttt", "think", "win"), game("rps", "read", "loss", reason="points"),
                                 game("rps", "read", "loss", reason="points")])
        self.write_metrics([{"kind": "turn", "level": 3, "elapsed": 1.0, "container": "c1",
                             "llm": {"cost": 0.02}}, {"kind": "end", "result": "win", "container": "c1"}])
        rep = self.build()
        joined = " ".join(rep["slides"])
        self.assertIn("3 game(s) played", joined)
        self.assertIn("weakest family", joined)
        self.assertIn("read_opponent", joined)
        md = R.markdown(rep)
        self.assertIn("| Family |", md)
        self.assertTrue(md.isascii())

    def test_empty_input_is_an_empty_report_not_a_crash(self):
        rep = self.build()
        self.assertEqual(rep["games"], 0)
        self.assertIn("RESULTS BY FAMILY", R.format_report(rep))
        self.assertEqual(R.main(["--replays", self.replays, "--metrics", self.metrics,
                                 "--memory", self.memory]), 0)

    def test_broken_files_are_skipped(self):
        with open(os.path.join(self.replays, "bad.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.metrics, "bad.jsonl"), "w") as f:
            f.write("nonsense\n{}\n")
        self.write_replay("ok", [game()])
        self.assertEqual(self.build()["games"], 1)

    def test_notes_file_is_written(self):
        self.write_replay("m5", [game()])
        out = os.path.join(self.dir, "notes.md")
        self.assertEqual(R.main(["--replays", self.replays, "--metrics", self.metrics, "--memory", self.memory,
                                 "--notes", out, "--json"]), 0)
        with open(out) as f:
            self.assertIn("Numbers from the replays", f.read())


if __name__ == "__main__":
    unittest.main()
