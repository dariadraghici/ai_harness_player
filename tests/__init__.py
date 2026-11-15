"""Tests (stdlib unittest). All run with the fake LLM: free and offline.

Run from the repository root: python -m unittest   (python3 on Linux/macOS)
"""

import os

os.environ["BOT_FAKE_LLM"] = "1"           # must be set before bot.config is imported
os.environ.setdefault("BOT_LOG_LEVEL", "warning")

# The engine subsystem (bot/libs/) installs packages and reads documentation from the network. A test suite does
# neither: it is off here and the tests of tests/test_libs.py switch on exactly the stage they exercise, with pip
# and the registry mocked.
os.environ.setdefault("BOT_ENGINE_SUBSYSTEM", "off")
os.environ.setdefault("BOT_LIBS_NETWORK", "off")
os.environ.setdefault("BOT_LIBS_INSTALL", "off")
