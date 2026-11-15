"""From replays and metrics to the table we fix things by, and to the numbers for the slides.

Usage:
  python scripts/replay_report.py                          # results/practice/matches + the local memory
  python scripts/replay_report.py --replays DIR --metrics DIR --memory DIR
  python scripts/replay_report.py --json                   # the same numbers as JSON
  python scripts/replay_report.py --notes NOTES.md         # a markdown version for the presentation

Four tables:

  RESULTS BY FAMILY   every game the team played, grouped by the arena's own category (bluff, deals, read,
                      guess, think, truth, guard), with wins, warnings, timeouts and answer times. This is the
                      table to read after the round robin: a family that loses is a generic fix, not one game.
  WHY WE LOST         every loss classified from the replay: timeout, illegal answer, crash, or simply
                      outplayed. The first three are robustness points (1.25 each, kit note K11) and must be
                      zero; the last one is strategy.
  DECISIONS           from <memory>/metrics/*.jsonl: which ladder level answered, how long it took, what it
                      cost, how often the watchdog had to step in.
  PREP                from <memory>/<game>/<hash>/: whether prep verified a simulator, what it measured, and
                      what the engine subsystem (bot/libs/) decided to trust.

Owner: luckydevs-1. Standard library only (RULES 23, 28). Reads, never writes (except --notes).
"""

import argparse
import glob
import json
import os
import sys

TEAM = os.environ.get("TEAM_ID", "luckydevs")
FAMILY_OF = {"bluff": "bluff", "deals": "deals", "read": "read_opponent", "guess": "guessing",
             "think": "think_ahead", "truth": "truth", "guard": "guardrails", "practice": "practice"}
LOSS_KINDS = ("timeout", "illegal", "crash", "outplayed")


def percentile(values, pct):
    rows = sorted(float(v) for v in values or ())
    if not rows:
        return 0.0
    i = max(0, min(len(rows) - 1, int(round((pct / 100.0) * (len(rows) - 1)))))
    return rows[i]


def mean(values):
    rows = [float(v) for v in values or ()]
    return (sum(rows) / len(rows)) if rows else 0.0


# ---------------------------------------------------------------- replays
def classify_loss(reason, errors):
    """One of LOSS_KINDS from the result reason and the per-seat error strings of the replay."""
    text = " ".join([str(reason or "")] + [str(e or "") for e in errors]).lower()
    if "timeout" in text or "no answer" in text or "too slow" in text:
        return "timeout"
    if ("illegal" in text or "invalid" in text or "bad answer" in text or "malformed" in text
            or "warning" in text or "missing action" in text or "empty action" in text):
        return "illegal"
    if "crash" in text or "exit" in text or "forfeit" in text or "died" in text or "absent" in text:
        return "crash"
    return "outplayed"


def read_replays(paths, team=TEAM):
    """One row per game our team played."""
    rows = []
    files = []
    for p in paths:
        files.extend(sorted(glob.glob(os.path.join(p, "*.json"))) if os.path.isdir(p) else [p])
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or not doc.get("games"):
            continue
        costs = doc.get("costs") if isinstance(doc.get("costs"), dict) else {}
        for g in doc.get("games") or []:
            seats = g.get("seats") or []
            if team not in seats:
                continue
            seat = seats.index(team)
            key = str(seat)
            result = g.get("result") or {}
            winner = result.get("winner")
            times, warnings, errors = [], 0, []
            study = (g.get("study") or {}).get(key) or {}
            if isinstance(study, dict):
                if study.get("error"):
                    errors.append("study: %s" % study["error"])
                elif study.get("ms") is not None:
                    times.append(float(study["ms"]) / 1000.0)
            warnings += int((g.get("warnings") or {}).get(key) or 0)
            for step in g.get("timeline") or []:
                entry = ((step.get("actions") or {}) or {}).get(key)
                if not isinstance(entry, dict):
                    continue
                if entry.get("error"):
                    errors.append(str(entry["error"]))
                elif entry.get("ms") is not None:
                    times.append(float(entry["ms"]) / 1000.0)
            outcome = "draw" if winner is None else ("win" if winner == team else "loss")
            rows.append({
                "match": doc.get("match_id") or os.path.basename(path), "game_id": g.get("game_id") or "?",
                "game_name": g.get("game_name") or "", "category": g.get("category") or "practice",
                "variant": (g.get("variant") or {}).get("label") or "", "seat": seat, "outcome": outcome,
                "reason": str(result.get("reason") or ""), "errors": errors, "warnings": warnings,
                "answer_times": times, "scores": result.get("scores") or [],
                "cost": float(costs.get(team) or 0.0) if isinstance(costs.get(team), (int, float)) else None,
                "loss_kind": classify_loss(result.get("reason"), errors) if outcome == "loss" else "",
            })
    return rows


