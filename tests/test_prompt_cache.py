"""The gateway's prompt cache (bot/llm.py): mark the stable prefix, bill it right, and never lose a turn to it.

Model cost is a scored tie-break (kit note K11) and the system message of a decision is identical for every
turn of a game, so it belongs in the gateway's prompt cache. Measured against the real gateway: two identical
plain-text calls reported cached_tokens 0; the same calls with a cache_control block wrote 4842 tokens and then
read them back.
"""

import json
import os
import unittest
from unittest import mock

from bot import config, llm


class CachePrefixTests(unittest.TestCase):

    def setUp(self):
        llm._blocks_ok[0] = True
        self.addCleanup(lambda: llm._blocks_ok.__setitem__(0, True))

    def msgs(self, system_chars=3000):
        return [{"role": "system", "content": "s" * system_chars}, {"role": "user", "content": "the turn"}]

    def test_a_long_system_message_is_marked_and_the_original_is_untouched(self):
        original = self.msgs()
        out, marked = llm.cache_prefix(original)
        self.assertTrue(marked)
        self.assertIsInstance(original[0]["content"], str)          # not mutated
        block = out[0]["content"][0]
        self.assertEqual(block["type"], "text")
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})
        self.assertEqual(block["text"], original[0]["content"])
        self.assertEqual(out[1], original[1])                       # the changing part stays a plain string

    def test_a_short_prompt_is_not_worth_an_entry(self):
        out, marked = llm.cache_prefix(self.msgs(10))
        self.assertFalse(marked)
        self.assertIsInstance(out[0]["content"], str)
        self.assertLess(10, config.PROMPT_CACHE_MIN_CHARS)

    def test_nothing_to_mark_is_not_an_error(self):
        for messages in ([], [{"role": "user", "content": "x" * 5000}], None,
                         [{"role": "system", "content": [{"type": "text", "text": "already blocks"}]}]):
            out, marked = llm.cache_prefix(messages)
            self.assertFalse(marked, messages)
            self.assertEqual(out, messages)

    def test_the_switch_and_the_process_flag_turn_it_off(self):
        with mock.patch.dict(os.environ, {"BOT_PROMPT_CACHE": "off"}):
            self.assertFalse(llm.cache_enabled())
            self.assertFalse(llm.cache_prefix(self.msgs())[1])
        llm._blocks_ok[0] = False
        self.assertFalse(llm.cache_enabled())
        self.assertFalse(llm.cache_prefix(self.msgs())[1])


class AccountingTests(unittest.TestCase):

    def usage(self, **kw):
        llm._usage.clear()
        llm._account("k", kw, tag="t")
        return dict(llm._last_call)

    def test_a_cache_read_is_ten_times_cheaper_than_a_fresh_prompt(self):
        fresh = self.usage(prompt_tokens=1000, completion_tokens=0)
        read = self.usage(prompt_tokens=1000, completion_tokens=0,
                          prompt_tokens_details={"cached_tokens": 900, "text_tokens": 100})
        self.assertEqual(read["cached_tokens"], 900)
        self.assertLess(read["cost"], fresh["cost"])
        self.assertAlmostEqual(read["cost"], 100 * config.PRICE_INPUT_PER_TOKEN
                               + 900 * config.PRICE_CACHE_READ_PER_TOKEN, places=12)

    def test_a_cache_write_costs_more_than_a_plain_token(self):
        write = self.usage(prompt_tokens=1000, completion_tokens=0,
                           prompt_tokens_details={"cached_tokens": 0, "cache_creation_tokens": 900})
        self.assertEqual(write["cache_write_tokens"], 900)
        self.assertAlmostEqual(write["cost"], 100 * config.PRICE_INPUT_PER_TOKEN
                               + 900 * config.PRICE_CACHE_WRITE_PER_TOKEN, places=12)
        self.assertGreater(config.PRICE_CACHE_WRITE_PER_TOKEN, config.PRICE_INPUT_PER_TOKEN)

    def test_the_anthropic_field_names_are_read_too(self):
        read = self.usage(prompt_tokens=500, completion_tokens=0, cache_read_input_tokens=400,
                          cache_creation_input_tokens=50)
        self.assertEqual((read["cached_tokens"], read["cache_write_tokens"]), (400, 50))


class FallbackTests(unittest.TestCase):
    """A gateway that refuses content blocks must cost us one retry, never a turn."""

    def setUp(self):
        llm._blocks_ok[0] = True
        self.addCleanup(lambda: llm._blocks_ok.__setitem__(0, True))
        llm.set_key({"base_url": "http://127.0.0.1:9/v1", "api_key": "k", "model": "haiku"})
        self.addCleanup(llm.set_key, None)
        p = mock.patch.object(config, "FAKE_LLM", False)
        p.start()
        self.addCleanup(p.stop)

    def answer(self, text="ok"):
        class R:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
        return R()

    def test_blocks_are_sent_when_the_gateway_takes_them(self):
        seen = []

        def opener(req, timeout=None):
            seen.append(json.loads(req.data.decode()))
            return self.answer()
        with mock.patch.object(llm._gateway_opener, "open", opener):
            out = llm.complete([{"role": "system", "content": "s" * 3000}, {"role": "user", "content": "u"}],
                               timeout=5)
        self.assertEqual(out, "ok")
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0]["messages"][0]["content"], list)

    def test_a_refusal_falls_back_to_plain_text_once_and_stays_off(self):
        import urllib.error
        seen = []

        def opener(req, timeout=None):
            body = json.loads(req.data.decode())
            seen.append(body)
            if isinstance(body["messages"][0]["content"], list):
                raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)
            return self.answer("plain")
        messages = [{"role": "system", "content": "s" * 3000}, {"role": "user", "content": "u"}]
        with mock.patch.object(llm._gateway_opener, "open", opener):
            out = llm.complete(messages, timeout=8)
            self.assertEqual(out, "plain")
            self.assertEqual(len(seen), 2)                 # blocks, then plain
            self.assertFalse(llm._blocks_ok[0])
            seen.clear()
            self.assertEqual(llm.complete(messages, timeout=8), "plain")
            self.assertEqual(len(seen), 1)                 # never tried blocks again

    def test_another_error_does_not_switch_caching_off(self):
        import urllib.error

        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)
        with mock.patch.object(llm._gateway_opener, "open", opener):
            self.assertIsNone(llm.complete([{"role": "system", "content": "s" * 3000},
                                            {"role": "user", "content": "u"}], timeout=5))
        self.assertTrue(llm._blocks_ok[0])


if __name__ == "__main__":
    unittest.main()
