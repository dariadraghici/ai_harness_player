"""Verification of generated simulators: which candidate, if any, may the engines trust?

Owner: luckydevs-3. Status: real.

Everything that touches generated code runs in a sandbox child
(prep/sandbox.py); the checks themselves are the job prep/verify_job.py. This
module is the parent side:

  verify_simulator(spec, path, deadline)   one candidate -> VerifyReport
  compare(reports)                         differential test between candidates
  verify_variant(spec, deadline)           the whole chain for one variant:
                                           verify all -> repair the failing ones
                                           (codegen.repair_simulator) -> verify
                                           again -> pick the winner -> files
  is_done(spec)                            a valid verify.json with its winner on disk
  check_turn(worker, turn_msg, ...)        one real turn against a simulator (for the
                                           live consistency check during a match)

Checks (names in verify.json): load, parse, legal, contract, selfplay, seed
(only when the game has outcomes we cannot know, contract C11), stored_turns
(only when real turns were recorded), agreement (differential test).
A candidate is VERIFIED when all of its own checks pass on every turn sample
and it is not in a minority against the other candidates.

Files, in <variant_dir> = memory.variant_dir(game_id, rules_hash):
  simulator.py   the verified winner (written ONLY for a verified winner)
  verify.json    the report: every candidate, the winner, playouts per second
Then spec.simulator_path and spec.verified are set and memory.save_variant
stores them (meta.json last).

Turn samples: the prep sample_turn (seat 0) plus, when it exists,
  <memory>/<game_id>/turns/<memory.safe_name(label)>.jsonl
one JSON object per line: {"seat": 0 or 1, "variant": {...}, "turn": {the turn
message, at least "observation" and "legal_actions"}}.

Nothing here raises, and nothing is started or written after the deadline.
"""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import config, log, memory
from . import classify, codegen, sandbox

# ---------------------------------------------------------------- constants
# proposed for bot/config.py
VERIFY_JOB_BUDGET_S = 10.0       # what the checks of one candidate may use inside the child
VERIFY_JOB_EXTRA_S = 4.0         # on top of the budget, before the child is killed
VERIFY_MIN_S = 4.0               # do not start a verification with less time left
VERIFY_GAMES = 30                # random games per candidate
VERIFY_STEP_CAP = 400            # a random game must end within this many steps
VERIFY_TRACE_GAMES = 6           # games compared between candidates
VERIFY_TRACE_STEPS = 60
VERIFY_MAX_CANDIDATES = 4
VERIFY_MAX_REPAIR_ROUNDS = 3     # repair rounds per variant
VERIFY_REPAIR_MIN_S = 30.0       # start a repair round only with this much time left
VERIFY_REPAIR_CALL_S = 120.0     # one repair call may take this long
VERIFY_REGEN_MIN_S = 150.0       # fresh candidates only with this much time left
VERIFY_FAILURES_TO_MODEL = 8     # failures sent to the model per repair
STORED_TURNS_MAX = 40            # stored real turns used per variant

JOB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_job.py")
SIMULATOR_FILE = "simulator.py"
VERIFY_FILE = "verify.json"
OWN_CHECKS = ("load", "parse", "legal", "contract", "selfplay")
LIMITS = [
    "one prep sample turn (seat 0) per variant: a module that hard-codes that deal or only parses "
    "that text can pass; stored real turns and the differential test reduce the risk",
    "legal action order and spelling are checked only where a turn sample lists the legal actions",
    "the differential test compares game trajectories only in games without unknown outcomes",
    "an illegal action that does not raise ValueError (C10) is a warning, not a failure",
    "in chance and hidden-information games a module may play the opponent's turn itself inside "
    "apply_action (winner.both_seats is then false): an engine then searches a one-player game",
]


