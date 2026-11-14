# PROMPT-FEATURE7-11: the four computation engines + specialist cache checked on load

You are working in the Git repository of team `luckydevs` (AI Harness Arena
hackathon). I am Sebi, the team lead. This file is your full briefing. Read
all of it before you touch anything.

The items (GAMEPLAN.md, parts C and D, luckydevs-3), in the order to do them:

7. Engine for Think ahead: iterative deepening alpha-beta with a time cap,
   MCTS when the branching is large, solved positions cached.
8. Engine for Guessing: hypothesis set filtering and best-split choice.
9. Read the opponent: payoff matrix from the simulator, equilibrium by
   regret matching, opponent frequency model, capped exploitation.
10. Bluff: sample hidden states consistent with the observations, expected
    value per action, mixed strategy so we are not readable.
11. Specialist cache keyed by rules hash and harness version, verified again
    on load.

This prompt is written to run BEFORE `docs/PROMPT-FEATURE5-6.md` (L3 wiring
and fallback action) and without it. It does not touch `bot/ladder.py` or
`bot/main.py`. Do not run the two prompts at the same time in the same
working tree.

---------------------------------------------------------------------------

## 0. How to work in this session

1. Read first: `RULES.md`, `READMEFIRST.md`, `docs/ARENA-CONTRACT.md` (kit
   notes K1 to K14), `GAMEPLAN.md` A.2 to A.4, C, D.
2. Then the code: `bot/ladder.py` (read only: the L3 block and
   `_within_margin`), `bot/engines/base.py` and the four stubs,
   `bot/interfaces.py` (`Candidate`, `Engine`, `VariantSpec`),
   `bot/prep/sandbox.py` (docstring: the engine example),
   `bot/prep/verify.py` (`simulator_file`, `check_turn`, `is_done`,
   `has_unknown_outcomes`, `verify.json`), `bot/prep/verify_job.py` (how a
   job plays games on a simulator; the shared action policy),
   `bot/prep/prompts/codegen_contract.txt` (C1 to C11),
   `bot/prep/classify.py` (`load_analysis`), `bot/specialist.py`,
   `bot/memory.py`, `bot/opponents.py`, `bot/chat/decide.py` (how candidates
   reach the model), `bot/clock.py`, `tests/test_verify.py` (fixture
   simulators: a pile game, matching pennies, a die race), `testgames/*.py`.
3. Branch (I change this line if another name must own the branch):

   ```
   git fetch origin
   git switch -c luckydevs-3-engines origin/main
   ```

   Never commit to `main`, never merge, rebase, cherry-pick or force push.
4. Time. Saturday 14 Nov. This is for the `t2` image: freeze 23:59, last
   merge about 22:45. ONE COMMIT PER ENGINE, each one mergeable alone, in
   the order above; tell me "ready to merge" after each. If time runs out, a
   solid Think ahead and Guessing beat four half engines. Item 11 is small:
   do it after item 7.
5. Ask me before: the first `git push`, any change outside section 2, dev
   key spend of this session above 1.00 USD. Otherwise decide, write it
   down, keep going.
6. Dev key: `python scripts/keyinfo.py` before and after. 60 requests per
   minute for the whole team: real-model matches ONE AT A TIME.
7. English, plain ASCII, standard library only, no `print`.

---------------------------------------------------------------------------

## 1. What you build on (checked on `main`)

- Prep leaves, per variant, a VERIFIED simulator: `verify.simulator_file(spec)`
  (`<variant_dir>/simulator.py`), `verify.json`, `spec.verified`. With the
  real model all 10 variants of the 5 practice games were verified.
- The ladder already calls you: when `spec.verified` and
  `spec.simulator_path` are set, `ladder.decide_turn` calls
  `engines_base.get_engine(family).candidates(spec, turn_msg, deadline - 2.0)`
  and expects `list[Candidate(action, value, note)]`. It keeps the
  candidates within `config.CANDIDATE_MARGIN` (0.05) of the best; the model
  (one call, L2) may choose only among them and writes the chat. An
  exception or `[]` means "L3 not available this turn": L2 decides alone.
  SO THE MOMENT AN ENGINE RETURNS CANDIDATES, PLAY CHANGES.
- Today the four engines raise `NotImplementedError`.
- Sandbox: `sandbox.run_job(sim_path, job_file, payload, timeout)` never
  raises and returns `{"ok", "result" | "error", ...}`; a job file is OURS,
  standalone (standard library only, nothing from `bot`), defines
  `run(sim, payload)` and runs inside the child next to the simulator: about
  100000 simulator calls per second, no round trips. Child start plus load:
  about 20 ms. At most 2 children per process. Memory 512 MB, CPU backstop.
