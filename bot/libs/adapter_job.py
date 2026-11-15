"""The adapter as a sandbox JOB: the only place where a generated adapter and a pip-installed library run.

Owner: luckydevs-3 area. Status: real.

Never imported by the bot (bot/engines/library.py and bot/libs/validate.py send it to a sandbox child through
bot/prep/sandbox.py). Standalone: standard library only, plus the modules it loads BY PATH
(bot/engines/sim_common.py, bot/engines/search_job.py) and the generated adapter.

The child runs `python -I -S` (isolated, no `site`), so the library import path is built here and nowhere else:
`site.getsitepackages()` for what the image has baked in, plus the bundle directory that bot/libs/install.py
published. The child already has no model key, no stdin, a memory cap and a CPU cap, and its stdout goes to
/dev/null, so a library that prints cannot touch our protocol.

Modes:
  suggest   parse the real observation, ask the adapter for candidates, check their shape, return them.
  validate  the TrustReport measurements: parse every sample, check every suggestion against the arena's legal
            actions or the action regex, measure latency and peak memory, and - when prep built a verified
            simulator - compare the adapter's best action with an exact alpha-beta search of that simulator.
"""

import json
import os
import re
import site
import sys
import time
import types

MAX_ROWS = 12
LATENCY_REPS = 3


def _prepare_path(lib_path):
    for d in site.getsitepackages():
        if d not in sys.path:
            sys.path.append(d)
    if lib_path and lib_path not in sys.path:
        sys.path.insert(0, lib_path)


def _load_by_path(path, name):
    mod = types.ModuleType(name)
    mod.__file__ = path
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    exec(compile(source, path, "exec"), mod.__dict__)
    return mod


def _engines_dir():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # .../bot
    return os.path.join(here, "engines")


def _peak_mb():
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        return 0.0


def _err(exc):
    text = str(exc)
    return "%s: %s" % (type(exc).__name__, text[:200]) if text else type(exc).__name__


