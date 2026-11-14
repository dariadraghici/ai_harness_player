"""One object per game variant: what prep produced, loaded per variant.

Owner: luckydevs-3. Status: real (base: profile + playbook; simulators later).

Lookup order at start: this process -> /memory (another container of ours may
have prepared it) -> a quick cold prep of that single variant inside the study
clock -> a default spec (rules only) if even that fails.
"""

import threading
import time

from . import config, log, memory
from .interfaces import GameProfile, VariantSpec
from .prep import classify

_lock = threading.Lock()
_registry = {}      # (game_id, rules_hash) -> VariantSpec


def put(spec):
    with _lock:
        _registry[(spec.game_id, spec.rules_hash)] = spec


def get(game_id, rhash):
    with _lock:
        return _registry.get((game_id, rhash))


def all_specs():
    with _lock:
        return list(_registry.values())


def lookup(game_id, rules):
    """In process, else memory. None if not found."""
    rhash = memory.rules_hash(rules)
    spec = get(game_id, rhash)
    if spec:
        return spec
    spec = memory.load_variant(game_id, rhash, rules)
    if spec:
        put(spec)
        log.info("specialist %s/%s loaded from memory" % (game_id, rhash))
    return spec


def build_variant(game_id, game_name, label, rules, sample_turn, deadline, thinking=True, source="prep"):
    """Build (classify + playbook) and store one variant. Never raises; returns a VariantSpec.

    EXTENSION POINT (luckydevs-3): after classify, generate simulators
    (prep/codegen.py), verify them (prep/verify.py), and set
    spec.simulator_path / spec.verified only when verification passed.
    """
    rhash = memory.rules_hash(rules)
    spec = VariantSpec(game_id=game_id, game_name=game_name or "", label=label or "",
                       rules=rules or "", rules_hash=rhash, sample_turn=sample_turn, source=source)
    try:
        timeout = min(config.PREP_CALL_TIMEOUT_S, deadline - time.monotonic() - 2.0)
        if timeout > 2.0:
            spec.profile, spec.playbook = classify.classify_variant(
                game_name, label, rules, sample_turn, timeout, thinking=thinking)
        else:
            spec.profile = classify._default_profile(sample_turn, rules)
            spec.source = "default"
        # ---- EXTENSION POINT luckydevs-3: simulator generation + verification here.
        memory.save_variant(spec)
    except Exception:
        log.exception("build_variant failed for %s/%s" % (game_id, label))
        if not isinstance(spec.profile, GameProfile):
            spec.profile = GameProfile()
    put(spec)
    log.info("specialist %s/%s (%s) family=%s example=%r source=%s" % (
        game_id, rhash, label, spec.profile.family, spec.profile.example_action[:40], spec.source))
    return spec


def for_start(start_msg, deadline):
    """Find or build the specialist for the drawn variant of a start message."""
    game_id = str(start_msg.get("game_id") or "unknown")
    rules = start_msg.get("rules") or ""
    variant = start_msg.get("variant") or {}
    label = variant.get("label", "") if isinstance(variant, dict) else str(variant)
    spec = lookup(game_id, rules)
    if spec:
        if not spec.rules:
            spec.rules = rules
        return spec
    # Cold: classify this single variant within the study clock. Keep some
    # time for the opening chat. Thinking only when the clock is long.
    left = deadline - time.monotonic()
    log.info("cold prep of %s/%s with %.0fs left" % (game_id, label, left))
    return build_variant(game_id, start_msg.get("game_name", ""), label, rules, None,
                         deadline - min(6.0, left * 0.25), thinking=left > 45, source="cold")
