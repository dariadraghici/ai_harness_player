"""All constants and BOT_* settings in one place.

Shared file (owner: Sebi). Change only with permission, see RULES.md.

In the local practice arena only LLM_* and BOT_* variables reach the bot,
so every setting of our own is named BOT_*.
"""

import os

HARNESS_VERSION = "0.1.0"

# ---------------------------------------------------------------- clocks
# Defaults from the arena contract (docs/ARENA-CONTRACT.md 2.2). When a message
# carries a different clock, hard deadlines keep the same proportion.
PREP_CLOCK_S = 600
PREP_HARD_S = 510          # latest moment we answer ready to prep

START_WARM_CLOCK_S = 30
START_WARM_HARD_S = 18     # latest ready when the specialist is prepared
START_COLD_CLOCK_S = 120
START_COLD_HARD_S = 95     # latest ready for a cold start

TURN_CLOCK_S = 20
TURN_SOFT_S = 6            # target time for a move (answer time is a tie-break)
TURN_HARD_S = 13           # the watchdog sends the best move found so far

LLM_MATCH_TIMEOUT_S = 8    # timeout of a model call during a match

# ---------------------------------------------------------------- model
GATEWAY_BASE_URL = "https://llm.hackaton-2026.dvloper.io/v1"  # the ONLY model endpoint
GATEWAY_MODEL = "haiku"                                       # the ONLY model
PRICE_INPUT_PER_TOKEN = 0.10 / 1e6
PRICE_OUTPUT_PER_TOKEN = 0.50 / 1e6
PRICE_CACHE_READ_PER_TOKEN = 0.01 / 1e6

DECIDE_MAX_TOKENS = 400
OPENING_MAX_TOKENS = 120
PREP_THINKING_BUDGET = 2000
PREP_MAX_TOKENS = PREP_THINKING_BUDGET + 2500
PREP_CALL_TIMEOUT_S = 120
PREP_PARALLEL_VARIANTS = 4

# ---------------------------------------------------------------- decisions
CHAT_MAX_CHARS = 200
CHAT_FALLBACK = "Good game so far. Let us see how this plays out."
OPENING_FALLBACK = "Hello, good luck and have fun."
# L3: the model may only pick among candidates whose value is within this
# margin of the best candidate (absolute, in the engine's value units).
CANDIDATE_MARGIN = 0.05
# How many recent chat entries go into the decision prompt.
CHAT_HISTORY_LIMIT = 12


# ---------------------------------------------------------------- BOT_* env
def _env(name, default=""):
    return os.environ.get(name, default)


def env_flag(name):
    return _env(name).strip().lower() in ("1", "true", "yes", "on")


LOG_LEVEL = _env("BOT_LOG_LEVEL", "info").lower()      # debug, info, warning, error
FAKE_LLM = env_flag("BOT_FAKE_LLM")                    # 1 = no network, canned answers
FAKE_LLM_MODE = _env("BOT_FAKE_LLM_MODE", "ok").lower()  # ok, fail, hang
FAKE_LLM_DELAY_S = float(_env("BOT_FAKE_LLM_DELAY", "60") or 60)
MEMORY_DIR = _env("MEMORY_DIR", "/memory")
TEAM_ID = _env("TEAM_ID", "luckydevs")

# What the fake LLM builds in prep (canned profile and playbook) must never be
# loaded by a run with the real model, and the other way round: both use the
# same local memory folder in practice. A different version string keeps the
# two caches apart (bot/memory.py ignores variants of another version).
if FAKE_LLM:
    HARNESS_VERSION += "-fake"
