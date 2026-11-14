"""Orchestrates prep under a time budget.

Owner: luckydevs-3. Status: real.

Phase A: for each variant (in parallel): rules hash -> cached specialist in
memory? load it. Otherwise one classify call (thinking on; prep spend does
not count for the cost score), checked by code, stored with the sample turn
and analysis.json. Then the variants of the game are aligned on one family
and re-saved where it changed. After phase A everything the bot needs to
play is in memory.

Phase B: for the variants that want a simulator (classify.SIMULATOR_FAMILIES;
family unknown only when plenty of time is left), generate candidate
simulators in parallel (prep/codegen.py), with their one repair round. A
variant whose manifest already has a static-ok candidate of this harness
version is skipped. Phase B is finished at PHASE_B_END_FRACTION of the given
budget (about 45 percent of a 600 s prep clock), leaving the rest for the
next stages (verification). With a short clock it does not start.
Codegen runs ONLY here, never on the cold start path.

main.py answers ready when run_prep returns, and at the hard deadline at the
latest, with whatever is finished by then. Early answers are fine.
"""

import time
from concurrent.futures import ThreadPoolExecutor, wait

from .. import config, log, memory, specialist
from . import classify, codegen

# proposed for bot/config.py
PHASE_B_END_FRACTION = 0.53      # of the budget given to run_prep (509 s -> 270 s = 45% of 600 s)
PHASE_B_MIN_S = 60.0             # phase B starts only with this much time before its end
PHASE_B_UNKNOWN_MIN_S = 150.0    # family unknown gets candidates only with this much time


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
    t_a = time.monotonic()
    log.info("prep %s: phase A %d/%d variants ready in %.1fs" % (
        game_id, len(done), len(variants), t_a - t_start))
    phase_b(done, analyses, t_start + PHASE_B_END_FRACTION * max(0.0, deadline - t_start))
    log.info("prep %s: phase B done in %.1fs" % (game_id, time.monotonic() - t_a))
    # ---- EXTENSION POINT luckydevs-3: simulator verification (prep/verify.py in
    # prep/sandbox.py) and engine selection, before deadline; set spec.verified
    # and spec.simulator_path only for a verified winner.
    return done


def phase_b(specs, analyses, end):
    """Generate simulator candidates for the variants worth it, until end. Never raises."""
    try:
        left = end - time.monotonic()
        if left < PHASE_B_MIN_S:
            log.info("phase B skipped: %.0fs left" % left)
            return
        jobs = []
        for s in specs:
            a = analyses.get(s.rules_hash) or {}
            wants = a.get("wants_simulator", s.profile.family in classify.SIMULATOR_FAMILIES)
            if not wants and not (s.profile.family == "unknown" and left >= PHASE_B_UNKNOWN_MIN_S):
                continue
            if codegen.has_ok_candidates(s):
                log.info("phase B: %s/%s already has static-ok candidates" % (s.game_id, s.rules_hash))
                continue
            jobs.append((0 if wants else 1, s))
        if not jobs:
            return
        jobs.sort(key=lambda j: j[0])
        pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="phaseb")
        try:
            futs = [pool.submit(codegen.generate_simulators, s, end, codegen.CODEGEN_CANDIDATES,
                                analyses.get(s.rules_hash)) for _, s in jobs]
            wait(futs, timeout=max(0.0, end - time.monotonic()))
        finally:
            pool.shutdown(wait=False)
    except Exception:
        log.exception("phase B failed")


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
