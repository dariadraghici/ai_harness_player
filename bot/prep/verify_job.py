"""The verification suite. A sandbox JOB: it runs INSIDE the child, next to the simulator.

Owner: luckydevs-3. Status: real.

Never imported by the bot. bot/prep/verify.py sends it to a sandbox child
(sandbox.run_job) with a payload and reads back one JSON report. Standalone:
standard library only, nothing from `bot`.

run(sim, payload) with payload["mode"]:

  "verify" (default)  every check on one candidate. Payload:
      samples        [{"observation", "legal_actions" (list or null), "seat", "variant", "kind"}];
                     the first one is the prep sample turn, the rest are stored real turns
      simultaneous   True / False / None (from the profile)
      seeded         True when the game has outcomes we cannot know (chance, hidden values):
                     contract point C11 is then checked
      action_regex   trusted regex of a valid action, or ""
      example_action the profile's example action, or ""
      games, step_cap, budget_s, trace_games, trace_steps
    Result: {"checks": {name: bool}, "failures": [sentence], "warnings": [sentence],
             "exact_order", "legal_complete", "legal_count", "games", "steps_per_game",
             "playouts_per_s", "views": [...], "traces": [...], "seconds"}

  "turn"   one turn message against the simulator (the live consistency check):
      {"observation", "legal_actions", "seat", "variant"} -> {"ok", "reason", "legal_count"}

Every failure is ONE sentence written for the model that will repair the
module: what was called with which input, what came back, what was expected.

The same action policy is used for every candidate (sorted legal actions, a
seeded choice), so two modules that agree on the rules produce equal traces;
verify.py compares the traces of the candidates (differential test).
"""

import hashlib
import json
import random
import re
import time

FUNCTIONS = ("parse_observation", "legal_actions", "apply_action", "is_terminal", "returns", "current_player")
MAX_FAILURES = 12
BAD_ACTION = "__not_an_action__"
INLINE_LEGAL = 60          # longer legal lists travel as a digest


def short(value, n=160):
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except Exception:
        text = repr(value)
    return text if len(text) <= n else text[:n - 3] + "..."


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)


def err(exc):
    text = str(exc)
    return "%s: %s" % (type(exc).__name__, text[:200]) if text else type(exc).__name__


def digest(legal):
    """A legal action list as something small and comparable (order does not matter)."""
    items = sorted(legal)
    if len(items) <= INLINE_LEGAL:
        return items
    h = hashlib.sha1("\n".join(items).encode("utf-8", "replace")).hexdigest()[:12]
    return ["#%d" % len(items), h] + items[:5]


