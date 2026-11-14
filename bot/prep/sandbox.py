"""Runs generated code in a child process with a timeout.

Owner: luckydevs-3. Status: stub.

Done means: call_simulator() starts `python -I` in a child process (so it can
be killed; a thread cannot), loads the simulator file, calls one function
with JSON arguments, returns the JSON result, kills the child at the
timeout, caps memory (resource.setrlimit on Linux), sends the child's stdout
to stderr so it can never touch the protocol, and keeps the process count
low (container limit: 256 processes; reuse one worker per engine).
"""


class SandboxError(Exception):
    pass


def call_simulator(path, function, args, timeout):
    """Call function(*args) of the simulator at path in a child process. Stub."""
    # TODO(luckydevs-3): implement.
    raise NotImplementedError("sandbox not implemented yet")
