"""One self-contained HTML page that shows how a specialist was built and why each move was chosen.

Usage:
  python scripts/demo_page.py                                  # -> results/demo.html
  python scripts/demo_page.py --match real-taboo --out /tmp/demo.html
  python scripts/demo_page.py --replays DIR --memory DIR --metrics DIR

For the live demo and for the judges (harness design is 25 points, the presentation 30). Everything comes from
files the harness already writes: the replays of the arena, the one-line-per-decision metrics in /memory, and
what prep stored per variant (profile, verify, trust, report). No network, no dependency, one file you can open
offline or print: the page carries its own CSS and no script at all.

Three parts:
  THE HARNESS        the fallback ladder and the prep pipeline in words, with our own numbers next to them
  HOW A SPECIALIST   per variant: the family and where it came from, whether a simulator was generated and
   WAS BUILT         verified, how fast it is, how many positions were solved in advance, and what the engine
                     subsystem installed and how far it trusted it
  WHY THIS MOVE      one match, turn by turn: the level that answered, the action, the reason, the time it
                     took and the chat line that went with it

Owner: luckydevs-1. Standard library only (RULES 23, 28).
"""

import argparse
import html
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import replay_report as RR            # noqa: E402  (same folder, stdlib only)

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font: 15px/1.55 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       background: #0f1115; color: #e8eaed; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #2a2f3a; }
h3 { font-size: 15px; margin: 18px 0 6px; color: #c9cdd6; }
p, li { max-width: 80ch; }
.sub { color: #9aa1ad; margin: 0 0 18px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 8px; }
.card { background: #171a21; border: 1px solid #262b36; border-radius: 10px; padding: 12px 14px; min-width: 150px; }
.card .n { font-size: 22px; font-weight: 600; }
.card .l { color: #9aa1ad; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 4px; font-size: 13.5px; }
th, td { text-align: left; padding: 6px 9px; border-bottom: 1px solid #242935; vertical-align: top; }
th { color: #9aa1ad; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:hover td { background: #161922; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
code, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12.5px; }
.lv3 { color: #7ee787; } .lv2 { color: #79c0ff; } .lv1 { color: #ffa657; }
.ok { color: #7ee787; } .bad { color: #ff7b72; } .muted { color: #9aa1ad; }
.ladder { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.step { background: #171a21; border: 1px solid #262b36; border-left: 3px solid #79c0ff; border-radius: 8px;
        padding: 10px 12px; flex: 1 1 220px; }
.step b { display: block; margin-bottom: 3px; }
footer { margin-top: 36px; color: #9aa1ad; font-size: 12px; border-top: 1px solid #2a2f3a; padding-top: 10px; }
@media print { body { background: #fff; color: #111; } .card, .step { border-color: #ccc; }
  th, td { border-color: #ddd; } h2 { border-color: #ccc; } }
"""


class Html(str):
    """A string that is already HTML (built here, never from the data). Everything else is escaped."""


def esc(value, limit=400):
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return html.escape(text, quote=True)


def card(number, label):
    return '<div class="card"><div class="n">%s</div><div class="l">%s</div></div>' % (esc(number), esc(label))


def span(klass, value):
    return Html('<span class="%s">%s</span>' % (esc(klass, 20), esc(value, 40)))


def table(headers, rows, numeric=()):
    out = ["<table><tr>"]
    for i, h in enumerate(headers):
        out.append('<th class="n">%s</th>' % esc(h) if i in numeric else "<th>%s</th>" % esc(h))
    out.append("</tr>")
    for row in rows:
        out.append("<tr>")
        for i, cell in enumerate(row):
            klass = ' class="n"' if i in numeric else ""
            out.append("<td%s>%s</td>" % (klass, cell if isinstance(cell, Html) else esc(cell)))
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


# ---------------------------------------------------------------- the move-by-move table
def pick_match(replay_dirs, team, wanted=None):
    """(match id, games) of the match to show: the one asked for, else the newest with the most turns."""
    best = None
    import glob
    files = []
    for d in replay_dirs:
        files.extend(sorted(glob.glob(os.path.join(d, "*.json"))) if os.path.isdir(d) else [d])
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not doc.get("games"):
            continue
        name = doc.get("match_id") or os.path.splitext(os.path.basename(path))[0]
        if wanted and name != wanted:
            continue
        turns, timed = 0, 0
        for g in doc["games"]:
            seats = g.get("seats") or []
            if team not in seats:
                continue
            seat = str(seats.index(team))
            for step in g.get("timeline") or ():
                entry = ((step.get("actions") or {}) or {}).get(seat)
                if isinstance(entry, dict) and entry.get("action") is not None:
                    turns += 1
                    timed += 1 if float(entry.get("ms") or 0) > 0 else 0
        if not turns:
            continue
        # a match whose answers were really timed says more in the demo than a fake-model run
        score = (1 if timed else 0, turns, os.path.getmtime(path))
        if best is None or score > best[0]:
            best = (score, name, doc)
    return (best[1], best[2]) if best else (None, None)


def _same(a, b):
    return " ".join(str(a or "").split()).casefold() == " ".join(str(b or "").split()).casefold()


def decisions_by_turn(metric_rows):
    """{(game_id, game_index, turn): the metrics line} for joining a replay with our own reasons."""
    out = {}
    for r in metric_rows:
        if r.get("kind") != "turn":
            continue
        key = (str(r.get("game_id")), str(r.get("game_index")), str(r.get("turn")))
        out[key] = r
    return out


def move_rows(doc, team, by_turn):
    rows = []
    for g in doc.get("games") or []:
        seats = g.get("seats") or []
        if team not in seats:
            continue
        seat = str(seats.index(team))
        # The replay's chat entries carry the STEP they were sent with (and the game), so the line that went
        # with a move can be shown next to it: same decision, same row.
        chat_of = {}
        for entry in doc.get("chat") or []:
            ours = entry.get("from") in ("you", team) or entry.get("team") == team
            if ours and entry.get("step") is not None:
                chat_of[(str(entry.get("game")), str(entry.get("step")))] = entry.get("text")
        for step in g.get("timeline") or []:
            entry = ((step.get("actions") or {}) or {}).get(seat)
            if not isinstance(entry, dict):
                continue
            turn = step.get("step")
            metrics = by_turn.get((str(g.get("game_id")), str(g.get("index")), str(turn))) or {}
            # The metrics line carries the action we sent, so a line that does not match this step is a
            # different turn and its reason would be a lie: drop it rather than show the wrong one.
            if metrics.get("action") is not None and _same(metrics.get("action"), entry.get("action")):
                pass
            elif metrics.get("action") is not None:
                metrics = {}
            level = metrics.get("level")
            level_html = span("lv%s" % level, "L%s" % level) if level in (1, 2, 3) else span("muted", "-")
            ms = entry.get("ms")
            rows.append([
                "%s / %s" % (g.get("game_id"), turn),
                level_html,
                Html("<code>%s</code>" % esc(entry.get("action"), 40)),
                (metrics.get("why") or "")[:120],
                "%.2f" % (float(ms) / 1000.0) if isinstance(ms, (int, float)) else "-",
                (chat_of.get((str(g.get("index")), str(turn))) or "")[:160],
            ])
    return rows


# ---------------------------------------------------------------- the page
def render(rep, match_id, doc, metric_rows, team):
    d = rep["decisions"]
    fams = rep["families"]
    turns = d["turns"] or 1
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
             "<title>luckydevs harness</title><style>%s</style></head><body>" % CSS]
    parts.append("<h1>luckydevs: how the harness plays</h1>")
    parts.append('<p class="sub">One generic bot, one model (Haiku through the gateway), seven game families. '
                 'Every number on this page comes from our own replays and from the one line per decision the '
                 'harness writes to /memory. Generated %s.</p>' % esc(time.strftime("%Y-%m-%d %H:%M")))

    wins = sum(a["win"] for a in fams.values())
    bad = sum(a["lost_badly"] for a in fams.values())
    warn = sum(a["warnings"] for a in fams.values())
    parts.append('<div class="cards">')
    parts.append(card(rep["games"], "games played"))
    parts.append(card("%d%%" % round(100.0 * wins / max(1, rep["games"])), "won"))
    parts.append(card(warn, "warnings"))
    parts.append(card(bad, "lost by timeout, illegal move or crash"))
    parts.append(card("%d%%" % round(100.0 * d["levels"].get("3", 0) / turns), "moves from the engine (L3)"))
    parts.append(card("%.2fs" % RR.mean(d["elapsed"]), "mean answer time"))
    if d.get("games_ended"):
        parts.append(card("$%.4f" % (d["cost"] / max(1, d["games_ended"])), "model spend per game"))
    parts.append("</div>")

    parts.append("<h2>The harness: one decision, four levels</h2>")
    parts.append('<div class="ladder">')
    for name, text in (
        ("L3 engine", "A simulator that prep generated and VERIFIED is searched in a sandbox child "
                      "(alpha-beta, MCTS, equilibrium, entropy), inside a slice of the move clock."),
        ("L3b library", "A generated adapter on an installed open-source library, used only as far as the "
                        "trust level prep measured for it (A exact, B strong, C advisory)."),
        ("L2 model", "ONE Haiku call per turn returns the move and the chat line together, and it may only "
                     "choose among the engine's candidates when there are any."),
        ("L1 safe move", "The first legal action, or an action that matches the format prep extracted. This is "
                         "what the watchdog sends if anything above is late."),
    ):
        parts.append('<div class="step"><b>%s</b>%s</div>' % (esc(name), esc(text, 240)))
    parts.append("</div>")
    rows = []
    for level, label in (("3", "L3 engine"), ("2", "L2 model"), ("1", "L1 safe move")):
        n = d["levels"].get(level, 0)
        rows.append([label, n, "%d%%" % round(100.0 * n / turns)])
    parts.append(table(["Level that answered", "Turns", "Share"], rows, numeric=(1, 2)))
    parts.append('<p class="muted">The watchdog answered %d of %d turns, and %d turn(s) carried a warning '
                 'from the arena. Peak resident memory %.0f MB, at most %d sandbox child(ren) in one turn.</p>'
                 % (d["watchdog"], d["turns"], d["warning_turns"], d["rss_mb"], d["children"]))

    parts.append("<h2>How a specialist was built, variant by variant</h2>")
    parts.append('<p>Prep gets 600 seconds and no knowledge of the game. It classifies the rules, has the '
                 'model write a simulator, VERIFIES that simulator against the sample turn and against itself, '
                 'solves what it can in advance, and only then answers <code>ready</code>.</p>')
    rows = []
    for p in rep["prep"]:
        rows.append([
            "%s / %s" % (p["game_id"], str(p["label"])[:26]),
            p["family"],
            span("ok", "verified") if p["verified"] else span("muted", "no simulator"),
            "%.0f" % p["playouts_per_s"] if p["playouts_per_s"] else "-",
            p["solved_roots"] or "-",
            span("ok", p["trust"]) if p["trust"] in ("A", "B") else (p["trust"] or "-"),
            str(p["library"] or "-")[:20],
        ])
    parts.append(table(["Game / variant", "Family", "Simulator", "Playouts/s", "Solved roots",
                        "Library trust", "Library"], rows, numeric=(3, 4)))
    verified = sum(1 for p in rep["prep"] if p["verified"])
    parts.append('<p class="muted">A simulator was verified for %d of %d prepared variant(s). A variant '
                 'without one is still played: the ladder falls to the model, validated by code.</p>'
                 % (verified, len(rep["prep"])))

    if doc is not None:
        parts.append("<h2>Why this move: %s</h2>" % esc(match_id))
        parts.append('<p>Move by move, from the replay and from our own decision log: which level answered, '
                     'what we played, why, how long it took, and the chat line that came from the same '
                     'decision.</p>')
        parts.append(table(["Game / turn", "Level", "Action", "Why", "Seconds", "Chat line"],
                           move_rows(doc, team, decisions_by_turn(metric_rows)), numeric=(4,)))

    parts.append("<h2>Results by family</h2>")
    rows = []
    for fam in sorted(fams, key=lambda f: (-fams[f]["games"], f)):
        a = fams[fam]
        rows.append([fam, ", ".join(a["game_ids"]), a["games"], "%d / %d / %d" % (a["win"], a["draw"], a["loss"]),
                     "%d%%" % round(a["win_pct"]), a["warnings"],
                     span("bad", a["lost_badly"]) if a["lost_badly"] else span("ok", "0"),
                     "%.2f" % a["p50"], "%.2f" % a["p99"]])
    parts.append(table(["Family", "Games", "N", "W / D / L", "Won", "Warnings", "Lost badly", "p50 s", "p99 s"],
                       rows, numeric=(2, 4, 5, 6, 7, 8)))
    losses = [r for r in rep["rows"] if r["outcome"] == "loss"]
    if losses:
        rows = []
        for kind in RR.LOSS_KINDS:
            group = [r for r in losses if r["loss_kind"] == kind]
            if group:
                rows.append([kind, len(group), ", ".join(sorted(set(r["game_id"] for r in group))),
                             (group[0]["reason"] or (group[0]["errors"] or [""])[0])[:90]])
        parts.append("<h3>Why we lost</h3>")
        parts.append(table(["Kind", "N", "Games", "First example"], rows, numeric=(1,)))

    parts.append("<h2>What this says</h2><ul>")
    for line in rep["slides"]:
        parts.append("<li>%s</li>" % esc(line, 300))
    parts.append("</ul>")
    parts.append("<footer>Generated by scripts/demo_page.py from results/practice/matches (replays), "
                 "&lt;memory&gt;/metrics (one JSON line per decision) and &lt;memory&gt;/&lt;game&gt;/"
                 "&lt;rules hash&gt; (what prep stored). No network, no dependency, standard library only."
                 "</footer></body></html>")
    return "\n".join(parts)


def main(argv=None):
    repo = os.path.dirname(_HERE)
    default_memory = os.environ.get("MEMORY_DIR") or os.path.join(repo, "memory", RR.TEAM)
    ap = argparse.ArgumentParser(description="Build one self-contained HTML page from replays and metrics.")
    ap.add_argument("--replays", action="append", default=[])
    ap.add_argument("--metrics", action="append", default=[])
    ap.add_argument("--memory", action="append", default=[])
    ap.add_argument("--team", default=RR.TEAM)
    ap.add_argument("--match", help="which match to show move by move (default: the one with the most turns)")
    ap.add_argument("--out", default=os.path.join(repo, "results", "demo.html"))
    args = ap.parse_args(argv)
    replays = args.replays or [os.path.join(repo, "results", "practice", "matches")]
    memory_dirs = args.memory or [default_memory]
    metrics = args.metrics or [os.path.join(d, "metrics") for d in memory_dirs]

    rep = RR.build(replays, metrics, memory_dirs, args.team)
    match_id, doc = pick_match(replays, args.team, args.match)
    page = render(rep, match_id, doc, RR.read_metrics(metrics), args.team)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="ascii", errors="xmlcharrefreplace") as f:
        f.write(page)
    print("wrote %s (%.1f KB): %d game(s), %d prepared variant(s), move table from %s" % (
        args.out, len(page) / 1024.0, rep["games"], len(rep["prep"]), match_id or "(no match found)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
