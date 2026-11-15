"""Which library could compute good moves for this capability (TASK section 3B).

Owner: luckydevs-3 area. Status: real. Standard library only.

Candidates come from two places: the curated catalog (bot/libs/catalog.json, data) and names Haiku proposes from
its own memory. Both are treated the same way: a name is only kept when the PyPI JSON API confirms it, so a
hallucinated or typosquatted name dies here instead of in `pip install`. Filters (all of them in `verify`):

  * a wheel for THIS interpreter and platform: manylinux x86_64 cpXY/abi3, or a pure-python *-none-any wheel;
  * not yanked, size within the entry's cap and MAX_MB;
  * the release is at least MIN_AGE_DAYS old (a brand new version is a supply-chain risk);
  * requires_python accepts our interpreter;
  * a source repository is linked (and, for a name that is not in the catalog, enough releases and age);
  * not a client of a hosted language model (the catalog's deny list, rulebook 7.7).

A `baked` catalog entry is installed in the image, so it is usable with no network at all: it skips the registry
and is proved by an import probe in bot/libs/install.py instead.
"""

import json
import os
import re
import sys
import time
from collections import namedtuple

from .. import log
from . import capabilities, net

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")
MAX_MB = 150.0                 # per library, TASK section 3B
MIN_AGE_DAYS = 14.0
MIN_RELEASES_UNLISTED = 3      # a name the model invented needs a real history
MIN_AGE_DAYS_UNLISTED = 180.0
MAX_KEEP = 3                   # at most 3 libraries per game
MODEL_NAMES_MAX = 6

Lib = namedtuple("Lib", "name import_name tags why baked rank max_mb docs version size_mb source score")

_catalog = [None]


def catalog():
    if _catalog[0] is None:
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                _catalog[0] = json.load(f)
        except Exception:
            log.exception("libs/discover: catalog unreadable")
            _catalog[0] = {"entries": [], "deny": []}
    return _catalog[0]


def normalize(name):
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def denied(name):
    n = normalize(name)
    deny = set(normalize(x) for x in catalog().get("deny") or ())
    if n in deny:
        return True
    return bool(re.search(r"(?:^|-)(?:openai|anthropic|llm|gpt|claude|gemini|langchain)(?:-|$)", n))


def entries_for(tags):
    """Catalog rows that serve at least one of the tags, best match first."""
    rows = []
    for e in catalog().get("entries") or ():
        if not isinstance(e, dict) or not e.get("name"):
            continue
        hits = [t for t in tags if t in (e.get("tags") or ())]
        if not hits:
            continue
        rank = min(tags.index(t) for t in hits)
        rows.append((rank, -int(e.get("rank") or 0), 0 if e.get("baked") else 1,
                     float(e.get("max_mb") or MAX_MB), e))
    rows.sort(key=lambda r: r[:4])
    return [r[4] for r in rows]


def python_tag():
    return "cp%d%d" % (sys.version_info[0], sys.version_info[1])


def wheel_ok(filename):
    """A wheel we can install without a compiler: pure python, or an x86_64 manylinux build for this interpreter.

    The wheel name is name-version(-build)?-pythontag-abitag-platformtag.whl (PEP 427). A pure-python wheel with
    a platform tag (z3_solver-...-py3-none-manylinux_2_27_x86_64.whl) is fine, a Windows or macOS wheel is not.
    """
    f = str(filename or "")
    if not f.lower().endswith(".whl"):
        return False
    parts = f[:-4].split("-")
    if len(parts) < 5:
        return False
    py_tags, _abi, plat_tags = parts[-3].lower(), parts[-2].lower(), parts[-1].lower()
    ours = python_tag()
    py_ok = any(t == ours or t in ("py3", "py2.py3") or t.startswith("abi3") for t in py_tags.split("."))
    if not py_ok:
        return False
    for plat in plat_tags.split("."):
        if plat == "any":
            return True
        if "x86_64" in plat and ("manylinux" in plat or plat.startswith("linux")):
            return True
    return False