@dataclass
class VerifyReport:
    passed: bool = False                                  # verified: own checks and agreement
    checks: List[str] = field(default_factory=list)       # names of checks that passed
    failures: List[str] = field(default_factory=list)     # one sentence each, fed back to codegen
    seconds: float = 0.0
    failed_checks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    own_passed: bool = False                              # every own check passed (before agreement)
    agreement: str = "n/a"                                # majority, minority, split, alone, n/a
    exact_order: bool = False                             # legal actions in the arena's order
    both_seats: bool = True                               # alternating game: both seats get the move
    legal_complete: Optional[bool] = None
    legal_count: int = 0
    games: int = 0
    steps_per_game: float = 0.0
    playouts_per_s: float = 0.0
    samples: int = 0
    error: str = ""                                       # sandbox-level problem, "" when the job ran
    views: List[Any] = field(default_factory=list, repr=False)
    traces: List[Any] = field(default_factory=list, repr=False)

    def to_dict(self):
        return {"verified": self.passed, "own_passed": self.own_passed, "agreement": self.agreement,
                "checks": list(self.checks), "failed_checks": list(self.failed_checks),
                "failures": list(self.failures), "warnings": list(self.warnings),
                "exact_order": self.exact_order, "both_seats": self.both_seats,
                "legal_complete": self.legal_complete, "legal_count": self.legal_count, "games": self.games, "steps_per_game": self.steps_per_game,
                "playouts_per_s": self.playouts_per_s, "samples": self.samples, "error": self.error,
                "seconds": self.seconds}


# ---------------------------------------------------------------- small helpers
def simulator_file(spec):
    """Where the verified simulator of this variant lives (the storage key is spec.rules_hash)."""
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), SIMULATOR_FILE)


def verify_file(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), VERIFY_FILE)


