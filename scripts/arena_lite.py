"""Runs the organizers' practice arena (participant-kit/arena-lite) from our repository.

  python scripts/arena_lite.py practice --list
  python scripts/arena_lite.py prep --bot "python -u -m bot.main" --game pig --show
  python scripts/arena_lite.py <any arena-lite command and options>

Same as `python -m arena ...` inside participant-kit/arena-lite, with three
additions so that nothing in the kit has to be edited (RULES.md 27):
  - it runs from the repository root, so "python -u -m bot.main" works as a bot
    command, and results/ and memory/ land in our (ignored) folders;
  - our own test games in testgames/ are added to the arena's `games` folder
    (use them with --game <id>; list them with --games-dir games --list);
  - the kit folder is found by scripts/_common.arena_lite_dir().

Owner: luckydevs-1. scripts/practice.py is the convenient front end.
"""

import os
import sys

from _common import REPO, arena_lite_dir


def main(argv=None):
    lite = arena_lite_dir()
    os.environ.setdefault("ARENA_ROOT", REPO)     # read by arena.config at import time
    os.chdir(REPO)
    sys.path.insert(0, lite)
    import arena.games
    own = os.path.join(REPO, "testgames")
    if os.path.isdir(own) and own not in arena.games.__path__:
        arena.games.__path__.append(own)
    from arena.cli import main as arena_main
    return arena_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
