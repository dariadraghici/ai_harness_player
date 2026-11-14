"""Loader of the prep prompt templates in bot/prep/prompts/*.txt.

Owner: luckydevs-3. Status: real.

Templates are plain text with {{NAME}} placeholders (same syntax as
bot/chat/decide.py fill()). They are .txt files inside bot/ so they reach the
image (.dockerignore drops *.md).
"""

import os
import threading

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_cache = {}
_lock = threading.Lock()


def load(name):
    with _lock:
        if name not in _cache:
            with open(os.path.join(_DIR, name), "r", encoding="utf-8") as f:
                _cache[name] = f.read()
        return _cache[name]


def render(name, **values):
    text = load(name)
    for k, v in values.items():
        text = text.replace("{{%s}}" % k, str(v))
    return text
