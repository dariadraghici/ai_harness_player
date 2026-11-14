"""Simulator generation by the model.

Owner: luckydevs-3. Status: real (generation, static check, one repair round).
The candidates are then verified in the sandbox (prep/verify.py), which also
calls repair_simulator() with the failures of its checks.

During prep, for a variant worth it, ask the model for several DIFFERENT
candidate simulator modules at the same time (one working brief per
candidate, bot/prep/prompts/codegen_briefs.txt), all following ONE contract
(bot/prep/prompts/codegen_contract.txt). Keep the ones that pass a static
check, repair the others once, and leave the files and a manifest in
/memory:

  <variant_dir>/candidates/sim_<n>.py      every candidate, good or bad
  <variant_dir>/candidates/index.json      the manifest (read by verify.py); it
                                           carries the harness version and the
                                           hash of the contract text, and a
                                           manifest of another one counts as
                                           "no candidates"

HARD RULE: generated code is NEVER imported, exec-ed, eval-ed or run by the
bot, not even in a child process. Only prep/sandbox.py may run it. Here it is
inspected as text with ast; the static check is hygiene and an early repair
signal, NOT a security boundary.

Nothing here raises: a failure means no candidate.
"""

import ast
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .. import config, log, memory
from . import classify, templates

# ---------------------------------------------------------------- constants
# proposed for bot/config.py
CODEGEN_CANDIDATES = 3
# Thinking overshoots its budget and counts toward max_tokens: measured up to
# 12000 output tokens for 2000-6000 characters of code with budgets 3000-4000.
CODEGEN_THINKING_BUDGETS = (2000, 1500, 2500)   # one per brief, cycled
CODEGEN_REPAIR_THINKING = 2000                  # None after a cut-off answer
CODEGEN_MAX_TOKENS = 16000
CODEGEN_CALL_CAP_S = 150.0
CODEGEN_MIN_CALL_S = 20.0          # do not start a codegen call with less time left
CODEGEN_GEN_SHARE = 0.65           # share of the given budget for generation; the rest is repair
CODEGEN_REPAIR_MIN_S = 25.0        # start a repair call only with this much time left
CODEGEN_MAX_CODE_CHARS = 60000
CODEGEN_MAX_FAILURES = 20

ALLOWED_IMPORTS = ("math", "random", "itertools", "collections", "re", "json", "copy", "functools",
                   "heapq", "string", "fractions", "typing", "operator", "bisect")
FORBIDDEN_NAMES = ("print", "input", "eval", "exec", "open", "__import__", "compile", "breakpoint",
                   "exit", "quit", "globals", "locals", "vars", "help", "memoryview", "setattr",
                   "delattr", "getattr")
_ALLOWED_DUNDERS = ("__name__", "__init__", "__doc__")
# Builtins a top-level constant expression may call.
_PURE_BUILTINS = ("range", "list", "tuple", "dict", "set", "frozenset", "sorted", "len", "str", "int",
                  "float", "bool", "enumerate", "zip", "min", "max", "sum", "abs", "any", "all",
                  "reversed", "map", "filter", "round", "chr", "ord")
REQUIRED_FUNCTIONS = {"parse_observation": 3, "legal_actions": 1, "apply_action": 2,
                      "is_terminal": 1, "returns": 1, "current_player": 1}

MANIFEST = "index.json"
CUT_OFF_NOTE = ("the answer was cut off before the closing code fence: it was too long. Write a shorter "
                "module (under 200 lines) and spend less time thinking")
_manifest_lock = threading.Lock()


# ---------------------------------------------------------------- code extraction
def _fenced_blocks(text):
    """[(language, body, cut_off)] of the ``` fenced blocks, parsed line by line."""
    blocks, cur, lang = [], None, ""
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("```"):
            if cur is None:
                lang, cur = s[3:].strip().lower(), []
            else:
                blocks.append((lang, "\n".join(cur), False))
                cur = None
            continue
        if cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append((lang, "\n".join(cur), True))
    return blocks


