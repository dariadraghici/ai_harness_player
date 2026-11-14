# PROMPT-FEATURE3-4: sandbox + simulator verification (prep)

You are working in the Git repository of team `luckydevs` (AI Harness Arena
hackathon). I am Sebi, the team lead. This file is your full briefing for the
next two features of the prep pipeline. Read all of it before you touch
anything.

The two features (GAMEPLAN.md, part C, luckydevs-3):

3. Sandbox: generated code runs in a child process with time and memory
   limits and can never take down the bot.
4. Verification: the simulator parses every `sample_turn`; its legal actions
   equal the sample's `legal_actions`; random self-play runs without errors;
   failures are sent back to the model for repair; the best candidate wins.

---------------------------------------------------------------------------

## 0. How to work in this session

1. Read first, in this order: `RULES.md`, `READMEFIRST.md`,
   `docs/ARENA-CONTRACT.md` (the kit notes K1 to K14 at the top matter here:
   K1, K2, K7, K8), `participant-kit/PROTOCOL.md`, then `GAMEPLAN.md` parts
   A.2 to A.4, C and D. The rules in `RULES.md` are mandatory.
2. Then read the code you build on: all of `bot/prep/` (especially
   `codegen.py`, `pipeline.py`, `classify.call_model`, `templates.py`), all
   of `bot/prep/prompts/codegen_*.txt`, `bot/specialist.py`, the simulator
   contract comment in `bot/interfaces.py`, the L3 block of `bot/ladder.py`,
   `bot/engines/base.py` and `bot/engines/search.py` (the next customer of
   your sandbox), `bot/memory.py`, `bot/clock.py`, `bot/protocol.py`
   (`install`), `bot/main.py` (`on_prep`, `shutdown`), `tests/test_prep.py`,
   `tests/test_kit.py`, `testgames/*.py`. As a reference for talking to a
   child process with timeouts on every operating system, read the `Bot`
   class in `participant-kit/arena-lite/arena/protocol.py` (reader thread
   plus queue, kill of the process group). Read it, do not import it: the kit
   is not in our image.
3. Branch (I change this line if another name must own the branch):

   ```
   git fetch origin
   git switch -c luckydevs-3-prep-sandbox-verify origin/main
   ```

   Never commit to `main`, never merge, rebase, cherry-pick or force push.
4. Time. Today is Saturday 14 Nov. This work does NOT have to be in the `t1`
   image (freeze 18:00): nothing plays on a simulator until an engine exists.
   Do not rush it into the 17:15 merge. Target: hand-over at about 18:30, so
   the engines can be built on it before the T2 freeze (23:59, the important
   one). Commit the sandbox first, on its own; then verification.
5. Ask me before: the first `git push` of the branch, any change to a file
   outside the list in section 3, and any step that would take the dev key
   spend of this session above 0.75 USD. For everything else pick the
   sensible default, write it down for the hand-over, and keep going. Batch
   questions in one message.
6. Dev key: `python scripts/keyinfo.py` before your first real call and at
   the end; report the difference. At most 6 model calls in flight
   (`classify.call_model` already enforces it). Everything that does not
   need the real model runs with fixture simulators (section 8).
7. Every file you write is English, plain ASCII (rule 28).

---------------------------------------------------------------------------

## 1. Where this sits

Prep today: phase A classifies every variant (profile, playbook,
`analysis.json`); phase B asks the model for three candidate simulator
modules per variant and keeps the ones that pass a static `ast` check
(`<variant_dir>/candidates/sim_<n>.py` and `candidates/index.json`). Nothing
executes them. `spec.verified` is always `False`, so the ladder never
reaches L3.

Your two features turn candidate FILES into one simulator the engines can
trust:

- the sandbox is the ONLY place where generated code ever runs: a child
  process the bot can always kill, that can never write to the bot's stdout,
  never exhaust the container and never block an answer;
- verification runs each candidate through checks inside the sandbox, sends
  concrete failures back to the model (`codegen.repair_simulator` exists),
  picks the best candidate, stores it as `simulator.py` with a `verify.json`
  report, and sets `spec.simulator_path` and `spec.verified`.

The judges score harness design on "understands rules, tracks state,
verifies moves, uses tools and computation" (rulebook 12.1). This stage is
the "verifies" part; `verify.json` is also where the numbers for our
presentation come from (verification pass rate per game).