def _sha(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def _declared(text):
    t = str(text or "").strip().lower()
    return bool(t) and t not in ("no", "none", "n/a", "-", "false") and not t.startswith(("none", "no ", "no.", "no,"))


def has_unknown_outcomes(spec, analysis=None):
    """True when applying an action can need a value we cannot know: chance, or hidden values in a
    game that is not simultaneous (there the two actions are given explicitly, contract C5)."""
    a = analysis or {}
    p = spec.profile
    return _declared(a.get("chance")) or (p.perfect_information is False and p.simultaneous is not True)


def stored_turns_file(spec):
    return os.path.join(memory.root(), memory.safe_name(spec.game_id), "turns",
                        memory.safe_name(spec.label) + ".jsonl")


def turn_samples(spec):
    """The prep sample turn first, then stored real turns (spread over the file, capped)."""
    out = []
    st = spec.sample_turn if isinstance(spec.sample_turn, dict) else None
    if st and st.get("observation"):
        out.append({"observation": str(st["observation"]), "legal_actions": st.get("legal_actions")
                    if isinstance(st.get("legal_actions"), list) else None,
                    "seat": 0, "variant": {"label": spec.label}, "kind": "prep"})
    try:
        path = stored_turns_file(spec)
        if os.path.isfile(path):
            rows = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        turn = row.get("turn") if isinstance(row, dict) else None
                        if isinstance(turn, dict) and turn.get("observation") and row.get("seat") in (0, 1):
                            rows.append({"observation": str(turn["observation"]),
                                         "legal_actions": turn.get("legal_actions")
                                         if isinstance(turn.get("legal_actions"), list) else None,
                                         "seat": row["seat"],
                                         "variant": row.get("variant") if isinstance(row.get("variant"), dict)
                                         else {"label": spec.label}, "kind": "stored"})
                    except ValueError:
                        continue
            if len(rows) > STORED_TURNS_MAX:
                step = len(rows) / float(STORED_TURNS_MAX)
                rows = [rows[int(i * step)] for i in range(STORED_TURNS_MAX)]
            out.extend(rows)
    except Exception:
        log.exception("reading stored turns failed")
    return out


# ---------------------------------------------------------------- one candidate
def verify_simulator(spec, path, deadline, analysis=None, samples=None):
    """Run every check on the simulator at path inside a sandbox child. Never raises."""
    t0 = time.monotonic()
    rep = VerifyReport()
    try:
        if samples is None:
            samples = turn_samples(spec)
        rep.samples = len(samples)
        if not samples:
            rep.error = "no-sample"
            rep.failures = ["no turn sample: nothing to verify against"]
            rep.failed_checks = ["parse"]
            return rep
        left = deadline - time.monotonic()
        if left < VERIFY_MIN_S:
            rep.error = "no-time"
            rep.failures = ["not verified: no time left"]
            return rep
        if analysis is None:
            analysis = classify.load_analysis(spec.game_id, spec.rules_hash) or {}
        seeded = has_unknown_outcomes(spec, analysis)
        budget = max(1.0, min(VERIFY_JOB_BUDGET_S, left - VERIFY_JOB_EXTRA_S))
        payload = {"mode": "verify", "samples": samples, "simultaneous": spec.profile.simultaneous,
                   "seeded": seeded, "action_regex": analysis.get("action_regex") or "",
                   "example_action": spec.profile.example_action or "", "games": VERIFY_GAMES,
                   "step_cap": VERIFY_STEP_CAP, "budget_s": budget, "trace_games": VERIFY_TRACE_GAMES,
                   "trace_steps": VERIFY_TRACE_STEPS}
        out = sandbox.run_job(path, JOB_FILE, payload, budget + VERIFY_JOB_EXTRA_S,
                              cpu_s=int(budget * 3 + 20),
                              slot_wait_s=max(0.0, left - budget - VERIFY_JOB_EXTRA_S))
        if not out.get("ok"):
            rep.error = out.get("kind") or "error"
            msg = str(out.get("error") or "")
            if rep.error == "timeout":
                rep.failures = ["the module did not finish the checks within %.0f s: an endless loop, or code "
                                "far too slow for search" % (budget + VERIFY_JOB_EXTRA_S)]
            elif "cannot load the simulator" in msg:
                rep.failures = ["the module does not load: %s" % msg[:300]]
                rep.failed_checks = ["load"]
            else:
                rep.failures = ["the process that ran the module died (memory limit of %d MB, or a crash): %s" % (
                    sandbox.CHILD_MEMORY_MB, msg[:300])]
            rep.failed_checks = rep.failed_checks or ["load"]
            return rep
        r = out.get("result") or {}
        checks = r.get("checks") or {}
        wanted = list(OWN_CHECKS) + (["seed"] if seeded else []) + (["stored_turns"] if "stored_turns" in checks else [])
        rep.checks = [c for c in wanted if checks.get(c) is True]
        rep.failed_checks = [c for c in wanted if checks.get(c) is not True]
        rep.failures = [str(f) for f in (r.get("failures") or [])]
        rep.warnings = [str(w) for w in (r.get("warnings") or [])]
        rep.own_passed = not rep.failed_checks
        if rep.failed_checks and not rep.failures:
            rep.failures = ["the checks %s did not pass" % ", ".join(rep.failed_checks)]
        rep.passed = rep.own_passed
        rep.exact_order = bool(r.get("exact_order"))
        rep.both_seats = bool(r.get("both_seats", True))
        rep.legal_complete = r.get("legal_complete")
        rep.legal_count = int(r.get("legal_count") or 0)
        rep.games = int(r.get("games") or 0)
        rep.steps_per_game = float(r.get("steps_per_game") or 0.0)
        rep.playouts_per_s = float(r.get("playouts_per_s") or 0.0)
        rep.views = r.get("views") or []
        rep.traces = r.get("traces") or []
        return rep
    except Exception:
        log.exception("verify_simulator failed for %s" % path)
        rep.error = rep.error or "internal"
        rep.failures = rep.failures or ["internal verification error"]
        rep.passed = rep.own_passed = False
        return rep
    finally:
        rep.seconds = round(time.monotonic() - t0, 2)


# ---------------------------------------------------------------- differential test
def _show_legal(d):
    if isinstance(d, list) and d and isinstance(d[0], str) and d[0].startswith("#"):
        return "%s actions (first: %s)" % (d[0][1:], json.dumps(d[2:]))
    return json.dumps(d)


def _dump(v):
    return json.dumps(v, sort_keys=True)


def _difference(mine, theirs, n_others, where):
    """One sentence for the minority module."""
    head = "differential test: %s " % where
    tail = "; %d other independently written implementation%s of the same rules" % (
        n_others, "" if n_others == 1 else "s")
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return head + "you give %s%s give %s" % (json.dumps(mine)[:200], tail, json.dumps(theirs)[:200])
    if mine.get("terminal") != theirs.get("terminal") and "terminal" in theirs:
        return head + "your is_terminal is %s%s say %s" % (mine.get("terminal"), tail, theirs.get("terminal"))
    if mine.get("cp") != theirs.get("cp"):
        return head + "your current_player is %s%s say %s" % (mine.get("cp"), tail, theirs.get("cp"))
    if mine.get("legal") != theirs.get("legal"):
        return head + "your legal_actions lists %s%s list %s" % (
            _show_legal(mine.get("legal"))[:300], tail, _show_legal(theirs.get("legal"))[:300])
    if mine.get("returns") != theirs.get("returns"):
        return head + "your returns is %s%s give %s" % (mine.get("returns"), tail, theirs.get("returns"))
    return head + "you give %s%s give %s" % (json.dumps(mine)[:200], tail, json.dumps(theirs)[:200])


def _vote(values):
    """values: key -> comparable string. Returns (majority string or None, split bool)."""
    groups = {}
    for k, v in values.items():
        groups.setdefault(v, []).append(k)
    if len(groups) == 1:
        return next(iter(groups)), False
    best = max(groups.values(), key=len)
    if len(best) * 2 > len(values):
        return values[best[0]], False
    return None, True


def _step(rep, g, k):
    """Trace value of game g at step k: the step record, the end record after it, or None."""
    traces = rep.traces or []
    if g >= len(traces):
        return None
    steps = traces[g].get("steps") or []
    if k < len(steps):
        st = steps[k]
        return {"cp": st.get("cp"), "legal": st.get("legal"), "terminal": st.get("terminal")}
    if k == len(steps):
        return {"end": True, "ended": traces[g].get("ended"), "returns": traces[g].get("returns"),
                "error": bool(traces[g].get("error"))}
    return None


def _history(rep, g, k):
    steps = ((rep.traces or [])[g].get("steps") or [])[:k]
    return [s.get("act") for s in steps if "act" in s]


def _first_difference(rep, ref, n_others):
    """First point where rep differs from the reference report: a sentence, or None."""
    for i in range(max(len(rep.views or []), len(ref.views or []))):
        a = rep.views[i] if i < len(rep.views) else None
        b = ref.views[i] if i < len(ref.views) else None
        if a is not None and b is not None and _dump(a) != _dump(b):
            where = "on the sample state" if i == 0 else "on stored real turn %d" % i
            return _difference(a, b, n_others, where)
    for g in range(min(len(rep.traces or []), len(ref.traces or []))):
        k = 0
        while True:
            a, b = _step(rep, g, k), _step(ref, g, k)
            if a is None or b is None:
                break
            if _dump(a) != _dump(b):
                hist = _history(ref, g, k)
                where = "after the actions %s from the sample state" % json.dumps(hist[-10:]) if hist \
                    else "on the sample state"
                if a.get("end") or b.get("end"):
                    where = "at the end of the game " + where if a.get("end") and b.get("end") else where
                    if a.get("end") != b.get("end"):
                        return ("differential test: %s your game %s; %d other independently written "
                                "implementation%s of the same rules say it %s" % (
                                    where, "is over" if a.get("end") else "continues", n_others,
                                    "" if n_others == 1 else "s", "is over" if b.get("end") else "continues"))
                return _difference(a, b, n_others, where)
            if a.get("end"):
                break
            k += 1
    return None


def compare(reports):
    """Differential test. reports: key -> VerifyReport (changed in place). Returns a summary dict.

    Voters are the candidates whose own checks pass. Where they disagree and a strict majority
    exists, the minority fails the check "agreement" with a sentence that says where. Candidates
    that failed their own checks get the first difference to the majority as one more hint.
    """
    summary = {"voters": 0, "minority": [], "split": False}
    try:
        voters = [k for k, r in reports.items() if r.own_passed]
        summary["voters"] = len(voters)
        for r in reports.values():
            r.agreement = "n/a"
        if len(voters) < 2:
            for k in voters:
                reports[k].agreement = "alone"
            return summary
        # Pairwise equality classes over the whole comparable record.
        sig = {k: _dump([reports[k].views, [[_step(reports[k], g, i) for i in range(len(t.get("steps") or []) + 1)]
                                            for g, t in enumerate(reports[k].traces or [])]]) for k in voters}
        majority, _ = _vote(sig)
        if majority is None:
            # No strict majority (1 against 1, or all different): nobody is punished.
            for k in voters:
                reports[k].agreement = "split"
            summary["split"] = True
            return summary
        good = [k for k in voters if sig[k] == majority]
        ref = reports[good[0]]
        for k in voters:
            if k in good:
                reports[k].agreement = "majority"
                if "agreement" not in reports[k].checks:
                    reports[k].checks.append("agreement")
                continue
            r = reports[k]
            r.agreement = "minority"
            r.passed = False
            r.failed_checks.append("agreement")
            r.failures.append(_first_difference(r, ref, len(good)) or
                              "differential test: this module behaves differently from %d other independently "
                              "written implementations of the same rules" % len(good))
            summary["minority"].append(k)
        for k, r in reports.items():
            if k not in voters and (r.views or r.traces):
                hint = _first_difference(r, ref, len(good))
                if hint and len(r.failures) < VERIFY_FAILURES_TO_MODEL:
                    r.failures.append(hint)
        return summary
    except Exception:
        log.exception("differential test failed")
        return summary


# ---------------------------------------------------------------- the chain for one variant
class _Cand:
    def __init__(self, n, path, entry):
        self.n = n
        self.path = path
        self.file = os.path.basename(path)
        self.brief = entry.get("brief")
        self.repaired = bool(entry.get("repaired"))
        self.rounds = 0
        self.report = None
        self.prev = None         # (code, report, repaired) before the last repair


def _own_score(rep, repaired):
    return (bool(rep.own_passed), len([c for c in rep.checks if c != "agreement"]), bool(rep.both_seats),
            bool(rep.exact_order), not repaired, float(rep.playouts_per_s))


def score(rep, repaired):
    """Higher is better: verified, more checks passed, both seats get the move, exact legal order,
    not repaired, faster playouts."""
    return (bool(rep.passed),) + _own_score(rep, repaired)[1:]


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def is_done(spec):
    """True when verify.json of this harness version and contract names a winner that is on disk.

    Sets spec.simulator_path and spec.verified accordingly (a stale claim in meta.json is undone)."""
    try:
        v = memory.read_json(verify_file(spec))
        path = simulator_file(spec)
        if (isinstance(v, dict) and v.get("harness_version") == config.HARNESS_VERSION
                and v.get("contract") == codegen.contract_version() and v.get("verified")
                and isinstance(v.get("winner"), dict)):
            code = _read(path)
            if code is not None and _sha(code) == v["winner"].get("sha256"):
                spec.simulator_path, spec.verified = path, True
                return True
        if spec.verified or spec.simulator_path:
            spec.simulator_path, spec.verified = None, False
        return False
    except Exception:
        log.exception("is_done failed")
        return False


def _load_candidates(spec):
    m = codegen.read_manifest(spec) or {}
    by_file = {c.get("file"): c for c in m.get("candidates", []) if isinstance(c, dict)}
    cands = []
    for path in codegen.ok_paths(spec)[:VERIFY_MAX_CANDIDATES]:
        entry = by_file.get(os.path.basename(path)) or {}
        cands.append(_Cand(entry.get("n", len(cands) + 1), path, entry))
    return cands


def _verify_all(spec, cands, deadline, analysis, samples, tag, round_no):
    todo = [c for c in cands if c.report is None]
    if todo:
        pool = ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="verify")
        try:
            futs = {pool.submit(verify_simulator, spec, c.path, deadline, analysis, samples): c for c in todo}
            wait(list(futs), timeout=max(0.0, deadline - time.monotonic()) + 1.0)
            for f, c in futs.items():
                c.report = f.result() if f.done() else VerifyReport(error="no-time", failures=["not verified: no time left"])
        finally:
            pool.shutdown(wait=False)
    # A repair that made things worse is undone.
    for c in cands:
        if c.prev is not None and c.report is not None:
            old_code, old_report, old_repaired = c.prev
            if _own_score(c.report, True) < _own_score(old_report, old_repaired) and time.monotonic() <= deadline:
                memory.write_text_atomic(c.path, old_code)
                codegen._update_manifest(spec, [{"n": c.n, "sha256": _sha(old_code), "chars": len(old_code),
                                                 "repaired": old_repaired, "rolled_back": True}])
                log.info("verify %s cand %d: the repair scored lower, previous version restored" % (tag, c.n))
                c.report, c.repaired = old_report, old_repaired
            c.prev = None
    reports = {c.n: c.report for c in cands}
    summary = compare(reports)
    for c in cands:
        r = c.report
        log.info("verify %s cand %d round %d: %s agreement=%s checks=%s failed=%s pps=%.0f %.1fs%s" % (
            tag, c.n, round_no, "VERIFIED" if r.passed else "failed", r.agreement, ",".join(r.checks),
            ",".join(r.failed_checks) or "-", r.playouts_per_s, r.seconds,
            "" if r.passed else " | " + "; ".join(r.failures)[:300]))
    return summary


