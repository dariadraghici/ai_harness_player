"""Report on the influence layer: per family and tactic, attempts, hit rate, quality drop, control baseline.

Usage:
  python scripts/influence_report.py                      # <MEMORY_DIR or ./memory/luckydevs>/metrics/*.jsonl
  python scripts/influence_report.py --path DIR_OR_FILE   # a metrics folder or one .jsonl file (repeatable)
  python scripts/influence_report.py --json               # the same numbers as JSON
  python scripts/influence_report.py --notes FILE.md      # a markdown table for the presentation notes

Reads the two event types the bot writes (bot/metrics.py record_influence / record_influence_result):
  ev=influence         one attempt, at send time (tactic, control, shadow_tactic, family, opponent class, ...)
  ev=influence_result  its score, one decision (one round when simultaneous) later (hit, their_quality_drop)

A control is a turn that WOULD have used `shadow_tactic` but was sent quiet on purpose. Its hit rate is the baseline:
a tactic is only worth keeping when it beats the controls of its family. `hit` alone can be luck; the honest metric
is `their_quality_drop` (needs a verified simulator, phase 2), shown when present.

Owner: luckydevs-1 area. Standard library only (RULES 23, 28).
"""

import argparse
import glob
import json
import os
import sys


def default_paths():
    root = os.environ.get("MEMORY_DIR")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (root, os.path.join(here, "memory", "luckydevs")):
        if cand and os.path.isdir(os.path.join(cand, "metrics")):
            return [os.path.join(cand, "metrics")]
    return []


