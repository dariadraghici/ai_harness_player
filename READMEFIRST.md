# READMEFIRST: the map of this repository

## What this project is

This is team `luckydevs`' entry for the AI Harness Arena (dvloper.io, EESTEC
Olympics 14). We submit one Docker image: a generic "mesh" bot that knows no
game in advance. Before a tournament the arena starts one container per game,
sends it the rules of a secret two-player text game and gives it 10 minutes
of prep to turn itself into a specialist. Then specialists play each other
(20 s per move) while chatting in a shared chat that is part of the game.
Every team uses the same model (Claude Haiku through the organizers'
gateway); the harness around the model is the only difference. Our harness
answers every move through a fallback ladder (L3 engine, L2 model validated
by code, L1 safe move), so it never loses a game for a stupid reason.

Read next: `RULES.md` (how we work, mandatory), `docs/ARENA-CONTRACT.md` (the
protocol and the rules of the event; its "Kit notes" at the top are new),
`GAMEPLAN.md` (who builds what, when). The organizers' participant kit
(`participant-kit/`: `PROTOCOL.md`, `README.md`, the rulebook PDF, the practice
arena `arena-lite/`, the sample bot) is the authority on the event; it is not
in our Git, every member clones it (step 0 below).

## Quick start

Commands use `python`; on Linux/WSL/macOS type `python3` if `python` is
missing. Run everything from the repository root. Python 3.10 or newer and
Docker (with buildx) are needed. No pip install: the bot uses only the
standard library.

```
# 0. The participant kit (once; ignored by our Git). Update it with `git pull` inside it
#    when the organizers release new games (after T1 and after T2).
git clone https://git.hackaton-2026.dvloper.io/arena/participant-kit.git participant-kit

# 1. Secrets (once). Ask Sebi for the values. Never commit these files.
cp secrets.local.env.example secrets.local.env     # fill in
cp dev.env.example dev.env                         # LLM_BASE_URL, LLM_API_KEY, LLM_MODEL=haiku

# 2. Unit tests (fake LLM, free, about 15 s; the arena-lite tests are skipped without the kit)
python -m unittest

# 3. One practice match on the organizers' arena-lite, local bot, against the random bot
python scripts/practice.py --list                          # tic_tac_toe, pig, guess_number + our rps_twist, nim_deal
python scripts/practice.py --game tic_tac_toe --fake       # fake LLM, free
python scripts/practice.py --game tic_tac_toe              # real dev key (about 0.002 USD)
python scripts/practice.py --game guess_number --prep      # the tournament flow: prep, then warm containers
python scripts/practice.py --game tic_tac_toe --debug      # also logs every prompt (see "our logs" in the output)
python scripts/practice.py --game rps_twist --opponent self --games 4

# 4. Build the image (linux/amd64)
python scripts/build.py dev

# 5. Practice match with prep, played by the image
python scripts/practice.py --game tic_tac_toe --prep --image dev

# 6. The whole pre-push checklist against an image (fake LLM; add --real for the real key)
python scripts/check_image.py dev

# 7. Dev key budget and spend (never prints the key)
python scripts/keyinfo.py

# Any other arena-lite command (prep only, show the prep message, ...)
python scripts/arena_lite.py prep --bot "python -u -m bot.main" --game pig --show
```

A practice run prints `RESULT: CLEAN` when our bot had no warning, timeout,
crash, bad answer, stray stdout line or failed prep, and chatted. Pushing
(`python scripts/push.py t1`) is done by Sebi only.

Fake and real runs share the local memory folder but never each other's
specialists: with `BOT_FAKE_LLM=1` the harness version gets the suffix
`-fake` (`bot/config.py`).

The Practice page `https://arena.hackaton-2026.dvloper.io/#/practice` runs an
image we already PUSHED against the organizers' bots (Prompt Only, dvloper
House, dvloper Master), against itself or against a human. Sign in with
`PRACTICE_TOKEN` from `secrets.local.env`. Limits: one run at a time, 10 runs
per team per hour. Sebi coordinates who uses the runs.

