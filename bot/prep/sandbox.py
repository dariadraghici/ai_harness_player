"""Runs generated code in a killable child process. The ONLY place where it runs.

Owner: luckydevs-3. Status: real.

Guarantees, whatever the simulator does (endless loop, memory blowup, crash,
print flood, SystemExit, a huge or non-JSON result):
  1. the bot's stdout, stdin and protocol are never touched (the child gets
     pipes of its own and a minimal environment without the model key);
  2. every call returns or fails within the timeout the caller gave;
  3. the child's memory and CPU are capped (set by the child on itself, see
     sandbox_child.py; best effort where the platform has no `resource`);
  4. no child survives the work it was started for (kill on timeout, on
     close, at kill_all(); the child also exits when its stdin closes; the
     CPU limit is the backstop when the parent dies);
  5. no exception other than SandboxError leaves this module.

Two ways to use a child:

  single calls   call_simulator(path, function, args, timeout), or
                 Worker.call(function, args, timeout): one of the six
                 simulator functions, one round trip per call (about 0.1 ms
                 plus the call). For a handful of calls.
  jobs           Worker.job(job_file, payload, timeout) / run_job(...): one of
                 OUR files (standalone, standard library only, defines
                 run(sim, payload)) runs inside the child next to the
                 simulator and sends back one JSON result. Thousands of
                 simulator calls then cost no round trip. Verification is a
                 job (verify_job.py); an engine's search loop will be one.

A Worker starts its child lazily, can be reused for many requests, restarts
by itself after a kill, and holds no child once closed: an idle specialist
has no child. At most MAX_CHILDREN children are alive at once in the process.

Example (the engines):
    w = sandbox.Worker(spec.simulator_path, cpu_s=600)
    try:
        best = w.job(SEARCH_JOB_FILE, {"observation": obs, "seat": 0, "seconds": 3.0}, timeout=5.0)
    except sandbox.SandboxError as e:
        ...                       # L3 not available this turn
    finally:
        w.close()                 # or keep it while the game is being played
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from .. import log

# ---------------------------------------------------------------- constants
# proposed for bot/config.py
MAX_CHILDREN = 2                 # children alive at once in this process (the container has 2 CPUs)
CHILD_MEMORY_MB = 512            # address space limit of a child
CHILD_CPU_S = 120                # CPU seconds of a child's whole life (backstop, not the timeout)
START_TIMEOUT_S = 10.0           # start of the child plus the load of the simulator
CLOSE_GRACE_S = 0.5              # after closing stdin, before the kill
MAX_LINE_BYTES = 9 * 1024 * 1024  # an answer line longer than this kills the child
STDERR_KEEP_BYTES = 4000         # tail of the child's stderr kept for error messages

CHILD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_child.py")

_EOF = object()
_TOO_LONG = object()
_slots = threading.BoundedSemaphore(MAX_CHILDREN)
_live = set()
_live_lock = threading.Lock()


class SandboxError(Exception):
    """A sandbox call failed: timeout, crash of the child, bad answer, or an error inside the call.

    kind: "timeout", "crash", "error" (the call itself raised; etype has the exception type
    name from the child, for example "ValueError"), "busy" (no free child slot in time),
    "closed" (stopped by kill_all or close while running).
    """

    def __init__(self, message, kind="error", etype=""):
        Exception.__init__(self, message)
        self.kind = kind
        self.etype = etype


def live_count():
    with _live_lock:
        return len(_live)


def kill_all(reason="deadline"):
    """Kill every live child of this process. Returns how many. Never raises."""
    with _live_lock:
        workers = list(_live)
    for w in workers:
        try:
            w.kill(reason)
        except Exception:
            log.exception("sandbox kill_all")
    if workers:
        log.info("sandbox: killed %d child(ren): %s" % (len(workers), reason))
    return len(workers)


class Worker:
    """One sandbox child for one simulator file (or none: path=None for a job without a simulator).

    Not thread-safe: one controller thread per worker. Every method either returns or raises
    SandboxError; close() and kill() never raise.
    """

    def __init__(self, path=None, mem_mb=CHILD_MEMORY_MB, cpu_s=CHILD_CPU_S, name=""):
        self.path = os.path.abspath(path) if path else None
        self.mem_mb = int(mem_mb)
        self.cpu_s = int(cpu_s)
        self.name = name or (os.path.basename(self.path) if self.path else "job")
        self.proc = None
        self.starts = 0
        self.missing = []            # simulator functions the loaded module does not define
        self._q = None
        self._err = bytearray()
        self._has_slot = False
        self._slot_lock = threading.Lock()
        self._stopped = ""           # reason, when killed from outside (kill_all)

    # ------------------------------------------------------------ life cycle
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self, deadline):
        if not _slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise SandboxError("no free sandbox child within the time given", "busy")
        self._has_slot = True
        self._stopped = ""
        env = {"PATH": os.environ.get("PATH", "")}
        for k in ("SYSTEMROOT", "SystemRoot", "TEMP", "TMP"):      # Windows needs these to start Python
            if k in os.environ:
                env[k] = os.environ[k]
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-I", "-S", CHILD_FILE, str(self.mem_mb), str(self.cpu_s)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, close_fds=True, **kwargs)
        except Exception as e:
            self._release()
            raise SandboxError("cannot start the sandbox child: %s" % e, "crash")
        self.starts += 1
        self._q = queue.Queue()
        self._err = bytearray()
        threading.Thread(target=self._read_out, args=(self.proc, self._q), daemon=True,
                         name="sandbox-out").start()
        threading.Thread(target=self._read_err, args=(self.proc, self._err), daemon=True,
                         name="sandbox-err").start()
        with _live_lock:
            _live.add(self)
        if self.path:
            try:
                res = self._request({"op": "load", "path": self.path}, max(0.1, deadline - time.monotonic()))
            except SandboxError as e:
                self.kill()              # a simulator that does not load leaves no child behind
                raise SandboxError("cannot load the simulator: %s" % e, e.kind, e.etype)
            self.missing = list((res or {}).get("missing") or [])

    @staticmethod
    def _read_out(proc, q):
        try:
            while True:
                line = proc.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    q.put(_TOO_LONG)
                    break
                q.put(line)
        except Exception:
            pass
        q.put(_EOF)

    @staticmethod
    def _read_err(proc, keep):
        try:
            while True:
                chunk = proc.stderr.read1(65536)
                if not chunk:
                    break
                keep.extend(chunk)
                if len(keep) > STDERR_KEEP_BYTES:
                    del keep[:len(keep) - STDERR_KEEP_BYTES]
        except Exception:
            pass

    def _release(self):
        with self._slot_lock:            # kill() can come from kill_all() in another thread
            had, self._has_slot = self._has_slot, False
        if had:
            try:
                _slots.release()
            except ValueError:
                pass

    def kill(self, reason=""):
        """Kill the child now (process group), reap it, free its slot. Never raises."""
        proc, self.proc = self.proc, None
        if reason:
            self._stopped = reason
        with _live_lock:
            _live.discard(self)
        if proc is not None:
            try:
                if proc.poll() is None:
                    if os.name != "nt":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
        self._release()

    def close(self):
        """Polite end: close stdin, wait a moment, then kill. Never raises."""
        proc = self.proc
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=CLOSE_GRACE_S)
            except Exception:
                pass
        self.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ requests
    def _stderr_tail(self):
        text = bytes(self._err).decode("utf-8", "replace").strip()
        return (" | child stderr: " + text[-300:]) if text else ""

    def _request(self, req, timeout):
        """Send one request, wait for its answer. Kills the child on anything but a clean answer."""
        t0 = time.monotonic()
        proc = self.proc
        if proc is None:
            raise SandboxError("the sandbox child is not running", "crash")
        try:
            proc.stdin.write((json.dumps(req, ensure_ascii=True) + "\n").encode("ascii"))
            proc.stdin.flush()
        except Exception as e:
            tail = self._stderr_tail()
            self.kill()
            raise SandboxError("the sandbox child is gone (%s)%s" % (type(e).__name__, tail), "crash")
        op = req.get("fn") or req.get("op")
        try:
            item = self._q.get(timeout=max(0.0, timeout - (time.monotonic() - t0)))
        except queue.Empty:
            self.kill()
            raise SandboxError("timeout: %s did not answer within %.1f s (endless loop or too slow)" % (
                op, timeout), "timeout")
        if item is _EOF or item is _TOO_LONG:
            stopped, tail = self._stopped, self._stderr_tail()
            code = proc.poll()
            self.kill()
            if stopped:
                raise SandboxError("stopped: %s" % stopped, "closed")
            if item is _TOO_LONG:
                raise SandboxError("%s returned more than %d bytes" % (op, MAX_LINE_BYTES), "crash")
            raise SandboxError("the sandbox child died during %s (exit code %s; killed by a limit or "
                               "crashed)%s" % (op, code, tail), "crash")
        try:
            res = json.loads(item.decode("utf-8"))
            if not isinstance(res, dict):
                raise ValueError("not an object")
        except Exception:
            self.kill()
            raise SandboxError("the sandbox child sent a line that is not a JSON answer", "crash")
        if not res.get("ok"):
            if res.get("etype") == "MemoryError":
                self.kill()          # after a MemoryError the child is not trustworthy
            raise SandboxError(str(res.get("error") or "error in %s" % op), "error", str(res.get("etype") or ""))
        return res.get("result")

    def _run(self, req, timeout):
        try:
            timeout = float(timeout)
            deadline = time.monotonic() + timeout
            if timeout <= 0:
                raise SandboxError("no time left for %s" % (req.get("fn") or req.get("op")), "timeout")
            if not self.alive():
                self.kill()                                   # reap a dead child, free its slot
                self._start(min(deadline, time.monotonic() + START_TIMEOUT_S))
            return self._request(req, max(0.01, deadline - time.monotonic()))
        except SandboxError:
            raise
        except Exception as e:                                # guarantee 5
            log.exception("sandbox request failed unexpectedly")
            self.kill()
            raise SandboxError("sandbox failure: %s: %s" % (type(e).__name__, e), "crash")

    def call(self, function, args, timeout):
        """Call one simulator function with JSON arguments; returns its JSON result.

        Raises SandboxError (kind "error" when the function itself raised; etype is the
        exception type name, so ValueError from the simulator is recognizable)."""
        return self._run({"op": "call", "fn": function, "args": list(args or [])}, timeout)

    def job(self, job_file, payload, timeout):
        """Run run(sim, payload) of one of OUR job files inside the child; returns its JSON result.

        Raises SandboxError."""
        return self._run({"op": "job", "job": os.path.abspath(job_file), "payload": payload or {}}, timeout)

    def ping(self, timeout=START_TIMEOUT_S):
        return self._run({"op": "ping"}, timeout)


# ---------------------------------------------------------------- one-shot helpers
def call_simulator(path, function, args, timeout):
    """Call function(*args) of the simulator at path in a fresh child. Raises SandboxError.

    One child start per call (about 30 ms): for one-off calls. Use a Worker for more."""
    w = Worker(path)
    try:
        return w.call(function, args, timeout)
    finally:
        w.close()


def run_job(sim_path, job_file, payload, timeout, mem_mb=CHILD_MEMORY_MB, cpu_s=CHILD_CPU_S):
    """Run one job in a fresh child and close it. NEVER raises.

    Returns {"ok": True, "result": ..., "seconds": s} or
            {"ok": False, "error": "...", "kind": "...", "etype": "...", "seconds": s}."""
    t0 = time.monotonic()
    w = Worker(sim_path, mem_mb=mem_mb, cpu_s=cpu_s)
    try:
        res = w.job(job_file, payload, timeout)
        return {"ok": True, "result": res, "seconds": round(time.monotonic() - t0, 3)}
    except SandboxError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "etype": e.etype,
                "seconds": round(time.monotonic() - t0, 3)}
    except Exception as e:
        log.exception("run_job failed unexpectedly")
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "kind": "crash", "etype": "",
                "seconds": round(time.monotonic() - t0, 3)}
    finally:
        w.close()