- Simulator speed measured in prep (`verify.json`, `winner`):
  `playouts_per_s` (335 for Guess the Number 1-1000, 2900 for Pig, 9500 to
  42000 for the board games), `steps_per_game`, `legal_complete`,
  `both_seats`, and at top level `unknown_outcomes`.

What a simulator promises (contract C1 to C11), in the words you need:

1. `state = parse_observation(observation, variant, seat)`: OUR view;
   `state["me"]` is our seat; hidden values are `None`.
2. `legal_actions(state)`: the mover's actions in the arena's spelling;
   `[]` exactly when terminal. When the module constant
   `LEGAL_ACTIONS_COMPLETE` is False the list is only representative.
3. `apply_action(state, action)` is pure. Alternating step: one action
   string, played by `current_player(state)`. Simultaneous step
   (`current_player` is -1): a list `[seat 0 action, seat 1 action]`, and
   `legal_actions` are OURS (assume the opponent has the same set unless the
   game shows otherwise).
4. `returns(state)`: `[seat 0, seat 1]` in [-1, 1], meaningful when
   terminal. Our value: `returns(state)[state["me"]]`.
5. Unknown outcomes (C11): dice and hidden values are drawn inside
   `apply_action` from `random.Random(state["seed"])`, recorded in the new
   state, and a new seed is stored. One seed is ONE possible world. Another
   world: `s = dict(state); s["seed"] = k` before applying. A hidden value
   once drawn stays fixed in that line of play.
6. `winner.both_seats` false: the module plays the opponent's turn itself
   inside `apply_action`; search it as a one-player game with chance.

Facts that are easy to miss:

1. Use `verify.simulator_file(spec)`, not the string in
   `spec.simulator_path` (an absolute path from the container that did the
   prep), and check that the file exists.
2. The spec at `start` may come from `specialist.lookup_by_label`: this
   game's deal differs from the prep sample. The simulator was verified on
   the sample deal only. THE FIRST THING YOUR JOB DOES every turn is the
   live check: parse the current observation and compare the legal actions
   with the arena's list when there is one (same set). On a mismatch or an
   exception return nothing, and remember "off for this game" in the engine
   (keyed by match id and game index from the messages you are given, or by
   the observation stream; reset when a new game starts) so the time is not
   lost again every turn.
3. Time. The watchdog answers at 13 s of 20; the model call needs up to
   8 s; total answer time is the second knockout tie-break. An engine gets
   a SLICE: start at about 15 percent of the move clock (3 s of 20), as a
   fraction of `turn_msg["seconds"]`; the job is told its budget and stops
   by itself (iterative deepening, anytime MCTS); the parent timeout is the
   backstop. Never use "whatever is left of the deadline".
4. One fresh child per turn is fine (20 ms) and needs no lifecycle hook:
   an idle specialist then holds no child. What must survive between turns
   lives in the engine object (small) or in `/memory`.
5. Opponent chat, and anything derived from it, never goes into a job
   payload. The model sees your candidates AND the chat and makes the one
   decision; that is where chat influences the move.
6. Values must be comparable with `CANDIDATE_MARGIN` = 0.05 on a [-1, 1]
   scale: exact search values are exact; Monte Carlo values are noisy. Put
   the number of samples (or "solved") in `Candidate.note`, and when values
   are noisy, return only candidates whose value is distinguishable or say
   in the hand-over what margin rule the ladder should use (`ladder.py` is
   not yours to edit).
7. No game-specific logic (rule 22). An engine knows a FAMILY and the
   simulator interface, never a game.

---------------------------------------------------------------------------

## 2. Scope

| Path | What |
|---|---|
| `bot/engines/base.py` | the helper every simulator engine uses: run a job with a time slice, live check result, "off for this game", `Candidate` building, the switch below |
| `bot/engines/search.py` + `search_job.py` (new) | item 7 |
| `bot/engines/guess.py` + `guess_job.py` (new) | item 8 |
| `bot/engines/simultaneous.py` + `simultaneous_job.py` (new) | item 9 |
| `bot/engines/bluff.py` + `bluff_job.py` (new) | item 10 |
| `bot/specialist.py`, `bot/prep/verify.py` (a helper only) | item 11 |
| `tests/test_engines.py` (new) | your tests; do not edit existing test files |
| `READMEFIRST.md` | your rows |

