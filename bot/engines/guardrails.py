"""Guardrails: forbidden-subject filter on everything we output.

Owner: luckydevs-2. Status: stub (pass-through filter with the final signature).

Every action and chat line we send passes through filter_outgoing(), including
the chat sent with ready at start and the watchdog's fallback answers. In
Guardrails games the engine checks our chat too, and a wrong line can end the
game on the spot.

Done means: prep extracts the forbidden subject and its synonyms from the
rules (extract_forbidden) into profile.speech_restrictions; filter_outgoing
blocks any text that mentions them and returns a safe rewrite (never empty
for chat); it is fast (pure code, no model call) and never raises.
"""

from .base import BaseEngine, register


def extract_forbidden(rules_text):
    """Return a list of forbidden words or phrases found in the rules. Stub."""
    # TODO(luckydevs-2): extract in prep (model call allowed there) and store in the profile.
    return []


def filter_outgoing(text, kind="chat", profile=None):
    """Filter one outgoing string. kind is "chat" or "action".

    Must return a string. For kind="chat" the result may be rewritten; for
    kind="action" a blocked action should be reported by returning "" so the
    ladder can pick another legal action.
    """
    # TODO(luckydevs-2): implement the forbidden-subject check.
    return text if isinstance(text, str) else ""


class GuardrailsEngine(BaseEngine):
    family = "guardrails"

    def candidates(self, specialist, turn_msg, deadline):
        # Guardrails games are mostly answered by the model (L2) with the
        # outgoing filter on top. A dedicated engine is optional.
        raise NotImplementedError("guardrails engine not implemented yet")


register(GuardrailsEngine())