After your work the next features are the engines (alpha-beta / MCTS for
Think ahead, hypothesis filtering for Guessing). They will run search loops
INSIDE the sandbox child, next to the simulator, thousands of calls per
move. Design the sandbox for that customer, not only for verification.

---------------------------------------------------------------------------

## 2. What exists now (checked on `main` today)

- `sandbox.call_simulator(path, function, args, timeout)` and
  `sandbox.SandboxError`: stubs. `verify.verify_simulator(spec, path,
  deadline)` and `verify.VerifyReport(passed, checks, failures, seconds)`:
  stubs. Keep these names; other docstrings refer to them.
- `codegen.generate_simulators(spec, deadline, n, analysis)` returns the
  static-ok candidate paths. `codegen.ok_paths(spec)`, `read_manifest(spec)`,
  `has_ok_candidates(spec)`, `candidates_dir(spec)` exist.
  `codegen.repair_simulator(spec, path, failure_report, deadline, analysis)`
  makes one model call, OVERWRITES the candidate file on success and marks
  it `repaired` in the manifest; `failure_report` may be a `VerifyReport`, a
  list of strings or a string.
- The contract every candidate follows is
  `bot/prep/prompts/codegen_contract.txt` (C1 to C10): six functions, the
  state is a JSON-serializable dict with `state["me"]`, variant parameters
  versus deal setup (C1), simultaneous steps (C5: `current_player` is -1 and
  `apply_action` takes a list of two actions), `returns` in [-1, 1] (C7),
  purity (C8), `ValueError` on bad input (C10), the constant
  `LEGAL_ACTIONS_COMPLETE` (C6).
- `pipeline.run_prep`: phase A, `align`, phase B until
  `PHASE_B_END_FRACTION` (0.53) of its budget, then a marked extension
  point for you. `main.py` answers `ready` when `run_prep` returns.
- `specialist.lookup_by_label`: at `start` a prepared spec is found by
  variant label when the rules text differs from prep; such a spec keeps
  `rules_hash` = the hash prep stored the variant under (the storage key).

Baseline I measured today with the real model (prep of `tic_tac_toe`, `pig`,
`guess_number`, `rps_twist`, `nim_deal`, 2 variants each, 3 candidates per
variant; a quick throwaway check, not a real verifier):

| | count |
|---|---|
| candidates generated | 30 |
| pass the static check, no repair needed | 30 |
| parse the sample turn, legal actions right, 20 random games end | 25 |
| `parse_observation` raises on the sample turn (both `rps_twist`, brief `parser-first`) | 2 |
| random self-play does not end in 300 steps (`pig` x2, `guess_number` x1) | 3 |

Prep answers `ready` after 30 to 70 s of the 600 s, so there is a lot of
unused time for verification and repair rounds.

Facts that are easy to miss:

1. THE CONTRACT HAS A HOLE: outcomes we cannot know. In Pig the candidates
   turn "roll" into a state with `pending = True` and no dice value; in
   Guess the Number a guess cannot be answered because the opponent's secret
   is `None`. Such a simulator cannot play a game forward: the self-play
   that "passes" only ends at a move cap, and an engine could not search on
   it. C3 ("store what we cannot see as None") and C8 ("no randomness") say
   nothing about how to continue. Section 6 is about closing this hole; do
   not skip it, 2 of our 5 practice games have it.
2. One sample turn per variant is all prep gets: seat 0, mid-game, maybe
   missing, `legal_actions` null when there are more than 50 actions
   (Guess the Number). A parser that only fits this one text, or a module
   that hard-codes the sample deal (kit note K2, RULES 30), passes a naive
   sample check. `nim_deal` is our test game for that: its rules text
   changes with every deal.
3. `bot/protocol.py` points file descriptor 1 at stderr at start-up and
   keeps a private handle for protocol lines. A child must still never be
   given the bot's stdout or stdin: give it pipes of its own.
4. The container allows 256 processes (threads count) and 2 CPUs, shared
   with the bot's own threads and, in prep, with model calls. Several of our
   specialists are alive at once and an idle one must use little memory and
   no CPU.
5. `main.py` ends the process with `os._exit(0)` at shutdown and the prep
   worker thread can outlive the `ready` answer (watchdog). A child that
   keeps running after its parent stopped caring burns CPU during a match.
6. With `BOT_FAKE_LLM=1` codegen produces no candidate at all (the fake
   answers JSON, not code). Your code must do nothing, fast and quietly, in
   that case, and your tests need simulators that you write yourself.
7. Chat never enters prep. Keep it that way: no opponent text may ever reach
   a codegen or repair prompt, so generated code is buggy at worst, not
   hostile. The sandbox protects us from bugs (endless loops, memory blowups,
   crashes, prints, huge outputs). It is not a security product; do not
   spend time on escape scenarios.
8. `resource` (rlimits) exists only on Unix. The image is Linux; some of us
   develop on Windows. Import it defensively; limits are best effort where
   the platform has none, and tests for them are skipped there.

---------------------------------------------------------------------------

## 3. Scope

Files you may change or create:

| Path | What |
|---|---|
| `bot/prep/sandbox.py` | feature 3, the parent side |
| `bot/prep/sandbox_child.py` (new; your name for it) | the program that runs inside the child |
| `bot/prep/verify.py` | feature 4 |
| `bot/prep/pipeline.py` | wiring and time budget |
| `bot/prep/codegen.py`, `bot/prep/prompts/codegen_*.txt` | only what sections 5.4 and 6 need (keep a backup before repair, contract point C11, contract version in the manifest) |
| `bot/specialist.py` | only the minimum the wiring needs |
| `tests/test_sandbox.py`, `tests/test_verify.py` (new) | your tests. `tests/` belongs to luckydevs-1; I allow these two new files. Do not edit existing test files |
| `READMEFIRST.md` | the rows of the luckydevs-3 table and the two lines `simulator.py` and `verify.json` of "Where things are stored" |

Not in scope, do not touch: the engines (`bot/engines/*`), `bot/ladder.py`,
`bot/interfaces.py`, `bot/config.py`, `bot/main.py`, `bot/llm.py`,
`bot/memory.py`, `bot/chat/`, `bot/prompts/`, `scripts/`, `testgames/`,
`participant-kit/`, `GAMEPLAN.md`, `RULES.md`. What you need there you
PROPOSE in the hand-over (rule 11). Constants live as module constants in
your files, with the comment "proposed for bot/config.py".

Hard limits:

- Generated code runs ONLY inside the sandbox child. Never import, `exec` or
  `eval` a candidate in the bot process, not in tests either.
- Nothing you add may raise into `pipeline.run_prep`, slow down an answer or
  outlive its deadline. A failure means: no verified simulator, the bot
  plays exactly as today (L2).
- Setting `spec.verified = True` must not change how the bot plays today:
  the engines are stubs that raise `NotImplementedError`, and the ladder
  falls to L2. Check that this is still true at the end (section 8).
- Only `llm.complete` (through `classify.call_model`) talks to a model.
- No game-specific logic (rule 22), standard library only, no `print`, logs
  through `bot/log.py`.

---------------------------------------------------------------------------

## 4. Feature 3: the sandbox

Goal: a small API through which the bot runs code next to a generated
simulator, with these guarantees, whatever the candidate does:

1. the bot's stdout, stdin and protocol are never touched;
2. every call returns or fails within the time the caller gave;
3. the child's memory and CPU are capped;
4. no child survives the work it was started for;
5. no exception other than `SandboxError` leaves the API, and that one only
   where the docstring says so.

### 4.1 Design I want (improve it if you find better, keep the guarantees)

- The child is `python -I <absolute path of sandbox_child.py>` (isolated
  mode, so no user site, no `PYTHONPATH`, no current directory on the path).
  The child program is OURS and trusted; it uses the standard library only
  and imports nothing from `bot`. It loads the simulator file with
  `importlib` and then serves requests: one JSON object per line on its
  stdin, one JSON answer per line on its stdout.
- First thing in the child, before the candidate is loaded: set the limits
  on itself (`resource.setrlimit`: address space, CPU seconds as a backstop,
  file size, open files), keep a private handle for answers and point
  `sys.stdout` and file descriptor 1 somewhere harmless, so that a stray
  `print` in generated code cannot corrupt an answer. Doing this in the
  child avoids `preexec_fn`, which is unsafe in a threaded parent.
- The parent starts it with pipes for stdin and stdout, stderr discarded or
  drained with a size cap, its own process group or session, and a minimal
  environment (never pass `LLM_API_KEY` or any other variable the simulator
  has no use for).
- The parent reads answers through a reader thread and a queue and waits
  with a timeout. On timeout, a closed pipe, a line that is not JSON or
  longer than a cap: kill the process group, reap it, report a failure.
- TWO ways to use a child, because the two customers differ:
  - single calls: `call_simulator(path, function, args, timeout)` runs one
    of the six functions and returns its JSON result. Simple, slow (one
    round trip per call). Keep the stub's signature.
  - a JOB that runs inside the child next to the simulator: the parent
    sends one request ("run this named job with this payload, within this
    many seconds") and gets one report back. The job code is ours and lives
    in `bot/` (for example the verification suite of section 5; later the
    search loop of an engine). Thousands of simulator calls then cost no
    round trips. Decide how job code reaches the isolated child (a path
    argument, a small registry inside `sandbox_child.py`) and document it:
    the engines will add jobs.
- A worker object that can be reused across requests (the engines keep one
  per game while it is being played) and is cheap to restart after a kill;
  plus a one-shot helper for verification. An idle specialist holds NO
  child: start lazily, stop when the work is done.
- A registry of live children with a "kill everything" function, used when
  a deadline passes. The child also exits by itself when its stdin closes,
  and the CPU limit is the backstop for a child whose parent died.
- A process-wide cap on children alive at once (2 is right for 2 CPUs).

### 4.2 What "done" means for the sandbox

Each of these is a test with a fixture simulator (section 8), and each must
leave the bot process alive, its stdout untouched and no child behind:
endless loop at import; endless loop inside a function; allocation of
several GB; deep recursion; `raise SystemExit`; an uncaught exception; a
`print` flood (written into a fixture by hand, the static check would stop
it in real candidates); a result that is not JSON-serializable; a result of
50 MB; a child killed from outside in the middle of a call; a missing file;
100 calls in a row on one worker; two workers at the same time.

Report the measured cost: start-up time of a child, round trip of a single
call, calls per second inside a job.

---------------------------------------------------------------------------

## 5. Feature 4: verification

Goal: for every variant with candidates, decide with evidence which
candidate, if any, the engines may trust; make the model fix what code
found wrong; record everything.

### 5.1 The checks (all run as one job inside the sandbox)

Each check has a name; each failure is ONE sentence written for the model
that will repair the code: what was called with which input, what came
back, what was expected. Cap the number and the length of failures.

1. Load: the module imports within a short time and defines the six
   functions and `LEGAL_ACTIONS_COMPLETE`.
2. Sample parse: `parse_observation(sample.observation, {}, 0)` and the same
   with `{"label": <label>}` return a JSON-serializable dict with
   `state["me"] == 0`; `current_player` is 0, or -1 when the profile says
   the game is simultaneous.
3. Sample legal actions: when the sample has a list, `legal_actions(state)`
   must hold exactly the same strings. Same order is a bonus for the score,
   not a failure (the ladder matches by string). When the sample has null:
   the list is not empty, every entry is a string, every entry matches the
   trusted action regex of `analysis.json` when there is one, and the
   profile's example action is in the list when
   `LEGAL_ACTIONS_COMPLETE` is true.
4. Contract behavior on the sample state and along self-play:
   `apply_action` does not mutate its input (compare a JSON dump before and
   after); the same call twice gives equal results; a state that went
   through `json.loads(json.dumps(...))` behaves the same (states cross the
   process boundary); an illegal action raises `ValueError`;
   `legal_actions` is `[]` exactly when `is_terminal` is true.
5. Random self-play from the sample state, seeded, several games: every
   game ends within a step cap; `returns` is two floats in [-1, 1]; in a
   simultaneous step `apply_action` gets the two-action list of C5. Measure
   playouts per second and steps per game and put them in the report: the
   engines choose between full search and sampling from these numbers.
6. Every stored turn, not only the sample: verification takes a LIST of
   turn samples. Today that list is the prep `sample_turn`. Also read
   `<memory>/<game_id>/turns/<label, made safe with memory.safe_name>.jsonl`
   when it exists: one JSON object per line, `{"seat": 0 or 1, "variant":
   {...}, "turn": {the turn message without chat}}`, real turns recorded in
   earlier matches of this game. Nothing writes that file yet (I will add
   the recorder outside your scope); your part is to use it when present,
   with the right seat and variant, capped at a number of lines. These are
   the only observations of seat 1 and of other deals prep will ever see.
7. Agreement between candidates (differential test). The candidates of one
   variant were written independently from the same rules. Drive them with
   the same action sequences from the sample state and compare what is
   visible through the interface (legal action sets, `current_player`,
   `is_terminal`, `returns`), never the state itself. Where they disagree
   and a majority exists, the minority is wrong with high probability: that
   is a failure with a very useful message ("after the actions [...] you
   list [...], two other implementations of the same rules list [...]").
   With section 6 in place, give all candidates the same seed.

A candidate is VERIFIED when checks 1 to 5 pass for every turn sample that
exists, and it is not in a minority in check 7. Without any turn sample
nothing can be verified: keep the best candidate recorded in the report,
`spec.verified` stays `False`.

### 5.2 Report, score, winner

- `verify_simulator(spec, path, deadline)` returns a `VerifyReport`; extend
  the dataclass as you need (keep `passed`, `checks`, `failures`,
  `seconds`).
- Score for "the best candidate wins": verified first; then more checks
  passed; then exact order in check 3; then not repaired; then faster
  playouts.
- The winner is copied to `<variant_dir>/simulator.py` (atomic write), then
  `verify.json` is written (per candidate: the checks, the failures, the
  repair rounds, playouts per second; the winner; the harness version; the
  hash of the code; the time spent), then `spec.simulator_path` and
  `spec.verified` are set and `memory.save_variant(spec)` stores them
  (`meta.json` last, as today). A variant whose `verify.json` of the
  current harness version and contract names a winner that is still on disk
  is not verified again.
- `simulator.py` is written ONLY for a verified winner.

### 5.3 Repair loop

- Candidate fails: `codegen.repair_simulator(spec, path, report, deadline,
  analysis)`, then verify again. Several rounds while time is left, a cap of
  rounds per candidate, candidates repaired in parallel.
- The repair call overwrites the file. Keep the previous version, and put
  it back when the repaired module scores lower than the one it replaced.
- When every candidate of a variant is still failing and enough time is
  left, one more round of fresh candidates is better than a fifth repair of
  a hopeless one. Your judgement; keep it simple.
- Stop early: as soon as a variant has a verified winner that agrees with
  the others, its work is done. Prep time is free for the score, but
  finishing early frees CPU for the other variants.

### 5.4 Known limits (write them into `verify.json` and the hand-over)

- A module that hard-codes the sample deal can pass with one sample turn.
  Checks 6 and 7 reduce the risk; they do not remove it. The real answer is
  the live check during a match (compare the simulator's legal actions with
  the arena's on every turn, GAMEPLAN part D), which is NOT in your scope.
  Make it cheap to build: a function that checks one turn message against a
  simulator in a worker.
- Legal action order and exact spelling are checked only where a turn
  sample has a list.

---------------------------------------------------------------------------

## 6. Close the hole in the contract: outcomes we cannot know

See fact 1 of section 2. Verification by self-play and every later engine
need `apply_action` to return a state the game can continue from, also when
the outcome is random (dice, a card drawn) or depends on something hidden
(the opponent's secret number, their cards).

My default, as a new contract point C11 in `codegen_contract.txt` (and the
matching edits of C3 and C8). Choose another convention only if yours is
simpler and serves the engines as well; choose exactly one:

- `parse_observation` sets `state["seed"] = 0`.
- When `apply_action` needs a value that is random or hidden, it draws it
  with `random.Random(state["seed"])` from the values still consistent with
  everything the state knows, records the drawn value in the new state, and
  stores a new seed in the new state. Values we could not see at parse time
  stay `None` until they are needed.
- So the same input still gives the same output (C8 holds), a game can
  always be played to its end, and an engine samples other outcomes or
  other hidden worlds by changing `state["seed"]`.

Consequences in your scope:

- Candidates made before the contract change are stale. Put a version or a
  hash of the contract text into the manifest and treat a manifest of
  another contract as "no candidates", so phase B regenerates.
- Check 5 becomes meaningful for chance and hidden-information games; add a
  check that two different seeds can give different outcomes in a game the
  profile marks as having chance or hidden information, and that one seed
  always gives the same game.
- Tell the next features in the hand-over exactly how to use the seed.

---------------------------------------------------------------------------

## 7. Wiring and time budget (`pipeline.py`)

- Per variant, as one chain: candidates (phase B, exists) -> verify ->
  repair -> verify -> winner. Do not wait for the slowest variant before
  the first one is verified; variants run in parallel under the global caps
  (model calls: `classify.call_model`; children: your cap).
- A variant from the cache with a valid `verify.json` is done. A variant
  with candidates but no valid `verify.json` enters at "verify".
- Budget: everything of yours ends at about 85 percent of the budget given
  to `run_prep`; express it as a fraction, like the existing constants. A
  short clock (the tests run prep with 6 s) must simply skip your stage.
- Deadline discipline, same as feature 2: check the time before every model
  call and before every child; at the deadline kill all children; a thread
  that wakes up late starts nothing and writes nothing.
- Only prep verifies. The cold start path (`specialist.for_start`) never
  starts a child.
- Log one line per candidate per round and one summary line per variant:
  verified or not, the winner, rounds, seconds, playouts per second.
- You change what prep stores in `/memory` (rule 29): delete `memory/` in
  the repository root before local runs and ask for the `HARNESS_VERSION`
  bump in the hand-over.

---------------------------------------------------------------------------

## 8. Tests and checks

Fixture simulators: write them yourself as strings or small files inside
the tests: one correct module for a tiny game you invent (a few lines of
rules, a sample turn to match), and broken copies of it, one defect each:
the sandbox cases of 4.2, and for verification: wrong legal list, right
strings in another order, mutates its input, different result on the second
call, never terminal, `returns` out of range, no `ValueError` on an illegal
action, parser that fails on the second stored turn, a simultaneous game
that ignores C5, a module that disagrees with two correct ones after three
moves, a chance game without the seed rule. The model is mocked with
`unittest.mock.patch` on `bot.llm.complete` where a repair is tested.

`tests/test_sandbox.py`: the guarantees of 4.1 on the cases of 4.2; the
bot's stdout stays clean (run the fixtures from a child Python process that
plays the role of the bot and check its stdout byte for byte); no child
left behind (count them); the caps; behavior without `resource`.

`tests/test_verify.py`: every check passes on the good fixture and fails
with a readable sentence on its broken copy; the score order; the winner
file, `verify.json` and the spec fields; the repair loop (repaired module
is verified again; a worse repair is rolled back; the round cap); a second
run does not verify again; a deadline in the past starts no child; no
sample turn means not verified; the stored turns file is used when present.

Then, in this order:

1. `python -m unittest`: everything passes, existing tests unchanged, the
   run is not much slower than now (63 tests, about 15 s).
2. `python scripts/practice.py --game <id> --prep --fake` for `tic_tac_toe`,
   `guess_number`, `nim_deal`: `RESULT: CLEAN`, and your stage did nothing.
3. Real key, inside the budget of section 0: `--prep` for all five games
   (`tic_tac_toe`, `pig`, `guess_number`, `rps_twist`, `nim_deal`), each
   `RESULT: CLEAN`. Table per variant: candidates, verified before repair,
   verified after repair, rounds, winner, playouts per second, seconds of
   your stage, seconds until `ready`, cost. Compare with the baseline of
   section 2 and say what the failures were.
4. Proof that play did not change: in one of those runs the log shows the
   verified simulator, L3 reports not available (engine stub), the decision
   comes from L2 as before.
5. Proof for `nim_deal`: take the verified winner and check it by hand, in
   a sandbox child, against observations of OTHER deals (the replay files
   in `results/practice/matches/` hold the observation of every turn of
   both seats). Report how many parse and how many give the legal list the
   arena gave. This is developer work in the scratch area; do not commit it.
6. `python scripts/build.py dev` and `python scripts/check_image.py dev`:
   5 of 5. Then one `--prep --image dev` match with the real key, to see the
   limits work inside the container (2 CPUs, 2 GB, 256 processes): report
   the peak number of processes and anything that behaved differently from
   your laptop.

---------------------------------------------------------------------------

## 9. Hand-over

Commits: `prep: ...` prefix, the sandbox before verification. Update the
luckydevs-3 rows of `READMEFIRST.md` in the same commits.

Finish with one "ready to merge" message (rule 8) that contains:

1. Branch name, what changed, which files, how it was tested (commands,
   results, the table of section 8), anything risky.
2. Proposals for shared files, each as a ready-to-paste snippet:
   `bot/interfaces.py` (contract C11 and anything else you pinned down),
   `bot/config.py` (your constants; the `HARNESS_VERSION` bump),
   `GAMEPLAN.md` (boxes of part C that can be ticked).
3. For the engines, which come next: the sandbox API with a ten line
   example of a job that runs a search loop next to the simulator; how to
   use the seed; the fields of `verify.json` an engine should read; the
   one-turn check function for the live consistency check.
4. For me: the exact format of the stored turns file you read (section 5.1,
   check 6), so the recorder I add writes what you expect.
5. What verification cannot see (section 5.4), every default you chose
   without asking, and the dev key spend.
