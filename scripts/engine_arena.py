"""Measure how strong our CODE is, with no arena and no model call.

Usage:
  python scripts/engine_arena.py --game kuhn_bet --a code --b random -n 30
  python scripts/engine_arena.py --game nim_deal --a engine --b first --variant "4 heaps, last stone loses"
  python scripts/engine_arena.py --game tic_tac_toe --a code --b random -n 20 --seconds 3 --json

Why: a practice match costs eight minutes of prep and real model spend, so "is the engine better than the
model here?" was answered two games at a time. Our test games and the kit's practice games are plain Python
objects, and the decision path needs nothing but an observation and a list of legal actions, so the whole
thing can be played in this process, in seconds, for free. Use it to decide whether a family's engine earns
its place (BOT_L3), and to see a regression before a tournament does.

Players:
  code     the real decision path (bot/ladder.py) with the MODEL FORCED TO FAIL, so the move comes from the
           dedicated policy, the engine or the safe move - our code alone, no tokens
  engine   only the family engine / dedicated policy, nothing else (empty answer = a loss of the turn)
  random   the game's own random_action: the baseline every engine must beat
  first    the first legal action: the floor, what the watchdog would send

A specialist with a VERIFIED simulator must already be in /memory for the engine to have anything to search
(run `python scripts/practice.py --game <id> --prep` once). The harness says so instead of pretending.

Owner: luckydevs-1. Standard library only (RULES 23, 28). Reads /memory, writes nothing.
"""

import argparse
import json
import os
import random
import sys
import time

# The model must never be called from here: the point is to measure the code, for free. We do NOT switch the
# fake model on, because that changes HARNESS_VERSION and would hide every specialist prep really built; we
# simply never give the client a key (llm.complete then returns None without touching the network) and replace
# it outright in `silence_model()` below.
os.environ.setdefault("BOT_LOG_LEVEL", "error")
os.environ.setdefault("BOT_ENGINE_SUBSYSTEM", "off")

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
for path in (_HERE, REPO, os.path.join(REPO, "testgames")):
    if path not in sys.path:
        sys.path.insert(0, path)

PLAYERS = ("code", "engine", "random", "first")


def silence_model():
    """Every model call returns nothing, so the move can only come from our own code."""
    from bot import llm
    llm.complete = lambda *a, **kw: None
    return llm


def arena_lite():
    from _common import arena_lite_dir
    return arena_lite_dir()


def load_game(game_id):
    """The Game subclass with this id, from testgames/ first and then the kit's practice games."""
    lite = arena_lite()
    if lite not in sys.path:
        sys.path.insert(0, lite)
    from arena.core import Game
    names = []
    for name in sorted(os.listdir(os.path.join(REPO, "testgames"))):
        if name.endswith(".py") and not name.startswith("_"):
            names.append(("", name[:-3]))
    practice = os.path.join(lite, "arena", "practice")
    if os.path.isdir(practice):
        for name in sorted(os.listdir(practice)):
            if name.endswith(".py") and not name.startswith("_"):
                names.append(("arena.practice.", name[:-3]))
    for prefix, module in names:
        mod = __import__(prefix + module, fromlist=["x"]) if prefix else __import__(module)
        for value in vars(mod).values():
            if isinstance(value, type) and issubclass(value, Game) and value is not Game \
                    and getattr(value, "id", "") == game_id:
                return value
    raise SystemExit("no game with id %r in testgames/ or in the kit's practice games" % game_id)


def find_spec(game_id, label, rules):
    """The prepared specialist of this variant from /memory, or None."""
    from bot import memory, specialist
    import glob
    for meta_path in sorted(glob.glob(os.path.join(memory.root(), memory.safe_name(game_id), "*", "meta.json"))):
        meta = memory.read_json(meta_path, {}) or {}
        if str(meta.get("label") or "") != label:
            continue
        rhash = os.path.basename(os.path.dirname(meta_path))
        spec = memory.load_variant(game_id, rhash, rules)
        if spec is None:
            continue
        specialist.check_simulator(spec)
        return spec
    return None


def turn_message(game, seat, variant, index, step, seconds):
    legal = game.legal_actions(seat)
    return {"observation": game.observation(seat),
            "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None,
            "seconds": seconds, "game_index": index, "turn": step, "seat": seat,
            "variant": dict(variant), "chat": []}


