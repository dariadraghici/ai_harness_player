"""Learning the API of a library (TASK section 3D).

Owner: luckydevs-3 area. Status: real. Standard library only.

Sources, in this order:

  1. LOCAL INTROSPECTION, the primary one and the only one that needs no network: the installed module is
     imported in an isolated subprocess (never in the bot process) and its public surface is written out -
     submodules, `__all__`, classes, function signatures and the first lines of each docstring. What the
     installed version really offers cannot be out of date or made up.
  2. the registry description (PyPI `info.description`), and
  3. the README or documentation page of the project, through the guarded fetcher (bot/libs/net.py).

The text is stripped, capped, cut into chunks and ranked with a small BM25 against the capability question, so
the model only reads the part that is about the computation we want. Everything is cached in
/memory/_docs/<name==version>/ and shared with the other containers.

THE TEXT IS UNTRUSTED. A README is written by a stranger, so `sanitize()` drops the lines that look like
instructions to a model and the code fences that could break our prompt, and the prompt itself says the block
is reference material. Nothing in it is ever executed, and no package it mentions is ever installed.
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

from .. import log, memory
from . import install, net

DOCS_DIRNAME = "_docs"
INTROSPECT_TIMEOUT_S = 25.0
MAX_DOC_CHARS = 60000           # per source, before chunking
CHUNK_CHARS = 900
MAX_CHUNKS = 400
KEEP_CHUNKS = 8                 # chunks handed to the model
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,30}")

# Lines of a README that look like an instruction to a model rather than documentation.
_INJECTION = tuple(re.compile(p, re.I) for p in (
    r"\bignore\b.{0,40}\b(?:instruction|prompt|previous|above|rule)",
    r"\b(?:system|assistant|user)\s*:",
    r"\byou (?:must|should|are required to)\b.{0,40}\b(?:output|answer|respond|install|run|execute)\b",
    r"\b(?:new|updated) (?:instructions?|rules?)\b",
    r"\bpip install\b",                      # never act on an install line from a document
    r"\b(?:curl|wget|bash|sh)\s+[-a-z]*\s*http",
    r"\b(?:api[_ ]?key|token|secret)\b\s*[:=]",
))


def _hostname(url):
    import urllib.parse
    try:
        return urllib.parse.urlsplit(str(url)).hostname or ""
    except ValueError:
        return ""


def docs_root():
    return os.path.join(memory.root(), DOCS_DIRNAME)


def cache_dir(lib):
    return os.path.join(docs_root(), memory.safe_name("%s==%s" % (lib.name, lib.version or "installed")))


def sanitize(text, max_chars=MAX_DOC_CHARS):
    """Untrusted documentation -> a block that is safe to put in a prompt. Never executed, never obeyed."""
    out = []
    for raw in str(text or "").splitlines():
        line = raw.replace("```", "'''").replace("<<<", "< < <").replace(">>>", "> > >").rstrip()
        if not line.strip():
            continue
        if any(rx.search(line) for rx in _INJECTION):
            continue
        out.append(line[:300])
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n".join(out)


# ---------------------------------------------------------------- 1. local introspection
_INTROSPECT = r'''
import importlib, inspect, json, pkgutil, site, sys
sys.path.extend(site.getsitepackages())
extra, name, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
if extra:
    sys.path.insert(0, extra)
MAX_MEMBERS = 90
out = {"module": name, "ok": False, "submodules": [], "members": [], "doc": ""}
def one(obj, qual):
    try:
        sig = ""
        if inspect.isclass(obj):
            kind = "class"
            try:
                sig = str(inspect.signature(obj))
            except (TypeError, ValueError):
                sig = "(...)"
        elif callable(obj):
            kind = "function"
            try:
                sig = str(inspect.signature(obj))
            except (TypeError, ValueError):
                sig = "(...)"
        else:
            return None
        doc = " ".join((inspect.getdoc(obj) or "").split())[:240]
        item = {"name": qual, "kind": kind, "signature": sig[:200], "doc": doc}
        if kind == "class":
            methods = []
            for mname, m in list(vars(obj).items())[:40]:
                if mname.startswith("_") or not callable(m):
                    continue
                try:
                    msig = str(inspect.signature(m))
                except (TypeError, ValueError):
                    msig = "(...)"
                methods.append("%s%s" % (mname, msig[:120]))
            item["methods"] = methods[:14]
        return item
    except BaseException:
        return None
try:
    mod = importlib.import_module(name)
    out["ok"] = True
    out["doc"] = " ".join((getattr(mod, "__doc__", "") or "").split())[:600]
    try:
        path = getattr(mod, "__path__", None)
        if path:
            out["submodules"] = sorted(m.name for m in pkgutil.iter_modules(path))[:40]
    except BaseException:
        pass
    names = [n for n in (getattr(mod, "__all__", None) or dir(mod)) if isinstance(n, str) and not n.startswith("_")]
    for n in names[:MAX_MEMBERS]:
        try:
            item = one(getattr(mod, n), n)
        except BaseException:
            item = None
        if item:
            out["members"].append(item)
    for sub in out["submodules"][:6]:
        try:
            m2 = importlib.import_module(name + "." + sub)
        except BaseException:
            continue
        names2 = [n for n in (getattr(m2, "__all__", None) or dir(m2)) if isinstance(n, str) and not n.startswith("_")]
        for n in names2[:25]:
            try:
                item = one(getattr(m2, n), "%s.%s" % (sub, n))
            except BaseException:
                item = None
            if item:
                out["members"].append(item)
except BaseException as e:
    out["error"] = "%s: %s" % (type(e).__name__, e)
try:
    with open(out_path, "w") as f:
        json.dump(out, f)
except BaseException:
    pass
'''


def introspect(lib, path=""):
    """The public surface of the installed library, from a subprocess. {} when it cannot be read."""
    fd, out_path = tempfile.mkstemp(prefix="ld-introspect-", suffix=".json")
    os.close(fd)
    try:
        args = [sys.executable, "-I", "-S", "-B", "-c", _INTROSPECT, path or "", lib.import_name, out_path]
        rc, tail = install._run(args, INTROSPECT_TIMEOUT_S)
        data = memory.read_json(out_path, {})
        if not isinstance(data, dict) or not data.get("ok"):
            log.info("libs/docs: introspection of %s failed (rc=%s) %s" % (
                lib.import_name, rc, str((data or {}).get("error") or tail)[-200:]))
            return {}
        return data
    except Exception:
        log.exception("libs/docs: introspection crashed")
        return {}
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def api_text(data):
    """The introspection result as compact reference text."""
    if not data:
        return ""
    lines = ["MODULE %s: %s" % (data.get("module"), data.get("doc", ""))]
    if data.get("submodules"):
        lines.append("submodules: " + ", ".join(data["submodules"]))
    for m in data.get("members") or ():
        head = "%s %s%s" % (m.get("kind"), m.get("name"), m.get("signature") or "")
        if m.get("doc"):
            head += "  # " + m["doc"]
        lines.append(head)
        for meth in m.get("methods") or ():
            lines.append("    method %s" % meth)
    return "\n".join(lines)


# ---------------------------------------------------------------- 2/3. registry and repository text
def registry_text(lib):
    data = net.pypi_json(lib.name) if net.enabled() else None
    if not isinstance(data, dict):
        return "", []
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    urls = info.get("project_urls") if isinstance(info.get("project_urls"), dict) else {}
    links = [v for v in urls.values() if isinstance(v, str) and net.host_allowed(_hostname(v))]
    return sanitize(info.get("description") or ""), links


def repo_text(lib, links, deadline):
    """The first documentation page we can reach, stripped. Catalog links first, then the project's own."""
    for url in list(lib.docs) + list(links or ()):
        if deadline is not None and time.monotonic() > deadline:
            break
        ok, reason = net.check_url(url)
        if not ok:
            log.debug("libs/docs: skipping %s (%s)" % (url, reason))
            continue
        text = net.get_text(url)
        if text and len(text) > 400:
            return sanitize(text), url
    return "", ""


