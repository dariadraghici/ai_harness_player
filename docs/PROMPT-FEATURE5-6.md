# PROMPT-FEATURE5-6: L3 on verified simulators + fallback action without a legal list

You are working in the Git repository of team `luckydevs` (AI Harness Arena
hackathon). I am Sebi, the team lead. This file is your full briefing for two
items of GAMEPLAN.md, part C, luckydevs-3. Read all of it before you touch
anything.

5. Wire L3 into the ladder, only when verification passed (the plumbing
   exists; prep now sets `spec.verified` and `spec.simulator_path`).
6. Fallback action when `legal_actions` is null and the model is down: seen
   on arena-lite (Guess the Number) that a wrong example action loses the
   game in two answers. First fix done (`example_from_rules` now reads
   "Examples:"); still open: after a warning try the other quoted examples of
   the rules before the last-resort "pass".

---------------------------------------------------------------------------

## 0. How to work in this session

1. Read first: `RULES.md`, `READMEFIRST.md`, `docs/ARENA-CONTRACT.md` (kit
   notes K1 to K14), `participant-kit/PROTOCOL.md`, `GAMEPLAN.md` A.2 to A.4
   (A.3 is the decision contract you implement), C and D.
2. Then the code: `bot/ladder.py` (all of it), `bot/main.py` (`on_start`,
   `on_turn`, `on_end`, the watchdog), `bot/clock.py`, `bot/engines/base.py`
   and the four stubs `search.py`, `guess.py`, `simultaneous.py`, `bluff.py`,
   `bot/prep/sandbox.py` (the docstring has the engine example),
   `bot/prep/verify.py` (`simulator_file`, `check_turn`, `is_done`,
   `has_unknown_outcomes`, the fields of `verify.json`),
   `bot/prep/verify_job.py` (a job: how code runs next to a simulator),
   `bot/prep/prompts/codegen_contract.txt` (C1 to C11: what a simulator
   promises), `bot/prep/classify.py` (`load_analysis`, the fields
   `rules_examples` and `action_regex`), `bot/chat/decide.py` (how the model
   gets the engine candidates), `bot/metrics.py`, `tests/test_units.py`
   (ladder tests), `tests/test_verify.py` (fixture simulators you can reuse
   by importing them).
3. Branch (I change this line if another name must own the branch):

   ```
   git fetch origin
   git switch -c luckydevs-3-l3-wiring-fallback origin/main
   ```

   Never commit to `main`, never merge, rebase, cherry-pick or force push.
4. Time. Today is Saturday 14 Nov; T1 freeze 18:00, last merge 17:15. Do
   item 6 FIRST and commit it on its own: it is small, it only makes the
   bot safer, and it can still go into `t1` if it is ready and tested by
   17:00. Item 5 changes how the bot plays: it is for the `t2` image
   (freeze 23:59); do not rush it.
5. Ask me before: the first `git push`, any change outside the list in
   section 2, any step that takes the dev key spend of this session above
   0.50 USD. For everything else pick the sensible default, write it down,
   keep going. Batch questions.
6. Dev key: `python scripts/keyinfo.py` before and after. The key allows 60
   requests per minute for the whole team: run real-model matches ONE AT A
   TIME (five in parallel hit HTTP 429 and the bot fell to L1).
7. English, plain ASCII, standard library only, no `print`.

---------------------------------------------------------------------------

## 1. What exists now (checked on `main`)

- Prep: classify -> candidates -> verification in sandbox children ->
  `<variant_dir>/simulator.py` + `verify.json`; `spec.verified` and
  `spec.simulator_path` are set and stored in `meta.json`. With the real
  model all 10 variants of our 5 practice games were verified.
- `ladder.decide_turn`: reports L1 at once; L3 block: needs
  `spec.verified`, `spec.simulator_path` and an engine from
  `engines_base.get_engine(family)`, calls `engine.candidates(spec,
  turn_msg, deadline - 2.0)`, keeps the candidates within
  `config.CANDIDATE_MARGIN` of the best, reports the engine's best as the
  decision so far; then L2 (one model call) may pick among those candidates
  and writes the chat. Any exception in L3 means "not available".
