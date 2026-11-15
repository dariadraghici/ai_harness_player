"""Gateway client: key switching, thinking control, timeouts, spend tracking, fake mode.

Owner: luckydevs-1. Status: real.

Standard library only (urllib): the gateway is OpenAI-compatible, so a chat
completion is one POST to <base_url>/chat/completions. The ONLY model is the
gateway's "haiku"; no other endpoint may ever be configured here (RULES.md 17).

complete() never raises. On error, timeout or empty content it returns None
and logs the reason. The timeout is a hard wall-clock limit: the HTTP call
runs in a daemon thread and we stop waiting for it when the time is up.

Fake mode (BOT_FAKE_LLM=1): no network call, deterministic canned answers.
BOT_FAKE_LLM_MODE=fail returns None, =hang blocks for BOT_FAKE_LLM_DELAY
seconds ignoring the timeout (to test the watchdog).
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import config, log

_lock = threading.Lock()
_key = {
    "base_url": os.environ.get("LLM_BASE_URL") or config.GATEWAY_BASE_URL,
    "api_key": os.environ.get("LLM_API_KEY", ""),
    "model": os.environ.get("LLM_MODEL") or config.GATEWAY_MODEL,
}
# Per-key usage: key suffix -> counters. Never store or log the full key.
_usage = {}
# Most recent call stats for metrics recording.
_last_call = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cached_tokens": 0,
    "cost": 0.0,
    "tag": "",
    "failed": False,
}

# Direct opener bypassing all proxies for gateway model calls (ARENA-CONTRACT 2.1).
# Model calls go straight to the gateway regardless of HTTP_PROXY/HTTPS_PROXY.
_gateway_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get_gateway_opener():
    """Return opener configured to bypass proxies for direct gateway model calls."""
    return _gateway_opener


def get_general_opener():
    """Return opener for general traffic honoring HTTPS_PROXY and NO_PROXY."""
    return urllib.request.build_opener()


def _key_id(api_key):
    return ("..." + api_key[-4:]) if api_key else "none"


def set_key(llm_dict):
    """Switch to the key of the latest prep or start. Called on every prep and start."""
    if not isinstance(llm_dict, dict):
        return
    with _lock:
        if llm_dict.get("base_url"):
            _key["base_url"] = str(llm_dict["base_url"])
        if llm_dict.get("api_key"):
            _key["api_key"] = str(llm_dict["api_key"])
        if llm_dict.get("model"):
            _key["model"] = str(llm_dict["model"])
    log.info("llm key switched to %s model=%s" % (_key_id(_key["api_key"]), _key["model"]))


def current_key_id():
    return _key_id(_key["api_key"])


def usage(key_id=None):
    """Counters for one key (default: current) or all keys."""
    with _lock:
        if key_id is None:
            key_id = _key_id(_key["api_key"])
        if key_id == "all":
            return json.loads(json.dumps(_usage))
        return dict(_usage.get(key_id, {"calls": 0, "failures": 0, "input": 0,
                                         "output": 0, "cached": 0, "cost": 0.0}))


def last_call():
    """Return token breakdown and cost of the most recent model call."""
    with _lock:
        return dict(_last_call)


def pop_last_call():
    """Return token breakdown and cost of the most recent call, then reset."""
    with _lock:
        res = dict(_last_call)
        _last_call.update({
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cost": 0.0,
            "tag": "",
            "failed": False,
        })
        return res


# ------------------------------------------------------------------ prompt cache
# The system message of a decision (rules, playbook, tactics) is identical for every turn of a game, so the
# gateway can keep it in its prompt cache: a cache read costs 0.01 USD per million tokens instead of 0.10
# (ARENA-CONTRACT 2.6), and model cost is a scored tie-break. The gateway only caches a block that asks for it,
# so a plain string prompt is never cached - measured against the real gateway: two identical plain calls
# reported cached_tokens 0, the same calls with the block below reported a write and then a read of 4842 tokens.
# If a gateway ever refuses content blocks, the first failure switches this off for the process and the call is
# retried as plain text, so the worst case is one retry and never a lost turn.
_blocks_ok = [True]


def cache_enabled():
    return (os.environ.get("BOT_PROMPT_CACHE", "on").strip().lower() not in ("0", "off", "false", "no")
            and _blocks_ok[0])


def cache_prefix(messages):
    """The same messages with the system prefix marked for the prompt cache, or them unchanged.

    Only the LAST system message is marked (our prompts have exactly one) and only when it is long enough to
    be worth an entry. Never mutates the caller's list.
    """
    try:
        if not cache_enabled():
            return messages, False
        index = None
        for i, m in enumerate(messages or ()):
            if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str):
                index = i
        if index is None or len(messages[index]["content"]) < config.PROMPT_CACHE_MIN_CHARS:
            return messages, False
        out = list(messages)
        out[index] = {"role": "system",
                      "content": [{"type": "text", "text": messages[index]["content"],
                                   "cache_control": {"type": "ephemeral"}}]}
        return out, True
    except Exception:
        log.exception("llm: prompt cache marking failed")
        return messages, False


def _account(kid, u, failed=False, tag=""):
    with _lock:
        c = _usage.setdefault(kid, {"calls": 0, "failures": 0, "input": 0,
                                    "output": 0, "cached": 0, "cost": 0.0})
        c["calls"] += 1
        if failed:
            c["failures"] += 1
        prompt = int(u.get("prompt_tokens") or 0) if u else 0
        out = int(u.get("completion_tokens") or 0) if u else 0
        details = (u.get("prompt_tokens_details") or {}) if u else {}
        cached = int(u.get("cache_read_input_tokens") or details.get("cached_tokens") or 0) if u else 0
        written = int(u.get("cache_creation_input_tokens") or details.get("cache_creation_tokens")
                      or details.get("cache_write_tokens") or 0) if u else 0
        total = prompt + out
        cost = (max(prompt - cached - written, 0) * config.PRICE_INPUT_PER_TOKEN
                + cached * config.PRICE_CACHE_READ_PER_TOKEN
                + written * config.PRICE_CACHE_WRITE_PER_TOKEN
                + out * config.PRICE_OUTPUT_PER_TOKEN)
        if u:
            c["input"] += prompt
            c["output"] += out
            c["cached"] += cached
            c["written"] = c.get("written", 0) + written
            c["cost"] += cost

        _last_call["prompt_tokens"] = prompt
        _last_call["completion_tokens"] = out
        _last_call["total_tokens"] = total
        _last_call["cached_tokens"] = cached
        _last_call["cache_write_tokens"] = written
        _last_call["cost"] = cost
        _last_call["tag"] = str(tag or "")
        _last_call["failed"] = bool(failed)


def complete(messages, max_tokens=300, timeout=8.0, thinking_budget=None, tag="call"):
    """One chat completion. Returns the text or None. Never raises.

    thinking_budget: None -> thinking disabled (fast). An int -> thinking
    enabled with that budget (min 1024) and max_tokens raised to at least
    budget + 1500, as the contract requires.
    """
    try:
        return _complete(messages, max_tokens, timeout, thinking_budget, tag)
    except Exception:
        log.exception("llm.complete %s failed unexpectedly" % tag)
        return None


def _complete(messages, max_tokens, timeout, thinking_budget, tag):
    if timeout is None or timeout <= 0.2:
        log.warning("llm %s skipped: no time left" % tag)
        return None
    if thinking_budget is None:
        thinking = {"type": "disabled"}
    else:
        thinking_budget = max(1024, int(thinking_budget))
        thinking = {"type": "enabled", "budget_tokens": thinking_budget}
        max_tokens = max(int(max_tokens), thinking_budget + 1500)
    if log.is_debug():
        for m in messages:
            log.debug("llm %s prompt [%s]:\n%s" % (tag, m.get("role"), m.get("content")))

    with _lock:
        key = dict(_key)
    kid = _key_id(key["api_key"])

    if config.FAKE_LLM:
        return _fake(messages, tag, kid, timeout)

    if not key["api_key"]:
        log.warning("llm %s skipped: no api key" % tag)
        return None
    sent, cached_prefix = cache_prefix(messages)
    box = {}

    def worker(payload):
        try:
            req = urllib.request.Request(
                key["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": "Bearer " + key["api_key"],
                         "Content-Type": "application/json"})
            with _gateway_opener.open(req, timeout=timeout) as r:
                box["data"] = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                detail = ""
            box["error"] = "HTTP %s %s" % (e.code, detail)
            box["status"] = e.code
        except Exception as e:
            box["error"] = "%s: %s" % (type(e).__name__, e)

    def attempt(payload, budget):
        box.clear()
        th = threading.Thread(target=worker, args=(payload,), name="llm-" + tag, daemon=True)
        th.start()
        th.join(budget)
        return th.is_alive()

    t0 = time.monotonic()
    body = {"model": key["model"], "messages": sent, "max_tokens": int(max_tokens), "thinking": thinking}
    alive = attempt(body, timeout)
    if not alive and cached_prefix and box.get("status") in (400, 404, 415, 422, 500) :
        # this gateway does not want content blocks: never mark a prefix again and send it as plain text
        _blocks_ok[0] = False
        log.warning("llm %s: the gateway refused a cached prefix (%s), falling back to plain text" % (
            tag, box.get("status")))
        left = timeout - (time.monotonic() - t0)
        if left > 1.5:
            body = {"model": key["model"], "messages": messages, "max_tokens": int(max_tokens),
                    "thinking": thinking}
            alive = attempt(body, left)
    dt = time.monotonic() - t0
    if alive:
        log.warning("llm %s timed out after %.1fs" % (tag, dt))
        _account(kid, None, failed=True, tag=tag)
        return None
    if "error" in box:
        log.warning("llm %s failed after %.1fs: %s" % (tag, dt, box["error"]))
        _account(kid, None, failed=True, tag=tag)
        return None
    data = box.get("data") or {}
    u = data.get("usage") or {}
    text = None
    try:
        choice = data["choices"][0]
        text = choice["message"].get("content")
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        finish = choice.get("finish_reason")
    except Exception:
        finish = "?"
    _account(kid, u, failed=not text, tag=tag)
    log.info("llm %s %.1fs in=%s out=%s cached=%s finish=%s" % (
        tag, dt, u.get("prompt_tokens"), u.get("completion_tokens"), _last_call.get("cached_tokens"), finish))
    if not text or not str(text).strip():
        log.warning("llm %s returned empty content (finish=%s)" % (tag, finish))
        return None
    return str(text)


# ------------------------------------------------------------------ fake mode
def _fake(messages, tag, kid, timeout):
    mode = config.FAKE_LLM_MODE
    if mode == "hang":
        time.sleep(config.FAKE_LLM_DELAY_S)   # deliberately ignores the timeout
        return None
    if mode == "fail":
        _account(kid, None, failed=True, tag=tag)
        log.warning("llm %s fake failure" % tag)
        return None
    _account(kid, {"prompt_tokens": 100, "completion_tokens": 20}, tag=tag)
    text = "\n".join(str(m.get("content", "")) for m in messages)
    if "TASK: classify" in text:
        from .prep.classify import example_from_rules
        return json.dumps({
            "family": "unknown", "perfect_information": None, "simultaneous": None,
            "speech_restrictions": "", "action_format": "see rules",
            "example_action": example_from_rules(text) or "1",
            "playbook": "Fake playbook: play legal moves."})
    if "TASK: opening" in text:
        return json.dumps({"chat": "Fake opening line."})
    m = re.search(r"LEGAL_ACTIONS_JSON: (\[.*\])", text)
    ex = re.search(r"EXAMPLE ACTION \(format only[^:]*: (.+)", text)
    action = ex.group(1).strip() if ex else "1"
    if m:
        try:
            legal = json.loads(m.group(1))
            if legal:
                action = legal[-1]      # last legal action, so tests can tell L2 from L1
        except Exception:
            pass
    return "```json\n" + json.dumps({"action": action, "chat": "Fake chat line.",
                                     "why": "fake"}) + "\n```"