def _requires_python_ok(spec):
    """A light check of a requires_python specifier against the running interpreter."""
    text = str(spec or "").strip()
    if not text:
        return True
    ours = (sys.version_info[0], sys.version_info[1])
    for part in text.split(","):
        part = part.strip()
        m = re.match(r"(>=|>|<=|<|==|!=|~=)\s*(\d+)(?:\.(\d+))?", part)
        if not m:
            continue
        op, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        them = (major, minor)
        if op in (">=", "~=") and ours < them:
            return False
        if op == ">" and ours <= them:
            return False
        if op == "<=" and ours > them:
            return False
        if op == "<" and ours >= (them if m.group(3) else (major, 0)):
            return False
        if op == "==" and m.group(3) and ours != them:
            return False
    return True


def _age_days(iso):
    try:
        text = str(iso or "").replace("Z", "+00:00")
        stamp = time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        return max(0.0, (time.time() - time.mktime(stamp) + time.timezone) / 86400.0)
    except Exception:
        return 0.0


PRE_RELEASE = re.compile(r"(?:\.|^|\d)(?:dev|rc|a|b|alpha|beta|pre|post)\d*(?:\.|$)", re.I)
RELEASES_SCANNED = 40          # newest releases we look at (a long history is not worth the work)


def _stable(version):
    return not PRE_RELEASE.search(str(version or ""))


def _ordered_releases(data):
    """[(version, files)] newest first by upload time, stable releases before pre-releases."""
    rows = []
    releases = data.get("releases") if isinstance(data.get("releases"), dict) else {}
    for version, files in releases.items():
        files = [f for f in (files or ()) if isinstance(f, dict)]
        if not files:
            continue
        newest = max((str(f.get("upload_time_iso_8601") or f.get("upload_time") or "") for f in files), default="")
        rows.append((0 if _stable(version) else 1, newest, str(version), files))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    rows.sort(key=lambda r: r[0])          # stable first, each group newest first
    return [(r[2], r[3]) for r in rows[:RELEASES_SCANNED]]


def verify(name, entry=None, deadline=None):
    """(Lib, reason). Lib is None when the name is refused; reason always says why.

    Picks the newest release that is at least MIN_AGE_DAYS old and has a wheel we can install: a package whose
    latest version shipped yesterday is used at an older, settled version instead of being thrown away.
    """
    entry = entry or {}
    listed = bool(entry.get("name"))
    if denied(name):
        return None, "denied: it is a client of a hosted language model (rulebook 7.7)"
    if deadline is not None and time.monotonic() > deadline:
        return None, "no time left for the registry check"
    data = net.pypi_json(name)
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
        return None, "not found on the package index"
    info = data["info"]
    cap = min(float(entry.get("max_mb") or MAX_MB), MAX_MB)
    if not _requires_python_ok(info.get("requires_python")):
        return None, "requires_python %r excludes this interpreter" % (info.get("requires_python"),)
    releases = _ordered_releases(data)
    if not listed:
        # A name we did not curate needs the signals of a real project: a source repository and a history.
        urls = info.get("project_urls") if isinstance(info.get("project_urls"), dict) else {}
        repo = next((v for k, v in urls.items() if re.search(r"source|repo|code|github|homepage", str(k), re.I)
                     and isinstance(v, str) and net.host_allowed(_host(v))), "")
        if not repo and not str(info.get("home_page") or ""):
            return None, "no source repository is linked"
        if len(data.get("releases") or {}) < MIN_RELEASES_UNLISTED:
            return None, "not in our catalog and only %d release(s)" % len(data.get("releases") or {})
        oldest = min((_age_days(f.get("upload_time_iso_8601")) for _v, files in releases for f in files), default=0.0)
        if oldest < MIN_AGE_DAYS_UNLISTED:
            return None, "not in our catalog and the project is only %.0f days old" % oldest
    why_not = ""
    for version, files in releases:
        wheels = [f for f in files if f.get("packagetype") == "bdist_wheel" and not f.get("yanked")
                  and wheel_ok(f.get("filename"))]
        if not wheels:
            why_not = why_not or "no manylinux x86_64 or pure-python wheel for %s" % python_tag()
            continue
        wheel = min(wheels, key=lambda f: float(f.get("size") or 0) or 1e12)
        age = _age_days(wheel.get("upload_time_iso_8601") or wheel.get("upload_time"))
        if age < MIN_AGE_DAYS:
            why_not = why_not or "every wheel is younger than %.0f days" % MIN_AGE_DAYS
            continue
        size_mb = float(wheel.get("size") or 0) / (1024.0 * 1024.0)
        if size_mb > cap:
            why_not = why_not or "wheel is %.0f MB, cap is %.0f MB" % (size_mb, cap)
            continue
        if not _requires_python_ok(wheel.get("requires_python")):
            why_not = why_not or "the wheel excludes this interpreter"
            continue
        lib = Lib(name=str(info.get("name") or name), import_name=str(entry.get("import") or _import_guess(name)),
                  tags=tuple(entry.get("tags") or ()), why=str(entry.get("why") or (info.get("summary") or ""))[:200],
                  baked=bool(entry.get("baked")), rank=int(entry.get("rank") or 0), max_mb=cap,
                  docs=tuple(entry.get("docs") or ()), version=version, size_mb=round(size_mb, 2),
                  source="catalog" if listed else "model", score=0.0)
        return lib, "ok"
    return None, why_not or "no usable release"


