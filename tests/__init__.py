"""Tests (stdlib unittest). All run with the fake LLM: free and offline.

Run from the repository root: python -m unittest   (python3 on Linux/macOS)
"""

import os

os.environ["BOT_FAKE_LLM"] = "1"           # must be set before bot.config is imported
os.environ.setdefault("BOT_LOG_LEVEL", "warning")