def _repair_all(spec, failing, deadline, analysis, tag):
    """One repair call per failing candidate, in parallel. Marks the changed ones for a new verification."""
    call_deadline = min(deadline, time.monotonic() + VERIFY_REPAIR_CALL_S)

    def one(c):
        old_code = _read(c.path)
        if old_code is None:
            return
        failures = (c.report.failures or ["the module failed verification"])[:VERIFY_FAILURES_TO_MODEL]
        c.rounds += 1
        if codegen.repair_simulator(spec, c.path, failures, call_deadline, analysis) and time.monotonic() <= deadline:
            c.prev = (old_code, c.report, c.repaired)
            c.report, c.repaired = None, True
    pool = ThreadPoolExecutor(max_workers=len(failing), thread_name_prefix="verify-repair")
    try:
        wait([pool.submit(one, c) for c in failing], timeout=max(0.0, call_deadline - time.monotonic()) + 2.0)
    finally:
        pool.shutdown(wait=False)


def verify_variant(spec, deadline, analysis=None):
    """Verify, repair and pick the winner for one variant. Never raises. Returns a summary dict."""
    t0 = time.monotonic()
    tag = "%s/%s" % (spec.game_id, spec.rules_hash)
    out = {"status": "none", "verified": False, "winner": None, "rounds": 0, "seconds": 0.0}
    try:
        if is_done(spec):
            out.update(status="cached", verified=True)
            return out
        if deadline - time.monotonic() < VERIFY_MIN_S:
            out["status"] = "no-time"
            return out
        cands = _load_candidates(spec)
        if not cands:
            out["status"] = "no-candidates"
            return out
        if analysis is None:
            analysis = classify.load_analysis(spec.game_id, spec.rules_hash) or {}
        samples = turn_samples(spec)
        if not samples:
            _write(spec, cands, None, analysis, samples, t0, 0, deadline, "no turn sample: nothing can be verified")
            out["status"] = "no-sample"
            return out

        regenerated = False
        rounds = 0
        while True:
            summary = _verify_all(spec, cands, deadline, analysis, samples, tag, rounds)
            n_ver = sum(1 for c in cands if c.report.passed)
            left = deadline - time.monotonic()
            if n_ver >= 2 or (n_ver >= 1 and rounds >= 1):
                break
            failing = [c for c in cands if not c.report.passed and c.report.error != "no-time"]
            if not failing:
                break
            if rounds < VERIFY_MAX_REPAIR_ROUNDS and left >= VERIFY_REPAIR_MIN_S:
                _repair_all(spec, failing, deadline, analysis, tag)
                rounds += 1
                if any(c.report is None for c in cands):
                    continue
                if n_ver >= 1:
                    break            # nothing could be repaired; keep the verified one
            if n_ver == 0 and not regenerated and deadline - time.monotonic() >= VERIFY_REGEN_MIN_S:
                # Every candidate is still failing: fresh candidates beat one more repair.
                regenerated = True
                log.info("verify %s: no candidate verified after %d repair round(s); generating fresh ones" % (
                    tag, rounds))
                gen_end = time.monotonic() + min(0.5 * (deadline - time.monotonic()), 160.0)
                codegen.generate_simulators(spec, gen_end, codegen.CODEGEN_CANDIDATES, analysis)
                fresh = _load_candidates(spec)
                if fresh:
                    cands = fresh
                    rounds = max(rounds, VERIFY_MAX_REPAIR_ROUNDS - 1)     # at most one more repair round
                    continue
            break

        verified = [c for c in cands if c.report.passed]
        best = max(cands, key=lambda c: score(c.report, c.repaired))
        winner = max(verified, key=lambda c: score(c.report, c.repaired)) if verified else None
        ok = _write(spec, cands, winner, analysis, samples, t0, rounds, deadline,
                    "" if winner else "no candidate passed; best was sim_%d" % best.n, summary)
        out.update(status="verified" if (winner and ok) else "failed", verified=bool(winner and ok),
                   winner=winner.file if winner else None, rounds=rounds)
        w = winner or best
        log.info("verify summary %s (%s) family=%s verified=%s winner=%s candidates=%d verified_candidates=%d "
                 "rounds=%d pps=%.0f steps=%.0f in %.1fs" % (
                     tag, spec.label, spec.profile.family, out["verified"], out["winner"], len(cands),
                     len(verified), rounds, w.report.playouts_per_s, w.report.steps_per_game,
                     time.monotonic() - t0))
        return out
    except Exception:
        log.exception("verify_variant failed for %s" % tag)
        out["status"] = "error"
        return out
    finally:
        out["seconds"] = round(time.monotonic() - t0, 2)