class Suite:

    def __init__(self, sim, payload):
        self.sim = sim
        self.p = payload
        self.failures = []
        self.warnings = []
        self.checks = {}
        self.t0 = time.perf_counter()
        self.budget = float(payload.get("budget_s") or 10.0)
        self.seeded = bool(payload.get("seeded"))

    # ------------------------------------------------------------ bookkeeping
    def fail(self, check, sentence):
        self.checks[check] = False
        if len(self.failures) < MAX_FAILURES and not any(f[:70] == sentence[:70] for f in self.failures):
            self.failures.append(sentence)        # one sentence per kind of failure

    def ok(self, check):
        self.checks.setdefault(check, True)

    def left(self):
        return self.budget - (time.perf_counter() - self.t0)

    def where(self, sample, i):
        if i == 0:
            return "the sample observation (seat %s)" % sample.get("seat", 0)
        return "this real observation of seat %s: %s" % (sample.get("seat"), short(sample.get("observation"), 500))

    # ------------------------------------------------------------ policy shared by all candidates
    def choose(self, state, legal, rng):
        items = sorted(legal)
        if self.sim.current_player(state) == -1:
            pair = [items[rng.randrange(len(items))], items[rng.randrange(len(items))]]
            me = state.get("me", 0) if isinstance(state, dict) else 0
            return pair if me == 0 else [pair[1], pair[0]]
        return items[rng.randrange(len(items))]

    # ------------------------------------------------------------ checks 1-3 on one turn sample
    def load(self):
        if self.sim is None:
            self.fail("load", "the module could not be loaded")
            return False
        missing = [f for f in FUNCTIONS if not callable(getattr(self.sim, f, None))]
        if missing:
            self.fail("load", "the module does not define the function(s) %s" % ", ".join(missing))
            return False
        if not isinstance(getattr(self.sim, "LEGAL_ACTIONS_COMPLETE", None), bool):
            self.fail("load", "the module must define the constant LEGAL_ACTIONS_COMPLETE = True or False (C6)")
        self.ok("load")
        return True

    def parse(self, sample, i, check="parse"):
        """Returns the parsed state or None."""
        seat = sample.get("seat", 0)
        variants = [sample.get("variant") or {}]
        if i == 0:
            variants = [{}, dict(sample.get("variant") or {})]
        state = None
        for variant in variants:
            try:
                state = self.sim.parse_observation(sample.get("observation", ""), variant, seat)
            except Exception as e:
                self.fail(check, "parse_observation(observation, %s, %s) raised %s on %s" % (
                    short(variant, 80), seat, err(e), self.where(sample, i)))
                return None
            if not isinstance(state, dict):
                self.fail(check, "parse_observation returned %s, expected a dict state (C9)" % type(state).__name__)
                return None
            try:
                if json.loads(dump(state)) != state:
                    raise ValueError("it changes in a JSON round trip (non-string keys or tuples)")
            except Exception as e:
                self.fail(check, "the state from parse_observation is not JSON-serializable (C9): %s" % err(e))
                return None
        if state.get("me") != seat:
            self.fail(check, "parse_observation(..., seat=%s) returned state[\"me\"] = %s on %s; "
                             "state[\"me\"] must be our seat (C2)" % (seat, short(state.get("me")), self.where(sample, i)))
        if self.seeded and not (isinstance(state.get("seed"), int) and not isinstance(state.get("seed"), bool)):
            self.fail(check, "parse_observation must set state[\"seed\"] = 0 (C11); got %s" % short(state.get("seed")))
        try:
            cp = self.sim.current_player(state)
        except Exception as e:
            self.fail(check, "current_player(state) raised %s right after parse_observation on %s" % (
                err(e), self.where(sample, i)))
            return None
        sim_flag = self.p.get("simultaneous")
        want = [-1] if sim_flag is True else [seat] if sim_flag is False else [seat, -1]
        if cp not in want:
            self.fail(check, "current_player(state) is %s right after parse_observation on %s; a turn message "
                             "exists only when we must act, so it must be %s (C4)" % (
                                 short(cp), self.where(sample, i),
                                 "-1 (simultaneous step)" if want == [-1] else "our seat %s" % seat
                                 if want == [seat] else "%s or -1" % seat))
        try:
            if self.sim.is_terminal(state) is not False:
                self.fail(check, "is_terminal(state) is not False right after parse_observation on %s, "
                                 "but we were asked to move" % self.where(sample, i))
        except Exception as e:
            self.fail(check, "is_terminal(state) raised %s right after parse_observation" % err(e))
            return None
        self.ok(check)
        return state

    def legal(self, sample, i, state, check="legal"):
        """Returns the legal list or None. Sets self.exact_order on the first sample."""
        try:
            la = self.sim.legal_actions(state)
        except Exception as e:
            self.fail(check, "legal_actions(state) raised %s on the state parsed from %s" % (err(e), self.where(sample, i)))
            return None
        if not isinstance(la, list) or not all(isinstance(a, str) for a in la):
            self.fail(check, "legal_actions(state) must return a list of strings, got %s" % short(la))
            return None
        if len(set(la)) != len(la):
            self.fail(check, "legal_actions(state) lists an action twice: %s" % short(la))
        want = sample.get("legal_actions")
        if isinstance(want, list):
            missing = [a for a in want if a not in la]
            extra = [a for a in la if a not in want]
            if missing or extra:
                self.fail(check, "legal_actions on the state parsed from %s must be exactly %s (the arena's list); "
                                 "yours %s%s" % (self.where(sample, i), short(want, 300),
                                                 "misses %s" % short(missing[:8]) if missing else "",
                                                 (" and " if missing else "") + "wrongly has %s" % short(extra[:8])
                                                 if extra else ""))
            elif i == 0:
                self.exact_order = (la == want)
        else:
            if not la:
                self.fail(check, "legal_actions returned [] on the state parsed from %s, but we must move there" %
                          self.where(sample, i))
            rx = self.p.get("action_regex") or ""
            if rx and la:
                try:
                    pat = re.compile(rx)
                    bad = [a for a in la[:2000] if not pat.fullmatch(a)]
                    if bad:
                        self.fail(check, "legal_actions lists %s, which do not have the action format of the rules "
                                         "(every valid action fully matches %s)" % (short(bad[:3]), rx))
                except re.error:
                    pass
            ex = self.p.get("example_action") or ""
            if ex and la and getattr(self.sim, "LEGAL_ACTIONS_COMPLETE", False) is True and i == 0:
                if ex not in la and ex.lower() not in [a.lower() for a in la]:
                    self.warnings.append("the example action %r of the rules is not in legal_actions although "
                                         "LEGAL_ACTIONS_COMPLETE is True" % ex)
        self.ok(check)
        return la

    # ------------------------------------------------------------ check 4 on the sample state
    def contract(self, state, legal):
        check = "contract"
        rng = random.Random(1)
        try:
            act = self.choose(state, legal, rng)
            before = dump(state)
            r1 = self.sim.apply_action(state, act)
            if dump(state) != before:
                self.fail(check, "apply_action(state, %s) changed its input state; copy first (C8)" % short(act))
                state = json.loads(before)
            if not isinstance(r1, dict):
                self.fail(check, "apply_action(state, %s) returned %s, expected a new dict state" % (
                    short(act), type(r1).__name__))
                return
            r2 = self.sim.apply_action(state, act)
            if dump(r1) != dump(r2):
                self.fail(check, "apply_action(state, %s) gave two different results for the same input; the same "
                                 "input must always give the same output (C8; draw unknown outcomes only with "
                                 "random.Random(state[\"seed\"]), C11)" % short(act))
            r3 = self.sim.apply_action(json.loads(before), act)
            if dump(r1) != dump(r3):
                self.fail(check, "apply_action behaves differently on a state that went through JSON "
                                 "(json.loads(json.dumps(state))); states must be plain JSON data (C9)")
            if self.sim.legal_actions(state) != legal:
                self.fail(check, "legal_actions(state) gave two different results for the same state (C8)")
        except Exception as e:
            self.fail(check, "apply_action(state, %s) raised %s on the state parsed from the sample observation, "
                             "although the action is in legal_actions(state)" % (short(locals().get("act")), err(e)))
            return
        try:
            self.sim.apply_action(state, BAD_ACTION)
            self.warnings.append("apply_action accepted the illegal action %r without raising ValueError (C10)" % BAD_ACTION)
        except ValueError:
            pass
        except Exception as e:
            self.warnings.append("apply_action raised %s for an illegal action, expected ValueError (C10)" %
                                 type(e).__name__)
        self.ok(check)

    # ------------------------------------------------------------ check 5: self-play
    def play(self, start, rng, cap, strict, record=None):
        """One game. Returns (ended, steps, final state or None, failure sentence or None)."""
        st, hist = start, []
        for steps in range(cap + 1):
            term = self.sim.is_terminal(st)
            la = self.sim.legal_actions(st)
            if not isinstance(term, bool):
                return False, steps, None, "is_terminal returned %s, expected True or False" % short(term)
            if not isinstance(la, list) or not all(isinstance(a, str) for a in la):
                return False, steps, None, "legal_actions returned %s after the actions %s" % (short(la), short(hist[-8:]))
            cp = None if term else self.sim.current_player(st)
            if record is not None:
                record.append({"cp": cp, "legal": digest(la), "terminal": term})
            if term:
                if la:
                    return False, steps, None, ("legal_actions must be [] when is_terminal is True; it lists %s "
                                                "after the actions %s" % (short(la[:6]), short(hist[-8:])))
                return True, steps, st, None
            if not la:
                return False, steps, None, ("legal_actions returned [] although is_terminal is False, after the "
                                            "actions %s from the sample state: the game cannot continue%s" % (
                                                short(hist[-8:]), self.unknown_hint()))
            if cp not in (0, 1, -1):
                return False, steps, None, "current_player returned %s, expected 0, 1 or -1" % short(cp)
            if steps == cap:
                break
            act = self.choose(st, la, rng)
            if strict:
                before = dump(st)
            nxt = self.sim.apply_action(st, act)
            if strict:
                if dump(st) != before:
                    return False, steps, None, "apply_action(state, %s) changed its input state (C8)" % short(act)
                if not isinstance(nxt, dict) or json.loads(dump(nxt)) != nxt:
                    return False, steps, None, ("apply_action(state, %s) returned a state that is not plain JSON "
                                                "data (C9)" % short(act))
            hist.append(act)
            if record is not None:
                record[-1]["act"] = act
            st = nxt
        return False, cap, None, ("a random game from the sample state did not end within %d steps (last actions "
                                  "%s): is_terminal never became True%s" % (cap, short(hist[-6:]), self.unknown_hint()))

    def unknown_hint(self):
        if not self.seeded:
            return ""
        return ("; when an outcome is random or hidden, apply_action must draw it with "
                "random.Random(state[\"seed\"]) and return a state the game continues from (C11)")

    def check_returns(self, final):
        r = self.sim.returns(final)
        if (not isinstance(r, (list, tuple)) or len(r) != 2
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r)):
            return "returns(state) must be a list of two numbers [seat 0, seat 1], got %s" % short(r)
        if not all(-1.0 <= float(x) <= 1.0 for x in r):
            return "returns(state) gave %s; both values must be in [-1, 1] (C7)" % short(r)
        return None

    def seeded_start(self, state, g):
        st = dict(state)
        if self.seeded and "seed" in st:
            st["seed"] = g
        return st

    def selfplay(self, state):
        check = "selfplay"
        games = int(self.p.get("games") or 30)
        cap = int(self.p.get("step_cap") or 400)
        done, total_steps, bad_games = 0, 0, 0
        for g in range(games):
            if self.left() < 0.3 * self.budget and done >= 3:
                break
            if self.left() <= 0:
                break
            try:
                ended, steps, final, why = self.play(self.seeded_start(state, g), random.Random(100 + g), cap, g < 2)
                if why is None:
                    why = self.check_returns(final)
            except Exception as e:
                why = "a random game from the sample state raised %s" % err(e)
            if why:
                self.fail(check, why)
                bad_games += 1
                if bad_games >= 3:
                    break
                continue
            done += 1
            total_steps += steps
        self.games = done
        self.steps_per_game = round(total_steps / done, 1) if done else 0.0
        if done < 3 and self.checks.get(check, True):
            self.fail(check, "only %d random game(s) finished within %.0f s: the module is far too slow for search" % (
                done, self.budget))
        self.ok(check)
        if self.checks.get(check):
            self.speed(state, cap)

    def speed(self, state, cap):
        """Plain playouts for a short while: the number the engines plan with."""
        t0, n = time.perf_counter(), 0
        window = min(0.3, max(0.05, self.left() * 0.2))
        try:
            while time.perf_counter() - t0 < window:
                st, rng = self.seeded_start(state, n), random.Random(n)
                for _ in range(cap):
                    if self.sim.is_terminal(st):
                        break
                    st = self.sim.apply_action(st, self.choose(st, self.sim.legal_actions(st), rng))
                n += 1
        except Exception:
            pass
        self.playouts_per_s = round(n / max(time.perf_counter() - t0, 1e-9), 1)

    # ------------------------------------------------------------ C11: seeds
    def seed_check(self, state):
        check = "seed"
        cap = int(self.p.get("step_cap") or 400)

        def fingerprint(seed):
            st = dict(state)
            st["seed"] = seed
            rec = []
            ended, steps, final, why = self.play(st, random.Random(5), cap, False, rec)
            if final is not None:
                final = dict(final)
                final.pop("seed", None)
            return dump([rec, ended, steps, final])
        try:
            if fingerprint(3) != fingerprint(3):
                self.fail(check, "the same state[\"seed\"] gave two different games; the only randomness allowed is "
                                 "random.Random(state[\"seed\"]) (C8, C11)")
                return
            prints = set(fingerprint(s) for s in (1, 2, 3, 4, 5, 6))
            if len(prints) < 2:
                self.fail(check, "changing state[\"seed\"] never changes what happens in a game, but this game has "
                                 "outcomes we cannot know (random or hidden): apply_action must draw each such "
                                 "outcome with random.Random(state[\"seed\"]) from the values still possible, store "
                                 "it in the new state and store a new seed there (C11)")
                return
        except Exception as e:
            self.fail(check, "playing a game with another state[\"seed\"] raised %s" % err(e))
            return
        self.ok(check)

    # ------------------------------------------------------------ traces for the differential test
    def traces(self, state):
        out = []
        cap = int(self.p.get("trace_steps") or 60)
        for g in range(int(self.p.get("trace_games") or 6)):
            if self.left() <= 0:
                break
            rec, entry = [], {}
            try:
                ended, steps, final, why = self.play(dict(state), random.Random(7000 + g), cap, False, rec)
                entry = {"steps": rec, "ended": ended}
                if final is not None:
                    r = self.sim.returns(final)
                    entry["returns"] = [round(float(x), 6) for x in r]
                if why and not ended and steps < cap:
                    entry["error"] = why[:200]
            except Exception as e:
                entry = {"steps": rec, "ended": False, "error": err(e)}
            out.append(entry)
        return out

    # ------------------------------------------------------------ the whole suite
    def run(self):
        self.exact_order = False
        self.games = 0
        self.steps_per_game = 0.0
        self.playouts_per_s = 0.0
        views, traces, legal_count = [], [], 0
        samples = [s for s in (self.p.get("samples") or []) if isinstance(s, dict)]
        if self.load() and samples:
            first_state = None
            for i, sample in enumerate(samples):
                check_p = "parse" if i == 0 else "stored_turns"
                check_l = "legal" if i == 0 else "stored_turns"
                if self.left() <= 0 and i > 0:
                    break
                state = self.parse(sample, i, check_p)
                la = self.legal(sample, i, state, check_l) if state is not None else None
                view = None
                if state is not None and la is not None:
                    try:
                        view = {"cp": self.sim.current_player(state), "legal": digest(la)}
                    except Exception:
                        view = None
                views.append(view)
                if i == 0:
                    first_state, first_legal = state, la
                    legal_count = len(la) if la is not None else 0
                    if state is None:
                        self.checks.setdefault("legal", False)
            if first_state is not None and first_legal:
                self.contract(first_state, first_legal)
                self.selfplay(first_state)
                if self.seeded and self.checks.get("selfplay"):
                    self.seed_check(first_state)
                if not self.seeded:
                    traces = self.traces(first_state)
        elif not samples:
            self.fail("parse", "no turn sample to check against")
        for name in ("parse", "legal", "contract", "selfplay"):
            self.checks.setdefault(name, False)
        if self.seeded:
            self.checks.setdefault("seed", False)
        complete = getattr(self.sim, "LEGAL_ACTIONS_COMPLETE", None) if self.sim is not None else None
        return {"checks": self.checks, "failures": self.failures, "warnings": self.warnings[:6],
                "exact_order": bool(self.exact_order), "legal_complete": complete if isinstance(complete, bool) else None,
                "legal_count": legal_count, "games": self.games, "steps_per_game": self.steps_per_game,
                "playouts_per_s": self.playouts_per_s, "views": views, "traces": traces,
                "samples": len(samples), "seconds": round(time.perf_counter() - self.t0, 3)}


