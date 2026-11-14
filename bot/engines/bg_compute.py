"""Low-priority, local-only computation between arena requests.

The manager owns at most one sandbox child.  A live request pauses it and
kills any active job before the request worker is allowed to use the sandbox.
Results are small, versioned policy hints; the verified simulator remains the
source of truth for every live move.
"""

import os
import threading
import time

from .. import config, log, memory
from ..prep import sandbox

JOB_FILES = {
    "think_ahead": "search_job.py",
    "guessing": "guess_job.py",
    "read_opponent": "simultaneous_job.py",
    "bluff": "bluff_job.py",
}
MAX_RECORDS = 256
MAX_ACTION_CHARS = 160
BACKGROUND_FILE = "background.json"
_POLICY_CACHE = {}


def _job_path(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _compact(result):
    if not isinstance(result, dict):
        return {}
    rows = result.get("actions")
    if not isinstance(rows, list):
        return {}
    actions = []
    for row in rows[:16]:
        if not isinstance(row, dict) or not row.get("action"):
            continue
        actions.append({
            "action": str(row["action"])[:MAX_ACTION_CHARS],
            "value": round(float(row.get("value", row.get("ev", 0.0)) or 0.0), 5),
            "probability": round(float(row.get("p_final", row.get("p", 0.0)) or 0.0), 5),
        })
    return {"algo": str(result.get("algo") or "")[:40], "actions": actions}


def _background_path(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), BACKGROUND_FILE)


def load_policy(spec):
    """Load bounded, simulator-matched hints for an engine payload."""
    try:
        path = _background_path(spec)
        mtime = os.path.getmtime(path)
        cached = _POLICY_CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = memory.read_json(path, {})
        if not isinstance(data, dict) or data.get("harness_version") != config.HARNESS_VERSION:
            return {}
        if data.get("simulator") and data["simulator"] != _sim_sha(spec):
            return {}
        records = data.get("records")
        policy = {"records": records[-MAX_RECORDS:]} if isinstance(records, list) else {}
        _POLICY_CACHE[path] = (mtime, policy)
        return policy
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("background policy load failed for %s/%s", spec.game_id, spec.label)
        return {}


def _sim_sha(spec):
    try:
        data = memory.read_json(
            os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "verify.json"), {})
        return str((data.get("winner") or {}).get("sha256") or "")
    except Exception:
        return ""


class BackgroundCompute:
    """Pause-safe background worker for one specialist process."""

    def __init__(self):
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._paused.set()
        self._lock = threading.Lock()
        self._worker = None
        self._specs = []
        self._thread = None
        self._cursor = 0

    def start(self, specs):
        self.stop()
        with self._lock:
            self._specs = list(specs or [])
            self._cursor = 0
            self._stop.clear()
            self._paused.clear()
            self._thread = threading.Thread(target=self._run, name="background-compute",
                                            daemon=True)
            self._thread.start()

    def add_specs(self, specs):
        with self._lock:
            known = {(s.game_id, s.rules_hash) for s in self._specs}
            self._specs.extend(s for s in specs or [] if (s.game_id, s.rules_hash) not in known)

    def pause(self):
        self._paused.set()
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.kill("live request")

    def resume(self):
        with self._lock:
            if self._thread is not None and not self._stop.is_set():
                self._paused.clear()

    def stop(self):
        self._stop.set()
        self._paused.set()
        with self._lock:
            worker, thread = self._worker, self._thread
            self._worker = None
            self._thread = None
        if worker is not None:
            worker.kill("shutdown")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _next_spec(self):
        with self._lock:
            if not self._specs:
                return None
            spec = self._specs[self._cursor % len(self._specs)]
            self._cursor += 1
            return spec

    def _run(self):
        while not self._stop.is_set():
            if self._paused.wait(0.1):
                continue
            spec = self._next_spec()
            if spec is None:
                self._paused.wait(0.5)
                continue
            if sandbox.live_count() >= sandbox.capacity():
                self._paused.wait(0.5)      # live work (warm engine children) holds every slot: do not queue
                continue
            self._compute(spec)

    def _compute(self, spec):
        profile = getattr(spec, "profile", None)
        family = getattr(profile, "family", "")
        job_name = JOB_FILES.get(family)
        sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
        if not (job_name and spec.verified and spec.simulator_path and sample.get("observation")):
            self._paused.wait(0.1)
            return
        legal = sample.get("legal_actions")
        payload = {
            "observation": str(sample.get("observation") or ""),
            "legal_actions": [str(a) for a in legal] if isinstance(legal, list) else None,
            "variant": {"label": spec.label},
            "seat": 0,
            "budget_s": 0.75,
            "margin": config.CANDIDATE_MARGIN,
            "family": family,
            "background": load_policy(spec),
        }
        worker = sandbox.Worker(spec.simulator_path, mem_mb=64, cpu_s=15, nice=10,
                                name="background", slot_wait_s=0.1)
        with self._lock:
            self._worker = worker
        try:
            if self._paused.is_set() or self._stop.is_set():
                return
            result = worker.job(_job_path(job_name), payload, timeout=1.5)
            if not self._paused.is_set() and not self._stop.is_set():
                self._persist(spec, _compact(result))
        except sandbox.SandboxError as exc:
            if not self._paused.is_set() and not self._stop.is_set():
                log.info("background %s skipped: %s", family, exc)
        except Exception:
            log.exception("background computation failed for %s/%s", spec.game_id, spec.label)
        finally:
            worker.close()
            with self._lock:
                if self._worker is worker:
                    self._worker = None

    def _persist(self, spec, record):
        if not record:
            return
        path = _background_path(spec)
        try:
            old = memory.read_json(path, {})
            records = old.get("records") if isinstance(old, dict) else []
            records = records if isinstance(records, list) else []
            records = [r for r in records if isinstance(r, dict)]
            records.append(dict(record, time=int(time.time())))
            memory.write_json_atomic(path, {
                "harness_version": config.HARNESS_VERSION,
                "simulator": _sim_sha(spec),
                "family": spec.profile.family,
                "records": records[-MAX_RECORDS:],
            })
            _POLICY_CACHE.pop(path, None)
        except Exception:
            log.exception("background policy save failed for %s/%s" % (spec.game_id, spec.label))
