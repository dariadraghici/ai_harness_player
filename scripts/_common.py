"""Shared helpers for scripts/. Standard library only; works on Windows, macOS, Linux.

Owner: luckydevs-1. Secrets are parsed in Python, never sourced in a shell
(the Harbor robot user contains a "$").
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = "harbor.hackaton-2026.dvloper.io"
IMAGE_REPO = REGISTRY + "/luckydevs/bot"
SECRETS = os.path.join(REPO, "secrets.local.env")
DEV_ENV = os.path.join(REPO, "dev.env")
KIT_URL = "https://git.hackaton-2026.dvloper.io/arena/participant-kit.git"


def arena_lite_dir():
    """Folder of the organizers' practice arena (the participant kit is NOT in our Git).

    Default: <repo>/participant-kit/arena-lite. Another place: set BOT_ARENA_LITE.
    Exits with the clone command when it is missing.
    """
    path = os.environ.get("BOT_ARENA_LITE") or os.path.join(REPO, "participant-kit", "arena-lite")
    if not os.path.isfile(os.path.join(path, "arena", "__main__.py")):
        sys.exit("arena-lite not found at %s\nGet the participant kit (it is ignored by our Git):\n"
                 "  git clone %s participant-kit\nor set BOT_ARENA_LITE to its arena-lite folder." % (path, KIT_URL))
    return os.path.abspath(path)


def image_ref(tag):
    return tag if ("/" in tag or ":" in tag) else "%s:%s" % (IMAGE_REPO, tag)


def read_env_file(path):
    """Plain KEY=VALUE lines, split on the first "=". Missing file -> {}."""
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def run(cmd, check=True, capture=False, **kw):
    print("+ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=kw.pop("cwd", REPO), text=True,
                       capture_output=capture, **kw)
    if check and r.returncode != 0:
        if capture:
            print(r.stdout, r.stderr, file=sys.stderr)
        sys.exit("command failed with exit code %d" % r.returncode)
    return r


def git(*args):
    r = subprocess.run(["git"] + list(args), cwd=REPO, text=True, capture_output=True)
    return r.stdout.strip()


def git_dirty():
    """Uncommitted or untracked (not ignored) files."""
    return git("status", "--porcelain")


def git_commit():
    return git("rev-parse", "HEAD")
