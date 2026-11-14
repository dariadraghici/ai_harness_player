"""Think ahead: alpha-beta / MCTS on the generated simulator.

Owner: luckydevs-3. Status: stub.

Done means: iterative deepening alpha-beta with a time cap, MCTS when the branching is large, small games solved completely and cached in /memory; returns ranked candidates with values in [-1, 1].
"""

from .base import BaseEngine, register


class SearchEngine(BaseEngine):
    family = "think_ahead"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-3): implement. Must return list[Candidate] sorted best first,
        # use only specialist.simulator_path through prep/sandbox.py (killable
        # child process) and stop before deadline (time.monotonic()).
        raise NotImplementedError("think_ahead engine not implemented yet")


register(SearchEngine())
