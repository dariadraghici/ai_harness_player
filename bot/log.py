"""Logging to stderr. Never to stdout (stdout belongs to bot/protocol.py)."""

import sys
import threading
import time
import traceback

from . import config

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_threshold = _LEVELS.get(config.LOG_LEVEL, 20)
_lock = threading.Lock()
_t0 = time.time()


def _emit(level, msg):
    if _LEVELS[level] < _threshold:
        return
    line = "[bot %7.2f %s] %s\n" % (time.time() - _t0, level.upper()[:4], msg)
    try:
        with _lock:
            sys.__stderr__.write(line)
            sys.__stderr__.flush()
    except Exception:
        pass


def debug(msg):
    _emit("debug", msg)


def info(msg):
    _emit("info", msg)


def warning(msg):
    _emit("warning", msg)


def error(msg):
    _emit("error", msg)


def exception(msg):
    """Log msg plus the current traceback."""
    _emit("error", msg + "\n" + traceback.format_exc())


def is_debug():
    return _threshold <= 10
