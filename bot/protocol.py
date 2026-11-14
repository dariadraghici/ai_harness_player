"""The ONLY module that writes to stdout. Also message parsing.

Owner: luckydevs-1. Status: real.

install() must run first thing at startup: it keeps a private handle to the
real stdout (file descriptor 1) for protocol lines and points both sys.stdout
and file descriptor 1 at stderr. A stray print in our code, in a library, in
a C extension or in a child process can then never corrupt the protocol.
"""

import json
import os
import sys
import threading

from . import log

_out_fd = None
_write_lock = threading.Lock()

KNOWN_TYPES = ("prep", "start", "turn", "end", "shutdown")


def install():
    """Redirect stdout to stderr and keep a private protocol handle."""
    global _out_fd
    if _out_fd is not None:
        return
    try:
        sys.stdout.flush()
    except Exception:
        pass
    _out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def parse_line(line):
    """Return the message dict, or None for garbage. Never raises."""
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except Exception:
        log.warning("input is not JSON, ignored: %r" % line[:200])
        return None
    if not isinstance(msg, dict):
        log.warning("input is not a JSON object, ignored: %r" % line[:200])
        return None
    if msg.get("type") not in KNOWN_TYPES:
        log.warning("unknown message type %r, ignored" % (msg.get("type"),))
        return None
    return msg


def _write_line(obj):
    data = (json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    with _write_lock:
        fd = _out_fd if _out_fd is not None else 1
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]


class Request:
    """One message that needs exactly one answer (prep, start, turn).

    answer() is safe to call from any thread and any number of times: only
    the first call writes a line, later calls return False.
    """

    def __init__(self, msg, deadline):
        self.msg = msg
        self.type = msg.get("type")
        self.deadline = deadline          # time.monotonic() of the hard deadline
        self.best = None                  # best answer so far (dict), set by the worker
        self.result = None                # final answer from the worker
        self.answered = False
        self.answer_meta = {}
        self._lock = threading.Lock()

    def set_best(self, obj):
        with self._lock:
            if not self.answered:
                self.best = obj

    def answer(self, obj, meta=None):
        with self._lock:
            if self.answered:
                return False
            self.answered = True
            self.answer_meta = meta or {}
        try:
            _write_line(obj)
        except Exception:
            log.exception("failed to write answer")
        return True

    def drop(self):
        """Mark as answered without sending (the arena moved on)."""
        with self._lock:
            already = self.answered
            self.answered = True
        return not already


def ready(chat=None):
    obj = {"type": "ready"}
    if chat is not None:
        obj["chat"] = chat
    return obj


def action(action_str, chat):
    return {"type": "action", "action": action_str, "chat": chat}
