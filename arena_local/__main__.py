"""Command line of the local practice arena (our emulator of docs/ARENA-CONTRACT.md).

  python -m arena_local practice --list
  python -m arena_local practice --game tictactoe [--prep] [--opponent random|self]
         [--image TAG_OR_REF | --bot-cmd "python -u -m bot.main"] [--games 2]
         [--env-file dev.env] [--games-dir testgames] [--variant LABEL] [--seed N]

Run from the repository root. Results go to results/<time>-<game>/ (replay.json
and the bot's stderr logs). Exit code 0 when our bot had no warning, timeout,
crash or stray stdout line and exited cleanly.
"""

import argparse
import importlib.util
import os
import shlex
import sys
import tempfile
import time

from .players import ProcessPlayer, RandomPlayer, bot_env
from .runner import dump, play_match, summary_ok

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE_REPO = "harbor.hackaton-2026.dvloper.io/luckydevs/bot"


def load_games(games_dir):
    games = {}
    for fn in sorted(os.listdir(games_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location("testgame_" + fn[:-3], os.path.join(games_dir, fn))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "GAME"):
            games[mod.GAME.game_id] = mod.GAME
    return games


def read_env_file(path):
    vals = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m arena_local")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("practice", help="play one practice match")
    p.add_argument("--list", action="store_true", help="list games and variants")
    p.add_argument("--game")
    p.add_argument("--variant", help="fixed variant label (default: random per game)")
    p.add_argument("--opponent", default="random", help="random or self")
    p.add_argument("--prep", action="store_true", help="send prep first (warm specialist, 30 s study)")
    p.add_argument("--image", help="play with this image (tag like dev, or a full reference)")
    p.add_argument("--bot-cmd", help='local command (default: "%s -u -m bot.main")' % sys.executable)
    p.add_argument("--games", type=int, default=2)
    p.add_argument("--env-file", default=os.path.join(REPO, "dev.env"))
    p.add_argument("--games-dir", default=os.path.join(REPO, "testgames"))
    p.add_argument("--results", default=os.path.join(REPO, "results"))
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--move-seconds", type=int, default=20)
    p.add_argument("--set", action="append", default=[], metavar="BOT_X=V",
                   help="extra BOT_* setting for our bot, e.g. --set BOT_FAKE_LLM=1")
    a = ap.parse_args(argv)

    games = load_games(a.games_dir)
    if a.list or not a.game:
        for gid, g in games.items():
            print("%-14s %-16s family=%-14s variants=%s" % (
                gid, g.game_name, getattr(g, "family", "?"), ",".join(v["label"] for v in g.variants)))
        return 0
    if a.game not in games:
        print("unknown game %r; use --list" % a.game)
        return 2
    game = games[a.game]

    env_vals = read_env_file(a.env_file)
    for kv in a.set:
        k, _, v = kv.partition("=")
        env_vals[k] = v
    llm = {"base_url": env_vals.get("LLM_BASE_URL", ""), "api_key": env_vals.get("LLM_API_KEY", ""),
           "model": env_vals.get("LLM_MODEL", "haiku")}

    run_dir = os.path.join(a.results, time.strftime("%Y%m%d-%H%M%S") + "-" + game.game_id)
    os.makedirs(run_dir, exist_ok=True)
    mem_dir = os.path.join(a.results, "memory")
    os.makedirs(mem_dir, exist_ok=True)
    tmp_env = None

    def make_player(name):
        nonlocal tmp_env
        err = os.path.join(run_dir, name + ".stderr.log")
        if a.image:
            ref = a.image if ("/" in a.image or ":" in a.image) else "%s:%s" % (IMAGE_REPO, a.image)
            if tmp_env is None:
                fd, tmp_env = tempfile.mkstemp(suffix=".env")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for k, v in env_vals.items():
                        if k.startswith("LLM_") or k.startswith("BOT_"):
                            f.write("%s=%s\n" % (k, v))
            cmd = ["docker", "run", "-i", "--rm", "--platform", "linux/amd64", "--env-file", tmp_env,
                   "-e", "MEMORY_DIR=/memory", "-e", "TEAM_ID=" + name,
                   "-v", os.path.abspath(mem_dir) + ":/memory"]
            if hasattr(os, "getuid"):
                # Files in results/memory stay owned by you, not by root.
                cmd += ["--user", "%d:%d" % (os.getuid(), os.getgid())]
            cmd.append(ref)
            return ProcessPlayer(name, cmd, None, err)
        cmd = shlex.split(a.bot_cmd) if a.bot_cmd else [sys.executable, "-u", "-m", "bot.main"]
        return ProcessPlayer(name, cmd, bot_env(env_vals, os.path.abspath(mem_dir), name), err, cwd=REPO)

    ours = make_player("luckydevs")
    opp = make_player("luckydevs-self") if a.opponent == "self" else RandomPlayer("random", game, a.seed)
    try:
        replay = play_match(game, [ours, opp], games=a.games, prep=a.prep, llm=llm,
                            variant_label=a.variant, seed=a.seed, move_seconds=a.move_seconds)
    finally:
        if tmp_env:
            os.remove(tmp_env)
    path = os.path.join(run_dir, "replay.json")
    dump(replay, path)
    print("match score:", replay["match_score"])
    for name, st in replay["stats"].items():
        print("%-16s warnings=%d timeouts=%d crashes=%d avg=%.2fs max=%.2fs prep_ready=%s stray=%d lost_by=%s exit=%s" % (
            name, st["warnings"], st["timeouts"], st["crashes"], st["avg_answer"], st["max_answer"],
            st["prep_ready_s"], len(st["stray_stdout"]), st["lost_by"], replay["exit"][name]))
    print("replay:", os.path.relpath(path, REPO))
    ok = summary_ok(replay, "luckydevs")
    print("RESULT:", "CLEAN" if ok else "PROBLEMS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
