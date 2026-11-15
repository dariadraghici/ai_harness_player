"""A sandbox job that reports the child's environment (used by tests/test_libs.py). Standalone."""

import os

KEYS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
        "MALLOC_ARENA_MAX", "LLM_API_KEY", "TEAM_ID")


def run(sim, payload):
    return dict((k, os.environ[k]) for k in KEYS if k in os.environ)
