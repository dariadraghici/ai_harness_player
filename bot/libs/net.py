"""The ONE network door of the engine subsystem (rulebook 7.9 / 7.11).

Owner: luckydevs-3 area. Status: real. Standard library only (urllib).

Every rule of the contract is enforced here, in code, so that a reviewer has one file to read:

  1. GET only. No request body, no POST/PUT/DELETE: `_open` builds the request with method="GET" and data=None.
  2. Domain allowlist. Only the hosts in ALLOWLIST (package registry, source hosts, documentation hosts) are
     opened, and a redirect to any other host is refused (`_Redirect`).
  3. NO GAME CONTENT EVER LEAVES. A URL may only be built here, from a package name or from phrases of the
     fixed capability vocabulary (bot/libs/capabilities.py). `query()` refuses any term that is not in that
     vocabulary, so rules, observations, chat and anything derived from them cannot reach a query string.
     The game's name is not a capability, so by default it is not allowed either (ALLOW_GAME_NAME_IN_QUERIES).
  4. Read-only, bounded: every call has a timeout and a byte cap, and never raises.
  5. The arena's proxy is used as it is: the default opener reads HTTP_PROXY / HTTPS_PROXY / NO_PROXY from the
     environment and nothing here unsets them. The model gateway is NOT reachable from here (it is not on the
     allowlist, and bot/llm.py has its own direct opener).
  6. Nothing is written to stdout: failures go to bot/log.py (stderr).

Switches: BOT_LIBS_NETWORK=off blocks every request (`enabled()`), BOT_LIBS_ALLOWLIST adds comma-separated
host suffixes, BOT_ALLOW_GAME_NAME_IN_QUERIES=on lifts rule 3's last sentence (off by default).
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import log
from . import capabilities

USER_AGENT = "luckydevs-harness/1.0 (+arena practice bot; generic library discovery)"

# Host suffixes we may open. Package registry, source hosting, documentation.
ALLOWLIST = (
    "pypi.org",
    "files.pythonhosted.org",
    "github.com",
    "raw.githubusercontent.com",
    "readthedocs.io",
    "readthedocs.org",
    "docs.python.org",
    "scipy.org",
    "numpy.org",
    "networkx.org",
)
TIMEOUT_S = 8.0
MAX_BYTES = 1 << 20              # 1 MiB per document
REGISTRY_MAX_BYTES = 24 << 20    # the registry answer of a popular package carries every release (numpy: 3.7 MB)
REGISTRY_TIMEOUT_S = 14.0
MAX_CALLS = 60                   # per process, a hard stop on any loop that goes wrong
_calls = [0]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


class NetBlocked(Exception):
    """A request was refused by the guard. Carries the reason for the log and the tests."""


def enabled():
    return os.environ.get("BOT_LIBS_NETWORK", "on").strip().lower() not in ("0", "off", "false", "no", "none")


def allow_game_name():
    return os.environ.get("BOT_ALLOW_GAME_NAME_IN_QUERIES", "off").strip().lower() in ("1", "on", "true", "yes")


def allowlist():
    extra = [h.strip().lower() for h in os.environ.get("BOT_LIBS_ALLOWLIST", "").split(",") if h.strip()]
    return tuple(ALLOWLIST) + tuple(extra)


def host_allowed(host):
    host = (host or "").strip().lower().rstrip(".")
    if "@" in host or not host:
        return False
    host = host.split(":")[0]
    for suffix in allowlist():
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def check_url(url):
    """(ok, reason) for a URL, without opening anything. Pure: the tests use it directly."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False, "unparsable url"
    if parts.scheme not in ("http", "https"):
        return False, "scheme %r is not http(s)" % parts.scheme
    if parts.username or parts.password:
        return False, "credentials in the url"
    if not host_allowed(parts.hostname):
        return False, "host %r is not on the allowlist" % (parts.hostname,)
    return True, ""


