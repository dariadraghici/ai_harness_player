"""Orchestrates prep under a time budget.

Owner: luckydevs-3. Status: real (base: classify + playbook per variant).

For each variant: rules hash -> cached specialist in memory? load it.
Otherwise one model call (thinking enabled; prep spend does not count for the
cost score) that returns family, playbook, action format and an example
action; store it with the sample_turn. Variants run concurrently, a few at a
time. main.py answers ready when run_prep returns, and at the hard deadline
at the latest, with whatever is finished by then.
"""

import time
from concurrent.futures import ThreadPoolExecutor, wait

from .. import config, log, specialist


def run_prep(msg, deadline):
    """Prepare every variant of the prep message. Returns the list of specs done."""
    game_id = str(msg.get("game_id") or "unknown")
    game_name = str(msg.get("game_name") or "")
    variants = [v for v in (msg.get("variants") or []) if isinstance(v, dict)]
    log.info("prep %s (%s): %d variants, %.0fs budget" % (
        game_id, game_name, len(variants), deadline - time.monotonic()))

    def one(v):
        rules = v.get("rules") or ""
        cached = specialist.lookup(game_id, rules)
        if cached:
            if v.get("sample_turn") and not cached.sample_turn:
                cached.sample_turn = v.get("sample_turn")
            return cached
        return specialist.build_variant(game_id, game_name, v.get("label", ""), rules,
                                        v.get("sample_turn"), deadline - 5.0)

    # ---- EXTENSION POINT luckydevs-3: after the base profile of every variant
    # exists, spend the remaining prep time on simulator generation and
    # verification (best variants first), always stopping before deadline.
    done = []
    pool = ThreadPoolExecutor(max_workers=max(1, config.PREP_PARALLEL_VARIANTS),
                              thread_name_prefix="prep")
    try:
        futures = [pool.submit(one, v) for v in variants]
        finished, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        for f in finished:
            try:
                done.append(f.result())
            except Exception:
                log.exception("prep variant failed")
    finally:
        pool.shutdown(wait=False)
    log.info("prep %s: %d/%d variants ready" % (game_id, len(done), len(variants)))
    return done