def ask(player, spec, game, seat, variant, index, step, seconds, rng, start_msg):
    """(action or None, seconds spent). None means the player had no answer."""
    t0 = time.monotonic()
    if player == "random":
        return game.random_action(seat, rng), time.monotonic() - t0
    if player == "first":
        legal = game.legal_actions(seat)
        if isinstance(legal, list) and legal:
            return legal[0], time.monotonic() - t0
        return (spec.profile.example_action if spec else None), time.monotonic() - t0
    turn = turn_message(game, seat, variant, index, step, seconds)
    if player == "code":
        from bot import ladder
        decision = ladder.decide_turn(spec, start_msg, turn, time.monotonic() + seconds)
        return decision.action, time.monotonic() - t0
    # player == "engine"
    from bot import policies
    from bot.engines import base as engines_base
    rules = spec.rules if spec else ""
    rows = []
    try:
        if spec is not None and policies.specialized(rules, spec.sample_turn):
            policy = policies.choose(rules, spec.sample_turn)
            rows = [r.get("action") for r in policy.decide({"spec": spec, "turn": turn},
                                                           time.monotonic() + seconds) or ()
                    if isinstance(r, dict)]
        else:
            engine = engines_base.get_engine(spec.profile.family if spec else "unknown")
            if engine is not None and spec is not None and spec.verified:
                rows = [c.action for c in engine.candidates(spec, turn, time.monotonic() + seconds) or ()]
    except Exception as exc:                      # a broken engine is a result, not a crash of the harness
        print("engine raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    return (rows[0] if rows else None), time.monotonic() - t0


def play(game_cls, variant, players, index, seconds, specs, rng, step_cap=400):
    """One game. players[seat] is the player name. Returns (result, stats per seat)."""
    game = game_cls(variant, index)
    stats = [{"moves": 0, "seconds": 0.0, "no_answer": 0, "illegal": 0} for _ in (0, 1)]
    start = [{"seat": s, "game_index": index, "variant": dict(variant), "match_id": "arena-%d" % index,
              "opponent": "harness", "chat": []} for s in (0, 1)]
    for step in range(1, step_cap + 1):
        if game.is_over():
            break
        seats = game.to_move()
        actions = {}
        for seat in seats:
            spec = specs[seat]
            raw, spent = ask(players[seat], spec, game, seat, variant, index, step, seconds, rng, start[seat])
            stats[seat]["moves"] += 1
            stats[seat]["seconds"] += spent
            action = game.normalize(seat, raw) if raw is not None else None
            if action is None:
                if raw is None:
                    stats[seat]["no_answer"] += 1
                else:
                    stats[seat]["illegal"] += 1
                fallback = game.random_action(seat, rng)
                action = game.normalize(seat, fallback)
            actions[seat] = action
        if any(a is None for a in actions.values()):
            break
        game.apply(actions)
    return game.result(), stats


def run(game_id, label, players, n, seconds, seed, swap=True):
    silence_model()
    game_cls = load_game(game_id)
    variants = {str(v.get("label")): v for v in game_cls.variants()}
    if label and label not in variants:
        raise SystemExit("variant %r not found; have: %s" % (label, ", ".join(variants)))
    variant = variants[label] if label else list(variants.values())[0]
    label = str(variant.get("label"))
    rules = game_cls(variant, 1).rules()
    spec = find_spec(game_id, label, rules)
    out = {"game": game_id, "variant": label, "players": list(players), "games": 0,
           "wins": [0, 0], "draws": 0, "seconds": [0.0, 0.0], "moves": [0, 0],
           "no_answer": [0, 0], "illegal": [0, 0],
           "specialist": None if spec is None else {"family": spec.profile.family, "verified": spec.verified,
                                                    "source": spec.source}}
    rng = random.Random(seed)
    for i in range(n):
        seats = list(players) if (not swap or i % 2 == 0) else [players[1], players[0]]
        specs = [spec, spec]
        result, stats = play(game_cls, variant, seats, seed + i, seconds, specs, rng)
        out["games"] += 1
        winner = result.get("winner")
        for seat in (0, 1):
            who = 0 if seats[seat] == players[0] else 1
            out["seconds"][who] += stats[seat]["seconds"]
            out["moves"][who] += stats[seat]["moves"]
            out["no_answer"][who] += stats[seat]["no_answer"]
            out["illegal"][who] += stats[seat]["illegal"]
        if winner is None:
            out["draws"] += 1
        else:
            out["wins"][0 if seats[winner] == players[0] else 1] += 1
    return out


def report(out):
    a, b = out["players"]
    rows = ["", "%s / %s   %s vs %s" % (out["game"], out["variant"], a, b)]
    spec = out["specialist"]
    rows.append("specialist: %s" % ("none in /memory: the engine has nothing to search" if not spec else
                                    "family %s, simulator %s (%s)" % (spec["family"],
                                                                      "verified" if spec["verified"] else "none",
                                                                      spec["source"])))
    n = max(1, out["games"])
    rows.append("%d game(s), seats swapped every game, engine slice %.2f s per move"
                % (out["games"], out.get("slice_s") or 0.0))
    for i, who in enumerate((a, b)):
        moves = max(1, out["moves"][i])
        rows.append("  %-7s won %2d (%3d%%)  mean %.3f s/move  no answer %d  illegal %d" % (
            who, out["wins"][i], round(100.0 * out["wins"][i] / n), out["seconds"][i] / moves,
            out["no_answer"][i], out["illegal"][i]))
    rows.append("  draws   %2d (%3d%%)" % (out["draws"], round(100.0 * out["draws"] / n)))
    edge = out["wins"][0] - out["wins"][1]
    rows.append("%s is %s" % (a, "ahead by %d game(s)" % edge if edge > 0 else
                              ("behind by %d game(s)" % -edge if edge < 0 else "level")))
    rows.append("")
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Play our code against a baseline in a local game, no model call.")
    ap.add_argument("--game", required=True, help="game id (testgames/ or the kit's practice games)")
    ap.add_argument("--variant", default="", help="variant label (default: the first one)")
    ap.add_argument("--a", default="code", choices=PLAYERS)
    ap.add_argument("--b", default="random", choices=PLAYERS)
    ap.add_argument("-n", "--games", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="move clock our side is told it has (the arena gives 20)")
    ap.add_argument("--slice", type=float, default=0.8, dest="slice_s",
                    help="seconds the engine may really search per move (the arena slice is about 3 s)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-swap", action="store_true", help="do not swap seats between games")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    # The engine takes a FRACTION of the move clock (bot/engines/base.py), so a short test clock would starve
    # it. Ask for the slice we want instead, within what bot/resources.py allows.
    fraction = min(0.40, max(0.05, args.slice_s / max(0.5, args.seconds)))
    os.environ["BOT_SLICE_FRACTION"] = "%.4f" % fraction
    out = run(args.game, args.variant, (args.a, args.b), max(1, args.games), args.seconds, args.seed,
              swap=not args.no_swap)
    out["slice_s"] = round(fraction * args.seconds, 2)
    print(json.dumps(out, indent=2) if args.json else report(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
