"""Writing the adapter module (TASK section 3E).

Owner: luckydevs-3 area. Status: real. Standard library only.

Haiku writes the module; this file decides whether the module may exist at all. The loop is the one that
already works for our simulators (bot/prep/codegen.py): generate, check the text with `ast` WITHOUT running it,
send the failures back, at most MAX_ROUNDS times, always inside a deadline. Prep spend is not scored, so
thinking is on.

The static check is stricter than the one for simulators, because an adapter is allowed to import a
third-party library: only the chosen library plus a fixed list of standard modules may be imported, nothing may
happen at import time, and the dangerous builtins are refused. The module is never imported in the bot process
- only inside a sandbox child (bot/libs/adapter_job.py).
"""

import ast
import os
import re
import time

from .. import config, log, memory
from ..prep import classify, codegen

PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
MAX_ROUNDS = 4                  # one generation plus three repairs
MIN_CALL_S = 25.0
MAX_CODE_CHARS = 40000
ADAPTER_FILE = "adapter.py"
API_CHARS = 7000                # of the introspection text given to the model
DOCS_CHARS = 6000
EXTRA_API_CHARS = 2500          # of a second library that is also installed

STDLIB_OK = ("math", "random", "itertools", "collections", "re", "json", "copy", "functools", "heapq", "string",
             "fractions", "typing", "operator", "bisect", "statistics", "time", "array", "dataclasses", "enum",
             "abc", "decimal", "numbers", "textwrap", "unicodedata")
FORBIDDEN_MODULES = ("os", "sys", "subprocess", "socket", "urllib", "http", "requests", "shutil", "pathlib",
                     "pickle", "marshal", "multiprocessing", "threading", "ctypes", "importlib", "builtins",
                     "code", "pty", "signal", "resource", "tempfile", "glob", "webbrowser", "ftplib", "smtplib",
                     "telnetlib", "asyncio", "concurrent")
FORBIDDEN_NAMES = ("print", "input", "eval", "exec", "open", "__import__", "compile", "breakpoint", "exit",
                   "quit", "globals", "locals", "vars", "help", "setattr", "delattr")
REQUIRED_METHODS = {"setup": 3, "parse": 3, "suggest": 3, "update": 2}      # including self
_cache = {}


def template(name):
    if name not in _cache:
        with open(os.path.join(PROMPT_DIR, name), "r", encoding="utf-8") as f:
            _cache[name] = f.read()
    return _cache[name]


def render(name, **values):
    text = template(name)
    for k, v in values.items():
        text = text.replace("{{%s}}" % k, str(v))
    return text


def imports_of(code):
    """The top-level module names the finished adapter really imports. The model is allowed to decide that the
    library adds nothing for a tiny game and to solve it with the standard library instead; the report then has
    to say so rather than claim a library that is not used."""
    out = []
    try:
        for node in ast.walk(ast.parse(code or "")):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if n and n not in out:
                    out.append(n)
    except (SyntaxError, ValueError, RecursionError):
        pass
    return out


def uses_library(code, lib_names):
    """True when the adapter imports at least one of the libraries we installed for it."""
    got = set(imports_of(code))
    return any(n in got for n in lib_names if n)


def adapter_path(spec):
    return os.path.join(memory.variant_dir(spec.game_id, spec.rules_hash), ADAPTER_FILE)


def _guarded_import(node, allowed):
    """A top-level try/except that only imports (and sets a name to a constant when the import fails)."""
    inner = list(node.body) + list(node.orelse) + list(node.finalbody)
    for handler in node.handlers:
        inner.extend(handler.body)
    for stmt in inner:
        if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            value = stmt.value
            if value is None or codegen._const_expr_ok(value, allowed):
                continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