def _host(url):
    try:
        import urllib.parse
        return urllib.parse.urlsplit(str(url)).hostname or ""
    except Exception:
        return ""


def _import_guess(name):
    return re.sub(r"[-.]+", "_", str(name or "")).strip("_").lower()


def from_catalog_offline(tags):
    """The baked catalog rows for these tags, with no network at all (the arena may have no registry)."""
    out = []
    for e in entries_for(tags):
        if not e.get("baked"):
            continue
        out.append(Lib(name=e["name"], import_name=str(e.get("import") or _import_guess(e["name"])),
                       tags=tuple(e.get("tags") or ()), why=str(e.get("why") or "")[:200], baked=True,
                       rank=int(e.get("rank") or 0), max_mb=float(e.get("max_mb") or MAX_MB),
                       docs=tuple(e.get("docs") or ()), version="", size_mb=0.0, source="baked", score=0.0))
    return out


def _score(lib, tags):
    """How well this library fits THIS game: how important its best matching capability is to us, our curated
    rank among the libraries that serve it, whether it is already in the image, minus the install cost.

    Breadth is deliberately not rewarded: a library tagged for four capabilities is not better at this one than
    a focused one (that is what `rank` in the catalog is for)."""
    hits = [t for t in lib.tags if t in tags]
    if not hits:
        return 0.0
    best = min(tags.index(t) for t in hits)
    return ((len(tags) - best) * 6.0 + lib.rank * 4.0 + (6.0 if lib.baked else 0.0)
            - min(5.0, lib.size_mb / 30.0))


def candidates(tags, model_names=(), deadline=None, want=MAX_KEEP):
    """The libraries worth installing for these capability tags, best first. Never raises.

    Baked catalog rows are always offered (they cost no network and no install). Everything else, catalog or
    model-proposed, must pass `verify` on the package index.
    """
    tags = [t for t in (tags or ()) if t in capabilities.TAGS]
    if not tags:
        return []
    out, seen, reasons = [], set(), []
    try:
        for lib in from_catalog_offline(tags):
            key = normalize(lib.name)
            if key not in seen:
                seen.add(key)
                out.append(lib._replace(score=_score(lib, tags)))
        if net.enabled():
            rows = [(e["name"], e) for e in entries_for(tags) if normalize(e["name"]) not in seen]
            extra = [n for n in (model_names or ())[:MODEL_NAMES_MAX] if normalize(n) not in seen]
            for name, entry in rows + [(n, {}) for n in extra]:
                if deadline is not None and time.monotonic() > deadline:
                    reasons.append("%s: out of time" % name)
                    break
                lib, why = verify(name, entry, deadline)
                if lib is None:
                    reasons.append("%s: %s" % (name, why))
                    continue
                seen.add(normalize(lib.name))
                out.append(lib._replace(score=_score(lib, tags)))
        out.sort(key=lambda l: -l.score)
        if reasons:
            log.info("libs/discover: refused %s" % "; ".join(reasons[:6]))
        log.info("libs/discover: tags=%s -> %s" % (
            ",".join(tags), ", ".join("%s%s(%.0f)" % (l.name, "" if not l.version else "==" + l.version, l.score)
                                      for l in out[:want]) or "nothing"))
        return out[:want]
    except Exception:
        log.exception("libs/discover: candidate search failed")
        return out[:want]
