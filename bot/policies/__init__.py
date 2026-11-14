"""Small deterministic policy registry used before simulator/model fallback."""

from . import allocation, generic, wordle


POLICIES = (wordle, allocation, generic)


def choose(rules_text, sample_turn=None):
    scored = [(float(policy.can_handle(rules_text, sample_turn)), policy)
              for policy in POLICIES]
    return max(scored, key=lambda item: item[0])[1]