def _write(spec, cands, winner, analysis, samples, t0, rounds, deadline, note="", summary=None):
    """simulator.py (verified winner only), verify.json, the spec fields, meta.json. False when too late."""
    if time.monotonic() > deadline:
        log.info("verify %s/%s finished after the deadline; nothing written" % (spec.game_id, spec.rules_hash))
        return False
    doc = {"harness_version": config.HARNESS_VERSION, "contract": codegen.contract_version(),
           "game_id": spec.game_id, "label": spec.label, "rules_hash": spec.rules_hash,
           "family": spec.profile.family, "verified": False, "winner": None, "note": note,
           "unknown_outcomes": has_unknown_outcomes(spec, analysis),
           "samples": {"prep": sum(1 for s in samples if s.get("kind") == "prep"),
                       "stored": sum(1 for s in samples if s.get("kind") == "stored")},
           "repair_rounds": rounds, "agreement": summary or {}, "limits": LIMITS,
           "seconds": round(time.monotonic() - t0, 2), "updated": int(time.time()), "candidates": []}
    for c in cands:
        entry = {"n": c.n, "file": c.file, "brief": c.brief, "repaired": c.repaired, "repair_rounds": c.rounds}
        if c.report is not None:
            entry.update(c.report.to_dict())
        doc["candidates"].append(entry)
    path = None
    if winner is not None:
        code = _read(winner.path)
        if code is None:
            winner = None
        else:
            path = simulator_file(spec)
            memory.write_text_atomic(path, code)
            r = winner.report
            doc["verified"] = True
            doc["winner"] = {"n": winner.n, "file": winner.file, "sha256": _sha(code), "repaired": winner.repaired,
                             "playouts_per_s": r.playouts_per_s, "steps_per_game": r.steps_per_game,
                             "exact_order": r.exact_order, "both_seats": r.both_seats,
                             "legal_complete": r.legal_complete, "legal_count": r.legal_count,
                             "agreement": r.agreement, "warnings": list(r.warnings)}
    memory.write_json_atomic(verify_file(spec), doc)
    spec.simulator_path = path if winner is not None else None
    spec.verified = winner is not None
    memory.save_variant(spec)
    return True


# ---------------------------------------------------------------- one turn (live check, for later)
def check_turn(worker, turn_msg, variant, seat, timeout=1.0):
    """Does the simulator in this sandbox Worker parse this real turn and list the arena's legal actions?

    Returns (ok, reason). Never raises. Meant for the live consistency check during a match:
    one call per turn on the worker the engine already holds; on False drop to L2 for the game."""
    try:
        res = worker.job(JOB_FILE, {"mode": "turn", "observation": turn_msg.get("observation", ""),
                                    "legal_actions": turn_msg.get("legal_actions"), "seat": seat,
                                    "variant": variant if isinstance(variant, dict) else {}}, timeout)
        return bool(res.get("ok")), str(res.get("reason") or "")
    except sandbox.SandboxError as e:
        return False, "sandbox: %s" % e
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
