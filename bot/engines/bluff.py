"""Bluff: Monte Carlo over hidden states.

Owner: luckydevs-3. Status: stub.

Done means: sample hidden states consistent with what we saw, expected value per action, mixed play so we are not readable; the opponent's chat may shift the beliefs.
"""

from .base import BaseEngine, register


class BluffEngine(BaseEngine):
    family = "bluff"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-3): implement. Must return list[Candidate] sorted best first,
        # use only specialist.simulator_path through prep/sandbox.py (killable
        # child process) and stop before deadline (time.monotonic()).
        raise NotImplementedError("bluff engine not implemented yet")


register(BluffEngine())