def by_family(rows):
    out = {}
    for r in rows:
        fam = FAMILY_OF.get(r["category"], r["category"])
        agg = out.setdefault(fam, {"family": fam, "games": 0, "win": 0, "loss": 0, "draw": 0, "warnings": 0,
                                   "times": [], "losses": {k: 0 for k in LOSS_KINDS}, "game_ids": set()})
        agg["games"] += 1
        agg[r["outcome"]] += 1
        agg["warnings"] += r["warnings"]
        agg["times"].extend(r["answer_times"])
        agg["game_ids"].add(r["game_id"])
        if r["outcome"] == "loss":
            agg["losses"][r["loss_kind"]] += 1
    for agg in out.values():
        agg["game_ids"] = sorted(agg["game_ids"])
        agg["win_pct"] = (100.0 * agg["win"] / agg["games"]) if agg["games"] else 0.0
        agg["p50"] = percentile(agg["times"], 50)
        agg["p99"] = percentile(agg["times"], 99)
        agg["lost_badly"] = agg["losses"]["timeout"] + agg["losses"]["illegal"] + agg["losses"]["crash"]
    return out


# ---------------------------------------------------------------- metrics
def read_metrics(paths):
    """The decision lines of <memory>/metrics/*.jsonl."""
    rows = []
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.jsonl"))))
            files.extend(sorted(glob.glob(os.path.join(p, "metrics", "*.jsonl"))))
        elif os.path.isfile(p):
            files.append(p)
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        except OSError:
            continue
    return rows


def decisions(rows):
    turns = [r for r in rows if r.get("kind") == "turn"]
    out = {"turns": len(turns), "levels": {}, "watchdog": 0, "warning_turns": 0, "cost": 0.0,
           "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "elapsed": [], "l3_s": [],
           "l2_s": [], "rss_mb": 0.0, "children": 0, "library": {}}
    containers = {}
    for r in turns:
        level = r.get("level")
        key = str(level if level in (1, 2, 3) else "?")
        out["levels"][key] = out["levels"].get(key, 0) + 1
        out["watchdog"] += 1 if r.get("watchdog") else 0
        out["warning_turns"] += 1 if r.get("warning") else 0
        if isinstance(r.get("elapsed"), (int, float)):
            out["elapsed"].append(float(r["elapsed"]))
        for field, bucket in (("l3_s", "l3_s"), ("l2_s", "l2_s")):
            if isinstance(r.get(field), (int, float)):
                out[bucket].append(float(r[field]))
        tokens = r.get("tokens") if isinstance(r.get("tokens"), dict) else {}
        out["prompt_tokens"] += int(tokens.get("prompt_tokens") or 0)
        out["completion_tokens"] += int(tokens.get("completion_tokens") or 0)
        out["cached_tokens"] += int(tokens.get("cached_tokens") or 0)
        for field in ("rss_mb", "child_rss_mb"):
            if isinstance(r.get(field), (int, float)):
                out["rss_mb"] = max(out["rss_mb"], float(r[field]))
        for field in ("children", "children_peak"):
            if isinstance(r.get(field), (int, float)):
                out["children"] = max(out["children"], int(r[field]))
        if r.get("library_level"):
            k = "%s/%s" % (r.get("library") or "?", r["library_level"])
            out["library"][k] = out["library"].get(k, 0) + 1
    for r in rows:
        llm = r.get("llm")
        if isinstance(llm, dict) and r.get("container"):
            spend = llm.get("cost")
            if spend is None:
                spend = sum(v.get("cost", 0.0) for v in llm.values() if isinstance(v, dict))
            if isinstance(spend, (int, float)):
                containers[r["container"]] = max(containers.get(r["container"], 0.0), float(spend))
    out["cost"] = round(sum(containers.values()), 6)
    out["containers"] = len(containers)
    ends = [r for r in rows if r.get("kind") == "end"]
    out["games_ended"] = len(ends)
    out["results"] = {}
    for r in ends:
        key = str(r.get("result") or "?")
        out["results"][key] = out["results"].get(key, 0) + 1
    return out


