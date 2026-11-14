"""The program that runs INSIDE a sandbox child process. Never imported by the bot.

Owner: luckydevs-3. Status: real.

Started by bot/prep/sandbox.py as

    python -I -S -B <this file> <memory limit MB> <cpu limit s>

It is OUR code and trusted, but it lives next to generated simulator code, so
it is standalone: standard library only, nothing from `bot`. Protocol with the
parent: one JSON object per line on stdin, one JSON answer per line on the
private copy of stdout.

Requests:
  {"op": "ping"}
  {"op": "load", "path": "<simulator file>"}
  {"op": "call", "fn": "<one of the six functions>", "args": [...]}
  {"op": "job", "job": "<absolute path of a job file>", "payload": {...}}

Answers: {"ok": true, "result": ...} or {"ok": false, "error": "...", "etype": "..."}.

A JOB is one of our own files (for example bot/prep/verify_job.py, later the
search loop of an engine). It defines run(sim, payload) -> JSON result, where
sim is the loaded simulator module (None when no simulator was loaded). The
job runs here, next to the simulator, so thousands of simulator calls cost no
round trip. A job file must be standalone like this one.

Before any generated code is loaded this program
  - limits itself (address space, CPU seconds, file size, open files, no core
    dump) where the platform has `resource`;
  - keeps a private handle for answers and points file descriptor 1 and
    sys.stdout at the null device, so a stray print cannot corrupt an answer;
  - reads requests from a private handle and points sys.stdin at the null
    device, so generated code cannot swallow a request.
It exits when its stdin closes.
"""

import json
import os
import sys
import traceback
import types

MAX_ANSWER_BYTES = 8 * 1024 * 1024      # an answer longer than this becomes an error answer
SIM_FUNCTIONS = ("parse_observation", "legal_actions", "apply_action", "is_terminal", "returns",
                 "current_player")


def set_limits(mem_mb, cpu_s):
    """Best effort: returns the list of limits that were set."""
    done = []
    try:
        import resource
    except ImportError:
        return done
    wanted = [("RLIMIT_AS", mem_mb * 1024 * 1024 if mem_mb > 0 else None),
              ("RLIMIT_CPU", cpu_s if cpu_s > 0 else None),
              ("RLIMIT_FSIZE", 1024 * 1024),
              ("RLIMIT_NOFILE", 64),
              ("RLIMIT_CORE", 0)]
    for name, value in wanted:
        if value is None or not hasattr(resource, name):
            continue
        try:
            res = getattr(resource, name)
            soft, hard = resource.getrlimit(res)
            value = int(value)
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(res, (value, hard if name == "RLIMIT_CPU" else value))
            done.append(name)
        except (ValueError, OSError):
            pass
    return done


def load_file(path, name):
    """Run the file's CURRENT source as a module. No bytecode cache on purpose: a candidate is
    rewritten in place by a repair, and a cached .pyc is matched by mtime in seconds and size
    only, so a stale one could be loaded for a new file."""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    module = types.ModuleType(name)
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def describe(exc):
    text = str(exc)
    return "%s: %s" % (type(exc).__name__, text[:400]) if text else type(exc).__name__


def last_frame(exc, path):
    """'line N' of the simulator file where the exception came from, or ''."""
    try:
        for frame in reversed(traceback.extract_tb(exc.__traceback__)):
            if path and os.path.abspath(frame.filename) == os.path.abspath(path):
                return "line %d" % frame.lineno
    except Exception:
        pass
    return ""


def main():
    mem_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    cpu_s = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    limits = set_limits(mem_mb, cpu_s)

    # Private handles first, then make the standard streams harmless.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    inp = os.fdopen(os.dup(0), "rb")
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(os.devnull, "w")

    state = {"sim": None, "path": None, "jobs": {}}

    def answer(obj):
        try:
            data = json.dumps(obj, ensure_ascii=True, allow_nan=False).encode("ascii")
        except (TypeError, ValueError, RecursionError) as e:
            data = json.dumps({"ok": False, "etype": "NotJson",
                               "error": "the result is not JSON-serializable (%s)" % describe(e)}).encode("ascii")
        if len(data) > MAX_ANSWER_BYTES:
            data = json.dumps({"ok": False, "etype": "TooLarge",
                               "error": "the result is larger than %d bytes" % MAX_ANSWER_BYTES}).encode("ascii")
        out.write(data + b"\n")

    def handle(req):
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "result": {"pid": os.getpid(), "limits": limits}}
        if op == "load":
            state["sim"], state["path"] = None, req.get("path")
            state["sim"] = load_file(req["path"], "simulator")
            missing = [f for f in SIM_FUNCTIONS if not callable(getattr(state["sim"], f, None))]
            return {"ok": True, "result": {"missing": missing}}
        if op == "call":
            fn = req.get("fn")
            if state["sim"] is None:
                return {"ok": False, "etype": "NoSimulator", "error": "no simulator loaded"}
            if fn not in SIM_FUNCTIONS or not callable(getattr(state["sim"], fn, None)):
                return {"ok": False, "etype": "NoFunction", "error": "no simulator function %r" % (fn,)}
            return {"ok": True, "result": getattr(state["sim"], fn)(*list(req.get("args") or []))}
        if op == "job":
            path = req.get("job")
            if path not in state["jobs"]:
                state["jobs"][path] = load_file(path, "job_%d" % len(state["jobs"]))
            return {"ok": True, "result": state["jobs"][path].run(state["sim"], req.get("payload") or {})}
        return {"ok": False, "etype": "BadRequest", "error": "unknown op %r" % (op,)}

    while True:
        line = inp.readline()
        if not line:
            break                       # stdin closed: the parent is done with us
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request is not an object")
        except Exception as e:
            answer({"ok": False, "etype": "BadRequest", "error": describe(e)})
            continue
        try:
            res = handle(req)
        except MemoryError:
            res = {"ok": False, "etype": "MemoryError", "error": "MemoryError: the memory limit was reached"}
        except BaseException as e:      # SystemExit and KeyboardInterrupt from generated code included
            where = last_frame(e, state["path"])
            res = {"ok": False, "etype": type(e).__name__,
                   "error": describe(e) + (" (%s)" % where if where else "")}
        answer(res)


if __name__ == "__main__":
    main()