class _Redirect(urllib.request.HTTPRedirectHandler):
    """A redirect may not leave the allowlist, and may not turn into another method."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = check_url(newurl)
        if not ok:
            log.warning("libs/net: redirect refused (%s)" % reason)
            return None
        new = urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        if new is not None and new.get_method() != "GET":
            return None
        return new


# The default opener reads the proxy from the environment: that is what the arena wants (7.9).
_opener = urllib.request.build_opener(_Redirect())


def _open(url, timeout, max_bytes):
    ok, reason = check_url(url)
    if not ok:
        raise NetBlocked(reason)
    if not enabled():
        raise NetBlocked("BOT_LIBS_NETWORK=off")
    if _calls[0] >= MAX_CALLS:
        raise NetBlocked("call budget of %d requests is spent" % MAX_CALLS)
    _calls[0] += 1
    req = urllib.request.Request(url, data=None, method="GET",
                                 headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    t0 = time.monotonic()
    with _opener.open(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise NetBlocked("answer larger than %d bytes" % max_bytes)
    log.debug("libs/net: GET %s -> %d bytes in %.2fs" % (url[:120], len(data), time.monotonic() - t0))
    return data


def get_bytes(url, timeout=TIMEOUT_S, max_bytes=MAX_BYTES):
    """Raw bytes of a GET, or None. Never raises."""
    try:
        return _open(url, timeout, max_bytes)
    except NetBlocked as e:
        log.info("libs/net: blocked %s (%s)" % (str(url)[:100], e))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.info("libs/net: GET failed %s (%s: %s)" % (str(url)[:100], type(e).__name__, e))
    except Exception:
        log.exception("libs/net: unexpected failure")
    return None


def get_json(url, timeout=TIMEOUT_S, max_bytes=MAX_BYTES):
    data = get_bytes(url, timeout, max_bytes)
    if not data:
        return None
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        log.info("libs/net: not JSON: %s" % str(url)[:100])
        return None
    return obj if isinstance(obj, (dict, list)) else None


def get_text(url, timeout=TIMEOUT_S, max_bytes=MAX_BYTES):
    """Plain text of a document, HTML stripped. UNTRUSTED: see bot/libs/docs.py."""
    data = get_bytes(url, timeout, max_bytes)
    if not data:
        return ""
    return strip_html(data.decode("utf-8", "replace"))


def strip_html(text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", str(text or ""))
    text = _TAG_RE.sub(" ", text)
    for entity, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                       ("&#39;", "'")):
        text = text.replace(entity, ch)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------- queries (rule 3)
def terms_ok(terms):
    """(ok, reason): every term must be a phrase of the fixed capability vocabulary."""
    vocabulary = capabilities.all_terms()
    for t in terms:
        if not isinstance(t, str) or " ".join(t.split()).lower() not in vocabulary:
            return False, "term %r is not in the capability vocabulary" % (t,)
    return True, ""


def query(base, terms, field="q"):
    """A query URL built ONLY from vocabulary phrases, or None when a term is not in the vocabulary.

    There is deliberately no way to pass free text: a caller that wants to ask about a game cannot.
    """
    ok, reason = terms_ok(terms)
    if not ok:
        log.warning("libs/net: query refused (%s)" % reason)
        return None
    ok, reason = check_url(base)
    if not ok:
        log.warning("libs/net: query refused (%s)" % reason)
        return None
    return base + ("&" if urllib.parse.urlsplit(base).query else "?") + urllib.parse.urlencode(
        {field: " ".join(" ".join(t.split()) for t in terms)})


def pypi_json(name):
    """The PyPI JSON API of one package (never the search page). None when it does not exist."""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,60}[A-Za-z0-9])?", str(name or "")):
        log.info("libs/net: refused package name %r" % (name,))
        return None
    return get_json("https://pypi.org/pypi/%s/json" % urllib.parse.quote(str(name), safe=""),
                    timeout=REGISTRY_TIMEOUT_S, max_bytes=REGISTRY_MAX_BYTES)


def calls_made():
    return _calls[0]


def reset_for_tests():
    _calls[0] = 0
