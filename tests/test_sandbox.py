"""Sandbox tests (bot/prep/sandbox.py): whatever a simulator does, the bot survives.

Owner: luckydevs-3. Fixture simulators are written here by hand; they are only
ever loaded inside a sandbox child, never in this process.
"""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bot.prep import sandbox

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = os.name != "nt"
try:
    import resource  # noqa: F401
    HAVE_RESOURCE = True
except ImportError:
    HAVE_RESOURCE = False

# A correct module for a tiny game: a pile of stones, take 1 to 3, the last stone wins.
GOOD = '''"""Pile game."""
import copy

LEGAL_ACTIONS_COMPLETE = True


def parse_observation(observation, variant, seat):
    stones = int(observation.split("Stones left:")[1].split(".")[0])
    return {"me": seat, "stones": stones, "to_move": seat, "winner": None, "seed": 0}


def legal_actions(state):
    if state["winner"] is not None:
        return []
    return ["take %d" % n for n in (1, 2, 3) if n <= state["stones"]]


def apply_action(state, action):
    if action not in legal_actions(state):
        raise ValueError("illegal action: %r" % (action,))
    s = copy.deepcopy(state)
    s["stones"] -= int(action.split()[1])
    if s["stones"] == 0:
        s["winner"] = s["to_move"]
    s["to_move"] = 1 - s["to_move"]
    return s


def is_terminal(state):
    return state["winner"] is not None


def returns(state):
    if state["winner"] is None:
        return [0.0, 0.0]
    return [1.0, -1.0] if state["winner"] == 0 else [-1.0, 1.0]


def current_player(state):
    return state["to_move"]
'''
OBS = "PILE. You are seat 0. Stones left: 7. Your turn."

BROKEN = {
    "loop_at_import": GOOD + "\nwhile True:\n    pass\n",
    "loop_in_function": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    while True:\n        pass\n"),
    "memory": GOOD.replace("def is_terminal(state):\n",
                           "def is_terminal(state):\n    x = [bytearray(64 * 1024 * 1024) for _ in range(64)]\n"),
    "recursion": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    return is_terminal(state)\n"),
    "system_exit": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    raise SystemExit(3)\n"),
    "exception": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    return state['nope']\n"),
    "print_flood": GOOD.replace("def is_terminal(state):\n",
                                "def is_terminal(state):\n    for _ in range(20000):\n        print('x' * 100)\n"),
    "not_json": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    return {1, 2, 3}\n"),
    "huge": GOOD.replace("def is_terminal(state):\n", "def is_terminal(state):\n    return 'y' * (50 * 1024 * 1024)\n"),
    "reads_env": GOOD.replace("import copy\n", "import copy\nimport os\n").replace(
        "def is_terminal(state):\n",
        "def is_terminal(state):\n    return sorted(k for k in os.environ if 'LLM' in k or 'SECRET' in k)\n"),
    "reads_stdin": GOOD.replace("import copy\n", "import copy\nimport sys\n").replace(
        "def is_terminal(state):\n", "def is_terminal(state):\n    return sys.stdin.read()\n"),
}

# One of OUR job files: a loop next to the simulator.
JOB = '''import time


def run(sim, payload):
    state = sim.parse_observation(payload["observation"], {}, 0)
    t0, n = time.perf_counter(), 0
    for _ in range(payload["calls"]):
        sim.legal_actions(state)
        n += 1
    return {"calls": n, "per_second": n / max(time.perf_counter() - t0, 1e-9)}
'''


