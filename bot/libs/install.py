"""Installing a library bundle in isolation (TASK section 3C).

Owner: luckydevs-3 area. Status: real. Standard library only.

Rules this file keeps:

  * NOTHING REACHES OUR STDOUT (rulebook 5.1). pip runs as a subprocess with stdout and stderr captured and
    echoed through bot/log.py, which writes to stderr.
  * Wheels only (`--only-binary=:all:`), so no compiler and no setup.py is ever executed, and the baked
    wheelhouse (/opt/wheelhouse) is tried before the network.
  * One wall-clock timeout per pip call, and the whole bundle stops at its deadline.
  * /memory is shared by every container of ours at the same time (5.14), so an install holds an `fcntl.flock`,
    writes into a private temporary directory and publishes it with one atomic rename. A bundle another
    container published first is reused as it is.
  * A bundle is only called ready when every library imports in a fresh subprocess.

Why a target directory and not a venv: the sandbox child runs with `python -I -S` (isolated, no `site`), so a
virtual environment would not be activated for it anyway - the job adds the directory to `sys.path` itself
(bot/libs/adapter_job.py). A plain directory is therefore equivalent and costs no bootstrap. pip does not see
the libraries baked into the image from a target directory, so a bundle whose dependencies are all baked is
installed with `--no-deps` instead of downloading them again.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from .. import log, memory

WHEELHOUSE = os.environ.get("BOT_WHEELHOUSE", "/opt/wheelhouse")
BUNDLE_DIRNAME = "_venvs"              # the TASK's path name; it holds target directories
PIPCACHE_DIRNAME = "_pipcache"
READY = ".ready.json"
PIP_TIMEOUT_S = 90.0                   # one pip call
PROBE_TIMEOUT_S = 25.0
MIN_INSTALL_S = 12.0                   # do not start pip with less time left than this


def root():
    return os.path.join(memory.root(), BUNDLE_DIRNAME)


def bundle_id(libs):
    """A stable id for a set of pinned libraries plus this interpreter."""
    key = "|".join(sorted("%s==%s" % (l.name, l.version or "latest") for l in libs))
    key += "|py%d.%d" % sys.version_info[:2]
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def bundle_dir(libs):
    return os.path.join(root(), bundle_id(libs))


def is_ready(path, libs):
    try:
        data = memory.read_json(os.path.join(path, READY), {})
        if not isinstance(data, dict):
            return False
        have = set(data.get("libs") or ())
        return have >= set("%s==%s" % (l.name, l.version or "latest") for l in libs)
    except Exception:
        return False


class _Lock:
    """fcntl.flock on a file in /memory, with a timeout. A platform without fcntl degrades to no lock."""

    def __init__(self, path, timeout=60.0):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.fh = open(self.path, "a+b")
            end = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() >= end:
                        log.info("libs/install: lock %s busy, going on without it" % os.path.basename(self.path))
                        return self
                    time.sleep(0.3)
        except Exception:
            log.exception("libs/install: lock failed")
            return self

    def __exit__(self, *exc):
        try:
            if self.fh is not None:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
        except Exception:
            pass
        self.fh = None
        return False


def _env():
    """pip's environment: the arena proxy is kept (7.9), the model key is not passed on."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SystemRoot",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                    "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "PIP_INDEX_URL")}
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_CACHE_DIR"] = os.path.join(memory.root(), PIPCACHE_DIRNAME)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(args, timeout):
    """A subprocess whose output can never reach our stdout. Returns (returncode, tail)."""
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=_env(), close_fds=True)
    except subprocess.TimeoutExpired:
        return 124, "timeout after %.0fs" % timeout
    except Exception as e:
        return 125, "%s: %s" % (type(e).__name__, e)
    tail = (p.stdout or b"").decode("utf-8", "replace").strip()
    return p.returncode, tail[-1200:]


def probe(import_names, path=""):
    """{import name: True/False}: does it import in a fresh, isolated subprocess?"""
    names = [str(n) for n in import_names if n]
    if not names:
        return {}
    code = ("import importlib, json, site, sys\n"
            "sys.path.extend(site.getsitepackages())\n"
            "p = %r\n"
            "if p: sys.path.insert(0, p)\n"
            "out = {}\n"
            "for n in %r:\n"
            "    try:\n"
            "        importlib.import_module(n)\n"
            "        out[n] = True\n"
            "    except BaseException:\n"
            "        out[n] = False\n"
            "sys.stderr.write(json.dumps(out))\n" % (path, names))
    rc, tail = _run([sys.executable, "-I", "-S", "-B", "-c", code], PROBE_TIMEOUT_S)
    for line in reversed(tail.splitlines()):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return {n: bool(obj.get(n)) for n in names}
        except ValueError:
            continue
    log.info("libs/install: import probe failed (rc=%s) %s" % (rc, tail[-200:]))
    return {n: False for n in names}


def _ranked(libs, order):
    """The libraries that work, in the order discovery ranked them: the best fit is used first."""
    return sorted(libs, key=lambda l: order.get(l.name, 999))


def _spec(lib):
    return "%s==%s" % (lib.name, lib.version) if lib.version else lib.name