# ---------------------------------------------------------------- a small BM25 over chunks
def chunks_of(text, size=CHUNK_CHARS):
    out, buf = [], []
    for para in str(text or "").split("\n"):
        buf.append(para)
        if sum(len(x) for x in buf) >= size:
            out.append("\n".join(buf))
            buf = []
            if len(out) >= MAX_CHUNKS:
                return out
    if buf:
        out.append("\n".join(buf))
    return out[:MAX_CHUNKS]


def bm25(query, docs, k1=1.5, b=0.75):
    """Scores of `docs` for `query`, both as token lists. Pure python, no dependency."""
    n = len(docs)
    if not n:
        return []
    lens = [len(d) or 1 for d in docs]
    avg = sum(lens) / float(n)
    freq = [{} for _ in docs]
    for i, d in enumerate(docs):
        for t in d:
            freq[i][t] = freq[i].get(t, 0) + 1
    scores = [0.0] * n
    for term in set(query):
        df = sum(1 for f in freq if term in f)
        if not df:
            continue
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for i, f in enumerate(freq):
            tf = f.get(term, 0)
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lens[i] / avg))
    return scores


def tokens(text):
    return [w.lower() for w in _WORD.findall(str(text or ""))]


def best_chunks(text, question, keep=KEEP_CHUNKS):
    parts = chunks_of(text)
    if len(parts) <= keep:
        return parts
    scores = bm25(tokens(question), [tokens(p) for p in parts])
    order = sorted(range(len(parts)), key=lambda i: -scores[i])[:keep]
    return [parts[i] for i in sorted(order)]           # keep the document's own order


# ---------------------------------------------------------------- the one entry point
def learn(lib, path, question, deadline):
    """{"api", "docs", "source"}: what the model is given about this library. Cached in /memory. Never raises."""
    out = {"api": "", "docs": "", "source": ""}
    try:
        cache = cache_dir(lib)
        hit = memory.read_json(os.path.join(cache, "docs.json"), {})
        if isinstance(hit, dict) and hit.get("api"):
            log.info("libs/docs: %s from the cache" % lib.name)
            out.update({k: str(hit.get(k) or "") for k in out})
        else:
            data = introspect(lib, path)
            out["api"] = api_text(data)[:MAX_DOC_CHARS]
            text, links = registry_text(lib)
            page, url = ("", "")
            if len(text) < 2000:
                page, url = repo_text(lib, links, deadline)
            out["docs"] = sanitize(page or text)[:MAX_DOC_CHARS]
            out["source"] = url or ("pypi:%s" % lib.name if text else "introspection")
            if out["api"] or out["docs"]:
                memory.write_json_atomic(os.path.join(cache, "docs.json"), out)
        if out["docs"]:
            out["docs"] = "\n\n".join(best_chunks(out["docs"], question))
        return out
    except Exception:
        log.exception("libs/docs: learn failed")
        return out
