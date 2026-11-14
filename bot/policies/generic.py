"""Grammar-first fallback policy for variants without a specialized solver."""


def can_handle(rules_text, sample_turn=None):
    return 0.1


def prep(ctx):
    return {}


def decide(state, deadline):
    legal = state.get("legal_actions") if isinstance(state, dict) else None
    return [{"action": str(action), "score": 0.0} for action in (legal or [])]
