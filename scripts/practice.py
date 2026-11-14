"""Thin wrapper around the local practice arena (arena_local) with our env file.

  python scripts/practice.py --list
  python scripts/practice.py --game tictactoe                 local bot, real dev key
  python scripts/practice.py --game tictactoe --fake          local bot, fake LLM (free)
  python scripts/practice.py --game rps_twist --prep --image dev
  python scripts/practice.py --game guess_number --opponent self --games 3

Every other option is passed through to `python -m arena_local practice`
(see --help there). Uses dev.env in the repository root.
"""

import subprocess
import sys

from _common import DEV_ENV, REPO


def main():
    args = sys.argv[1:]
    extra = []
    if "--fake" in args:
        args.remove("--fake")
        extra += ["--set", "BOT_FAKE_LLM=1"]
    if "--debug" in args:
        args.remove("--debug")
        extra += ["--set", "BOT_LOG_LEVEL=debug"]
    if "--env-file" not in args:
        extra += ["--env-file", DEV_ENV]
    cmd = [sys.executable, "-m", "arena_local", "practice"] + args + extra
    sys.exit(subprocess.call(cmd, cwd=REPO))


if __name__ == "__main__":
    main()
