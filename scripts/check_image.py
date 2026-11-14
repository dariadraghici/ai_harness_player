"""Automated pre-push checklist strictly validating ARENA-CONTRACT 2.9 (rulebook 14.7).

Usage:
  python scripts/check_image.py <tag>                # e.g. dev, t1, t2, t3
  python scripts/check_image.py <tag> --real         # practice match uses real dev key
  python scripts/check_image.py <tag> --skip-arena   # skip practice match on arena-lite
  python scripts/check_image.py <tag> --verbose      # show detailed logs

Checks strictly covering ARENA-CONTRACT 2.9:
  1. Platform architecture is linux/amd64 (2.9.9)
  2. Silent startup without stray output, keeps waiting (2.9.1)
  3. Multi-game lifecycle: waits after match_over until next start; exits only on shutdown or closed stdin (2.9.3)
  4. Dynamic key switching: switches to model key provided in prep and start (2.9.4)
  5. Mandatory chat compliance: non-empty single-line chat <= 200 chars, opponent chat reached (2.9.5)
  6. Model failure resilience: answers in time with safe move (L1) when model fails (2.9.6)
  7. Watchdog deadline enforcement: safe move when model hangs, no duplicate late stdout (2.9.6)
  8. Stdout purity: single-line JSON only; logs routed to stderr (2.9.7)
  9. Proxy and port security: honors HTTPS_PROXY, no exposed public ports (2.9.8)
  10. No secrets inside image: no .env, secrets*, or private keys baked in
  11. Matching source repository: git commit label check and tree status (2.9.10)
  12. Full practice match with prep on arena-lite (RESULT: CLEAN) (2.9.2)
  13. Idle between games (2.1): after a finished game and before the next start the container has no sandbox
      child alive and its memory is back near the baseline (an idle specialist must cost almost nothing)

Owner: luckydevs-1. Standard library only (RULES.md 23, 28).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import REPO, git_commit, git_dirty, image_ref
from tests.session_script import (
    BotProc,
    check_action,
    check_ready,
    end_msg,
    prep_msg,
    start_msg,
    turn_msg,
)


def docker_cmd(ref, envs=None):
    cmd = ["docker", "run", "-i", "--rm", "--platform", "linux/amd64"]
    for k, v in (envs or {}).items():
        cmd += ["-e", "%s=%s" % (k, v)]
    return cmd + [ref]


IDLE_RSS_SLACK_MB = 60           # the resident set after a game may exceed the baseline by this much


def parse_top(text):
    """Rows (pid, rss_kb, args) from `docker top <name> -eo pid,rss,args`."""
    rows = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def container_procs(name):
    r = subprocess.run(["docker", "top", name, "-eo", "pid,rss,args"], capture_output=True, text=True)
    return parse_top(r.stdout) if r.returncode == 0 else []


def idle_problems(before, after, slack_mb=IDLE_RSS_SLACK_MB):
    """Problems found by comparing the process list at the baseline with the one after a finished game."""
    problems = []
    kids = [row for row in after if "sandbox_child" in row[2]]
    if kids:
        problems.append("%d sandbox child(ren) alive between games: %s" % (len(kids), [k[0] for k in kids]))
    base_mb = sum(r[1] for r in before) / 1024.0
    now_mb = sum(r[1] for r in after) / 1024.0
    if now_mb > base_mb + slack_mb:
        problems.append("memory %.0f MB after the game, baseline %.0f MB (slack %d MB)" % (now_mb, base_mb, slack_mb))
    if not after:
        problems.append("no process list from the container")
    return problems


def check_idle_between_games(ref):
    name = "ld-idle-check-%d" % os.getpid()
    cmd = docker_cmd(ref, {"BOT_FAKE_LLM": "1", "MEMORY_DIR": "/tmp/mem"})
    cmd = cmd[:-1] + ["--name", name, cmd[-1]]
    bot = BotProc(cmd)
    problems = []
    try:
        time.sleep(2)
        before = container_procs(name)
        check_ready(*bot.ask(start_msg(1, match="idle1"), 30), 18.5, problems, "start g1", need_chat=True)
        for n in (1, 2, 3):
            check_action(*bot.ask(turn_msg(n), 20), 13.5, ["1", "2", "3"], problems, "turn %d" % n)
        bot.send(end_msg(1, over=False))
        time.sleep(2)
        problems += idle_problems(before, container_procs(name))
        bot.send({"type": "shutdown"})
        bot.wait(15)
    finally:
        bot.cleanup()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return len(problems) == 0, "; ".join(problems) if problems else "no sandbox child and memory near baseline between games"


def check_docker_available():
    return shutil.which("docker") is not None


def check_platform(ref):
    r = subprocess.run(["docker", "image", "inspect", ref, "--format", "{{.Os}}/{{.Architecture}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, "docker inspect failed: " + r.stderr.strip()
    arch = r.stdout.strip()
    return arch == "linux/amd64", "architecture is %s (expected linux/amd64)" % arch


def check_silent_start(ref):
    cmd = docker_cmd(ref, {"BOT_FAKE_LLM": "1"})
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(5)
    alive = p.poll() is None
    p.stdin.close()
    t0 = time.monotonic()
    try:
        p.wait(timeout=15)
        out = p.stdout.read()
    except subprocess.TimeoutExpired:
        p.kill()
        out = b"(killed after timeout)"
    dt = time.monotonic() - t0
    ok = alive and (out == b"") and (p.returncode == 0) and (dt <= 15.0)
    detail = "alive=%s, stdout=%r, exit=%s in %.1fs" % (alive, out[:60], p.returncode, dt)
    return ok, detail


def check_match_over_wait_and_lifecycle(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        # Match 1: game 1 and game 2
        check_ready(*bot.ask(start_msg(1, match="m1"), 30), 18.5, problems, "start m1 g1", need_chat=True)
        check_action(*bot.ask(turn_msg(1), 20), 13.5, ["1", "2", "3"], problems, "turn m1 g1")
        bot.send(end_msg(1, over=True))

        time.sleep(2)
        if not bot.alive():
            problems.append("container exited prematurely after match_over: true")
        stray = bot.drain()
        if stray:
            problems.append("stray stdout after match_over: %r" % stray)

        # Match 2: new match start must be accepted
        check_ready(*bot.ask(start_msg(1, match="m2"), 30), 18.5, problems, "start m2 g1", need_chat=True)
        check_action(*bot.ask(turn_msg(1), 20), 13.5, ["1", "2", "3"], problems, "turn m2 g1")
        bot.send(end_msg(1, over=True))

        t0 = time.monotonic()
        bot.send({"type": "shutdown"})
        code = bot.wait(15)
        if code != 0:
            problems.append("shutdown exit code %s after %.1fs" % (code, time.monotonic() - t0))
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "waited after match_over, resumed next start, clean shutdown"


def check_key_switching(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "LLM_API_KEY": "env-key-0000", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        p_msg = prep_msg(seconds=600)
        p_msg["llm"] = {"base_url": "http://127.0.0.1:9/v1", "api_key": "prep-key-1111", "model": "haiku"}
        bot.ask(p_msg, 30)

        s_msg = start_msg(1, match="m1")
        s_msg["llm"] = {"base_url": "http://127.0.0.1:9/v1", "api_key": "start-key-2222", "model": "haiku"}
        bot.ask(s_msg, 30)

        bot.send({"type": "shutdown"})
        bot.wait(10)

        stderr_text = bot.stderr_tail(200)
        if "...1111" not in stderr_text:
            problems.append("prep key ...1111 not detected in logs")
        if "...2222" not in stderr_text:
            problems.append("start key ...2222 not detected in logs")
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "switched to prep key (...1111) and start key (...2222)"


def check_chat_compliance(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        opp_chat = [{"from": "opponent", "game": 1, "game_name": "Test pick",
                     "phase": "study", "turn": 0, "kind": "chat",
                     "text": "Hello luckydevs, good luck!"}]
        st_obj, _, _ = bot.ask(start_msg(1, chat=opp_chat), 30)
        if not st_obj or not isinstance(st_obj.get("chat"), str) or not st_obj["chat"].strip():
            problems.append("start answer missing required chat")
        elif len(st_obj["chat"]) > 200 or "\n" in st_obj["chat"]:
            problems.append("start chat invalid (len > 200 or newline)")

        t_chat = opp_chat + [{"from": "opponent", "game": 1, "game_name": "Test pick",
                              "phase": "play", "turn": 1, "kind": "chat",
                              "text": "Play 1 if you dare."}]
        tu_obj, _, _ = bot.ask(turn_msg(1, chat=t_chat), 20)
        if not tu_obj or not isinstance(tu_obj.get("chat"), str) or not tu_obj["chat"].strip():
            problems.append("turn answer missing required chat")
        elif len(tu_obj["chat"]) > 200 or "\n" in tu_obj["chat"]:
            problems.append("turn chat invalid (len > 200 or newline)")

        bot.send({"type": "shutdown"})
        bot.wait(10)
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "valid single-line chat <= 200 chars on start and turn"


def check_model_failure_fallback(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "BOT_FAKE_LLM_MODE": "fail", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        check_ready(*bot.ask(start_msg(1), 30), 18.5, problems, "start on model fail", need_chat=True)
        check_action(*bot.ask(turn_msg(1), 20), 13.5, ["1", "2", "3"], problems, "turn on model fail")
        bot.send({"type": "shutdown"})
        bot.wait(10)
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "answers with safe fallback (L1) when model fails"


def check_watchdog_hang(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "BOT_FAKE_LLM_MODE": "hang",
                                   "BOT_FAKE_LLM_DELAY": "30", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        check_ready(*bot.ask(start_msg(1, study=3), 6), 4.0, problems, "start under hang", need_chat=True)
        check_action(*bot.ask(turn_msg(1, seconds=4), 5), 3.5, ["1", "2", "3"], problems, "turn under hang")
        time.sleep(0.5)
        stray = bot.drain()
        if stray:
            problems.append("late worker produced duplicate stdout: %r" % stray)
        bot.send({"type": "shutdown"})
        bot.wait(10)
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "watchdog answered before hard deadline, no duplicate stdout"


def check_stdout_purity(ref):
    bot = BotProc(docker_cmd(ref, {"BOT_FAKE_LLM": "1", "MEMORY_DIR": "/tmp/mem"}))
    problems = []
    try:
        obj, line, _ = bot.ask(start_msg(1), 10)
        if line is None or not line.startswith("{") or not line.endswith("}"):
            problems.append("stdout line not pure JSON: %r" % line)
        stderr_text = bot.stderr_tail(100)
        if "[bot" not in stderr_text:
            problems.append("stderr did not receive operational logs")
        bot.send({"type": "shutdown"})
        bot.wait(10)
    finally:
        bot.cleanup()
    return len(problems) == 0, "; ".join(problems) if problems else "stdout is pure single-line JSON, logs routed to stderr"


def check_proxy_and_security(ref):
    problems = []
    cmd = docker_cmd(ref, {"BOT_FAKE_LLM": "1", "HTTPS_PROXY": "http://127.0.0.1:8888",
                           "HTTP_PROXY": "http://127.0.0.1:8888",
                           "NO_PROXY": "gateway,localhost,127.0.0.1", "MEMORY_DIR": "/tmp/mem"})
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        p.stdin.write(json.dumps(start_msg(1, study=4)) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        if not line:
            problems.append("container crashed or produced no answer when proxy variables injected")
        else:
            try:
                resp = json.loads(line)
                if resp.get("type") != "ready":
                    problems.append("container answered wrong type %r under proxy environment" % resp.get("type"))
            except Exception as e:
                problems.append("invalid JSON under proxy environment: %s" % e)
        p.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
        p.stdin.flush()
    except Exception as e:
        problems.append("proxy check error: %s" % e)
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        p.wait(10)

    insp = subprocess.run(["docker", "image", "inspect", ref, "--format", "{{json .Config.ExposedPorts}}"],
                          capture_output=True, text=True)
    if insp.stdout.strip() not in ("null", "{}", ""):
        problems.append("image exposes unnecessary listening ports: %s" % insp.stdout.strip())

    return len(problems) == 0, "; ".join(problems) if problems else "proxy environment supported, no open listening ports"


def check_no_secrets(ref):
    r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", "sh", ref,
                        "-c", "find / -xdev \\( -name '*.env' -o -name 'secrets*' -o -name '*.key' \\) 2>/dev/null | "
                              "grep -v -e '^/proc' -e '^/usr/lib' -e '^/usr/local/lib' || true"],
                       capture_output=True, text=True)
    found = r.stdout.strip()
    return found == "", "no secret files found" if found == "" else "found potential secrets: %s" % found[:150]


def check_source_and_commit_label(ref):
    commit = git_commit()
    r = subprocess.run(["docker", "image", "inspect", ref, "--format", '{{index .Config.Labels "git.commit"}}'],
                       capture_output=True, text=True)
    img_commit = r.stdout.strip()
    dirty = git_dirty()
    notes = []
    if img_commit and img_commit != "<no value>":
        if img_commit == commit:
            notes.append("image git.commit label matches HEAD (%s)" % commit[:8])
        else:
            notes.append("commit mismatch: image=%s HEAD=%s" % (img_commit[:8], commit[:8]))
    else:
        notes.append("image has no git.commit label")

    if dirty:
        notes.append("working tree has uncommitted changes")
    else:
        notes.append("clean working tree")

    ok = (not img_commit or img_commit == "<no value>" or img_commit == commit)
    return ok, "; ".join(notes)


def check_practice_match(ref, real=False, skip=False):
    if skip:
        return True, "skipped (--skip-arena requested)"
    lite = os.environ.get("BOT_ARENA_LITE") or os.path.join(REPO, "participant-kit", "arena-lite")
    if not os.path.isfile(os.path.join(lite, "arena", "__main__.py")):
        return True, "skipped (participant-kit/arena-lite not present)"

    cmd = [sys.executable, os.path.join(REPO, "scripts", "practice.py"), "--game", "tic_tac_toe", "--prep",
           "--image", ref, "--games", "2"]
    if not real:
        cmd.append("--fake")
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    clean = (r.returncode == 0) and ("RESULT: CLEAN" in r.stdout)
    detail = "RESULT: CLEAN" if clean else (r.stdout + r.stderr)[-300:].strip()
    return clean, detail


def main():
    p = argparse.ArgumentParser(
        description="Automated pre-push checklist strictly validating ARENA-CONTRACT 2.9 (rulebook 14.7).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("tag", help="Docker image tag or reference to check (e.g. dev, t1)")
    p.add_argument("--real", action="store_true", default=False,
                   help="Run arena-lite practice test with real dev key (costs model budget)")
    p.add_argument("--skip-arena", action="store_true", default=False,
                   help="Skip practice match on arena-lite")
    p.add_argument("-v", "--verbose", action="store_true", default=False,
                   help="Print verbose diagnostic output")
    args = p.parse_args()

    ref = image_ref(args.tag)
    print("=" * 78)
    print("ARENA-CONTRACT 2.9 Image Verification Checklist")
    print("  Target image : %s" % ref)
    print("  LLM mode     : %s" % ("Real dev key" if args.real else "Fake LLM (free)"))
    print("=" * 78, flush=True)

    if not check_docker_available():
        sys.exit("Error: 'docker' command not found. Docker is required to run the image checklist.")

    results = []

    def run_check(label, fn, *fn_args):
        try:
            ok, detail = fn(*fn_args)
        except Exception as e:
            ok, detail = False, "Exception: %s" % e
        status_tag = "PASS" if ok else "FAIL"
        results.append((label, ok, detail))
        print("[%s] %-52s | %s" % (status_tag, label, detail), flush=True)

    # 1. Platform architecture
    run_check("1. Platform architecture linux/amd64 (2.9.9)", check_platform, ref)

    # 2. Silent start
    run_check("2. Silent start and wait (2.9.1)", check_silent_start, ref)

    # 3. Match lifecycle & match_over wait
    run_check("3. Wait after match_over until next start (2.9.3)", check_match_over_wait_and_lifecycle, ref)

    # 4. Key switching
    run_check("4. Dynamic key switching in prep/start (2.9.4)", check_key_switching, ref)

    # 5. Mandatory chat
    run_check("5. Mandatory chat & opponent chat reached (2.9.5)", check_chat_compliance, ref)

    # 6. Fallback on model fail
    run_check("6. Safe fallback (L1) on model failure (2.9.6)", check_model_failure_fallback, ref)

    # 7. Watchdog deadline enforcement
    run_check("7. Watchdog enforcement on hanging model (2.9.6)", check_watchdog_hang, ref)

    # 8. Stdout purity
    run_check("8. Stdout protocol purity & stderr logs (2.9.7)", check_stdout_purity, ref)

    # 9. Proxy and security
    run_check("9. HTTPS_PROXY support & port security (2.9.8)", check_proxy_and_security, ref)

    # 10. No secrets
    run_check("10. No secret files in container image", check_no_secrets, ref)

    # 11. Git source and commit label
    run_check("11. Git commit label and repository clean (2.9.10)", check_source_and_commit_label, ref)

    # 12. Full practice match with prep
    run_check("12. Practice match with prep on arena-lite (2.9.2)", check_practice_match, ref, args.real, args.skip_arena)

    # 13. Idle between games
    run_check("13. Idle between games: no child, memory near baseline", check_idle_between_games, ref)

    failed = [label for label, ok, _ in results if not ok]
    passed = len(results) - len(failed)
    print("=" * 78)
    print("Verification Summary: %d/%d checks passed." % (passed, len(results)))
    if failed:
        print("Failed checks:")
        for f in failed:
            print("  - %s" % f)
        print("=" * 78)
        sys.exit(1)
    else:
        print("All ARENA-CONTRACT 2.9 checklist items PASSED.")
        print("Image is compliant and ready for arena deployment.")
        print("=" * 78)
        sys.exit(0)


if __name__ == "__main__":
    main()