# ---------------------------------------------------------------- candidate shape
def clean_rows(rows, legal, regex):
    """(kept, problems): candidate dicts that are well formed and whose action is playable."""
    kept, problems = [], []
    if not isinstance(rows, list):
        return [], ["suggest did not return a list (A4)"]
    seen = set()
    for i, row in enumerate(rows[:MAX_ROWS * 2]):
        if not isinstance(row, dict):
            problems.append("candidate %d is not a dict (A4)" % i)
            continue
        action = row.get("action")
        if not isinstance(action, str) or not action.strip():
            problems.append("candidate %d has no action string (A4)" % i)
            continue
        action = " ".join(action.split())
        if legal:
            match = next((a for a in legal if " ".join(str(a).split()).casefold() == action.casefold()), None)
            if match is None:
                problems.append("action %r is not in legal_actions (A4)" % action[:40])
                continue
            action = match
        elif regex:
            try:
                if not re.fullmatch(regex, action):
                    problems.append("action %r does not match the action format (A4)" % action[:40])
                    continue
            except re.error:
                pass
        if action.casefold() in seen:
            continue
        seen.add(action.casefold())
        value = row.get("value")
        try:
            value = None if value is None else max(-1.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            problems.append("candidate %d has a non-numeric value (A4)" % i)
            value = None
        try:
            conf = max(0.0, min(1.0, float(row.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        meta = dict((str(k)[:24], str(v)[:60]) for k, v in list(meta.items())[:6])
        kept.append({"action": action, "value": value, "confidence": conf, "exact": bool(row.get("exact")),
                     "meta": meta})
        if len(kept) >= MAX_ROWS:
            break
    return kept, problems


def _adapter(payload):
    _prepare_path(payload.get("lib_path") or "")
    mod = _load_by_path(payload["adapter_path"], "generated_adapter")
    klass = getattr(mod, "Adapter", None)
    if klass is None:
        raise RuntimeError("the module has no class Adapter")
    obj = klass()
    obj.setup(payload.get("profile") or {}, payload.get("variant") or {})
    return obj


# ---------------------------------------------------------------- modes
def _suggest(adapter, payload, observation, legal, budget):
    t0 = time.perf_counter()
    state = adapter.parse(observation, legal)
    t1 = time.perf_counter()
    rows = adapter.suggest(state, float(budget))
    t2 = time.perf_counter()
    kept, problems = clean_rows(rows, legal, payload.get("action_regex") or "")
    return {"actions": kept, "problems": problems, "parse_s": round(t1 - t0, 4),
            "suggest_s": round(t2 - t1, 4), "state_ok": state is not None}


def mode_suggest(sim, payload):
    adapter = _adapter(payload)
    for event in payload.get("events") or ():
        try:
            adapter.update(event)
        except Exception as e:
            return {"ok": False, "reason": "update raised %s" % _err(e)}
    legal = payload.get("legal_actions") or None
    out = _suggest(adapter, payload, str(payload.get("observation") or ""), legal,
                   float(payload.get("budget_s") or 2.0))
    out.update(ok=bool(out["actions"]), reason="" if out["actions"] else "no usable candidate",
               peak_mb=_peak_mb(), live=True)
    return out


def _exact_values(sim, payload, observation, legal, budget):
    """{action: value} from an exact alpha-beta search of OUR verified simulator, or None."""
    if sim is None:
        return None
    try:
        common = _load_by_path(os.path.join(_engines_dir(), "sim_common.py"), "sim_common")
        search = _load_by_path(os.path.join(_engines_dir(), "search_job.py"), "search_job")
    except Exception:
        return None
    try:
        probe = {"observation": observation, "legal_actions": legal, "variant": payload.get("variant") or {},
                 "seat": payload.get("seat") if payload.get("seat") in (0, 1) else 0, "margin": 0.0,
                 "budget_s": budget}
        g, state, reason = common.resolve(sim, probe)
        if g is None:
            return None
        la = g.legal(state)
        if len(la) < 2:
            return None
        out = search.alpha_beta(g, state, list(la), common.Clock(budget), {"margin": 0.0, "known": None,
                                                                          "leaf_k": 0})
        rows = (out or {}).get("actions") or []
        if not rows:
            return None
        # Only values the search PROVED (no bound) may judge the adapter. Two cases are safe to compare:
        # the whole root was solved, or a proven win exists - then playing anything else is a mistake.
        exact = dict((a["action"], float(a["value"])) for a in rows if a.get("bound") is None)
        if not exact:
            return None
        if out.get("solved") and len(exact) == len(rows):
            return exact
        if max(exact.values()) >= 0.999:
            return exact
        return None
    except Exception:
        return None


def mode_validate(sim, payload):
    """The measurements of the TrustReport. One dict, all numbers, no judgement (bot/libs/validate.py judges)."""
    report = {"ok": True, "live": True, "samples": 0, "parsed": 0, "suggested": 0, "legal_rows": 0,
              "problem_rows": 0, "problems": [], "latencies": [], "agree": 0, "compared": 0, "exact_rows": 0,
              "peak_mb": 0.0, "crashes": 0, "empty": 0, "overruns": 0}
    try:
        adapter = _adapter(payload)
    except Exception as e:
        return {"ok": False, "live": True, "reason": "setup failed: %s" % _err(e), "peak_mb": _peak_mb()}
    budget = float(payload.get("budget_s") or 2.0)
    exact_budget = float(payload.get("exact_budget_s") or 0.0)
    samples = [s for s in (payload.get("samples") or ()) if isinstance(s, dict) and s.get("observation")]
    clock_end = time.perf_counter() + float(payload.get("total_s") or 20.0)
    for i, sample in enumerate(samples):
        if time.perf_counter() > clock_end:
            break
        observation = str(sample.get("observation") or "")
        legal = sample.get("legal_actions") or None
        report["samples"] += 1
        reps = int(payload.get("reps") or LATENCY_REPS) if i == 0 else 1
        rows, problems = [], []
        for _ in range(max(1, reps)):
            try:
                out = _suggest(adapter, payload, observation, legal, budget)
            except Exception as e:
                report["crashes"] += 1
                report["problems"].append("sample %d raised %s" % (i, _err(e)))
                break
            report["latencies"].append(out["parse_s"] + out["suggest_s"])
            if out["suggest_s"] > budget * 1.5:
                report["overruns"] += 1
            rows, problems = out["actions"], out["problems"]
            if out["state_ok"]:
                report["parsed"] += 1 if _first(report, i) else 0
        if problems:
            report["problem_rows"] += len(problems)
            report["problems"].extend(problems[:3])
        if not rows:
            report["empty"] += 1
            continue
        report["suggested"] += 1
        report["legal_rows"] += len(rows)
        report["exact_rows"] += sum(1 for r in rows if r["exact"])
        if exact_budget > 0.05 and time.perf_counter() < clock_end:
            truth = _exact_values(sim, payload, observation, legal, exact_budget)
            if truth:
                report["compared"] += 1
                best = max(truth.values())
                if truth.get(rows[0]["action"], best - 1.0) >= best - 1e-9:
                    report["agree"] += 1
                else:
                    report["problems"].append(
                        "sample %d: the adapter plays %r (exact %.2f) but %.2f is available" % (
                            i, rows[0]["action"][:24], truth.get(rows[0]["action"], float("nan")), best))
    report["peak_mb"] = _peak_mb()
    report["problems"] = report["problems"][:10]
    return report


def _first(report, i):
    """Count a parse once per sample, not once per repetition."""
    key = "_parsed_%d" % i
    if report.get(key):
        return False
    report[key] = True
    return True


def run(sim, payload):
    mode = str(payload.get("mode") or "suggest")
    try:
        if mode == "validate":
            out = mode_validate(sim, payload)
        else:
            out = mode_suggest(sim, payload)
        return dict((k, v) for k, v in out.items() if not str(k).startswith("_"))
    except Exception as e:
        return {"ok": False, "live": True, "reason": _err(e), "peak_mb": _peak_mb()}
