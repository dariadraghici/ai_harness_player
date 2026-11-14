"""One object per game variant: what prep produced, loaded per variant.

Owner: luckydevs-3. Status: real (base: profile + playbook; simulators later).

Lookup order at start: this process -> /memory (another container of ours may
have prepared it), both by the hash of the rules text -> the same game and
variant label when the rules text of this game differs from the one prep saw
(lookup_by_label) -> a quick cold prep of that single variant inside the study
clock -> a default spec (rules only) if even that fails.
"""

import dataclasses
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

    Writes analysis.json (classify) before memory.save_variant writes meta.json.
    Simulators are NOT generated here (see prep/pipeline.py phase B); set
    spec.simulator_path / spec.verified only when verification passed (later).
    """
    rhash = memory.rules_hash(rules)
    spec = VariantSpec(game_id=game_id, game_name=game_name or "", label=label or "",
                       rules=rules or "", rules_hash=rhash, sample_turn=sample_turn, source=source)
    try:
        timeout = min(config.PREP_CALL_TIMEOUT_S, deadline - time.monotonic() - 2.0)
        if timeout > 2.0:
            spec.profile, spec.playbook, analysis = classify.analyze_variant(
                game_name, label, rules, sample_turn, timeout, thinking=thinking)
        else:
            spec.profile, analysis = classify.evidence_profile(rules, sample_turn)
            spec.source = "default"
        # Simulator generation runs only in prep (prep/pipeline.py phase B), never
        # here: the cold path must stay fast.
        classify.write_analysis(game_id, rhash, analysis)   # before meta.json
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
    spec = lookup_by_label(game_id, label, rules)
    if spec:
        return spec
    # Cold: classify this single variant within the study clock. Keep some
    # time for the opening chat. Thinking only when the clock is long.
    left = deadline - time.monotonic()
    log.info("cold prep of %s/%s with %.0fs left" % (game_id, label, left))
    return build_variant(game_id, start_msg.get("game_name", ""), label, rules, None,
                         deadline - min(6.0, left * 0.25), thinking=left > 45, source="cold")


def lookup_by_label(game_id, label, rules):
    """The prepared spec of this game and variant label, re-targeted at these rules. None if none.

    Why: the rules text in `start` is the text of THIS game's deal. When a game
    embeds its setup in the rules (ARENA-CONTRACT, kit note K2), that text
    differs from the one prep saw, the rules hash misses, and without this
    lookup a prepared container would fall back to a cold prep inside the 30 s
    study clock and lose its playbook and simulator. The label names the same
    variant, so what prep built (profile, playbook, simulator) still applies;
    only the rules text is replaced by the current one. rules_hash stays the
    hash prep stored the variant under: it is the storage key of everything
    prep built (memory.variant_dir, simulator candidates, analysis), not the
    hash of the current text. Nothing is written to memory and the in-process
    registry is not grown.
    """
    try:
        if not label:
            return None
        with _lock:
            cands = [s for s in _registry.values() if s.game_id == game_id and s.label == label]
        if cands:
            base = max(cands, key=lambda s: (bool(s.verified), bool(s.playbook), s.source == "prep"))
        else:
            base = memory.find_variant_by_label(game_id, label, rules)
        if base is None:
            return None
        spec = dataclasses.replace(base, rules=rules or "", source="label")
        log.info("specialist %s/%s (%s) reused by label: the rules text of this game (hash %s) "
                 "differs from prep" % (game_id, base.rules_hash, label, memory.rules_hash(rules)))
        return spec
    except Exception:
        log.exception("lookup_by_label failed for %s/%s" % (game_id, label))
        return None
