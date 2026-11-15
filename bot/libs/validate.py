"""How far the adapter can be trusted (TASK section 3F): the TrustReport.

Owner: luckydevs-3 area. Status: real. Standard library only.

The measurements are taken inside a sandbox child (bot/libs/adapter_job.py, mode "validate"); this file only
judges them, so the rule that decides a trust level is one readable function:

  A  every sample parsed, every suggestion playable, and the best action agreed with an EXACT alpha-beta search
     of our own verified simulator on at least MIN_EXACT_POSITIONS positions, inside the latency budget.
  B  parses and answers legally and fast, and agrees with the exact search in most positions (or there was no
     verified simulator to compare with).
  C  answers legally often enough to be used as evidence in the model prompt, nothing more.
  X  rejected: it crashes, cannot parse, proposes illegal actions or overruns its deadline.

The level decides what the runtime does with the output (bot/engines/library.py and the ladder), so the cost of
being wrong here is a lost game: every threshold below is deliberately conservative.
"""

import os
import time

from .. import config, log, memory, resources
from ..prep import sandbox, verify

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter_job.py")
TRUST_FILE = "trust.json"
LEVELS = ("A", "B", "C", "X")

MIN_EXACT_POSITIONS = 2        # exact-search comparisons needed for level A
AGREE_B = 0.6                  # agreement with the exact search needed for level B
LEGAL_C = 0.9                  # share of samples that must answer legally for level C
VALIDATE_TOTAL_S = 55.0        # the whole validation pass
SUGGEST_BUDGET_S = 3.0         # what one suggest() may use while validating
EXACT_BUDGET_S = 1.5           # per position, for the cross-check search
CHILD_CPU_S = 180


def trust_path(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), TRUST_FILE)


def load(spec):
    data = memory.read_json(trust_path(spec), {})
    return data if isinstance(data, dict) else {}


def p95(values):
    rows = sorted(float(v) for v in values or ())
    if not rows:
        return 0.0
    i = max(0, min(len(rows) - 1, int(round(0.95 * (len(rows) - 1)))))
    return round(rows[i], 4)


def judge(report, budget_s=SUGGEST_BUDGET_S):
    """(level, reasons) from the raw measurements of adapter_job. Pure: the tests call it directly."""
    reasons = []
    if not isinstance(report, dict) or not report.get("ok", True) or report.get("reason"):
        return "X", [str((report or {}).get("reason") or "no report")]
    samples = int(report.get("samples") or 0)
    if samples <= 0:
        return "X", ["no sample observation to validate against"]
    parsed = int(report.get("parsed") or 0)
    answered = int(report.get("suggested") or 0)
    crashes = int(report.get("crashes") or 0)
    problems = int(report.get("problem_rows") or 0)
    overruns = int(report.get("overruns") or 0)
    compared = int(report.get("compared") or 0)
    agree = int(report.get("agree") or 0)
    latency = p95(report.get("latencies"))
    if crashes:
        return "X", ["%d crash(es) while suggesting" % crashes]
    if parsed < samples:
        return "X", ["parsed %d of %d sample observations" % (parsed, samples)]
    if answered == 0:
        return "X", ["never produced a usable candidate"]
    if overruns:
        reasons.append("%d call(s) overran the %.1fs budget" % (overruns, budget_s))
    if latency > budget_s * 1.5:
        return "X", ["p95 latency %.2fs is over the %.1fs budget" % (latency, budget_s)]
    legal_share = answered / float(samples)
    if problems and problems > answered:
        return "X", ["proposed more unplayable actions (%d) than usable answers (%d)" % (problems, answered)]
    if legal_share < LEGAL_C:
        return "X", ["only %d of %d samples answered" % (answered, samples)]
    ratio = (agree / float(compared)) if compared else None
    if overruns:
        return "C", reasons + ["advisory only: the deadline was not respected"]
    if problems:
        return "C", reasons + ["%d unplayable action(s) were proposed" % problems]
    if compared >= MIN_EXACT_POSITIONS and ratio is not None and ratio >= 0.999:
        return "A", ["agreed with the exact search in %d of %d positions" % (agree, compared),
                     "p95 latency %.2fs" % latency]
    if ratio is None:
        # Either prep built no simulator for this variant, or an exact search of it says nothing about this
        # position (hidden information, a chance node, a tree too large to solve). Level A needs proof, so
        # without the cross-check the ceiling is B.
        return "B", reasons + ["legal and fast, but no exact cross-check was possible (no verified simulator, "
                               "or its search cannot prove this position)", "p95 latency %.2fs" % latency]
    if ratio >= AGREE_B:
        return "B", reasons + ["agreed with the exact search in %d of %d positions" % (agree, compared),
                               "p95 latency %.2fs" % latency]
    return "C", reasons + ["agreed with the exact search in only %d of %d positions" % (agree, compared)]


def samples_for(spec):
    """The observations we validate against: the prep sample plus the real turns prep/verify stored."""
    try:
        rows = verify.turn_samples(spec) or []
    except Exception:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        obs = row.get("observation")
        if not obs:
            continue
        legal = row.get("legal_actions")
        out.append({"observation": str(obs),
                    "legal_actions": [str(a) for a in legal] if isinstance(legal, list) and legal else None})
    return out[:6]