def pid_alive(pid):
    if not POSIX:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class SandboxTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ld-sandbox-")
        self.pids = []

    def tearDown(self):
        sandbox.kill_all("test teardown")
        self.assertEqual(sandbox.live_count(), 0)
        for pid in self.pids:
            self.assertFalse(pid_alive(pid), "child %d left behind" % pid)
        shutil.rmtree(self.dir, ignore_errors=True)

    def sim(self, name="good"):
        path = os.path.join(self.dir, name + ".py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(GOOD if name == "good" else BROKEN[name])
        return path

    def worker(self, name="good", **kw):
        w = sandbox.Worker(self.sim(name), **kw)
        self.addCleanup(w.close)
        return w

    def started(self, w):
        pid = w.ping(5)["pid"]
        self.pids.append(pid)
        return pid

    def state(self, w):
        return w.call("parse_observation", [OBS, {}, 0], 5)

    def failing(self, name, kind, etype=None, timeout=5.0, fn="is_terminal"):
        w = self.worker(name)
        s = self.state(w)
        self.pids.append(w.ping(5)["pid"])
        t0 = time.monotonic()
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call(fn, [s], timeout)
        self.assertLess(time.monotonic() - t0, timeout + 2.0)
        self.assertEqual(cm.exception.kind, kind, str(cm.exception))
        if etype:
            self.assertEqual(cm.exception.etype, etype, str(cm.exception))
        return w, s, cm.exception

    # ------------------------------------------------------------ the good path
    def test_single_calls_and_reuse(self):
        w = self.worker()
        pid = self.started(w)
        s = self.state(w)
        self.assertEqual(s["stones"], 7)
        for _ in range(100):
            self.assertEqual(w.call("legal_actions", [s], 5), ["take 1", "take 2", "take 3"])
        s2 = w.call("apply_action", [s, "take 3"], 5)
        self.assertEqual((s2["stones"], s["stones"]), (4, 7))
        self.assertEqual((w.starts, w.ping(5)["pid"]), (1, pid))      # one child for all of it
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call("apply_action", [s, "take 9"], 5)
        self.assertEqual((cm.exception.kind, cm.exception.etype), ("error", "ValueError"))
        with self.assertRaises(sandbox.SandboxError):
            w.call("not_a_function", [], 5)
        self.assertTrue(w.alive())                                    # errors do not cost the child
        w.close()
        self.assertFalse(w.alive())
        self.assertEqual(sandbox.live_count(), 0)

    def test_one_shot_call_and_missing_file(self):
        self.assertEqual(sandbox.call_simulator(self.sim(), "parse_observation", [OBS, {}, 1], 5)["me"], 1)
        with self.assertRaises(sandbox.SandboxError):
            sandbox.call_simulator(os.path.join(self.dir, "nope.py"), "legal_actions", [{}], 5)
        self.assertEqual(sandbox.live_count(), 0)

    def test_job_runs_next_to_the_simulator(self):
        job = os.path.join(self.dir, "job_loop.py")
        with open(job, "w", encoding="utf-8") as f:
            f.write(JOB)
        w = self.worker()
        res = w.job(job, {"observation": OBS, "calls": 20000}, 10)
        self.assertEqual(res["calls"], 20000)
        self.assertGreater(res["per_second"], 20000)                  # no round trip per call
        out = sandbox.run_job(self.sim(), job, {"observation": OBS, "calls": 10}, 10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["calls"], 10)
        bad = sandbox.run_job(self.sim(), os.path.join(self.dir, "no_job.py"), {}, 5)   # never raises
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["error"])
        late = sandbox.run_job(self.sim(), job, {"observation": OBS, "calls": 10}, 0)
        self.assertEqual((late["ok"], late["kind"]), (False, "timeout"))

    # ------------------------------------------------------------ what a simulator can do wrong
    def test_endless_loop_at_import(self):
        w = self.worker("loop_at_import")
        t0 = time.monotonic()
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call("legal_actions", [{}], 1.0)
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertFalse(w.alive())

    def test_endless_loop_in_function_then_restart(self):
        w, s, _ = self.failing("loop_in_function", "timeout", timeout=0.7)
        self.assertFalse(w.alive())
        self.assertEqual(w.call("legal_actions", [s], 5), ["take 1", "take 2", "take 3"])   # restarted by itself
        self.assertEqual(w.starts, 2)

    @unittest.skipUnless(HAVE_RESOURCE and sys.platform.startswith("linux"), "needs rlimits")
    def test_memory_limit(self):
        w = self.worker("memory", mem_mb=256)
        s = self.state(w)
        self.pids.append(w.ping(5)["pid"])
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call("is_terminal", [s], 20)
        self.assertIn(cm.exception.kind, ("error", "crash"))
        self.assertIn("RLIMIT_AS", sandbox.Worker(None).ping(5)["limits"])

    def test_deep_recursion(self):
        self.failing("recursion", "error", "RecursionError")

    def test_system_exit_and_exception(self):
        w, s, _ = self.failing("system_exit", "error", "SystemExit")
        self.assertTrue(w.alive())
        w2, _, e = self.failing("exception", "error", "KeyError")
        self.assertIn("line", str(e))                                 # where in the simulator file

    def test_print_flood_does_not_reach_the_answers(self):
        w = self.worker("print_flood")
        s = self.state(w)
        self.assertIs(w.call("is_terminal", [s], 20), False)
        self.assertEqual(w.call("legal_actions", [s], 5), ["take 1", "take 2", "take 3"])

    def test_results_that_cannot_travel(self):
        self.failing("not_json", "error", "NotJson")
        self.failing("huge", "error", "TooLarge", timeout=30)

    def test_child_sees_no_key_and_no_stdin(self):
        old = os.environ.get("LLM_API_KEY")
        os.environ["LLM_API_KEY"] = "sk-test-not-for-children"
        os.environ["HARBOR_SECRET"] = "x"
        try:
            w = self.worker("reads_env")
            self.assertEqual(w.call("is_terminal", [self.state(w)], 5), [])
        finally:
            os.environ.pop("HARBOR_SECRET", None)
            if old is None:
                os.environ.pop("LLM_API_KEY", None)
            else:
                os.environ["LLM_API_KEY"] = old
        w2 = self.worker("reads_stdin")
        self.assertEqual(w2.call("is_terminal", [self.state(w2)], 5), "")     # the null device, not our requests
        self.assertEqual(w2.call("legal_actions", [self.state(w2)], 5), ["take 1", "take 2", "take 3"])

    @unittest.skipUnless(POSIX, "needs POSIX signals")
    def test_child_killed_from_outside(self):
        w = self.worker("loop_in_function")
        s = self.state(w)
        pid = self.started(w)
        threading.Timer(0.3, lambda: os.kill(pid, signal.SIGKILL)).start()
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call("is_terminal", [s], 10)
        self.assertEqual(cm.exception.kind, "crash")
        self.assertEqual(w.call("legal_actions", [s], 5), ["take 1", "take 2", "take 3"])

    # ------------------------------------------------------------ caps and cleanup
    def test_two_at_once_cap_and_kill_all(self):
        a, b = self.worker("loop_in_function"), self.worker()
        sa, sb = self.state(a), self.state(b)
        self.started(a), self.started(b)
        self.assertEqual(sandbox.live_count(), 2)
        t0 = time.monotonic()
        with self.assertRaises(sandbox.SandboxError) as cm:              # third child: no slot
            sandbox.call_simulator(self.sim(), "legal_actions", [sb], 0.5)
        self.assertEqual(cm.exception.kind, "busy")
        self.assertLess(time.monotonic() - t0, 2.0)
        box = {}

        def blocked():
            try:
                a.call("is_terminal", [sa], 30)
            except sandbox.SandboxError as e:
                box["kind"] = e.kind
        th = threading.Thread(target=blocked)
        th.start()
        time.sleep(0.3)
        self.assertEqual(sandbox.kill_all("deadline passed"), 2)
        th.join(5)
        self.assertEqual(box.get("kind"), "closed")
        self.assertEqual(sandbox.live_count(), 0)
        self.assertEqual(sandbox.call_simulator(self.sim(), "legal_actions", [sb], 5),
                         ["take 1", "take 2", "take 3"])                # the slots are free again

    def test_child_exits_when_stdin_closes(self):
        w = self.worker()
        pid = self.started(w)
        proc = w.proc
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=5), 0)
        w.kill()
        self.assertFalse(pid_alive(pid))

    def test_limits_without_resource_module(self):
        spec = importlib.util.spec_from_file_location("sandbox_child_for_test", sandbox.CHILD_FILE)
        child = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(child)                                  # our own file, not a candidate
        saved = sys.modules.get("resource", "absent")
        sys.modules["resource"] = None                                  # import resource -> ImportError
        try:
            self.assertEqual(child.set_limits(256, 10), [])
        finally:
            if saved == "absent":
                sys.modules.pop("resource", None)
            else:
                sys.modules["resource"] = saved

    def test_bot_stdout_stays_clean(self):
        """A process that plays the role of the bot runs the worst fixtures; its stdout is byte-exact."""
        paths = [self.sim(n) for n in ("print_flood", "system_exit", "exception", "loop_in_function")]
        script = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from bot import protocol\n"
            "protocol.install()\n"
            "from bot.prep import sandbox\n"
            "for p in %r:\n"
            "    w = sandbox.Worker(p)\n"
            "    try:\n"
            "        s = w.call('parse_observation', [%r, {}, 0], 5)\n"
            "        w.call('is_terminal', [s], 0.5)\n"
            "    except sandbox.SandboxError:\n"
            "        pass\n"
            "    finally:\n"
            "        w.close()\n"
            "protocol._write_line(protocol.ready())\n" % (REPO, paths, OBS))
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
        self.assertEqual(r.stdout, b'{"type":"ready"}\n', r.stderr[-800:])
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