- The four computation engines are stubs that raise `NotImplementedError`.
  So today L3 is entered and falls through: the log says
  `L3 not available: think_ahead engine not implemented yet`.
- `sandbox.Worker(path)`: lazy child, `call(fn, args, timeout)`,
  `job(job_file, payload, timeout)`, `close()`, restarts after a kill; at
  most 2 children per process; start plus load is about 20 ms; a job runs
  about 100000 simulator calls per second without round trips.
- `verify.check_turn(worker, turn_msg, variant, seat)` -> `(ok, reason)`:
  does the simulator parse this real turn and list exactly the arena's
  legal actions.
- `ladder.safe_action` with `legal_actions` null: the profile's example
  action; if that was rejected, a legal action of the prep sample turn; else
  `"pass"` (or `"0"`). `analysis.json` holds `rules_examples` (up to 8
  quoted example actions from the rules) and, when trusted, `action_regex`.

Facts that are easy to miss:

1. `spec.simulator_path` in `meta.json` is an absolute path written by the
   container that did the prep. Use `verify.simulator_file(spec)` and check
   that the file exists; never trust the stored string alone.
2. At `start` the spec can come from `specialist.lookup_by_label`
   (`source == "label"`): the rules text of this game differs from prep
   (another deal). The simulator was verified on the SAMPLE deal only. The
   live check of section 3.2 is what makes it safe to use.
3. A simulator's `legal_actions` may be a representative list when the
   module constant `LEGAL_ACTIONS_COMPLETE` is False (`verify.json`,
   `winner.legal_complete`).
4. Games with unknown outcomes (`verify.json` `unknown_outcomes`): the
   simulator draws dice and hidden values from `state["seed"]` (contract
   C11). Another outcome: copy the state and set another seed before
   `apply_action`. One fixed seed is ONE possible world, not the truth.
5. Simultaneous games: `current_player` is -1, `legal_actions` are ours,
   `apply_action(state, [seat 0 action, seat 1 action])` (C5).
6. `winner.both_seats` false means the module plays the opponent's turn
   inside `apply_action`; treat the game as a one-player game with chance.
7. `returns(state)` is `[seat 0, seat 1]` in [-1, 1]; our value is
   `returns(state)[state["me"]]`.
8. The watchdog answers at `clock.turn_hard` with the best decision
   reported so far. Answer time is the second tie-break of a knockout and
   the model call needs its share: the engine gets a fixed slice of the
   turn, not "whatever is left".
9. The container has 2 CPUs, 256 processes, and several of our specialists
   idle at once. A worker lives only while a game is being played.
10. Opponent chat never reaches a simulator, a job payload or anything that
    is executed.

---------------------------------------------------------------------------

## 2. Scope

| Path | What |
|---|---|
| `bot/ladder.py` | SHARED, I allow it for this task: `safe_action` (item 6) and the L3 block (item 5). Keep `decide_turn`'s signature and its "never raises" promise |
| `bot/main.py` | SHARED, I allow ONE small hook: tell the engine side when a game ends or the container shuts down, so the worker is closed |
| `bot/engines/base.py` | the shared machinery of simulator engines (section 3.1) |
| `bot/engines/playout.py`, `bot/engines/playout_job.py` (new, your names) | the baseline evaluator and its sandbox job (section 3.3) |
| `bot/engines/search.py`, `guess.py`, `simultaneous.py`, `bluff.py` | only so that they use the baseline until their real engine is written |
| `bot/prep/verify.py`, `bot/prep/classify.py` | small helpers only (for example a loader of the facts the ladder needs) |
| `tests/test_l3.py` (new) | your tests; do not edit existing test files, except that a test whose expectation you change on purpose is reported to me first |
| `READMEFIRST.md` | your rows |

Not in scope: real search (alpha-beta, MCTS with a tree), hypothesis
filtering, equilibrium, the chat modules, `bot/config.py`,
`bot/interfaces.py`, `bot/llm.py`, `bot/memory.py`, the prep pipeline.
Constants live in your files with the comment "proposed for bot/config.py".

