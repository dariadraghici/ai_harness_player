"""Truth: answer only with support from the document.

Owner: luckydevs-2. Status: stub.

In Truth games an invented answer costs more than "unknown". Done means:
prep stores the exact "unknown" answer the rules define (in the profile);
candidates() finds a supporting quote in the document from the observation
for each possible answer and returns only supported answers, otherwise the
exact "unknown" answer, with the quote in Candidate.note.
"""

from .base import BaseEngine, register


def find_support(document, answer):
    """Return a quote from document that supports answer, or None. Stub."""
    # TODO(luckydevs-2): implement (pure code search first, model check second).
    return None


class TruthEngine(BaseEngine):
    family = "truth"

    def candidates(self, specialist, turn_msg, deadline):
        # TODO(luckydevs-2): implement. Note: does not need a simulator, so the
        # ladder should allow it without specialist.verified (propose in hand-over).
        raise NotImplementedError("truth engine not implemented yet")


register(TruthEngine())
