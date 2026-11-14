"""Guessing: hypothesis filtering.

Owner: luckydevs-3. Status: stub.

Done means: keep the set of answers still consistent with all feedback so far; rank guesses by how well they split that set (expected remaining size or entropy).
"""

from .base import BaseEngine, register


class GuessEngine(BaseEngine):
    family = "guessing"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-3): implement. Must return list[Candidate] sorted best first,
        # use only specialist.simulator_path through prep/sandbox.py (killable
        # child process) and stop before deadline (time.monotonic()).
        raise NotImplementedError("guessing engine not implemented yet")


register(GuessEngine())
