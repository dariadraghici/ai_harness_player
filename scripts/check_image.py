"""Automates the pre-push checklist (ARENA-CONTRACT 2.9) as far as possible.

  python scripts/check_image.py <tag>          e.g. dev, t1
  python scripts/check_image.py <tag> --real   step 4 uses the real dev key (costs a little)

Checks:
  1. platform of the image is linux/amd64
  2. `docker run -i --rm <image>` starts, prints nothing, keeps waiting
  3. scripted session against the IMAGE (fake LLM): valid answers in time,
     match_over does not exit, stdout purity, clean shutdown
  4. a practice match with prep against the random bot, played by the IMAGE on the
     organizers' arena-lite (scripts/practice.py; needs participant-kit/)
  5. no secret file inside the image
Not automated (check by hand): registry digest after push (scripts/push.py
prints it), proxy behavior in the real arena, source pushed to Gitea.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _common import REPO, image_ref  # noqa: E402
from tests.session_script import run_session  # noqa: E402


def docker_cmd(ref, envs):
    cmd = ["docker", "run", "-i", "--rm", "--platform", "linux/amd64"]
    for k, v in envs.items():
        cmd += ["-e", "%s=%s" % (k, v)]
    return cmd + [ref]


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python scripts/check_image.py <tag> [--real]")
    ref = image_ref(sys.argv[1])
    real = "--real" in sys.argv
    results = []

    def report(name, ok, detail=""):
        results.append((name, ok))
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    # 1. platform
    r = subprocess.run(["docker", "image", "inspect", ref, "--format", "{{.Os}}/{{.Architecture}}"],
                       capture_output=True, text=True)
    report("platform linux/amd64", r.stdout.strip() == "linux/amd64", r.stdout.strip() or r.stderr.strip())

    # 2. silent start, keeps waiting
    p = subprocess.Popen(docker_cmd(ref, {"BOT_FAKE_LLM": "1"}), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(5)
    alive = p.poll() is None
    p.stdin.close()
    t0 = time.monotonic()
    try:
        p.wait(timeout=15)
        out = p.stdout.read()
    except subprocess.TimeoutExpired:
        p.kill()
        out = b"(killed)"
    report("silent start and wait", alive and out == b"" and p.returncode == 0,
           "alive=%s stdout=%r exit=%s after stdin close in %.1fs" % (
               alive, out[:80], p.returncode, time.monotonic() - t0))

    # 3. scripted session against the image
    problems, bot = run_session(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "MEMORY_DIR": "/tmp/mem"}))
    report("scripted session against image", not problems, "; ".join(problems))
    if problems:
        print(bot.stderr_tail())
    bot.cleanup()

    # 4. practice match with prep against the random bot, played by the image
    # (the organizers' arena-lite; Tic-Tac-Toe is also the game of their chat check)
    cmd = [sys.executable, os.path.join(REPO, "scripts", "practice.py"), "--game", "tic_tac_toe", "--prep",
           "--image", ref, "--games", "2"]
    if not real:
        cmd += ["--fake"]
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    print((r.stdout + r.stderr)[-1500:])
    report("practice match with prep (image, %s LLM)" % ("real" if real else "fake"),
           r.returncode == 0 and "RESULT: CLEAN" in r.stdout)

    # 5. no secrets in the image
    r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "sh", ref,
                        "-c", "find / -xdev \\( -name '*.env' -o -name 'secrets*' \\) 2>/dev/null | "
                              "grep -v -e '^/proc' -e '^/usr/lib' -e '^/usr/local/lib' || true"],
                       capture_output=True, text=True)
    report("no secret files in image", r.stdout.strip() == "", r.stdout.strip()[:200])

    failed = [n for n, ok in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    print("Manual: after push, compare the digest printed by scripts/push.py with the registry; "
          "make sure the tagged commit is pushed to Gitea.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
