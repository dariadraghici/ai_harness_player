"""The /memory store: atomic writes, per-game folders, rules hash.

Owner: luckydevs-1. Status: real.

/memory persists for the whole event and is shared by ALL our containers at
the same time, so every write is atomic (temp file unique to this process and
thread, then os.replace). Layout:

  <root>/<game_id>/<rules_hash>/meta.json      harness version, label, time
  <root>/<game_id>/<rules_hash>/profile.json   GameProfile
  <root>/<game_id>/<rules_hash>/playbook.md    how to play, what to say
  <root>/<game_id>/<rules_hash>/sample_turn.json
  <root>/<game_id>/<rules_hash>/simulator.py   later (luckydevs-3)
  <root>/<game_id>/<rules_hash>/verify.json    later (luckydevs-3)
  <root>/opponents/<team_id>.json              opponent profiles
  <root>/metrics/<container_id>.jsonl          one file per container

A cached variant made by a different HARNESS_VERSION is ignored.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid

from . import config, log
from .interfaces import GameProfile, VariantSpec

_root = None
_root_lock = threading.Lock()


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".probe-%d-%s" % (os.getpid(), uuid.uuid4().hex[:6]))
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def root():
    """The memory root. Falls back to a temp folder if MEMORY_DIR is unusable."""
    global _root
    with _root_lock:
        if _root is None:
            want = os.environ.get("MEMORY_DIR") or config.MEMORY_DIR
            if _writable(want):
                _root = want
            else:
                _root = os.path.join(tempfile.gettempdir(), "luckydevs-memory")
                os.makedirs(_root, exist_ok=True)
                log.warning("memory dir %r not writable, using %r" % (want, _root))
        return _root


def reset_root_for_tests(path=None):
    global _root
    with _root_lock:
        _root = path


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "unknown")).strip("._")
    return s[:80] or "unknown"


def rules_hash(rules):
    """Short SHA-256 of the normalized rules text."""
    text = str(rules or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ atomic io
def write_bytes_atomic(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%d.%s.tmp" % (path, os.getpid(), threading.get_ident(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_text_atomic(path, text):
    write_bytes_atomic(path, str(text).encode("utf-8"))


def write_json_atomic(path, obj):
    write_text_atomic(path, json.dumps(obj, indent=1, ensure_ascii=True, sort_keys=True))


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("cannot read %s: %s" % (path, e))
        return default


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def append_jsonl(path, obj):
    """Append one line. Use only for files owned by this container."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception as e:
        log.warning("cannot append %s: %s" % (path, e))


# ------------------------------------------------------------------ variants
def variant_dir(game_id, rhash):
    return os.path.join(root(), safe_name(game_id), safe_name(rhash))


def save_variant(spec):
    """Store what prep built for one variant. Never raises."""
    try:
        d = variant_dir(spec.game_id, spec.rules_hash)
        write_text_atomic(os.path.join(d, "playbook.md"), spec.playbook or "")
        write_json_atomic(os.path.join(d, "profile.json"), spec.profile.to_dict())
        if spec.sample_turn is not None:
            write_json_atomic(os.path.join(d, "sample_turn.json"), spec.sample_turn)
        # meta.json last: a reader treats a variant as present only when meta exists.
        write_json_atomic(os.path.join(d, "meta.json"), {
            "harness_version": config.HARNESS_VERSION,
            "game_id": spec.game_id, "game_name": spec.game_name,
            "label": spec.label, "rules_hash": spec.rules_hash,
            "verified": bool(spec.verified), "simulator_path": spec.simulator_path,
        })
        return True
    except Exception:
        log.exception("save_variant failed")
        return False


def load_variant(game_id, rhash, rules=""):
    """Return a VariantSpec from memory, or None if missing or from another harness version."""
    try:
        d = variant_dir(game_id, rhash)
        meta = read_json(os.path.join(d, "meta.json"))
        if not meta:
            return None
        if meta.get("harness_version") != config.HARNESS_VERSION:
            log.info("cached variant %s/%s ignored: harness %s != %s" % (
                game_id, rhash, meta.get("harness_version"), config.HARNESS_VERSION))
            return None
        return VariantSpec(
            game_id=game_id, game_name=meta.get("game_name", ""),
            label=meta.get("label", ""), rules=rules, rules_hash=rhash,
            profile=GameProfile.from_dict(read_json(os.path.join(d, "profile.json"), {})),
            playbook=read_text(os.path.join(d, "playbook.md")),
            sample_turn=read_json(os.path.join(d, "sample_turn.json")),
            simulator_path=meta.get("simulator_path"),
            verified=bool(meta.get("verified")),
            source="memory",
        )
    except Exception:
        log.exception("load_variant failed")
        return None


def find_variant_by_label(game_id, label, rules=""):
    """A stored variant of this game with this label, whatever its rules hash. None if none.

    The rules text of a real game can differ from the text prep saw (the rules
    may embed that game's deal: a position, a document, a prize order), so the
    rules hash of `start` can miss a prepared variant. The label still names
    the variant. Prefers a verified variant, then one with a playbook, then the
    newest. The result carries `rules` (the caller's text), not the stored one.
    """
    try:
        if not label:
            return None
        gdir = os.path.join(root(), safe_name(game_id))
        best = None
        for name in os.listdir(gdir):
            mpath = os.path.join(gdir, name, "meta.json")
            if not os.path.isfile(mpath):
                continue
            meta = read_json(mpath)
            if not meta or meta.get("harness_version") != config.HARNESS_VERSION:
                continue
            if str(meta.get("label") or "") != str(label):
                continue
            try:
                has_playbook = os.path.getsize(os.path.join(gdir, name, "playbook.md")) > 0
            except OSError:
                has_playbook = False
            rank = (bool(meta.get("verified")), has_playbook, os.path.getmtime(mpath))
            if best is None or rank > best[0]:
                best = (rank, name)
        return load_variant(game_id, best[1], rules) if best else None
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("find_variant_by_label failed")
        return None


# ------------------------------------------------------------------ opponents
def opponent_path(team_id):
    return os.path.join(root(), "opponents", safe_name(team_id) + ".json")


def metrics_path(container_id):
    return os.path.join(root(), "metrics", safe_name(container_id) + ".jsonl")