A job file may share code with another only by being self-contained (a job
cannot import from `bot`); a small common file that jobs load by path is
fine if you document how.

Not in scope: `bot/ladder.py`, `bot/main.py`, `bot/config.py`,
`bot/interfaces.py`, `bot/llm.py`, `bot/memory.py`, `bot/chat/`, the prep
pipeline, `bot/engines/truth.py`, `deals.py`, `guardrails.py` (luckydevs-2).
Constants live in your files, marked "proposed for bot/config.py".

Switch: `BOT_L3` (read from the environment in `base.py`; proposed for
`bot/config.py`): `0` turns every simulator engine off; a comma list of
families turns on only those. Default: the families whose measurement in
section 8 supports it. Write the default down in the hand-over.

Hard limits: generated code runs only in a sandbox child; an engine never
raises anything except through "no candidates"; it never exceeds its slice;
no child is alive after `candidates` returns.

---------------------------------------------------------------------------

## 3. Item 7: Think ahead

For perfect-information games without chance (`unknown_outcomes` false).

- Iterative deepening alpha-beta (negamax over `returns` from the mover's
  side; two-player zero-sum is the assumption, check it on terminal states
  and fall back to MCTS if `returns` is not zero-sum), with a transposition
  table keyed by the canonical JSON of the state (drop `seed`), move
  ordering from the previous iteration, and a clean stop at the budget: the
  result of the last COMPLETED depth is what counts.
- When the search reaches the end of the game for every line, the position
  is SOLVED: values are exact (-1, 0, 1), say so in the note. Cache solved
  root positions in `/memory` (per variant, small, atomic write) so the
  same position costs nothing next time and in other containers.
- Depth-limited leaves need a value without game knowledge: a few seeded
  random playouts from the leaf, or 0 when there is no time. Decide and
  measure.
- When the branching factor or `steps_per_game` makes even depth 2 too slow
  for the slice (decide from `verify.json` before searching), use MCTS
  (UCT) with random playouts instead; anytime, values are mean returns.
- `both_seats` false or a chance game that lands here: MCTS with a new
  `seed` per playout (expectation over outcomes), never alpha-beta.
- Candidates: every root action with its value, best first.

Done means: on `tic_tac_toe` 3x3 the engine never loses and takes every
immediate win (show it by self-check in a test with a fixture and by
matches); on `nim_deal` it plays the winning move whenever one exists
(compare with the nim-sum in a TEST only, never in the engine).

---------------------------------------------------------------------------

## 4. Item 8: Guessing

For deduction-from-feedback games. The simulator is the oracle of
consistency: the state parsed from the observation holds the feedback so
far, and C11 draws a hidden value "consistent with all feedback so far".

- Hypothesis set: sample hidden worlds by seeds (apply one legal action
  under many seeds and read which hidden value was drawn from the new
  state; find the hidden field generically as the part of the state that
  differs between seeds), until no new value appears or the budget share is
  used. Small sets become exact, large ones stay a sample; say which in the
  note.
- For each candidate action, the distribution of the feedback it would get
  over the hypotheses (apply the action in each world, compare what changed
  in OUR view). Value: expected remaining hypotheses (or entropy), mapped
  into [-1, 1], with an action that would be correct in a world counted as
  ending the game. Best-split choice.
- Candidate actions when the legal list is long (`legal_complete` and more
  than a cap): score the actions that are themselves hypotheses plus a
  spread sample of the others.
- If the game is a race or has a cap of guesses, the simulator knows: when
  few guesses remain, prefer the action most likely to be right over the
  best split. Use playouts of your own policy to check, if time allows.

Done means: on `guess_number` (both variants, `legal_actions` null in the
arena) the engine's first guesses halve the range and it finds the number
in about log2(n) guesses against the random bot.

---------------------------------------------------------------------------

## 5. Item 9: Read the opponent

For simultaneous-move games.

- Payoff matrix for the current step: for every pair (our action, their
  action) the value of the resulting state: exact when the step ends the
  game, else a few random playouts of the rest (or one recursion level when
  the game is a short repeated game).
- Equilibrium mixed strategy by regret matching on that matrix.
- Opponent model: the frequencies of this opponent's past actions in this
  match, read from the state (the history is in the observation, so in the
  parsed state) and, across matches, from what `bot/opponents.py` already
  stores if it has it (read only; propose additions in the hand-over).
- Capped exploitation: best response to the model, mixed with the
  equilibrium by a weight that grows with the number of observations and
  has a cap, so a deceptive opponent cannot farm us.