def payload_for(spec, analysis, lib, lib_path, adapter_path, mode="validate", samples=None, total_s=None):
    profile = spec.profile
    return {"mode": mode, "adapter_path": adapter_path, "lib_path": lib_path or "",
            "variant": {"label": spec.label}, "seat": getattr(spec, "seat", None),
            "action_regex": (analysis or {}).get("action_regex") or "",
            "profile": {"family": profile.family, "action_format": profile.action_format,
                        "example_action": profile.example_action, "simultaneous": profile.simultaneous,
                        "perfect_information": profile.perfect_information,
                        "speech_restrictions": profile.speech_restrictions,
                        "action_regex": (analysis or {}).get("action_regex") or "",
                        "capability_tags": list((analysis or {}).get("capability_tags") or ())},
            "samples": samples if samples is not None else [], "reps": 3,
            "budget_s": SUGGEST_BUDGET_S, "exact_budget_s": EXACT_BUDGET_S if spec.verified else 0.0,
            "total_s": total_s or VALIDATE_TOTAL_S}


def measure(spec, analysis, lib, lib_path, adapter_path, deadline, samples=None):
    """Run the validation job in a sandbox child. Returns the raw report (never raises)."""
    samples = samples if samples is not None else samples_for(spec)
    if not samples:
        return {"ok": False, "reason": "no sample observation to validate against"}
    left = max(0.0, deadline - time.monotonic())
    if left < 6.0:
        return {"ok": False, "reason": "no time left for validation"}
    total = min(VALIDATE_TOTAL_S, left - 3.0)
    payload = payload_for(spec, analysis, lib, lib_path, adapter_path, "validate", samples, total)
    sim_path = verify.simulator_file(spec) if spec.verified else None
    if sim_path and not os.path.isfile(sim_path):
        sim_path = None
    plan = resources.plan("prep_verify")
    out = sandbox.run_job(sim_path, JOB_FILE, payload, total + 4.0, mem_mb=plan.child_mem_mb, cpu_s=CHILD_CPU_S,
                          slot_wait_s=max(0.0, left - total - 4.0))
    if not out.get("ok"):
        return {"ok": False, "reason": "the validation child failed (%s): %s" % (
            out.get("kind"), str(out.get("error"))[:200])}
    report = out.get("result")
    return report if isinstance(report, dict) else {"ok": False, "reason": "the child sent no report"}


def behaviour_check(spec, analysis, lib, lib_path, deadline, samples=None):
    """A `check(code)` for bot/libs/adapter_gen.generate: writes the candidate next to the variant and runs the
    cheap part of the validation, so a module that parses but does not work is repaired instead of trusted."""
    samples = samples if samples is not None else samples_for(spec)

    def check(code):
        try:
            path = os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "adapter_candidate.py")
            memory.write_text_atomic(path, code)
            report = measure(spec, analysis, lib, lib_path, path, min(deadline, time.monotonic() + 30.0),
                             samples[:2])
            if not isinstance(report, dict) or report.get("reason") and not report.get("samples"):
                return [str(report.get("reason") or "validation did not run")]
            fails = []
            if report.get("crashes"):
                fails.extend(str(p) for p in (report.get("problems") or ())[:4])
                fails = fails or ["suggest raised an exception"]
            if int(report.get("parsed") or 0) < int(report.get("samples") or 0):
                fails.append("parse did not return a state for every sample observation (A3)")
            if not int(report.get("suggested") or 0):
                fails.append("suggest returned no usable candidate for any sample (A4)")
            for p in (report.get("problems") or ())[:4]:
                if "not in legal_actions" in str(p) or "does not match the action format" in str(p):
                    fails.append(str(p))
            if report.get("overruns"):
                fails.append("suggest used more than its deadline_s (A5)")
            return fails[:8]
        except Exception:
            log.exception("libs/validate: behaviour check failed")
            return []
    return check


def validate(spec, analysis, lib, lib_path, adapter_path, deadline, rounds=0, notes=(), uses_library=None):
    """Measure, judge, store trust.json. Returns the stored report."""
    report = measure(spec, analysis, lib, lib_path, adapter_path, deadline)
    level, reasons = judge(report)
    stored = {
        "harness_version": config.HARNESS_VERSION,
        "level": level, "reasons": reasons, "library": lib.name, "version": lib.version,
        "import": lib.import_name, "lib_path": lib_path or "", "tags": list(lib.tags), "why": lib.why,
        "source": lib.source, "rounds": rounds, "notes": list(notes), "uses_library": uses_library,
        "measured": {k: v for k, v in (report or {}).items() if k != "problems"},
        "problems": list((report or {}).get("problems") or ())[:10],
        "p95_latency_s": p95((report or {}).get("latencies")), "when": int(time.time()),
    }
    memory.write_json_atomic(trust_path(spec), stored)
    log.info("libs/validate %s/%s: trust %s via %s%s (%s)" % (
        spec.game_id, spec.label[:20], level, lib.name,
        "" if uses_library is None else (" [used]" if uses_library else " [the adapter uses the standard library]"),
        "; ".join(reasons)[:200]))
    return stored
