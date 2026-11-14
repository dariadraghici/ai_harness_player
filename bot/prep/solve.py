"""Prep: solve what is solvable and store it (WP4 of PROMPT-RESOURCES.md).

Owner: luckydevs-3. Status: real.

For a variant whose family is think_ahead and whose simulator is verified, the positions that can follow the
prep sample position within a few plies are searched by the match's own alpha-beta (prep/solve_job.py) with a
generous budget, in as many sandbox children as the resource plan allows, and every root that is solved to the
end with exact values is added to <variant_dir>/solved.json through the same writer and the same key as the
engine's own cache (engines/search.py). In the match such a root costs 0 ms and 0 tokens.

What is stored is keyed by the canonical state text and tied to the hash of the simulator, so a repaired
simulator invalidates it, and a position of another deal simply is never asked for. No sentence about the
sample deal is stored anywhere. Everything here is best effort and never raises.
"""

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

from .. import log, memory, resources
from ..engines import base, search
from . import sandbox, verify

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "solve_job.py")
SOLVE_MAX_S = 90.0              # wall clock of the whole pass
SOLVE_FRACTION = 0.35           # of the time left when the pass starts
SOLVE_MIN_S = 25.0              # do not start with less than this left
MAX_PLIES = 4
ROOT_S = 4.0                    # per root; the match slice is 3 s, prep may be generous


def marker(spec):
    """What a finished pass was about: the sample position and the depth. Stored in solved.json next to the
    simulator hash, so a pass is repeated only when one of them changed."""
    sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
    text = "%s|%d|%s" % (sample.get("observation") or "", MAX_PLIES, sorted(sample.get("legal_actions") or []))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_done(spec):
    d = memory.read_json(search.cache_path(spec), {})
    sha = base.sim_sha(spec)
    return bool(isinstance(d, dict) and sha and d.get("sim") == sha and d.get("presolved") == marker(spec))


def eligible(spec):
    sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
    return bool(spec.verified and spec.simulator_path and spec.profile.family == "think_ahead"
                and sample.get("observation") and base.enabled("think_ahead"))


def solve_variant(spec, deadline, plan=None):
    """Solve and store the roots near the sample position. Returns {"solved": n, "tried": n, ...}. Never raises."""
    t0 = time.monotonic()
    info = {"solved": 0, "tried": 0, "positions": 0, "stored": 0, "seconds": 0.0}
    try:
        if not eligible(spec) or is_done(spec):
            return info
        left = deadline - time.monotonic()
        if left < 5.0:
            return info
        plan = plan or resources.plan("prep_solve")
        facts = base.simulator_facts(spec)
        sample = spec.sample_turn
        legal = sample.get("legal_actions")
        payload = {"observation": str(sample.get("observation") or ""),
                   "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None,
                   "variant": {"label": spec.label}, "budget_s": round(left - 2.0, 2), "root_s": ROOT_S,
                   "max_plies": MAX_PLIES, "tt_entries": plan.tt_entries, "margin": 0.05,
                   "unknown": facts["unknown_outcomes"], "both_seats": facts["both_seats"], "seat": 0}
        seats = (0, 1) if facts["both_seats"] else (0,)        # a position is keyed with state["me"]: solve both views
        jobs = max(1, min(plan.children, sandbox.capacity()) // len(seats))     # all tasks start at once: one wave
        tasks = []
        for as_seat in seats:
            for part in range(jobs):
                pl = dict(payload, as_seat=as_seat)
                if jobs > 1:
                    pl["part"], pl["parts"] = part, jobs
                tasks.append(pl)

        def one(pl):
            return sandbox.run_job(spec.simulator_path, JOB_FILE, pl, left + 2.0, mem_mb=plan.child_mem_mb,
                                   cpu_s=int(left * 2 + 30), slot_wait_s=max(0.0, left - 2.0))

        found, answered = [], 0
        pool = ThreadPoolExecutor(max_workers=max(1, min(len(tasks), plan.threads)), thread_name_prefix="solve")
        try:
            futs = [pool.submit(one, pl) for pl in tasks]
            for f in futs:
                try:
                    out = f.result(timeout=max(1.0, deadline - time.monotonic() + 3.0))
                except Exception:
                    continue
                if not out.get("ok"):
                    log.info("solve %s/%s: job failed (%s): %s" % (spec.game_id, spec.label, out.get("kind"),
                                                                    str(out.get("error"))[:160]))
                    continue
                res = out.get("result") or {}
                if not res.get("ok") or res.get("live") is False:
                    log.info("solve %s/%s: %s" % (spec.game_id, spec.label, res.get("reason")))
                    continue
                answered += 1
                found.extend(res.get("roots") or [])
                info["tried"] += int(res.get("tried") or 0)
                info["positions"] = max(info["positions"], int(res.get("positions") or 0))
        finally:
            pool.shutdown(wait=False)
        info["solved"] = len(found)
        if found or answered == len(tasks):          # a pass whose jobs all answered is done, even with 0 roots
            info["stored"] = save_roots(spec, found, plan.cache_roots, marker(spec) if answered == len(tasks) else None)
    except Exception:
        log.exception("solve failed for %s/%s" % (spec.game_id, spec.label))
    info["seconds"] = round(time.monotonic() - t0, 1)
    return info


def save_roots(spec, items, limit, done_marker=None):
    """Add solved roots in ONE read-modify-write (another container may write the file in between: a lost
    update only loses cache entries). Returns how many entries the file holds afterwards."""
    sha = base.sim_sha(spec)
    if not sha:
        return 0
    d = memory.read_json(search.cache_path(spec), {})
    roots = d.get("roots") if isinstance(d, dict) and d.get("sim") == sha else []
    keep = [r for r in (roots or []) if isinstance(r, list) and len(r) == 2]
    new_keys = set(k for k, _ in items)
    keep = [r for r in keep if r[0] not in new_keys]
    keep.extend([k, v] for k, v in items if isinstance(v, dict))
    keep = keep[-max(search.CACHE_MAX_ROOTS, int(limit)):]
    out = {"sim": sha, "roots": keep}
    mk = done_marker or (d.get("presolved") if isinstance(d, dict) and d.get("sim") == sha else None)
    if mk:
        out["presolved"] = mk
    memory.write_json_atomic(search.cache_path(spec), out)
    return len(keep)


def solve_all(specs, deadline):
    """The pass of prep: every eligible variant gets a share of the time. Never raises."""
    try:
        if not resources.enabled():
            return {}                  # BOT_RESOURCES=off: prep exactly as before
        now = time.monotonic()
        todo = [s for s in specs if eligible(s)]
        left = deadline - now
        if not todo or left < SOLVE_MIN_S:
            return {}
        window = min(SOLVE_MAX_S, SOLVE_FRACTION * left)
        per = window / len(todo)
        out = {}
        for i, s in enumerate(todo):
            end = now + per * (i + 1)
            out[s.rules_hash] = solve_variant(s, end)
            log.info("solve %s/%s: %s" % (s.game_id, s.label, out[s.rules_hash]))
        return out
    except Exception:
        log.exception("solve_all failed")
        return {}
