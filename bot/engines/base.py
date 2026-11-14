"""Engine interface and registry: family -> engine.

Owner: luckydevs-3. Status: real (registry), engines are stubs.

The ladder calls get_engine(profile.family) and then
engine.candidates(specialist, turn_msg, deadline). An engine that is not ready
raises NotImplementedError or returns []; the ladder treats both as
"L3 not available" and goes down to L2.
"""

from typing import Dict, List, Optional

from ..interfaces import Candidate, Engine, VariantSpec  # noqa: F401  (re-exported)


class NotAvailable(Exception):
    """Raised by an engine that cannot help for this turn."""


class BaseEngine:
    family = "unknown"

    def candidates(self, specialist: VariantSpec, turn_msg: dict, deadline: float) -> List[Candidate]:
        raise NotImplementedError("%s engine not implemented" % self.family)


_REGISTRY: Dict[str, BaseEngine] = {}


def register(engine: BaseEngine):
    _REGISTRY[engine.family] = engine
    return engine


def get_engine(family: str) -> Optional[BaseEngine]:
    _load_all()
    return _REGISTRY.get(family)


_loaded = False


def _load_all():
    global _loaded
    if _loaded:
        return
    _loaded = True
    # Importing registers each engine. Guardrails and truth are filters /
    # answer policies used by other modules; they register an engine too.
    from . import bluff, deals, guardrails, guess, search, simultaneous, truth  # noqa: F401
