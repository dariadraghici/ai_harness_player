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