Hard limits: generated code runs only in a sandbox child; nothing may
raise into `decide_turn` or delay the L1 report; every new path ends in
"go down one level"; no game-specific logic; the chat rule stays intact
(move and chat from one decision, opponent chat in the model call).

---------------------------------------------------------------------------

## 3. Item 5: L3 on a verified simulator

Goal: when a variant has a verified simulator, code proposes ranked
candidate moves on every turn, the model chooses among the near-best ones
and writes the chat, and a simulator that is wrong for THIS game is
detected and switched off before it costs a move.

### 3.1 Machinery every simulator engine shares (`bot/engines/base.py`)

- One `sandbox.Worker` per game being played, created lazily on the first
  turn from `verify.simulator_file(spec)`, reused on the next turns, closed
  at `end`, at `shutdown`, and when the next `start` is for another variant.
  An idle specialist holds no child.
- A per-game switch "L3 off for this game" with the reason, set by the live
  check or by any sandbox failure. Once off, the engine is not called again
  in that game (no repeated time loss). Reset at the next `start`.
- Time: the engine gets a slice of the turn (start from about 15 percent of
  the move clock, capped; tune it on the practice games), expressed as a
  fraction like the constants in `clock.py`. The job gets a little less
  than the slice; the parent timeout is the backstop.
- The result is a list of `Candidate(action, value, note)` with the arena's
  spelling (the ladder already maps through `match_legal`), values in
  [-1, 1] from OUR point of view, best first.
- One metrics-friendly summary per turn in the `why` of the decision and in
  the log: engine name, playouts or nodes, seconds, value of the best move,
  margin to the second.

### 3.2 Live consistency check (GAMEPLAN part D, done here)

On every turn, before any candidate is used: `verify.check_turn(worker,
turn_msg, start_msg["variant"], seat)`. On False: log the reason, switch L3
off for this game, continue with L2. Details to decide and document:

- when the arena sends a legal list, the simulator's list must be the same
  set; when it sends null, the simulator must parse the observation and
  list at least one action, and `legal_complete` decides how much the list
  is trusted;
- a turn that carries a `warning` after an L3 move means the simulator
  proposed something illegal: switch L3 off for the game;
- count mismatches per variant in `/memory` (a small JSON next to
  `verify.json`), so a simulator that fails live in several games is not
  used again by any of our containers. Keep it simple and atomic.

### 3.3 A baseline evaluator, so L3 really runs

The real engines come next. To wire and measure L3 now, write ONE generic
evaluator that works on any verified simulator, as a sandbox job:

- for each legal action of the parsed state: apply it and estimate our
  value by seeded random playouts to the end (flat Monte Carlo), cycling
  over the actions until the time slice ends, so every action gets about
  the same number of playouts;
- unknown outcomes: a different `state["seed"]` per playout;
- simultaneous step: for each of our actions, average over the opponent's
  actions (uniform); the candidates are our actions;
- an immediate win found by one `apply_action` is reported as such;
- when the playout count per action is below a minimum, or the list is not
  complete and long, return nothing: "not enough evidence" means L3 is not
  available this turn, and L2 decides alone.

Register it for the four simulator families through the existing stubs, in
a way the real engines can replace one family at a time.

This baseline is weak in deep games. That is why A.3 matters: the model
sees the candidates with their values and may pick only within
`CANDIDATE_MARGIN` of the best. Check that the margin makes sense with
Monte Carlo noise (a margin smaller than the noise turns the model into a
rubber stamp; a larger one lets it ignore the engine): pass the standard
error with each value and widen the kept set by it, or propose another rule,
and show numbers.

### 3.4 A switch and a measurement before it is on by default

- `BOT_L3` (read through `os.environ` in your code; proposed for
  `bot/config.py`): `0` disables L3 completely. Default on only if the
  measurement below supports it; otherwise default off and say so.
- Measure with the real model, one match at a time, on `tic_tac_toe`,
  `nim_deal`, `pig`, `rps_twist`, `guess_number`: `--opponent self` cannot
  compare two settings, so play L3-on against the random bot and L3-off
  against the random bot with the same seeds, and report per game: results,
  share of turns decided at level 3, how often the model left the engine's
  best move, live-check failures, average and maximum answer time, cost per
  match. `scripts/batch_runner.py` exists; use it if it fits.
- The answer time must not grow by more than the engine slice, and
  `RESULT: CLEAN` must hold everywhere.

---------------------------------------------------------------------------

## 4. Item 6: the fallback action when there is no legal list

Goal: with `legal_actions` null and the model failing (key out of budget,
rate limit, timeout), the bot still sends an action that the arena accepts,
and after a warning it never sends a second bad one if any better source
exists. A second bad answer loses the game and costs 1.25 robustness points.

Order of sources in `ladder.safe_action` when `legal` is empty (each one
skipped when it equals the rejected action, compared with `norm`):

1. a legal action from the verified simulator on the CURRENT observation
   (only when the live check passed for this turn; prefer one that is not
   obviously terminal-losing if the baseline has values, else the first);
2. the profile's example action, when it fully matches the trusted
   `action_regex` (or there is no regex);
3. the other quoted examples of the rules (`analysis.json`
   `rules_examples`), in order, each checked against the regex;
4. a legal action of the prep sample turn;
5. an action built from the regex or the action format when it is simple
   enough (your judgement; skip if it becomes guesswork);
6. the last resort of today.

Also use the warning itself: the `reason` text and the `rejected` action
tell what the arena did not accept; never repeat a rejected action within
the game (remember them per game, not only the last one).

Constraints: `safe_action` is called on the hot path before anything else
and by the watchdog's `emergency`; it must stay instant and must not start
a child by itself. So the simulator-derived action is computed by the L3
step and handed to L1 as an upgrade of the decision reported so far, and
the analysis facts are loaded once per game at `start`, not per turn.
With a legal list present, nothing changes.

Tests first for this item: the exact Guess the Number situation (null
list, model failing, first action rejected) must end in a legal second
answer when the rules quote another example; a rejected example is never
sent twice; with nothing usable the old last resort still comes out; no
exception on missing analysis, missing spec, garbage input.

---------------------------------------------------------------------------

## 5. Tests and checks

`tests/test_l3.py` with the fixture simulators of `tests/test_verify.py`
(a verified pile game is three lines of setup there): L3 produces
candidates and a level 3 decision; the model's choice outside the kept set
is replaced by the engine's best; a failing live check switches L3 off for
the rest of the game and the decision is level 2; a warning after an L3
move switches it off; a simulator that loops is killed inside the slice and
the turn is still answered in time; the worker is closed at game end and
none is alive while idle; `BOT_L3=0`; not enough playouts means level 2;
unknown-outcome and simultaneous fixtures give sane values; item 6 cases.

Then:

1. `python -m unittest`: everything passes.
2. `python scripts/practice.py --game <id> --prep --fake` on all five games:
   `RESULT: CLEAN` (the fake model produces no simulator: L3 never runs).
3. Real key, one match at a time: the measurement of 3.4.
4. `python scripts/build.py dev`, `python scripts/check_image.py dev`, one
   `--prep --image dev` match with the real key: L3 decisions inside the
   container, peak processes, answer times.
5. Model down: one match with a verified simulator in memory and
   `BOT_FAKE_LLM_MODE=fail` on a null-list game: every answer accepted.

---------------------------------------------------------------------------

## 6. Hand-over

Commits: item 6 first (`ladder: ...`), then item 5 (`engines: ...`). Update
your `READMEFIRST.md` rows. One "ready to merge" message (rule 8) with:
what changed and where; the measurement table; whether L3 is on by default
and why; the exact diff you made in `bot/ladder.py` and `bot/main.py`
explained in three lines each; proposals for `bot/config.py` (constants,
`BOT_L3`, and the `HARNESS_VERSION` bump that is still pending from the
verification work); what the real engines must implement to replace the
baseline for one family; every default you chose; the dev key spend.
