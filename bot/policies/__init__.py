"""Small deterministic policy registry used before simulator/model fallback."""

from . import (allocation, battleship, bulls_cows, dots_boxes, generic, hex,
               mini_chess, nim, pig, prisoners, rps, wordle)


POLICIES = (wordle, allocation, nim, bulls_cows, pig, battleship, prisoners, rps,
            dots_boxes, hex, mini_chess, generic)


def choose(rules_text, sample_turn=None):
    scored = [(float(policy.can_handle(rules_text, sample_turn)), policy)
              for policy in POLICIES]
    return max(scored, key=lambda item: item[0])[1]


def specialized(rules_text, sample_turn=None):
    policy = choose(rules_text, sample_turn)
    return policy is not generic