def extract_code(text):
    """Return (code or None, notes). The largest fenced python block, else the whole text."""
    notes = []
    if not isinstance(text, str) or not text.strip():
        return None, ["the model returned nothing"]
    blocks = [(len(b), b, cut) for lang, b, cut in _fenced_blocks(text)
              if lang in ("", "python", "py", "python3") and "def " in b]
    if blocks:
        _, body, cut = max(blocks, key=lambda b: b[0])
        if cut:
            notes.append(CUT_OFF_NOTE)
        code = body
    else:
        code = text
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError):
            return None, ["no python code block in the answer"]
        if not any(isinstance(n, ast.FunctionDef) for n in tree.body):
            return None, ["no python code block in the answer"]
    if len(code) > CODEGEN_MAX_CODE_CHARS:
        notes.append("code longer than %d characters, cut" % CODEGEN_MAX_CODE_CHARS)
        code = code[:CODEGEN_MAX_CODE_CHARS]
    return code.strip("\n") + "\n", notes


def strip_main_block(code):
    """Remove a top-level `if __name__ == "__main__":` block (text only). Returns (code, removed)."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return code, False
    lines = code.split("\n")
    spans = []
    for node in tree.body:
        if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
            spans.append((node.lineno, node.end_lineno))
    if not spans:
        return code, False
    for a, b in reversed(spans):
        del lines[a - 1:b]
    return "\n".join(lines), True


# ---------------------------------------------------------------- static check
def _const_expr_ok(node, imported):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name) and (f.id in _PURE_BUILTINS or f.id in imported):
                continue
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Constant):
                continue
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in imported:
                continue
            return False
        if isinstance(sub, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
            return False
    return True


def static_check(code):
    """Text-only checks with ast. Returns a list of human-readable failures ([] = pass)."""
    if not isinstance(code, str) or not code.strip():
        return ["empty module"]
    fails = []
    if len(code) > CODEGEN_MAX_CODE_CHARS:
        fails.append("module longer than %d characters" % CODEGEN_MAX_CODE_CHARS)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return ["syntax error at line %s: %s%s" % (e.lineno, e.msg,
                                                   " (%s)" % e.text.strip()[:80] if e.text else "")]
    except (ValueError, RecursionError) as e:
        return ["cannot parse the module: %s" % e]

    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)

    defs = {}
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef):
            defs[node.name] = node
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue     # docstring or a bare string
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names_ok = all(isinstance(t, ast.Name) or (isinstance(t, ast.Tuple) and
                           all(isinstance(e, ast.Name) for e in t.elts)) for t in targets)
            if not names_ok:
                fails.append("line %d: top-level assignment must bind plain names" % node.lineno)
            elif node.value is not None and not _const_expr_ok(node.value, imported):
                fails.append("line %d: top-level assignment calls code at import time "
                             "(only constants are allowed at the top level)" % node.lineno)
            continue
        kind = type(node).__name__
        fails.append("line %d: top-level %s is not allowed (only a docstring, imports, constants "
                     "and function definitions)" % (node.lineno, kind))

    for name, n in REQUIRED_FUNCTIONS.items():
        fn = defs.get(name)
        if fn is None:
            fails.append("missing top-level function %s (%d parameter%s)" % (name, n, "s" if n > 1 else ""))
            continue
        a = fn.args
        count = len(a.posonlyargs) + len(a.args)
        required_kwonly = [k for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
        if count != n or a.vararg or a.kwarg or required_kwonly:
            fails.append("function %s has %d positional parameter%s, expected exactly %d" % (
                name, count, "" if count == 1 else "s", n))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    fails.append("line %d: import of %r is not allowed (allowed: %s)" % (
                        node.lineno, a.name, ", ".join(ALLOWED_IMPORTS)))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in ALLOWED_IMPORTS:
                fails.append("line %d: import from %r is not allowed (allowed: %s)" % (
                    node.lineno, "." * node.level + (node.module or ""), ", ".join(ALLOWED_IMPORTS)))
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                fails.append("line %d: use of %r is not allowed" % (node.lineno, node.id))
            elif node.id.startswith("__") and node.id.endswith("__") and node.id not in _ALLOWED_DUNDERS:
                fails.append("line %d: use of %r is not allowed" % (node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in _ALLOWED_DUNDERS:
                fails.append("line %d: attribute %r is not allowed" % (node.lineno, node.attr))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            fails.append("line %d: %s statement is not allowed (no global state)" % (
                node.lineno, type(node).__name__.lower()))
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            fails.append("line %d: async code is not allowed" % node.lineno)

    out = []
    for f in fails:
        if f not in out:
            out.append(f)
    return out[:CODEGEN_MAX_FAILURES]


# ---------------------------------------------------------------- prompts
def load_briefs():
    """[(brief_id, text)] from codegen_briefs.txt (blocks separated by a line ===)."""
    out = []
    for block in templates.load("codegen_briefs.txt").split("\n===\n"):
        lines = block.strip().split("\n", 1)
        if len(lines) == 2:
            out.append((lines[0].strip(), " ".join(lines[1].split())))
    return out or [("default", "Transcribe the rules into code.")]


def facts_text(spec, analysis):
    p = spec.profile
    a = analysis or {}
    rows = [
        ("family", p.family),
        ("turn structure", a.get("turn_structure") or ("simultaneous" if p.simultaneous else "alternating"
                                                       if p.simultaneous is False else "unknown")),
        ("perfect information", p.perfect_information),
        ("hidden information", a.get("hidden_information") or "unknown"),
        ("chance", a.get("chance") or "unknown"),
        ("action format", p.action_format or "see rules"),
        ("action regex (every valid action fully matches it)", a.get("action_regex") or "none"),
        ("example action", p.example_action or "none"),
    ]
    return "\n".join("- %s: %s" % (k, v) for k, v in rows)


def sample_text(spec):
    st = spec.sample_turn if isinstance(spec.sample_turn, dict) else None
    if not st or not st.get("observation"):
        return templates.load("codegen_nosample.txt")
    legal = st.get("legal_actions")
    legal_s = json.dumps(legal, ensure_ascii=True) if isinstance(legal, list) else \
        "null (more than 50 legal actions; your legal_actions still follows C6)"
    return templates.render("codegen_sample.txt", OBSERVATION=str(st.get("observation"))[:6000], LEGAL=legal_s)


def system_message():
    return templates.render("codegen_system.txt", CONTRACT=templates.load("codegen_contract.txt"),
                            ALLOWED_IMPORTS=", ".join(ALLOWED_IMPORTS),
                            FORBIDDEN=", ".join(FORBIDDEN_NAMES[:12]) + ", global statements")


def build_messages(spec, analysis, brief_text):
    return [{"role": "system", "content": system_message()},
            {"role": "user", "content": templates.render(
                "codegen_user.txt", BRIEF=brief_text, GAME_NAME=spec.game_name, LABEL=spec.label,
                RULES=spec.rules, FACTS=facts_text(spec, analysis), SAMPLE=sample_text(spec))}]


def build_repair_messages(spec, analysis, code, failures):
    return [{"role": "system", "content": system_message()},
            {"role": "user", "content": templates.render(
                "codegen_repair.txt", FAILURES="\n".join("- " + f for f in failures),
                GAME_NAME=spec.game_name, LABEL=spec.label, RULES=spec.rules,
                FACTS=facts_text(spec, analysis), SAMPLE=sample_text(spec), CODE=code)}]


# ---------------------------------------------------------------- files and manifest
def candidates_dir(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), "candidates")


def _sha(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def contract_version():
    """Short hash of the contract text. Candidates written for another contract are stale."""
    return hashlib.sha256(templates.load("codegen_contract.txt").encode("utf-8")).hexdigest()[:12]


def _current(manifest):
    return (isinstance(manifest, dict) and manifest.get("harness_version") == config.HARNESS_VERSION
            and manifest.get("contract") == contract_version())


def read_manifest(spec):
    m = memory.read_json(os.path.join(candidates_dir(spec), MANIFEST))
    return m if isinstance(m, dict) else None


def _update_manifest(spec, entries):
    """Merge entries (by n) into index.json atomically. Never raises."""
    try:
        with _manifest_lock:
            path = os.path.join(candidates_dir(spec), MANIFEST)
            m = memory.read_json(path)
            if not _current(m):
                m = {"harness_version": config.HARNESS_VERSION, "contract": contract_version(),
                     "game_id": spec.game_id, "rules_hash": spec.rules_hash, "label": spec.label,
                     "candidates": []}
            m["family"] = spec.profile.family
            m["updated"] = int(time.time())
            by_n = {c.get("n"): c for c in m.get("candidates", []) if isinstance(c, dict)}
            for e in entries:
                by_n[e["n"]] = dict(by_n.get(e["n"], {}), **e)
            m["candidates"] = [by_n[k] for k in sorted(by_n)]
            memory.write_json_atomic(path, m)
    except Exception:
        log.exception("manifest update failed")


def has_ok_candidates(spec):
    """True when the manifest of the current harness version and contract has a static-ok candidate on disk."""
    try:
        m = read_manifest(spec)
        if not _current(m):
            return False
        d = candidates_dir(spec)
        return any(c.get("static_ok") and c.get("file") and os.path.exists(os.path.join(d, c["file"]))
                   for c in m.get("candidates", []))
    except Exception:
        return False


def ok_paths(spec):
    """Static-ok candidate paths, best first: unrepaired before repaired, then by n."""
    m = read_manifest(spec)
    if not _current(m):
        return []
    d = candidates_dir(spec)
    good = [c for c in m.get("candidates", []) if c.get("static_ok") and c.get("file")
            and os.path.exists(os.path.join(d, c["file"]))]
    good.sort(key=lambda c: (bool(c.get("repaired")), c.get("n", 0)))
    return [os.path.join(d, c["file"]) for c in good]


def _check(code):
    """Normalize (drop a __main__ block) and statically check. Returns (code, failures, notes)."""
    code, removed = strip_main_block(code)
    return code, static_check(code), (["removed a top-level __main__ block"] if removed else [])


# ---------------------------------------------------------------- generation
def generate_simulators(spec, deadline, n_candidates=CODEGEN_CANDIDATES, analysis=None):
    """Generate candidates, static-check them, repair the failed ones once.

    deadline: time.monotonic() by which generation AND repair are finished.
    Returns the paths of the static-ok candidates, best first; [] instead of raising.
    """
    try:
        return _generate(spec, deadline, n_candidates, analysis)
    except Exception:
        log.exception("generate_simulators failed for %s/%s" % (spec.game_id, spec.rules_hash))
        return []


def _generate(spec, deadline, n_candidates, analysis):
    t0 = time.monotonic()
    tag = "%s/%s" % (spec.game_id, spec.rules_hash)
    if deadline - t0 < CODEGEN_MIN_CALL_S:
        log.info("codegen %s skipped: %.0fs left" % (tag, deadline - t0))
        return []
    analysis = analysis if analysis is not None else (classify.load_analysis(spec.game_id, spec.rules_hash) or {})
    gen_deadline = t0 + CODEGEN_GEN_SHARE * (deadline - t0)
    briefs = load_briefs()
    cdir = candidates_dir(spec)
    lock = threading.Lock()
    state = {"closed": False}
    results = {}

    def one(n):
        brief_id, brief = briefs[(n - 1) % len(briefs)]
        budget = CODEGEN_THINKING_BUDGETS[(n - 1) % len(CODEGEN_THINKING_BUDGETS)]
        t = time.monotonic()
        text = classify.call_model(build_messages(spec, analysis, brief), CODEGEN_MAX_TOKENS, gen_deadline,
                                   thinking_budget=budget, tag="codegen-%d" % n, cap_s=CODEGEN_CALL_CAP_S,
                                   margin_s=2.0, min_s=CODEGEN_MIN_CALL_S)
        secs = round(time.monotonic() - t, 1)
        code, notes = extract_code(text)
        entry = {"n": n, "file": None, "brief": brief_id, "static_ok": False, "failures": notes,
                 "repaired": False, "chars": 0, "seconds": secs, "harness_version": config.HARNESS_VERSION,
                 "sha256": None, "thinking_budget": budget}
        if code is not None:
            code, fails, more = _check(code)
            entry.update({"file": "sim_%d.py" % n, "static_ok": not fails, "failures": notes + more + fails,
                          "chars": len(code), "sha256": _sha(code)})
        with lock:
            if state["closed"] or time.monotonic() > gen_deadline:
                log.info("codegen %s cand %d returned after the deadline; ignored" % (tag, n))
                return
            if code is not None:
                memory.write_text_atomic(os.path.join(cdir, entry["file"]), code)
            results[n] = (entry, code)
        log.info("codegen %s cand %d brief=%s static_ok=%s chars=%d %.1fs%s" % (
            tag, n, brief_id, entry["static_ok"], entry["chars"], secs,
            "" if entry["static_ok"] else " failures=%s" % "; ".join(entry["failures"])[:300]))

    pool = ThreadPoolExecutor(max_workers=max(1, n_candidates), thread_name_prefix="codegen")
    try:
        futs = [pool.submit(one, n) for n in range(1, n_candidates + 1)]
        wait(futs, timeout=max(0.0, gen_deadline - time.monotonic()))
    finally:
        with lock:
            state["closed"] = True
            got = dict(results)
        pool.shutdown(wait=False)

    entries = []
    for n in range(1, n_candidates + 1):
        if n in got:
            entries.append(got[n][0])
        else:
            entries.append({"n": n, "file": None, "brief": briefs[(n - 1) % len(briefs)][0],
                            "static_ok": False, "failures": ["no answer before the deadline"],
                            "repaired": False, "chars": 0, "seconds": round(time.monotonic() - t0, 1),
                            "harness_version": config.HARNESS_VERSION, "sha256": None})
    _update_manifest(spec, entries)

    # One repair round for candidates that have code but failed the static check.
    to_repair = [e for e in entries if e["file"] and not e["static_ok"]]
    repaired = 0
    if to_repair and deadline - time.monotonic() >= CODEGEN_REPAIR_MIN_S:
        rpool = ThreadPoolExecutor(max_workers=len(to_repair), thread_name_prefix="codegen-repair")
        try:
            rfuts = [rpool.submit(repair_simulator, spec, os.path.join(cdir, e["file"]), e["failures"],
                                  deadline, analysis) for e in to_repair]
            done, _ = wait(rfuts, timeout=max(0.0, deadline - time.monotonic()))
            repaired = sum(1 for f in done if f.result())
        finally:
            rpool.shutdown(wait=False)
    elif to_repair:
        log.info("codegen %s: no time left for the repair round" % tag)

    paths = ok_paths(spec)
    log.info("codegen summary %s family=%s generated=%d static_ok=%d repaired=%d in %.1fs" % (
        tag, spec.profile.family, sum(1 for e in entries if e["file"]), len(paths), repaired,
        time.monotonic() - t0))
    return paths


def _failure_list(failure_report):
    if failure_report is None:
        return ["unspecified failure"]
    if isinstance(failure_report, str):
        return [failure_report]
    if hasattr(failure_report, "failures"):
        return [str(f) for f in (failure_report.failures or [])] or ["unspecified failure"]
    try:
        return [str(f) for f in failure_report] or ["unspecified failure"]
    except TypeError:
        return [str(failure_report)]


def repair_simulator(spec, path, failure_report, deadline, analysis=None):
    """One model call to fix the candidate at path. Returns path on success, else None. Never raises.

    failure_report: a VerifyReport (its .failures), a list of strings or a string.
    On success the file is overwritten and the manifest entry gets repaired=True.
    """
    try:
        return _repair(spec, path, _failure_list(failure_report), deadline, analysis)
    except Exception:
        log.exception("repair_simulator failed for %s" % path)
        return None


def _repair(spec, path, failures, deadline, analysis):
    m = re.search(r"sim_(\d+)\.py$", str(path))
    n = int(m.group(1)) if m else 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        log.warning("repair: cannot read %s: %s" % (path, e))
        return None
    if analysis is None:
        analysis = classify.load_analysis(spec.game_id, spec.rules_hash) or {}
    t = time.monotonic()
    cut = any("cut off" in f for f in failures)
    text = classify.call_model(build_repair_messages(spec, analysis, code, failures), CODEGEN_MAX_TOKENS,
                               deadline, thinking_budget=None if cut else CODEGEN_REPAIR_THINKING,
                               tag="codegen-repair-%d" % n, cap_s=CODEGEN_CALL_CAP_S, margin_s=2.0,
                               min_s=CODEGEN_MIN_CALL_S)
    secs = round(time.monotonic() - t, 1)
    new, notes = extract_code(text)
    fails = notes
    if new is not None:
        new, fails2, more = _check(new)
        fails = (notes + more + fails2) if fails2 else []
    if time.monotonic() > deadline:
        log.info("repair %s returned after the deadline; ignored" % path)
        return None
    if new is not None and not fails:
        memory.write_text_atomic(path, new)
        _update_manifest(spec, [{"n": n, "file": os.path.basename(path), "static_ok": True, "failures": [],
                                 "repaired": True, "chars": len(new), "sha256": _sha(new),
                                 "repair_seconds": secs, "failures_before_repair": failures[:CODEGEN_MAX_FAILURES]}])
        log.info("codegen repair %s ok in %.1fs" % (os.path.basename(path), secs))
        return path
    _update_manifest(spec, [{"n": n, "repair_failures": (fails or ["repair failed"])[:CODEGEN_MAX_FAILURES],
                             "repair_seconds": secs}])
    log.info("codegen repair %s failed in %.1fs: %s" % (os.path.basename(path), secs, "; ".join(fails)[:200]))
    return None
