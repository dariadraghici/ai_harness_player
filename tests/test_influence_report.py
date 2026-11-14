"""scripts/influence_report.py: attempts, hit rate, control baseline, lift."""

import json
import os
import tempfile
import unittest

from scripts import influence_report as R


def ev(kind, ident, **kw):
    d = {"ev": kind, "kind": kind, "id": ident}
    d.update(kw)
    return d


class ReportTests(unittest.TestCase):

    def events(self):
        out = []
        for i in range(10):                      # invite_blunder: 6 of 10 hit
            out.append(ev("influence", "a%d" % i, family="think_ahead", tactic="invite_blunder", control=False,
                          opp_class="reactive"))
            out.append(ev("influence_result", "a%d" % i, scored=True, hit=i < 6, their_quality_drop=0.2 if i < 6 else 0.0))
        for i in range(5):                       # controls of the same family: 1 of 5 hit
            out.append(ev("influence", "c%d" % i, family="think_ahead", tactic="quiet", control=True,
                          shadow_tactic="invite_blunder"))
            out.append(ev("influence_result", "c%d" % i, scored=True, hit=i == 0, control=True))
        out.append(ev("influence", "q1", family="bluff", tactic="quiet", control=False))
        out.append(ev("influence", "r1", family="bluff", tactic="range_polarize", control=False, opp_class="reactive"))
        out.append(ev("influence", "r1", family="bluff", tactic="range_polarize", resend=True))     # a re-send
        out.append(ev("influence_result", "r1", scored=False, hit=None, reason="warning"))
        return out

    def test_rates_baseline_and_lift(self):
        rep = R.build_report(self.events())
        by = {(r["family"], r["tactic"]): r for r in rep["rows"]}
        t = by[("think_ahead", "invite_blunder")]
        self.assertEqual((t["attempts"], t["scored"], t["hits"]), (10, 10, 6))
        self.assertAlmostEqual(t["hit_rate"], 0.6)
        self.assertAlmostEqual(t["control_rate"], 0.2)
        self.assertAlmostEqual(t["lift"], 0.4)
        self.assertAlmostEqual(t["avg_quality_drop"], 0.12)
        b = by[("bluff", "range_polarize")]
        self.assertEqual((b["attempts"], b["scored"]), (1, 0))             # the re-send is not a second attempt
        self.assertIsNone(b["hit_rate"])
        self.assertEqual(rep["totals"]["plain_quiet"], 1)
        self.assertEqual(rep["controls"]["think_ahead"]["controls"], 5)

    def test_table_and_files(self):
        d = tempfile.mkdtemp(prefix="ld-rep-")
        with open(os.path.join(d, "x.jsonl"), "w") as f:
            for e in self.events():
                f.write(json.dumps(e) + "\n")
            f.write("not json\n{\"ev\": \"other\"}\n")
        events = R.read_events([d])
        self.assertEqual(len(events), len(self.events()))
        table = R.format_table(R.build_report(events))
        for text in ("invite_blunder", "think_ahead", "60%", "20% (n=5)", "+40 pts", "Control rate"):
            self.assertIn(text, table)
        self.assertEqual(R.main(["--path", d]), 0)
        self.assertEqual(R.main(["--path", os.path.join(d, "none")]), 0)       # no events: an empty table, not a crash

    def test_verdicts_and_the_markdown_for_the_presentation_notes(self):
        rep = R.build_report(self.events())
        by = {(r["family"], r["tactic"]): r for r in rep["rows"]}
        self.assertEqual(by[("think_ahead", "invite_blunder")]["verdict"], "keep")        # +40 pts over 5 controls
        self.assertTrue(by[("bluff", "range_polarize")]["verdict"].startswith("more data"))
        loser = [ev("influence", "z%d" % i, family="bluff", tactic="strength_claim", control=False) for i in range(6)]
        loser += [ev("influence_result", "z%d" % i, scored=True, hit=False) for i in range(6)]
        loser += [ev("influence", "k%d" % i, family="bluff", tactic="quiet", control=True, shadow_tactic="strength_claim")
                  for i in range(6)]
        loser += [ev("influence_result", "k%d" % i, scored=True, hit=i < 3, control=True) for i in range(6)]
        rep2 = R.build_report(loser)
        self.assertEqual(rep2["rows"][0]["verdict"], "drop")
        md = R.format_markdown(R.build_report(self.events() + loser))
        self.assertIn("| think_ahead | invite_blunder |", md)
        self.assertIn("Kept (beat the control of their family): think_ahead/invite_blunder", md)
        self.assertIn("Dropped (did not beat the control): bluff/strength_claim", md)
        self.assertTrue(all(ord(c) < 128 for c in md))
        d = tempfile.mkdtemp(prefix="ld-rep-")
        notes = os.path.join(d, "notes.md")
        with open(os.path.join(d, "x.jsonl"), "w") as f:
            for e in self.events():
                f.write(json.dumps(e) + "\n")
        self.assertEqual(R.main(["--path", d, "--notes", notes]), 0)
        self.assertIn("Verdict", open(notes).read())

    def test_empty_input(self):
        self.assertIn("no influence attempts", R.format_table(R.build_report([])))


if __name__ == "__main__":
    unittest.main()
