"""Tests of a generated simulator against sample turns.

Owner: luckydevs-3. Status: stub.

Done means: inside prep/sandbox.py (never in the bot process): the simulator
parses every stored sample_turn; its legal_actions equal the sample's
legal_actions (as sets of exact strings); random self-play from the parsed
state runs N games without exceptions or infinite loops; the result is
written to <memory>/<game_id>/<rules_hash>/verify.json, and spec.verified is
set only when everything passed.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class VerifyReport:
    passed: bool = False
    checks: List[str] = field(default_factory=list)     # names of checks that passed
    failures: List[str] = field(default_factory=list)   # human-readable, fed back to codegen
    seconds: float = 0.0


def verify_simulator(spec, path, deadline):
    """Return a VerifyReport for the simulator at path. Stub."""
    # TODO(luckydevs-3): implement.
    raise NotImplementedError("simulator verification not implemented yet")