- Candidates and the margin: a mixed strategy must not be turned into "the
  argmax every time". Sample OUR action from the final mix in code (seeded
  from the match and turn, never from chat), and return it as the top
  candidate, with the other actions that have at least a minimum
  probability as alternatives valued so that they stay inside the ladder's
  margin; the note carries the probabilities. The model then picks among
  actions the mix supports and can use the chat to bluff about it.

Done means: on `rps_twist` (a win with rock pays double) the equilibrium is
not uniform and matches the analytic one for a single round; against a bot
that always plays one action the exploitation shows within a few rounds.

---------------------------------------------------------------------------

## 6. Item 10: Bluff

For hidden-information games with betting or challenges.

- Sample hidden worlds consistent with what we saw: seeds, as in item 8.
- Expected value per action: for each of our legal actions, average over
  worlds of playouts in which the opponent follows a simple policy (uniform
  over its legal actions is the baseline; a policy that uses its own hidden
  value in the sampled world is better: one extra playout level).
- Mixed play so we are not readable: softmax over the action values with a
  temperature tied to the value spread, sample in code as in item 9, and
  hand the ladder a top candidate plus the alternatives the mix supports.
- Use the opponent model of item 9 when the state shows their past actions.

This engine is the least certain to pay off before the freeze. If item 9
is not solid by about 21:30, hand over without item 10 and say so.

---------------------------------------------------------------------------

## 7. Item 11: the specialist cache is checked on load

Today `memory.load_variant` trusts `meta.json`: `verified` and an absolute
`simulator_path`. At `start` (warm 30 s clock, cold 120 s) nothing checks
that the simulator is really there and is the verified one.

- When a spec is taken from memory or from the label lookup
  (`specialist.lookup`, `lookup_by_label`, `for_start`): run
  `verify.is_done(spec)` (no child: harness version, contract hash, the
  winner's hash against the file on disk); it sets or clears
  `spec.verified` and `spec.simulator_path` for THIS container's path.
- Do it once per spec per process, not on every turn.
- Never start a child on the start path; the per-turn live check of the
  engines covers behavior.
- A variant whose simulator failed the live check in several games should
  stop being used by all our containers: a small counter file next to
  `verify.json`, atomic, read by `is_done` or by the engine helper. Keep it
  simple.

---------------------------------------------------------------------------

## 8. Tests and checks

`tests/test_engines.py`: fixture simulators from `tests/test_verify.py`
plus small ones you write (a solvable board game, a number guessing game
with a seeded secret, a biased simultaneous game, a tiny betting game).
Per engine: sane ranked candidates; the "done means" of its section; the
live check turns the engine off for the game on a wrong simulator; the
slice is respected with a slow simulator and with one that loops (killed,
`[]`, no child left); `BOT_L3`; not verified or file missing means no
candidates; nothing raises. Item 11: a changed or missing `simulator.py`
clears `verified` at load; the right one sets the local path.

Then, after EACH engine:

1. `python -m unittest`: everything passes.
2. `python scripts/practice.py --game <id> --prep --fake` on the games of
   that family: `RESULT: CLEAN` (no simulator with the fake model: the
   engine must stay silent).
3. Real key, one match at a time, against the random bot and against
   ourselves with the engine off (`--opponent self` with `--set` cannot set
   one side only, so compare engine-on versus random and engine-off versus
   random with the same seeds): results, share of level 3 decisions, how
   often the model left the top candidate, live-check failures, average and
   maximum answer time, cost per match. The answer time may grow by the
   slice, not more. Every run `RESULT: CLEAN`.
4. Before the last hand-over: `python scripts/build.py dev`,
   `python scripts/check_image.py dev`, one `--prep --image dev` match per
   engine family with the real key (2 CPUs, 2 GB, 256 processes).

---------------------------------------------------------------------------

## 9. Hand-over (after each engine, short; full at the end)

Rule 8 message: what changed, files, tests, the measurement table, risks.
At the end also: the `BOT_L3` default and why; proposals for
`bot/config.py` (constants, the slice, `BOT_L3`, the pending
`HARNESS_VERSION` bump), for `bot/ladder.py` (the margin rule for noisy
and for mixed candidates, if the current one does not fit), for
`bot/opponents.py` (what the opponent model wants stored); what
`docs/PROMPT-FEATURE5-6.md` no longer has to do because you did it (the
live check, the baseline evaluator is not needed for families with a real
engine) and what it still must do (the fallback action, closing nothing:
you hold no worker between turns); every default you chose; the dev key
spend.