## How a decision is made

```
stdin line -> main.py (reader thread -> event queue -> main loop)
                |
   prep  -----> llm.set_key -> prep/pipeline.run_prep
                  for each variant (parallel): memory.rules_hash
                    -> specialist.lookup (process, then /memory)
                    -> else specialist.build_variant -> prep/classify (1 model call,
                       thinking on) -> memory.save_variant
                  per variant that wants a simulator: prep/codegen (candidates) -> prep/verify
                    (checks in prep/sandbox children, repair, winner -> simulator.py, spec.verified)
                -> {"type":"ready"}   (at clock.prep_hard at the latest)

   start -----> llm.set_key -> specialist.for_start
                  (process -> /memory by rules hash -> same game + variant label when the
                   rules text of this game differs from prep -> cold prep of this variant)
                -> chat/defense.build_context -> chat/decide.opening_chat
                -> engines/guardrails.filter_outgoing
                -> {"type":"ready","chat":...}   (at clock.start_hard at the latest)

   turn  -----> ladder.decide_turn
                  L1 ladder.safe_action (reported at once: the watchdog always has a move)
                  L3 engines/base.get_engine(family).candidates   (only with a verified simulator)
                  L2 chat/defense (untrusted opponent chat block) -> chat/decide.decide
                     (ONE model call: move + chat) -> ladder.match_legal
                  ladder.finalize -> engines/guardrails.filter_outgoing + clean chat
                -> {"type":"action","action":...,"chat":...}
                   (worker result, or the watchdog's best-so-far at clock.turn_hard)
                -> metrics.record

   end   -----> opponents.record_game_end, metrics.record   (match_over: keep waiting)
   shutdown / closed stdin -> save -> exit 0
```

Only `bot/protocol.py` writes to stdout; `protocol.install()` points file
descriptor 1 at stderr before anything else runs.

## File by file

Status: `real` = finished base behavior, `basic` = works but simple,
`stub` = signature and docstring only (the ladder falls through it).

### Shared (owner: Sebi; change only with permission)

