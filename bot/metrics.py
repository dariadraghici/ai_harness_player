"""One JSON line per decision (level used, time, tokens), for tuning and the demo.

Owner: luckydevs-1. Status: basic.

Lines go to <memory>/metrics/<container_id>.jsonl. One file per container, so
no two processes ever append to the same file.
"""

import os
import socket
import time

from . import log, memory

CONTAINER_ID = "%s-%d-%d" % (socket.gethostname(), os.getpid(), int(time.time()))


def record(kind, **fields):
    """Append one metrics line. Never raises."""
    try:
        fields["kind"] = kind
        fields["ts"] = round(time.time(), 3)
        fields["container"] = CONTAINER_ID
        memory.append_jsonl(memory.metrics_path(CONTAINER_ID), fields)
    except Exception:
        log.exception("metrics.record failed")