def check_turn(sim, payload):
    """One real turn message against the simulator: does it parse, and is the legal list the arena's?"""
    seat = payload.get("seat", 0)
    try:
        state = sim.parse_observation(payload.get("observation", ""), payload.get("variant") or {}, seat)
        la = sim.legal_actions(state)
        cp = sim.current_player(state)
    except Exception as e:
        return {"ok": False, "reason": "raised %s" % err(e), "legal_count": 0}
    if not isinstance(la, list) or not la:
        return {"ok": False, "reason": "no legal actions", "legal_count": 0}
    if cp not in (seat, -1):
        return {"ok": False, "reason": "current_player is %s, we are seat %s" % (short(cp), seat), "legal_count": len(la)}
    want = payload.get("legal_actions")
    if isinstance(want, list) and sorted(want) != sorted(la):
        missing = [a for a in want if a not in la]
        extra = [a for a in la if a not in want]
        return {"ok": False, "legal_count": len(la),
                "reason": "legal actions differ: misses %s, wrongly has %s" % (short(missing[:5]), short(extra[:5]))}
    return {"ok": True, "reason": "", "legal_count": len(la)}


def run(sim, payload):
    if payload.get("mode") == "turn":
        if sim is None:
            return {"ok": False, "reason": "no simulator loaded", "legal_count": 0}
        return check_turn(sim, payload)
    return Suite(sim, payload).run()