| Path | What it is / what it is for | Status |
|---|---|---|
| `bot/__init__.py` | package marker of the harness | real |
| `bot/main.py` | entrypoint (`python -u -m bot.main`): protocol loop, reader thread, workers, watchdog, pause-safe idle compute, shutdown | real |
| `bot/interfaces.py` | contracts between modules: `GameProfile`, `Candidate`, `Decision`, `ChatLine`, `ChatContext`, `VariantSpec`, `Engine`, the generated-simulator contract, `FAMILIES` | real |
| `bot/config.py` | all constants (deadlines, model, prices, limits) and `BOT_*` settings; `HARNESS_VERSION` | real |
| `bot/ladder.py` | the fallback ladder L3 -> L2 -> L1; legal-action matching; `finalize` (filter + chat cleanup); watchdog `emergency` | real |
| `Dockerfile` | the image: `python:3.12-slim`, copies `bot/` and `/logo.png`, starts the bot | real |
| `.gitignore` | keeps secrets, `results/`, local memory, `participant-kit`, caches, editor folders out of Git | real |
| `.dockerignore` | keeps secrets, `.git`, tests, docs, scripts, test games, the kit, results out of the image | real |
| `RULES.md` | rules for every human and agent | real |
| `GAMEPLAN.md` | priorities, owners, milestones, checklists | real |
| `READMEFIRST.md` | this map (everyone adds their own new files to their section) | real |
| `AGENTS.md`, `CLAUDE.md` | 3 lines for coding agents: read RULES, READMEFIRST, ARENA-CONTRACT first | real |
| `README.md` | the one-line README created with the repository (left unchanged) | - |
| `docs/ARENA-CONTRACT.md` | the protocol and event rules (section 2 of the briefing) plus the kit notes K1 to K14 | real |
| `docs/PROMPT-INITIAL.md` | the briefing this base was built from | real |
| `dev.env.example` | template of `dev.env` (committed, empty values) | real |
| `secrets.local.env.example` | template of `secrets.local.env` (committed, empty values) | real |
| `dev.env` | NOT in Git. Local env file for practice: `LLM_*` and `BOT_*` only | local |
| `secrets.local.env` | NOT in Git. All secrets (model dev key, Harbor robot, practice token) | local |
| `participant-kit/` | NOT in Git (the organizers' repository; clone it, never edit it). `PROTOCOL.md`, `README.md`, `CHANGELOG.md`, `docs/` (rulebook PDF), `arena-lite/` (the practice arena), `starter/` (sample bot) | local |

### luckydevs-1: runtime, operations, measurement, test games

| Path | What it is / what it is for | Status |
|---|---|---|
| `bot/protocol.py` | the only stdout writer; stdout redirect; message parsing; `Request` (exactly one answer, thread-safe) | real |
| `bot/action_guard.py` | final turn-action choke point: cached grammar validation, warning blacklist, canonical legal-action emission | real |
| `bot/clock.py` | hard and soft deadlines derived from the message clocks (same proportions as `config.py`) | real |
| `bot/log.py` | logging to stderr (`BOT_LOG_LEVEL`: debug, info, warning, error) | real |
| `bot/llm.py` | gateway client (stdlib urllib): `set_key`, `complete` (thinking control, hard timeout, never raises), usage and cost per key, fake mode | real |
| `bot/memory.py` | `/memory` store: root with fallback, rules hash, atomic writes, variant save/load with harness version check, `find_variant_by_label`, paths | real |
| `bot/metrics.py` | one JSON line per decision (game, level, time, tokens, cost, why) to `<memory>/metrics/<container>.jsonl` | real |
| `bot/resources.py` | one source of truth for CPU and memory: measured limits (affinity, cgroup v2/v1, /proc; paths injectable for tests), the three phases (prep, turn, idle), `plan(kind)` (children, child memory, threads, table sizes, search workers, cache size, warm worker), the `BOT_*` knobs and the `BOT_RESOURCES=off` switch that returns exactly the old constants; `enter`/`scoped` apply a phase to the sandbox and the engines | real |
| `scripts/_common.py` | shared helpers: image name, secrets parsing (no shell `source`), git checks, where arena-lite is (`BOT_ARENA_LITE` or `participant-kit/arena-lite`) | real |
| `scripts/build.py` | `docker build --platform linux/amd64`, labels the image with the Git commit | real |
| `scripts/push.py` | refuses dirty tree or mismatched image; Harbor login via stdin; push; digest; tag reminder | real |
| `scripts/check_image.py` | automated pre-push checklist strictly validating ARENA-CONTRACT 2.9 (all 12 contract checks) plus check 13: idle between games (no sandbox child alive, memory near the baseline, via `docker top`) | real |
| `scripts/practice.py` | practice match on the organizers' arena-lite with `dev.env` (`--fake`, `--debug`, `--prep`, `--image`, `--opponent random/self/starter`); checks the replay and our logs and prints `RESULT: CLEAN` or the problems | real |
| `scripts/batch_runner.py` | batch runner: plays N practice matches across games and opponents (random, self); prints ASCII metrics table (wins, warnings, timeouts, answer times p50/p99, cost, peak RSS, sandbox children, CPU seconds per match); `--jobs N` runs matches in parallel with one `ARENA_ROOT` per worker (default host CPUs - 1 with `--fake`, never parallel with the real key); `--set BOT_X=V` passes a bot setting to every match (for example `BOT_RESOURCES=off`) | real |
| `scripts/arena_lite.py` | runs any arena-lite command from the repository root and adds `testgames/` to its games, without editing the kit | real |
| `scripts/keyinfo.py` | budget and spend of the dev key | real |
| `logo.png` | square team logo (512x512 PNG) for the Docker image | real |
| `tests/__init__.py` | sets `BOT_FAKE_LLM=1` for all tests | real |
| `tests/session_script.py` | `BotProc` (bot as child process) and the scripted arena session, reused by `check_image.py` | real |
| `tests/test_protocol.py` | session, cold start, garbage, warning, null legal actions, watchdog, model failure, closed stdin | real |
| `tests/test_action_guard.py` | grammar validation, canonical legal actions, warning blacklist, cached grammar, static action-emission check | real |
| `tests/test_units.py` | memory, ladder, JSON parsing, chat cleanup, defense block, clock proportions | real |
| `tests/test_kit.py` | kit message shapes (example action from kit rules, specialist found by label, variant settings in the prompt) and five fake-LLM matches on arena-lite that must be CLEAN (skipped without the kit) | real |
| `tests/test_batch_runner.py` | unit tests for batch runner: CLI argument parsing, replay parsing, metric aggregation, ASCII table formatting | real |
| `tests/test_metrics.py` | unit tests for extended metrics logging: per-call tokens, cost calculation, decision recording | real |
| `tests/test_check_image.py` | unit tests for pre-push checklist functions and ARENA-CONTRACT 2.9 validation | real |
| `tests/test_proxy.py` | unit tests for proxy routing: direct gateway calls bypass proxy, general traffic honors HTTPS_PROXY, urllib honors NO_PROXY | real |
| `tests/test_resources.py` | unit tests of `bot/resources.py` with fake cgroup trees: detection order, clamps, floors, idle zeroing, off switch, failure paths | real |
| `tests/test_sandbox_capacity.py` | slot pool tests: capacity 1 and 4 bounded exactly, shrinking under load kills nothing, growing wakes waiters, memory overrun dies clean | real |
| `tests/test_engines_parallel.py` | warm worker per game, parallel parts, merges (function of the parts, not of arrival order), a failing part, idle watchdog, determinism against the old sequential code | real |
| `tests/test_solve.py` | prep pre-solving: roots near the sample position are stored, served from the cache, tied to the simulator hash, both seats, no prose in the file | real |
| `testgames/nim_deal.py` | our own test game for arena-lite: Think ahead, Nim whose dealt heaps are written into the rules text, so the rules of `prep` and `start` differ (kit note K2); the check that prep work is reused for a new deal | real |
| `testgames/rps_twist.py` | our own test game for arena-lite (`arena.core.Game` subclass): Read the opponent, simultaneous moves, payoff twist. New test games go here, in the same form | real |

### luckydevs-2: chat and the language-centred families

| Path | What it is / what it is for | Status |
|---|---|---|
| `bot/chat/__init__.py` | package marker | real |
| `bot/chat/decide.py` | THE single decision call (move + chat), opening chat, JSON parsing, chat cleanup + guardrails filter | real |
| `bot/chat/defense.py` | opponent chat -> `ChatContext`, injection flags, the untrusted block for prompts | basic |
| `bot/chat/offense.py` | chat tactics per family for the prompt, fallback lines | stub |
| `bot/prompts/decide_system.txt` | system prompt of the decision call (stable, cacheable) | basic |
| `bot/prompts/opening_system.txt` | system prompt of the opening chat line | basic |
| `bot/prompts/classify_system.txt` | system prompt of the prep classification (used by luckydevs-3's classify) | basic |
| `bot/prompts/classify_user.txt` | user template of the prep classification | basic |
| `bot/opponents.py` | opponent profiles in `/memory/opponents/` (win/loss counters only) | stub |
| `bot/engines/guardrails.py` | `filter_outgoing` on every action and chat line (pass-through now), `extract_forbidden` | stub |
| `bot/engines/truth.py` | Truth engine: answer only with a supporting quote | stub |
| `bot/engines/deals.py` | Deals engine: values, reservation price, concessions | stub |

### luckydevs-3: prep pipeline and computation engines

| Path | What it is / what it is for | Status |
|---|---|---|
| `bot/prep/__init__.py` | package marker | real |
| `bot/prep/pipeline.py` | prep under the time budget: phase A (classify variants in parallel, cache lookup), family alignment across variants; then simulator generation and verification, followed by deadline-bounded mastery self-play and playbook persistence | real |
| `bot/prep/classify.py` | profile: code evidence (family signals, format sentences, quoted examples, sample facts) + one model call + field checks + at most one repair call; `analysis.json`; `align_families`; `call_model` (deadline-checked, capped prep model calls) | real |
| `bot/prep/templates.py` | loader of the prep prompt templates in `bot/prep/prompts/` | real |
| `bot/prep/prompts/classify_system.txt` | system prompt of the classify call (fields and what each is used for) | real |
| `bot/prep/prompts/classify_user.txt` | user message of the classify call (starts with `TASK: classify`) | real |
| `bot/prep/prompts/classify_repair.txt` | follow-up message of the one repair call (the complaints found by code) | real |
| `bot/prep/prompts/codegen_contract.txt` | THE simulator contract C1-C10 (variant parameters vs deal setup, seat, information state, simultaneous steps, legal actions, returns, purity, JSON state, errors), used by generation and repair | real |
| `bot/prep/prompts/codegen_system.txt` | system prompt of codegen: role, contract, code rules (import allowlist, forbidden names), output format | real |
| `bot/prep/prompts/codegen_user.txt` | user message of one codegen call (starts with `TASK: codegen`): brief, rules, prep facts, sample turn | real |
| `bot/prep/prompts/codegen_sample.txt`, `codegen_nosample.txt` | the sample-turn block of the codegen prompts (with and without a sample turn) | real |
| `bot/prep/prompts/codegen_repair.txt` | user message of a repair call (`TASK: codegen-repair`): failures + current module | real |
| `bot/prep/prompts/codegen_briefs.txt` | the working briefs that make parallel candidates differ (minimal, parser-first, numbered-checks) | real |
| `tests/test_prep.py` | prep unit tests (mocked model): evidence, field checks, repair, playbook cap, alignment, analysis.json; code extraction, static check, generation + manifest + repair, deadlines, phase B, cold path without codegen | real |
| `tests/test_sandbox.py` | sandbox tests with hand-written fixture simulators: endless loops, memory limit, recursion, SystemExit, print flood, results that cannot travel, child killed from outside, the cap of 2 children, `kill_all`, no key and no stdin in the child, the bot's stdout stays byte-exact | real |
| `tests/test_verify.py` | verification tests with fixture simulators (one defect each): every check and its failure sentence, simultaneous and chance games, stored turns, differential test, score, winner files, repair loop, rollback, round cap, cache, stale contract, deadline kills the children, prep end to end with play unchanged (L2) | real |
| `tests/test_engines.py` | engine tests with hand-written fixture simulators (pile, tic-tac-toe, nim, chance game): `BOT_L3`, live check and "off for this game", live counters, looping and slow simulators (slice kept, no child left), solved cache, never loses at tic-tac-toe, nim against the nim-sum, MCTS ranking | real |
| `tests/test_tactical_search.py` | gravity Connect-N regressions for immediate wins, mandatory blocks, anti-feed safety, forcing double threats and short search budgets | real |
| `bot/prep/codegen.py` | simulator generation: parallel candidates with different briefs, code extraction, static `ast` check (never executes code), files + manifest `candidates/index.json` (harness version and hash of the contract text; another one = no candidates), one repair round, `repair_simulator` (also called by `verify` with the failures of its checks) | real |
| `bot/prep/verify.py` | verification, parent side: `verify_simulator` (one candidate through the job in a sandbox child -> `VerifyReport`), `compare` (differential test between candidates), `verify_variant` (verify all, repair rounds with rollback of a worse repair, fresh candidates when all fail, winner -> `simulator.py` + `verify.json`, `spec.verified`), `is_done` (harness version, contract, winner hash against the file on disk, live-check counters; sets `spec.verified` and this container's `spec.simulator_path`), `note_live` / `live_disabled` (counters of games with a failed live check, `live.json`), `turn_samples` (prep sample + stored real turns), `check_turn` (one real turn against a simulator, for the live check) | real |
| `bot/prep/sandbox.py` | the only place where generated code runs: `Worker` (lazy, reusable, restarts after a kill), single calls (`call_simulator`) and jobs (`Worker.job`, `run_job`: one of our files runs next to the simulator), timeouts, optional child niceness, a slot pool (2 children by default, `set_capacity(n)` once per phase from the resource plan; shrinking never kills a child), `kill_all`, `SandboxError` | real |
| `bot/prep/mastery.py` | deadline-bounded post-verification self-play orchestration, aggregate strategy distillation, and `strategy.json` persistence | real |
| `bot/prep/mastery_job.py` | standalone sandbox job that stress-tests verified simulators with generic random and greedy policies | real |
| `bot/prep/solve.py` | prep pass that solves the roots near the sample position with the match's own alpha-beta (as many children as the resource plan allows) and stores them in `solved.json` through the engine's cache format; best effort, off with `BOT_RESOURCES=off` | real |
| `bot/prep/solve_job.py` | the solving as a sandbox job: breadth-first positions where it is our move, alpha-beta per root with a generous budget, roots solved with exact values for every move are returned | real |
| `bot/prep/verify_job.py` | the verification suite as a sandbox job (runs inside the child, standalone, never imported by the bot): load, sample parse, legal actions, contract behavior (no mutation, same result twice, JSON round trip), seeded random self-play with playouts per second, seed check (contract C11), stored real turns, traces for the differential test; mode `turn` for one turn | real |
| `bot/prep/sandbox_child.py` | the program inside the child (`python -I -S`, standalone, never imported by the bot): limits itself (memory, CPU, files), hides stdout and stdin from generated code, serves load / call / job requests as JSON lines | real |
| `bot/specialist.py` | per-variant registry: process -> memory -> same variant label (`lookup_by_label`) -> cold prep; `build_variant` (extension point); `check_simulator` settles the simulator claim of every spec loaded from memory or by label (verify.is_done, no child, once per spec); `for_start` also records our seat on the spec (`spec.seat`) for the engines | real |
| `bot/engines/__init__.py` | package marker | real |
| `bot/engines/bg_compute.py` | one-child, low-priority local compute between requests; instant pause/kill on live work and bounded atomic policy checkpoints; waits while the sandbox has no free slot | real |
| `bot/engines/base.py` | `BaseEngine`, `NotAvailable`, registry family -> engine; `SimEngine`, the helper of every engine that runs on a verified simulator: switch `BOT_L3`, simulator file check, configurable per-family time slice (`BOT_SLICE_FRACTION` overrides), one WARM sandbox child per game and part (killed at `end`, when idle, or after one move clock unused; fresh child per turn with `BOT_WARM_WORKER=off`), parallel parts merged in part order and cancelled by a decisive part (`BOT_SEARCH_WORKERS`), live check result, "off for this game", per-variant live counters (`live.json`), `Candidate` building; `MixedEngine` (read_opponent, bluff): the seeded draw of one action from a mixed strategy and the candidates around it | real |
| `bot/engines/search.py` | Think ahead, parent side: payload, cache of solved roots in `<variant_dir>/solved.json`, candidates (exact values when solved) | real |
| `bot/engines/search_job.py` | Think ahead search as a sandbox job: validated integer-bitboard gravity search, exact immediate wins, mandatory blocks, anti-feed filtering, forcing double-threat priority, bounded threat evaluation, and terminal endgame solving up to 24 inferred empty cells; MCTS remains for chance and hidden-value modules | real |
| `bot/engines/sim_common.py` | helpers the engine jobs load by path (seat resolution and live check, canonical state text, hidden paths, world sampling by seeds, worlds written back into a state, view of a state without hidden parts, `Clock`) | real |
| `bot/engines/guess.py` | Guessing, parent side: payload and candidates (relative values: the best split is 1.0, every 0.1 bit lost costs 0.05) | real |
| `bot/engines/guess_job.py` | Guessing as a sandbox job: hypotheses = hidden values the simulator can draw consistent with the feedback (sampled by seeds, exact when small), every candidate action applied in every hypothesis, partition by what our view shows, expected information, credit for naming a possible answer, spread sample + neighbors on long action lists | real |
| `bot/engines/simultaneous.py` | Read the opponent, parent side: payload, the opponent's action counts of earlier games as a prior, and the draw of OUR action from the final mixed strategy (seeded from a per-process salt, the game and the turn; never from chat); candidates = drawn action first, the other supported actions inside the margin, probabilities in the note | real |
| `bot/engines/simultaneous_job.py` | Read the opponent as a sandbox job: payoff matrices for the current simultaneous step (exact when the step ends the game, else playouts with common random numbers), regret matching (RM+, linear averaging), opponent frequency model read from the history in the parsed state (orientation found by a probe), capped exploitation (weight at most 0.5) | real |
| `bot/engines/bluff.py` | Bluff, parent side: payload, the draw of ONE action from the softmax probabilities (seeded, never from chat), candidates with EV and probability in the note. OFF by default (no local practice game of the family to measure) | real |
| `bot/engines/bluff_job.py` | Bluff as a sandbox job: sample the hidden worlds by seeds, expected value per action by playouts against a random opponent and, in a share of the samples, one level smarter (it uses its own hidden card), common random numbers, softmax with a temperature tied to spread and noise | real |

## Where things are stored at runtime

In a container, `MEMORY_DIR` (default `/memory`, shared by all our containers
during the event; falls back to a temp folder when not writable):

```
<memory>/<game_id>/<rules_hash>/meta.json         harness_version, label, verified (written last)
<memory>/<game_id>/<rules_hash>/profile.json      GameProfile
<memory>/<game_id>/<rules_hash>/playbook.md       playbook from prep
<memory>/<game_id>/<rules_hash>/sample_turn.json  sample turn from prep (test case for simulators)
<memory>/<game_id>/<rules_hash>/simulator.py      the verified winner (only when verification passed)
<memory>/<game_id>/<rules_hash>/verify.json       verification report: every candidate, checks, failures, winner, playouts per second
<memory>/<game_id>/<rules_hash>/solved.json      Think ahead: solved root positions (tied to the simulator hash)
<memory>/<game_id>/<rules_hash>/live.json        games in which the live check of the engines passed / failed (a variant that keeps failing is switched off)
<memory>/opponents/<team_id>.json                 opponent profiles
<memory>/metrics/<container_id>.jsonl             one line per answer / game end
<memory>/<game_id>/<rules_hash>/strategy.json     aggregate prep self-play findings and safety checks
<memory>/<game_id>/<rules_hash>/background.json    bounded local idle-compute policy hints
```

Locally (arena-lite through `scripts/practice.py`, all ignored by Git):

```
memory/luckydevs/                             MEMORY_DIR of our bot in practice runs (delete it after
                                              changing prep, or bump HARNESS_VERSION); the opponent
                                              has its own folder (memory/opponent, memory/sparring)
results/practice/matches/<id>.json            the replay: rules, state after every step, actions, chat,
                                              timings, warnings (same format as the tournament replays)
results/practice/logs/<id>-luckydevs.log      our bot's stderr plus [arena] notes (prompts with --debug)
results/practice/logs/warm/luckydevs--<game>.log   with --prep: the log of the warm container of a game
```

Unit tests use temporary folders and leave nothing in the repository.
