"""Entrypoint: the protocol loop. Tiny and bulletproof.

Shared file (owner: Sebi). Status: real.

Threads:
  reader  : reads stdin lines into the event queue ("line", text) / ("eof", None)
  worker  : one per request (prep, start, turn); computes the answer and
            posts ("done", request) to the same queue
  main    : the only thread that decides when to answer. It waits on the
            queue until the request's hard deadline (no CPU spin while idle);
            at the deadline it answers with the best result so far (watchdog).

Every prep, start and turn gets exactly one answer (protocol.Request has a
lock and an answered flag). The process exits only on shutdown or closed stdin.

Run: python -u -m bot.main
"""

from . import protocol


def run():
    protocol.install()          # first: stdout now goes to stderr, protocol keeps fd 1

    import os
    import queue
    import signal
    import sys
    import threading
    import time

    from . import action_guard, clock, config, ladder, llm, log, memory, metrics, opponents, resources, specialist
    from .engines import base as engines_base
    from .chat import decide, defense, influence
    from .engines.bg_compute import BackgroundCompute
    from .interfaces import VariantSpec
    from .prep import pipeline

    class Bot:
        def __init__(self):
            self.q = queue.Queue()
            self.pending = None
            self.prepared = False
            self.prep_study_seconds = None
            self.prep_move_seconds = None
            self.start_msg = None
            self.spec = None
            self.move_seconds = None
            self.background = BackgroundCompute()

        # ------------------------------------------------------------ plumbing
        def reader(self):
            stdin = sys.stdin.buffer
            while True:
                try:
                    line = stdin.readline()
                except Exception:
                    line = b""
                if not line:
                    self.q.put(("eof", None))
                    return
                self.q.put(("line", line.decode("utf-8", "replace")))

        def spawn(self, req, fn):
            def work():
                try:
                    fn(req)
                except Exception:
                    log.exception("worker for %s failed" % req.type)
                finally:
                    self.q.put(("done", req))
            req.t0 = time.monotonic()
            req.perf0 = metrics.perf_start()
            req.timing = {}
            self.pending = req
            threading.Thread(target=work, name="worker-" + req.type, daemon=True).start()

        def finish(self, req, by_watchdog):
            """Answer req with the worker's result, the best so far, or a fallback."""
            if req is not self.pending:
                return
            self.pending = None
            obj = req.result
            source = "worker"
            if obj is None:
                obj = req.best
                source = "best-so-far"
            if obj is None:
                obj = self.fallback(req)
                source = "fallback"
            if by_watchdog:
                log.warning("watchdog answered %s (%s) after %.2fs" % (
                    req.type, source, time.monotonic() - req.t0))
            if req.type == "turn":
                ctx = {"spec": self.spec, "start": self.start_msg, "turn": req.msg}
                obj = action_guard.emit_action(ctx, [obj], req.deadline)
                source = "guarded-" + source
            if req.type == "prep":
                resources.enter("idle")          # prep is over: no child alive, no warm cache, until the next start
            if req.answer(obj):
                elapsed = time.monotonic() - req.t0
                info = getattr(req, "info", {}) or {}
                metrics.record(req.type, game_id=(self.start_msg or {}).get("game_id")
                               if req.type != "prep" else req.msg.get("game_id"),
                               game_index=req.msg.get("game_index"), turn=req.msg.get("turn"),
                               level=info.get("level"), why=info.get("why"),
                               elapsed=round(elapsed, 3), watchdog=by_watchdog, source=source,
                               warning=bool(req.msg.get("warning")), llm=llm.usage(),
                               action=(obj.get("action") if isinstance(obj, dict) else None),
                               **metrics.perf_fields(getattr(req, "perf0", None), getattr(req, "timing", None),
                                                     dict(engines_base.LAST_RUN) if req.type == "turn" else None))
                if req.type == "turn" and isinstance(obj, dict):
                    influence.on_sent(info.get("goal"), self.spec, self.start_msg, req.msg,
                                      obj.get("action"), obj.get("chat"))       # logs the attempt, queues its score
                if req.type == "prep" and not by_watchdog:
                    self.background.start(specialist.all_specs())
                elif req.type in ("start", "turn") and not by_watchdog:
                    self.background.resume()

        def fallback(self, req):
            if req.type == "prep":
                return protocol.ready()
            if req.type == "start":
                prof = self.spec.profile if self.spec else None
                return protocol.ready(decide.clean_chat(config.OPENING_FALLBACK, prof))
            d = ladder.emergency(self.spec, req.msg)
            req.info = {"level": d.level, "why": d.why}
            return {"action": d.action, "chat": d.chat}

        # ------------------------------------------------------------ handlers
        def on_prep(self, msg):
            llm.set_key(msg.get("llm"))
            self.prepared = True
            self.prep_study_seconds = msg.get("study_seconds")
            self.prep_move_seconds = msg.get("move_seconds")
            resources.enter("prep")
            t0 = time.monotonic()
            req = protocol.Request(msg, clock.prep_hard(msg, t0))

            def work(r):
                pipeline.run_prep(msg, r.deadline - 1.0)
                r.result = protocol.ready()
            self.spawn(req, work)

        def on_start(self, msg):
            llm.set_key(msg.get("llm"))           # switch the key first
            resources.enter("turn")
            t0 = time.monotonic()
            self.start_msg = msg
            self.move_seconds = msg.get("move_seconds") or self.prep_move_seconds
            variant = msg.get("variant") or {}
            # A rules-only spec right away, so every later decision has the rules.
            self.spec = VariantSpec(
                game_id=str(msg.get("game_id") or "unknown"), game_name=str(msg.get("game_name") or ""),
                label=variant.get("label", "") if isinstance(variant, dict) else str(variant),
                rules=msg.get("rules") or "", rules_hash=memory.rules_hash(msg.get("rules") or ""),
                source="default")
            req = protocol.Request(msg, clock.start_hard(msg, t0, self.prepared, self.prep_study_seconds))
            log.info("start %s game %s seat %s vs %s (%s), %.1fs to ready" % (
                msg.get("game_id"), msg.get("game_index"), msg.get("seat"), msg.get("opponent"),
                "warm" if self.prepared else "cold", req.deadline - t0))

            def work(r):
                spec = specialist.for_start(msg, r.deadline - 3.0)
                if self.start_msg is msg:
                    self.spec = spec
                self.background.add_specs([spec])
                ctx = defense.build_context(msg.get("chat"))
                left = r.deadline - time.monotonic() - 1.0
                r.result = protocol.ready(decide.opening_chat(spec, msg, ctx, min(5.0, left)))
                r.info = {"level": None, "why": "spec source=%s family=%s" % (
                    spec.source, spec.profile.family)}
            self.spawn(req, work)

        def on_turn(self, msg):
            if resources.phase() != "turn":
                resources.enter("turn")
            engines_base.LAST_RUN.clear()
            t0 = time.monotonic()
            req = protocol.Request(msg, clock.turn_hard(msg, t0, self.move_seconds))
            spec, start_msg = self.spec, self.start_msg

            def progress(d):
                req.set_best({"action": d.action, "chat": d.chat})
                req.info = {"level": d.level, "why": d.why, "goal": d.goal}

            def work(r):
                d = ladder.decide_turn(spec, start_msg, msg, r.deadline, progress, self.move_seconds, r.timing)
                r.info = {"level": d.level, "why": d.why, "goal": d.goal}
                r.result = {"action": d.action, "chat": d.chat}
            self.spawn(req, work)

        def on_end(self, msg):
            log.info("end game %s: %s (%s) scores=%s match_score=%s match_over=%s" % (
                msg.get("game_index"), msg.get("result"), msg.get("reason"), msg.get("scores"),
                msg.get("match_score"), msg.get("match_over")))
            sm = self.start_msg or {}
            opponents.record_game_end(sm.get("opponent"), msg, sm.get("game_id"))
            influence.on_game_end(sm, defense.build_context(msg.get("chat")))
            try:
                from .engines import library as engines_library
                engines_library.record_result(self.spec, msg.get("result") == "win")
            except Exception:
                log.exception("library result not recorded")
            metrics.record("end", game_id=sm.get("game_id"), game_index=msg.get("game_index"),
                           result=msg.get("result"), reason=msg.get("reason"),
                           match_over=msg.get("match_over"), llm=llm.usage())
            # match_over does NOT end the container: keep waiting for the next start. Between games the warm
            # engine children are closed and the sandbox is emptied (phase idle); the only thing that may run
            # then is the low-priority background compute (one nice'd child, killed by the next request).
            resources.enter("idle")
            self.background.resume()

        def shutdown(self, why):
            log.info("shutting down: %s" % why)
            self.background.stop()
            try:
                resources.enter("idle")
            except Exception:
                log.exception("idle on shutdown failed")
            try:
                opponents.save_all()
                metrics.record("shutdown", why=why, llm=llm.usage("all"))
            except Exception:
                log.exception("save on shutdown failed")
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)

        # ------------------------------------------------------------ loop
        def loop(self):
            threading.Thread(target=self.reader, name="stdin-reader", daemon=True).start()
            handlers = {"prep": self.on_prep, "start": self.on_start, "turn": self.on_turn,
                        "end": self.on_end}
            while True:
                try:
                    timeout = None
                    if self.pending is not None:
                        timeout = max(0.0, self.pending.deadline - time.monotonic())
                    try:
                        kind, data = self.q.get(timeout=timeout)
                    except queue.Empty:
                        self.finish(self.pending, by_watchdog=True)
                        continue
                    if kind == "done":
                        self.finish(data, by_watchdog=False)
                        continue
                    if kind == "eof":
                        self.shutdown("stdin closed")
                    msg = protocol.parse_line(data)
                    if msg is None:
                        continue
                    t = msg["type"]
                    if t == "shutdown":
                        self.shutdown("shutdown message")
                    if t in ("start", "turn"):
                        self.background.pause()
                    if self.pending is not None:
                        if t == "end":
                            log.warning("end arrived while %s pending; dropping it" % self.pending.type)
                            self.pending.drop()
                            self.pending = None
                        else:
                            log.warning("%s arrived while %s pending; answering it now" % (
                                t, self.pending.type))
                            self.finish(self.pending, by_watchdog=True)
                    handlers[t](msg)
                except SystemExit:
                    raise
                except Exception:
                    log.exception("main loop error (continuing)")

    def on_sigterm(signum, frame):
        log.info("SIGTERM, exiting")
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except Exception:
        pass
    log.info("luckydevs bot %s starting (fake_llm=%s, memory=%s)" % (
        config.HARNESS_VERSION, config.FAKE_LLM, memory.root()))
    log.info("resources: %s" % resources.describe())
    Bot().loop()


if __name__ == "__main__":
    run()
