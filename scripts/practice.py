"""Practice matches on the organizers' arena-lite (participant-kit), with our env file.

  python scripts/practice.py --list
  python scripts/practice.py --game tic_tac_toe --fake          local bot, fake LLM (free)
  python scripts/practice.py --game tic_tac_toe                 local bot, real dev key
  python scripts/practice.py --game guess_number --prep         the tournament flow: prep, then warm
  python scripts/practice.py --game pig --prep --image dev      played by the IMAGE
  python scripts/practice.py --game rps_twist --opponent self --games 4
  python scripts/practice.py --game tic_tac_toe --debug         also logs every prompt

Our bot is bot-a (name luckydevs). --opponent is random (the kit's random bot,
default), self (our bot again), starter (the kit's sample bot; needs
`pip install openai`) or any arena-lite bot spec (a command or an image).
Options we do not know (--seed, --study, --move, --prep-seconds, ...) are
passed to `python -m arena practice` unchanged.

At the end the replay and our logs are checked and one line is printed:
  RESULT: CLEAN      no warning, timeout, crash, bad answer, stray stdout line,
                     failed prep or silent chat on our side
  RESULT: PROBLEMS   with the list (exit code 1)

Owner: luckydevs-1. The arena itself is never edited (RULES.md 27).
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

from _common import DEV_ENV, REPO, arena_lite_dir, image_ref, read_env_file

US = "luckydevs"
# Where arena-lite writes results/ and memory/ (scripts/arena_lite.py: the repository root by default).
OUT = os.environ.get("ARENA_ROOT") or REPO


def _join(parts):
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def bot_command():
    return _join([sys.executable, "-u", "-m", "bot.main"])


def opponent_spec(name, lite, ours):
    if name == "random":
        return _join([sys.executable, os.path.join(lite, "examples", "random_bot.py")])
    if name == "starter":
        return _join([sys.executable, os.path.join(os.path.dirname(lite), "starter", "main.py")])
    if name == "self":
        return ours
    return name


def log_files(match_id):
    """Our log files of this run: the match log and, with --prep, the warm container logs."""
    logs = os.path.join(OUT, "results", "practice", "logs")
    out = [os.path.join(logs, "%s-%s.log" % (match_id, US))]
    warm = os.path.join(logs, "warm")
    if os.path.isdir(warm):
        out += [os.path.join(warm, f) for f in sorted(os.listdir(warm)) if f.startswith(US + "--")]
    return out


def sizes(paths):
    return {p: (os.path.getsize(p) if os.path.exists(p) else 0) for p in paths}


def evaluate(doc, team, new_log_text="", prep_lines=()):
    """Return (problems, stats) for `team` from a replay document of arena-lite."""
    problems, times = [], []
    stats = {"games": 0, "wins": 0, "draws": 0, "losses": 0, "warnings": 0, "answers": 0}
    for g in doc.get("games") or []:
        seats = g.get("seats") or []
        if team not in seats:
            continue
        k = str(seats.index(team))
        n = g.get("index")
        stats["games"] += 1
        res = g.get("result") or {}
        outcome = "draws" if res.get("winner") is None else ("wins" if res.get("winner") == team else "losses")
        stats[outcome] += 1
        if g.get("error"):
            problems.append("game %s: game engine error (not our bot)" % n)
        stats["warnings"] += (g.get("warnings") or {}).get(k) or 0
        st = (g.get("study") or {}).get(k) or {}
        if st.get("error"):
            problems.append("game %s: no valid ready at start (%s)" % (n, st.get("error")))
        elif "ms" in st:
            times.append(st["ms"])
        for step in g.get("timeline") or []:
            e = (step.get("actions") or {}).get(k)
            if not isinstance(e, dict):
                continue
            if e.get("warning"):
                problems.append("game %s step %s: WARNING %s" % (n, step.get("step"), e["warning"].get("reason")))
            if e.get("error"):
                problems.append("game %s step %s: lost by %s" % (n, step.get("step"), e.get("error")))
            elif e.get("action") is not None:
                times.append(e.get("ms") or 0)
    stats["answers"] = len(times)
    stats["avg_s"] = round(sum(times) / len(times) / 1000.0, 2) if times else 0.0
    stats["max_s"] = round(max(times) / 1000.0, 2) if times else 0.0
    stats["chat_lines"] = len([c for c in doc.get("chat") or [] if c.get("team") == team])
    if stats["answers"] and not stats["chat_lines"]:
        problems.append("we sent no chat line in the whole match (chat rule, ARENA-CONTRACT 2.5)")
    if doc.get("status") != "done":
        problems.append("match status is %r (%s)" % (doc.get("status"), doc.get("error")))
    stray = [ln for ln in new_log_text.splitlines() if "unexpected stdout line" in ln]
    if stray:
        problems.append("%d stray stdout line(s), first: %s" % (len(stray), stray[0][:160]))
    for ln in prep_lines:
        if ln.startswith("prep %s x " % team) and ": ready" not in ln:
            problems.append(ln.strip())
    return problems, stats


def run(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--game")
    ap.add_argument("--opponent", default="random")
    ap.add_argument("--image")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--games", type=int)
    ap.add_argument("--set", action="append", default=[], metavar="BOT_X=V")
    ap.add_argument("--id")
    ap.add_argument("-h", "--help", action="store_true")
    args, rest = ap.parse_known_args(argv)
    if args.help:
        print(__doc__)
        return 0
    lite = arena_lite_dir()
    launcher = [sys.executable, os.path.join(REPO, "scripts", "arena_lite.py"), "practice"]
    if args.list:
        subprocess.call(launcher + ["--list"], cwd=REPO)
        print("--- our own test games (testgames/)", flush=True)
        return subprocess.call(launcher + ["--games-dir", "games", "--list"], cwd=REPO)

    ours = ("image:" + image_ref(args.image)) if args.image else bot_command()
    match_id = (args.id or "p-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), args.game or "any")).lower()
    env = {k: v for k, v in read_env_file(DEV_ENV).items() if k.startswith(("LLM_", "BOT_"))}
    if not env and not args.fake:
        sys.exit("dev.env not found or empty: copy dev.env.example to dev.env, or use --fake")
    if args.fake:
        env["BOT_FAKE_LLM"] = "1"
    if args.debug:
        env["BOT_LOG_LEVEL"] = "debug"
    for item in args.set:
        k, _, v = item.partition("=")
        env[k.strip()] = v.strip()

    cmd = launcher + ["--bot-a", ours, "--bot-b", opponent_spec(args.opponent, lite, ours),
                      "--name-a", US, "--name-b", "sparring" if args.opponent == "self" else "opponent",
                      "--id", match_id]
    if args.game:
        cmd += ["--game", args.game]
    if args.games:
        cmd += ["--best-of", str(args.games)]
    cmd += rest
    before = sizes(log_files(match_id))
    # The env file holds the dev key: a private temp file, removed after the run.
    fd, env_path = tempfile.mkstemp(prefix="luckydevs-practice-", suffix=".env")
    lines = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join("%s=%s\n" % kv for kv in env.items()))
        p = subprocess.Popen(cmd + ["--env-file", env_path], cwd=REPO, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
        code = p.wait()
    finally:
        try:
            os.remove(env_path)
        except OSError:
            pass

    replay = os.path.join(OUT, "results", "practice", "matches", match_id + ".json")
    if code != 0 or not os.path.exists(replay):
        print("RESULT: PROBLEMS\n  - arena-lite exited with code %s and the replay %s" % (
            code, "exists" if os.path.exists(replay) else "is missing"))
        return 1
    with open(replay, encoding="utf-8") as f:
        doc = json.load(f)
    new_text = ""
    for path in log_files(match_id):
        try:
            with open(path, "rb") as f:
                f.seek(before.get(path, 0))
                new_text += f.read().decode("utf-8", "replace")
        except OSError:
            pass
    problems, st = evaluate(doc, US, new_text, lines)
    print("\n%s: %d game(s): %d won, %d drawn, %d lost | warnings %d | answers %d, avg %.2fs, max %.2fs | "
          "our chat lines %d" % (US, st["games"], st["wins"], st["draws"], st["losses"], st["warnings"],
                                 st["answers"], st["avg_s"], st["max_s"], st["chat_lines"]))
    print("replay: %s" % replay)
    print("our logs: %s" % ", ".join(p for p in log_files(match_id) if os.path.exists(p)))
    if problems:
        print("RESULT: PROBLEMS")
        for pr in problems:
            print("  - " + pr)
        return 1
    print("RESULT: CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