def _pip_install(specs, target, deadline, no_deps, offline):
    args = [sys.executable, "-m", "pip", "install", "--target", target, "--only-binary=:all:", "--no-input",
            "--disable-pip-version-check", "--no-color", "--timeout", "20", "--retries", "1", "-q",
            "--no-warn-script-location"]
    if os.path.isdir(WHEELHOUSE):
        args += ["--find-links", WHEELHOUSE]
    if offline:
        args += ["--no-index"]
    if no_deps:
        args += ["--no-deps"]
    args += list(specs)
    timeout = min(PIP_TIMEOUT_S, max(0.0, deadline - time.monotonic()))
    if timeout < MIN_INSTALL_S:
        return 126, "no time left for pip"
    t0 = time.monotonic()
    rc, tail = _run(args, timeout)
    log.info("libs/install: pip %s%s%s -> rc=%s in %.1fs%s" % (
        " ".join(specs), " (offline)" if offline else "", " (no-deps)" if no_deps else "", rc,
        time.monotonic() - t0, (" | " + tail[-300:]) if rc else ""))
    return rc, tail


def ensure(libs, deadline):
    """Make `libs` importable. Returns (path, ok_libs, notes).

    path is "" when nothing extra is needed (everything is baked into the image); otherwise the directory the
    job must put on sys.path. ok_libs are the libraries that really import. Never raises.
    """
    libs = list(libs or ())
    if not libs:
        return "", [], []
    notes = []
    try:
        # 1. what is already importable in the image? (baked libraries cost nothing)
        have = probe([l.import_name for l in libs])
        baked_ok = [l for l in libs if have.get(l.import_name)]
        missing = [l for l in libs if not have.get(l.import_name)]
        for l in baked_ok:
            notes.append("%s: already in the image" % l.name)
        order = dict((l.name, i) for i, l in enumerate(libs))        # discovery rank, not "baked first"
        if not missing:
            return "", baked_ok, notes
        if os.environ.get("BOT_LIBS_INSTALL", "on").strip().lower() in ("0", "off", "false", "no"):
            return "", baked_ok, notes + ["BOT_LIBS_INSTALL=off: %s not installed" % ",".join(l.name for l in missing)]

        path = bundle_dir(missing)
        if is_ready(path, missing):
            ok = probe([l.import_name for l in missing], path)
            got = [l for l in missing if ok.get(l.import_name)]
            notes.append("bundle %s from /memory" % bundle_id(missing))
            return (path if got else ""), _ranked(baked_ok + got, order), notes

        with _Lock(os.path.join(root(), ".lock-" + bundle_id(missing))):
            if is_ready(path, missing):                       # another container published it while we waited
                ok = probe([l.import_name for l in missing], path)
                got = [l for l in missing if ok.get(l.import_name)]
                return (path if got else ""), _ranked(baked_ok + got, order), notes + ["bundle published by another container"]
            tmp = os.path.join(root(), ".tmp-%s-%d" % (bundle_id(missing), os.getpid()))
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp, exist_ok=True)
            specs = [_spec(l) for l in missing]
            # dependencies of a catalog row are baked, so --no-deps keeps the download to the library itself
            no_deps = all(l.source in ("catalog", "baked") for l in missing)
            # The wheelhouse baked into the image first: in the arena that is the only source we are sure of.
            # BOT_LIBS_NETWORK=off means no network for the subsystem at all, pip included.
            from . import net
            online = net.enabled()
            rc, _tail = _pip_install(specs, tmp, deadline, no_deps=no_deps, offline=True)
            if rc != 0 and online:
                rc, _tail = _pip_install(specs, tmp, deadline, no_deps=no_deps, offline=False)
            if rc != 0 and no_deps:
                rc, _tail = _pip_install(specs, tmp, deadline, no_deps=False, offline=not online)
            if rc != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                return "", baked_ok, notes + ["pip failed for %s (rc=%s)" % (",".join(specs), rc)]
            ok = probe([l.import_name for l in missing], tmp)
            got = [l for l in missing if ok.get(l.import_name)]
            short = [l for l in missing if not ok.get(l.import_name)]
            if short and no_deps and time.monotonic() < deadline - MIN_INSTALL_S:
                # it installed but does not import: a dependency of it is not in the image after all
                rc, _tail = _pip_install([_spec(l) for l in short], tmp, deadline, no_deps=False,
                                         offline=not online)
                if rc == 0:
                    ok = probe([l.import_name for l in missing], tmp)
                    got = [l for l in missing if ok.get(l.import_name)]
                    notes.append("%s needed its dependencies after all" % ",".join(l.name for l in short))
            if not got:
                shutil.rmtree(tmp, ignore_errors=True)
                return "", baked_ok, notes + ["%s installed but does not import" % ",".join(specs)]
            memory.write_json_atomic(os.path.join(tmp, READY), {
                "libs": ["%s==%s" % (l.name, l.version or "latest") for l in got],
                "python": "%d.%d" % sys.version_info[:2], "when": int(time.time())})
            try:
                os.makedirs(root(), exist_ok=True)
                os.replace(tmp, path)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                if not is_ready(path, got):
                    return "", baked_ok, notes + ["could not publish the bundle"]
            notes.append("installed %s (%.1f MB of wheels)" % (
                ",".join(_spec(l) for l in got), sum(l.size_mb for l in got)))
            return path, _ranked(baked_ok + got, order), notes
    except Exception:
        log.exception("libs/install: ensure failed")
        return "", [], notes + ["install crashed"]


def cleanup_tmp(max_age_s=3600.0):
    """Remove temporary install directories a dead container left behind. Never raises."""
    try:
        for name in os.listdir(root()):
            if not name.startswith(".tmp-"):
                continue
            p = os.path.join(root(), name)
            if time.time() - os.path.getmtime(p) > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass
