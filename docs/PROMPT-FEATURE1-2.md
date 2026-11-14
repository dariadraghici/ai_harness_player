# PROMPT-FEATURE1-2: game profile + simulator generation (prep)

KIT UPDATE (14 Nov, after 14:00): read `docs/PROMPT-FEATURE1-2-KIT-UPDATE.md`
after this file. Where the two differ, the update wins (sample turn seat,
rules text of `start` vs `prep`, practice commands).

You are working in the Git repository of team `luckydevs` (AI Harness Arena
hackathon). I am Sebi, the team lead. This file is your full briefing for two
features of the prep pipeline. Read all of it before you touch anything.

The two features (GAMEPLAN.md, part C, luckydevs-3):

1. Game profile and family classification from the rules text (a basic LLM
   version exists in `bot/prep/classify.py`).
2. Simulator generation: several candidates in parallel, fixed interface
   (`bot/interfaces.py`, `SIMULATOR_FUNCTIONS`).

---------------------------------------------------------------------------

## 0. How to work in this session

1. Read first, in this order: `RULES.md`, `READMEFIRST.md`,
   `docs/ARENA-CONTRACT.md`, then `GAMEPLAN.md` parts A.2 to A.4 and C. The
   rules in `RULES.md` are mandatory; this file does not override them.
2. Then read the code you will touch or depend on: `bot/prep/` (all five
   files), `bot/specialist.py`, `bot/interfaces.py`, `bot/config.py`,
   `bot/llm.py`, `bot/memory.py`, `bot/chat/decide.py` (`template`, `fill`,
   `parse_json_object`), `bot/ladder.py` (`safe_action`, the L3 block),
   `bot/prompts/classify_*.txt`, `tests/session_script.py`,
   `tests/test_protocol.py`, `testgames/*.py`, `arena_local/runner.py`
   (`_sample_turn` and the `prep` and `start` messages).
3. You work in the area of luckydevs-3. Branch name (I change this line if
   another name must own the branch):

   ```
   git fetch origin
   git switch -c luckydevs-3-prep-profile-codegen origin/main
   ```

   Never commit to `main`, never merge, rebase, cherry-pick or force push.
4. Time. Today is Saturday 14 Nov. T1 freeze is 18:00, last merge 17:15.
   Target: hand-over by 16:00. Do feature 1 first and commit it on its own
   (it improves play today and must be mergeable alone). Then feature 2.
   Prefer simple code that works over elaborate code.
5. Ask me before: the first `git push` of the branch, any change to a file
   outside the list in section 3, and any step that would take the dev key
   spend of this session above 0.30 USD. For everything else pick the
   sensible default, write it down for the hand-over, and keep going. When
   you ask, batch the questions in one message.
6. Dev key: run `python scripts/keyinfo.py` before your first real call and
   at the end, and report the difference. The key is shared by the team:
   at most 6 model calls in flight at once, no large batches. Use
   `BOT_FAKE_LLM=1` (or a mocked `llm.complete`) for everything that does
   not need the real model.
7. Every file you write is English, plain ASCII (rule 28).

---------------------------------------------------------------------------

## 1. Why these two features matter

The plan (GAMEPLAN A.2): in prep the model writes a simulator of the game,
the harness tests and repairs it; in a match, code computes the move on the
verified simulator (L3) and one cheap model call picks the final move and
the chat line. Prep spend does not count for the cost score, so expensive
model work belongs here.

- The profile is consumed TODAY, on every turn, by the code that already
  plays: `chat/decide.py` puts the family (which selects the chat tactics),
  the playbook, the action format and the example action into the decision
  prompt; `ladder.safe_action` sends `profile.example_action` when
  `legal_actions` is null; the guardrails filter (luckydevs-2) will read
  `profile.speech_restrictions`. A wrong family or a wrong example action
  costs games now. The family also selects the L3 engine later.
- Simulator candidates are the raw material for the next features (sandbox,
  verification, engines). Nothing plays on them until verification exists.
  So feature 2 must have zero effect on how the bot plays today, and zero
  risk for the protocol loop.

---------------------------------------------------------------------------

## 2. What exists now (checked in the code on `main`)

- `classify.classify_variant(game_name, label, rules, sample_turn, timeout,
  thinking=True)` makes one model call and returns `(GameProfile, playbook)`.
  It never raises. When the model gives nothing usable the profile is the
  default: family `unknown`, example action from `example_from_rules` or
  from the sample's first legal action. Nothing the model returns is checked.
- `specialist.build_variant` calls it and saves through
  `memory.save_variant`. It is used by prep (`pipeline.run_prep`, 4 variants
  in parallel) AND by the cold start path (`specialist.for_start`, no
  `sample_turn`, thinking only when more than 45 s are left).
- `codegen.generate_simulators(spec, deadline, n_candidates=3)` and
  `codegen.repair_simulator(spec, path, failure_report, deadline)` are stubs
  that raise `NotImplementedError`. `sandbox.py` and `verify.py` are stubs
  too and stay stubs in this session.
- The simulator contract is the comment block above `SIMULATOR_FUNCTIONS` in
  `bot/interfaces.py`: six top-level functions, JSON-serializable state.
- `llm.complete(messages, max_tokens, timeout, thinking_budget, tag)` returns
  the text or `None`, never raises, has a hard wall-clock timeout. It has no
  temperature parameter and does not report the finish reason.

Facts that are easy to miss (each one will bite you if ignored):

1. `bot/llm.py` (fake mode) imports `example_from_rules` from
   `bot/prep/classify.py` and answers the classify call only when the prompt
   contains the text `TASK: classify`. Keep that function name and keep that
   marker line in the user message. `tests/test_protocol.py` test 5 expects
   the example action `pick 7` taken from the rules text on a cold start.
2. In fake mode every other prompt gets a JSON action object as the answer.
   So with `BOT_FAKE_LLM=1` a codegen call returns something that is not
   Python. Your code must treat that as "no candidate" quickly and quietly.
3. `BOT_FAKE_LLM_MODE=hang` makes `llm.complete` sleep and IGNORE its
   timeout (test 6: prep clock 6 s, hang 30 s). After the watchdog answered
   `ready`, the prep worker thread is still alive. If it starts new model
   calls when it wakes up, they run during the match. In the real arena that
   is worse: after `start` the key is the match key, so a late prep call
   spends match budget, which is what the cost score measures. Rule: check
   the deadline before EVERY model call; never start a call after it.
4. A `sample_turn` is one `turn` message. It has NO `seat` field (the seat
   comes with `start`). It may be missing. Its `legal_actions` is `null`
   when there are more than 50 actions.
5. A prep variant has `label`, `rules` and maybe `sample_turn`, nothing
   else. The `settings` of a variant arrive only with `start`. So every
   parameter of a variant must be read from its rules text.
6. `GameProfile.from_dict` drops keys it does not know, and
   `memory.save_variant` stores only the profile fields. `interfaces.py` and
   `memory.py` are not yours to edit.
7. `memory.save_variant` writes `meta.json` last, and a variant counts as
   present only when `meta.json` exists. `pipeline.run_prep` skips the build
   for a variant found in the cache.
8. The playbook is pasted into the system prompt of every decision call in
   a match. Match tokens are the cost score. Keep it short.
9. `.dockerignore` drops `*.md`; prompt templates are `.txt` files inside
   `bot/` so they reach the image.

---------------------------------------------------------------------------

## 3. Scope

Files you may change or create:

| Path | What |
|---|---|
| `bot/prep/classify.py` | feature 1 |
| `bot/prep/codegen.py` | feature 2 |
| `bot/prep/pipeline.py` | wiring of both, time budget |
| `bot/specialist.py` | only the minimum the wiring needs |
| `bot/prep/prompts/*.txt` (new folder) | your prompt templates, with a small loader in `bot/prep/` |
| `tests/test_prep.py` (new file) | your unit tests. `tests/` belongs to luckydevs-1; I allow this ONE new file. Do not edit any existing file in `tests/` |
| `READMEFIRST.md` | only the rows of the luckydevs-3 table (status of your files, your new files) |

`bot/prompts/` belongs to luckydevs-2. Do not edit
`bot/prompts/classify_system.txt` or `classify_user.txt`; write your new
classify prompt in `bot/prep/prompts/`, stop loading the old two, and say so
in the hand-over (I decide whether the old files are deleted).

Not in scope, do not touch: `bot/prep/sandbox.py`, `bot/prep/verify.py`, the
engines, `bot/ladder.py`, `bot/interfaces.py`, `bot/config.py`, `bot/llm.py`,
`bot/memory.py`, `bot/main.py`, `bot/chat/`, `bot/prompts/`, `scripts/`,
`arena_local/`, `testgames/`, `GAMEPLAN.md`, `RULES.md`, `Dockerfile`. When
you need something there (a new `GameProfile` field, a config constant, a
contract clarification), work without it and PROPOSE it in the hand-over
(rule 11). Constants you need now live as module constants in your own
files, clearly named, with a comment "proposed for bot/config.py".

Hard limits of this session:

- Generated simulator code is NEVER imported, `exec`-ed, `eval`-ed or run
  inside the bot process, and not in a child process started by the bot
  either: that is the job of `sandbox.py`, which does not exist yet. In this
  session the bot only produces candidate FILES and inspects them as text
  (`ast`).
- `spec.verified` stays `False` and `spec.simulator_path` stays `None`, so
  the ladder keeps L3 off. Do not write a file named `simulator.py`; that
  name is reserved for the verified winner.
- Only `llm.complete` talks to a model. No other endpoint, library or tool.
- No game-specific logic: no game ids, no game names, no test for a
  particular title, in code or in prompts (rule 22). Signals are about kinds
  of rules ("both players choose at the same time"), never about a game.
- Standard library only. No `print`. Log through `bot/log.py`.
- Nothing you add may raise into `pipeline.run_prep`, `build_variant` or
  `for_start`. A failure means: keep the profile we have, no candidates.

---------------------------------------------------------------------------

## 4. Feature 1: game profile and family classification

Goal (from the docstring of `classify.py`): a robust profile (family,
information, move order, speech restrictions, exact action format, a valid
example action) and a playbook, cross-checked against the rules text and
against `sample_turn` when present. "Robust" means: the result is never
worse than today's default, it is still useful when the model is down, and
no field is stored without a code check.

Build it in three layers.

### 4.1 Evidence from code (no model)

A pure function that reads the rules text and the optional `sample_turn` and
returns plain facts. It runs in milliseconds and cannot fail (wrap it).

- From the sample turn: is `legal_actions` a list or null, how many, what
  the strings look like; the observation text; whether the chat has entries
  with `kind` `move` (talking is the move).
- From the rules text: the sentence or sentences that define the action
  format; quoted example actions (`example_from_rules` exists; keep its name
  and its current results); generic signals per family, for example
  simultaneous choice, hidden or private information and betting, offers
  and acceptance or bids, feedback on a guess, a board and alternating
  moves, a document to answer from with an "unknown" answer, a subject that
  must not be mentioned.
- A score per family from those signals, and a "confident or not" flag.

Uses: (a) the fallback profile when the model call fails (today that is
family `unknown`, which loses the family chat tactics and later the engine);
(b) the cross-check of the model's answer.

### 4.2 The model call

- New templates in `bot/prep/prompts/`. Keep the user message starting with
  the line `TASK: classify`. Stable part first (instructions), then game
  name, variant label, rules, and the sample turn (`observation`,
  `legal_actions`, `turn`).
- One JSON object as the answer, parsed with `parse_json_object`. Ask for
  the existing fields (`family`, `perfect_information`, `simultaneous`,
  `speech_restrictions`, `action_format`, `example_action`, `playbook`) plus
  the few extras of 4.4. Ask for a short verbatim quote from the rules that
  supports the family; code checks that the quote really is in the rules.
- Explain to the model what each field is used for (the example action is
  SENT as our move when everything else fails, so it must be valid as is;
  the speech restriction feeds a filter on everything we say), so it knows
  what precision is needed.
- `speech_restrictions`: only what the rules literally forbid us to say,
  short, no commentary, empty when the rules forbid nothing. luckydevs-2
  reads this field in the guardrails filter.
- Playbook: at most 12 short lines and at most about 900 characters (cut it
  in code): how to win, what to say in chat, what to watch in the
  opponent's chat, what never to say.
- Thinking on in prep (`config.PREP_THINKING_BUDGET`); the `thinking`
  argument and the `timeout` argument of `classify_variant` keep their
  meaning for the cold path.

### 4.3 Checks and repair in code

Check every field before it is stored; decide with these rules:

- `family` must be in `FAMILIES`. Model says `unknown` or something invalid:
  use the evidence family when it is confident. Model and confident evidence
  disagree, or the quote is not in the rules: one repair call (below).
- `simultaneous`, `perfect_information`: booleans; cross-check with the
  evidence; a family that contradicts them (for example `think_ahead` with
  hidden information) counts as a disagreement.
- `example_action`. Sample has a legal list: the example must be one of its
  strings (same matching as `ladder.match_legal`: exact, then case and
  whitespace insensitive, stored in the arena's spelling); otherwise use the
  sample's first legal action. No list: the example must match the action
  format. Ask the model for a Python regular expression that every valid
  action fully matches; trust the regex only when it compiles, matches the
  model's example, and matches every quoted example found in the rules;
  when in doubt prefer the quoted example from the rules verbatim, because
  it is the only string that is valid by construction. Never store an empty
  example when any source has one.
- `action_format` must not be empty: fall back to the format sentence from
  the rules text.
- Family `guardrails` with an empty `speech_restrictions` is a disagreement.
- Variants of one game share a family. After all variants of a prep are
  classified, align them on the majority family (tie: the one the evidence
  supports), and log every change.

Repair: at most ONE extra model call per variant, only when there is a
disagreement and the remaining time allows it, with the specific complaint
("you said X, but the rules say ... ; answer again"). After that, code
decides: validated model answer, else evidence, else today's default. Total
time of `classify_variant`, repair included, stays inside its `timeout`.

Keep `classify_variant` returning `(GameProfile, playbook)` so
`specialist.build_variant` and the fake LLM path keep working.

### 4.4 Extra facts for later stages

Codegen and the future engines need a few facts that `GameProfile` has no
field for. Keep this list small; add a fact only when something reads it:

- family confidence and the runner-up family, the supporting quote, the
  list of disagreements found (for logs, metrics and the presentation);
- the action regex (when trusted);
- turn structure: alternating, simultaneous, or talk-is-the-move;
- hidden information and chance in one short phrase each;
- whether a simulator is worth generating (4 families use one: think_ahead,
  guessing, read_opponent, bluff; `unknown` only when time is left; not
  truth, guardrails or deals for now). Decide this in code from the family.

Because `interfaces.py` and `memory.py` are shared, store these in a file of
your own, `analysis.json`, in the variant folder
(`memory.variant_dir(game_id, rules_hash)`, written with
`memory.write_json_atomic`, BEFORE `memory.save_variant` writes `meta.json`),
with a small loader in `classify.py`. In the hand-over, propose which of
them should become `GameProfile` fields.

---------------------------------------------------------------------------

## 5. Feature 2: simulator generation

Goal: during prep, for every variant that is worth it, ask the model for
several different candidate simulator modules at the same time, keep the
ones that pass a static check, repair the others once, and leave the files
and a manifest in `/memory` for the verification stage.

### 5.1 The interface is fixed

Six top-level functions, exactly these names and parameters:
`parse_observation(observation, variant, seat)`, `legal_actions(state)`,
`apply_action(state, action)`, `is_terminal(state)`, `returns(state)`,
`current_player(state)`. The state is JSON-serializable. Do not rename, add
or remove functions.

The draft contract leaves some points open. A candidate is useless if each
one answers them differently, so pin them down ONCE, in one template file
(for example `bot/prep/prompts/codegen_contract.txt`) used by the generation
prompt and the repair prompt, and list them in the hand-over so I can copy
them into `bot/interfaces.py`. The open points I found, with my default:

1. `variant`: may be `{}` or only `{"label": ...}`. All parameters of the
   variant are constants in the module, read from this variant's rules text.
   One simulator per variant (per rules hash).
2. `seat`: the sample turn has no seat. When the observation itself says
   which side we are, the observation wins; `seat` is used only when it
   does not. The state records our seat.
3. The state is OUR view (an information state). What we cannot see is
   stored as unknown (`None`), never invented.
4. A turn message exists only when we must act, so right after
   `parse_observation`, `current_player(state)` is our seat, or `-1` for a
   simultaneous step.
5. Simultaneous step: `legal_actions(state)` returns OUR legal actions;
   `apply_action(state, action)` receives a JSON list of two action
   strings, seat 0 first. (Choose another convention if you find a better
   one, but choose exactly one and write it down.)
6. `legal_actions`: the exact arena spelling, and for the sample
   observation exactly the sample's `legal_actions`, in the same order.
   When the arena sends null (more than 50 actions) the function still
   returns the full list if it is finite and reasonably small, else a
   representative list; say which in a module constant.
7. `returns(state)`: one float per seat, higher is better for that seat;
   win 1, loss -1, draw 0 for win/lose games; for points games the point
   difference scaled into [-1, 1].
8. Purity: no function mutates its arguments; no global state is modified;
   no I/O; the same input gives the same output.

### 5.2 The generation prompt

Give the model everything it needs and say why:

- the full rules text of this variant, the label, the profile and the
  analysis facts of feature 1;
- the sample turn verbatim (`observation` and `legal_actions`), described as
  the first test the code will face: `parse_observation` must accept exactly
  this text, and `legal_actions` of the result must equal this list;
- the contract file of 5.1;
- the code rules: Python 3.12, standard library only, imports only from a
  short allowlist (for example `math`, `random`, `itertools`, `collections`,
  `re`, `json`, `copy`, `functools`, `heapq`, `string`, `fractions`); no
  file, network, process, thread or environment access; no `print`, `input`,
  `eval`, `exec`, `open`, `__import__`; no code that runs at import time
  except constants; loops must end; parse the observation defensively and
  raise `ValueError` with a clear message when the text does not fit;
- the output format: the complete module in ONE fenced python block,
  nothing else.

### 5.3 Several candidates in parallel

- `n_candidates` (default 3) calls at the same time. `llm.complete` has no
  temperature, so the candidates must differ by the prompt: give each one a
  different working brief (for example: 1. smallest possible state, direct
  transcription of the rules; 2. start from the sample observation, write
  the parser first, then the rules; 3. list the rules as numbered checks,
  then write code that names each check). You may also vary the thinking
  budget. Tag the calls `codegen-<n>`.
- Token room: a simulator is long and thinking tokens count toward
  `max_tokens`. `config.PREP_MAX_TOKENS` (4500 with thinking 2000) is too
  small. Use your own constants, for example thinking 3000 to 4000 and
  `max_tokens` about 12000, timeout about 150 s and never past the deadline.
- Use your own `ThreadPoolExecutor` for the candidate calls (do not submit
  them to the pool that runs the variants: a bounded pool waiting on itself
  deadlocks). One process-wide cap on model calls in flight (semaphore,
  default 6) so several variants do not flood the gateway or the 2 CPUs.
- Deadline discipline: before each call compute the time left; skip the
  call when less than a minimum is left; pass `timeout = min(cap, time
  left - margin)`; collect with a wait that times out at the deadline; do
  not wait for stragglers (`shutdown(wait=False)`); a straggler that returns
  after the deadline writes nothing and starts nothing.
- A cut-off or empty answer is normal (ARENA-CONTRACT 2.6): it is
  simply a failed candidate.

### 5.4 From answer to candidate file

1. Extract the code: the largest fenced python block; else the whole text if
   it parses. Size cap (for example 60 KB).
2. Static check, text only, with `ast`: it parses; all six functions are
   defined at top level with the right number of parameters; imports only
   from the allowlist; none of the forbidden names; top level contains only
   imports, constant assignments, function definitions and a docstring.
   Return a list of human-readable failures.
   This check is hygiene and an early repair signal. It is NOT a security
   boundary; the sandbox will be. That is why nothing here executes the
   code.
3. Write every candidate, good or bad, to
   `<variant_dir>/candidates/sim_<n>.py` with `memory.write_text_atomic`,
   and a manifest `<variant_dir>/candidates/index.json` with, per candidate:
   n, file name, brief id, static_ok, failures, repaired (bool), characters,
   seconds, the harness version, and a hash of the code. The manifest is
   what `verify.py` will read; it is also the source of numbers for the
   presentation.
4. `generate_simulators` returns the paths of the candidates that pass the
   static check, best first (unrepaired before repaired, then by n). It
   returns `[]` instead of raising.

### 5.5 Repair

`repair_simulator(spec, path, failure_report, deadline)`: one model call
with the contract, the rules, the current code and the failure list, asking
for the complete corrected module; same extraction and static check; on
success overwrite the file, update the manifest, return the path; otherwise
return `None`. `failure_report` may be a `VerifyReport`, a list of strings
or a string (the verification stage will call it with real test failures).
In this session it is used for one round on candidates that failed the
static check, when time is left.

---------------------------------------------------------------------------

## 6. Wiring and time budget (`pipeline.py`, `specialist.py`)

- Phase A (exists): classify every variant in parallel and save it. After
  phase A everything the bot needs to play is in memory. Then align the
  family across variants (4.3) and save again where it changed.
- Phase B (new, in `pipeline.run_prep`, the marked extension point): for
  the variants that want a simulator, run `generate_simulators` in
  parallel, then the one repair round.
- Codegen runs ONLY in prep. Never from `specialist.for_start` (cold start
  has a study clock to respect and nothing could use the result). Keep
  `build_variant` free of codegen so the cold path stays as fast as now.
- A variant found in the cache still goes through phase B when it has no
  manifest with at least one static-ok candidate of the current harness
  version. A variant that has one is skipped.
- Budget: prep answers `ready` when `run_prep` returns, and early answers
  are fine, so return as soon as the work is done. Leave room for the
  stages that come next: codegen plus its repair round must be finished at
  about 45 percent of the prep clock. Express every budget as a fraction of
  the deadline you are given, like `bot/clock.py` does (test 6 runs prep
  with a 6 s clock: with such a clock phase B must simply not start).
- Log one line per candidate and one summary line per variant
  (`bot/log.py`): family, confidence, candidates generated / static-ok /
  repaired, seconds.
- You change what prep stores in `/memory` (rule 29). Do not edit
  `bot/config.py`: delete `results/memory/` before your local practice
  runs, and ask for the `HARNESS_VERSION` bump in the hand-over.

---------------------------------------------------------------------------

## 7. Tests and checks

Unit tests in the new file `tests/test_prep.py` (stdlib `unittest`; replace
the model with `unittest.mock.patch` on `bot.llm.complete`; use
`memory.reset_root_for_tests` with a temp folder like `tests/test_units.py`):

1. Evidence: short rules snippets that you write yourself, one or two per
   family for all 7 families, give the expected family; a snippet with no
   signal is not confident.
2. Checks: invalid family, family `unknown`, a quote that is not in the
   rules, an example action outside the sample's legal list, an example
   that does not match the format, an empty action format, guardrails
   without a restriction; the repair call is made at most once; with the
   model returning `None` the profile comes from the evidence; nothing
   raises on `None`, empty or garbage input.
3. The playbook is cut to the cap. Variants are aligned on one family.
4. Code extraction: fenced block, prose around it, two blocks, cut-off
   answer, a JSON object instead of code.
5. Static check: one good module passes; missing function, wrong parameter
   count, `import os`, `open(`, code at top level, syntax error each fail
   with a readable reason.
6. `generate_simulators` with a mocked model that returns one good module,
   one with a syntax error and one with a forbidden import: the returned
   paths, the files on disk and the manifest are right; the repair round is
   attempted; a deadline in the past makes no model call and returns `[]`;
   a model that returns `None` gives `[]`.
7. The cold path does not call codegen.

Then, in this order:

1. `python -m unittest`: everything passes, the existing tests unchanged
   (watch tests 5 and 6 of `tests/test_protocol.py`), and the run is not
   noticeably slower than before.
2. `python scripts/practice.py --game tictactoe --prep --fake`, the same for
   `guess_number` and `rps_twist`: each prints `RESULT: CLEAN`.
3. Real key, inside the budget of section 0: one run with `--prep` per test
   game. Collect a table: game, family found (the truth is the `family`
   attribute in `testgames/*.py`), example action valid, candidates
   generated / static-ok / repaired, seconds of phase A and B, cost.
   `RESULT: CLEAN` for each.
4. For your own judgement of prompt quality you may run a candidate by hand
   in a throwaway child process (`python -I`, with a timeout) and compare
   its `legal_actions` with a sample turn. That is developer work in the
   scratch area: do not commit it and do not add it to the bot. If the
   candidates are mostly wrong on the sample turn, improve the prompt
   before you hand over, and report the numbers you saw.

---------------------------------------------------------------------------

## 8. Hand-over

Commits: `prep: ...` prefix, small, feature 1 before feature 2. Update the
luckydevs-3 rows of `READMEFIRST.md` in the same commits (status of
`classify.py`, `codegen.py`, `pipeline.py`; the new `bot/prep/prompts/`
files; `tests/test_prep.py`).

Finish with one "ready to merge" message (rule 8) that contains:

1. Branch name, what changed, which files, how it was tested (the commands
   and their results, the table of section 7), anything risky.
2. Proposals for shared files, each as a ready-to-paste snippet:
   - `bot/interfaces.py`: the contract clarifications of 5.1; the
     `analysis.json` facts that should become `GameProfile` fields;
   - `bot/config.py`: your constants; the `HARNESS_VERSION` bump;
   - `READMEFIRST.md` "Where things are stored": `analysis.json`,
     `candidates/sim_<n>.py`, `candidates/index.json`;
   - `GAMEPLAN.md`: which boxes of part C can be ticked;
   - `bot/prompts/classify_system.txt`, `classify_user.txt`: no longer
     loaded.
3. What the next feature (sandbox + verification) must know: the manifest
   format, the conventions of 5.1, how `repair_simulator` wants its failure
   report, which checks you did NOT do because they need execution.
4. For luckydevs-2: the exact format of `speech_restrictions`.
5. Every default you chose without asking, and the dev key spend.
