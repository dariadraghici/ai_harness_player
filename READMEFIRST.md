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
                  [luckydevs-3: prep/codegen -> prep/sandbox -> prep/verify]
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
| `bot/main.py` | entrypoint (`python -u -m bot.main`): protocol loop, reader thread, workers, watchdog, shutdown | real |
| `bot/interfaces.py` | contracts between modules: `GameProfile`, `Candidate`, `Decision`, `ChatLine`, `ChatContext`, `VariantSpec`, `Engine`, the generated-simulator contract, `FAMILIES` | real |
| `bot/config.py` | all constants (deadlines, model, prices, limits) and `BOT_*` settings; `HARNESS_VERSION` | real |
| `bot/ladder.py` | the fallback ladder L3 -> L2 -> L1; legal-action matching; `finalize` (filter + chat cleanup); watchdog `emergency` | real |
| `Dockerfile` | the image: `python:3.12-slim`, copies `bot/`, starts the bot; logo line commented | real |
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
| `bot/clock.py` | hard and soft deadlines derived from the message clocks (same proportions as `config.py`) | real |
| `bot/log.py` | logging to stderr (`BOT_LOG_LEVEL`: debug, info, warning, error) | real |
| `bot/llm.py` | gateway client (stdlib urllib): `set_key`, `complete` (thinking control, hard timeout, never raises), usage and cost per key, fake mode | real |
| `bot/memory.py` | `/memory` store: root with fallback, rules hash, atomic writes, variant save/load with harness version check, `find_variant_by_label`, paths | real |
| `bot/metrics.py` | one JSON line per answer and per game end to `<memory>/metrics/<container>.jsonl` | basic |
| `scripts/_common.py` | shared helpers: image name, secrets parsing (no shell `source`), git checks, where arena-lite is (`BOT_ARENA_LITE` or `participant-kit/arena-lite`) | real |
| `scripts/build.py` | `docker build --platform linux/amd64`, labels the image with the Git commit | real |
| `scripts/push.py` | refuses dirty tree or mismatched image; Harbor login via stdin; push; digest; tag reminder | real |
| `scripts/check_image.py` | automated pre-push checklist (platform, silent start, scripted session, practice match with prep on arena-lite, no secrets in image) | real |
| `scripts/practice.py` | practice match on the organizers' arena-lite with `dev.env` (`--fake`, `--debug`, `--prep`, `--image`, `--opponent random/self/starter`); checks the replay and our logs and prints `RESULT: CLEAN` or the problems | real |
| `scripts/batch_runner.py` | batch runner: plays N practice matches across games and opponents (random, self); prints ASCII metrics table (wins, warnings, timeouts, answer times, cost) | real |
| `scripts/arena_lite.py` | runs any arena-lite command from the repository root and adds `testgames/` to its games, without editing the kit | real |
| `scripts/keyinfo.py` | budget and spend of the dev key | real |
| `tests/__init__.py` | sets `BOT_FAKE_LLM=1` for all tests | real |
| `tests/session_script.py` | `BotProc` (bot as child process) and the scripted arena session, reused by `check_image.py` | real |
| `tests/test_protocol.py` | session, cold start, garbage, warning, null legal actions, watchdog, model failure, closed stdin | real |
| `tests/test_units.py` | memory, ladder, JSON parsing, chat cleanup, defense block, clock proportions | real |
| `tests/test_kit.py` | kit message shapes (example action from kit rules, specialist found by label, variant settings in the prompt) and five fake-LLM matches on arena-lite that must be CLEAN (skipped without the kit) | real |
| `tests/test_batch_runner.py` | unit tests for batch runner: CLI argument parsing, replay parsing, metric aggregation, ASCII table formatting | real |
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
| `bot/prep/pipeline.py` | prep under the time budget: variants in parallel, cache lookup, extension points | basic |
| `bot/prep/classify.py` | family + profile + playbook with one model call; `example_from_rules` heuristic | basic |
| `bot/prep/codegen.py` | simulator generation and repair by the model | stub |
| `bot/prep/verify.py` | simulator tests against sample turns, `VerifyReport` | stub |
| `bot/prep/sandbox.py` | generated code in a killable child process | stub |
| `bot/specialist.py` | per-variant registry: process -> memory -> same variant label (`lookup_by_label`) -> cold prep; `build_variant` (extension point) | real |
| `bot/engines/__init__.py` | package marker | real |
| `bot/engines/base.py` | `BaseEngine`, `NotAvailable`, registry family -> engine | real |
| `bot/engines/search.py` | Think ahead: alpha-beta / MCTS | stub |
| `bot/engines/guess.py` | Guessing: hypothesis filtering | stub |
| `bot/engines/simultaneous.py` | Read the opponent: mixed strategy + opponent model | stub |
| `bot/engines/bluff.py` | Bluff: Monte Carlo over hidden states | stub |

## Where things are stored at runtime

In a container, `MEMORY_DIR` (default `/memory`, shared by all our containers
during the event; falls back to a temp folder when not writable):

```
<memory>/<game_id>/<rules_hash>/meta.json         harness_version, label, verified (written last)
<memory>/<game_id>/<rules_hash>/profile.json      GameProfile
<memory>/<game_id>/<rules_hash>/playbook.md       playbook from prep
<memory>/<game_id>/<rules_hash>/sample_turn.json  sample turn from prep (test case for simulators)
<memory>/<game_id>/<rules_hash>/simulator.py      later (luckydevs-3)
<memory>/<game_id>/<rules_hash>/verify.json       later (luckydevs-3)
<memory>/opponents/<team_id>.json                 opponent profiles
<memory>/metrics/<container_id>.jsonl             one line per answer / game end
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