# ---------------------------------------------------------------- the static check
def static_check(code, import_roots):
    """Text-only checks with ast. Returns the failures ([] = pass). The code is never executed."""
    if not isinstance(code, str) or not code.strip():
        return ["empty module"]
    fails = []
    if len(code) > MAX_CODE_CHARS:
        fails.append("module longer than %d characters; write a shorter one" % MAX_CODE_CHARS)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return ["syntax error at line %s: %s%s" % (e.lineno, e.msg,
                                                   " (%s)" % e.text.strip()[:80] if e.text else "")]
    except (ValueError, RecursionError) as e:
        return ["cannot parse the module: %s" % e]

    allowed = set(STDLIB_OK) | set(r.split(".")[0] for r in import_roots if r)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in FORBIDDEN_MODULES or root not in allowed:
                    fails.append("line %d: import of %r is not allowed (allowed: the library %s plus %s)" % (
                        node.lineno, a.name, "/".join(sorted(import_roots)) or "-", ", ".join(STDLIB_OK[:8]) + ", ..."))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root in FORBIDDEN_MODULES or root not in allowed:
                fails.append("line %d: import from %r is not allowed" % (node.lineno, node.module))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            fails.append("line %d: %s() must not be used" % (node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and isinstance(node.attr, str) and node.attr.startswith("__") \
                and node.attr not in ("__name__", "__doc__", "__class__", "__dict__"):
            fails.append("line %d: attribute %r must not be used" % (node.lineno, node.attr))

    klass = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Adapter":
            klass = node
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
            continue
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        elif isinstance(node, ast.Try) and _guarded_import(node, allowed):
            continue           # `try: import x / except ImportError: x = None` degrades instead of crashing
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value if isinstance(node, ast.Assign) else node.value
            if value is not None and not codegen._const_expr_ok(value, allowed):
                fails.append("line %d: top-level assignment runs code at import time (A8): only constants" %
                             node.lineno)
        else:
            fails.append("line %d: top-level %s is not allowed (A8)" % (node.lineno, type(node).__name__))
    if klass is None:
        return fails + ["there is no top-level `class Adapter` (A1)"]
    methods = {n.name: n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, count in REQUIRED_METHODS.items():
        fn = methods.get(name)
        if fn is None:
            fails.append("Adapter has no method %s (A1)" % name)
            continue
        if isinstance(fn, ast.AsyncFunctionDef):
            fails.append("Adapter.%s must not be async (A1)" % name)
            continue
        args = fn.args
        got = len(args.posonlyargs) + len(args.args)
        required_kwonly = [k for k, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
        if got != count or args.vararg or args.kwarg or required_kwonly:
            fails.append("Adapter.%s takes %d parameter(s) including self, expected exactly %d (A1)" % (
                name, got, count))
    return fails


# ---------------------------------------------------------------- the prompt
def facts_text(spec, analysis, tags):
    a = analysis or {}
    p = spec.profile
    rows = ["family: %s" % p.family,
            "capability tags: %s" % ", ".join(tags),
            "action format: %s" % (p.action_format or "(not stated)"),
            "example action: %s" % (p.example_action or "(none)"),
            "action regex (ours): %s" % (a.get("action_regex") or "(none)"),
            "simultaneous: %s, perfect information: %s" % (p.simultaneous, p.perfect_information),
            "variant label: %s" % spec.label]
    if p.speech_restrictions:
        rows.append("speech restrictions: %s" % p.speech_restrictions[:120])
    if spec.playbook:
        rows.append("playbook: " + " ".join(spec.playbook.split())[:400])
    return "\n".join(rows)


def sample_text(spec):
    sample = spec.sample_turn if isinstance(spec.sample_turn, dict) else {}
    obs = str(sample.get("observation") or "")
    if not obs:
        return "(no sample turn: parse the observation from the rules text alone)"
    legal = sample.get("legal_actions")
    rows = ["observation:", obs[:1800]]
    if isinstance(legal, list) and legal:
        rows.append("legal_actions (%d): %s" % (len(legal), ", ".join(str(a) for a in legal[:40])))
    else:
        rows.append("legal_actions: null (more than 50 moves, use the action format)")
    return "\n".join(rows)


def extra_text(extra):
    """The block about a second installed library the adapter may use instead of, or next to, the first."""
    if not extra:
        return ""
    lib, learned = extra
    return ("ALSO INSTALLED (use it instead of, or together with, the library above if it fits better):\n"
            "import %s   (distribution %s %s) - %s\n%s\n\n" % (
                lib.import_name, lib.name, lib.version or "installed", lib.why or "",
                (learned.get("api") or "")[:EXTRA_API_CHARS]))


def import_roots(lib, extra=None):
    roots = [lib.import_name]
    if extra:
        roots.append(extra[0].import_name)
    return roots


def build_messages(spec, analysis, tags, lib, learned, extra=None):
    profile = {"TAGS": ", ".join(tags), "WHY": lib.why or "(no note)", "IMPORT": lib.import_name,
               "NAME": lib.name, "VERSION": lib.version or "installed",
               "API": (learned.get("api") or "(introspection failed)")[:API_CHARS],
               "DOCS": (learned.get("docs") or "(no documentation available)")[:DOCS_CHARS],
               "EXTRA": extra_text(extra),
               "RULES": spec.rules[:6000], "FACTS": facts_text(spec, analysis, tags), "SAMPLE": sample_text(spec)}
    system = render("adapter_system.txt", CONTRACT=template("adapter_contract.txt"))
    return [{"role": "system", "content": system},
            {"role": "user", "content": render("adapter_user.txt", **profile)}]


def build_repair_messages(spec, analysis, tags, lib, learned, code, failures, extra=None):
    msgs = build_messages(spec, analysis, tags, lib, learned, extra)
    msgs.append({"role": "user", "content": render("adapter_repair.txt",
                                                   FAILURES="\n".join("- %s" % f for f in failures[:12]),
                                                   CODE=code[:MAX_CODE_CHARS])})
    return msgs


# ---------------------------------------------------------------- the loop
def generate(spec, analysis, tags, lib, learned, deadline, check=None, extra=None):
    """Write and check adapter.py for this variant. Returns (code or None, rounds, failures).

    check(code) -> failures: an extra check that may RUN the module in a sandbox child (bot/libs/validate.py
    passes the behaviour checks here, so a module that parses but cannot parse the observation is repaired too).
    """
    code, failures, rounds = None, [], 0
    try:
        for rounds in range(1, MAX_ROUNDS + 1):
            left = deadline - time.monotonic()
            if left < MIN_CALL_S:
                log.info("libs/adapter: no time for round %d (%.0fs left)" % (rounds, left))
                break
            msgs = (build_messages(spec, analysis, tags, lib, learned, extra) if code is None else
                    build_repair_messages(spec, analysis, tags, lib, learned, code, failures, extra))
            text = classify.call_model(msgs, config.PREP_MAX_TOKENS, deadline,
                                       thinking_budget=config.PREP_THINKING_BUDGET,
                                       tag="adapter-%d" % rounds, cap_s=config.PREP_CALL_TIMEOUT_S)
            if not text:
                failures = ["the model returned nothing"]
                continue
            fresh, notes = codegen.extract_code(text)
            if not fresh:
                failures = list(notes) or ["no fenced python block in the answer"]
                continue
            fresh, removed = codegen.strip_main_block(fresh)
            if removed:
                notes = list(notes) + ["the __main__ block was removed (A8)"]
            fails = static_check(fresh, import_roots(lib, extra))
            if not fails and check is not None:
                fails = check(fresh) or []
            code, failures = fresh, fails
            log.info("libs/adapter %s round %d: %s%s" % (
                spec.label[:20], rounds, "OK" if not fails else "%d failure(s)" % len(fails),
                "" if not fails else " | " + "; ".join(fails)[:300]))
            if not fails:
                return code, rounds, []
        return (code if not failures else None), rounds, failures
    except Exception:
        log.exception("libs/adapter: generation failed")
        return None, rounds, failures + ["generation crashed"]
