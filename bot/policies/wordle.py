"""Policy-registry adapter for the deterministic Wordle engine."""

from ..engines import wordle as engine


def can_handle(rules_text, sample_turn=None):
    return 1.0 if engine.is_wordle_rules(rules_text) else 0.0


def prep(ctx):
    return {}


def decide(state, deadline):
    spec = state.get("spec")
    turn = state.get("turn") or {}
    return [{"action": candidate.action, "score": candidate.value}
            for candidate in engine.candidates(spec, turn, deadline)]
