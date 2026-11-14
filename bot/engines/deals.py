"""Deals: negotiation and auction policy.

Owner: luckydevs-2. Status: stub.

Done means: code computes our value, reservation price and concession schedule, and decides accept or reject; the model only writes the words inside those limits.
"""

from .base import BaseEngine, register


class DealsEngine(BaseEngine):
    family = "deals"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-2): implement. Must return list[Candidate] sorted best first,
        # use only specialist.simulator_path through prep/sandbox.py (killable
        # child process) and stop before deadline (time.monotonic()).
        raise NotImplementedError("deals engine not implemented yet")


register(DealsEngine())
