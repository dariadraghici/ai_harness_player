"""Orchestrates prep under a time budget.

Owner: luckydevs-3. Status: real.

Phase A: for each variant (in parallel): rules hash -> cached specialist in
memory? load it. Otherwise one classify call (thinking on; prep spend does
not count for the cost score), checked by code, stored with the sample turn
and analysis.json. Then the variants of the game are aligned on one family
and re-saved where it changed. After phase A everything the bot needs to
play is in memory.

Then one CHAIN per variant that wants a simulator (classify.SIMULATOR_FAMILIES;
family unknown only when plenty of time is left), all chains in parallel:

  candidates   prep/codegen.py: several candidate modules, static check, one
               repair round. Skipped when the manifest of this harness version
               and contract already has a static-ok candidate. Generation ends
               at PHASE_B_END_FRACTION of the budget.
  verify       prep/verify.py: every candidate through the checks in a sandbox
               child, differential test, repair rounds with the failures,
               winner -> simulator.py + verify.json, spec.verified. Skipped
               when verify.json already names a winner that is on disk. Ends
               at VERIFY_END_FRACTION of the budget; at that moment every
               sandbox child still alive is killed.

A variant is verified as soon as ITS candidates are ready; nobody waits for
the slowest variant. With a short clock the chains do not start. Codegen and
verification run ONLY here, never on the cold start path.

main.py answers ready when run_prep returns, and at the hard deadline at the
latest, with whatever is finished by then. Early answers are fine.
"""

import time
from concurrent.futures import ThreadPoolExecutor, wait

from .. import config, log, memory, specialist
from . import classify, codegen, sandbox, verify

# proposed for bot/config.py
PHASE_B_END_FRACTION = 0.53      # generation ends here (509 s -> 270 s = 45% of 600 s)
VERIFY_END_FRACTION = 0.85       # verification and its repair rounds end here (509 s -> 433 s)
PHASE_B_MIN_S = 60.0             # generation starts only with this much time before its end
PHASE_B_UNKNOWN_MIN_S = 150.0    # family unknown gets candidates only with this much time


def run_prep(msg, deadline):
    """Prepare every variant of the prep message. Returns the list of specs done."""
    t_start = time.monotonic()
    game_id = str(msg.get("game_id") or "unknown")
    game_name = str(msg.get("game_name") or "")
    variants = [v for v in (msg.get("variants") or []) if isinstance(v, dict)]
    budget = max(0.0, deadline - t_start)
    log.info("prep %s (%s): %d variants, %.0fs budget" % (game_id, game_name, len(variants), budget))

    done = phase_a(game_id, game_name, variants, deadline)
    analyses = align(done)
    t_a = time.monotonic()
    log.info("prep %s: phase A %d/%d variants ready in %.1fs" % (
        game_id, len(done), len(variants), t_a - t_start))
    run_chains(done, analyses, t_start + PHASE_B_END_FRACTION * budget, t_start + VERIFY_END_FRACTION * budget)
    log.info("prep %s: simulators done in %.1fs: %d/%d variants verified" % (
        game_id, time.monotonic() - t_a, sum(1 for s in done if s.verified), len(done)))
    return done


def run_chains(specs, analyses, gen_end, verify_end):
    """Candidates -> verification, one chain per variant worth it, in parallel. Never raises.

    gen_end: generation of candidates is finished by then. verify_end: everything is finished by
    then, and every sandbox child still alive is killed."""
    try:
        left = gen_end - time.monotonic()
        jobs = []
        for s in specs:
            a = analyses.get(s.rules_hash) or {}
            wants = a.get("wants_simulator", s.profile.family in classify.SIMULATOR_FAMILIES)
            if not wants and not (s.profile.family == "unknown" and left >= PHASE_B_UNKNOWN_MIN_S):
                continue
            if verify.is_done(s):
                log.info("chain %s/%s: verified simulator from the cache" % (s.game_id, s.rules_hash))
                continue
            has = codegen.has_ok_candidates(s)
            if not has and left < PHASE_B_MIN_S:
                continue                 # no time to generate, nothing to verify
            if has and verify_end - time.monotonic() < verify.VERIFY_MIN_S:
                continue
            jobs.append((0 if wants else 1, s, has))
        if not jobs:
            if left < PHASE_B_MIN_S:
                log.info("simulator chains skipped: %.0fs left" % left)
            return
        jobs.sort(key=lambda j: j[0])

        def chain(s, has):
            a = analyses.get(s.rules_hash)
            if not has:
                codegen.generate_simulators(s, gen_end, codegen.CODEGEN_CANDIDATES, a)
            verify.verify_variant(s, verify_end, a)

        pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="chain")
        try:
            futs = [pool.submit(chain, s, has) for _, s, has in jobs]
            _, late = wait(futs, timeout=max(0.0, verify_end - time.monotonic()))
            if late:
                log.info("simulator chains: %d variant(s) not finished at the deadline" % len(late))
                sandbox.kill_all("prep verification deadline")
        finally:
            pool.shutdown(wait=False)
    except Exception:
        log.exception("simulator chains failed")
        sandbox.kill_all("simulator chains failed")


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
