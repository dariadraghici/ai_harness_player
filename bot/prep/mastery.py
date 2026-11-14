"""Budget-aware self-play and strategy distillation for prepared variants."""

import os
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config, log, memory
from . import sandbox

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mastery_job.py")
STRATEGY_FILE = "strategy.json"
SAFETY_MARGIN_S = 7.0
ROUND_S = 18.0
MIN_ROUND_S = 2.5
MAX_PLAYBOOK_LINES = 12
MAX_PLAYBOOK_CHARS = 900


def safe_deadline(deadline, margin=SAFETY_MARGIN_S):
    return max(0.0, float(deadline) - max(0.0, float(margin)))


def _empty_summary():
    return {
        "rounds": 0, "games": 0, "wins": 0, "losses": 0, "draws": 0,
        "loops": 0, "illegal": 0, "errors": 0, "repeated_actions": 0,
        "steps": 0, "elapsed_s": 0.0, "tactical_wins": 0, "tactical_blocks": 0,
    }


def _merge(total, result):
    summary = result.get("summary") if isinstance(result, dict) else None
    if not isinstance(summary, dict):
        total["errors"] += 1
        return
    total["rounds"] += 1
    for key in ("games", "wins", "losses", "draws", "loops", "illegal", "errors",
                "repeated_actions", "steps", "elapsed_s", "tactical_wins",
                "tactical_blocks"):
        total[key] += summary.get(key, 0) or 0


def _playbook(spec, summary, verified):
    lines = []
    if spec.playbook:
        lines.extend(" ".join(line.split()) for line in spec.playbook.splitlines() if line.strip())
    if verified:
        lines.append("PREP SELF-TEST: %d games, %d steps; prefer verified engine candidates." %
                     (summary["games"], summary["steps"]))
        lines.append("ROBUSTNESS: %d illegal, %d simulator errors, %d loops, %d repeated actions." %
                     (summary["illegal"], summary["errors"], summary["loops"],
                      summary["repeated_actions"]))
        lines.append("TACTICS: %d immediate wins and %d single-move blocks found in the verified sample state." %
                     (summary.get("tactical_wins", 0), summary.get("tactical_blocks", 0)))
        if summary["games"]:
            lines.append("SELF-PLAY: %d wins, %d losses, %d draws; keep play diverse when values are close." %
                         (summary["wins"], summary["losses"], summary["draws"]))
    else:
        lines.append("SIMULATOR UNAVAILABLE: use the exact rules and action format; validate every move.")
        lines.append("EDGE CASES: treat variant settings and the current observation as authoritative.")
    lines.append("Do not treat sample-deal values or quoted examples as fixed facts of this game.")
    out = []
    size = 0
    for line in lines:
        line = line[:MAX_PLAYBOOK_CHARS]
        extra = len(line) + (1 if out else 0)
        if len(out) >= MAX_PLAYBOOK_LINES or size + extra > MAX_PLAYBOOK_CHARS:
            break
        out.append(line)
        size += extra
    return "\n".join(out)


def _persist(spec, summary, verified):
    try:
        spec.playbook = _playbook(spec, summary, verified)
        d = memory.variant_dir(spec.game_id, spec.rules_hash)
        memory.write_json_atomic(os.path.join(d, STRATEGY_FILE), {
            "harness_version": config.HARNESS_VERSION,
            "family": spec.profile.family,
            "verified": bool(verified),
            "summary": summary,
        })
        memory.save_variant(spec)
    except Exception:
        log.exception("strategy persistence failed for %s/%s", spec.game_id, spec.label)


def _run_one(spec, budget, seed):
    sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
    observation = sample.get("observation")
    if not observation or not spec.simulator_path:
        return {"ok": False, "reason": "no simulator or sample observation"}
    payload = {
        "observation": observation,
        "variant": {"label": spec.label},
        "budget_s": round(budget, 3),
        "seed": seed,
    }
    return sandbox.run_job(spec.simulator_path, JOB_FILE, payload,
                           max(1.0, budget + 2.0), cpu_s=120)


def refine(specs, analyses, deadline):
    """Use remaining prep time for local self-play and persist safe strategy facts."""
    end = safe_deadline(deadline)
    eligible = [s for s in specs if s.verified and s.simulator_path
                and isinstance(s.sample_turn, dict) and s.sample_turn.get("observation")]
    totals = {id(s): _empty_summary() for s in specs}
    if not eligible:
        for spec in specs:
            _persist(spec, totals[id(spec)], False)
        return totals
    while time.monotonic() + MIN_ROUND_S < end:
        left = end - time.monotonic()
        budget = min(ROUND_S, left - 1.0)
        if budget < MIN_ROUND_S:
            break
        workers = min(2, len(eligible))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mastery") as pool:
            futures = [pool.submit(_run_one, spec, budget, index + totals[id(spec)]["rounds"])
                       for index, spec in enumerate(eligible)]
            for spec, future in zip(eligible, futures):
                try:
                    _merge(totals[id(spec)], future.result())
                except Exception:
                    log.exception("mastery self-play failed for %s/%s", spec.game_id, spec.label)
                    totals[id(spec)]["errors"] += 1
        if time.monotonic() >= end:
            break
    for spec in specs:
        _persist(spec, totals[id(spec)], bool(spec.verified and spec.simulator_path))
    return totals
