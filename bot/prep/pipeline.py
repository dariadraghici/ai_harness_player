"""Orchestrates prep under a time budget.

Owner: luckydevs-3. Status: real.

Phase A: for each variant (in parallel): rules hash -> cached specialist in
memory? load it. Otherwise one classify call (thinking on; prep spend does
not count for the cost score), checked by code, stored with the sample turn
and analysis.json. Then the variants of the game are aligned on one family
and re-saved where it changed. After phase A everything the bot needs to
play is in memory.

main.py answers ready when run_prep returns, and at the hard deadline at the
latest, with whatever is finished by then. Early answers are fine.
"""

import time
from concurrent.futures import ThreadPoolExecutor, wait

from .. import config, log, memory, specialist
from . import classify


def run_prep(msg, deadline):
    """Prepare every variant of the prep message. Returns the list of specs done."""
    t_start = time.monotonic()
    game_id = str(msg.get("game_id") or "unknown")
    game_name = str(msg.get("game_name") or "")
    variants = [v for v in (msg.get("variants") or []) if isinstance(v, dict)]
    log.info("prep %s (%s): %d variants, %.0fs budget" % (
        game_id, game_name, len(variants), deadline - t_start))

    done = phase_a(game_id, game_name, variants, deadline)
    analyses = align(done)
    log.info("prep %s: phase A %d/%d variants ready in %.1fs" % (
        game_id, len(done), len(variants), time.monotonic() - t_start))
    # ---- EXTENSION POINT luckydevs-3: simulator verification and engine selection.
    return done


def phase_a(game_id, game_name, variants, deadline):
    def one(v):
        rules = v.get("rules") or ""
        cached = specialist.lookup(game_id, rules)
        if cached:
            if v.get("sample_turn") and not cached.sample_turn:
                cached.sample_turn = v.get("sample_turn")
            return cached
        return specialist.build_variant(game_id, game_name, v.get("label", ""), rules,
                                        v.get("sample_turn"), deadline - 5.0)

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
    return done


def align(specs):
    """Align the family of all variants; re-save the changed ones. Returns rules_hash -> analysis."""
    analyses = {}
    try:
        for s in specs:
            a = classify.load_analysis(s.game_id, s.rules_hash)
            if a is not None:
                analyses[s.rules_hash] = a
        for s in classify.align_families(specs, analyses):
            classify.write_analysis(s.game_id, s.rules_hash, analyses.get(s.rules_hash))
            memory.save_variant(s)
    except Exception:
        log.exception("align failed")
    return analyses
