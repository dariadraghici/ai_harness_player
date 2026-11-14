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


def _account(kid, u, failed=False, tag=""):
    with _lock:
        c = _usage.setdefault(kid, {"calls": 0, "failures": 0, "input": 0,
                                    "output": 0, "cached": 0, "cost": 0.0})
        c["calls"] += 1
        if failed:
            c["failures"] += 1
        prompt = int(u.get("prompt_tokens") or 0) if u else 0
        out = int(u.get("completion_tokens") or 0) if u else 0
        cached = int(u.get("cache_read_input_tokens") or
                     (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0) if u else 0
        total = prompt + out
        cost = (max(prompt - cached, 0) * config.PRICE_INPUT_PER_TOKEN
                + cached * config.PRICE_CACHE_READ_PER_TOKEN
                + out * config.PRICE_OUTPUT_PER_TOKEN)
        if u:
            c["input"] += prompt
            c["output"] += out
            c["cached"] += cached
            c["cost"] += cost

        _last_call["prompt_tokens"] = prompt
        _last_call["completion_tokens"] = out
        _last_call["total_tokens"] = total
        _last_call["cached_tokens"] = cached
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
    body = {"model": key["model"], "messages": messages, "max_tokens": int(max_tokens),
            "thinking": thinking}
    box = {}

    def worker():
        try:
            req = urllib.request.Request(
                key["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + key["api_key"],
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["data"] = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                detail = ""
            box["error"] = "HTTP %s %s" % (e.code, detail)
        except Exception as e:
            box["error"] = "%s: %s" % (type(e).__name__, e)

    t0 = time.monotonic()
    th = threading.Thread(target=worker, name="llm-" + tag, daemon=True)
    th.start()
    th.join(timeout)
    dt = time.monotonic() - t0
    if th.is_alive():
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
    log.info("llm %s %.1fs in=%s out=%s finish=%s" % (
        tag, dt, u.get("prompt_tokens"), u.get("completion_tokens"), finish))
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
