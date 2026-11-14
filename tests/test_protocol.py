"""Protocol tests: the bot as a child process, fake LLM. Owner: luckydevs-1."""

import time
import unittest

from tests.session_script import (BotProc, RULES_B, check_action, check_ready, prep_msg,
                                  run_session, start_msg, turn_msg)


class ProtocolTests(unittest.TestCase):

    def _close(self, bot):
        bot.close_stdin()
        bot.wait(10)
        bot.cleanup()

    def test_1_scripted_session(self):
        problems, bot = run_session()
        self.assertEqual(problems, [], bot.stderr_tail())
        bot.cleanup()

    def test_2_cold_start(self):
        bot = BotProc()
        try:
            p = []
            check_ready(*bot.ask(start_msg(1, study=120), 120), 95.5, p, "cold start", True)
            check_action(*bot.ask(turn_msg(1), 20), 13.5, ["1", "2", "3"], p, "cold turn")
            self.assertEqual(p, [], bot.stderr_tail())
            self.assertIn("cold prep", bot.stderr_tail(200))
        finally:
            self._close(bot)

    def test_3_garbage_input(self):
        bot = BotProc()
        try:
            for junk in ["not json", "[1,2,3]", '{"type": "bogus"}', '{"no_type": 1}', "{", '"str"',
                         "\x00\x01\xff", "", "null", '{"type": "end"}']:
                bot.send(junk)
            p = []
            check_ready(*bot.ask(start_msg(1, study=120), 120), 95.5, p, "start after junk", True)
            check_action(*bot.ask(turn_msg(1), 20), 13.5, ["1", "2", "3"], p, "turn after junk")
            self.assertEqual(p, [], bot.stderr_tail())
            self.assertTrue(bot.alive())
        finally:
            self._close(bot)

    def test_4_warning_not_repeated(self):
        bot = BotProc()
        try:
            p = []
            check_ready(*bot.ask(start_msg(1, study=120), 120), 95.5, p, "start", True)
            # The fake LLM picks the last legal action; reject exactly that.
            w = {"count": 1, "max": 2, "reason": "illegal action", "rejected": "3"}
            check_action(*bot.ask(turn_msg(2, warning=w), 20), 13.5, ["1", "2", "3"], p,
                         "turn with warning", rejected="3")
            w2 = {"count": 1, "max": 2, "reason": "illegal action", "rejected": "2"}
            check_action(*bot.ask(turn_msg(4, warning=w2), 20), 13.5, ["1", "2", "3"], p,
                         "turn with warning 2", rejected="2")
            self.assertEqual(p, [], bot.stderr_tail())
        finally:
            self._close(bot)

    def test_5_null_legal_actions(self):
        bot = BotProc()
        try:
            p = []
            check_ready(*bot.ask(start_msg(1, label="big", rules=RULES_B, study=120), 120), 95.5, p,
                        "start", True)
            obj, line, dt = bot.ask(turn_msg(1, legal=None), 20)
            check_action(obj, line, dt, 13.5, None, p, "null legal")
            self.assertEqual(p, [], bot.stderr_tail())
            self.assertEqual(obj["action"], "pick 7")    # example from the rules text
            w = {"count": 1, "max": 2, "reason": "illegal", "rejected": "pick 7"}
            check_action(*bot.ask(turn_msg(3, legal=None, warning=w), 20), 13.5, None, p,
                         "null legal with warning", rejected="pick 7")
            self.assertEqual(p, [], bot.stderr_tail())
        finally:
            self._close(bot)

    def test_6_watchdog_answers_slow_worker(self):
        # The fake LLM hangs for 30 s and ignores timeouts; short test clocks.
        bot = BotProc(env_extra={"BOT_FAKE_LLM_MODE": "hang", "BOT_FAKE_LLM_DELAY": "30"})
        try:
            p = []
            m = prep_msg(seconds=6)                      # hard deadline 6 * 510/600 = 5.1 s
            m["study_seconds"] = 3                       # warm start hard = 3 * 18/30 = 1.8 s
            check_ready(*bot.ask(m, 8), 5.6, p, "prep (hang)", False)
            check_ready(*bot.ask(start_msg(1, study=3), 3), 2.3, p, "start (hang)", True)
            # hard 4 * 13/20 = 2.6 s; model timeout min(1.6, 2.6 - 1) = 1.6 s, so L2 is tried and hangs.
            obj, line, dt = bot.ask(turn_msg(1, seconds=4), 4)
            check_action(obj, line, dt, 3.1, ["1", "2", "3"], p, "turn (hang)")
            self.assertGreater(dt, 2.0)                  # it really waited for the watchdog
            self.assertEqual(p, [], bot.stderr_tail())
            self.assertIn("watchdog answered", bot.stderr_tail(200))
            time.sleep(0.5)
            self.assertEqual(bot.drain(), [], "a late worker must not send a second line")
        finally:
            self._close(bot)

    def test_7_model_failure(self):
        problems, bot = run_session(env_extra={"BOT_FAKE_LLM_MODE": "fail"})
        self.assertEqual(problems, [], bot.stderr_tail())
        bot.cleanup()

    def test_8_closed_stdin_exits(self):
        bot = BotProc(env_extra={"BOT_FAKE_LLM_MODE": "hang", "BOT_FAKE_LLM_DELAY": "60"})
        p = []
        check_ready(*bot.ask(start_msg(1, study=4), 5), 4.0, p, "start", True)
        bot.send(turn_msg(1))                           # a worker is busy (hanging)
        t0 = time.monotonic()
        bot.close_stdin()
        code = bot.wait(10)
        self.assertEqual(p, [], bot.stderr_tail())
        self.assertEqual(code, 0, bot.stderr_tail())
        self.assertLess(time.monotonic() - t0, 10)
        bot.cleanup()


if __name__ == "__main__":
    unittest.main()
