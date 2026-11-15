"""The engine subsystem: discover -> install -> learn -> generate -> validate -> use.

Owner: luckydevs-3 area. Status: real. Standard library only in the bot process.

One stage per module, each with its own time budget and its own try/except, so a failure anywhere costs us the
subsystem and nothing else:

    capabilities.py  what kind of computation this game needs (closed tag vocabulary, no game names)
    net.py           the only network door: GET, allowlist, capability-vocabulary queries, no game content
    discover.py      catalog + model-proposed names, every one verified on the package index
    install.py       wheels only, flock, atomic publish in /memory, import probe
    docs.py          local introspection first, then registry/README, BM25-ranked chunks
    adapter_gen.py   Haiku writes adapter.py, static check with ast, repair rounds
    validate.py      TrustReport A/B/C/X measured in a sandbox child
    adapter_job.py   the only place where the adapter and the library ever run

`prepare_all` is called by bot/prep/pipeline.py inside the prep budget. `BOT_ENGINE_SUBSYSTEM=off` turns the
whole thing off and the harness behaves exactly as it did before.

The bot process never imports a third-party library: everything that touches one happens in a sandbox child or
in a short-lived subprocess.
"""

import os
import time

# NOTHING is imported from the harness or from the stages at package import time. The subsystem is only loaded
# when prep or a turn really needs it: importing it must not cost the bot process a single module (and a test
# runner that walks the package must not drag the harness in either).

REPORT_FILE = "report.json"
# Stage caps (TASK section 3). The window prep gives us is split by these shares and capped by these numbers.
DISCOVER_S = 30.0
INSTALL_S = 120.0
DOCS_S = 40.0
ADAPTER_S = 200.0
VALIDATE_S = 60.0
STAGE_TOTAL = DISCOVER_S + INSTALL_S + DOCS_S + ADAPTER_S + VALIDATE_S
MIN_WINDOW_S = 70.0            # below this there is no point in starting
# Families whose own engine is a stub or weak: a library helps most there, so they go first.
WEAK_FAMILIES = ("deals", "truth", "guardrails", "unknown", "bluff")


def _stages():
    """The stage modules, imported on first use."""
    from . import adapter_gen, capabilities, discover, docs, install, net, validate
    return adapter_gen, capabilities, discover, docs, install, net, validate


def enabled():
    """Is the engine subsystem on? Off with BOT_ENGINE_SUBSYSTEM=off, and off by default with the fake model:
    a canned answer cannot write an adapter, and a practice run should not spend prep on pip. BOT_ENGINE_
    SUBSYSTEM=on forces it on even with the fake model (that is how its own tests exercise it)."""
    raw = os.environ.get("BOT_ENGINE_SUBSYSTEM", "").strip().lower()
    if raw in ("0", "off", "false", "no", "none"):
        return False
    if raw in ("1", "on", "true", "yes"):
        return True
    from .. import config
    return not config.FAKE_LLM


def report_path(spec):
    from .. import memory
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), REPORT_FILE)


def is_done(spec):
    """True when this variant already has a trusted adapter of this harness version on disk."""
    from .. import config
    from . import adapter_gen, validate
    try:
        data = validate.load(spec)
        if not isinstance(data, dict) or data.get("harness_version") != config.HARNESS_VERSION:
            return False
        if data.get("level") not in ("A", "B", "C"):
            return bool(data.get("level"))        # a stored X is a decision too: do not redo it
        return os.path.isfile(adapter_gen.adapter_path(spec))
    except Exception:
        return False


def _order(specs, analyses):
    """Variants worth a library, best first: a weak family first, then a richer capability match."""
    from . import capabilities
    rows = []
    for s in specs:
        tags = capabilities.tags_for(s, (analyses or {}).get(s.rules_hash))
        if not tags:
            continue
        weak = 0 if (s.profile.family in WEAK_FAMILIES) else 1
        rows.append((weak, -len(tags), s, tags))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(s, tags) for _w, _n, s, tags in rows]


def prepare_all(specs, analyses, deadline, window_s=None):
    """Build a library engine for as many variants as the window allows. Returns {rules_hash: trust level}."""
    from .. import log
    from . import install
    out = {}
    if not enabled():
        log.info("libs: BOT_ENGINE_SUBSYSTEM=off, the engine subsystem is skipped")
        return out
    try:
        install.cleanup_tmp()
        left = deadline - time.monotonic()
        window = min(left, window_s if window_s is not None else max(0.0, left))
        if window < MIN_WINDOW_S:
            log.info("libs: skipped, only %.0fs left" % left)
            return out
        rows = _order(specs, analyses)
        if not rows:
            log.info("libs: no variant has a capability tag")
            return out
        log.info("libs: %.0fs window, %d variant(s) in order: %s" % (
            window, len(rows), ", ".join("%s[%s]" % (s.label[:14], ",".join(t[:2])) for s, t in rows[:4])))
        end = time.monotonic() + window
        for spec, tags in rows:
            if time.monotonic() > end - MIN_WINDOW_S * 0.5:
                log.info("libs: window spent, %s and the rest keep the standard engines" % spec.label[:16])
                break
            if is_done(spec):
                from . import validate
                data = validate.load(spec)
                out[spec.rules_hash] = data.get("level")
                log.info("libs %s: trust %s from /memory (%s)" % (spec.label[:16], data.get("level"),
                                                                  data.get("library")))
                continue
            out[spec.rules_hash] = prepare_one(spec, (analyses or {}).get(spec.rules_hash), tags, end)
        return out
    except Exception:
        log.exception("libs: prepare_all failed")
        return out