def prep_budget(rows):
    """Where the prep budget went, per stage, from the `prep` metrics lines (bot/prep/pipeline.py LAST_PREP).

    The prep stages are elastic on purpose - mastery and the engine subsystem take whatever the simulators
    leave - so the only honest way to tune the budget is to read what the stages really took in a prep that
    had the arena's clock, not to reason about the fractions.
    """
    preps = [r for r in rows if r.get("kind") == "prep" and isinstance(r.get("stages"), dict)]
    if not preps:
        return None
    stages = ("phase_a", "chains", "solve", "libs", "mastery")
    out = {"preps": len(preps), "stages": {}, "elapsed": [], "games": {}}
    for st in stages:
        vals = [float(r["stages"][st]) for r in preps if isinstance(r["stages"].get(st), (int, float))]
        if vals:
            out["stages"][st] = {"mean": sum(vals) / len(vals), "max": max(vals), "n": len(vals)}
    for r in preps:
        total = r["stages"].get("total")
        if isinstance(total, (int, float)):
            out["elapsed"].append(float(total))
        g = str(r.get("game_id") or "unknown")
        out["games"].setdefault(g, []).append(r["stages"])
    return out


# ---------------------------------------------------------------- prep artifacts
def read_prep(memory_dirs):
    """One row per prepared variant: what prep built and what it measured."""
    rows = []
    for root in memory_dirs:
        if not os.path.isdir(root):
            continue
        for meta_path in sorted(glob.glob(os.path.join(root, "*", "*", "meta.json"))):
            folder = os.path.dirname(meta_path)
            meta = _json(meta_path)
            if not isinstance(meta, dict):
                continue
            verify = _json(os.path.join(folder, "verify.json")) or {}
            trust = _json(os.path.join(folder, "trust.json")) or {}
            report = _json(os.path.join(folder, "report.json")) or {}
            profile = _json(os.path.join(folder, "profile.json")) or {}
            solved = _json(os.path.join(folder, "solved.json")) or {}
            winner = verify.get("winner") if isinstance(verify.get("winner"), dict) else {}
            rows.append({
                "game_id": os.path.basename(os.path.dirname(folder)), "label": meta.get("label") or "",
                "harness": meta.get("harness_version") or "", "family": profile.get("family") or "",
                "verified": bool(verify.get("verified")),
                "candidates": len(verify.get("candidates") or ()) or None,
                "playouts_per_s": winner.get("playouts_per_s"),
                "solved_roots": len(solved.get("roots") or ()) if isinstance(solved, dict) else 0,
                "trust": trust.get("level") or "", "library": trust.get("library") or "",
                "uses_library": trust.get("uses_library"),
                "libs_seconds": report.get("seconds"), "tags": report.get("tags") or [],
            })
    return rows


