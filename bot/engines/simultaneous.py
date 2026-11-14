"""Read the opponent: mixed strategy plus opponent model.

Owner: luckydevs-3. Status: stub.

Done means: payoff matrix from the simulator, equilibrium by regret matching, frequency model of this opponent's past moves, capped exploitation; returns candidates whose value is the mixed-strategy weight.
"""

from .base import BaseEngine, register


class SimultaneousEngine(BaseEngine):
    family = "read_opponent"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-3): implement. Must return list[Candidate] sorted best first,
        # use only specialist.simulator_path through prep/sandbox.py (killable
        # child process) and stop before deadline (time.monotonic()).
        raise NotImplementedError("read_opponent engine not implemented yet")


register(SimultaneousEngine())
