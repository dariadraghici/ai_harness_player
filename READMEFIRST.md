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
protocol and the rules of the event), `GAMEPLAN.md` (who builds what, when).

## Quick start

Commands use `python`; on Linux/WSL/macOS type `python3` if `python` is
missing. Run everything from the repository root. Python 3.10 or newer and
Docker (with buildx) are needed. No pip install: the bot uses only the
standard library.

```
# 1. Secrets (once). Ask Sebi for the values. Never commit these files.
cp secrets.local.env.example secrets.local.env     # fill in
cp dev.env.example dev.env                         # LLM_BASE_URL, LLM_API_KEY, LLM_MODEL=haiku

# 2. Unit tests (fake LLM, free, about 15 s)
python -m unittest

# 3. One practice match, local bot, against the random bot
python scripts/practice.py --list
python scripts/practice.py --game tictactoe --fake        # fake LLM, free
python scripts/practice.py --game tictactoe               # real dev key (about 0.002 USD)
python scripts/practice.py --game tictactoe --debug       # also logs every prompt to results/.../luckydevs.stderr.log

# 4. Build the image (linux/amd64)
python scripts/build.py dev

# 5. Practice match with prep, played by the image
python scripts/practice.py --game tictactoe --prep --image dev

# 6. The whole pre-push checklist against an image (fake LLM; add --real for the real key)
python scripts/check_image.py dev

# 7. Dev key budget and spend (never prints the key)
python scripts/keyinfo.py
```

A practice run prints `RESULT: CLEAN` when our bot had no warning, timeout,
crash or stray stdout line. Pushing (`python scripts/push.py t1`) is done by
Sebi only.

The practice page `https://arena.hackaton-2026.dvloper.io/#/practice` needs
the practice token `PRACTICE_TOKEN` from `secrets.local.env`; how the page is
used is not documented in this repository (ask Sebi).

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
                  (process -> /memory -> cold prep of this variant)
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
| `.gitignore` | keeps secrets, `results/`, local memory, caches, editor folders out of Git | real |
| `.dockerignore` | keeps secrets, `.git`, tests, docs, scripts, local arena, results out of the image | real |
| `RULES.md` | rules for every human and agent | real |
| `GAMEPLAN.md` | priorities, owners, milestones, checklists | real |
| `READMEFIRST.md` | this map (everyone adds their own new files to their section) | real |
| `AGENTS.md`, `CLAUDE.md` | 3 lines for coding agents: read RULES, READMEFIRST, ARENA-CONTRACT first | real |
| `README.md` | the one-line README created with the repository (left unchanged) | - |
| `docs/ARENA-CONTRACT.md` | the protocol and event rules (section 2 of the briefing) plus marked notes | real |
| `docs/PROMPT-INITIAL.md` | the briefing this base was built from | real |
| `dev.env.example` | template of `dev.env` (committed, empty values) | real |
| `secrets.local.env.example` | template of `secrets.local.env` (committed, empty values) | real |
| `dev.env` | NOT in Git. Local env file for practice: `LLM_*` and `BOT_*` only | local |
| `secrets.local.env` | NOT in Git. All secrets (model dev key, Harbor robot, practice token) | local |

### luckydevs-1: runtime, operations, measurement, test games

| Path | What it is / what it is for | Status |
|---|---|---|
| `bot/protocol.py` | the only stdout writer; stdout redirect; message parsing; `Request` (exactly one answer, thread-safe) | real |
| `bot/clock.py` | hard and soft deadlines derived from the message clocks (same proportions as `config.py`) | real |
| `bot/log.py` | logging to stderr (`BOT_LOG_LEVEL`: debug, info, warning, error) | real |
| `bot/llm.py` | gateway client (stdlib urllib): `set_key`, `complete` (thinking control, hard timeout, never raises), usage and cost per key, fake mode | real |
| `bot/memory.py` | `/memory` store: root with fallback, rules hash, atomic writes, variant save/load with harness version check, paths | real |
| `bot/metrics.py` | one JSON line per answer and per game end to `<memory>/metrics/<container>.jsonl` | basic |
| `scripts/_common.py` | shared helpers: image name, secrets parsing (no shell `source`), git checks | real |
| `scripts/build.py` | `docker build --platform linux/amd64`, labels the image with the Git commit | real |
| `scripts/push.py` | refuses dirty tree or mismatched image; Harbor login via stdin; push; digest; tag reminder | real |
| `scripts/check_image.py` | automated pre-push checklist (platform, silent start, scripted session, practice match with prep, no secrets in image) | real |
| `scripts/practice.py` | wrapper around `arena_local` with `dev.env` (`--fake`, `--debug`) | real |
| `scripts/keyinfo.py` | budget and spend of the dev key | real |
| `tests/__init__.py` | sets `BOT_FAKE_LLM=1` for all tests | real |
| `tests/session_script.py` | `BotProc` (bot as child process) and the scripted arena session, reused by `check_image.py` | real |
| `tests/test_protocol.py` | session, cold start, garbage, warning, null legal actions, watchdog, model failure, closed stdin | real |
| `tests/test_units.py` | memory, ladder, JSON parsing, chat cleanup, defense block, clock proportions | real |
| `arena_local/__init__.py` | package marker of our local practice arena | real |
| `arena_local/__main__.py` | CLI: `python -m arena_local practice --list / --game ... [--prep] [--image ...] [--opponent random|self]` | real |
| `arena_local/runner.py` | plays a match by the contract: prep with sample_turn, start, turns, warnings, timeouts, end, shutdown; replay JSON | real |
| `arena_local/players.py` | process player (local command or `docker run`), random bot (chats, tries injections), bot env (`LLM_*`/`BOT_*` only) | real |
| `testgames/tictactoe.py` | Think ahead test game, legal actions listed | real |
| `testgames/guess_number.py` | Guessing test game, `legal_actions` is null (format from rules) | real |
| `testgames/rps.py` | Read the opponent test game, simultaneous moves, payoff twist | real |

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
| `bot/specialist.py` | per-variant registry: process -> memory -> cold prep; `build_variant` (extension point) | real |
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

Locally (`arena_local`, all ignored by Git):

```
results/memory/                         MEMORY_DIR of the bot in practice runs (delete it after
                                        changing prep, or bump HARNESS_VERSION)
results/<time>-<game>/replay.json       moves, chat, results, warnings, answer times, exit codes
results/<time>-<game>/luckydevs.stderr.log   our bot's log (prompts too with --debug)
```

Unit tests use temporary folders and leave nothing in the repository.