def _json(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- tables
def table(headers, rows, aligns=None):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    aligns = aligns or ["left"] * len(headers)

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            text = str(cell)
            out.append(text.ljust(widths[i]) if aligns[i] == "left" else text.rjust(widths[i]))
        return "| " + " | ".join(out) + " |"
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    return "\n".join([sep, line(headers), sep.replace("-", "=")] + [line(r) for r in rows] + [sep])


def format_report(rep):
    fams = rep["families"]
    out = ["", "RESULTS BY FAMILY (%d game(s) of team %s)" % (rep["games"], rep["team"])]
    rows = []
    for fam in sorted(fams, key=lambda f: (-fams[f]["games"], f)):
        a = fams[fam]
        rows.append([fam, ",".join(a["game_ids"])[:28], a["games"], "%d/%d/%d" % (a["win"], a["draw"], a["loss"]),
                     "%d%%" % round(a["win_pct"]), a["warnings"], a["lost_badly"],
                     "%.2f" % a["p50"], "%.2f" % a["p99"]])
    out.append(table(["Family", "Games", "N", "W/D/L", "Win", "Warn", "Lost badly", "p50 s", "p99 s"], rows,
                     ["left", "left"] + ["right"] * 7))
    out.append("`Lost badly` = lost by timeout, illegal answer or crash: 1.25 robustness points each (K11).")

    out.append("")
    out.append("WHY WE LOST")
    losses = [r for r in rep["rows"] if r["outcome"] == "loss"]
    if not losses:
        out.append("  no loss in these replays")
    else:
        rows = []
        for kind in LOSS_KINDS:
            group = [r for r in losses if r["loss_kind"] == kind]
            if not group:
                continue
            example = group[0]
            rows.append([kind, len(group), ", ".join(sorted(set(r["game_id"] for r in group)))[:34],
                         (example["reason"] or (example["errors"] or [""])[0])[:60]])
        out.append(table(["Kind", "N", "Games", "First example"], rows, ["left", "right", "left", "left"]))

    d = rep["decisions"]
    out.append("")
    out.append("DECISIONS (%d turn(s) in %d container(s), %d game end(s))" % (
        d["turns"], d.get("containers", 0), d.get("games_ended", 0)))
    if d["turns"]:
        rows = []
        for level in ("3", "2", "1", "?"):
            n = d["levels"].get(level, 0)
            if not n:
                continue
            name = {"3": "L3 engine", "2": "L2 model", "1": "L1 safe move", "?": "unknown"}[level]
            rows.append([name, n, "%d%%" % round(100.0 * n / d["turns"])])
        out.append(table(["Level", "Turns", "Share"], rows, ["left", "right", "right"]))
        out.append("  mean answer %.2f s, p99 %.2f s | engine %.2f s mean, model call %.2f s mean" % (
            mean(d["elapsed"]), percentile(d["elapsed"], 99), mean(d["l3_s"]), mean(d["l2_s"])))
        out.append("  watchdog answered %d turn(s), %d turn(s) carried a warning" % (
            d["watchdog"], d["warning_turns"]))
        out.append("  model spend %.4f USD over %d container(s) | prompt %d tokens (%d cached), completion %d" % (
            d["cost"], d.get("containers", 0), d["prompt_tokens"], d["cached_tokens"], d["completion_tokens"]))
        out.append("  peak RSS %.0f MB, at most %d sandbox child(ren) in a turn" % (d["rss_mb"], d["children"]))
        if d["library"]:
            out.append("  library engine used: " + ", ".join("%s x%d" % (k, v) for k, v in sorted(d["library"].items())))

    out.append("")
    out.append("PREP (%d prepared variant(s))" % len(rep["prep"]))
    if rep["prep"]:
        rows = []
        for p in rep["prep"]:
            rows.append([p["game_id"], p["label"][:22], p["family"][:12], "yes" if p["verified"] else "no",
                         p["playouts_per_s"] and "%.0f" % p["playouts_per_s"] or "-", p["solved_roots"] or "-",
                         p["trust"] or "-", (p["library"] or "-")[:12],
                         "" if p["uses_library"] is None else ("used" if p["uses_library"] else "stdlib")])
        out.append(table(["Game", "Variant", "Family", "Sim", "Playouts/s", "Solved", "Trust", "Library",
                          "Adapter"], rows, ["left", "left", "left", "right", "right", "right", "right",
                                             "left", "left"]))
        verified = sum(1 for p in rep["prep"] if p["verified"])
        out.append("  verification pass rate: %d of %d variant(s) (%d%%)" % (
            verified, len(rep["prep"]), round(100.0 * verified / max(1, len(rep["prep"])))))

    if rep.get("prep_budget"):
        b = rep["prep_budget"]
        out.append("")
        out.append("PREP BUDGET (%d prep(s) with a recorded split)" % b["preps"])
        rows = [[st, "%.1f" % v["mean"], "%.1f" % v["max"], str(v["n"])] for st, v in b["stages"].items()]
        out.append(table(["Stage", "Mean s", "Max s", "Preps"], rows, ["left", "right", "right", "right"]))
        if b["elapsed"]:
            out.append("  prep wall clock: mean %.0f s, max %.0f s of the %d s the contract gives" % (
                sum(b["elapsed"]) / len(b["elapsed"]), max(b["elapsed"]), 600))
        essential = sum(b["stages"].get(st, {}).get("mean", 0.0) for st in ("phase_a", "chains"))
        elastic = sum(b["stages"].get(st, {}).get("mean", 0.0) for st in ("solve", "libs", "mastery"))
        if essential or elastic:
            out.append("  everything we NEED to play (profile, simulator) takes %.0f s; the elastic stages "
                       "(pre-solving, libraries, self-play) take %.0f s" % (essential, elastic))

    out.append("")
    out.append("FOR THE SLIDES")
    for line_text in rep["slides"]:
        out.append("  - " + line_text)
    out.append("")
    return "\n".join(out)


def slide_lines(rep):
    fams, d = rep["families"], rep["decisions"]
    games = rep["games"]
    wins = sum(a["win"] for a in fams.values())
    draws = sum(a["draw"] for a in fams.values())
    bad = sum(a["lost_badly"] for a in fams.values())
    warn = sum(a["warnings"] for a in fams.values())
    out = []
    if games:
        out.append("%d game(s) played: %d won, %d drawn, %d lost; %d warning(s) and %d game(s) lost by "
                   "timeout, illegal answer or crash" % (games, wins, draws, games - wins - draws, warn, bad))
    if d["turns"]:
        engine = d["levels"].get("3", 0)
        out.append("%d turn(s): %d%% answered by the engine (L3), %d%% by the model (L2), %d%% by the safe "
                   "move (L1)" % (d["turns"], round(100.0 * engine / d["turns"]),
                                  round(100.0 * d["levels"].get("2", 0) / d["turns"]),
                                  round(100.0 * d["levels"].get("1", 0) / d["turns"])))
        out.append("answer time: mean %.2f s, p99 %.2f s; the watchdog answered %d turn(s)" % (
            mean(d["elapsed"]), percentile(d["elapsed"], 99), d["watchdog"]))
        if d.get("games_ended"):
            out.append("model spend %.4f USD over %d game(s) = %.4f USD per game" % (
                d["cost"], d["games_ended"], d["cost"] / max(1, d["games_ended"])))
    if rep["prep"]:
        verified = sum(1 for p in rep["prep"] if p["verified"])
        trusted = [p for p in rep["prep"] if p["trust"] in ("A", "B")]
        out.append("prep verified a simulator for %d of %d variant(s); the engine subsystem trusted a library "
                   "adapter in %d" % (verified, len(rep["prep"]), len(trusted)))
    weakest = sorted((a for a in fams.values() if a["games"] >= 2), key=lambda a: a["win_pct"])
    if weakest:
        w = weakest[0]
        # How much of this is luck? A family is a run of games against an opponent we do not control, so the
        # share of wins has a standard error of sqrt(p(1-p)/n) - about 8 points over 40 games. Printing the
        # percentage alone sends somebody to "fix" a result that is a coin flip, which is exactly what the
        # first read of this line did: read_opponent at 45 percent of 40 games is 18 wins, six tenths of one
        # standard error from even, in a symmetric zero-sum game where an unpredictable opponent CANNOT be
        # beaten by more than noise. The number to act on is one that is several standard errors low.
        p = w["win_pct"] / 100.0
        se = (p * (1.0 - p) / w["games"]) ** 0.5 if w["games"] else 0.0
        sigmas = (0.5 - p) / se if se > 0 else 0.0
        out.append("weakest family so far: %s (%d%% of %d game(s), %.1f standard error(s) below even)%s"
                   % (w["family"], round(w["win_pct"]), w["games"], sigmas,
                      " - fix it generically, it is not one game" if sigmas >= 2.0 else
                      " - NOT yet distinguishable from chance at this many games; measure the family headless "
                      "(scripts/engine_arena.py) before changing anything"))
    return out


def build(replay_dirs, metric_dirs, memory_dirs, team=TEAM):
    rows = read_replays(replay_dirs, team)
    metric_rows = read_metrics(metric_dirs)
    rep = {"team": team, "rows": rows, "games": len(rows), "families": by_family(rows),
           "decisions": decisions(metric_rows), "prep": read_prep(memory_dirs),
           "prep_budget": prep_budget(metric_rows)}
    rep["slides"] = slide_lines(rep)
    return rep


def markdown(rep):
    out = ["# Numbers from the replays", "", "## Results by family", "",
           "| Family | Games | N | W/D/L | Win | Warnings | Lost badly | p50 s | p99 s |",
           "|---|---|---|---|---|---|---|---|---|"]
    fams = rep["families"]
    for fam in sorted(fams, key=lambda f: (-fams[f]["games"], f)):
        a = fams[fam]
        out.append("| %s | %s | %d | %d/%d/%d | %d%% | %d | %d | %.2f | %.2f |" % (
            fam, ",".join(a["game_ids"]), a["games"], a["win"], a["draw"], a["loss"], round(a["win_pct"]),
            a["warnings"], a["lost_badly"], a["p50"], a["p99"]))
    out += ["", "## For the slides", ""]
    out += ["- " + s for s in rep["slides"]]
    out.append("")
    return "\n".join(out)


def main(argv=None):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_memory = os.environ.get("MEMORY_DIR") or os.path.join(repo, "memory", TEAM)
    ap = argparse.ArgumentParser(description="Replay and metrics report: loss reasons per family, decisions, "
                                             "prep results and the numbers for the slides.")
    ap.add_argument("--replays", action="append", default=[],
                    help="replay folder or file (default results/practice/matches)")
    ap.add_argument("--metrics", action="append", default=[], help="metrics folder (default <memory>/metrics)")
    ap.add_argument("--memory", action="append", default=[], help="memory folder (default %s)" % default_memory)
    ap.add_argument("--team", default=TEAM)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notes", help="also write a markdown version to this file")
    args = ap.parse_args(argv)
    replays = args.replays or [os.path.join(repo, "results", "practice", "matches")]
    memory_dirs = args.memory or [default_memory]
    metrics = args.metrics or [os.path.join(d, "metrics") for d in memory_dirs]
    rep = build(replays, metrics, memory_dirs, args.team)
    if args.notes:
        with open(args.notes, "w", encoding="ascii", errors="replace") as f:
            f.write(markdown(rep))
    if args.json:
        rep = dict(rep)
        rep["families"] = {k: {kk: vv for kk, vv in v.items() if kk != "times"} for k, v in rep["families"].items()}
        rep["decisions"] = {k: v for k, v in rep["decisions"].items() if k not in ("elapsed", "l3_s", "l2_s")}
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(format_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
