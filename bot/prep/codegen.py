"""Simulator generation by the model.

Owner: luckydevs-3. Status: stub.

Done means: during prep, ask Haiku (thinking enabled; prep spend is free) to
write several candidate simulator modules that follow the contract in
bot/interfaces.py (SIMULATOR_FUNCTIONS), in parallel; write each candidate
to <memory>/<game_id>/<rules_hash>/candidates/sim_<n>.py; feed verification
failures back to the model for repair; return the paths.
"""


def generate_simulators(spec, deadline, n_candidates=3):
    """Return a list of file paths of candidate simulators. Stub."""
    # TODO(luckydevs-3): implement.
    raise NotImplementedError("simulator generation not implemented yet")


def repair_simulator(spec, path, failure_report, deadline):
    """Ask the model to fix one candidate given a verify failure report. Stub."""
    raise NotImplementedError("simulator repair not implemented yet")
