"""Batch runner: play N practice matches over all practice games.

  python scripts/batch_runner.py -n 3 --fake
  python scripts/batch_runner.py -n 5 --opponents random,self --fake
  python scripts/batch_runner.py -n 2 --games tic_tac_toe,pig --fake
  python scripts/batch_runner.py -n 1 --prep --fake
  python scripts/batch_runner.py -n 1 --real --output results/batch_report.json

Opponents covered:
  - random (the kit's random bot)
  - self (our bot as sparring opponent, using --opponent self)

Metrics tracked and printed in a clean ASCII table:
  - Wins (won / total matches and win percentage)
  - Warnings (total warnings issued)
  - Timeouts (timeouts during turns, study, or process timeouts)
  - Average and Maximum Answer Time (in seconds)
  - Cost per Match (USD spend per match from bot metrics and logs)

Owner: luckydevs-1. Standard library only (RULES.md 23, 28).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# Ensure scripts folder is in sys.path for importing _common
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import DEV_ENV, REPO, SECRETS, arena_lite_dir, image_ref, read_env_file

US = "luckydevs"
OUT = os.environ.get("ARENA_ROOT") or REPO

# Canonical fallback practice games if discovery finds none
DEFAULT_GAMES = ["tic_tac_toe", "pig", "guess_number", "rps_twist", "nim_deal"]
DEFAULT_OPPONENTS = ["random", "self"]

# Gateway pricing per token (from bot/config.py)
PRICE_INPUT = 0.10 / 1e6
PRICE_OUTPUT = 0.50 / 1e6
PRICE_CACHE_READ = 0.01 / 1e6


def discover_available_games():
    """Discover all practice games from arena-lite and testgames/."""
    found = []
    # 1. From participant-kit/arena-lite/arena/games
    try:
        lite = os.environ.get("BOT_ARENA_LITE") or os.path.join(REPO, "participant-kit", "arena-lite")
        games_dir = os.path.join(lite, "arena", "games")
        if os.path.isdir(games_dir):
            for name in sorted(os.listdir(games_dir)):
                if name.startswith(("_", ".")):
                    continue
                full = os.path.join(games_dir, name)
                if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
                    found.append(name)
                elif name.endswith(".py"):
                    found.append(name[:-3])
    except Exception:
        pass

    # 2. From testgames/
    testgames_dir = os.path.join(REPO, "testgames")
    if os.path.isdir(testgames_dir):
        for name in sorted(os.listdir(testgames_dir)):
            if name.endswith(".py") and not name.startswith(("_", ".")):
                found.append(name[:-3])

    # Ensure all canonical default practice games are included
    for g in DEFAULT_GAMES:
        if g not in found:
            found.append(g)

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for g in found:
        if g not in seen:
            seen.add(g)
            deduped.append(g)

    return deduped


class MatchResult:
    """Outcome and collected metrics for a single practice match."""

    def __init__(self, game, opponent, match_index, match_id):
        self.game = game
        self.opponent = opponent
        self.match_index = match_index
        self.match_id = match_id
        self.status = "PENDING"  # CLEAN, PROBLEMS, TIMEOUT, CRASH, SKIPPED
        self.games_count = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.warnings = 0
        self.timeouts = 0
        self.answer_times = []  # seconds
        self.cost_usd = 0.0
        self.problems = []
        self.error_message = ""
        self.replay_path = ""
        self.duration_s = 0.0

    @property
    def avg_time_s(self):
        return (sum(self.answer_times) / len(self.answer_times)) if self.answer_times else 0.0

    @property
    def max_time_s(self):
        return max(self.answer_times) if self.answer_times else 0.0

    def to_dict(self):
        return {
            "game": self.game,
            "opponent": self.opponent,
            "match_index": self.match_index,
            "match_id": self.match_id,
            "status": self.status,
            "games_count": self.games_count,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "warnings": self.warnings,
            "timeouts": self.timeouts,
            "avg_time_s": round(self.avg_time_s, 3),
            "max_time_s": round(self.max_time_s, 3),
            "cost_usd": round(self.cost_usd, 6),
            "problems": self.problems,
            "error_message": self.error_message,
            "duration_s": round(self.duration_s, 2),
        }


class AggregateGroup:
    """Aggregated metrics across matches for a (game, opponent) pair."""

    def __init__(self, game, opponent):
        self.game = game
        self.opponent = opponent
        self.matches_planned = 0
        self.matches_completed = 0
        self.total_games = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.warnings = 0
        self.timeouts = 0
        self.all_answer_times = []
        self.total_cost_usd = 0.0
        self.crashes = 0

    def add_match(self, mr):
        self.matches_completed += 1
        self.total_games += mr.games_count
        self.wins += mr.wins
        self.losses += mr.losses
        self.draws += mr.draws
        self.warnings += mr.warnings
        self.timeouts += mr.timeouts
        self.all_answer_times.extend(mr.answer_times)
        self.total_cost_usd += mr.cost_usd
        if mr.status in ("CRASH", "TIMEOUT") and mr.games_count == 0:
            self.crashes += 1

    @property
    def avg_time_s(self):
        return (sum(self.all_answer_times) / len(self.all_answer_times)) if self.all_answer_times else 0.0

    @property
    def max_time_s(self):
        return max(self.all_answer_times) if self.all_answer_times else 0.0

    @property
    def cost_per_match(self):
        return (self.total_cost_usd / self.matches_completed) if self.matches_completed else 0.0

    @property
    def win_pct(self):
        base = self.total_games if self.total_games > 0 else self.matches_completed
        return (self.wins / base * 100.0) if base > 0 else 0.0

    def to_dict(self):
        return {
            "game": self.game,
            "opponent": self.opponent,
            "matches_completed": self.matches_completed,
            "total_games": self.total_games,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "win_pct": round(self.win_pct, 1),
            "warnings": self.warnings,
            "timeouts": self.timeouts,
            "avg_time_s": round(self.avg_time_s, 3),
            "max_time_s": round(self.max_time_s, 3),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "cost_per_match": round(self.cost_per_match, 6),
            "crashes": self.crashes,
        }


def _snapshot_metrics_files():
    """Record current files and byte positions in memory/luckydevs/metrics."""
    mdir = os.path.join(OUT, "memory", US, "metrics")
    snap = {}
    if os.path.isdir(mdir):
        for f in os.listdir(mdir):
            p = os.path.join(mdir, f)
            if os.path.isfile(p):
                snap[p] = os.path.getsize(p)
    return snap


def _extract_cost(match_id, snap_before):
    """Calculate USD cost for our bot from newly added metrics lines or match log."""
    cost = 0.0
    found_in_metrics = False

    # 1. Inspect newly written lines in memory/luckydevs/metrics/*.jsonl
    mdir = os.path.join(OUT, "memory", US, "metrics")
    if os.path.isdir(mdir):
        for f in os.listdir(mdir):
            p = os.path.join(mdir, f)
            if not os.path.isfile(p):
                continue
            prev_size = snap_before.get(p, 0)
            try:
                curr_size = os.path.getsize(p)
                if curr_size > prev_size:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(prev_size)
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                llm_info = obj.get("llm") or {}
                                # If llm is dict of keys (as in shutdown: usage("all"))
                                if isinstance(llm_info, dict):
                                    if "cost" in llm_info:
                                        cost = max(cost, float(llm_info.get("cost") or 0.0))
                                        found_in_metrics = True
                                    else:
                                        for _, v in llm_info.items():
                                            if isinstance(v, dict) and "cost" in v:
                                                cost = max(cost, float(v.get("cost") or 0.0))
                                                found_in_metrics = True
                            except Exception:
                                pass
            except Exception:
                pass

    if found_in_metrics:
        return cost

    # 2. Fallback: Parse token usage from match log: results/practice/logs/<match_id>-luckydevs.log
    log_path = os.path.join(OUT, "results", "practice", "logs", "%s-%s.log" % (match_id, US))
    if os.path.isfile(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # Format from bot/llm.py: "llm decide 0.4s in=123 out=45 finish=stop"
                    m = re.search(r"\bllm\s+\S+\s+[\d.]+s\s+in=(\d+)\s+out=(\d+)", line)
                    if m:
                        inp = int(m.group(1))
                        outp = int(m.group(2))
                        cost += (inp * PRICE_INPUT) + (outp * PRICE_OUTPUT)
        except Exception:
            pass

    return cost


def parse_replay_metrics(replay_path, team=US):
    """Extract wins, warnings, timeouts, answer times from replay JSON."""
    stats = {
        "games_count": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "warnings": 0,
        "timeouts": 0,
        "answer_times": [],
        "problems": [],
    }
    if not os.path.isfile(replay_path):
        stats["problems"].append("Replay file missing: %s" % replay_path)
        return stats

    try:
        with open(replay_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        stats["problems"].append("Failed to parse replay JSON: %s" % e)
        return stats

    for g in doc.get("games") or []:
        seats = g.get("seats") or []
        if team not in seats:
            continue
        k = str(seats.index(team))
        n = g.get("index")
        stats["games_count"] += 1

        # Outcome
        res = g.get("result") or {}
        winner = res.get("winner")
        reason = str(res.get("reason") or "").lower()
        if winner is None:
            stats["draws"] += 1
        elif winner == team:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        if "timeout" in reason and winner != team:
            stats["timeouts"] += 1

        # Warnings
        w = (g.get("warnings") or {}).get(k) or 0
        stats["warnings"] += int(w)

        # Study phase
        st = (g.get("study") or {}).get(k) or {}
        st_err = str(st.get("error") or "").lower()
        if st_err:
            stats["problems"].append("Game %s: study error (%s)" % (n, st_err))
            if "timeout" in st_err:
                stats["timeouts"] += 1
        elif "ms" in st and st["ms"] is not None:
            stats["answer_times"].append(st["ms"] / 1000.0)

        # Timeline turns
        for step in g.get("timeline") or []:
            actions = step.get("actions") or {}
            e = actions.get(k)
            if not isinstance(e, dict):
                continue
            if e.get("warning"):
                w_reason = (e["warning"] or {}).get("reason", "unknown")
                stats["problems"].append("Game %s step %s: warning (%s)" % (n, step.get("step"), w_reason))
            err = str(e.get("error") or "").lower()
            if err:
                stats["problems"].append("Game %s step %s: lost by %s" % (n, step.get("step"), err))
                if "timeout" in err:
                    stats["timeouts"] += 1
            elif e.get("action") is not None:
                ms = e.get("ms")
                if ms is not None:
                    stats["answer_times"].append(float(ms) / 1000.0)

    if doc.get("status") != "done":
        err_msg = doc.get("error") or "status=" + str(doc.get("status"))
        stats["problems"].append("Match not done (%s)" % err_msg)
        if "timeout" in str(err_msg).lower():
            stats["timeouts"] += 1

    return stats


def run_practice_match(game, opponent, match_index, run_id, fake=True, prep=False,
                       image=None, match_timeout=180, best_of=1, verbose=False):
    """Execute one practice match via scripts/practice.py."""
    match_id = ("batch-%s-%s-%s-%d" % (run_id, game, opponent, match_index)).lower()
    mr = MatchResult(game, opponent, match_index, match_id)
    replay_file = os.path.join(OUT, "results", "practice", "matches", "%s.json" % match_id)
    mr.replay_path = replay_file

    cmd = [
        sys.executable,
        os.path.join(_SCRIPTS_DIR, "practice.py"),
        "--game", game,
        "--opponent", opponent,
        "--id", match_id,
    ]
    if fake:
        cmd.append("--fake")
    if prep:
        cmd.append("--prep")
    if image:
        cmd.extend(["--image", image])
    if best_of and best_of > 1:
        cmd.extend(["--games", str(best_of)])

    snap_before = _snapshot_metrics_files()
    t0 = time.monotonic()

    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            capture_output=not verbose,
            text=True,
            timeout=match_timeout,
        )
        mr.duration_s = time.monotonic() - t0
        output_text = (proc.stdout or "") + (proc.stderr or "")

        # Extract metrics from replay
        stats = parse_replay_metrics(replay_file, US)
        mr.games_count = stats["games_count"]
        mr.wins = stats["wins"]
        mr.losses = stats["losses"]
        mr.draws = stats["draws"]
        mr.warnings = stats["warnings"]
        mr.timeouts = stats["timeouts"]
        mr.answer_times = stats["answer_times"]
        mr.problems.extend(stats["problems"])

        # Cost
        mr.cost_usd = _extract_cost(match_id, snap_before)

        if proc.returncode == 0 and not mr.problems and "RESULT: CLEAN" in output_text:
            mr.status = "CLEAN"
        else:
            mr.status = "PROBLEMS"
            if proc.returncode != 0 and not mr.problems:
                mr.problems.append("Process exited with code %d" % proc.returncode)

    except subprocess.TimeoutExpired:
        mr.duration_s = time.monotonic() - t0
        mr.status = "TIMEOUT"
        mr.timeouts += 1
        mr.problems.append("Match process timed out after %ds" % match_timeout)
        mr.error_message = "Subprocess timeout"

    except Exception as e:
        mr.duration_s = time.monotonic() - t0
        mr.status = "CRASH"
        mr.problems.append("Execution failure: %s" % e)
        mr.error_message = str(e)

    return mr


def format_ascii_table(aggregates, total_matches, total_time_s):
    """Format aggregated metrics into a clean, standard-library ASCII table."""
    headers = [
        "Game",
        "Opponent",
        "Matches",
        "Wins",
        "Warnings",
        "Timeouts",
        "Avg (s)",
        "Max (s)",
        "Cost/Match",
    ]

    rows = []
    tot_planned = 0
    tot_completed = 0
    tot_games = 0
    tot_wins = 0
    tot_losses = 0
    tot_draws = 0
    tot_warnings = 0
    tot_timeouts = 0
    all_times = []
    tot_cost = 0.0

    for agg in aggregates:
        tot_planned += agg.matches_planned
        tot_completed += agg.matches_completed
        tot_games += agg.total_games
        tot_wins += agg.wins
        tot_losses += agg.losses
        tot_draws += agg.draws
        tot_warnings += agg.warnings
        tot_timeouts += agg.timeouts
        all_times.extend(agg.all_answer_times)
        tot_cost += agg.total_cost_usd

        base_games = agg.total_games if agg.total_games > 0 else agg.matches_completed
        win_str = "%d/%d (%d%%)" % (agg.wins, base_games, int(round(agg.win_pct))) if base_games > 0 else "0/0 (0%)"
        cost_str = "$%.4f" % agg.cost_per_match

        rows.append([
            agg.game,
            agg.opponent,
            str(agg.matches_completed),
            win_str,
            str(agg.warnings),
            str(agg.timeouts),
            ("%.2fs" % agg.avg_time_s) if agg.all_answer_times else "-",
            ("%.2fs" % agg.max_time_s) if agg.all_answer_times else "-",
            cost_str,
        ])

    # Total row
    tot_base = tot_games if tot_games > 0 else tot_completed
    tot_win_pct = (tot_wins / tot_base * 100.0) if tot_base > 0 else 0.0
    tot_win_str = "%d/%d (%d%%)" % (tot_wins, tot_base, int(round(tot_win_pct))) if tot_base > 0 else "0/0 (0%)"
    tot_avg_time = (sum(all_times) / len(all_times)) if all_times else 0.0
    tot_max_time = max(all_times) if all_times else 0.0
    tot_cost_per_match = (tot_cost / tot_completed) if tot_completed > 0 else 0.0

    total_row = [
        "TOTAL / OVERALL",
        "all",
        str(tot_completed),
        tot_win_str,
        str(tot_warnings),
        str(tot_timeouts),
        ("%.2fs" % tot_avg_time) if all_times else "-",
        ("%.2fs" % tot_max_time) if all_times else "-",
        "$%.4f" % tot_cost_per_match,
    ]

    # Compute column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))
    for i, val in enumerate(total_row):
        col_widths[i] = max(col_widths[i], len(val))

    def separator(char="-", cross="+"):
        return cross + cross.join(char * (w + 2) for w in col_widths) + cross

    def format_row(cols, alignments=None):
        out = []
        for i, val in enumerate(cols):
            w = col_widths[i]
            align = alignments[i] if alignments else "left"
            if align == "right":
                out.append(" " + val.rjust(w) + " ")
            elif align == "center":
                out.append(" " + val.center(w) + " ")
            else:
                out.append(" " + val.ljust(w) + " ")
        return "|" + "|".join(out) + "|"

    aligns = ["left", "left", "right", "right", "right", "right", "right", "right", "right"]

    lines = [
        "",
        separator("-", "+"),
        format_row(headers, ["center"] * len(headers)),
        separator("=", "+"),
    ]
    for row in rows:
        lines.append(format_row(row, aligns))
    lines.append(separator("-", "+"))
    lines.append(format_row(total_row, aligns))
    lines.append(separator("-", "+"))
    lines.append("")
    lines.append("Batch Summary: %d match(es) executed in %.1fs | Total spend: $%.4f" % (
        tot_completed, total_time_s, tot_cost))
    lines.append("")
    return "\n".join(lines)


def run_batch(games, opponents, n_matches, fake=True, prep=False, image=None,
              match_timeout=180, best_of=1, output_file=None, verbose=False):
    """Run all matches in the batch and print summary table."""
    run_id = time.strftime("%Y%m%d-%H%M%S")
    print("=" * 78)
    print("Luckydevs Practice Batch Runner")
    print("  Run ID       : %s" % run_id)
    print("  Matches / pair: %d" % n_matches)
    print("  Games        : %s" % ", ".join(games))
    print("  Opponents    : %s" % ", ".join(opponents))
    print("  Mode         : %s" % ("Fake LLM (free)" if fake else "Real dev key"))
    print("  Prep flow    : %s" % ("Enabled" if prep else "Cold containers"))
    if image:
        print("  Image ref    : %s" % image_ref(image))
    total_planned = len(games) * len(opponents) * n_matches
    print("  Total planned: %d matches" % total_planned)
    print("=" * 78, flush=True)

    # Initialize aggregators
    groups = {}
    for g in games:
        for opp in opponents:
            agg = AggregateGroup(g, opp)
            agg.matches_planned = n_matches
            groups[(g, opp)] = agg

    match_results = []
    current_index = 0
    t_start = time.monotonic()

    try:
        for g in games:
            for opp in opponents:
                for idx in range(1, n_matches + 1):
                    current_index += 1
                    prefix = "[%d/%d] %s vs %s (#%d)" % (current_index, total_planned, g, opp, idx)
                    if not verbose:
                        sys.stdout.write("%s ... " % prefix)
                        sys.stdout.flush()

                    mr = run_practice_match(
                        game=g,
                        opponent=opp,
                        match_index=idx,
                        run_id=run_id,
                        fake=fake,
                        prep=prep,
                        image=image,
                        match_timeout=match_timeout,
                        best_of=best_of,
                        verbose=verbose,
                    )
                    match_results.append(mr)
                    groups[(g, opp)].add_match(mr)

                    if not verbose:
                        status_str = mr.status
                        extra = "wins=%d/%d, warn=%d, to=%d, cost=$%.4f in %.1fs" % (
                            mr.wins, mr.games_count, mr.warnings, mr.timeouts, mr.cost_usd, mr.duration_s)
                        print("%s (%s)" % (status_str, extra), flush=True)
                        if mr.problems:
                            for p in mr.problems:
                                print("    - %s" % p, flush=True)

    except KeyboardInterrupt:
        print("\n\n[!] Batch interrupted by user (Ctrl+C). Generating partial results table...\n", flush=True)

    total_duration_s = time.monotonic() - t_start
    table_str = format_ascii_table(list(groups.values()), len(match_results), total_duration_s)
    print(table_str)

    if output_file:
        try:
            report_data = {
                "run_id": run_id,
                "timestamp": time.time(),
                "duration_s": round(total_duration_s, 2),
                "settings": {
                    "matches_per_pair": n_matches,
                    "games": games,
                    "opponents": opponents,
                    "fake": fake,
                    "prep": prep,
                    "image": image,
                    "best_of": best_of,
                },
                "aggregates": [agg.to_dict() for agg in groups.values()],
                "matches": [mr.to_dict() for mr in match_results],
            }
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2)
            print("Report saved to: %s" % os.path.abspath(output_file))
        except Exception as e:
            print("Failed to save report to %s: %s" % (output_file, e), file=sys.stderr)

    # Return 0 if all completed matches had 0 timeouts and 0 crashes
    has_critical_failure = any(mr.timeouts > 0 or mr.status == "CRASH" for mr in match_results)
    return 1 if has_critical_failure else 0


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Run N practice matches over practice games against random and self.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-n", "--matches", type=int, default=1,
                   help="Number of practice matches to play per (game, opponent) pair")
    p.add_argument("--opponents", default="random,self",
                   help="Comma-separated list of opponents (e.g. random,self,starter)")
    p.add_argument("--opponent", action="append", default=[],
                   help="Specify an opponent (repeatable, appends to opponents)")
    p.add_argument("--games", default="all",
                   help="Comma-separated list of games to play, or 'all'")
    p.add_argument("--fake", action="store_true", default=False,
                   help="Use fake LLM (free, deterministic)")
    p.add_argument("--real", action="store_true", default=False,
                   help="Use real dev key (from dev.env or secrets.local.env)")
    p.add_argument("--prep", action="store_true", default=False,
                   help="Run tournament flow: prep container then warm container")
    p.add_argument("--image", default="",
                   help="Docker image tag to run (e.g. dev, t1)")
    p.add_argument("--match-timeout", type=int, default=180,
                   help="Timeout in seconds for a single match before killing it")
    p.add_argument("--best-of", type=int, default=1,
                   help="Games per match (best-of N)")
    p.add_argument("--output", default="",
                   help="Optional JSON file path to write structured results")
    p.add_argument("-v", "--verbose", action="store_true", default=False,
                   help="Stream full arena-lite output for each match")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Discover games and display execution plan without running")
    return p


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Resolve opponents
    opp_list = []
    if args.opponents and args.opponents.strip():
        opp_list = [o.strip() for o in args.opponents.split(",") if o.strip()]
    for o in args.opponent:
        if o.strip() and o.strip() not in opp_list:
            opp_list.append(o.strip())
    if not opp_list:
        opp_list = list(DEFAULT_OPPONENTS)

    # Discover and filter games
    available = discover_available_games()
    if args.games.strip().lower() in ("all", "*", ""):
        selected_games = available
    else:
        req = [g.strip() for g in args.games.split(",") if g.strip()]
        selected_games = [g for g in req if g in available]
        missing = [g for g in req if g not in available]
        if missing:
            print("Warning: game(s) not found in arena-lite or testgames: %s" % ", ".join(missing),
                  file=sys.stderr)
        if not selected_games:
            sys.exit("No valid games selected. Available games: %s" % ", ".join(available))

    # Resolve LLM mode
    fake_mode = True
    if args.real:
        fake_mode = False
    elif not args.fake:
        # Check if dev.env exists and has key; if not, default to fake mode safely
        env_vars = read_env_file(DEV_ENV) or read_env_file(SECRETS)
        if not env_vars.get("LLM_API_KEY"):
            fake_mode = True
        else:
            fake_mode = False

    if args.dry_run:
        print("Dry Run Configuration:")
        print("  Matches per pair: %d" % args.matches)
        print("  Games (%d)       : %s" % (len(selected_games), ", ".join(selected_games)))
        print("  Opponents (%d)   : %s" % (len(opp_list), ", ".join(opp_list)))
        print("  Total matches   : %d" % (len(selected_games) * len(opp_list) * args.matches))
        print("  Fake LLM        : %s" % fake_mode)
        print("  Prep            : %s" % args.prep)
        print("  Image           : %s" % (args.image or "local python"))
        return 0

    return run_batch(
        games=selected_games,
        opponents=opp_list,
        n_matches=args.matches,
        fake=fake_mode,
        prep=args.prep,
        image=args.image,
        match_timeout=args.match_timeout,
        best_of=args.best_of,
        output_file=args.output,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