def read_events(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.jsonl"))))
        elif os.path.isfile(p):
            files.append(p)
    events = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"influence' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("ev") in ("influence", "influence_result"):
                        events.append(obj)
        except OSError:
            continue
    return events


MIN_SCORED = 5            # attempts that must be scored before the report gives a verdict
MIN_LIFT = 0.05           # hit-rate points over the family control that make a tactic worth keeping


def verdict(row):
    """keep / drop / more data. A tactic is kept only when it beats the control of its family."""
    if row["scored"] < MIN_SCORED:
        return "more data"
    if row["control_scored"] < MIN_SCORED or row["lift"] is None:
        return "more data (no baseline)"
    return "keep" if row["lift"] >= MIN_LIFT else "drop"


def _rate(hits, scored):
    return (hits / float(scored)) if scored else None


def build_report(events):
    """{"rows": [...], "controls": {family: {...}}, "totals": {...}} from the raw events."""
    attempts = {}
    results = {}
    for e in events:
        if e.get("ev") == "influence" and not e.get("resend"):
            attempts[e.get("id")] = e
        elif e.get("ev") == "influence_result" and e.get("scored"):
            results[e.get("id")] = e
    rows, controls = {}, {}
    quiet_plain = 0
    for ident, a in attempts.items():
        fam = a.get("family") or "unknown"
        res = results.get(ident)
        if a.get("control"):
            c = controls.setdefault(fam, {"controls": 0, "scored": 0, "hits": 0})
            c["controls"] += 1
            if res is not None:
                c["scored"] += 1
                c["hits"] += 1 if res.get("hit") else 0
            continue
        tactic = a.get("tactic") or "quiet"
        if tactic == "quiet":
            quiet_plain += 1
            continue
        r = rows.setdefault((fam, tactic), {"family": fam, "tactic": tactic, "attempts": 0, "scored": 0, "hits": 0,
                                            "drops": [], "drop_exact": 0, "classes": {}})
        r["attempts"] += 1
        cls = a.get("opp_class") or "unknown"
        r["classes"][cls] = r["classes"].get(cls, 0) + 1
        if res is not None:
            r["scored"] += 1
            r["hits"] += 1 if res.get("hit") else 0
            if isinstance(res.get("their_quality_drop"), (int, float)):
                r["drops"].append(float(res["their_quality_drop"]))
                r["drop_exact"] += 1 if res.get("drop_exact") else 0
    out = []
    for key in sorted(rows):
        r = rows[key]
        base = controls.get(r["family"], {})
        base_rate = _rate(base.get("hits", 0), base.get("scored", 0))
        rate = _rate(r["hits"], r["scored"])
        out.append({"family": r["family"], "tactic": r["tactic"], "attempts": r["attempts"], "scored": r["scored"],
                    "hits": r["hits"], "hit_rate": rate,
                    "avg_quality_drop": (sum(r["drops"]) / len(r["drops"])) if r["drops"] else None,
                    "control_scored": base.get("scored", 0), "control_rate": base_rate,
                    "lift": (rate - base_rate) if rate is not None and base_rate is not None else None,
                    "drop_exact": r["drop_exact"], "classes": r["classes"]})
        out[-1]["verdict"] = verdict(out[-1])
    return {"rows": out, "controls": controls,
            "totals": {"attempts": len(attempts), "plain_quiet": quiet_plain, "results": len(results)}}


def _pct(v):
    return "-" if v is None else "%.0f%%" % (100.0 * v)


def format_table(report):
    headers = ["Family", "Tactic", "Attempts", "Scored", "Hit rate", "Quality drop", "Control rate", "Lift", "Verdict"]
    rows = []
    for r in report["rows"]:
        rows.append([r["family"], r["tactic"], str(r["attempts"]), str(r["scored"]), _pct(r["hit_rate"]),
                     "-" if r["avg_quality_drop"] is None else "%.2f" % r["avg_quality_drop"],
                     "%s (n=%d)" % (_pct(r["control_rate"]), r["control_scored"]),
                     "-" if r["lift"] is None else "%+.0f pts" % (100.0 * r["lift"]), r["verdict"]])
    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def line(cols):
        return "| " + " | ".join(c.ljust(widths[i]) if i in (0, 1, 8) else c.rjust(widths[i])
                                 for i, c in enumerate(cols)) + " |"
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = ["", sep, line(headers), sep.replace("-", "=")]
    out += [line(r) for r in rows] or ["| (no influence attempts found)"]
    out.append(sep)
    t = report["totals"]
    out.append("Events: %d attempts (%d plain quiet), %d scored results. Controls per family: %s" % (
        t["attempts"], t["plain_quiet"], t["results"],
        ", ".join("%s %d/%d scored" % (f, c["scored"], c["controls"]) for f, c in sorted(report["controls"].items()))
        or "none"))
    out.append("A tactic is worth keeping only when its hit rate beats the control rate of its family.")
    out.append("")
    return "\n".join(out)


def format_markdown(report):
    """The same table as markdown, for the presentation notes (plain ASCII)."""
    lines = ["| Family | Tactic | Attempts | Scored | Hit rate | Quality drop | Control rate | Lift | Verdict |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        lines.append("| %s | %s | %d | %d | %s | %s | %s (n=%d) | %s | %s |" % (
            r["family"], r["tactic"], r["attempts"], r["scored"], _pct(r["hit_rate"]),
            "-" if r["avg_quality_drop"] is None else "%.2f" % r["avg_quality_drop"], _pct(r["control_rate"]),
            r["control_scored"], "-" if r["lift"] is None else "%+.0f pts" % (100.0 * r["lift"]), r["verdict"]))
    keep = [r for r in report["rows"] if r["verdict"] == "keep"]
    drop = [r for r in report["rows"] if r["verdict"] == "drop"]
    lines += ["", "Kept (beat the control of their family): %s." % (", ".join(
        "%s/%s" % (r["family"], r["tactic"]) for r in keep) or "none yet"),
        "Dropped (did not beat the control): %s." % (", ".join(
            "%s/%s" % (r["family"], r["tactic"]) for r in drop) or "none"),
        "A control is a turn that would have used the tactic but was sent quiet on purpose; hit alone can be luck, "
        "the quality drop is how much value the opponent gave up (0 = they played their best reply).", ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Influence layer report (hit rate vs control baseline).")
    ap.add_argument("--path", action="append", default=[], help="metrics folder or .jsonl file (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--notes", help="also write a markdown table to this file (presentation notes)")
    args = ap.parse_args(argv)
    paths = args.path or default_paths()
    if not paths:
        print("no metrics found: pass --path or set MEMORY_DIR", file=sys.stderr)
        return 1
    report = build_report(read_events(paths))
    if args.notes:
        with open(args.notes, "w", encoding="ascii", errors="replace") as f:
            f.write(format_markdown(report))
    print(json.dumps(report, indent=2) if args.json else format_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
