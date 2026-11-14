"""One JSON line per decision (game, level, time, tokens, cost, why).

Owner: luckydevs-1. Status: real.

Lines go to <memory>/metrics/<container_id>.jsonl. One file per container, so
no two processes ever append to the same file.

Every decision/action appends exactly one JSON line containing:
  - game_id: Game identifier
  - level: Fallback ladder level used (3 = engine, 2 = model, 1 = safe move)
  - elapsed: Time elapsed in seconds for this decision
  - ts: Wall-clock timestamp (seconds since epoch)
  - tokens: Breakdown dict with prompt_tokens, completion_tokens, total_tokens, cached_tokens
  - cost: Estimated cost in USD
  - why: Reasoning/explanation for the move or action
"""

import os
import socket
import time

from . import config, log, memory

CONTAINER_ID = "%s-%d-%d" % (socket.gethostname(), os.getpid(), int(time.time()))


def calculate_cost(prompt_tokens, completion_tokens, cached_tokens=0):
    """Calculate estimated cost in USD based on config pricing."""
    try:
        p = int(prompt_tokens or 0)
        c = int(completion_tokens or 0)
        cached = int(cached_tokens or 0)
        p_uncached = max(p - cached, 0)
        return round(
            (p_uncached * config.PRICE_INPUT_PER_TOKEN)
            + (cached * config.PRICE_CACHE_READ_PER_TOKEN)
            + (c * config.PRICE_OUTPUT_PER_TOKEN),
            6,
        )
    except Exception:
        return 0.0


def record_decision(game_id, level, elapsed, why="", tokens=None, cost=None, **extra):
    """Record one decision/action JSON line with full metrics. Never raises."""
    return record("action", game_id=game_id, level=level, elapsed=elapsed,
                  why=why, tokens=tokens, cost=cost, **extra)


def record(kind, **fields):
    """Append one metrics line to <memory>/metrics/<container_id>.jsonl. Never raises."""
    try:
        fields["kind"] = kind
        fields["ts"] = round(time.time(), 3)
        fields["container"] = CONTAINER_ID

        # For decisions/actions/turns, ensure game_id, level, tokens, cost, why are present
        if kind in ("action", "turn", "decision", "start"):
            if "game_id" not in fields or fields["game_id"] is None:
                fields["game_id"] = "unknown"

            if "level" not in fields:
                fields["level"] = None

            if "why" not in fields or fields["why"] is None:
                fields["why"] = ""

            # Ensure token breakdown is present
            tokens = fields.get("tokens")
            if not isinstance(tokens, dict):
                from . import llm
                call_info = llm.pop_last_call() if hasattr(llm, "pop_last_call") else {}
                p = int(call_info.get("prompt_tokens") or 0)
                c = int(call_info.get("completion_tokens") or 0)
                tot = int(call_info.get("total_tokens") or (p + c))
                cached = int(call_info.get("cached_tokens") or 0)
                tokens = {
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "total_tokens": tot,
                    "cached_tokens": cached,
                }
                fields["tokens"] = tokens

                if "cost" not in fields or fields["cost"] is None:
                    fields["cost"] = round(call_info.get("cost", calculate_cost(p, c, cached)), 6)
            else:
                p = int(tokens.get("prompt_tokens") or 0)
                c = int(tokens.get("completion_tokens") or 0)
                cached = int(tokens.get("cached_tokens") or 0)
                if "total_tokens" not in tokens:
                    tokens["total_tokens"] = p + c
                if "cost" not in fields or fields["cost"] is None:
                    fields["cost"] = calculate_cost(p, c, cached)

            if "cost" in fields and isinstance(fields["cost"], (int, float)):
                fields["cost"] = round(float(fields["cost"]), 6)

        memory.append_jsonl(memory.metrics_path(CONTAINER_ID), fields)
    except Exception:
        log.exception("metrics.record failed")