def _share(end, cap):
    """A stage gets its share of what is left, never more than its cap."""
    left = max(0.0, end - time.monotonic())
    return min(cap, left * (cap / STAGE_TOTAL) + cap * 0.25) if left > 0 else 0.0


def prepare_one(spec, analysis, tags, end):
    """One variant: discover, install, learn, generate, validate. Returns the trust level or None."""
    from .. import log, memory
    from . import adapter_gen, discover, docs, install, validate
    t0 = time.monotonic()
    report = {"variant": spec.label, "game_id": spec.game_id, "rules_hash": spec.rules_hash, "tags": list(tags),
              "stages": {}, "refused": [], "notes": []}
    try:
        # A. the profile is already there (prep phase A). It carries the capability tags from here on.
        if isinstance(analysis, dict) and analysis.get("capability_tags") != list(tags):
            analysis = dict(analysis or {})
            analysis["capability_tags"] = list(tags)

        # B. discovery
        t = time.monotonic()
        names = []
        libs = discover.candidates(tags, names, deadline=time.monotonic() + _share(end, DISCOVER_S))
        report["stages"]["discover_s"] = round(time.monotonic() - t, 1)
        report["candidates"] = [{"name": l.name, "version": l.version, "size_mb": l.size_mb, "source": l.source,
                                 "score": round(l.score, 1), "why": l.why} for l in libs]
        if not libs:
            report["notes"].append("no library passed discovery")
            _write(spec, report, None, t0)
            return None

        # C. install
        t = time.monotonic()
        path, ok_libs, notes = install.ensure(libs, time.monotonic() + _share(end, INSTALL_S))
        report["stages"]["install_s"] = round(time.monotonic() - t, 1)
        report["install"] = list(notes)
        if not ok_libs:
            report["notes"].append("nothing installed")
            _write(spec, report, None, t0)
            return None
        lib = ok_libs[0]
        second = ok_libs[1] if len(ok_libs) > 1 else None
        report["chosen"] = {"name": lib.name, "version": lib.version, "import": lib.import_name,
                            "why": lib.why, "source": lib.source, "lib_path": path or "(image)"}
        if second is not None:
            report["also_available"] = {"name": second.name, "import": second.import_name, "why": second.why}

        # D. docs
        t = time.monotonic()
        question = "how to compute %s with %s" % (", ".join(tags[:2]).replace("_", " "), lib.import_name)
        share = _share(end, DOCS_S)
        learned = docs.learn(lib, path, question, time.monotonic() + share * 0.7)
        extra = None
        if second is not None:
            learned2 = docs.learn(second, path, question, time.monotonic() + share * 0.3)
            if learned2.get("api"):
                extra = (second, learned2)
        report["stages"]["docs_s"] = round(time.monotonic() - t, 1)
        report["docs"] = {"source": learned.get("source") or "", "api_chars": len(learned.get("api") or ""),
                          "doc_chars": len(learned.get("docs") or "")}
        if not (learned.get("api") or learned.get("docs")):
            report["notes"].append("neither the API nor any documentation could be read")
            _write(spec, report, None, t0)
            return None

        # E. the adapter
        t = time.monotonic()
        samples = validate.samples_for(spec)
        check = validate.behaviour_check(spec, analysis, lib, path, end, samples) if samples else None
        code, rounds, failures = adapter_gen.generate(spec, analysis, tags, lib, learned,
                                                      time.monotonic() + _share(end, ADAPTER_S), check, extra)
        report["stages"]["adapter_s"] = round(time.monotonic() - t, 1)
        used = adapter_gen.imports_of(code or "")
        offered = [l.import_name for l in ([lib] + ([second] if second is not None else []))]
        report["adapter"] = {"rounds": rounds, "chars": len(code or ""), "failures": list(failures)[:6],
                             "imports": used,
                             "uses_library": adapter_gen.uses_library(code or "", offered)}
        if not code:
            report["notes"].append("the adapter could not be written: %s" % "; ".join(failures)[:200])
            _write(spec, report, None, t0)
            return None
        path_py = adapter_gen.adapter_path(spec)
        memory.write_text_atomic(path_py, code)

        # F. validation
        t = time.monotonic()
        stored = validate.validate(spec, analysis, lib, path, path_py,
                                   time.monotonic() + _share(end, VALIDATE_S), rounds,
                                   list(notes) + ["adapter imports: %s" % (", ".join(used) or "nothing")],
                                   uses_library=report["adapter"]["uses_library"])
        report["stages"]["validate_s"] = round(time.monotonic() - t, 1)
        report["trust"] = {"level": stored.get("level"), "reasons": stored.get("reasons"),
                           "p95_latency_s": stored.get("p95_latency_s"),
                           "peak_mb": (stored.get("measured") or {}).get("peak_mb")}
        _write(spec, report, stored.get("level"), t0)
        return stored.get("level")
    except Exception:
        log.exception("libs: prepare_one failed for %s" % spec.label[:20])
        report["notes"].append("the stage crashed")
        _write(spec, report, None, t0)
        return None


def _write(spec, report, level, t0):
    from .. import log, memory
    report["level"] = level
    report["seconds"] = round(time.monotonic() - t0, 1)
    try:
        memory.write_json_atomic(report_path(spec), report)
    except Exception:
        log.exception("libs: report not written")
    log.info("libs %s: %s in %.0fs (%s)" % (spec.label[:16], level or "no engine", report["seconds"],
                                            ", ".join(report.get("notes") or ()) or "ok"))
