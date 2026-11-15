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

# The settings above must hold whatever the import order was. `python -m unittest` walks the whole repository
# before it imports this package, and a package of ours that imports bot.memory (bot/policies/__init__.py) drags
# bot.config in first - with no BOT_FAKE_LLM in the environment yet, so FAKE_LLM would be False for the whole
# suite and every fake model answer would turn into a real HTTP call. Re-reading the module fixes the order
# dependency once, here, instead of in every test.
import importlib
import sys

if "bot.config" in sys.modules:
    importlib.reload(sys.modules["bot.config"])
    if "bot.log" in sys.modules:
        importlib.reload(sys.modules["bot.log"])
